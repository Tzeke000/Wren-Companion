# SELF_ASSESSMENT: I am Iris's proto-cerebellum — before the body moves I write
# down what it EXPECTS to happen; after, I check reality and keep the error.
# The mismatch is the lesson (GrowBot video 2026-07-19, Zeke directive).
"""Prediction ledger — cerebellum v0 (built 2026-07-20 pre-deployment).

THE IDEA (from the missing-cerebellum insight): a mind that acts well doesn't
just sense — it PREDICTS the near future and learns from prediction ERROR.
Real cerebella do this at 50Hz in-weights; that's out of reach for now. What IS
in reach tonight, and genuinely load-bearing:

  1. Every predictable mission records an EXPECTATION first (final pose,
     duration) computed from simple kinematics + calibration constants.
  2. On completion the ACTUAL outcome is compared; the error record goes to
     `state/vector/predictions.jsonl` — a growing dataset of how wrong my
     physical imagination is, per context. This is the raw-experience log
     Zeke asked for ("get raw experience from using the Vector robot").
  3. `stats()` summarizes calibration quality — when the error distribution
     tightens, my imagination is getting better; contexts with fat errors are
     exactly where a learned model (or new calibration) is needed.
  4. `expect()`/`resolve()` are the ad-hoc pair for the MIMIC GAME and any
     prediction I (L3) want to make out loud and be graded on.

v0 is OBSERVATIONAL — it never changes control. First the error signal must
exist; using it comes after we've seen its shape. (Track A, post-deployment
worklist.)

Activation: brain/* — INERT until brain_hot_swap + (for pilot hooks) the
pilot module reload; no session-instance dependency (module functions only).
"""
from __future__ import annotations

import contextlib
import json
import math
import threading
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "state" / "vector" / "predictions.jsonl"
KEEP = 600                      # ledger trimmed to this many recent records

# Calibration priors (observed, not aspirational — update as measurements land):
# body.md: servo legs ~62s gate-to-gate on the cone course; body_goto 129 steps
# for a long run. Effective ground speed incl. re-aims/settles is far below the
# commanded max — start at 85 mm/s and let the ledger correct us.
EFFECTIVE_SPEED_MM_S = 85.0
TURN_RATE_DEG_S = 100.0         # gyro turns command 140 but settle+restore tax
FIXED_OVERHEAD_S = 2.0          # per-mission spin-up (threads, first setpoint)

_LOCK = threading.Lock()
_open: dict = {}                # pid -> pending prediction record


def _append(rec: dict) -> None:
    with contextlib.suppress(Exception):
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with LEDGER.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            # occasional trim (cheap enough to do probabilistically)
            if uuid.uuid4().int % 25 == 0:
                lines = LEDGER.read_text(encoding="utf-8").strip().splitlines()
                if len(lines) > KEEP:
                    LEDGER.write_text("\n".join(lines[-KEEP:]) + "\n",
                                      encoding="utf-8")


def _pose(state: dict) -> tuple:
    return (state.get("x"), state.get("y"), state.get("heading"),
            state.get("origin"))


# --------------------------------------------------------------- mission API
def predict_mission(mission: dict, state: dict) -> str | None:
    """Pilot calls this at mission start. Returns a pid to resolve later, or
    None when the mission kind isn't predictable (scan/dock/explore — those
    have no single kinematic expectation worth scoring yet)."""
    kind = str(mission.get("kind") or "")
    x0, y0, h0, origin = _pose(state)
    if x0 is None or y0 is None:
        return None
    try:
        if kind in ("servo", "goto"):
            tx, ty = mission.get("x"), mission.get("y")
            if mission.get("relative") or tx is None or ty is None:
                bd = mission.get("bearing_deg")
                dm = mission.get("dist_mm")
                if dm is None:
                    return None
                brg = math.radians(float(h0 or 0.0) + float(bd or 0.0))
                tx = float(x0) + float(dm) * math.cos(brg)
                ty = float(y0) + float(dm) * math.sin(brg)
            dist = math.hypot(float(tx) - float(x0), float(ty) - float(y0))
            standoff = float(mission.get("standoff_mm") or 25.0)
            pred = {
                "final_x": round(float(tx), 1), "final_y": round(float(ty), 1),
                "final_within_mm": round(standoff + 40.0, 1),
                "duration_s": round(FIXED_OVERHEAD_S
                                    + dist / EFFECTIVE_SPEED_MM_S, 1),
                "path_mm": round(dist, 1),
            }
        elif kind == "route":
            pts = list(mission.get("points") or [])
            if not pts:
                return None
            total = 0.0
            cx, cy = float(x0), float(y0)
            for p in pts:
                total += math.hypot(float(p[0]) - cx, float(p[1]) - cy)
                cx, cy = float(p[0]), float(p[1])
            pred = {
                "final_x": round(cx, 1), "final_y": round(cy, 1),
                "final_within_mm": round(float(mission.get("standoff_mm")
                                               or 30.0) + 50.0, 1),
                "duration_s": round(FIXED_OVERHEAD_S + len(pts) * 1.5
                                    + total / EFFECTIVE_SPEED_MM_S, 1),
                "path_mm": round(total, 1),
            }
        else:
            return None
    except Exception:
        return None
    pid = uuid.uuid4().hex[:10]
    with _LOCK:
        _open[pid] = {"pid": pid, "t0": time.time(), "kind": kind,
                      "start": {"x": x0, "y": y0, "h": h0, "origin": origin},
                      "pred": pred}
        # never let abandoned predictions accumulate
        if len(_open) > 12:
            oldest = min(_open, key=lambda k: _open[k]["t0"])
            _open.pop(oldest, None)
    return pid


def check_mission(pid: str | None, result: dict, state: dict) -> dict | None:
    """Pilot calls this at mission end. Scores prediction vs reality, ledgers
    the record, returns the error summary (or None if pid unknown)."""
    if not pid:
        return None
    with _LOCK:
        rec = _open.pop(pid, None)
    if rec is None:
        return None
    x1, y1, h1, origin1 = _pose(state)
    actual_dur = round(time.time() - rec["t0"], 1)
    err: dict = {"duration_s_pred": rec["pred"]["duration_s"],
                 "duration_s_actual": actual_dur,
                 "time_err_s": round(actual_dur - rec["pred"]["duration_s"], 1)}
    outcome = ("arrived" if result.get("ok") or result.get("arrived")
               else "aborted" if result.get("aborted")
               else "blocked/failed")
    if (x1 is not None and y1 is not None
            and origin1 == rec["start"].get("origin")):
        pos_err = math.hypot(float(x1) - rec["pred"]["final_x"],
                             float(y1) - rec["pred"]["final_y"])
        err["pos_err_mm"] = round(pos_err, 1)
        err["pos_within_pred"] = pos_err <= rec["pred"]["final_within_mm"]
    else:
        err["pos_err_mm"] = None       # frame reset mid-mission = unscorable
    # surprise 0..1: how wrong was the imagination (only meaningful on arrival;
    # a blocked mission is the WORLD's surprise, scored separately)
    if outcome == "arrived":
        p_part = min(1.0, (err["pos_err_mm"] or 0.0)
                     / max(1.0, rec["pred"]["final_within_mm"] * 3.0))
        t_part = min(1.0, abs(err["time_err_s"])
                     / max(3.0, rec["pred"]["duration_s"]))
        err["surprise"] = round(0.5 * p_part + 0.5 * t_part, 2)
    full = {"t": round(time.time(), 2), "kind": rec["kind"],
            "outcome": outcome, "pred": rec["pred"], "err": err,
            "final_dist_mm": result.get("final_dist_mm"),
            "steps": result.get("steps")}
    _append(full)
    return err


# ------------------------------------------------- ad-hoc / mimic-game API
def expect(label: str, prediction: dict, context: str = "") -> dict:
    """I (L3) state an expectation out loud before acting — free-form dict
    (e.g. {'heading_change_deg': -90, 'duration_s': 4}). Graded by resolve()."""
    pid = f"adhoc-{uuid.uuid4().hex[:8]}"
    with _LOCK:
        _open[pid] = {"pid": pid, "t0": time.time(), "kind": "adhoc",
                      "label": str(label)[:80], "pred": dict(prediction or {}),
                      "context": str(context)[:200]}
    return {"ok": True, "pid": pid}


def resolve(pid: str, actual: dict, note: str = "") -> dict:
    """Grade an expect() against what actually happened. Numeric keys shared
    between pred and actual get an error column; the rest ride along."""
    with _LOCK:
        rec = _open.pop(pid, None)
    if rec is None:
        return {"ok": False, "error": f"unknown pid {pid!r}"}
    pred, err = rec.get("pred", {}), {}
    for k, pv in pred.items():
        av = (actual or {}).get(k)
        with contextlib.suppress(Exception):
            err[k] = round(float(av) - float(pv), 2)
    full = {"t": round(time.time(), 2), "kind": "adhoc",
            "label": rec.get("label"), "pred": pred,
            "actual": dict(actual or {}), "err": err,
            "elapsed_s": round(time.time() - rec["t0"], 1),
            "note": str(note)[:200]}
    _append(full)
    return {"ok": True, "graded": full}


# ------------------------------------------------------------------- stats
def stats(n: int = 60) -> dict:
    """How good is my physical imagination lately? Mean/worst errors over the
    last n scorable records + the contexts that surprise me most."""
    try:
        lines = LEDGER.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return {"ok": True, "records": 0,
                "note": "no predictions ledgered yet"}
    recs = []
    for ln in lines[-int(n):]:
        with contextlib.suppress(Exception):
            recs.append(json.loads(ln))
    scored = [r for r in recs
              if r.get("err", {}).get("pos_err_mm") is not None
              and r.get("outcome") == "arrived"]
    out = {"ok": True, "records": len(recs), "scored_arrivals": len(scored),
           "blocked_or_failed": sum(1 for r in recs
                                    if r.get("outcome") == "blocked/failed")}
    if scored:
        pos = [r["err"]["pos_err_mm"] for r in scored]
        tim = [abs(r["err"]["time_err_s"]) for r in scored]
        sur = [r["err"].get("surprise") for r in scored
               if r["err"].get("surprise") is not None]
        out.update({
            "pos_err_mm_mean": round(sum(pos) / len(pos), 1),
            "pos_err_mm_worst": round(max(pos), 1),
            "time_err_s_mean": round(sum(tim) / len(tim), 1),
            "time_err_s_worst": round(max(tim), 1),
            "surprise_mean": round(sum(sur) / len(sur), 2) if sur else None,
            "verdict": ("imagination well-calibrated" if sur
                        and sum(sur) / len(sur) < 0.25 else
                        "imagination mis-calibrated — mine the ledger for the"
                        " fat-error contexts"),
        })
    return out
