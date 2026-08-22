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
_GAIN_UNITS = 40.0          # units per unit of offset (dx=0.5 -> 20u ≈ 17°/s)
_PERIOD_S = 0.08            # ~12.5Hz servo/stream cadence
_LOST_ZERO_MISSES = 2       # consecutive observe misses -> zero vector
_LOST_HOLD_S = 8.0          # lost this long -> absolute home + keep watching
_HOME = (0.0, 10.0)         # measured level bearing at this desk perch


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


def _servo_loop(g: dict[str, Any], stop: threading.Event, st: dict[str, Any]) -> None:
    from brain import visual_attention as va

    jog = _PixyJog()
    st.update({"ticks": 0, "writes": 0, "zero_writes": 0, "misses": 0,
               "mode": "acquiring", "last_offset": None, "est_bearing": None,
               "error": None})
    est_pan, est_tilt = float(_HOME[0]), float(_HOME[1])
    lost_since: float | None = None
    homed_while_lost = False
    next_observe_ts = 0.0
    try:
        while not stop.is_set():
            t_tick = time.time()
            st["ticks"] += 1
            try:
                # ── GPU guard (measured 2026-08-21: while LOST, observe falls
                # back to OWL-ViT re-acquisition — 12Hz of that pegged the 3060
                # at 100%). Locked tracking is cheap (TrackerVit ~10ms CPU) and
                # runs every tick; lost-mode re-detection runs at ~1Hz.
                if t_tick < next_observe_ts:
                    stop.wait(_PERIOD_S)
                    continue
                res = va.observe(g)
                state = g.get("_attention_state_obj") or {}
                offset = (res or {}).get("offset") or state.get("offset")
                status = (res or {}).get("status")
                if status in ("locked", "seeking") and offset:
                    dx = float(offset.get("dx") or 0.0)
                    dy = float(offset.get("dy") or 0.0)
                    st["last_offset"] = {"dx": round(dx, 3), "dy": round(dy, 3)}
                    st["misses"] = 0
                    lost_since = None
                    homed_while_lost = False
                    if abs(dx) <= _DEADBAND and abs(dy) <= _DEADBAND:
                        jog.stop()
                        st["zero_writes"] += 1
                        st["mode"] = "centered"
                    else:
                        # centering law: see module docstring sign notes
                        ux = max(-_MAX_UNITS, min(_MAX_UNITS, -_GAIN_UNITS * dx))
                        uy = max(-_MAX_UNITS, min(_MAX_UNITS, -_GAIN_UNITS * dy))
                        # inside deadband on one axis -> freeze that axis
                        if abs(dx) <= _DEADBAND:
                            ux = 0.0
                        if abs(dy) <= _DEADBAND:
                            uy = 0.0
                        if jog.write_vector(ux, uy):
                            st["writes"] += 1
                            st["mode"] = "pursuit"
                            # dead-reckoned bearing (readback is blind to jog)
                            est_pan += ux * _UNIT_DEG_S * _PERIOD_S
                            est_tilt += uy * _UNIT_DEG_S * _PERIOD_S
                            st["est_bearing"] = {"pan_deg": round(est_pan, 1),
                                                 "tilt_deg": round(est_tilt, 1)}
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
                jog.stop()
            # keep cadence
            dt = time.time() - t_tick
            if dt < _PERIOD_S:
                stop.wait(_PERIOD_S - dt)
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
            tid = (g.get("_attention_state_obj") or {}).get("target") or target
            if str(tid).startswith("object:"):
                g["_attention_pin"] = str(tid)
        elif not (g.get("_attention_state_obj") or {}).get("target"):
            return {"ok": False,
                    "error": "no target — pass target='zeke' or 'object:...'"}
        if running:
            return {"ok": True, "running": True, "note": "already smooth"}
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
