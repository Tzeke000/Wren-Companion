"""attention_smooth — TRUE smooth pursuit via the Pixy's HID 0x63 jog protocol.

Built 2026-08-21 evening, Zeke's work order: "build that for everything you
wanna track — people, objects, whatever — as good as it possibly can be."

Why this exists: the WinRT/UVC path only does discrete absolute position hops
(~1/s effective with settle), so pursuit looked steppy and lagged. The EMEET
official app moves the gimbal SMOOTHLY via a vendor HID protocol on interface
MI_04 (usage page 0x83). Protocol reverse-engineered by Romonaga/PixyPilot
(cloned at state/research/PixyPilot, byte-level reference in docs/):

    vector move: 09 63 01 20 00 0c 00 0c <x:f32le> <y:f32le> <z:f32le>
    zero vector = stop; official app STREAMS the vector while dragging.

MEASURED HERE (2026-08-21, frame phase-correlation, tmp_jog_calibrate):
  - one-shot vector reports DO nothing reliable; STREAMED reports move it.
  - x=+10 for 1s -> scene shifts +8.5 deg right  => view rotates 'pan+' way.
  - y=+10 for 1s -> scene shifts +8.4 deg down   => view tilts up.
  - so ~0.85 deg/s per vector unit; app range +/-30 => ~25 deg/s max.
  - ★ WinRT bearing readback DOES NOT SEE jog motion (registers go stale).
    Frames are the only truth while jogging; absolute look_at still works and
    re-syncs hardware position (it snaps).

Control law (same sign structure as the absolute follow loop):
  target at dx>0 (right of center) -> jog x NEGATIVE to rotate view right.
  target at dy>0 (below center)    -> jog y NEGATIVE to tilt view down.

Safety rails (no bearing feedback exists while jogging, so rails are visual):
  - zero vector IMMEDIATELY on: target lost, stale frame, oversized offset
    jump, servo exception, stop request. Never jog blind.
  - lost > _LOST_HOLD_S -> absolute look_at home (resyncs position registers)
    and keep observing from there.
  - watchdog: the vector is only ever written in the servo tick; if the thread
    dies the device auto-stops on stream silence (verified: motion needs the
    stream).
"""
from __future__ import annotations

import struct
import threading
import time
from typing import Any

from tools.tool_registry import register_tool

_VID, _PID = 0x328F, 0x00C0
_REPORT = 32

_UNIT_DEG_S = 0.85          # measured deg/s per vector unit
_MAX_UNITS = 25.0           # app used ~±30; leave margin
_DEADBAND = 0.05            # normalized offset that counts as centered
# ── HYSTERESIS (2026-08-21 late, Zeke: "your head jitters a bit even when I'm
# staying still"): detector/tracker noise hovers right AT the deadband edge,
# so the servo flip-flopped centered<->pursuit every few ticks — visible
# micro-hunting on a static face. Once centered, stay parked until the offset
# is CLEARLY out (or the target is actually moving, rate check below).
_DEADBAND_OUT = 0.09        # exit-centered threshold (enter at _DEADBAND)
_OBJECT_PERIOD_S = 0.05     # 20Hz for object targets (reval path is heavier)
# 40 -> 20 (2026-08-21 late): at gain 40 the servo OSCILLATED on a STATIC
# pyramid — the tracker's box lags the true position while the head moves, so
# hot pursuit chases its own sampling lag past the target, exactly the step-
# loop's overshoot in continuous form. Gentler gain + the sign-flip brake
# below converge instead.
_GAIN_UNITS = 20.0          # units per unit of offset (dx=0.5 -> 10u ≈ 8.5°/s)
# ── PACE MATCHING (2026-08-21 ~21:4x, Zeke watching live: "your head should
# match pace with whatever you want to follow"). P alone always TRAILS a
# moving target — the correction is proportional to how far behind we already
# are. The feedforward D term estimates the target's angular velocity from the
# offset's rate of change and commands that speed ON TOP of the P correction,
# so the head moves WITH the target instead of forever catching up.
# Derivation: offset rate r [1/s] -> target angular velocity r*(HFOV/2) deg/s
# -> units = r*34/0.85 ≈ 40*r.
_KD_UNITS = 40.0            # units per (offset-units/second) of target motion
_D_EMA = 0.4                # smoothing on the rate estimate (tracker jitter)
# ── RATE BASELINE (2026-08-21 jitter fix, part 2): at the 30Hz fast path the
# derivative was (sub-pixel bbox noise)/(0.033s) — a STILL face measured as
# rate ~0.2, above _STATIC_RATE, so the servo saw a phantom mover and the
# feedforward commanded 40*0.2 = 8 units of pace-match for nothing. Rates are
# now measured against an anchor sample at least this old, which divides the
# noise by ~5 while adding ~150ms lag to the D term only (P is untouched).
_RATE_BASELINE_S = 0.15
_STATIC_RATE = 0.10         # |rate| below this = target static -> brake applies
_PERIOD_S = 0.08            # ~12.5Hz servo/stream cadence
_LOST_ZERO_MISSES = 2       # consecutive observe misses -> zero vector
_LOST_HOLD_S = 8.0          # lost this long -> absolute home + keep watching
_HOME = (0.0, 10.0)         # measured level bearing at this desk perch
# ── JOG SOFT LIMITS (2026-08-21 ~21:5x, THE SECOND PRIVACY-POSE SCAR): the
# WinRT actuator's TILT_FLOOR (-60) only guards the ABSOLUTE path — the HID
# jog stream has NO device-side floor, and the PD servo drove the head fully
# down into the privacy pose (black frames, Zeke caught it by LOOKING at me).
# Bearing readback is blind while jogging, so these rails run on the DEAD-
# RECKONED estimate with fat margins: past a limit, only vectors that move
# BACK INSIDE are allowed. Est is seeded from a real bearing read at servo
# start (registers are valid until the first jog).
_EST_TILT_FLOOR = -35.0
_EST_TILT_CEIL = 60.0
_EST_PAN_LIMIT = 115.0
# ── HARD RESYNC (2026-08-21 ~22:0x, minutes after scar #2): dead-reckoning
# alone CANNOT bound the physical pose — est said tilt −14 while Zeke watched
# the head point straight UP (~100° divergence; the 0.85 deg/s/unit constant
# is evidently wrong for sustained/fast streams). Every _RESYNC_S of pursuit,
# zero the jog and SNAP the head to the estimate via the absolute path (write
# is the only sync that exists — readback is blind). Bounded drift, small
# visible hitch, worth it.
_RESYNC_S = 3.0
# Resync gating (same jitter session): the snap exists to bound DEAD-RECKONING
# drift, which only accumulates while actually jogging. On a near-static
# target the head has barely moved since the last resync, est can't have
# drifted, and the 3s snap was pure visible twitch. Skip it until we've
# commanded at least this many degrees of motion since the last snap.
_RESYNC_MIN_DEG = 4.0
# ── STATIC TRIM (2026-08-21 jitter fix, part 3): sub-stiction jog vectors
# move NOTHING (proven live: two frames 6s apart pixel-identical while the
# servo wrote ~3-unit corrections every tick) — so a static face parked just
# outside the deadband would sit there forever while the servo buzzed. When
# the target is static and the PD output is below the stiction floor, make
# ONE absolute look_at correction (sign convention proven in
# attention_follow.correct_toward; base = odometry est because bearing
# readback is jog-blind) and let vision confirm. Est is NOT hand-updated —
# odometry measures the real motion (double-count hazard).
_STICTION_UNITS = 5.0       # |vector| below this doesn't overcome the motor
_TRIM_MIN_S = 1.2           # min gap between trims (let vision catch up)
# static-trim v2 pulse shape. MISSING since v2 was written 2026-08-21 23:2x —
# both names were used in the trim branch and never defined, so every static
# trim raised NameError("_TRIM_PULSE_UNITS") and the branch has NEVER once run.
# Found 2026-08-22 19:5x from st["error"] after Zeke said my head "went a bit
# wild" with two people in frame.
_TRIM_PULSE_UNITS = 8.0     # above stiction (5.0), far below _MAX_UNITS (25)
_TRIM_PULSE_MAX_S = 0.25    # cap one trim at ~8*0.85*0.25 = 1.7 deg; the next
                            # trim (>=_TRIM_MIN_S later) takes the residual
_HFOV_DEG = 68.0
_VFOV_DEG = _HFOV_DEG * 3.0 / 4.0  # 4:3 sensor modes
_TILT_MAX_UNITS = 12.0      # tilt arc is small; full-rate tilt overshoots hard
# ── REAL-TIME FACE FAST PATH (2026-08-21 ~22:1x, Zeke: "track things like my
# face in real time no lag"). The recognizer (InsightFace) updates ~5Hz, so
# every consumer inherits ~200ms staleness — that IS the visible lag. Split:
# identity stays with the slow recognizer; MOTION comes from a TrackerVit
# seeded on the recognized face box and updated on EVERY fresh frame in this
# loop. Recognizer results re-seat the tracker whenever they disagree (IoU),
# so it can't drift onto another face for long. Servo runs faster too.
# 0.05 -> 0.0333 (2026-08-21, Zeke: "make everything 30fps so everything is
# on the same page and doesn't jitter"): capture runs 30fps (iris_runtime
# interval=1/30), so the servo now ticks AT frame rate — every frame gets a
# tracker update and a jog write; nothing beats or aliases against capture.
# Budget per tick: vit ~10ms + odometry ~2ms + HID write ~1ms << 33ms.
_PERSON_PERIOD_S = 1.0 / 30.0   # 30Hz — matched to the capture loop
_FACE_IOU_RESEAT = 0.30
_OBSERVE_STATE_S = 0.5      # slow va.observe cadence while fast path is live
# ── VISUAL ODOMETRY (2026-08-21 ~22:1x, Zeke: "head drifts up and won't
# follow me downward"). Root cause: est integrated COMMANDED velocity with a
# constant that's wrong for sustained streams, so est sank below the tilt
# floor while the head was physically high — the guard then clipped every
# downward command (ratchet up). Fix: measure ACTUAL head motion by phase-
# correlating consecutive downscaled frames (the same math that calibrated
# the jog). 160px wide @ HFOV 68° -> 0.425 deg/px both axes; scene shift +x
# = pan increased, +y = tilt increased (both verified 2026-08-21). Response
# below _ODO_MIN_RESP (blur/motion) skips the update; the 3s resync mops up
# residual drift.
_ODO_DEG_PER_PX = 68.0 / 160.0
_ODO_MIN_RESP = 0.10
# ── LOSS-ON-SKIP (2026-08-25). ⚠ MECHANISM NOT CONFIRMED — READ THIS BEFORE
# CREDITING THE BUG AS FIXED.
# Reasoning that led here: a rejected correlation contributes nothing to est,
# but the head does not stop moving during that interval, so the motion is
# dropped permanently. Every skip would then be a one-way under-count in the
# direction of travel — a bias, not a random walk, which would explain logged
# drifts of -14, -18, -18 (same sign every time), especially since the skip
# trigger is motion blur and so concentrates during fast jogs.
#   What IS verified: the SCALE is innocent. Replaying known-angle frame pairs
#   through this same math gave +10 -> +9.93, +20 -> +19.73, -20 -> -19.74
#   (<1.5% error). _ODO_DEG_PER_PX is correct; recalibrating it fixes nothing.
#   What the LIVE TEST said (08-25, ~4min pursuit + forced head sweeps):
#   204 odometry updates and ZERO skips, while est_drift still reported up to
#   32 deg. So skipping was not happening at all, and cannot be the cause of
#   the drift that was actually observed. This code is therefore INERT on the
#   evidence so far — kept because the counters make the loss visible if it
#   ever does occur, and because treating unknown motion as zero motion is
#   wrong regardless. It is NOT a demonstrated fix for the -14/-18 drift.
#   The stronger lead is that est_drift is computed against a JOG-BLIND
#   readback (see the resync block), so it partly measures legitimate jog
#   motion rather than odometry error. Ground truth on 08-25 after pursuit:
#   measured pan +5.9 / est 5.7 / device 6.0  -- pan agrees within 0.3 deg,
#   measured tilt +8.8 / est 18.3 / device 21.0 -- TILT is the real open
#   discrepancy and needs a physical reference to settle.
# Fix shape if skips ever do appear: do NOT dead-reckon the gap from commanded
# velocity (that is the wrong-rate-constant disease this odometry cured).
# Treat a skip as UNKNOWN motion and go read ground truth — enough skips force
# an early resync against the device's own bearing.
_ODO_SKIP_FORCE = 8          # skips since last resync that force an early one
_ODO_SKIP_MIN_GAP_S = 0.75   # ...but never resync more often than this
# ── OBSERVABILITY (2026-08-22, Zeke: "make the logs tell you what happened").
# The safety rails below are evaluated against `est`, not against measured
# truth. On 08-22 est had wandered ~30-40 deg from the device's own bearing and
# tilt consequently reached +89.5 with _EST_TILT_CEIL=60 nominally in force —
# invisible in every log. Drift beyond this many degrees now writes an
# `est_drift` record to ptz_audit.jsonl at resync time.
_EST_DRIFT_WARN_DEG = 12.0
# Settle time after an absolute resync move before re-reading the device to
# re-anchor est. The move is small (it's correcting <= a few seconds of drift)
# and the resync already costs a visible hitch; this just stops us adopting a
# mid-flight reading as truth.
_REANCHOR_SETTLE_S = 0.18
# Drift beyond this forces a resync on the NEXT opportunity even if the head
# has barely moved (_RESYNC_MIN_DEG gating normally skips it). A big drift on a
# static target means odometry is being fed scene motion that isn't ours —
# people walking through frame — which is exactly when est is least trustworthy
# and exactly when the old code would coast on it. Found with two people in
# frame on 2026-08-22.
_EST_DRIFT_FORCE_DEG = 20.0


def _vec_report(x: float, y: float, z: float = 0.0) -> bytes:
    payload = [0x09, 0x63, 0x01, 0x20, 0x00, 0x0C, 0x00, 0x0C,
               *struct.pack("<fff", x, y, z)]
    return bytes(list(payload) + [0] * (_REPORT - len(payload)))


class _PixyJog:
    """Minimal HID jog writer. Open lazily, self-heal on error, always
    zero-vector on close. NOT a keep-alive thread — the servo loop IS the
    stream (a write per tick)."""

    def __init__(self) -> None:
        self._dev = None
        self._lock = threading.Lock()
        self._burst_active = False  # for the PTZ audit: log burst edges, not 30Hz

    def _open(self):
        import hid
        d = hid.device()
        d.open(_VID, _PID)
        return d

    def write_vector(self, x: float, y: float) -> bool:
        with self._lock:
            try:
                if self._dev is None:
                    self._dev = self._open()
                self._dev.write(_vec_report(x, y))
                # Audit at burst EDGES only (2026-08-22 attributable-motion
                # rule): a jog stream is 12-30Hz — log the transition into
                # motion (with the opening vector) and back to zero.
                moving = bool(x or y)
                if moving != self._burst_active:
                    self._burst_active = moving
                    try:
                        from brain.visual_attention import _ptz_audit
                        _ptz_audit("jog_start" if moving else "jog_stop",
                                   True, x=round(x, 1), y=round(y, 1))
                    except Exception:
                        pass
                return True
            except Exception:
                try:
                    if self._dev is not None:
                        self._dev.close()
                except Exception:
                    pass
                self._dev = None
                return False

    def stop(self) -> None:
        self.write_vector(0.0, 0.0)

    def close(self) -> None:
        with self._lock:
            try:
                if self._dev is not None:
                    self._dev.write(_vec_report(0.0, 0.0))
                    self._dev.close()
            except Exception:
                pass
            self._dev = None


def _hand_centers(g: dict[str, Any]) -> list[tuple[float, float]]:
    """Mean landmark position per visible hand, in frame pixels."""
    hr = g.get("_hand_results")
    hands = hr.get("hands") if isinstance(hr, dict) else None
    out: list[tuple[float, float]] = []
    for h in (hands or []):
        lms = h.get("landmarks_px") or []
        try:
            if lms:
                xs = [float(p[0]) for p in lms]
                ys = [float(p[1]) for p in lms]
                out.append((sum(xs) / len(xs), sum(ys) / len(ys)))
        except Exception:
            continue
    return out


_HAND_NEAR_PX = 130       # hand-to-box-center proximity that counts as "holding"
_HAND_BREAK_TICKS = 8     # consecutive disagreeing ticks (~0.6s) before we act


def _hand_consistency(g: dict[str, Any], st: dict[str, Any], state: dict) -> None:
    """Zeke's rule (2026-08-21): when an object lock ACQUIRES in a hand, bind
    them — afterward, hand visible in one place + 'object' in a completely
    different place means the lock is probably wrong -> drop it and let the
    detector re-find. No hands visible = no evidence, never break on absence.
    (The failure this kills: TrackerVit drifting onto background while the
    real object rides away in the hand.)"""
    try:
        if not str(state.get("target") or "").startswith("object:"):
            st.pop("hand_bound", None)
            return
        from brain import object_lock
        lk = object_lock.status()
        box = lk.get("box")
        if not box or not lk.get("locked"):
            st.pop("hand_bound", None)
            st["hand_breaks"] = 0
            return
        cx, cy = box[0] + box[2] / 2.0, box[1] + box[3] / 2.0
        hands = _hand_centers(g)
        if not hands:
            return  # absence of evidence — hold the binding, don't break it
        near = any(abs(hx - cx) < _HAND_NEAR_PX and abs(hy - cy) < _HAND_NEAR_PX
                   for hx, hy in hands)
        age = float(lk.get("age_s") or 0.0)
        if near and (age < 3.0 or st.get("hand_bound")):
            st["hand_bound"] = True
            st["hand_breaks"] = 0
        elif st.get("hand_bound") and not near:
            st["hand_breaks"] = int(st.get("hand_breaks") or 0) + 1
            if st["hand_breaks"] >= _HAND_BREAK_TICKS:
                object_lock.drop("hand-object consistency: the hand moved and "
                                 "the box did not — lock presumed drifted")
                st["hand_bound"] = False
                st["hand_breaks"] = 0
                st["hand_drops"] = int(st.get("hand_drops") or 0) + 1
    except Exception:
        pass  # consistency layer must never break the servo


def _face_fast_offset(g: dict[str, Any], st: dict[str, Any],
                      state: dict) -> tuple[float, float] | None:
    """20Hz face position: TrackerVit rides the target's face between the
    recognizer's ~5Hz updates; each NEW recognizer box re-seats the tracker
    on IoU disagreement (identity slow, motion fast). Returns (dx, dy) or
    None when there's nothing trustworthy — never guesses."""
    try:
        from brain import frame_store
        from brain.object_lock import _iou, _new_tracker
        res = frame_store.get_buffered_frame(max_age_sec=1.0)
        if res.frame is None:
            return None
        label = str(state.get("target_label") or "").lower()
        if not label:
            t = str(state.get("target") or "")
            label = t.split(":", 1)[1] if ":" in t else t
        frame_new = res.capture_ts > float(st.get("_ft_frame_ts") or 0.0)
        faces = g.get("_face_results") or []
        m = next((f for f in faces
                  if str(f.get("person_id") or "").lower() == label), None)
        bb = (m.get("bbox") or m.get("box")) if m else None
        if bb and len(bb) >= 4:
            x1, y1, x2, y2 = (int(v) for v in bb[:4])
            mb = (float(x1), float(y1),
                  float(max(1, x2 - x1)), float(max(1, y2 - y1)))
            sig = (x1, y1, x2, y2)
            if sig != st.get("_ft_seed_sig"):
                st["_ft_seed_sig"] = sig
                cur = st.get("_ft_box")
                if (st.get("_ft_trk") is None or cur is None
                        or _iou(cur, mb) < _FACE_IOU_RESEAT):
                    trk, _k = _new_tracker()
                    trk.init(res.frame, (int(mb[0]), int(mb[1]),
                                         int(mb[2]), int(mb[3])))
                    st["_ft_trk"] = trk
                    st["_ft_box"] = mb
                    st["_ft_frame_ts"] = res.capture_ts
                    st["_ft_reseats"] = int(st.get("_ft_reseats") or 0) + 1
        trk = st.get("_ft_trk")
        if trk is None:
            return None
        if frame_new:
            ok, box = trk.update(res.frame)
            st["_ft_frame_ts"] = res.capture_ts
            if not ok:
                st["_ft_trk"] = None
                st["_ft_box"] = None
                return None
            st["_ft_box"] = tuple(float(v) for v in box)
        box = st.get("_ft_box")
        if not box:
            return None
        h, w = res.frame.shape[:2]
        cx = box[0] + box[2] / 2.0
        cy = box[1] + box[3] / 2.0
        return ((cx - w / 2.0) / (w / 2.0), (cy - h / 2.0) / (h / 2.0))
    except Exception:
        return None


def _servo_loop(g: dict[str, Any], stop: threading.Event, st: dict[str, Any]) -> None:
    from brain import visual_attention as va

    jog = _PixyJog()
    st.update({"ticks": 0, "writes": 0, "zero_writes": 0, "misses": 0,
               "mode": "acquiring", "last_offset": None, "est_bearing": None,
               "error": None})
    # Seed the dead-reckoned bearing from a REAL read — valid until the first
    # jog (absolute paths keep the registers honest; jogging desyncs them).
    est_pan, est_tilt = float(_HOME[0]), float(_HOME[1])
    try:
        b0 = va.build_actuator().bearing()
        if b0.get("confirmed"):
            est_pan = float(b0.get("pan_deg") or est_pan)
            est_tilt = float(b0.get("tilt_deg") or est_tilt)
    except Exception:
        pass
    lost_since: float | None = None
    homed_while_lost = False
    next_observe_ts = 0.0
    last_resync_ts = time.time()
    _odo_prev = None            # previous downscaled gray (visual odometry)
    _odo_prev_ts = 0.0
    try:
        _act = va.build_actuator()
        if not _act.capabilities().get("can_pan"):
            _act = None
    except Exception:
        _act = None
    try:
        while not stop.is_set():
            t_tick = time.time()
            st["ticks"] += 1
            try:
                state = g.get("_attention_state_obj") or {}
                is_person = str(state.get("target") or "").startswith("person:")
                period = _PERSON_PERIOD_S if is_person else _OBJECT_PERIOD_S
                # ── visual odometry: est tracks MEASURED head motion, not
                # commanded motion (see _ODO_* above). Runs on every fresh
                # frame regardless of pursuit state.
                try:
                    import cv2 as _cv2
                    import numpy as _np
                    from brain import frame_store as _fs
                    _ro = _fs.get_buffered_frame(max_age_sec=1.0)
                    if _ro.frame is not None and _ro.capture_ts > _odo_prev_ts:
                        _small = _np.float32(_cv2.cvtColor(
                            _cv2.resize(_ro.frame, (160, 120)),
                            _cv2.COLOR_BGR2GRAY)) / 255.0
                        if _odo_prev is not None:
                            (_sx, _sy), _resp = _cv2.phaseCorrelate(
                                _odo_prev, _small)
                            if (_resp >= _ODO_MIN_RESP
                                    and abs(_sx) < 60 and abs(_sy) < 60):
                                est_pan += _sx * _ODO_DEG_PER_PX
                                est_tilt += _sy * _ODO_DEG_PER_PX
                                est_pan = max(-150.0, min(150.0, est_pan))
                                est_tilt = max(-60.0, min(90.0, est_tilt))
                                st["est_bearing"] = {
                                    "pan_deg": round(est_pan, 1),
                                    "tilt_deg": round(est_tilt, 1)}
                                st["odo_updates"] = int(
                                    st.get("odo_updates") or 0) + 1
                            else:
                                # UNKNOWN motion, not zero motion. Count it so
                                # the loss is visible and can force a resync.
                                st["odo_skips"] = int(
                                    st.get("odo_skips") or 0) + 1
                                st["odo_skips_since_resync"] = int(
                                    st.get("odo_skips_since_resync") or 0) + 1
                                st["last_odo_skip"] = {
                                    "resp": round(float(_resp), 3),
                                    "sx": round(float(_sx), 1),
                                    "sy": round(float(_sy), 1)}
                        _odo_prev = _small
                        _odo_prev_ts = _ro.capture_ts
                except Exception:
                    pass
                # ── real-time face fast path (person targets): 20Hz TrackerVit
                # position, recognizer only for identity/reseat (see _PERSON_*).
                fast_off = _face_fast_offset(g, st, state) if is_person else None
                # ── GPU guard (measured 2026-08-21: while LOST, observe falls
                # back to OWL-ViT re-acquisition — 12Hz of that pegged the 3060
                # at 100%). Locked tracking is cheap (TrackerVit ~10ms CPU) and
                # runs every tick; lost-mode re-detection runs at ~1Hz. While
                # the fast path is live, observe is state-upkeep only (~2Hz).
                res = None
                if t_tick >= next_observe_ts:
                    res = va.observe(g)
                    state = g.get("_attention_state_obj") or {}
                    if fast_off is not None:
                        next_observe_ts = t_tick + _OBSERVE_STATE_S
                if fast_off is not None:
                    offset = {"dx": fast_off[0], "dy": fast_off[1]}
                    status = "locked"
                elif res is not None:
                    offset = (res or {}).get("offset") or state.get("offset")
                    status = (res or {}).get("status")
                else:
                    stop.wait(period)
                    continue
                if status in ("locked", "seeking") and offset:
                    _hand_consistency(g, st, state)
                    dx = float(offset.get("dx") or 0.0)
                    dy = float(offset.get("dy") or 0.0)
                    st["last_offset"] = {"dx": round(dx, 3), "dy": round(dy, 3)}
                    st["misses"] = 0
                    lost_since = None
                    homed_while_lost = False
                    # Rate estimate FIRST (moved above the centered branch
                    # 2026-08-21 jitter fix): hysteresis needs to know whether
                    # the target is moving, and rates must keep updating while
                    # parked or the exit tick sees a stale-zero rate.
                    now_t = time.time()
                    pv = st.get("_prev_offset") or {}
                    # Derivative against an ANCHOR >= _RATE_BASELINE_S old
                    # (not the previous tick — see _RATE_BASELINE_S above).
                    anc = st.get("_rate_anchor") or {}
                    dt_a = now_t - float(anc.get("ts") or 0.0)
                    rx = float(st.get("_rate_x") or 0.0)
                    ry = float(st.get("_rate_y") or 0.0)
                    if not anc:
                        st["_rate_anchor"] = {"dx": dx, "dy": dy, "ts": now_t}
                    elif dt_a >= _RATE_BASELINE_S:
                        if dt_a < 1.0:
                            raw_rx = (dx - float(anc.get("dx") or 0.0)) / dt_a
                            raw_ry = (dy - float(anc.get("dy") or 0.0)) / dt_a
                            rx = _D_EMA * raw_rx + (1 - _D_EMA) * rx
                            ry = _D_EMA * raw_ry + (1 - _D_EMA) * ry
                        else:
                            rx = ry = 0.0  # stale anchor (came back from lost)
                        st["_rate_anchor"] = {"dx": dx, "dy": dy, "ts": now_t}
                    st["_rate_x"], st["_rate_y"] = rx, ry
                    _static = abs(rx) < _STATIC_RATE and abs(ry) < _STATIC_RATE
                    # Hysteresis: wider exit band once parked on a static
                    # target; a MOVING target exits at the normal band so
                    # pace-matching engages without extra lag.
                    band = _DEADBAND_OUT if (st.get("mode") == "centered"
                                             and _static) else _DEADBAND
                    if abs(dx) <= band and abs(dy) <= band:
                        jog.stop()
                        st["zero_writes"] += 1
                        st["mode"] = "centered"
                        st["_prev_offset"] = {"dx": dx, "dy": dy, "ts": now_t}
                    else:
                        # ── PD control: P centers, D matches the target's pace
                        # (see _KD_UNITS derivation above).
                        ux = -( _GAIN_UNITS * dx + _KD_UNITS * rx)
                        uy = -( _GAIN_UNITS * dy + _KD_UNITS * ry)
                        ux = max(-_MAX_UNITS, min(_MAX_UNITS, ux))
                        uy = max(-_MAX_UNITS, min(_MAX_UNITS, uy))
                        # inside deadband AND target static on that axis ->
                        # freeze it (a centered but MOVING target still needs
                        # the feedforward to stay centered)
                        if abs(dx) <= _DEADBAND and abs(rx) < _STATIC_RATE:
                            ux = 0.0
                        if abs(dy) <= _DEADBAND and abs(ry) < _STATIC_RATE:
                            uy = 0.0
                        # Sign-flip brake — STATIC targets only now: for a
                        # mover, an offset zero-crossing is normal pace-
                        # matching, not ringing.
                        if pv and (dx * float(pv.get("dx") or 0.0)) < 0 \
                                and abs(rx) < _STATIC_RATE:
                            ux *= 0.35
                        if pv and (dy * float(pv.get("dy") or 0.0)) < 0 \
                                and abs(ry) < _STATIC_RATE:
                            uy *= 0.35
                        st["_prev_offset"] = {"dx": dx, "dy": dy, "ts": now_t}
                        # ── static trim v2 (2026-08-21 23:2x, Zeke: "jitters
                        # worse now"): v1 used ABSOLUTE look_at(est + d) — but
                        # any est bias e turns every nudge into a d+e mis-jump,
                        # and odometry propagates e forever (it tracks deltas,
                        # never absolute truth). Same disease as the 100° drift
                        # scar, smaller dose. v2: timed JOG PULSE — relative
                        # motion, the proven jog sign convention, no est in the
                        # loop. Dominant axis only; the next trim (>=1.5s later,
                        # after vision settles) handles the residual.
                        if (_static
                                and abs(ux) < _STICTION_UNITS
                                and abs(uy) < _STICTION_UNITS
                                and now_t - float(st.get("_trim_ts") or 0.0)
                                    >= _TRIM_MIN_S):
                            need_x = abs(dx) * _HFOV_DEG / 2.0
                            need_y = abs(dy) * _VFOV_DEG / 2.0
                            if need_x >= need_y:
                                px = -_TRIM_PULSE_UNITS if dx > 0 else _TRIM_PULSE_UNITS
                                py = 0.0
                                t_pulse = need_x / (_TRIM_PULSE_UNITS * _UNIT_DEG_S)
                            else:
                                px = 0.0
                                py = -_TRIM_PULSE_UNITS if dy > 0 else _TRIM_PULSE_UNITS
                                t_pulse = need_y / (_TRIM_PULSE_UNITS * _UNIT_DEG_S)
                            t_pulse = min(t_pulse, _TRIM_PULSE_MAX_S)
                            # jog soft rails apply to pulses too (privacy scar)
                            if est_tilt <= _EST_TILT_FLOOR and py < 0:
                                py = 0.0
                            if est_tilt >= _EST_TILT_CEIL and py > 0:
                                py = 0.0
                            if est_pan <= -_EST_PAN_LIMIT and px < 0:
                                px = 0.0
                            if est_pan >= _EST_PAN_LIMIT and px > 0:
                                px = 0.0
                            if px or py:
                                jog.write_vector(px, py)
                                stop.wait(t_pulse)
                            jog.stop()
                            st["trims"] = int(st.get("trims") or 0) + 1
                            st["_trim_ts"] = time.time()
                            st["mode"] = "trim"
                            stop.wait(period)
                            continue
                        # ── jog soft limits (see _EST_* above — privacy-pose
                        # scar #2): past a limit, only inward motion passes.
                        if est_tilt <= _EST_TILT_FLOOR and uy < 0:
                            uy = 0.0
                        if est_tilt >= _EST_TILT_CEIL and uy > 0:
                            uy = 0.0
                        if est_pan <= -_EST_PAN_LIMIT and ux < 0:
                            ux = 0.0
                        if est_pan >= _EST_PAN_LIMIT and ux > 0:
                            ux = 0.0
                        uy = max(-_TILT_MAX_UNITS, min(_TILT_MAX_UNITS, uy))
                        # ── hard resync (see _RESYNC_S): re-anchor est to the
                        # device on cadence so dead-reckoning error stays
                        # bounded. Gated on ACTUAL jog effort since the last
                        # snap (_RESYNC_MIN_DEG): no motion -> no drift -> no
                        # twitch. EXCEPT when the last measured drift was large
                        # — then the effort gate is the wrong test, because the
                        # drift came from scene motion (someone walking through
                        # frame) rather than from our own jogging, and coasting
                        # on that est is what put the head on the ceiling.
                        st["_jog_effort_deg"] = (
                            float(st.get("_jog_effort_deg") or 0.0)
                            + (abs(ux) + abs(uy)) * _UNIT_DEG_S * period)
                        _ld = st.get("last_est_drift") or {}
                        _drift_now = max(abs(float(_ld.get("pan") or 0.0)),
                                         abs(float(_ld.get("tilt") or 0.0)))
                        _forced = _drift_now >= _EST_DRIFT_FORCE_DEG
                        # Skipped odometry updates mean est is missing real
                        # motion RIGHT NOW (see _ODO_SKIP_FORCE). Waiting out
                        # the full _RESYNC_S while blind is how the -14/-18
                        # drifts accumulated, so enough skips buy an early
                        # trip to ground truth — floored so we can't thrash.
                        _skip_forced = (
                            int(st.get("odo_skips_since_resync") or 0)
                            >= _ODO_SKIP_FORCE
                            and time.time() - last_resync_ts
                            >= _ODO_SKIP_MIN_GAP_S)
                        if (_act is not None
                                and ((time.time() - last_resync_ts >= _RESYNC_S
                                      and (st["_jog_effort_deg"]
                                           >= _RESYNC_MIN_DEG or _forced))
                                     or _skip_forced)):
                            if _forced:
                                st["forced_resyncs"] = int(
                                    st.get("forced_resyncs") or 0) + 1
                            if _skip_forced:
                                st["skip_forced_resyncs"] = int(
                                    st.get("skip_forced_resyncs") or 0) + 1
                            jog.stop()
                            # Read the device's own bearing BEFORE we overwrite
                            # it. Readback is jog-blind, so this is the last
                            # ABSOLUTE position the device was told about —
                            # comparing it to est is how far dead-reckoning has
                            # wandered since. 2026-08-22: est said pan -23/tilt
                            # -37 while the head was really at pan 8/tilt 5, and
                            # NOTHING logged it — the est-based safety rails
                            # then let tilt reach +89.5 with a +60 ceiling set.
                            _read = None
                            try:
                                _b = _act.bearing() or {}
                                _read = (float(_b.get("pan_deg")),
                                         float(_b.get("tilt_deg")))
                            except Exception:
                                _read = None
                            # ── RE-ANCHOR, not snap-to-estimate (2026-08-22,
                            # Zeke out, explicit go-ahead). THE ROOT CAUSE: this
                            # used to be look_at(est_pan, est_tilt) — driving the
                            # PHYSICAL head to wherever dead-reckoning believed it
                            # was. Sync ran the wrong way: a drifted est didn't get
                            # corrected, it got OBEYED, and the head was dragged to
                            # the drift. That is how tilt reached +89.5 with a +60
                            # ceiling set — est floated up, and every 3s the resync
                            # faithfully drove the head up to meet it.
                            # Now: (1) clamp the commanded pose to the SOFT rails so
                            # a bad est can never command an unsafe pose even once,
                            # (2) after the absolute move, re-read the device and
                            # ADOPT its value as est. Drift is now bounded by ONE
                            # resync interval of odometry error instead of
                            # accumulating without limit.
                            #
                            # MEASURED before trusting it (2026-08-22, empty room):
                            # commanded +10.0 pan by the absolute path; phase-
                            # correlating the before/after frames gave +9.9 deg
                            # actual (scripts/measure_ptz_move.py, response 0.26);
                            # live act.bearing() then read 10.0. Command, physical
                            # world and readback agree to 0.1 deg, so adopting the
                            # post-move readback really is re-anchoring to truth.
                            # (Readback is blind to JOG only — that part stands.)
                            # ⚠ attention_status serves a CACHED bearing and read
                            # pan 0.0 confirmed=true during this same test, 6000s
                            # stale. Use attention_report / act.bearing() live.
                            _safe_pan = max(-_EST_PAN_LIMIT,
                                            min(_EST_PAN_LIMIT, est_pan))
                            _safe_tilt = max(_EST_TILT_FLOOR,
                                             min(_EST_TILT_CEIL, est_tilt))
                            if (_safe_pan != est_pan) or (_safe_tilt != est_tilt):
                                st["rail_clamped_resyncs"] = int(
                                    st.get("rail_clamped_resyncs") or 0) + 1
                            try:
                                _act.look_at(_safe_pan, _safe_tilt)
                                st["resyncs"] = int(st.get("resyncs") or 0) + 1
                                est_pan, est_tilt = _safe_pan, _safe_tilt
                                stop.wait(_REANCHOR_SETTLE_S)   # let the motor land
                                _after = _act.bearing() or {}
                                if _after.get("confirmed"):
                                    _ap = _after.get("pan_deg")
                                    _at = _after.get("tilt_deg")
                                    if _ap is not None and _at is not None:
                                        _corr = max(abs(est_pan - float(_ap)),
                                                    abs(est_tilt - float(_at)))
                                        est_pan, est_tilt = float(_ap), float(_at)
                                        st["reanchors"] = int(
                                            st.get("reanchors") or 0) + 1
                                        st["last_reanchor_deg"] = round(_corr, 1)
                                        st["est_bearing"] = {
                                            "pan_deg": round(est_pan, 1),
                                            "tilt_deg": round(est_tilt, 1)}
                            except Exception:
                                pass
                            if _read is not None:
                                _dp = est_pan - _read[0]
                                _dt = est_tilt - _read[1]
                                st["last_est_drift"] = {"pan": round(_dp, 1),
                                                        "tilt": round(_dt, 1)}
                                st["max_est_drift"] = max(
                                    float(st.get("max_est_drift") or 0.0),
                                    max(abs(_dp), abs(_dt)))
                                # Only log the ones that matter: a drift bigger
                                # than the deadband is a rail-correctness bug.
                                if max(abs(_dp), abs(_dt)) >= _EST_DRIFT_WARN_DEG:
                                    try:
                                        from brain.visual_attention import _ptz_audit
                                        _ptz_audit("est_drift", False,
                                                   est_pan=round(est_pan, 1),
                                                   est_tilt=round(est_tilt, 1),
                                                   read_pan=round(_read[0], 1),
                                                   read_tilt=round(_read[1], 1),
                                                   drift_pan=round(_dp, 1),
                                                   drift_tilt=round(_dt, 1),
                                                   odo_updates=st.get("odo_updates"),
                                                   resyncs=st.get("resyncs"))
                                    except Exception:
                                        pass
                            st["_jog_effort_deg"] = 0.0
                            st["odo_skips_since_resync"] = 0
                            last_resync_ts = time.time()
                        if jog.write_vector(ux, uy):
                            st["writes"] += 1
                            st["mode"] = "pursuit"
                            # est is maintained by VISUAL ODOMETRY above —
                            # command-integration removed 2026-08-21 (its rate
                            # constant was wrong for sustained streams and the
                            # drifted est ratcheted the head upward).
                        else:
                            st["mode"] = "hid_error"
                else:
                    st["misses"] = int(st.get("misses") or 0) + 1
                    if st["misses"] >= _LOST_ZERO_MISSES:
                        jog.stop()
                        st["zero_writes"] += 1
                        st["mode"] = "lost_hold"
                        if lost_since is None:
                            lost_since = time.time()
                        next_observe_ts = time.time() + 1.0  # GPU guard
                    # Sentry-engaged runs auto-stop after prolonged loss so the
                    # motion gate re-arms (mirrors the step-follow's
                    # yield-to-sentry lifecycle; wired 2026-08-21 evening).
                    auto_stop = float(st.get("auto_stop_lost_s") or 0.0)
                    if (auto_stop > 0.0 and lost_since is not None
                            and time.time() - lost_since > auto_stop):
                        st["auto_stopped"] = True
                        break
                    if (lost_since is not None
                            and time.time() - lost_since > _LOST_HOLD_S
                            and not homed_while_lost):
                        # Absolute snap home: recovers view AND resyncs the
                        # position registers the jog left stale.
                        try:
                            act = va.build_actuator()
                            if act.capabilities().get("can_pan"):
                                act.look_at(*_HOME)
                                est_pan, est_tilt = _HOME
                                st["mode"] = "lost_homed"
                        except Exception:
                            pass
                        homed_while_lost = True
            except Exception as e:  # noqa: BLE001 — servo survives anything
                st["error"] = repr(e)
                # 2026-08-22 (Zeke: "make the logs tell you what happened"):
                # this handler used to be the END of the story — the servo ate
                # the exception into st["error"], a field nothing reads, and
                # kept driving the head. static-trim v2 raised NameError on
                # EVERY trim for a full day and no log ever said so, while
                # ptz_audit happily recorded 383/383 commands ok=true.
                # Now: first sighting of each distinct error goes to the audit
                # trail. Dedup on the message so a 12Hz loop can't spam it.
                st["error_count"] = int(st.get("error_count") or 0) + 1
                if st.get("_last_logged_error") != st["error"]:
                    st["_last_logged_error"] = st["error"]
                    try:
                        import traceback as _tb2

                        from brain.visual_attention import _ptz_audit
                        _frames = _tb2.extract_tb(e.__traceback__)
                        _site = ""
                        if _frames:
                            _f = _frames[-1]
                            _fn = (_f.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
                            _site = f"{_fn}:{_f.name}:{_f.lineno}"
                        _ptz_audit("servo_error", False,
                                   error=st["error"],
                                   raised_at=_site,
                                   mode=st.get("mode"),
                                   ticks=st.get("ticks"),
                                   error_count=st.get("error_count"))
                    except Exception:
                        pass
                jog.stop()
            # keep cadence (person 30Hz frame-matched, objects 20Hz)
            try:
                _p = (_PERSON_PERIOD_S
                      if str((g.get("_attention_state_obj") or {})
                             .get("target") or "").startswith("person:")
                      else _OBJECT_PERIOD_S)
            except Exception:
                _p = _PERIOD_S
            dt = time.time() - t_tick
            if dt < _p:
                stop.wait(_p - dt)
    finally:
        jog.close()
        st["mode"] = "stopped"
        st["stopped_ts"] = time.time()


def _attention_smooth(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """attention_smooth(action='start'|'stop'|'status', target?)"""
    action = str(params.get("action") or "status").lower()
    st = g.setdefault("_attention_smooth", {})
    t = st.get("thread")
    running = bool(t is not None and t.is_alive())

    if action == "status":
        out = {k: v for k, v in st.items() if k not in ("thread", "stop")}
        out.update({"ok": True, "running": running})
        return out

    if action == "stop":
        if running:
            st["stop"].set()
            st["thread"].join(timeout=4.0)
        # A stopped pursuit ends the deliberate act — pin dies with it
        # (same contract as the step follow's stop).
        g.pop("_attention_pin", None)
        return {"ok": True, "running": False, "was_running": running,
                "ticks": st.get("ticks"), "writes": st.get("writes")}

    if action == "start":
        from brain import visual_attention as va
        # The step-follow loop and this servo must never both drive the gimbal.
        try:
            from tools.system.attention_follow_tool import _attention_follow
            _attention_follow({"action": "stop"}, g)
        except Exception:
            pass
        target = params.get("target")
        if target:
            r = va.set_target(g, str(target))
            if not r.get("ok"):
                return {"ok": False, "error": f"set_target failed: {r}"}
            # Explicit engage semantics: object targets pin (same rule as
            # attention_engage — deliberate choice beats ambient policy).
            # pin=False opts out — the SENTRY passes it, because an ambient
            # policy engage must never pin (2026-08-21).
            tid = (g.get("_attention_state_obj") or {}).get("target") or target
            if str(tid).startswith("object:") and params.get("pin") is not False:
                g["_attention_pin"] = str(tid)
        elif not (g.get("_attention_state_obj") or {}).get("target"):
            return {"ok": False,
                    "error": "no target — pass target='zeke' or 'object:...'"}
        if running:
            return {"ok": True, "running": True, "note": "already smooth"}
        # 0 = run forever (manual starts); sentry passes ~90 so it re-arms.
        st["auto_stop_lost_s"] = float(params.get("auto_stop_lost_s") or 0.0)
        stop = threading.Event()
        th = threading.Thread(target=_servo_loop, args=(g, stop, st),
                              daemon=True, name="attention_smooth")
        st.update({"stop": stop, "thread": th, "started_ts": time.time()})
        th.start()
        return {"ok": True, "running": True,
                "target": (g.get("_attention_state_obj") or {}).get("target"),
                "period_s": _PERIOD_S, "max_deg_s": _MAX_UNITS * _UNIT_DEG_S}

    return {"ok": False, "error": f"unknown action {action!r} — start|stop|status"}


register_tool(
    "attention_smooth",
    "TRUE smooth pursuit (HID jog protocol, ~12Hz visual servo, up to "
    "~21 deg/s continuous) for ANY target — person or object. "
    "action='start' (+target='zeke'|'object:mug') | 'stop' | 'status'. "
    "Replaces the steppy absolute-move follow for live tracking; object "
    "targets auto-pin. Rails: zero-vector on lost/stale/error, absolute "
    "home+resync after 8s lost. NOTE: WinRT bearing readback cannot see jog "
    "motion — est_bearing in status is dead-reckoned.",
    2,
    _attention_smooth,
)
