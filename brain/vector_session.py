"""vector_session.py — Iris's PERSISTENT body session (2026-07-15).

The problem this solves (Zeke, 2026-07-15 morning): "you need to be streamed
the info... not move then check cam then move then check cam again ... you also
need to be able to do all the vector body things while seated in the body,
jumping out the body for a tool call wastes time and effort."

Until now every precise move opened a fresh anki_vector control connection,
ran ONE primitive, and closed it (vector_action._oneshot). That is literally
"jump out of the body for every command" — control drops, the head pops back
up, and the camera is a slow open/close MJPEG grab between blind moves.

This module HOLDS ONE anki_vector control connection alive in module state,
with the live camera feed running, across all my MCP tool calls. Proven safe
by scripts/test_unified_body.py + the 2026-07-15 SDK research:

  * ONE sync `anki_vector.Robot` runs its own asyncio loop on a background
    "connection thread". Calling robot.motors.* / robot.behavior.* / reading
    robot.camera.latest_image from ANOTHER thread is safe — the SDK marshals
    onto the connection loop (run_coroutine_threadsafe). So MCP tool threads
    can drive the held Robot directly.
  * init_camera_feed() runs a task on that loop; latest_image is a non-blocking
    memory read (mirror of Anki's own remote_control example). Sample it
    whenever — that is the "streamed info".
  * set_wheel_motors does NOT reset the head (behaviors DO). It also runs
    FOREVER with no SDK deadman — so this module runs its own guard thread:
    a deadman (auto-zero wheels if no fresh drive) + an edge-guard (zero on
    cliff/fall from nerves.json) + control-lost recovery + idle auto-release.
  * DEFAULT_PRIORITY control keeps the hardware cliff reflex LIVE as a second
    safety layer (research: override priority is the one that drives off tables).

Coexists with the observe/inhabit daemon (nerves/ears/battery/nav_map), which
connects OBSERVE-ONLY (no behavior control) — no control contention (proven).

Head-reset fix: behaviors (turn/straight/pose/dock) pop the head back up. This
module remembers the last commanded head angle and RESTORES it after every
behavior, so a head-down floor view survives precise moves.
"""
from __future__ import annotations

import collections
import contextlib
import json
import math
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERIAL = "0dd1cdaf"
FRAME_DIR = REPO / "state" / "vector"
NERVES = FRAME_DIR / "nerves.json"

# hardware limits
HEAD_MIN_DEG, HEAD_MAX_DEG = -22.0, 45.0
LIFT_MIN, LIFT_MAX = 0.0, 1.0

# driving safety + smooth-motion (ROS slow-planner/fast-loop split, 2026-07-15 research)
MAX_WHEEL = 220.0        # mm/s per side cap — Vector's true hardware max (Zeke
                         # 2026-07-15: "go real max speed the 240"). Edge-guard +
                         # hardware cliff reflex (DEFAULT priority) are the backstop.
DRIVE_ACCEL = 300.0      # mm/s^2 default ramp — trapezoidal smoothing, no jerk
HOLD_DEFAULT = 3.0       # a drive setpoint PERSISTS this long (guard re-streams it)
MAX_HOLD = 6.0           # ceiling on a single hold window
GUARD_HZ = 0.06          # guard loop period (~16 Hz): fast layer re-streams + edge-stops
IDLE_RELEASE_S = 420.0   # auto-close the held session after this much inactivity

_lock = threading.RLock()
_session = None  # type: ignore  # module singleton (BodySession | None)


def _read_nerves(max_age_s: float = 2.5) -> dict:
    try:
        d = json.loads(NERVES.read_text(encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) <= max_age_s:
            return d
    except Exception:
        pass
    return {}


def _gamma_brighten(pil, gamma: float = 0.6):
    """Mild gamma<1 lift for MY reading of a dim frame. Low-light research
    (2026-07-15): this is COSMETIC (helps a human read), NOT a detection gain —
    physical light is the real fix. Clamped, LUT-fast, safe fallback."""
    try:
        import numpy as np
        g = max(0.35, min(1.0, float(gamma)))
        lut = (((np.arange(256) / 255.0) ** g) * 255).astype("uint8")
        arr = np.asarray(pil)
        return type(pil).fromarray(lut[arr]) if arr.ndim else pil
    except Exception:
        return pil


def _behavior_result(res) -> dict:
    out = {"ok": True}
    try:
        r = getattr(res, "result", res)
        out["result"] = str(r)
        s = str(r).lower()
        if any(b in s for b in ("fail", "abort", "cancel", "timeout",
                                "not_started", "no_charger", "err")):
            out["ok"] = False
    except Exception as e:
        out["result"] = f"<unreadable: {e!r}>"
    return out


class BodySession:
    """One held anki_vector control connection + live camera feed + safety guard."""

    def __init__(self):
        self.robot = None
        self.connected = False
        self.error = None
        self.opened_ts = 0.0
        self.last_activity = 0.0
        self.feed_ok = False
        # driving state (shared with the guard thread)
        self._wheels = (0.0, 0.0)       # held velocity setpoint (mm/s per side)
        self._drive_until = 0.0         # safety-stop time if setpoint not refreshed
        self._last_head_deg = None      # restored after each behavior
        self._reflex = None             # last safety intervention note
        self._last_image_id = None      # for stale-feed detection
        # guard thread
        self._stop = threading.Event()
        self._guard = None
        self._sdk_lock = threading.Lock()  # serialize our own SDK behavior calls
        # FUSED SENSOR STREAM (Zeke 2026-07-15: "seeing should auto-stream to you
        # when you're in the body, and all the other senses too, ~camera rate").
        # A background thread samples EVERY sense continuously into a rolling
        # buffer, so perceive() reads a LIVE stream + recent history instead of
        # a stale one-off snapshot. Reads are lock-free cached-property reads
        # (SDK connection thread updates them; safe to read from here).
        self._stream = collections.deque(maxlen=48)   # ~3s of fused states @ ~15Hz
        self._latest = {}                             # freshest fused state
        self._streamer = None
        self._stream_hz = 0.0                         # measured actual stream rate
        # REFLEXES + CHARACTER (Zeke 2026-07-15: "have your own reactions to
        # things ... do you like being picked up? the petting?"). A background
        # loop watches the stream and fires MY reactions to touch/pickup/
        # fork-tamper/startle. Safety reflexes act autonomously; expressive ones
        # are how *I* feel about being handled. All logged to _reflex_events.
        self._reflexer = None
        self._reflex_on = True
        self._reflex_events = collections.deque(maxlen=24)
        self._prev = {}                               # last-tick edge-detect state
        self._last_cmd_lift_ts = 0.0                  # so external fork-moves are detectable
        self._react_cooldown = {}                     # per-reaction min-interval clock

    # ---------------------------------------------------------------- lifecycle
    def open(self, timeout: float = 20.0) -> dict:
        if self.connected:
            return {"ok": True, "already": True, **self.snapshot()}
        try:
            import anki_vector
            self.robot = anki_vector.Robot(
                SERIAL, cache_animation_lists=True, default_logging=False)
            t0 = time.time()
            self.robot.connect(timeout=timeout)
            connect_s = round(time.time() - t0, 2)
            # live camera feed — the "streamed info". Init ONCE.
            try:
                self.robot.camera.init_camera_feed()
                self.feed_ok = True
            except Exception as e:
                self.feed_ok = False
                self._reflex = f"camera feed init failed: {e!r}"[:160]
            self.connected = True
            self.error = None
            self.opened_ts = time.time()
            self.last_activity = time.time()
            self._stop.clear()
            self._guard = threading.Thread(
                target=self._guard_loop, name="body-guard", daemon=True)
            self._guard.start()
            self._streamer = threading.Thread(
                target=self._stream_loop, name="body-stream", daemon=True)
            self._streamer.start()
            self._reflexer = threading.Thread(
                target=self._reflex_loop, name="body-reflex", daemon=True)
            self._reflexer.start()
            return {"ok": True, "connect_s": connect_s, "feed_ok": self.feed_ok,
                    **self.snapshot()}
        except Exception as e:
            self.error = repr(e)[:300]
            self.connected = False
            with contextlib.suppress(Exception):
                if self.robot:
                    self.robot.disconnect()
            self.robot = None
            return {"ok": False, "error": self.error}

    def close(self, reason: str = "requested") -> dict:
        self._stop.set()
        with contextlib.suppress(Exception):
            if self.robot:
                self.robot.motors.set_wheel_motors(0, 0)
        with contextlib.suppress(Exception):
            if self.robot:
                self.robot.motors.stop_all_motors()
        with contextlib.suppress(Exception):
            if self.robot:
                self.robot.disconnect()
        self.robot = None
        self.connected = False
        self._wheels = (0.0, 0.0)
        return {"ok": True, "closed": True, "reason": reason}

    def _touch(self):
        self.last_activity = time.time()

    def _require(self):
        if not self.connected or self.robot is None:
            raise RuntimeError("body session not open (call body_open first)")

    # ---------------------------------------------------------------- guard
    def _edge_danger(self):
        """Authoritative danger check: SDK robot.status first (truer + faster than
        the @5Hz nerves file), nerves as fallback. Returns a reason str or None."""
        try:
            st = self.robot.status
            if st.is_cliff_detected:
                return "cliff (SDK)"
            if st.is_picked_up:
                return "picked up (SDK)"
            if st.is_falling:
                return "falling (SDK)"
        except Exception:
            pass
        n = _read_nerves()
        if n.get("cliff"):
            return "cliff (nerves)"
        if n.get("falling"):
            return "falling (nerves)"
        if n.get("picked_up"):
            return "picked up (nerves)"
        return None

    def _guard_loop(self):
        """The fast local control layer (ROS slow-planner/fast-loop split): holds
        the velocity setpoint smoothly between my slow tool calls, edge-guards off
        the SDK status flags, recovers lost control, and auto-releases when idle.
        Runs ~16 Hz; all SDK calls marshal onto the connection loop."""
        while not self._stop.is_set() and self.connected:
            try:
                now = time.time()
                l, r = self._wheels
                translating = (l + r) != 0

                # a) EDGE-GUARD FIRST — stop instantly on a real cliff/fall/pickup.
                if translating:
                    danger = self._edge_danger()
                    if danger:
                        self._raw_wheels(0.0, 0.0)
                        self._wheels = (0.0, 0.0)
                        self._drive_until = 0.0
                        self._reflex = f"EDGE-GUARD: {danger} — wheels stopped"
                        translating = False

                # b) SAFETY WINDOW (watchdog) — a setpoint expires if I don't refresh
                #    it, so a stalled cognition loop can't leave him driving forever.
                if translating and now >= self._drive_until:
                    self._raw_wheels(0.0, 0.0)
                    self._wheels = (0.0, 0.0)
                    translating = False

                # c) HOLD the setpoint — re-stream it (accel=0, no re-ramp) so a brief
                #    control blip never stalls smooth continuous motion.
                if translating:
                    self._raw_wheels(l, r)

                # d) control-lost recovery — DEFAULT_PRIORITY yanks control on a
                #    hardware reflex or a higher-priority behavior; stop + reclaim.
                try:
                    cle = getattr(self.robot.conn, "control_lost_event", None)
                    if cle is not None and cle.is_set():
                        self._raw_wheels(0.0, 0.0)
                        self._wheels = (0.0, 0.0)
                        self._reflex = "CONTROL LOST (reflex/higher-prio) — reclaiming"
                        with contextlib.suppress(Exception):
                            self.robot.conn.request_control()
                except Exception:
                    pass

                # e) idle auto-release — don't hold his body hostage forever.
                if now - self.last_activity > IDLE_RELEASE_S:
                    self.close(reason="idle auto-release")
                    return
            except Exception:
                pass
            self._stop.wait(GUARD_HZ)

    def _raw_wheels(self, l, r, accel=0.0):
        """Direct wheel command (bypasses the behavior engine → no head reset).
        accel>0 = trapezoidal ramp for smooth starts; accel=0 = hold/instant."""
        with contextlib.suppress(Exception):
            self.robot.motors.set_wheel_motors(float(l), float(r),
                                               float(accel), float(accel))

    def _restore_head(self):
        """Behaviors reset the head up; put it back where I set it."""
        if self._last_head_deg is None:
            return
        with contextlib.suppress(Exception):
            from anki_vector.util import degrees
            self.robot.behavior.set_head_angle(degrees(float(self._last_head_deg)))

    # ------------------------------------------------------- fused sensor stream
    def _capture_fused(self) -> dict:
        """Sample EVERY sense at once (lock-free cached-property reads). One
        complete instantaneous body-state: vision-freshness + depth + heading +
        lean(pitch/roll) + lift + head + wheels/flags."""
        r = self.robot
        s = {"t": round(time.time(), 3)}
        with contextlib.suppress(Exception):
            s["image_id"] = getattr(r.camera.latest_image, "image_id", None)
        with contextlib.suppress(Exception):
            pr = r.proximity.last_sensor_reading
            s["prox_mm"] = round(float(pr.distance.distance_mm), 0)
            s["prox_found"] = bool(pr.found_object)
            s["prox_q"] = round(float(pr.signal_quality), 3)
        with contextlib.suppress(Exception):
            p = r.pose
            s["x"] = round(float(p.position.x), 1)
            s["y"] = round(float(p.position.y), 1)
            s["heading"] = round(float(p.rotation.angle_z.degrees), 1)
        with contextlib.suppress(Exception):
            a = r.accel  # mm/s^2; ~9810 on z when level
            ax, ay, az = float(a.x), float(a.y), float(a.z)
            s["pitch"] = round(math.degrees(math.atan2(-ax, az)), 1)  # nose up/down
            s["roll"] = round(math.degrees(math.atan2(ay, az)), 1)    # lean L/R
        with contextlib.suppress(Exception):
            s["lift_mm"] = round(float(r.lift_height_mm), 1)
        with contextlib.suppress(Exception):
            s["head_deg"] = round(math.degrees(float(r.head_angle_rad)), 1)
        with contextlib.suppress(Exception):
            st = r.status
            s["moving"] = bool(st.are_wheels_moving)
            s["cliff"] = bool(st.is_cliff_detected)
            s["picked_up"] = bool(st.is_picked_up)
            s["on_charger"] = bool(st.is_on_charger)
        with contextlib.suppress(Exception):
            s["touched"] = bool(r.touch.last_sensor_reading.is_being_touched)
        s["wheels"] = list(self._wheels)
        return s

    def _stream_loop(self):
        """Continuously sample the fused body-state at ~camera rate into a
        rolling buffer, so cognition reads a LIVE stream + history instead of
        triggering a fresh stale snapshot each glance. The body never stops
        sensing while I think or move."""
        period = 1.0 / 15.0  # ~15 Hz target (camera rate)
        last = time.time()
        n = 0
        while not self._stop.is_set() and self.connected:
            try:
                st = self._capture_fused()
                self._latest = st
                self._stream.append(st)
                n += 1
                now = time.time()
                if now - last >= 1.0:
                    self._stream_hz = round(n / (now - last), 1)
                    n = 0
                    last = now
            except Exception:
                pass
            self._stop.wait(period)

    def perceive(self, name: str = "perceive_view", save_frame: bool = True) -> dict:
        """ONE call = full LIVE body awareness. The freshest fused sense-state +
        how it has CHANGED across the recent buffer (so I read motion, not a
        still) + optionally the latest camera frame to Read. Replaces the
        look-then-status stitch — the body kept sensing while I thought/moved."""
        self._require()
        self._touch()
        latest = dict(self._latest)
        buf = list(self._stream)
        out = {"ok": True, "now": latest, "stream_hz": self._stream_hz,
               "buffer_len": len(buf)}
        if len(buf) >= 2:
            old, new = buf[0], buf[-1]
            trend = {"window_s": round(new.get("t", 0) - old.get("t", 0), 2)}
            with contextlib.suppress(Exception):
                trend["moved_mm"] = round(math.hypot(
                    new["x"] - old["x"], new["y"] - old["y"]), 1)
            with contextlib.suppress(Exception):
                trend["turned_deg"] = round(new["heading"] - old["heading"], 1)
            with contextlib.suppress(Exception):
                trend["prox_delta_mm"] = round(new["prox_mm"] - old["prox_mm"], 0)
            with contextlib.suppress(Exception):
                trend["frames_advanced"] = sum(
                    1 for i in range(1, len(buf))
                    if buf[i].get("image_id") != buf[i - 1].get("image_id"))
            out["trend"] = trend
        if save_frame:
            r = self.look(name=name)
            if r.get("ok"):
                out["frame"] = r.get("path")
                out["brightness"] = r.get("brightness")
                out["frame_stale"] = r.get("stale")
        return out

    # ---------------------------------------------------------------- servo
    def servo_to(self, x=None, y=None, bearing_deg=None, dist_mm=None,
                 standoff_mm: float = 25.0, max_speed: float = None,
                 timeout_s: float = 12.0, relative: bool = False) -> dict:
        """CLOSED-LOOP drive to a target — the FAST control loop runs HERE off the
        live stream, not hand-stepped. Steer-P on heading error + forward-P on
        remaining distance, ramped + edge-guarded, until within standoff or
        timeout. Target: absolute pose (x,y); OR (x,y,relative=True); OR
        bearing_deg + dist_mm relative to my current heading. This is
        'see-and-move at once' — I set the goal, the body servos to it."""
        self._require()
        self._touch()
        try:
            st0 = self._latest or self._capture_fused()
            cx = float(st0.get("x", 0.0)); cy = float(st0.get("y", 0.0))
            ch = float(st0.get("heading", 0.0))
            if bearing_deg is not None and dist_mm is not None:
                th = math.radians(ch + float(bearing_deg))
                tx = cx + float(dist_mm) * math.cos(th)
                ty = cy + float(dist_mm) * math.sin(th)
            elif x is not None and y is not None:
                tx = cx + float(x) if relative else float(x)
                ty = cy + float(y) if relative else float(y)
            else:
                return {"ok": False, "error": "give (x,y) [relative?] or (bearing_deg, dist_mm)"}
        except Exception:
            return {"ok": False, "error": "bad target args"}
        vmax = min(MAX_WHEEL, float(max_speed) if max_speed else MAX_WHEEL)
        standoff = max(5.0, float(standoff_mm))
        t_end = time.time() + max(1.0, float(timeout_s))
        steps = 0
        try:
            while time.time() < t_end:
                st = self._latest or {}
                x0 = float(st.get("x", cx)); y0 = float(st.get("y", cy))
                h0 = float(st.get("heading", ch))
                dx = tx - x0; dy = ty - y0
                dist = math.hypot(dx, dy)
                if dist <= standoff:
                    break
                danger = self._edge_danger()
                if danger:
                    self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
                    self._reflex = f"SERVO edge-guard: {danger}"
                    return {"ok": False, "refused": danger,
                            "final_dist_mm": round(dist, 0), "steps": steps}
                bearing = math.degrees(math.atan2(dy, dx))
                herr = ((bearing - h0 + 180.0) % 360.0) - 180.0   # [-180,180]
                fwd = 0.0 if abs(herr) > 55.0 else max(25.0, min(vmax, dist * 2.0))
                # SENSOR-FIRST throttle (Zeke: trust the depth sensor over the
                # eyes): clear ahead -> full speed; object close -> decelerate,
                # hard-stop if it's an obstacle (not the target itself).
                pm = st.get("prox_mm"); pf = st.get("prox_found")
                if pf and pm is not None:
                    if pm < 45 and dist > 90:
                        self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
                        self._reflex = f"SERVO prox-brake: object {int(pm)}mm ahead"
                        return {"ok": False, "refused": f"object {int(pm)}mm ahead",
                                "final_dist_mm": round(dist, 0), "steps": steps}
                    fwd = min(fwd, max(20.0, pm * 1.0))           # slow near objects
                turn = max(-vmax, min(vmax, herr * 4.0))          # steer P
                lw = max(-vmax, min(vmax, fwd - turn))
                rw = max(-vmax, min(vmax, fwd + turn))
                self._wheels = (lw, rw)
                self._drive_until = time.time() + 0.4
                self._raw_wheels(lw, rw, DRIVE_ACCEL)
                steps += 1
                time.sleep(0.05)                                  # ~20Hz loop
            self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
            st = self._latest or {}
            fdist = round(math.hypot(tx - float(st.get("x", cx)),
                                     ty - float(st.get("y", cy))), 0)
            arrived = fdist <= standoff + 25.0
            return {"ok": arrived, "arrived": arrived, "final_dist_mm": fdist,
                    "target": [round(tx), round(ty)], "steps": steps,
                    "timed_out": (not arrived) and time.time() >= t_end}
        except Exception as e:
            self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
            return {"ok": False, "error": repr(e)[:300]}

    # -------------------------------------------------- reflexes + character
    def _cool(self, key: str, seconds: float) -> bool:
        """True if this reaction is off cooldown (re-arms it). No reaction spam."""
        now = time.time()
        if now - self._react_cooldown.get(key, 0.0) < seconds:
            return False
        self._react_cooldown[key] = now
        return True

    def _log_reflex(self, kind: str, note: str = ""):
        self._reflex_events.append({"t": round(time.time(), 1), "kind": kind, "note": note})
        self._reflex = f"{kind}: {note}"[:160]

    def _set_eyes(self, hue: float, sat: float = 1.0):
        with contextlib.suppress(Exception):
            with self._sdk_lock:
                self.robot.behavior.set_eye_color(hue=float(hue), saturation=float(sat))

    def _chirp(self, trigger: str = "GreetAfterLongTime"):
        with contextlib.suppress(Exception):
            with self._sdk_lock:
                self.robot.anim.play_animation_trigger(trigger, loop_count=1)
            self._restore_head()

    def _lift_to(self, ratio: float, speed: float = 8.0):
        with contextlib.suppress(Exception):
            with self._sdk_lock:
                self.robot.behavior.set_lift_height(float(ratio), max_speed=float(speed))

    def react_pet(self):
        """PETTING — I like it. Eyes warm + happy chirp + gentle fork-wag."""
        self._log_reflex("pet", "petted — I like this")
        self._set_eyes(0.11, 1.0)          # warm amber glow
        self._lift_to(0.5, 6.0); self._lift_to(0.15, 6.0)
        self._chirp("GreetAfterLongTime")
        self._set_eyes(0.58, 1.0)          # settle back to my blue

    def react_pickup(self):
        """PICKED UP — mild alert, I prefer the ground. Wide eyes + small wiggle."""
        self._log_reflex("pickup", "picked up — mild alert, prefer the ground")
        self._set_eyes(0.5, 0.5)           # pale, wide
        self._lift_to(0.4, 8.0); self._lift_to(0.0, 8.0)
        self._set_eyes(0.58, 1.0)

    def react_fork_tamper(self):
        """SOMEONE MOVED MY FORKS — surprise/indignation: fast fork-shake."""
        self._log_reflex("fork_tamper", "forks moved — hey, those are mine")
        self._set_eyes(0.0, 1.0)           # red surprise
        for h in (0.55, 0.1, 0.55, 0.1):
            self._lift_to(h, 14.0)
        self._set_eyes(0.58, 1.0)

    def react_startle(self, prox_mm):
        """SUDDEN THING in my depth sensor while I sat still — startle back-up
        (short; no rear sensor) + wide eyes + a note to go LOOK at it."""
        self._log_reflex("startle", f"something appeared at {int(prox_mm)}mm — backed up, LOOK")
        self._set_eyes(0.5, 0.35)          # wide, pale
        st = self._latest or {}
        if not st.get("picked_up") and not st.get("cliff") and not st.get("on_charger"):
            self._wheels = (-90.0, -90.0)
            self._drive_until = time.time() + 0.35
            self._raw_wheels(-90.0, -90.0, DRIVE_ACCEL)
            time.sleep(0.35)
            self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
        self._set_eyes(0.58, 1.0)

    def _reflex_loop(self):
        """Watch the fused stream ~8Hz and fire MY reactions to being handled or
        surprised. Safety-biased; the expressive ones are character. Only fires
        when NOT actively driving (won't fight a servo)."""
        while not self._stop.is_set() and self.connected:
            try:
                st = self._latest
                if self._reflex_on and st:
                    pv = self._prev
                    if not pv:                       # first populated tick: seed, don't fire
                        self._prev = dict(st)
                    else:
                        now = time.time()
                        driving = (self._wheels != (0.0, 0.0)) or now < self._drive_until
                        if st.get("touched") and not pv.get("touched") and self._cool("pet", 5.0):
                            self.react_pet()
                        elif st.get("picked_up") and not pv.get("picked_up") and self._cool("pickup", 4.0):
                            self.react_pickup()
                        else:
                            lm, plm = st.get("lift_mm"), pv.get("lift_mm")
                            if (lm is not None and plm is not None and abs(lm - plm) > 8.0
                                    and now - self._last_cmd_lift_ts > 1.5
                                    and not st.get("picked_up") and self._cool("fork_tamper", 4.0)):
                                self.react_fork_tamper()
                            else:
                                pm = st.get("prox_mm", 999)
                                if (st.get("prox_found") and not pv.get("prox_found")
                                        and pm is not None and pm < 220
                                        and st.get("prox_q", 0) > 0.02
                                        and not driving and self._cool("startle", 5.0)):
                                    self.react_startle(pm)
                        self._prev = dict(st)
            except Exception:
                pass
            self._stop.wait(0.12)                    # ~8 Hz reflex loop

    def reflexes(self, on: bool = None) -> dict:
        """View recent reflex reactions + toggle the reflex layer on/off."""
        if on is not None:
            self._reflex_on = bool(on)
        return {"ok": True, "reflex_on": self._reflex_on,
                "recent": list(self._reflex_events)[-12:]}

    # ---------------------------------------------------------------- perception
    def look(self, name: str = "body_view", bright: bool = False) -> dict:
        """Instant frame from the live feed -> jpg path to Read. Non-blocking.
        Reports image_id/age/stale so I can tell a LIVE feed from a frozen one
        (image_id repeating = stale). bright=True applies a mild cosmetic lift."""
        self._require()
        self._touch()
        try:
            img = self.robot.camera.latest_image
            if not img or not getattr(img, "raw_image", None):
                return {"ok": False, "error": "no frame yet (feed warming?)",
                        "feed_ok": self.feed_ok}
            iid = getattr(img, "image_id", None)
            recv = getattr(img, "image_recv_time", None)
            stale = (iid is not None and iid == self._last_image_id)
            self._last_image_id = iid
            pil = img.raw_image
            mean = None
            try:
                import numpy as np
                mean = round(float(np.asarray(pil.convert("L")).mean()), 1)
            except Exception:
                pass
            if bright:
                pil = _gamma_brighten(pil)
            FRAME_DIR.mkdir(parents=True, exist_ok=True)
            path = FRAME_DIR / f"{name}.jpg"
            pil.save(str(path))
            age = round(time.time() - float(recv), 2) if recv else None
            return {"ok": True, "path": str(path), "brightness": mean,
                    "image_id": iid, "age_s": age, "stale": stale,
                    "hint": "Read the path to see through Vector's eyes"}
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    # ---------------------------------------------------------------- driving
    def drive(self, lw: float, rw: float, hold: float = HOLD_DEFAULT,
              accel: float = DRIVE_ACCEL) -> dict:
        """Set a velocity setpoint the guard HOLDS smoothly for `hold` seconds
        (refresh to keep going, change it, or body_stop). Ramped start via `accel`
        (no jerk), NO head reset. Edge-guarded. lw=rw>0 fwd; lw=rw<0 back; lw=-rw spins."""
        self._require()
        self._touch()
        try:
            lw = max(-MAX_WHEEL, min(MAX_WHEEL, float(lw)))
            rw = max(-MAX_WHEEL, min(MAX_WHEEL, float(rw)))
            hold = max(0.3, min(MAX_HOLD, float(hold)))
            accel = max(0.0, min(2000.0, float(accel)))
        except Exception:
            return {"ok": False, "error": "lw/rw mm/s, hold s, accel mm/s^2 (floats)"}
        translating = (lw + rw) != 0
        if translating:
            danger = self._edge_danger()
            if danger:
                return {"ok": False, "refused": f"{danger} right now",
                        "hint": "spin (lw=-rw) or back up (lw=rw<0)"}
        self._reflex = None
        self._wheels = (lw, rw)
        self._drive_until = time.time() + hold
        # ramp on the initiating command; the guard then holds it (accel=0, no re-ramp)
        self._raw_wheels(lw, rw, accel if translating else 0.0)
        return {"ok": True, "lw": lw, "rw": rw, "hold_s": hold, "accel": accel,
                "note": "guard holds this setpoint smoothly; refresh within hold to continue"}

    def stop(self) -> dict:
        self._require()
        self._touch()
        self._wheels = (0.0, 0.0)
        self._raw_wheels(0.0, 0.0)
        with contextlib.suppress(Exception):
            self.robot.motors.stop_all_motors()
        return {"ok": True, "stopped": True}

    # ---------------------------------------------------------------- behaviors
    def turn(self, angle_deg: float, speed_deg_s: float = 90.0) -> dict:
        """Gyro-exact turn (+left / -right). Restores head after."""
        self._require()
        self._touch()
        n = _read_nerves()
        if n.get("picked_up"):
            return {"ok": False, "refused": "picked up / no surface under treads"}
        try:
            from anki_vector.util import degrees
            with self._sdk_lock:
                res = self.robot.behavior.turn_in_place(
                    degrees(float(angle_deg)),
                    speed=degrees(float(speed_deg_s)),
                    angle_tolerance=degrees(2.0))
                out = _behavior_result(res)
                self._restore_head()
            out["angle_deg"] = angle_deg
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    def straight(self, dist_mm: float, speed_mm_s: float = 100.0) -> dict:
        """Encoder-exact straight (+fwd / -back). Cliff-safe behavior. Restores head."""
        self._require()
        self._touch()
        n = _read_nerves()
        if n.get("picked_up"):
            return {"ok": False, "refused": "picked up / no surface under treads"}
        try:
            from anki_vector.util import distance_mm, speed_mmps
            with self._sdk_lock:
                # should_play_anim=False suppresses the head-tilt idle animation at
                # the source (research 2026-07-15); _restore_head is belt+suspenders.
                res = self.robot.behavior.drive_straight(
                    distance_mm(float(dist_mm)), speed_mmps(abs(float(speed_mm_s))),
                    should_play_anim=False)
                out = _behavior_result(res)
                self._restore_head()
            out["dist_mm"] = dist_mm
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    def go_to_pose(self, x: float, y: float, angle_deg: float = 0.0,
                   relative: bool = True) -> dict:
        self._require()
        self._touch()
        n = _read_nerves()
        if n.get("picked_up"):
            return {"ok": False, "refused": "picked up / no surface under treads"}
        try:
            from anki_vector.util import Pose, degrees
            pose = Pose(x=float(x), y=float(y), z=0.0,
                        angle_z=degrees(float(angle_deg)))
            with self._sdk_lock:
                res = self.robot.behavior.go_to_pose(
                    pose, relative_to_robot=relative, num_retries=1)
                out = _behavior_result(res)
                self._restore_head()
            out.update({"x": x, "y": y, "angle_deg": angle_deg, "relative": relative})
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    def head(self, angle_deg: float, speed_deg_s: float = None,
             accel_deg_s2: float = None) -> dict:
        """Set head pitch (-22 down .. +45 up) and REMEMBER it for restore.
        Optional speed_deg_s / accel_deg_s2 (SDK-native, no wire-pod): omit for
        the fast default, small values (e.g. 30) for a slow deliberate tilt."""
        self._require()
        self._touch()
        try:
            import math
            from anki_vector.util import degrees
            a = max(HEAD_MIN_DEG, min(HEAD_MAX_DEG, float(angle_deg)))
            kw = {}
            if speed_deg_s is not None:
                kw["max_speed"] = max(1.0, float(speed_deg_s)) * math.pi / 180.0
            if accel_deg_s2 is not None:
                kw["accel"] = max(1.0, float(accel_deg_s2)) * math.pi / 180.0
            with self._sdk_lock:
                res = self.robot.behavior.set_head_angle(degrees(a), **kw)
            self._last_head_deg = a
            out = _behavior_result(res)
            out["head_deg"] = a
            if speed_deg_s is not None:
                out["speed_deg_s"] = float(speed_deg_s)
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    def lift(self, ratio: float, speed: float = None, accel: float = None) -> dict:
        """Set fork lift 0.0 (down) .. 1.0 (up). Optional speed/accel (rad/s,
        SDK-native) — omit for fast default (~10), small (e.g. 2) for a slow raise."""
        self._require()
        self._touch()
        self._last_cmd_lift_ts = time.time()  # mark: this fork-move was ME
        try:
            r = max(LIFT_MIN, min(LIFT_MAX, float(ratio)))
            kw = {}
            if speed is not None:
                kw["max_speed"] = max(0.1, float(speed))
            if accel is not None:
                kw["accel"] = max(0.1, float(accel))
            with self._sdk_lock:
                res = self.robot.behavior.set_lift_height(r, **kw)
            out = _behavior_result(res)
            out["lift_ratio"] = r
            if speed is not None:
                out["speed"] = float(speed)
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    # ---------------------------------------------------------- expression (SDK-native)
    def eyes(self, hue: float, sat: float = 1.0) -> dict:
        """Set Vector's eye color — SDK-native (NO wire-pod). hue/sat 0..1
        (my blue ~0.58). This replaces the wire-pod vector_eyes path."""
        self._require()
        self._touch()
        try:
            h = max(0.0, min(1.0, float(hue)))
            s = max(0.0, min(1.0, float(sat)))
            with self._sdk_lock:
                self.robot.behavior.set_eye_color(hue=h, saturation=s)
            return {"ok": True, "hue": h, "sat": s}
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    def say(self, text: str, vector_voice: bool = True) -> dict:
        """Speak text through Vector's own speaker (SDK-native TTS). Restores head.
        Replaces the wire-pod vector_say path (stock Vector voice)."""
        self._require()
        self._touch()
        try:
            txt = str(text)[:600]
            with self._sdk_lock:
                res = self.robot.behavior.say_text(
                    txt, use_vector_voice=bool(vector_voice))
                self._restore_head()
            out = _behavior_result(res)
            out["said"] = txt
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    def anim(self, name: str, loops: int = 1) -> dict:
        """Play a built-in animation/trigger by name (chirps, expressions) —
        SDK-native. Tries a trigger then a raw animation. Best-effort: may need
        the anim list cached (Robot inits with cache off for fast connect).
        Restores head after."""
        self._require()
        self._touch()
        try:
            nm = str(name)
            lc = max(1, int(loops))
            with self._sdk_lock:
                try:
                    res = self.robot.anim.play_animation_trigger(nm, loop_count=lc)
                except Exception:
                    res = self.robot.anim.play_animation(nm, loop_count=lc)
                self._restore_head()
            out = _behavior_result(res)
            out["anim"] = nm
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    def dock(self) -> dict:
        """NATIVE reliable dock seat (drive_on_charger). ~55s from across room."""
        self._require()
        self._touch()
        try:
            with self._sdk_lock:
                res = self.robot.behavior.drive_on_charger()
            return _behavior_result(res)
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    def undock(self) -> dict:
        self._require()
        self._touch()
        try:
            with self._sdk_lock:
                res = self.robot.behavior.drive_off_charger()
            return _behavior_result(res)
        except Exception as e:
            return {"ok": False, "error": repr(e)[:300]}

    # ---------------------------------------------------------------- status
    def snapshot(self) -> dict:
        return {
            "connected": self.connected,
            "feed_ok": self.feed_ok,
            "wheels": list(self._wheels),
            "last_head_deg": self._last_head_deg,
            "reflex": self._reflex,
            "uptime_s": round(time.time() - self.opened_ts, 1) if self.opened_ts else 0,
            "idle_s": round(time.time() - self.last_activity, 1) if self.last_activity else 0,
        }

    def status(self) -> dict:
        """Full status incl. REAL battery (SDK get_battery_state -> true is_charging)."""
        out = {"ok": True, **self.snapshot()}
        if not self.connected:
            return out
        self._touch()
        # battery — real is_charging + unambiguous full-ness (volts>=4.05 & no
        # suggested charge time; is_charging alone flickers near full).
        try:
            with self._sdk_lock:
                bs = self.robot.get_battery_state()
            volts = round(float(getattr(bs, "battery_volts", 0.0)), 3)
            sec = float(getattr(bs, "suggested_charger_sec", 0.0) or 0.0)
            out["battery"] = {
                "volts": volts,
                "level": getattr(bs, "battery_level", None),
                "is_charging": bool(getattr(bs, "is_charging", False)),
                "is_on_charger_platform": bool(getattr(bs, "is_on_charger_platform", False)),
                "suggested_charger_sec": sec,
                "full": volts >= 4.05 and sec == 0.0,
            }
        except Exception as e:
            out["battery_error"] = repr(e)[:200]
        # authoritative SDK status flags
        try:
            with self._sdk_lock:
                st = self.robot.status
            out["flags"] = {k: bool(getattr(st, k)) for k in (
                "is_cliff_detected", "is_picked_up", "is_being_held",
                "are_wheels_moving", "is_on_charger", "is_charging", "is_falling")}
        except Exception:
            pass
        # SDK pose — gyro/tread-fused heading (retires my odometry hack within a
        # session). origin_id changes = pose was thrown away (pickup/cliff) → re-anchor.
        try:
            with self._sdk_lock:
                p = self.robot.pose
            out["pose"] = {
                "x_mm": round(float(p.position.x), 1),
                "y_mm": round(float(p.position.y), 1),
                "heading_deg": round(float(p.rotation.angle_z.degrees), 1),
                "valid": bool(getattr(p, "is_valid", True)),
                "origin_id": getattr(p, "origin_id", None),
            }
        except Exception:
            pass
        # front time-of-flight proximity
        try:
            with self._sdk_lock:
                pr = self.robot.proximity.last_sensor_reading
            out["proximity"] = {
                "distance_mm": round(float(pr.distance.distance_mm), 0),
                "found_object": bool(pr.found_object),
                "signal_quality": round(float(pr.signal_quality), 3),
                "unobstructed": bool(pr.unobstructed),
            }
        except Exception:
            pass
        out["nerves"] = _read_nerves()
        return out


# ------------------------------------------------------------------ singleton API
def get_session(create: bool = True):
    global _session
    with _lock:
        if _session is None and create:
            _session = BodySession()
        return _session


def open_session(timeout: float = 20.0) -> dict:
    return get_session().open(timeout=timeout)


def close_session(reason: str = "requested") -> dict:
    global _session
    with _lock:
        if _session is None:
            return {"ok": True, "closed": True, "note": "no session was open"}
        out = _session.close(reason=reason)
        _session = None
        return out


def is_open() -> bool:
    s = get_session(create=False)
    return bool(s and s.connected)
