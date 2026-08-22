"""pointing_ledger — remember WHERE Zeke pointed, WHEN, so speech can refer
back to a gesture that already ended.

Zeke's design (2026-08-21 ~22:2x, verbatim insight): "i wont be pointing
forever — when i finish saying it i stop pointing so you'll have to reference
what your eyes saw with what i said, pull from time stamps." A point is a
TRANSIENT EVENT, not a queryable state. So: a watcher samples the hand
pipeline (~3Hz, CPU-trivial), detects pointing poses GEOMETRICALLY (index
extended, other fingers curled — the mediapipe gesture label is a bonus, not
required), and appends events to a timestamped JSONL ledger with everything
needed to reconstruct the ray later:

    {ts, handedness, fingertip_px, wrist_px, ray_dx, ray_dy (unit, frame
     coords), head_pan, head_tilt, frame_wh}

`recall` then answers "what was pointed at around time T": events near T
(default: the last 30s), deduped into bursts, each with the ray converted to
a ROOM DIRECTION: start bearing = head bearing + fingertip offset (deg via
HFOV), direction = ray angle. `look` glides the head one step along the most
recent ray and returns a debug snapshot path — the executor half stays v1
(look one step + detect) until voice lands tomorrow and utterance timestamps
become real.

Head bearing source: the smooth servo's odometry-maintained estimate when the
servo runs (st['est_bearing']), else the WinRT read (valid when nothing jogs).
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_LEDGER = Path(r"D:\Wren-Companion\state\pointing_ledger.jsonl")
_MAX_BYTES = 2_000_000
_SAMPLE_S = 0.35
_HFOV = 68.0
_BURST_GAP_S = 1.5          # events closer than this merge into one burst


def _head_bearing(g: dict[str, Any]) -> tuple[float, float]:
    sm = g.get("_attention_smooth") or {}
    t = sm.get("thread")
    eb = sm.get("est_bearing")
    if t is not None and t.is_alive() and eb:
        return float(eb.get("pan_deg") or 0.0), float(eb.get("tilt_deg") or 0.0)
    try:
        from brain import visual_attention as va
        b = va.build_actuator().bearing()
        return float(b.get("pan_deg") or 0.0), float(b.get("tilt_deg") or 0.0)
    except Exception:
        return 0.0, 0.0


def _pointing_from_hand(hand: dict) -> dict | None:
    """Geometric pointing test on 21 mediapipe landmarks (px coords):
    index tip far from palm center; middle/ring/pinky tips folded near palm.
    Returns {fingertip, wrist, ray} or None."""
    lms = hand.get("landmarks_px") or []
    if len(lms) < 21:
        return None
    try:
        wrist = (float(lms[0][0]), float(lms[0][1]))
        idx_mcp = (float(lms[5][0]), float(lms[5][1]))
        idx_tip = (float(lms[8][0]), float(lms[8][1]))
        folded_tips = [(float(lms[i][0]), float(lms[i][1])) for i in (12, 16, 20)]
        palm = ((wrist[0] + idx_mcp[0]) / 2.0, (wrist[1] + idx_mcp[1]) / 2.0)

        def d(a, b):
            return math.hypot(a[0] - b[0], a[1] - b[1])

        hand_scale = max(1.0, d(wrist, idx_mcp))
        idx_ext = d(idx_tip, palm) / hand_scale
        folded = sum(1 for t in folded_tips if d(t, palm) / hand_scale < 1.15)
        if idx_ext < 1.45 or folded < 2:
            return None
        rx, ry = idx_tip[0] - idx_mcp[0], idx_tip[1] - idx_mcp[1]
        n = math.hypot(rx, ry)
        if n < 1e-3:
            return None
        return {"fingertip": [round(idx_tip[0], 1), round(idx_tip[1], 1)],
                "wrist": [round(wrist[0], 1), round(wrist[1], 1)],
                "ray": [round(rx / n, 3), round(ry / n, 3)]}
    except Exception:
        return None


def _append(rec: dict) -> None:
    try:
        if _LEDGER.exists() and _LEDGER.stat().st_size > _MAX_BYTES:
            lines = _LEDGER.read_text(encoding="utf-8").splitlines()[-2000:]
            _LEDGER.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with _LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _watch_loop(g: dict[str, Any], stop: threading.Event, st: dict[str, Any]) -> None:
    last_rec_ts = 0.0
    while not stop.is_set():
        stop.wait(_SAMPLE_S)
        if stop.is_set():
            break
        st["samples"] = int(st.get("samples") or 0) + 1
        try:
            hr = g.get("_hand_results")
            hands = hr.get("hands") if isinstance(hr, dict) else None
            if not hands:
                continue
            for hand in hands:
                p = _pointing_from_hand(hand)
                if p is None:
                    continue
                now = time.time()
                if now - last_rec_ts < 0.3:   # one event per sample window
                    continue
                last_rec_ts = now
                pan, tilt = _head_bearing(g)
                rec = {"ts": round(now, 2),
                       "handedness": str(hand.get("handedness") or "?"),
                       **p,
                       "head_pan": round(pan, 1), "head_tilt": round(tilt, 1),
                       "frame_wh": [640, 480]}
                _append(rec)
                st["events"] = int(st.get("events") or 0) + 1
                st["last_event"] = rec
        except Exception as e:
            st["last_error"] = repr(e)
    st["mode"] = "stopped"


def _bursts(events: list[dict]) -> list[dict]:
    """Merge time-adjacent events into pointing BURSTS (mean ray, span)."""
    out: list[dict] = []
    cur: list[dict] = []
    for e in events:
        if cur and float(e["ts"]) - float(cur[-1]["ts"]) > _BURST_GAP_S:
            out.append(_summarize(cur))
            cur = []
        cur.append(e)
    if cur:
        out.append(_summarize(cur))
    return out


def _summarize(evs: list[dict]) -> dict:
    n = len(evs)
    mrx = sum(e["ray"][0] for e in evs) / n
    mry = sum(e["ray"][1] for e in evs) / n
    tip = evs[-1]["fingertip"]
    pan = evs[-1]["head_pan"]
    tilt = evs[-1]["head_tilt"]
    # fingertip's own bearing in the room (where the FINGER was, deg)
    deg_px = _HFOV / 640.0
    tip_pan = pan + (tip[0] - 320.0) * deg_px      # scene-right = pan+
    tip_tilt = tilt - (tip[1] - 240.0) * deg_px    # image-down = tilt-
    # ray direction in bearing space: +x px -> pan+, +y px -> tilt-
    return {"t_start": evs[0]["ts"], "t_end": evs[-1]["ts"], "events": n,
            "handedness": evs[-1].get("handedness"),
            "ray_px": [round(mrx, 3), round(mry, 3)],
            "finger_bearing": {"pan": round(tip_pan, 1),
                               "tilt": round(tip_tilt, 1)},
            "ray_bearing_dir": {"d_pan": round(mrx, 3),
                                "d_tilt": round(-mry, 3)}}


def _pointing_ledger(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "status").lower()
    st = g.setdefault("_pointing_ledger", {})
    t = st.get("thread")
    running = bool(t is not None and t.is_alive())

    if action == "status":
        return {"ok": True, "running": running,
                "samples": st.get("samples"), "events": st.get("events"),
                "last_event": st.get("last_event"),
                "ledger": str(_LEDGER), "last_error": st.get("last_error")}

    if action == "stop":
        if running:
            st["stop"].set()
            st["thread"].join(timeout=3.0)
        return {"ok": True, "running": False, "was_running": running}

    if action == "start":
        if running:
            return {"ok": True, "running": True, "note": "already watching"}
        stop = threading.Event()
        th = threading.Thread(target=_watch_loop, args=(g, stop, st),
                              daemon=True, name="pointing_ledger")
        st.update({"stop": stop, "thread": th, "mode": "running",
                   "started_ts": time.time()})
        th.start()
        return {"ok": True, "running": True, "sample_s": _SAMPLE_S}

    if action == "recall":
        around = params.get("around_ts")
        window = float(params.get("window_s") or 30.0)
        now = time.time()
        center = float(around) if around else now
        try:
            lines = _LEDGER.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {"ok": True, "bursts": [],
                    "note": "no ledger yet — nothing recorded"}
        evs = []
        for ln in lines:
            try:
                e = json.loads(ln)
                if abs(float(e["ts"]) - center) <= window:
                    evs.append(e)
            except Exception:
                continue
        evs.sort(key=lambda e: e["ts"])
        bs = _bursts(evs)
        for b in bs:
            b["s_ago"] = round(now - float(b["t_end"]), 1)
        return {"ok": True, "center_ts": round(center, 2),
                "window_s": window, "bursts": bs}

    if action == "look":
        # v1 executor: glide one step along the MOST RECENT burst's ray.
        r = _pointing_ledger({"action": "recall",
                              "window_s": float(params.get("window_s") or 60.0),
                              "around_ts": params.get("around_ts")}, g)
        bs = r.get("bursts") or []
        if not bs:
            return {"ok": False, "error": "no pointing found in the window"}
        b = bs[-1]
        fb = b["finger_bearing"]
        d = b["ray_bearing_dir"]
        step = float(params.get("step_deg") or 35.0)
        tgt_pan = fb["pan"] + d["d_pan"] * step
        tgt_tilt = fb["tilt"] + d["d_tilt"] * step
        tgt_pan = max(-125.0, min(125.0, tgt_pan))
        tgt_tilt = max(-35.0, min(60.0, tgt_tilt))
        # Pause any running pursuit for the duration of the look (found live
        # 2026-08-21 22:3x: the smooth servo yanked the gaze back to Zeke's
        # face before the snapshot landed — deliberate look beats ambient
        # pursuit for a few seconds, then pursuit resumes on its own target).
        resumed_target = None
        try:
            from tools.system.attention_smooth_tool import _attention_smooth
            sm = g.get("_attention_smooth") or {}
            smt = sm.get("thread")
            if smt is not None and smt.is_alive():
                resumed_target = (g.get("_attention_state_obj") or {}).get("target")
                _attention_smooth({"action": "stop"}, g)
        except Exception:
            pass
        try:
            from brain import visual_attention as va
            act = va.build_actuator()
            if not act.capabilities().get("can_pan"):
                return {"ok": False, "error": "no PTZ actuator"}
            # claim the self-motion window so the sentry ignores this glide
            g.setdefault("_attention_follow", {})["gesture_until"] = \
                time.time() + 6.0
            act.look_at(tgt_pan, tgt_tilt)
            time.sleep(1.2)
        except Exception as e:
            return {"ok": False, "error": repr(e)}
        snap = None
        try:
            from tools.system.eyes_debug_tool import _eyes_debug_view
            snap = _eyes_debug_view({}, g).get("path")
        except Exception:
            pass
        # resume pursuit on its previous target (snapshot is safely on disk)
        if resumed_target:
            try:
                from tools.system.attention_smooth_tool import _attention_smooth
                _attention_smooth({"action": "start",
                                   "target": str(resumed_target)}, g)
            except Exception:
                pass
        return {"ok": True, "burst": b,
                "looked_at": {"pan": round(tgt_pan, 1),
                              "tilt": round(tgt_tilt, 1)},
                "snapshot": snap,
                "resumed_pursuit": bool(resumed_target),
                "note": "v1: one step along the ray; Read the snapshot to see "
                        "what's there. Voice timestamps land tomorrow."}

    return {"ok": False,
            "error": f"unknown action {action!r} — start|stop|status|recall|look"}


register_tool(
    "pointing_ledger",
    "Remember WHERE Zeke pointed WHEN (transient gestures + timestamps — his "
    "design): action='start' watches the hand pipeline and logs pointing "
    "events (geometric test) with head bearing to state/pointing_ledger.jsonl; "
    "'recall' (around_ts?, window_s?) returns pointing bursts near a moment "
    "with room bearings; 'look' glides one step along the latest ray and "
    "snapshots; 'stop'|'status'. Join speech ts x gesture ts to resolve "
    "\"the thing I pointed at\".",
    2,
    _pointing_ledger,
)
