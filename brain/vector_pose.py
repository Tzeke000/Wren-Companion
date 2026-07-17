# SELF_ASSESSMENT: I am Iris's POSE TRUTH tracker — how much to trust the
# body's odometry right now, and when/how to grab an absolute fix.
"""vector_pose.py — pose confidence + charger-anchored fixes (fusion v1,
2026-07-17 ~04:45; the first buildable step of the charger-anchored SLAM
design agreed with Zeke 2026-07-16).

The problem: odometry drifts (measured: equal wheels curve ~17°/10cm; the
07-16 cone miss was believed-pose sliding off real pose). The only absolute
reference today is the CHARGER (engine marker sighting via robot.world.charger).

V1 model: confidence starts at 1.0 at an absolute fix and DECAYS with motion —
distance and turning accumulated since the fix (turning is the bigger drift
source on this body). A pose-frame change (pickup/sleep/reboot → origin_id
changes) zeroes confidence outright: the frame is alien until re-anchored.

    tick(st)            <- called from the session's 15Hz stream loop (cheap)
    absolute_fix(src)   <- called when the engine reports a FRESH charger pose
    status()            <- confidence 0..1 + drift budget + advice

Confidence heuristic (calibration-informed, honest-not-precise):
    heading_err_deg ≈ turns_deg * 0.10 + dist_mm / 30 * 0.2   (bounded model)
    conf = exp(-(dist_mm/2500 + turns_deg/540))
Numbers derive from the measured wheel imbalance + gyro turn accuracy
(13.5/15 commanded); they are a BUDGET, not a measurement — the point is a
monotonic, motion-aware trust signal with clear re-anchor advice.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ANCHOR_JSON = REPO / "state" / "vector" / "pose_anchor.json"

_lock = threading.Lock()
_state = {
    "fix_ts": 0.0,          # last absolute fix (0 = never this frame)
    "fix_source": None,
    "origin": None,         # pose frame id the accumulators live in
    "dist_mm": 0.0,         # motion since fix
    "turns_deg": 0.0,
    "last_xy": None,
    "last_h": None,
}


def tick(st: dict) -> None:
    """Feed one fused stream state (~15Hz). Cheap; never raises."""
    try:
        x, y, h, origin = st.get("x"), st.get("y"), st.get("heading"), st.get("origin")
        if x is None or y is None:
            return
        with _lock:
            if origin != _state["origin"]:
                # frame reset: everything known about pose truth is void
                _state.update(origin=origin, fix_ts=0.0, fix_source=None,
                              dist_mm=0.0, turns_deg=0.0,
                              last_xy=(x, y), last_h=h)
                return
            lx = _state["last_xy"]
            if lx is not None:
                _state["dist_mm"] += math.hypot(x - lx[0], y - lx[1])
            if h is not None and _state["last_h"] is not None:
                _state["turns_deg"] += abs(((h - _state["last_h"] + 180.0)
                                            % 360.0) - 180.0)
            _state["last_xy"] = (x, y)
            _state["last_h"] = h
    except Exception:
        pass


def absolute_fix(source: str = "charger", detail: dict = None) -> dict:
    """Record an absolute fix NOW (engine saw the charger marker fresh, or a
    future overhead-cam/ArUco sighting). Resets the drift budget."""
    with _lock:
        _state["fix_ts"] = time.time()
        _state["fix_source"] = source
        _state["dist_mm"] = 0.0
        _state["turns_deg"] = 0.0
    rec = {"ts": _state["fix_ts"], "source": source,
           "origin": _state["origin"], "detail": detail or {}}
    try:
        ANCHOR_JSON.parent.mkdir(parents=True, exist_ok=True)
        ANCHOR_JSON.write_text(json.dumps(rec), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, **rec}


def confidence() -> float:
    with _lock:
        if _state["fix_ts"] <= 0.0:
            return 0.0 if _state["origin"] is not None else 0.0
        return round(math.exp(-(_state["dist_mm"] / 2500.0
                                + _state["turns_deg"] / 540.0)), 3)


def status() -> dict:
    with _lock:
        s = dict(_state)
    conf = confidence()
    heading_budget = round(s["turns_deg"] * 0.10 + (s["dist_mm"] / 30.0) * 0.2, 1)
    if s["fix_ts"] <= 0.0:
        advice = ("NO absolute fix in this pose frame — face the charger "
                  "(body_charger until known+fresh) or dock once; treat "
                  "coordinates as relative-only until then")
    elif conf < 0.35:
        advice = ("drift budget spent — re-anchor: sight the charger "
                  "(body_charger fresh) or body_park; don't trust old "
                  "waypoints/cone positions past ~centimeters")
    elif conf < 0.7:
        advice = "usable but aging — prefer short legs + re-sight home soon"
    else:
        advice = "pose trustworthy"
    return {"ok": True, "confidence": conf,
            "since_fix": {"dist_mm": round(s["dist_mm"], 0),
                          "turns_deg": round(s["turns_deg"], 0),
                          "age_s": round(time.time() - s["fix_ts"], 1)
                          if s["fix_ts"] else None},
            "est_heading_err_deg": heading_budget,
            "fix_source": s["fix_source"], "origin": s["origin"],
            "advice": advice}


def try_charger_fix(session) -> dict:
    """If the engine has a FRESH charger sighting on the live session, take an
    absolute fix from it. The freshness gate matters — a stale remembered pose
    is exactly the drifted thing we're correcting."""
    try:
        ch = session.robot.world.charger
        if ch is None or getattr(ch, "pose", None) is None:
            return {"ok": False, "note": "engine has no charger pose this "
                                         "connection — face the dock with "
                                         "marker vision on"}
        last_seen = float(getattr(ch, "time_since_last_seen", 1e9))
        if last_seen > 2.0:
            return {"ok": False, "note": f"charger sighting stale "
                                         f"({last_seen:.1f}s) — get it in "
                                         f"frame, then retry"}
        p = ch.pose
        return absolute_fix("charger", {
            "charger_x": round(float(p.position.x), 1),
            "charger_y": round(float(p.position.y), 1),
            "last_seen_s": round(last_seen, 2)})
    except Exception as e:
        return {"ok": False, "error": repr(e)[:200]}
