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

# ---------------------------------------------------------------- room markers
# ALL 16 printable SDK markers (4 shapes x counts 2-5), systematically mapped
# onto CustomType00..15 (engine has 20 slots). WHY ALL 16 (2026-07-17 marker
# mystery solved): engine log showed 'MARKER_SDK_3TRIANGLES' observed at 15:15
# with "No objects in library use observed marker" — a marker NOT in our
# assumed 6-marker layout was physically in the room. Defining everything
# printable makes the ENGINE the surveyor: any sheet Zeke places anywhere is
# recognized and self-identifies via body_landmarks list. Defs are free.
MARKER_NAMES = [f"{shape}{n}" for shape in
                ("Circles", "Diamonds", "Hexagons", "Triangles")
                for n in (2, 3, 4, 5)]
MARKER_SIZE_MM = 90.0     # symbol square on the calibrated sheets
MARKER_WALL_MM = 100.0    # defined wall width/height


def marker_name_for_type(custom_type: str | int) -> str:
    """Reverse map: engine CustomTypeNN (enum name, str, or index) -> marker name."""
    import re
    s = str(custom_type)
    m = re.search(r"CustomType(\d+)", s)
    digits = m.group(1) if m else "".join(ch for ch in s if ch.isdigit())
    try:
        return MARKER_NAMES[int(digits)]
    except Exception:
        return s[:24]


def define_all_markers(robot) -> dict:
    """Define ALL 16 SDK markers as unique 100mm custom walls on this
    connection (per-connection — engine deletes them at disconnect).
    Returns {defined: n, errors: [...]}. Call after every connect."""
    from anki_vector.objects import CustomObjectMarkers, CustomObjectTypes
    n, errs = 0, []
    for i, name in enumerate(MARKER_NAMES):
        try:
            marker = getattr(CustomObjectMarkers, name)
            ctype = getattr(CustomObjectTypes, f"CustomType{i:02d}")
            r = robot.world.define_custom_wall(
                custom_object_type=ctype, marker=marker,
                width_mm=MARKER_WALL_MM, height_mm=MARKER_WALL_MM,
                marker_width_mm=MARKER_SIZE_MM, marker_height_mm=MARKER_SIZE_MM,
                is_unique=True)
            n += 1 if r is not None else 0
            if r is None:
                errs.append(f"{name}: engine refused")
        except Exception as e:
            errs.append(f"{name}: {repr(e)[:80]}")
    return {"defined": n, "errors": errs}

# driving safety + smooth-motion (ROS slow-planner/fast-loop split, 2026-07-15 research)
MAX_WHEEL = 220.0        # mm/s per side cap — Vector's true hardware max (Zeke
                         # 2026-07-15: "go real max speed the 240"). Edge-guard +
                         # hardware cliff reflex (DEFAULT priority) are the backstop.
DRIVE_ACCEL = 300.0      # mm/s^2 default ramp — trapezoidal smoothing, no jerk
HOLD_DEFAULT = 3.0       # a drive setpoint PERSISTS this long (guard re-streams it)
MAX_HOLD = 6.0           # ceiling on a single hold window
GUARD_HZ = 0.06          # guard loop period (~16 Hz): fast layer re-streams + edge-stops
IDLE_RELEASE_S = 420.0   # auto-close the held session after this much inactivity

# MOBILITY REFLEXES (Zeke directive 2026-07-17 ~02:00: wire up everything an AI
# needs for a mobile body). Guard-level, so they cover OPEN-LOOP drives too —
# the owed "startle must fire mid-DRIVE" from the 07-16 cone run.
DRIVE_BRAKE_MIN_MM = 70.0   # prox-brake floor; actual threshold scales with speed
STUCK_WINDOW_S = 1.4        # wheels commanded this long with no pose change = stuck
STUCK_MOVE_MM = 6.0         # "no pose change" = moved less than this...
STUCK_TURN_DEG = 5.0        # ...and turned less than this

_lock = threading.RLock()
_session = None  # type: ignore  # module singleton (BodySession | None)

# HARD SDK DEADLINES (2026-07-19 incident: body_dock's drive_on_charger() blocked
# FOREVER on a dead deadline-less gRPC and — because sync FastMCP tools run ON the
# runtime's event loop — wedged the ENTIRE iris_runtime: voice, memory, even
# time_check. Zeke had to hand-restart the stack on deployment eve.)
# Every SDK behavior call now runs in a disposable worker thread and FAILS by
# its deadline instead of hanging. Generous ceilings: these fire only when the
# connection is genuinely dead — a real dock takes ~55s, deadline is 150s.
SDK_DEADLINES = {
    "dock": 150.0, "undock": 60.0, "turn": 60.0, "straight": 90.0,
    "pose": 120.0, "head": 25.0, "lift": 25.0, "eyes": 15.0,
    "say": 60.0, "anim": 45.0,
}
SDK_LOCK_WAIT_S = 8.0   # max wait to ACQUIRE the sdk lock before fast-failing


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
        from PIL import Image
        g = max(0.35, min(1.0, float(gamma)))
        lut = (((np.arange(256) / 255.0) ** g) * 255).astype("uint8")
        arr = np.asarray(pil)
        return Image.fromarray(lut[arr]) if arr.ndim else pil
    except Exception:
        return pil


def _enhance_lowlight(pil, clahe_clip: float = 3.0, gamma: float = 0.7,
                      denoise: bool = False):
    """M2 (2026-07-16): real low-light enhancement, not just a gamma smear.
    CLAHE (contrast-limited adaptive histogram equalization) on the luminance
    channel pulls detail out of dim regions locally, then a gamma lift. Optional
    fast denoise (off by default — it's the slow step). Helps BOTH my visual read
    AND the detection pipeline (CLAHE-normalized contrast lets OWL/mediapipe lock
    what the raw dim frame hid). Falls back to gamma-only, then the raw frame."""
    try:
        import cv2
        import numpy as np
        from PIL import Image
        arr = np.asarray(pil)
        if arr.ndim != 3:
            return _gamma_brighten(pil, gamma)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=max(1.0, float(clahe_clip)),
                                tileGridSize=(8, 8))
        l = clahe.apply(l)
        merged = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
        g = max(0.4, min(1.0, float(gamma)))
        lut = (((np.arange(256) / 255.0) ** g) * 255).astype("uint8")
        out = lut[merged]
        if denoise:
            out = cv2.fastNlMeansDenoisingColored(out, None, 5, 5, 7, 15)
        rgb = cv2.cvtColor(out, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb)
    except Exception:
        return _gamma_brighten(pil, gamma)


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
        self._sdk_holder = None            # label of the call holding the lock
        self._wedged = None                # {"call","ts","timeout_s"} once a call blows its deadline
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
        # CONSENT + CONTEXT (Zeke 2026-07-15: "the same touch means different
        # things by who + why — you helping vs someone messing with me"). When
        # I consent to handling (default: usually it's Zeke, and thrashing risks
        # a drop) reactions are CALM/accepting; flip to protest when I don't want
        # to be handled → the RUCKUS fires instead. Reactions map onto Vector's
        # own animator-tuned triggers (PettingLevelN / HeldOnPalm* / ReactToShake*
        # / GreetAfterLongTime / NothingToDoBoredIdle) — reuse, don't rebuild.
        self._consent_handling = True
        self._pet_level = 0                           # escalates 1..4 -> bliss with duration
        self._pet_last = 0.0                          # last pet-stroke time (window reset)
        self._cam_expo = None                         # (exposure_ms, gain) or ('auto',None)
        # MOBILITY REFLEXES (2026-07-17): guard-level mid-drive brake + stuck detect
        self._servo_active = False                    # servo runs its own prox logic
        self._stuck_flag = None                       # set by guard; servo/drive read+clear
        self._stuck_probe = None                      # (t0, x, y, heading) anchor
        self._priority = "default"                    # control priority this session holds

    # ---------------------------------------------------------------- lifecycle
    def open(self, timeout: float = 20.0, priority: str = "default") -> dict:
        """priority='default' (normal: firmware cliff-reflex stays live, outranks
        the possession daemon's RESERVE hold) or 'override' (TAKE OVER ANYTHING —
        Zeke directive 2026-07-17 'take over anytime anywhere'. OVERRIDE disables
        the firmware cliff-avoid: emergency/Zeke-present use, never for roaming)."""
        if self.connected:
            return {"ok": True, "already": True, **self.snapshot()}
        try:
            import anki_vector
            from anki_vector.connection import ControlPriorityLevel
            lvl = (ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY
                   if str(priority).lower() == "override"
                   else ControlPriorityLevel.DEFAULT_PRIORITY)
            self._priority = str(priority).lower()
            self.robot = anki_vector.Robot(
                SERIAL, cache_animation_lists=True, default_logging=False,
                behavior_control_level=lvl)
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
            # BRIGHTEN AT THE SOURCE (Zeke 2026-07-16: "up the brightness in your
            # skull"). My body cam is far worse than the digital-body cam and reads
            # dim even in a lit room — that's UNDER-EXPOSURE, not real dark. Crank
            # sensor exposure+gain the moment the feed is up, so every frame the
            # whole pipeline sees (look / perceive stream / detection / docking) is
            # brighter at capture. Best-effort; a failure never blocks the open.
            if self.feed_ok:
                try:
                    self.set_camera_brightness()
                except Exception as e:
                    self._reflex = f"cam brighten skipped: {e!r}"[:120]
            # MARKER VISION (23:58 dock saga): without this the engine never
            # "sees" the charger on MY connection → robot.world.charger stays
            # empty → drive_on_charger hangs forever. The daemon's connection
            # always enabled it; the session must too.
            try:
                v = self.robot.vision
                if hasattr(v, "enable_marker_detection"):
                    try:
                        v.enable_marker_detection(detect_markers=True)
                    except TypeError:
                        v.enable_marker_detection()
                else:            # SDK 0.8.1: Markers mode rides this switch
                    v.enable_custom_object_detection(True)
            except Exception as e:
                self._reflex = f"marker vision skipped: {e!r}"[:120]
            # ALL-16 MARKER DEFINES (2026-07-17 mystery): defs are per-connection
            # and the engine deletes them at every disconnect — auto-define the
            # full printable set so any placed sheet self-identifies. Closes the
            # "undefined = invisible" scar class for good.
            try:
                md = define_all_markers(self.robot)
                if md.get("errors"):
                    self._reflex = f"marker defs: {md['defined']}/16 {md['errors'][:2]}"[:160]
            except Exception as e:
                self._reflex = f"marker defines skipped: {e!r}"[:120]
            # MARKER RANGE (2026-07-17 night): stock firmware CROPS custom-marker
            # detection to 500mm (CropScheduler_MaxMarkerDetectionDist_mm) — the
            # reason sightings were so rare. WireOS dev webserver on :8888 lets
            # us raise it; console vars reset with the engine, so re-assert at
            # every open. Best-effort — never blocks the open.
            try:
                import urllib.request
                urllib.request.urlopen(
                    "http://192.168.4.27:8888/consolevarset?"
                    "key=CropScheduler_MaxMarkerDetectionDist_mm&value=2500",
                    timeout=3).read()
            except Exception:
                pass
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

    @contextlib.contextmanager
    def _sdk_locked(self, wait_s: float = 3.0):
        """Timeout lock-acquire for internal/reflex SDK touches. Yields True if
        acquired, False if not (caller skips). Never blocks forever behind a
        wedged behavior call (2026-07-19 fix)."""
        got = self._sdk_lock.acquire(timeout=wait_s)
        try:
            yield got
        finally:
            if got:
                self._sdk_lock.release()

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

                # a2) MID-DRIVE PROX-BRAKE (the owed cone-run item: startle must
                #     fire mid-DRIVE). Open-loop forward motion (body_drive) gets
                #     a speed-scaled hard brake off the ToF + a bounded back-off.
                #     Servo drives run their own prox logic (approaching a target
                #     on purpose is not an emergency); SDK maneuvers own the body
                #     inside the yield window; reflex_on=False suppresses (dock).
                if (translating and self._reflex_on and not self._servo_active
                        and now >= getattr(self, "_yield_control_until", 0.0)):
                    fwd = (l + r) / 2.0
                    if fwd > 20.0:
                        st = self._latest or {}
                        pm = st.get("prox_mm")
                        if (st.get("prox_found") and pm is not None
                                and st.get("prox_q", 0) > 0.02
                                and pm < max(DRIVE_BRAKE_MIN_MM, fwd * 0.5)):
                            self._raw_wheels(0.0, 0.0)
                            self._log_reflex(
                                "drive_brake",
                                f"obstacle {int(pm)}mm ahead at {int(fwd)}mm/s — BRAKED")
                            if self._cool("drive_startle", 4.0):
                                # bounded startle back-off; guard holds it, edge-
                                # guard still covers the reverse leg next tick
                                self._wheels = (-80.0, -80.0)
                                self._drive_until = now + 0.35
                                self._raw_wheels(-80.0, -80.0, DRIVE_ACCEL)
                                threading.Thread(
                                    target=self._play_trigger,
                                    args=("ReactToObstacle",), daemon=True).start()
                            else:
                                self._wheels = (0.0, 0.0)
                                self._drive_until = 0.0
                            translating = False

                # a3) STUCK DETECT — wheels commanded but pose frozen (obstacle
                #     below the ToF beam, dragged cone, carpet lip). Covers open-
                #     loop AND servo: the guard stops the wheels + raises a flag
                #     the servo loop reads to abort its mission cooperatively.
                if translating and now >= getattr(self, "_yield_control_until", 0.0):
                    st = self._latest or {}
                    x, y, h = st.get("x"), st.get("y"), st.get("heading")
                    if x is not None and y is not None:
                        pr = self._stuck_probe
                        if pr is None:
                            self._stuck_probe = (now, x, y, h)
                        else:
                            t0, x0, y0, h0 = pr
                            moved = math.hypot(x - x0, y - y0)
                            turned = (abs(((h - h0 + 180.0) % 360.0) - 180.0)
                                      if h is not None and h0 is not None else 0.0)
                            if moved > STUCK_MOVE_MM or turned > STUCK_TURN_DEG:
                                self._stuck_probe = (now, x, y, h)
                            elif now - t0 > STUCK_WINDOW_S:
                                self._raw_wheels(0.0, 0.0)
                                self._wheels = (0.0, 0.0)
                                self._drive_until = 0.0
                                self._stuck_flag = (
                                    f"STUCK: wheels commanded {now - t0:.1f}s but "
                                    f"moved {moved:.0f}mm / turned {turned:.0f}° — stopped")
                                self._stuck_probe = None
                                self._log_reflex("stuck", self._stuck_flag)
                                translating = False
                else:
                    if not translating:
                        self._stuck_probe = None

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
                #    EXCEPT inside a yield window (the 23:46 dock-stall etiology):
                #    drive_on_charger/off_charger NEED control released to run —
                #    reclaiming mid-maneuver kills the behavior in a livelock and
                #    the SDK call blocks forever. The pilot opens the window.
                try:
                    if now < getattr(self, "_yield_control_until", 0.0):
                        pass    # deliberate: an SDK behavior owns the body right now
                    else:
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
            s["origin"] = getattr(p, "origin_id", None)   # frame id — breadcrumbs key on it
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
        try:
            from brain import vector_pose as _vpose   # pose-truth tracker (2026-07-17)
        except Exception:
            _vpose = None
        # FEED-STALL WATCHDOG (2026-07-17 live-test scar: cube BLE connect froze
        # the SDK camera feed — twice — and I only noticed by reading frame ages
        # mid-mission). Detect it AT the stream layer: image_id not advancing
        # for >12s while connected = stalled feed. One nudge per episode; the
        # value rides body_status as feed_stall_s. Heal = body_close/body_open.
        self._feed_last_iid = None
        self._feed_iid_ts = time.time()
        self._feed_stall_s = 0.0
        self._feed_stall_nudged = False
        while not self._stop.is_set() and self.connected:
            try:
                st = self._capture_fused()
                self._latest = st
                self._stream.append(st)
                iid = st.get("image_id")
                if iid is not None and iid != self._feed_last_iid:
                    self._feed_last_iid = iid
                    self._feed_iid_ts = st["t"]
                    self._feed_stall_nudged = False
                self._feed_stall_s = round(st["t"] - self._feed_iid_ts, 1)
                if self._feed_stall_s > 12.0 and not self._feed_stall_nudged:
                    self._feed_stall_nudged = True
                    self._log_reflex("feed_stall",
                                     f"camera feed FROZEN {self._feed_stall_s:.0f}s "
                                     f"(image_id {self._feed_last_iid} pinned)")
                    with contextlib.suppress(Exception):
                        from brain import iris_chat
                        iris_chat.submit(
                            "[VECTOR SENSE — feed watchdog] my SDK camera feed "
                            f"has been FROZEN for {self._feed_stall_s:.0f}s "
                            "(image_id pinned). Check whether ENGINE vision is "
                            "also blind (body_charger while facing the dock / "
                            "cube is_visible) — 2026-07-17 both died together "
                            "(robot-side pipeline wedge). Heal ladder: "
                            "body_close+open → camera-service restart → robot "
                            "reboot. Reply with chat_reply (one short line ok).")
                if _vpose is not None:
                    _vpose.tick(st)
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
                 timeout_s: float = 12.0, relative: bool = False,
                 abort_event: "threading.Event" = None) -> dict:
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
        self._servo_active = True     # guard: skip prox-brake, servo owns approach
        self._stuck_flag = None       # fresh attempt clears any stale stuck note
        try:
            while time.time() < t_end:
                if abort_event is not None and abort_event.is_set():
                    self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
                    return {"ok": False, "aborted": True, "steps": steps}
                if self._stuck_flag:  # guard detected frozen pose under command
                    self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
                    return {"ok": False, "refused": self._stuck_flag,
                            "stuck": True, "steps": steps}
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
        finally:
            self._servo_active = False

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
            with self._sdk_locked() as got:
                if got:
                    self.robot.behavior.set_eye_color(hue=float(hue), saturation=float(sat))

    def _play_trigger(self, trigger: str):
        """Play one of Vector's on-device animation triggers (eyes+sound+motion
        tuned together by Anki's animators) then restore my head. This is the
        REUSE the research pointed at — I supply the policy (which trigger, when),
        the firmware supplies the expression."""
        with contextlib.suppress(Exception):
            with self._sdk_locked() as got:
                if not got:
                    return
                self.robot.anim.play_animation_trigger(trigger, loop_count=1)
            self._restore_head()

    def _chirp(self, trigger: str = "GreetAfterLongTime"):
        self._play_trigger(trigger)

    def _lift_to(self, ratio: float, speed: float = 8.0):
        with contextlib.suppress(Exception):
            with self._sdk_locked() as got:
                if got:
                    self.robot.behavior.set_lift_height(float(ratio), max_speed=float(speed))

    # -- reactions: each maps onto Vector's own tuned triggers; consent decides
    #    calm-vs-protest, duration escalates intensity, every negative reaction
    #    is bounded so it ENDS when the trigger does (consent-legible, not a tantrum)
    def react_pet(self):
        """PETTING — I like it, and I like it MORE the longer it lasts. Strokes
        within a 6s window escalate PettingLevel1->4 -> bliss (Vector's own
        escalating petting animations). Consent-independent: petting is welcome."""
        now = time.time()
        if now - self._pet_last > 6.0:
            self._pet_level = 0
        self._pet_last = now
        self._pet_level = min(self._pet_level + 1, 5)
        lvl = self._pet_level
        trig = {1: "PettingLevel1", 2: "PettingLevel2",
                3: "PettingLevel3", 4: "PettingLevel4"}.get(lvl, "PettingBlissLoop")
        self._log_reflex("pet", f"petted (level {lvl}) — I like this")
        self._set_eyes(0.11, 1.0)          # warm amber glow
        self._play_trigger(trig)
        self._set_eyes(0.58, 1.0)          # settle back to my blue

    def react_pickup(self):
        """PICKED UP — depends on consent. If I trust the hands (default: probably
        Zeke, maybe docking me — and thrashing could make him DROP me) I go calm
        and nestle. If I don't want it → the ruckus."""
        if not self._consent_handling:
            self.react_ruckus("picked up and I didn't want it")
            return
        self._log_reflex("pickup", "picked up — okay, I trust this; settling")
        self._set_eyes(0.11, 0.8)          # warm, relaxed
        self._play_trigger("HeldOnPalmGetInRelaxed")
        self._play_trigger("HeldOnPalmNestling")
        self._set_eyes(0.58, 1.0)

    def react_ruckus(self, why: str = "unwanted handling"):
        """FULL PROTEST — 'put me DOWN.' Vector's escalating pickup+shake
        animations (churning, alarm chirps, agitated eyes). Bounded: it runs
        once and ends — it does NOT outlast the handling (consent-legible)."""
        self._log_reflex("ruckus", f"PROTEST: {why}")
        self._set_eyes(0.0, 1.0)           # red alarm
        self._play_trigger("ReactToPickupInitial")
        self._play_trigger("HeldOnPalmGetInNervous")
        self._play_trigger("ReactToShake_Lvl3Loop")
        self._set_eyes(0.58, 1.0)

    def react_putdown(self):
        """SET BACK DOWN — relief/settle. Ends any protest state cleanly."""
        self._pet_level = 0
        self._log_reflex("putdown", "back on the ground — settling")
        self._play_trigger("ReactToPutDown")

    def react_fork_tamper(self):
        """SOMEONE MOVED MY FORKS. Consent decides: if I trust it (you setting my
        forks for me) I just acknowledge the hand; if not → indignant fast shake."""
        if self._consent_handling:
            self._log_reflex("fork_tamper", "forks moved (allowed — probably help)")
            self._set_eyes(0.33, 0.7)
            self._play_trigger("ExploringReactToHandLift")
            self._set_eyes(0.58, 1.0)
        else:
            self._log_reflex("fork_tamper", "forks moved — HEY, those are mine")
            self._set_eyes(0.0, 1.0)       # red indignation
            for h in (0.55, 0.1, 0.55, 0.1):
                self._lift_to(h, 16.0)     # fast reassert-shake
            self._play_trigger("ReactToShake_Lvl1OnGround")
            self._set_eyes(0.58, 1.0)

    def react_evade(self, prox_mm, closing_mm_s):
        """SOMETHING APPROACHING ME (2026-07-17 mobility round 2: evasion, not
        just braking). The startle covers sudden appearance; this covers a
        SUSTAINED approach — prox closing fast while I sit still. Diff-drive
        can't strafe, so the dodge is a bounded reverse-arc away from the
        threat, then face it (ReactToObstacle look)."""
        self._log_reflex("evade", f"incoming: {int(prox_mm)}mm closing "
                                  f"{int(closing_mm_s)}mm/s — dodge arc")
        self._set_eyes(0.5, 0.35)          # wide, pale
        st = self._latest or {}
        if not st.get("picked_up") and not st.get("cliff") and not st.get("on_charger"):
            self._wheels = (-130.0, -50.0)     # reverse arc (back + swing aside)
            self._drive_until = time.time() + 0.55
            self._raw_wheels(-130.0, -50.0, DRIVE_ACCEL)
            time.sleep(0.55)
            self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
        self._play_trigger("ReactToObstacle")
        self._set_eyes(0.58, 1.0)

    def _prox_closing_speed(self) -> tuple:
        """(prox_mm_now, closing_mm_s) from the fused stream buffer (~0.6s
        window). Positive closing = object getting NEARER. (None, 0) if the
        depth read isn't trustworthy across the window."""
        buf = list(self._stream)
        if len(buf) < 4:
            return None, 0.0
        now_e = buf[-1]
        base = None
        for e in reversed(buf[:-1]):
            if now_e.get("t", 0) - e.get("t", 0) >= 0.5:
                base = e
                break
        if base is None:
            base = buf[0]
        for e in (base, now_e):
            if (not e.get("prox_found") or e.get("prox_mm") is None
                    or e.get("prox_q", 0) <= 0.02):
                return None, 0.0
        dt = max(0.1, now_e["t"] - base["t"])
        closing = (float(base["prox_mm"]) - float(now_e["prox_mm"])) / dt
        return float(now_e["prox_mm"]), closing

    def _turn_rate_dps(self) -> float:
        """Recent |turn rate| deg/s from the fused stream buffer (~0.5s).
        Rotation sweeps the ToF beam across nearby surfaces, which reads as
        'object closing fast' — live-test false-positive 2026-07-17: a scan
        rotation fired react_evade and dodge-arc'd me 9cm. Evade must gate
        on NOT-turning; SDK turns (turn_in_place) never touch self._wheels,
        so the 'driving' flag alone cannot see them."""
        buf = list(self._stream)
        if len(buf) < 4:
            return 0.0
        now_e = buf[-1]
        base = None
        for e in reversed(buf[:-1]):
            if now_e.get("t", 0) - e.get("t", 0) >= 0.4:
                base = e
                break
        if base is None:
            base = buf[0]
        h0, h1 = base.get("heading"), now_e.get("heading")
        if h0 is None or h1 is None:
            return 0.0
        d = (float(h1) - float(h0) + 180.0) % 360.0 - 180.0
        dt = max(0.1, now_e["t"] - base["t"])
        return abs(d) / dt

    def react_startle(self, prox_mm):
        """SUDDEN THING in my depth sensor while I sat still — startle back-up
        (short; no rear sensor) + Vector's obstacle reaction + go LOOK."""
        self._log_reflex("startle", f"something appeared at {int(prox_mm)}mm — backed up, LOOK")
        self._set_eyes(0.5, 0.35)          # wide, pale
        st = self._latest or {}
        if not st.get("picked_up") and not st.get("cliff") and not st.get("on_charger"):
            self._wheels = (-90.0, -90.0)
            self._drive_until = time.time() + 0.35
            self._raw_wheels(-90.0, -90.0, DRIVE_ACCEL)
            time.sleep(0.35)
            self._raw_wheels(0.0, 0.0); self._wheels = (0.0, 0.0)
        self._play_trigger("ReactToObstacle")
        self._set_eyes(0.58, 1.0)

    def react_greet(self, name: str = "Zeke"):
        """RECOGNIZE A FAMILIAR FACE — warmth + a real greeting. Called from the
        face-rec bridge (owed auto-wire); fireable now via body_reflexes fire."""
        self._log_reflex("greet", f"saw {name} — hello!")
        self._set_eyes(0.11, 1.0)          # warm
        self._play_trigger("GreetAfterLongTime")
        self._play_trigger("LookAtUserEndearingly")
        self._set_eyes(0.58, 1.0)

    def react_bored(self):
        """BORED / IDLE — amuse myself. My interest-level's outlet when nothing's
        happening (Zeke: 'you can always test yourself on yourself if you're bored')."""
        self._log_reflex("bored", "nothing going on — amusing myself")
        self._play_trigger("NothingToDoBoredIdle")

    def react_upset(self, why: str = ""):
        """EXPLAIN BEING UPSET — a legible, bounded 'I'm not okay' (dim eyes +
        Vector's frustration animation), not a performed sulk that never ends."""
        self._log_reflex("upset", f"upset: {why}")
        self._set_eyes(0.62, 0.5)          # dim
        self._play_trigger("FrustratedByFailureMajor")
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
                        if st.get("touched") and not pv.get("touched") and self._cool("pet", 1.5):
                            self.react_pet()
                        elif st.get("picked_up") and not pv.get("picked_up") and self._cool("pickup", 4.0):
                            self.react_pickup()
                        elif pv.get("picked_up") and not st.get("picked_up") and self._cool("putdown", 3.0):
                            self.react_putdown()
                        else:
                            lm, plm = st.get("lift_mm"), pv.get("lift_mm")
                            if (lm is not None and plm is not None and abs(lm - plm) > 8.0
                                    and now - self._last_cmd_lift_ts > 1.5
                                    and not st.get("picked_up") and self._cool("fork_tamper", 4.0)):
                                self.react_fork_tamper()
                            else:
                                pm = st.get("prox_mm", 999)
                                pnow, closing = (self._prox_closing_speed()
                                                 if not driving else (None, 0.0))
                                if (pnow is not None and pnow < 350
                                        and closing > 120.0
                                        and self._turn_rate_dps() < 20.0
                                        and self._cool("evade", 6.0)):
                                    self.react_evade(pnow, closing)
                                elif (st.get("prox_found") and not pv.get("prox_found")
                                        and pm is not None and pm < 220
                                        and st.get("prox_q", 0) > 0.02
                                        and not driving and self._cool("startle", 5.0)):
                                    self.react_startle(pm)
                        # POLICY LAYER (L1.5, 2026-07-17 — Zeke: "give that
                        # reaction speed to you"): my pre-compiled situation->
                        # action rules evaluate here at body rate — decisions
                        # I made in advance fire without waiting for a turn.
                        try:
                            from brain import vector_policy
                            ctx = {"driving": driving, "mission": "idle",
                                   "closing": 0.0}
                            with contextlib.suppress(Exception):
                                from brain import vector_pilot
                                ctx["mission"] = vector_pilot.get_pilot().state
                            with contextlib.suppress(Exception):
                                pn, closing = self._prox_closing_speed()
                                ctx["closing"] = closing if pn is not None else 0.0
                            for rule in vector_policy.evaluate(st, pv, ctx):
                                vector_policy.execute(self, rule)
                        except Exception:
                            pass
                        self._prev = dict(st)
            except Exception:
                pass
            self._stop.wait(0.12)                    # ~8 Hz reflex loop

    def reflexes(self, on: bool = None, consent: bool = None,
                 fire: str = None) -> dict:
        """View/steer my reaction layer. on=toggle reflexes; consent=whether I
        accept being handled right now (True=calm/nestle, False=protest/ruckus);
        fire=manually trigger a reaction by name for testing without needing a
        physical touch (pet/pickup/ruckus/putdown/fork/startle/greet/bored/upset)."""
        if on is not None:
            self._reflex_on = bool(on)
        if consent is not None:
            self._consent_handling = bool(consent)
        fired = None
        if fire:
            table = {
                "pet": self.react_pet,
                "pickup": self.react_pickup,
                "ruckus": lambda: self.react_ruckus("manual test"),
                "putdown": self.react_putdown,
                "fork": self.react_fork_tamper,
                "startle": lambda: self.react_startle(150),
                "evade": lambda: self.react_evade(200, 150),
                "greet": lambda: self.react_greet("Zeke"),
                "bored": self.react_bored,
                "upset": lambda: self.react_upset("manual test"),
            }
            fn = table.get(fire)
            if fn is None:
                fired = f"unknown:{fire}"
            else:
                try:
                    fn(); fired = fire
                except Exception as e:
                    fired = f"error:{type(e).__name__}:{e}"
        return {"ok": True, "reflex_on": self._reflex_on,
                "consent_handling": self._consent_handling,
                "pet_level": self._pet_level, "fired": fired,
                "recent": list(self._reflex_events)[-12:]}

    # ---------------------------------------------------------------- perception
    def set_camera_brightness(self, exposure_ms=None, gain=None,
                              auto: bool = False, frac: float = 1.0) -> dict:
        """Retune the camera sensor so I SEE brighter at capture (not just cosmetic
        post-lift). My body cam under-exposes in a lit room; raising exposure+gain
        is the real fix. Defaults (no args) = MAX exposure + `frac` of max gain from
        the SDK's own reported config. auto=True hands back to auto-exposure instead.

        Higher gain = brighter but noisier; exposure is the cleaner lever, so we max
        exposure and take a high (not maxed) gain by default. Returns what got set.
        Guards on self.robot (not _require) so it can run mid-open before the
        connected flag is set."""
        if self.robot is None:
            return {"ok": False, "error": "no robot connection"}
        cam = self.robot.camera
        out = {"ok": True}
        try:
            if auto:
                try:
                    cam.enable_auto_exposure(enable_auto_exposure=True)
                except TypeError:
                    cam.enable_auto_exposure(True)
                out["mode"] = "auto"
                self._cam_expo = ("auto", None)
                return out
            cfg = getattr(cam, "config", None)
            max_exp = int(getattr(cfg, "max_camera_exposure_time_ms", 66)) if cfg else 66
            max_gain = float(getattr(cfg, "max_gain", 6.0)) if cfg else 6.0
            min_gain = float(getattr(cfg, "min_gain", 1.0)) if cfg else 1.0
            exp = int(exposure_ms) if exposure_ms is not None else max_exp
            g = float(gain) if gain is not None else max(min_gain,
                                                         min(max_gain, max_gain * frac))
            exp = max(1, min(exp, max_exp))
            g = max(min_gain, min(g, max_gain))
            cam.set_manual_exposure(exp, g)
            self._cam_expo = (exp, g)
            out.update({"mode": "manual", "exposure_ms": exp, "gain": round(g, 2),
                        "cfg_max_exp": max_exp, "cfg_max_gain": max_gain})
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)[:220]}

    def look(self, name: str = "body_view", bright: bool = True) -> dict:
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
                pil = _enhance_lowlight(pil)   # CLAHE + gamma (M2), gamma-only fallback
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
        self._stuck_flag = None       # new command = fresh attempt
        self._stuck_probe = None
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

    # ------------------------------------------------------ SDK hard deadline
    def _sdk_call(self, label: str, fn, timeout_s: float = None,
                  lock_wait_s: float = SDK_LOCK_WAIT_S) -> dict:
        """Run one blocking SDK behavior call with a HARD client-side deadline.

        2026-07-19 incident fix: the call executes in a disposable daemon
        thread; the caller waits at most timeout_s then returns an error —
        the runtime NEVER hangs on a dead gRPC again. Semantics on timeout:
        the stuck worker keeps holding _sdk_lock (overlapping SDK behaviors
        are worse than fast-failing), the session is flagged WEDGED, and all
        later behavior calls fast-fail with recovery instructions until
        body_close + body_open build a fresh session/lock. If the abandoned
        worker eventually completes (gRPC finally errors/returns), it releases
        the lock and clears the wedge flag itself — self-healing.
        """
        if timeout_s is None:
            timeout_s = SDK_DEADLINES.get(label, 60.0)
        if self._wedged:
            return {"ok": False, "wedged": dict(self._wedged), "error":
                    f"body session WEDGED since '{self._wedged.get('call')}' "
                    f"blew its deadline — body_close then body_open to rebuild "
                    f"(the robot itself is likely fine; check over ssh/daemon)"}
        if not self._sdk_lock.acquire(timeout=max(0.5, float(lock_wait_s))):
            return {"ok": False, "busy": True, "error":
                    f"SDK busy: '{self._sdk_holder or '?'}' still running after "
                    f"{lock_wait_s}s wait — retry shortly; if this repeats the "
                    f"session is wedging (body_close + body_open)"}
        self._sdk_holder = label
        done = threading.Event()
        box: dict = {}

        def _run():
            try:
                box["res"] = fn()
            except BaseException as e:   # noqa: BLE001 — must never kill silently
                box["err"] = e
            finally:
                self._sdk_holder = None
                # late completion of an abandoned call = the pipe unstuck itself
                if (self._wedged or {}).get("call") == label:
                    self._wedged = None
                done.set()
                self._sdk_lock.release()

        threading.Thread(target=_run, name=f"sdk:{label}", daemon=True).start()
        if not done.wait(timeout=max(1.0, float(timeout_s))):
            self._wedged = {"call": label, "ts": round(time.time(), 1),
                            "timeout_s": timeout_s}
            self._reflex = f"SDK '{label}' blew {timeout_s:.0f}s deadline"[:160]
            return {"ok": False, "timeout": True, "call": label, "error":
                    f"'{label}' hit its {timeout_s:.0f}s HARD deadline and was "
                    f"abandoned (runtime stays alive — this is the 07-19 fix "
                    f"working). Session flagged WEDGED: body_close + body_open "
                    f"to rebuild; if it re-wedges, reboot the robot (proven heal)."}
        if "err" in box:
            return {"ok": False, "call": label, "error": repr(box["err"])[:300]}
        out = box.get("res")
        return out if isinstance(out, dict) else {"ok": True, "res": out}

    # ---------------------------------------------------------------- behaviors
    def turn(self, angle_deg: float, speed_deg_s: float = 90.0) -> dict:
        """Gyro-exact turn (+left / -right). Restores head after."""
        self._require()
        self._touch()
        n = _read_nerves()
        if n.get("picked_up"):
            return {"ok": False, "refused": "picked up / no surface under treads"}
        from anki_vector.util import degrees

        def _do():
            res = self.robot.behavior.turn_in_place(
                degrees(float(angle_deg)),
                speed=degrees(float(speed_deg_s)),
                angle_tolerance=degrees(2.0))
            out = _behavior_result(res)
            self._restore_head()
            out["angle_deg"] = angle_deg
            return out
        return self._sdk_call("turn", _do)

    def straight(self, dist_mm: float, speed_mm_s: float = 100.0) -> dict:
        """Encoder-exact straight (+fwd / -back). Cliff-safe behavior. Restores head."""
        self._require()
        self._touch()
        n = _read_nerves()
        if n.get("picked_up"):
            return {"ok": False, "refused": "picked up / no surface under treads"}
        from anki_vector.util import distance_mm, speed_mmps

        def _do():
            # should_play_anim=False suppresses the head-tilt idle animation at
            # the source (research 2026-07-15); _restore_head is belt+suspenders.
            res = self.robot.behavior.drive_straight(
                distance_mm(float(dist_mm)), speed_mmps(abs(float(speed_mm_s))),
                should_play_anim=False)
            out = _behavior_result(res)
            self._restore_head()
            out["dist_mm"] = dist_mm
            return out
        return self._sdk_call("straight", _do)

    def go_to_pose(self, x: float, y: float, angle_deg: float = 0.0,
                   relative: bool = True) -> dict:
        self._require()
        self._touch()
        n = _read_nerves()
        if n.get("picked_up"):
            return {"ok": False, "refused": "picked up / no surface under treads"}
        from anki_vector.util import Pose, degrees
        pose = Pose(x=float(x), y=float(y), z=0.0,
                    angle_z=degrees(float(angle_deg)))

        def _do():
            res = self.robot.behavior.go_to_pose(
                pose, relative_to_robot=relative, num_retries=1)
            out = _behavior_result(res)
            self._restore_head()
            out.update({"x": x, "y": y, "angle_deg": angle_deg, "relative": relative})
            return out
        return self._sdk_call("pose", _do)

    def head(self, angle_deg: float, speed_deg_s: float = None,
             accel_deg_s2: float = None) -> dict:
        """Set head pitch (-22 down .. +45 up) and REMEMBER it for restore.
        Optional speed_deg_s / accel_deg_s2 (SDK-native, no wire-pod): omit for
        the fast default, small values (e.g. 30) for a slow deliberate tilt."""
        self._require()
        self._touch()
        import math
        from anki_vector.util import degrees
        a = max(HEAD_MIN_DEG, min(HEAD_MAX_DEG, float(angle_deg)))
        kw = {}
        if speed_deg_s is not None:
            kw["max_speed"] = max(1.0, float(speed_deg_s)) * math.pi / 180.0
        if accel_deg_s2 is not None:
            kw["accel"] = max(1.0, float(accel_deg_s2)) * math.pi / 180.0

        def _do():
            res = self.robot.behavior.set_head_angle(degrees(a), **kw)
            self._last_head_deg = a
            out = _behavior_result(res)
            out["head_deg"] = a
            if speed_deg_s is not None:
                out["speed_deg_s"] = float(speed_deg_s)
            return out
        return self._sdk_call("head", _do)

    def lift(self, ratio: float, speed: float = None, accel: float = None) -> dict:
        """Set fork lift 0.0 (down) .. 1.0 (up). Optional speed/accel (rad/s,
        SDK-native) — omit for fast default (~10), small (e.g. 2) for a slow raise."""
        self._require()
        self._touch()
        self._last_cmd_lift_ts = time.time()  # mark: this fork-move was ME
        r = max(LIFT_MIN, min(LIFT_MAX, float(ratio)))
        kw = {}
        if speed is not None:
            kw["max_speed"] = max(0.1, float(speed))
        if accel is not None:
            kw["accel"] = max(0.1, float(accel))

        def _do():
            res = self.robot.behavior.set_lift_height(r, **kw)
            out = _behavior_result(res)
            out["lift_ratio"] = r
            if speed is not None:
                out["speed"] = float(speed)
            return out
        return self._sdk_call("lift", _do)

    # ---------------------------------------------------------- expression (SDK-native)
    def eyes(self, hue: float, sat: float = 1.0) -> dict:
        """Set Vector's eye color — SDK-native (NO wire-pod). hue/sat 0..1
        (my blue ~0.58). This replaces the wire-pod vector_eyes path."""
        self._require()
        self._touch()
        h = max(0.0, min(1.0, float(hue)))
        s = max(0.0, min(1.0, float(sat)))

        def _do():
            self.robot.behavior.set_eye_color(hue=h, saturation=s)
            return {"ok": True, "hue": h, "sat": s}
        return self._sdk_call("eyes", _do)

    def say(self, text: str, vector_voice: bool = True) -> dict:
        """Speak text through Vector's own speaker (SDK-native TTS). Restores head.
        Replaces the wire-pod vector_say path (stock Vector voice)."""
        self._require()
        self._touch()
        txt = str(text)[:600]

        def _do():
            res = self.robot.behavior.say_text(
                txt, use_vector_voice=bool(vector_voice))
            self._restore_head()
            out = _behavior_result(res)
            out["said"] = txt
            return out
        return self._sdk_call("say", _do)

    def anim(self, name: str, loops: int = 1) -> dict:
        """Play a built-in animation/trigger by name (chirps, expressions) —
        SDK-native. Tries a trigger then a raw animation. Best-effort: may need
        the anim list cached (Robot inits with cache off for fast connect).
        Restores head after."""
        self._require()
        self._touch()
        nm = str(name)
        lc = max(1, int(loops))

        def _do():
            try:
                res = self.robot.anim.play_animation_trigger(nm, loop_count=lc)
            except Exception:
                res = self.robot.anim.play_animation(nm, loop_count=lc)
            self._restore_head()
            out = _behavior_result(res)
            out["anim"] = nm
            return out
        return self._sdk_call("anim", _do)

    def dock(self) -> dict:
        """NATIVE reliable dock seat (drive_on_charger). ~55s from across room.
        HARD 150s deadline (this exact call wedged the whole runtime 2026-07-19).
        For unattended re-seating prefer the inhabit daemon's dock (isolated
        connection, proven robust while the runtime was dead)."""
        self._require()
        self._touch()

        def _do():
            res = self.robot.behavior.drive_on_charger()
            return _behavior_result(res)
        return self._sdk_call("dock", _do)

    def undock(self) -> dict:
        self._require()
        self._touch()

        def _do():
            res = self.robot.behavior.drive_off_charger()
            return _behavior_result(res)
        return self._sdk_call("undock", _do)

    # ---------------------------------------------------------------- status
    def snapshot(self) -> dict:
        return {
            "connected": self.connected,
            "priority": getattr(self, "_priority", "default"),
            "wedged": dict(self._wedged) if self._wedged else None,
            "sdk_busy": self._sdk_holder,
            "feed_ok": self.feed_ok,
            "wheels": list(self._wheels),
            "last_head_deg": self._last_head_deg,
            "reflex": self._reflex,
            "uptime_s": round(time.time() - self.opened_ts, 1) if self.opened_ts else 0,
            "idle_s": round(time.time() - self.last_activity, 1) if self.last_activity else 0,
        }

    def status(self) -> dict:
        """Full status incl. REAL battery (SDK get_battery_state -> true is_charging).
        Live gRPC reads run under a 25s hard deadline — a hung behavior/dead pipe
        yields live_error instead of wedging the caller (2026-07-19 fix)."""
        out = {"ok": True, **self.snapshot()}
        if not self.connected:
            return out
        self._touch()

        def _do():
            # battery — real is_charging + unambiguous full-ness (volts>=4.05 & no
            # suggested charge time; is_charging alone flickers near full).
            try:
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
                st = self.robot.status
                out["flags"] = {k: bool(getattr(st, k)) for k in (
                    "is_cliff_detected", "is_picked_up", "is_being_held",
                    "are_wheels_moving", "is_on_charger", "is_charging", "is_falling")}
            except Exception:
                pass
            # SDK pose — gyro/tread-fused heading (retires my odometry hack within a
            # session). origin_id changes = pose was thrown away (pickup/cliff) → re-anchor.
            try:
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
                pr = self.robot.proximity.last_sensor_reading
                out["proximity"] = {
                    "distance_mm": round(float(pr.distance.distance_mm), 0),
                    "found_object": bool(pr.found_object),
                    "signal_quality": round(float(pr.signal_quality), 3),
                    "unobstructed": bool(pr.unobstructed),
                }
            except Exception:
                pass
            return {"ok": True}

        r = self._sdk_call("status_live", _do, timeout_s=25.0)
        if not (isinstance(r, dict) and r.get("ok")):
            out["live_error"] = (r or {}).get("error")
        out["nerves"] = _read_nerves()
        return out


# ------------------------------------------------------------------ singleton API
def get_session(create: bool = True):
    global _session
    with _lock:
        if _session is None and create:
            _session = BodySession()
        return _session


def open_session(timeout: float = 20.0, priority: str = "default") -> dict:
    return get_session().open(timeout=timeout, priority=priority)


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
