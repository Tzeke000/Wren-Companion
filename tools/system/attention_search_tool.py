"""attention_search + head_watch — the DECISION layer of person tracking.

Built 2026-08-25 (Fable) per the handoff §5: the PIXY's firmware tracker is a
good FOLLOWER but it does not SEARCH — when the subject fully leaves frame it
recentres and gives up. Going to look for someone who left is a decision, and
decisions are ours. Also Zeke's question — "can you not see that your head
degree changed, even if you're not the one who moved it?" — is an EVENT
question; the register updates for firmware/absolute moves (blind only to our
own jog stream), so external motion is detectable with zero accumulation and
therefore zero drift (the running-total odometry that DID drift stays dead).

Two tools:

  attention_search  action=start|stop|status  target=zeke
      Watcher thread. While the target is visible: does nothing (the chip —
      or our servo — follows). When the target has been gone _LOST_TRIGGER_S:
      sweeps absolute look_at stops (last bearing first, then learned
      hotspots, then a pan sweep), dwelling at each for the face pipeline +
      person_track to look. The INSTANT the target is seen again it stops
      commanding — hand back to whatever follows. Refuses to move while
      attention_smooth is driving (one driver at a time); in that case it
      only watches and reports.

  head_watch        action=start|stop|status
      Polls the device bearing ~2Hz. A bearing change with no self-command in
      the audit trail within _SELF_CMD_WINDOW_S = an EXTERNAL move (the chip,
      a human hand, another process). Coalesced into episodes; fires
      'head_moved_externally' on the signal bus at episode start. NOTE: the
      register is blind to OUR jog stream, so our own jogging never
      false-fires; out-of-process scripts (e.g. jog_sign_calibrate) don't
      audit and WILL read as external — that is honest, they are not this
      runtime.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_ROOT = Path(__file__).resolve().parent.parent.parent
_AUDIT = _ROOT / "state" / "attention" / "ptz_audit.jsonl"

# ── search tuning ───────────────────────────────────────────────────────────
_LOST_TRIGGER_S = 8.0     # gone this long -> go look (chip recentres ~sooner)
_PRESENCE_POLL_S = 0.3
_DWELL_S = 2.2            # per search stop: recognizer is ~5Hz, needs ~2s
_SEARCH_ROUNDS = 2
_REARM_S = 20.0           # base re-arm after a failed search...
_REARM_MAX_S = 600.0      # ...doubling per consecutive failure up to 10 min —
                          # a target who LEFT (bed, work) must not have the
                          # head sweeping the room every 20s all night. A
                          # sighting resets the backoff. (Caught live 08-25:
                          # first solo cycle would have re-swept forever.)
_PAN_SWEEP = (0.0, -25.0, 25.0, -50.0, 50.0)   # relative to last-seen pan
_TILT_SWEEP = (0.0, -12.0)                     # level first, then slightly down
_SOFT_PAN = (-115.0, 115.0)
_SOFT_TILT = (-35.0, 60.0)                     # same rails as the servo est

# ── head_watch tuning ───────────────────────────────────────────────────────
_WATCH_POLL_S = 0.5
_MOVE_EPS_DEG = 2.0       # bearing delta that counts as a move
_SELF_CMD_WINDOW_S = 2.5  # audit command newer than this -> it was us
_EPISODE_GAP_S = 3.0      # quiet this long ends an external-move episode


def _fire_signal(g: dict[str, Any], ev: str, data: dict) -> None:
    try:
        bus = g.get("_signal_bus")
        if bus is not None:
            bus.fire(ev, data=data, priority="medium")
    except Exception:
        pass


def _last_self_cmd_age() -> float:
    """Seconds since the newest command in the PTZ audit trail (inf if none).
    Cheap tail read — the file is append-only jsonl."""
    try:
        size = _AUDIT.stat().st_size
        with _AUDIT.open("rb") as f:
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        for line in reversed(tail):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("kind") in ("look_at", "jog_start", "jog_stop"):
                return max(0.0, time.time() - float(rec.get("ts") or 0.0))
        return float("inf")
    except Exception:
        return float("inf")


def _target_visible(g: dict[str, Any], label: str) -> bool:
    """Is the named person seen RIGHT NOW? Face match first (identity
    authority), else a live face-bound body track. An unbound person-shaped
    track is NOT presence — the Spartan helmet scored 0.65 on 08-25."""
    label = label.lower()
    try:
        for f in (g.get("_face_results") or []):
            if str(f.get("person_id") or "").lower() == label:
                return True
    except Exception:
        pass
    try:
        from brain import frame_store, person_track
        res = frame_store.get_buffered_frame(max_age_sec=1.0)
        if res.frame is not None:
            person_track.step(res.frame, g.get("_face_results") or [],
                              res.capture_ts)
        return person_track.target_offset(label) is not None
    except Exception:
        return False


def _servo_is_driving(g: dict[str, Any]) -> bool:
    st = g.get("_attention_smooth") or {}
    t = st.get("thread")
    return bool(t is not None and t.is_alive())


# ── search thread ───────────────────────────────────────────────────────────

def _search_loop(g: dict[str, Any], stop: threading.Event,
                 st: dict[str, Any], label: str) -> None:
    from brain import visual_attention as va
    act = None
    try:
        act = va.build_actuator()
        if not act.capabilities().get("can_pan"):
            act = None
    except Exception:
        act = None
    st.update({"mode": "watching", "searches": 0, "found_via_search": 0,
               "last_seen_ts": 0.0, "error": None})
    lost_since: float | None = None
    next_search_ok_ts = 0.0
    rearm_s = _REARM_S

    def sweep() -> bool:
        """One full search. True = target seen (hand back immediately)."""
        try:
            base = act.bearing() or {}
            base_pan = float(base.get("pan_deg") or 0.0)
        except Exception:
            base_pan = 0.0
        stops: list[tuple[float, float]] = []
        # learned hotspots first — where people actually appear in this room
        try:
            from tools.system.attention_follow_tool import _hotspots
            for h in _hotspots():
                stops.append((float(h["pan"]), float(h["tilt"])))
        except Exception:
            pass
        for dt_ in _TILT_SWEEP:
            for dp in _PAN_SWEEP:
                stops.append((base_pan + dp, 0.0 + dt_))
        seen: list[tuple[float, float]] = []
        for _round in range(_SEARCH_ROUNDS):
            for pan, tilt in stops:
                if stop.is_set():
                    return False
                pan = max(_SOFT_PAN[0], min(_SOFT_PAN[1], pan))
                tilt = max(_SOFT_TILT[0], min(_SOFT_TILT[1], tilt))
                if any(abs(pan - p) < 8 and abs(tilt - t) < 8
                       for p, t in seen):
                    continue
                seen.append((pan, tilt))
                try:
                    act.look_at(pan, tilt)
                except Exception as e:
                    st["error"] = repr(e)
                    return False
                deadline = time.time() + _DWELL_S
                while time.time() < deadline:
                    if stop.is_set():
                        return False
                    if _target_visible(g, label):
                        return True   # HAND BACK — not one more command
                    stop.wait(_PRESENCE_POLL_S)
            seen.clear()
        return False

    while not stop.is_set():
        try:
            visible = _target_visible(g, label)
            now = time.time()
            if visible:
                st["last_seen_ts"] = now
                lost_since = None
                rearm_s = _REARM_S      # a sighting resets the backoff
                st["mode"] = "watching"
            else:
                if lost_since is None:
                    lost_since = now
                gone_s = now - lost_since
                st["gone_s"] = round(gone_s, 1)
                if (gone_s >= _LOST_TRIGGER_S and now >= next_search_ok_ts
                        and act is not None):
                    if _servo_is_driving(g):
                        # one driver at a time — the servo has its own
                        # lost/home behaviour; we only observe.
                        st["mode"] = "deferring_to_servo"
                    else:
                        st["mode"] = "searching"
                        st["searches"] += 1
                        _fire_signal(g, "head_search_started",
                                     {"target": label,
                                      "gone_s": round(gone_s, 1)})
                        found = sweep()
                        if found:
                            st["found_via_search"] += 1
                            st["mode"] = "watching"
                            st["last_found_ts"] = time.time()
                            _fire_signal(g, "head_search_found",
                                         {"target": label})
                            lost_since = None
                            rearm_s = _REARM_S
                        else:
                            # go home, watch from there, re-arm with backoff
                            try:
                                act.look_at(0.0, 10.0)
                            except Exception:
                                pass
                            st["mode"] = "lost_hold"
                            next_search_ok_ts = time.time() + rearm_s
                            st["rearm_s"] = rearm_s
                            rearm_s = min(_REARM_MAX_S, rearm_s * 2.0)
            stop.wait(_PRESENCE_POLL_S if lost_since else 1.0)
        except Exception as e:  # noqa: BLE001
            st["error"] = repr(e)
            stop.wait(1.0)
    st["mode"] = "stopped"


# ── head_watch thread ───────────────────────────────────────────────────────

def _watch_loop(g: dict[str, Any], stop: threading.Event,
                st: dict[str, Any]) -> None:
    from brain import visual_attention as va
    try:
        act = va.build_actuator()
    except Exception as e:
        st.update({"mode": "error", "error": repr(e)})
        return
    st.update({"mode": "watching", "events": 0, "episodes": [],
               "error": None})
    prev: tuple[float, float] | None = None
    episode_last_ts = 0.0
    while not stop.is_set():
        try:
            b = act.bearing() or {}
            if b.get("confirmed"):
                cur = (float(b.get("pan_deg") or 0.0),
                       float(b.get("tilt_deg") or 0.0))
                if prev is not None:
                    dp, dt_ = cur[0] - prev[0], cur[1] - prev[1]
                    if max(abs(dp), abs(dt_)) >= _MOVE_EPS_DEG:
                        if _last_self_cmd_age() > _SELF_CMD_WINDOW_S:
                            now = time.time()
                            st["events"] = int(st.get("events") or 0) + 1
                            new_episode = (now - episode_last_ts
                                           > _EPISODE_GAP_S)
                            episode_last_ts = now
                            if new_episode:
                                ep = {"start_ts": now,
                                      "from": [round(prev[0], 1),
                                               round(prev[1], 1)],
                                      "to": [round(cur[0], 1),
                                             round(cur[1], 1)]}
                                eps = st.setdefault("episodes", [])
                                eps.append(ep)
                                del eps[:-10]
                                _fire_signal(g, "head_moved_externally",
                                             {"d_pan": round(dp, 1),
                                              "d_tilt": round(dt_, 1),
                                              "bearing": ep["to"]})
                            else:
                                eps = st.get("episodes") or []
                                if eps:
                                    eps[-1]["to"] = [round(cur[0], 1),
                                                     round(cur[1], 1)]
                prev = cur
        except Exception as e:  # noqa: BLE001
            st["error"] = repr(e)
        stop.wait(_WATCH_POLL_S)
    st["mode"] = "stopped"


# ── tool handlers ───────────────────────────────────────────────────────────

def _thread_tool(g: dict[str, Any], params: dict[str, Any], key: str,
                 runner, extra_start=None) -> dict[str, Any]:
    action = str(params.get("action") or "status").lower()
    st = g.setdefault(key, {})
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
        return {"ok": True, "running": False, "was_running": running}
    if action == "start":
        if running:
            return {"ok": True, "running": True, "note": "already running"}
        args = extra_start(params) if extra_start else ()
        if isinstance(args, dict):
            return args  # error dict from validator
        stop = threading.Event()
        th = threading.Thread(target=runner, args=(g, stop, st, *args),
                              daemon=True, name=key)
        st.update({"stop": stop, "thread": th, "started_ts": time.time()})
        th.start()
        return {"ok": True, "running": True}
    return {"ok": False, "error": f"unknown action {action!r}"}


def _attention_search(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    def validate(p):
        label = str(p.get("target") or "zeke").lower()
        if label.startswith("object:"):
            return {"ok": False, "error": "search is for PEOPLE; object "
                                          "targets have the lock/sentry path"}
        return (label,)
    return _thread_tool(g, params, "_attention_search", _search_loop, validate)


def _head_watch(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    return _thread_tool(g, params, "_head_watch", _watch_loop)


register_tool(
    "attention_search",
    "Search-when-lost decision layer: watches a person target and, when "
    "they've been gone ~8s and nothing else is driving the head, sweeps "
    "hotspots + pan stops with absolute moves, handing back the instant "
    "they're seen. action='start' (+target='zeke') | 'stop' | 'status'.",
    2,
    _attention_search,
)

register_tool(
    "head_watch",
    "External-head-motion event detector (no drift — event-based, not "
    "integrating): polls device bearing ~2Hz, fires 'head_moved_externally' "
    "when the head moves with no self-command in the PTZ audit within 2.5s "
    "(the firmware chip, a hand, another process). "
    "action='start'|'stop'|'status'.",
    2,
    _head_watch,
)
