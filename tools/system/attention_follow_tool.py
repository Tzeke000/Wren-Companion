"""attention_follow — the slow-pursuit loop for the PTZ eyes (EMEET PIXY).

Built 2026-08-20, the night after the head learned to move. Design contract
(Zeke's split, 2026-08-06): the PROGRAM owns the correction loop, IRIS owns the
intent. `attention_follow start zeke` = intent; everything per-cycle below is
mechanism.

Not 30Hz smooth pursuit — the WinRT control path costs ~0.5s per move, so this
is a deadband follower: check the target's frame offset every ~1.5s, correct
the bearing only when it drifts outside the deadband. Right for "keep facing
Zeke as he moves around the room"; wrong for "track a thrown ball" (that needs
the HID 0x63 jog protocol, unbuilt).

Safety rails:
- target lost -> HOLD position (never hunt, never wander)
- max correction per cycle capped (no violent swings)
- tilt floor enforced by the actuator itself (privacy-pose trap at -90)
- one loop max; start while running = status report, not a second thread
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

# ── Sightings ledger (2026-08-20, Zeke: "have things process without needing
# you") — every lock appends WHERE a person was seen (bearing-tagged), so
# searches can check real hotspots (desk, bed) before blind-sweeping, and I can
# ask attention_report "what have my eyes seen lately" any time.
_SIGHTINGS_PATH = Path(r"D:\Wren-Companion\state\attention_sightings.jsonl")
_SIGHTING_MIN_GAP_S = 10.0
_SIGHTINGS_MAX_BYTES = 2_000_000


def _append_sighting(rec: dict) -> None:
    try:
        if (_SIGHTINGS_PATH.exists()
                and _SIGHTINGS_PATH.stat().st_size > _SIGHTINGS_MAX_BYTES):
            lines = _SIGHTINGS_PATH.read_text(encoding="utf-8").splitlines()[-1000:]
            _SIGHTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with _SIGHTINGS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _recent_sightings(limit: int = 400) -> list[dict]:
    try:
        lines = _SIGHTINGS_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _hotspots(top_n: int = 5, max_age_s: float = 48 * 3600.0) -> list[dict]:
    """Most-frequent (pan,tilt) buckets people appear in, 15-deg grid."""
    now = time.time()
    buckets: dict[tuple, dict] = {}
    for s in _recent_sightings():
        if now - float(s.get("ts") or 0) > max_age_s:
            continue
        key = (round(float(s.get("pan") or 0) / 15.0) * 15,
               round(float(s.get("tilt") or 0) / 15.0) * 15)
        b = buckets.setdefault(key, {"pan": key[0], "tilt": key[1],
                                     "count": 0, "people": {}})
        b["count"] += 1
        p = str(s.get("person") or "?")
        b["people"][p] = b["people"].get(p, 0) + 1
    return sorted(buckets.values(), key=lambda b: -b["count"])[:top_n]

# Measured 2026-08-20 (light-switch landmark, 235px per 25 deg at 640px).
_HFOV_DEG = 68.0
_VFOV_DEG = _HFOV_DEG * 3.0 / 4.0  # 4:3 sensor modes

_DEADBAND = 0.12          # normalized offset that counts as "centered enough"
_GAIN = 0.8               # damped so a bad face box doesn't slingshot the head
_MAX_STEP_DEG = 25.0      # per-cycle correction cap
_MIN_STEP_DEG = 2.0       # ignore micro-corrections
# 1.5 -> 0.9 -> 0.5 (2026-08-20): with the persistent controller moves cost
# ~40ms and face detection refreshes at ~5Hz, so a 0.5s cycle is safe. Zeke's
# lateral-evasion tests kept beating the slower cycles.
_DEFAULT_PERIOD_S = 0.5

# ── Runaway guards (2026-08-20 ~01:0x, after the head walked itself to the
# -150 pan limit at tilt -90): it was chasing a face at a CONSTANT offset —
# almost certainly my own feed on Zeke's monitor (camera chasing its display).
# Three independent rails:
_SOFT_PAN_RANGE = (-125.0, 125.0)   # corrections/search never command beyond
_SOFT_TILT_RANGE = (-40.0, 45.0)    # people-height arc; hardware floor is -60
_MIN_FACE_H_FRAC = 0.08             # faces smaller than this are screens/
                                    # reflections/far phantoms — never chase
_RUNAWAY_N = 5                      # same-direction corrections with a non-
                                    # shrinking error = phantom -> abort chase


# Lost→search tuning (2026-08-20, after Zeke evaded the head by crouching then
# standing fast — the v1 loop just held position and never even knew which way
# he went).
_LOST_AFTER_MISSES = 4        # ~6s at 1.5s period before we start searching
_SEARCH_DWELL_S = 2.0         # linger per search stop so detection can fire
_SEARCH_PAN_STEPS = (0.0, 25.0, -25.0, 50.0, -50.0, 80.0, -80.0, 115.0, -115.0)
_SEARCH_TILT_STEPS = (0.0, -25.0, 15.0)   # relative to last-seen tilt
_SEARCH_ROUNDS = 2            # full sweeps before giving up -> hold at last-seen
_RESEARCH_EVERY_S = 45.0      # while lost-held, retry a sweep this often


def _any_face(g: dict[str, Any]) -> dict | None:
    """Best face currently detected, known OR unknown — a face worth turning
    to. Identification is the recognizer's job once we're pointed at it."""
    faces = list(g.get("_face_results") or [])
    if not faces:
        return None
    return max(faces, key=lambda f: float(f.get("confidence") or 0.0))


def _fire_signal(g: dict[str, Any], ev: str, data: dict) -> None:
    try:
        bus = g.get("_signal_bus")
        if bus is not None:
            bus.fire(ev, data=data, priority="medium")
    except Exception:
        pass


def _loop(g: dict[str, Any], stop: threading.Event, period_s: float) -> None:
    from brain import visual_attention as va

    act = va.build_actuator()
    st8 = g.setdefault("_attention_follow", {})
    st8.update({"cycles": 0, "corrections": 0, "last_offset": None,
                "last_correction": None, "misses": 0, "error": None,
                "mode": "follow", "searches": 0, "last_seen_bearing": None,
                "lost_since": None})
    prev_offset: dict | None = None
    lost_fired = False
    last_search_ts = 0.0

    def correct_toward(dx: float, dy: float) -> None:
        b = act.bearing()
        d_pan = -dx * (_HFOV_DEG / 2.0) * _GAIN
        d_tilt = -dy * (_VFOV_DEG / 2.0) * _GAIN
        d_pan = max(-_MAX_STEP_DEG, min(_MAX_STEP_DEG, d_pan))
        d_tilt = max(-_MAX_STEP_DEG, min(_MAX_STEP_DEG, d_tilt))
        # Soft range: a follow correction may never wedge the head into the
        # corner limits (that's how the display-chase runaway parked at -150).
        new_pan = max(_SOFT_PAN_RANGE[0],
                      min(_SOFT_PAN_RANGE[1], b["pan_deg"] + d_pan))
        new_tilt = max(_SOFT_TILT_RANGE[0],
                       min(_SOFT_TILT_RANGE[1], b["tilt_deg"] + d_tilt))
        if abs(d_pan) >= _MIN_STEP_DEG or abs(d_tilt) >= _MIN_STEP_DEG:
            r = act.look_at(new_pan, new_tilt)
            if r.get("ok"):
                st8["corrections"] += 1
                st8["last_correction"] = {"d_pan": round(d_pan, 1),
                                          "d_tilt": round(d_tilt, 1),
                                          "ts": time.time()}

    def _dwell_for_face(searches: int) -> bool:
        """Wait at the current bearing for a face; True when one shows."""
        deadline = time.time() + _SEARCH_DWELL_S
        while time.time() < deadline:
            if stop.is_set():
                return False
            face = _any_face(g)
            if face is not None:
                st8["mode"] = "follow"
                st8["found_via_search"] = {"person": face.get("person_id"),
                                           "ts": time.time()}
                _fire_signal(g, "follow_target_found",
                             {"person": face.get("person_id"),
                              "searches": searches})
                return True
            stop.wait(0.4)
        return False

    def search(last_bearing: dict, hint: dict | None) -> bool:
        """Find a face: last-seen bearing first, then LEARNED HOTSPOTS (where
        people actually appear in this room), then the blind sweep.
        Returns True if a face was found (head now pointed at it)."""
        st8["mode"] = "searching"
        st8["searches"] += 1
        base_pan = float(last_bearing.get("pan_deg") or 0.0)
        base_tilt = float(last_bearing.get("tilt_deg") or 0.0)
        visited: list[tuple] = []

        def try_stop(pan: float, tilt: float) -> bool | None:
            """None = skip (near a visited stop); else dwell result."""
            pan = max(_SOFT_PAN_RANGE[0], min(_SOFT_PAN_RANGE[1], pan))
            tilt = max(_SOFT_TILT_RANGE[0], min(_SOFT_TILT_RANGE[1], tilt))
            for vp, vt in visited:
                if abs(pan - vp) < 10.0 and abs(tilt - vt) < 10.0:
                    return None
            visited.append((pan, tilt))
            act.look_at(pan, tilt)
            return _dwell_for_face(st8["searches"])

        # 1. Learned hotspots, most-frequent first (skip none — cheap, likely).
        for h in _hotspots():
            if stop.is_set():
                return False
            r = try_stop(float(h["pan"]), float(h["tilt"]))
            if r:
                return True
        # 2. Sweep around last-seen, biased by the direction they vanished
        #    (dx,dy of the final offset before loss: crouch = down first).
        tilt_steps = list(_SEARCH_TILT_STEPS)
        pan_steps = list(_SEARCH_PAN_STEPS)
        if hint:
            if float(hint.get("dy") or 0.0) > 0.10:
                tilt_steps.sort(key=lambda t: t)      # downward first
            elif float(hint.get("dy") or 0.0) < -0.10:
                tilt_steps.sort(key=lambda t: -t)     # upward first
            if float(hint.get("dx") or 0.0) > 0.10:
                pan_steps.sort(key=lambda p: p)       # frame-right = pan-minus first
            elif float(hint.get("dx") or 0.0) < -0.10:
                pan_steps.sort(key=lambda p: -p)
        for _round in range(_SEARCH_ROUNDS):
            for dp in pan_steps:
                for dt in tilt_steps:
                    if stop.is_set():
                        return False
                    r = try_stop(base_pan + dp, base_tilt + dt)
                    if r:
                        return True
        # Exhausted: return to last-seen bearing and hold.
        act.look_at(base_pan, base_tilt)
        st8["mode"] = "lost_hold"
        return False

    while not stop.is_set():
        try:
            # A gesture (nod/shake/glide) temporarily owns the gimbal.
            if time.time() < float(st8.get("gesture_until") or 0.0):
                stop.wait(period_s)
                continue
            st8["cycles"] += 1
            res = va.observe(g)
            state = g.get("_attention_state_obj") or {}
            offset = (res or {}).get("offset") or state.get("offset")
            status = (res or {}).get("status")
            # Phantom filter: a "face" under ~8% of frame height is a screen,
            # reflection, or someone in another postcode — never chase it.
            bbox = state.get("last_bbox")
            if offset and bbox and len(bbox) == 4:
                try:
                    if (float(bbox[3]) - float(bbox[1])) < _MIN_FACE_H_FRAC * 480.0:
                        offset = None
                        st8["phantom_skips"] = int(st8.get("phantom_skips") or 0) + 1
                except Exception:
                    pass
            if status in ("locked", "seeking") and offset:
                dx = float(offset.get("dx") or 0.0)
                dy = float(offset.get("dy") or 0.0)
                # Runaway detector: N same-direction pan corrections while the
                # error refuses to shrink = chasing something glued to my own
                # view (display recursion). Abort: go home, declare lost.
                hist = st8.setdefault("_chase_hist", [])
                hist.append(dx)
                del hist[:-_RUNAWAY_N]
                if (len(hist) == _RUNAWAY_N
                        and all(abs(v) > 0.4 for v in hist)
                        and all((v > 0) == (hist[0] > 0) for v in hist)
                        and abs(hist[-1]) >= abs(hist[0]) - 0.05):
                    st8["runaways"] = int(st8.get("runaways") or 0) + 1
                    hist.clear()
                    act.home()
                    st8["mode"] = "lost_hold"
                    st8["misses"] = _LOST_AFTER_MISSES
                    # Cooldown (2026-08-20 01:4x): without it the loop churned
                    # chase->abort->search->refind->chase (30 searches in min-
                    # utes = the "eyes doing weird things"). After an abort,
                    # sit at home and just watch for a while.
                    st8["runaway_cooldown_until"] = time.time() + 60.0
                    _fire_signal(g, "follow_runaway_aborted",
                                 {"target": state.get("target")})
                    stop.wait(period_s)
                    continue
                prev_offset = {"dx": dx, "dy": dy}
                st8["last_offset"] = {"dx": dx, "dy": dy, "status": status}
                b_now = act.bearing()
                st8["last_seen_bearing"] = b_now
                st8["mode"] = "follow"
                st8["lost_since"] = None
                lost_fired = False
                # ── Wander (2026-08-20, Zeke: "when you get bored, find and
                # switch to a different one"). UNTESTED-MULTI: built with one
                # person in the room; needs a second face to prove.
                if st8.get("wander"):
                    now = time.time()
                    st8.setdefault("following_since", now)
                    span = float(st8.get("interest_span_s") or 240.0)
                    if now - float(st8["following_since"]) > span:
                        cur = str(state.get("target_label")
                                  or state.get("target") or "")
                        others = {str(f.get("person_id"))
                                  for f in (g.get("_face_results") or [])
                                  if f.get("person_id")
                                  and str(f.get("person_id")) not in (cur, "unknown")}
                        if others:
                            hist = st8.setdefault("wander_history", {})
                            pick = min(others, key=lambda p: hist.get(p, 0.0))
                            hist[cur] = now
                            va.set_target(g, pick)
                            st8["following_since"] = now
                            _fire_signal(g, "follow_wander_switch",
                                         {"from": cur, "to": pick})
                if status == "locked":
                    now = time.time()
                    if now - float(st8.get("_last_sighting_ts") or 0.0) >= _SIGHTING_MIN_GAP_S:
                        st8["_last_sighting_ts"] = now
                        _append_sighting({
                            "ts": now,
                            "person": (state.get("target_label")
                                       or state.get("target") or "?"),
                            "pan": round(float(b_now.get("pan_deg") or 0.0), 1),
                            "tilt": round(float(b_now.get("tilt_deg") or 0.0), 1)})
                if abs(dx) > _DEADBAND or abs(dy) > _DEADBAND:
                    correct_toward(dx, dy)
                st8["misses"] = 0
            else:
                st8["misses"] = int(st8.get("misses") or 0) + 1
                if st8["misses"] == _LOST_AFTER_MISSES:
                    st8["lost_since"] = time.time()
                    if not lost_fired:
                        # The signal Zeke asked for: I now KNOW I lost him.
                        _fire_signal(g, "follow_target_lost",
                                     {"target": state.get("target"),
                                      "last_offset": prev_offset,
                                      "last_bearing": st8.get("last_seen_bearing")})
                        lost_fired = True
                should_search = (
                    st8["misses"] >= _LOST_AFTER_MISSES
                    and st8.get("last_seen_bearing")
                    and time.time() > float(st8.get("runaway_cooldown_until") or 0.0)
                    and (st8.get("mode") != "lost_hold"
                         or time.time() - last_search_ts > _RESEARCH_EVERY_S))
                if should_search:
                    last_search_ts = time.time()
                    if search(st8["last_seen_bearing"], prev_offset):
                        st8["misses"] = 0
        except Exception as e:  # noqa: BLE001 — loop must survive anything
            st8["error"] = repr(e)
        stop.wait(period_s)
    st8["stopped_ts"] = time.time()


def _attention_follow(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "status").lower()
    st8 = g.get("_attention_follow") or {}
    running = bool(st8.get("thread") and st8["thread"].is_alive())

    if action == "status":
        out = {k: v for k, v in st8.items() if k not in ("thread", "stop")}
        out.update({"ok": True, "running": running})
        return out

    if action == "stop":
        if running:
            st8["stop"].set()
            st8["thread"].join(timeout=5.0)
        return {"ok": True, "running": False, "was_running": running,
                "cycles": st8.get("cycles"), "corrections": st8.get("corrections")}

    if action == "start":
        from brain import visual_attention as va
        target = params.get("target")
        if target:
            r = va.set_target(g, str(target))
            if not r.get("ok"):
                return {"ok": False, "error": f"set_target failed: {r}"}
        elif not (g.get("_attention_state_obj") or {}).get("target"):
            return {"ok": False,
                    "error": "no target set — pass target='zeke' (or object:...)"}
        if running:
            return {"ok": True, "running": True, "note": "already following",
                    "cycles": st8.get("cycles")}
        period = float(params.get("period_s") or _DEFAULT_PERIOD_S)
        period = max(0.5, min(10.0, period))
        stop = threading.Event()
        t = threading.Thread(target=_loop, args=(g, stop, period),
                             daemon=True, name="attention_follow")
        st8 = g.setdefault("_attention_follow", {})
        st8["stop"] = stop
        st8["thread"] = t
        st8["period_s"] = period
        st8["started_ts"] = time.time()
        st8["wander"] = bool(params.get("wander"))
        if params.get("interest_span_s"):
            st8["interest_span_s"] = float(params["interest_span_s"])
        t.start()
        return {"ok": True, "running": True, "period_s": period,
                "target": (g.get("_attention_state_obj") or {}).get("target")}

    return {"ok": False, "error": f"unknown action {action!r} — start|stop|status"}


# ── Gestures + variable speed (2026-08-20 ~01:1x, Zeke's list) ───────────────
# The persistent controller makes moves ~40ms, so smooth motion = many small
# absolute steps. That gives us variable SPEED (the UVC absolute interface has
# none) and expressive gestures: nod, shake, glide.

def _bearing_of(act) -> tuple[float, float]:
    b = act.bearing()
    return float(b.get("pan_deg") or 0.0), float(b.get("tilt_deg") or 0.0)


def _smooth_move(act, pan_to: float, tilt_to: float,
                 speed_deg_s: float = 60.0, step_hz: float = 15.0) -> None:
    """Glide to a bearing at a chosen speed via interpolated absolute steps."""
    pan0, tilt0 = _bearing_of(act)
    dist = max(abs(pan_to - pan0), abs(tilt_to - tilt0))
    if dist < 0.5:
        return
    speed = max(5.0, min(300.0, float(speed_deg_s)))
    steps = max(1, int((dist / speed) * step_hz))
    for i in range(1, steps + 1):
        f = i / steps
        act.look_at(pan0 + (pan_to - pan0) * f, tilt0 + (tilt_to - tilt0) * f)
        time.sleep(1.0 / step_hz)


def _gesture_guard(g: dict[str, Any], seconds: float) -> None:
    """Tell the follow loop to keep its hands off the gimbal briefly."""
    st8 = g.setdefault("_attention_follow", {})
    st8["gesture_until"] = time.time() + seconds


def _attention_gesture(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Expressive head moves: nod (yes), shake (no), glide (variable-speed
    look). Runs inline (a nod is ~2s). Follow loop pauses corrections while a
    gesture owns the gimbal, then resumes tracking."""
    from brain import visual_attention as va
    act = va.build_actuator()
    caps = act.capabilities()
    if not caps.get("can_pan"):
        return {"ok": False, "error": "no PTZ actuator — cannot gesture"}
    kind = str(params.get("gesture") or params.get("action") or "").lower()
    # Zeke's legibility findings (2026-08-20 01:3x, watching the real head):
    # fewer than 3 reps reads as a PAN, not a gesture — so 3 is the floor.
    # And tilt needs more amplitude than pan to be visible: nods at 8 deg were
    # invisible; 16 shows.
    times = max(3, min(6, int(params.get("times") or 3)))
    amp = float(params.get("amplitude_deg") or (16 if kind == "nod" else 12))
    amp = max(3.0, min(25.0, amp))
    speed = float(params.get("speed_deg_s") or (110.0 if kind == "nod" else 80.0))

    # ★ MEASURED 2026-08-20 01:5x: interpolated micro-steps DON'T WORK for
    # gestures — commands ~66ms apart overwrite each other before the gimbal
    # physically travels, so the nod returned ok=True while the head never
    # visibly moved (Zeke watched it not happen, twice). Gestures now command
    # the EXTREME and DWELL for physical travel before reversing. Interpolation
    # stays only in glide, where it makes motion SLOWER than natural, not faster.
    dwell = float(params.get("dwell_s") or 0.45)
    if kind == "nod":
        _gesture_guard(g, times * dwell * 2 + 3)
        pan0, tilt0 = _bearing_of(act)
        for _ in range(times):
            act.look_at(pan0, tilt0 - amp)   # down-stroke (the emphatic one)
            time.sleep(dwell)
            act.look_at(pan0, tilt0 + amp * 0.5)
            time.sleep(dwell)
        act.look_at(pan0, tilt0)
        return {"ok": True, "gesture": "nod", "times": times,
                "amplitude_deg": amp, "dwell_s": dwell}
    if kind == "shake":
        _gesture_guard(g, times * dwell * 2 + 3)
        pan0, tilt0 = _bearing_of(act)
        for _ in range(times):
            act.look_at(pan0 + amp, tilt0)
            time.sleep(dwell)
            act.look_at(pan0 - amp, tilt0)
            time.sleep(dwell)
        act.look_at(pan0, tilt0)
        return {"ok": True, "gesture": "shake", "times": times,
                "amplitude_deg": amp, "dwell_s": dwell}
    if kind == "glide":
        pan_to = float(params.get("pan_deg") or 0.0)
        tilt_to = float(params.get("tilt_deg") or 0.0)
        pan_to = max(_SOFT_PAN_RANGE[0], min(_SOFT_PAN_RANGE[1], pan_to))
        tilt_to = max(_SOFT_TILT_RANGE[0], min(_SOFT_TILT_RANGE[1], tilt_to))
        dist = max(abs(pan_to), abs(tilt_to)) + 1
        _gesture_guard(g, dist / max(5.0, speed) + 3)
        _smooth_move(act, pan_to, tilt_to, speed)
        return {"ok": True, "gesture": "glide",
                "bearing": {"pan_deg": pan_to, "tilt_deg": tilt_to},
                "speed_deg_s": speed}
    return {"ok": False,
            "error": f"unknown gesture {kind!r} — nod | shake | glide"}


def _attention_report(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Pull-side interface: what have my eyes seen / done lately."""
    from brain import visual_attention as va
    st8 = g.get("_attention_follow") or {}
    state = g.get("_attention_state_obj") or {}
    now = time.time()
    window_s = float(params.get("window_s") or 24 * 3600.0)
    recent = [s for s in _recent_sightings()
              if now - float(s.get("ts") or 0) <= window_s]
    by_person: dict[str, dict] = {}
    for s in recent:
        p = str(s.get("person") or "?")
        d = by_person.setdefault(p, {"count": 0, "last_ts": 0.0})
        d["count"] += 1
        d["last_ts"] = max(d["last_ts"], float(s.get("ts") or 0))
    for d in by_person.values():
        d["last_seen_min_ago"] = round((now - d["last_ts"]) / 60.0, 1)
        d.pop("last_ts", None)
    try:
        bearing = va.build_actuator().bearing()
    except Exception as e:
        bearing = {"error": repr(e)}
    return {"ok": True,
            "bearing": bearing,
            "target": state.get("target"),
            "follow_running": bool(st8.get("thread")
                                   and st8["thread"].is_alive()),
            "mode": st8.get("mode"),
            "follow_stats": {k: st8.get(k) for k in
                             ("cycles", "corrections", "searches", "misses",
                              "last_offset", "last_correction",
                              "found_via_search", "lost_since", "error")},
            "sightings_window_h": round(window_s / 3600.0, 1),
            "sightings_by_person": by_person,
            "hotspots": _hotspots()}


register_tool(
    "attention_follow",
    "FOLLOW LOOP for my PTZ eyes: keep facing a target as it moves (deadband "
    "pursuit ~1.5s cycle — faces, not thrown balls). action='start' "
    "(+target='zeke'|'object:mug', period_s opt) | 'stop' | 'status'. "
    "Lost = fires follow_target_lost signal, searches learned hotspots + "
    "last-seen sweep (direction-biased), then holds. Never wanders.",
    2,
    _attention_follow,
)

register_tool(
    "attention_gesture",
    "Expressive PTZ head moves: gesture='nod' (yes) | 'shake' (no) | 'glide' "
    "(variable-speed look: pan_deg, tilt_deg, speed_deg_s 5-300). Optional "
    "times, amplitude_deg. Follow loop yields the gimbal during a gesture and "
    "resumes after. Anticipation + settle baked in (Disney timing, v0).",
    1,
    _attention_gesture,
)

register_tool(
    "attention_report",
    "What have my eyes seen lately: current bearing/target/mode, follow-loop "
    "stats, bearing-tagged sightings by person (window_s opt, default 24h), "
    "and learned room hotspots. My pull-side interface to the eyes.",
    1,
    _attention_report,
)
