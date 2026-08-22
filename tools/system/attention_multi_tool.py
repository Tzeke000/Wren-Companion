"""attention_multi — hold locks on SEVERAL things at once (fixed gaze, v1).

Built 2026-08-21 night. Zeke's queued want after single-object tracking
landed: "then he wants MULTI-object." TrackerVit costs ~10ms CPU per update,
so N simultaneous locks are cheap; the expensive part (OWL-ViT) runs once per
target at acquire + staggered revalidation.

V1 scope (deliberate): the HEAD DOES NOT MOVE. Multi-lock is a fixed-gaze
skill — every tracker assumes frame-to-frame appearance continuity, and a
panning head invalidates all of them at once. Follow-one-while-knowing-others
is a v2 problem (needs bearing-compensated box prediction). If a pursuit loop
is running, start() refuses.

Lifecycle per target: OWL-ViT acquire (current view!) → TrackerVit update at
loop rate on fresh frames → staggered reval every _REVAL_S (detector agree →
reseat on drift; 2 consecutive detector misses → that lock dies, honest miss,
`multi_lock_lost` signal fires; the others keep going).

status returns per-target: box, score, age, reval counts, alive/lost.
eyes_debug_view shows only the single-lock layer — use action='snapshot' here
for a frame annotated with EVERY multi-lock box.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from tools.tool_registry import register_tool

_LOOP_S = 0.12           # ~8Hz across all targets (updates are ~10ms each)
_REVAL_S = 5.0           # per-target detector re-check cadence (staggered)
_REVAL_MISS_LIMIT = 2
_IOU_AGREE = 0.30
_ACQUIRE_SCORE = 0.05
_MAX_TARGETS = 5


def _iou(a, b) -> float:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax1 + aw, bx1 + bw), min(ay1 + ah, by1 + bh)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return float(inter / union) if union > 0 else 0.0


def _fire_signal(g: dict[str, Any], ev: str, data: dict) -> None:
    try:
        bus = g.get("_signal_bus")
        if bus is not None:
            bus.fire(ev, data=data, priority="low")
    except Exception:
        pass


def _pursuit_running(g: dict[str, Any]) -> str | None:
    for key, name in (("_attention_follow", "step follow"),
                      ("_attention_smooth", "smooth servo")):
        st = g.get(key) or {}
        t = st.get("thread")
        if t is not None and t.is_alive():
            return name
    return None


def _multi_loop(g: dict[str, Any], stop: threading.Event, st: dict[str, Any]) -> None:
    from brain import frame_store, object_lock

    targets: dict[str, dict] = st["targets"]
    last_frame_ts = 0.0
    try:
        while not stop.is_set():
            t0 = time.time()
            res = frame_store.get_buffered_frame(max_age_sec=2.0)
            if res.frame is None or res.capture_ts <= last_frame_ts:
                stop.wait(_LOOP_S)
                continue
            last_frame_ts = res.capture_ts
            frame = res.frame
            now = time.time()
            st["ticks"] = int(st.get("ticks") or 0) + 1
            alive = 0
            for want, t in targets.items():
                if t.get("dead"):
                    continue
                try:
                    tracker = t.get("tracker")
                    if tracker is not None:
                        ok, box = tracker.update(frame)
                        t["updates"] = t.get("updates", 0) + 1
                        if ok:
                            t["box"] = tuple(float(v) for v in box)
                            try:
                                t["score"] = float(tracker.getTrackingScore())
                            except Exception:
                                t["score"] = 1.0
                            t["last_seen_ts"] = now
                        else:
                            t["score"] = 0.0
                    # staggered revalidation (or acquire if no tracker yet).
                    # GPU guard (caught live on first run, same class as the
                    # smooth servo's): an UNACQUIRED target used to re-detect
                    # every tick — 8Hz OWL-ViT per absent object. Acquire
                    # retries are throttled to ~1Hz; locked-target reval keeps
                    # the slow _REVAL_S cadence.
                    since_reval = now - float(t.get("last_reval_ts") or 0.0)
                    if tracker is None:
                        due = since_reval >= 1.0
                    else:
                        due = since_reval >= _REVAL_S or t.get("score", 0.0) < 0.2
                    if due:
                        t["last_reval_ts"] = now
                        t["revals"] = t.get("revals", 0) + 1
                        det = object_lock._detect(want, frame, _ACQUIRE_SCORE)
                        if det is None:
                            t["reval_misses"] = t.get("reval_misses", 0) + 1
                            if (t["reval_misses"] >= _REVAL_MISS_LIMIT
                                    and tracker is not None):
                                t["dead"] = True
                                t["tracker"] = None
                                _fire_signal(g, "multi_lock_lost",
                                             {"target": want,
                                              "last_box": t.get("box")})
                        else:
                            t["reval_misses"] = 0
                            dx, dy, dw, dh, dscore = det
                            need_seat = (tracker is None or not t.get("box")
                                         or _iou(t["box"], (dx, dy, dw, dh))
                                         < _IOU_AGREE)
                            if need_seat:
                                trk, kind = object_lock._new_tracker()
                                trk.init(frame,
                                         (int(dx), int(dy), int(dw), int(dh)))
                                t.update({"tracker": trk, "kind": kind,
                                          "box": (dx, dy, dw, dh),
                                          "score": dscore, "dead": False,
                                          "reseats": t.get("reseats", 0) + 1,
                                          "acquired_ts": t.get("acquired_ts")
                                          or now,
                                          "last_seen_ts": now})
                            else:
                                t["box"] = (dx, dy, dw, dh)
                                t["score"] = max(t.get("score", 0.0), dscore)
                except Exception as e:  # one target's failure never kills the rest
                    t["err"] = repr(e)
                if not t.get("dead") and t.get("tracker") is not None:
                    alive += 1
            st["alive"] = alive
            dt = time.time() - t0
            if dt < _LOOP_S:
                stop.wait(_LOOP_S - dt)
    finally:
        st["mode"] = "stopped"
        st["stopped_ts"] = time.time()


def _target_view(want: str, t: dict) -> dict:
    now = time.time()
    return {"target": want,
            "alive": bool(not t.get("dead") and t.get("tracker") is not None),
            "box": [int(v) for v in t["box"]] if t.get("box") else None,
            "score": round(float(t.get("score") or 0.0), 3),
            "age_s": (round(now - t["acquired_ts"], 1)
                      if t.get("acquired_ts") else None),
            "last_seen_s_ago": (round(now - t["last_seen_ts"], 1)
                                if t.get("last_seen_ts") else None),
            "updates": t.get("updates", 0), "revals": t.get("revals", 0),
            "reseats": t.get("reseats", 0), "err": t.get("err")}


def _attention_multi(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "status").lower()
    st = g.setdefault("_attention_multi", {})
    t = st.get("thread")
    running = bool(t is not None and t.is_alive())

    if action == "status":
        out = {"ok": True, "running": running,
               "ticks": st.get("ticks"), "alive": st.get("alive"),
               "targets": [_target_view(w, tt)
                           for w, tt in (st.get("targets") or {}).items()]}
        return out

    if action == "stop":
        if running:
            st["stop"].set()
            st["thread"].join(timeout=4.0)
        return {"ok": True, "running": False, "was_running": running}

    if action == "snapshot":
        import cv2
        from brain import frame_store
        res = frame_store.get_buffered_frame(max_age_sec=3.0)
        if res.frame is None:
            return {"ok": False, "error": "no fresh frame"}
        frame = res.frame.copy()
        colors = [(0, 255, 0), (0, 200, 255), (255, 120, 0),
                  (200, 0, 255), (0, 0, 255)]
        for i, (want, tt) in enumerate((st.get("targets") or {}).items()):
            box = tt.get("box")
            if not box:
                continue
            c = colors[i % len(colors)]
            x, y, w, h = (int(v) for v in box)
            dead = bool(tt.get("dead"))
            cv2.rectangle(frame, (x, y), (x + w, y + h), c, 1 if dead else 2)
            cv2.putText(frame, f"{want}{' (lost)' if dead else ''}",
                        (x, max(14, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, c, 1)
        from pathlib import Path
        d = Path(r"D:\Wren-Companion\state\eyes_debug")
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"multi_{int(time.time())}.jpg"
        cv2.imwrite(str(out), frame)
        return {"ok": True, "path": str(out)}

    if action == "start":
        busy = _pursuit_running(g)
        if busy:
            return {"ok": False,
                    "error": f"eyes are busy ({busy}) — multi-lock is a "
                             "fixed-gaze skill (v1); stop pursuit first"}
        raw = params.get("targets")
        if isinstance(raw, str):
            wants = [w.strip() for w in raw.split(",") if w.strip()]
        elif isinstance(raw, list):
            wants = [str(w).strip() for w in raw if str(w).strip()]
        else:
            wants = []
        if not wants:
            return {"ok": False,
                    "error": "pass targets='mug, pyramid toy, helmet' "
                             f"(1..{_MAX_TARGETS})"}
        wants = wants[:_MAX_TARGETS]
        if running:
            st["stop"].set()
            st["thread"].join(timeout=4.0)
        st["targets"] = {w: {"tracker": None, "box": None, "score": 0.0,
                             "reval_misses": 0, "last_reval_ts": 0.0,
                             "dead": False} for w in wants}
        st.update({"ticks": 0, "alive": 0, "mode": "running",
                   "started_ts": time.time()})
        stop = threading.Event()
        th = threading.Thread(target=_multi_loop, args=(g, stop, st),
                              daemon=True, name="attention_multi")
        st["stop"] = stop
        st["thread"] = th
        th.start()
        return {"ok": True, "running": True, "targets": wants,
                "note": "fixed-gaze v1: locks die if the head moves; "
                        "acquire happens from the CURRENT view"}

    return {"ok": False,
            "error": f"unknown action {action!r} — start|stop|status|snapshot"}


register_tool(
    "attention_multi",
    "MULTI-object lock (fixed gaze, v1): hold TrackerVit locks on up to 5 "
    "things at once from the current view — action='start' "
    "(targets='mug, pyramid toy, helmet') | 'status' (boxes/scores/ages) | "
    "'snapshot' (frame annotated with every lock) | 'stop'. Staggered OWL-ViT "
    "revalidation; a lost lock fires multi_lock_lost and the rest keep going. "
    "Head must be STILL — refuses while a pursuit loop runs.",
    2,
    _attention_multi,
)
