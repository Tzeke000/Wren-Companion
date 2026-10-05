"""Predicted-vs-observed for my own PTZ head — a small, honest world model.

Origin: Zeke + Vale's research handoff (2026-10-04, item 3 "lightweight prediction";
the 09-30 "organ donor" guide's protocol). Borrowed mechanism: the world-model idea
from Cosmos / WoW, shrunk to the one physical system I act on every day — before a
move I PREDICT where the scene should land, after it I MEASURE where it did, and the
prediction error is the signal that my model of my own body is wrong.

The observed failure this targets (2026-10-02/03): after the servo jogged, the head's
position registers read "pan 0 / tilt 10, confirmed" while the view sat ~15 deg off,
for SIX hours. `confirmed` only means the WinRT register echoed back
(visual_attention.bearing) — it is blind to jog motion. Nothing in the live runtime
compared a commanded move with the image (body_verify checks DIRECTION only; the
offline scripts/measure_ptz_move.py was the only magnitude check).

Two checks:
  check_move(before, after, d_pan, d_tilt)  — a commanded move: predicted pixel
      shift (pinhole, pure rotation: f*tan(dtheta)) vs measured (phase correlation).
  check_home(frame)  — the view I EXPECT at home is a stored reference image; the
      measured offset from it IS the head's true error from home, in degrees,
      independent of the registers. This is the check that would have caught 10-02.

Verdicts are three-valued on purpose (Reflexion-style outcomes, no false certainty):
  match / mismatch / uncertain   (moves)
  at_home / off_home / uncertain (home)
`uncertain` = the image can't say (dark, low texture, someone walked through, the
lighting changed). It never silently becomes a pass.

State (consumers named — the guide's rule: a state that nobody reads is not real):
  state/attention/ptz_model.json       — register_trust + calibration + counters.
      READ BY: room_map _head_bearing (stale trust => bearing confirmed:false),
               the servo's home paths (re-sync on off_home), the self-check cron.
  state/attention/ptz_predictions.jsonl — every prediction + observation + verdict.
  state/attention/home_ref.png + home_ref.json — the reference home view.

Pure functions take frames as arguments so scripts/test_ptz_predict.py can prove the
maths on synthetic shifts without moving hardware.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    _CV = True
except Exception:  # pragma: no cover
    _CV = False

_ROOT = Path(__file__).resolve().parents[1]
_ATT = _ROOT / "state" / "attention"
MODEL_PATH = _ATT / "ptz_model.json"
LOG_PATH = _ATT / "ptz_predictions.jsonl"
HOME_REF_PNG = _ATT / "home_ref.png"
HOME_REF_META = _ATT / "home_ref.json"
GEOMETRY_PATH = _ROOT / "state" / "room_geometry.json"

ANALYSIS_W = 320            # frames are downscaled to this width before correlating
_LOCK = threading.RLock()

# Judgement thresholds (degrees). Tuned from the live test ladder — see the
# calibration notes in ptz_model.json once `calibrate` has run.
MOVE_TOL_ABS_DEG = 2.0       # a move may land this far from prediction and still match
MOVE_TOL_REL = 0.30          # ... or this fraction of the commanded size
HOME_TOL_DEG = 3.0           # |offset from the home reference| within this = at home
MIN_RESPONSE = 0.08          # phase-correlation peak below this = can't judge
MIN_TEXTURE = 4.0            # gradient-magnitude mean below this = too dark/flat


# ── geometry ────────────────────────────────────────────────────────────────

def _geometry() -> dict:
    try:
        return json.loads(GEOMETRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_model() -> dict:
    try:
        m = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
        if isinstance(m, dict):
            return m
    except Exception:
        pass
    geo = _geometry()
    return {
        "hfov_deg": float(geo.get("hfov_deg", 68.0)),
        "pan_sign": float(geo.get("pan_sign", -1.0)),   # MEASURED 08-30, never guessed
        "tilt_sign": None,                               # NOT measured yet -> calibrate
        "register_trust": "unknown",
        "register_trust_since": None,
        "register_trust_reason": "no check has run yet",
        "counts": {"match": 0, "mismatch": 0, "uncertain": 0,
                   "at_home": 0, "off_home": 0, "home_uncertain": 0},
        "calib_samples": [],   # [{cmd_pan, cmd_tilt, obs_dx, obs_dy}] for self-calibration
        "last_check": None,
    }


def save_model(m: dict) -> None:
    _ATT.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
    os.replace(tmp, MODEL_PATH)


def focal_px(width: int, hfov_deg: float) -> float:
    """Pinhole focal length in pixels for an image `width` px wide."""
    return (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def predict_shift(d_pan: float, d_tilt: float, *, width: int = ANALYSIS_W,
                  hfov_deg: float = 68.0, pan_sign: float = -1.0,
                  tilt_sign: Optional[float] = None) -> dict:
    """Predicted image-content shift (px, at `width`) for a pure-rotation move.

    Pan: content moves opposite to the head, times the rig's MEASURED pan_sign
    (same convention as body_verify._expected_direction_ok: with pan_sign=-1 a
    +pan command shifts content to +x). Tilt sign is unknown until calibrated, so
    the tilt prediction is a MAGNITUDE only (dy_sign_known False).
    """
    f = focal_px(width, hfov_deg)
    dx = -pan_sign * f * math.tan(math.radians(d_pan))
    mag_dy = f * math.tan(math.radians(abs(d_tilt)))
    if tilt_sign is None:
        dy = mag_dy
        known = False
    else:
        dy = -tilt_sign * f * math.tan(math.radians(d_tilt))
        known = True
    return {"dx": dx, "dy": dy, "dy_sign_known": known, "f_px": f, "width": width}


# ── observation ─────────────────────────────────────────────────────────────

def _prep(frame) -> "np.ndarray":
    """Downscale to ANALYSIS_W, grayscale, gradient magnitude.

    Gradients, not raw intensity: the room's lighting changes (lamp, daylight,
    monitor glow) shift intensities wholesale; edges stay put. That is what makes
    the home reference usable across a day.
    """
    if frame.ndim == 3:
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        g = frame
    h, w = g.shape[:2]
    if w != ANALYSIS_W:
        g = cv2.resize(g, (ANALYSIS_W, max(1, int(round(h * ANALYSIS_W / w)))),
                       interpolation=cv2.INTER_AREA)
    g = cv2.GaussianBlur(g.astype(np.float32), (3, 3), 0)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def observe_shift(before, after) -> dict:
    """Measured content shift (px at ANALYSIS_W) from `before` to `after` — PRIMARY
    measurer: phase correlation on mean-subtracted gradient images (~1 ms).

    dx>0 = content moved right (same convention as body_verify.measure_shift).
    Mean subtraction matters (research 10-05): with a Hanning window and no mean
    removal, phase correlation fails QUIETLY on dim frames (confident wrong shift,
    response indistinguishable from a good one). Response alone is not a reliable gate,
    which is why a mismatch is cross-checked by ORB before it is believed.
    """
    if not _CV:
        return {"ok": False, "reason": "cv2/numpy unavailable"}
    try:
        a, b = _prep(before), _prep(after)
        if a.shape != b.shape:
            b = cv2.resize(b, (a.shape[1], a.shape[0]))
        texture = float(min(a.mean(), b.mean()))
        a = a - float(a.mean())
        b = b - float(b.mean())
        win = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
        (dx, dy), resp = cv2.phaseCorrelate(a, b, win)
        return {"ok": True, "method": "phase", "dx": float(dx), "dy": float(dy),
                "response": round(float(resp), 4), "texture": round(texture, 2),
                "width": int(a.shape[1]), "height": int(a.shape[0])}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "method": "phase", "reason": repr(e)[:160]}


ORB_MIN_INLIERS = 20


def observe_shift_orb(before, after) -> dict:
    """SECOND, independent measurer: ORB features + RANSAC similarity transform at
    640 px (CLAHE + low FAST threshold so dim rooms still yield keypoints). Fails
    LOUDLY (few inliers) where phase correlation fails quietly — so it is the
    cross-check, not the primary. Shift reported at ANALYSIS_W scale."""
    if not _CV:
        return {"ok": False, "method": "orb", "reason": "cv2/numpy unavailable"}
    try:
        def g(f):
            x = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if f.ndim == 3 else f
            h, w = x.shape[:2]
            x = cv2.resize(x, (640, int(round(h * 640 / w))), interpolation=cv2.INTER_AREA)
            return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(x.astype(np.uint8))
        a, b = g(before), g(after)
        orb = cv2.ORB_create(nfeatures=1000, fastThreshold=7)
        ka, da = orb.detectAndCompute(a, None)
        kb, db = orb.detectAndCompute(b, None)
        if da is None or db is None or len(ka) < 8 or len(kb) < 8:
            return {"ok": False, "method": "orb", "reason": "too few keypoints",
                    "keypoints": [len(ka or []), len(kb or [])]}
        matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
        if len(matches) < 8:
            return {"ok": False, "method": "orb", "reason": "too few matches", "matches": len(matches)}
        pa = np.float32([ka[mm.queryIdx].pt for mm in matches])
        pb = np.float32([kb[mm.trainIdx].pt for mm in matches])
        M, inl = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=3.0)
        n_in = int(inl.sum()) if inl is not None else 0
        if M is None or n_in < ORB_MIN_INLIERS:
            return {"ok": False, "method": "orb", "reason": f"only {n_in} inliers",
                    "inliers": n_in, "matches": len(matches)}
        scale = ANALYSIS_W / 640.0
        rot = math.degrees(math.atan2(M[1, 0], M[0, 0]))
        # Translation of the IMAGE CENTRE (M's own tx,ty are about the top-left
        # corner, so any roll/scale leaks into them — 10-05 live: 4.5 px off phase).
        cx, cy = a.shape[1] / 2.0, a.shape[0] / 2.0
        ccx = M[0, 0] * cx + M[0, 1] * cy + M[0, 2]
        ccy = M[1, 0] * cx + M[1, 1] * cy + M[1, 2]
        return {"ok": True, "method": "orb", "dx": float(ccx - cx) * scale,
                "dy": float(ccy - cy) * scale, "roll_deg": round(rot, 2),
                "inliers": n_in, "matches": len(matches),
                "width": ANALYSIS_W, "height": int(round(a.shape[0] * scale)),
                "texture": 99.0, "response": 1.0}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "method": "orb", "reason": repr(e)[:160]}


def _judgeable(obs: dict) -> Optional[str]:
    if not obs.get("ok"):
        return obs.get("reason", "observation failed")
    if obs.get("texture", 0.0) < MIN_TEXTURE:
        return f"scene too dark/flat to judge (texture {obs.get('texture')} < {MIN_TEXTURE})"
    if obs.get("response", 0.0) < MIN_RESPONSE:
        return f"weak correlation peak (response {obs.get('response')} < {MIN_RESPONSE})"
    return None


def _obs_to_deg(obs: dict, m: dict):
    """Observed shift -> (pan_deg, tilt_deg, tilt_signed) of the head motion."""
    f = focal_px(obs.get("width", ANALYSIS_W), m.get("hfov_deg", 68.0))
    pan = math.degrees(math.atan(-obs["dx"] / (m.get("pan_sign", -1.0) * f)))
    if m.get("tilt_sign") is not None:
        return pan, math.degrees(math.atan(-obs["dy"] / (m["tilt_sign"] * f))), True
    return pan, math.degrees(math.atan(abs(obs["dy"]) / f)), False  # magnitude only


def _cascade(primary: dict, before, after, score):
    """Score the primary measurer; when it can't judge OR disagrees with the
    expectation, ask ORB. Returns (obs_used, scored, measurers). A 'bad' verdict is
    reported with both measurers only when they AGREE on it; if they contradict each
    other the honest answer is uncertain."""
    why = _judgeable(primary)
    scored = None if why else score(primary)
    if scored is not None and scored["good"]:
        return primary, scored, ["phase"]
    if before is None or after is None:
        if why:
            return primary, {"verdict_override": "uncertain", "detail": why}, []
        return primary, scored, ["phase"]
    orb = observe_shift_orb(before, after)
    if not orb.get("ok"):
        if why:
            return primary, {"verdict_override": "uncertain",
                             "detail": f"{why}; ORB cross-check failed too ({orb.get('reason')})",
                             "orb": orb}, []
        scored["orb"] = orb
        scored["single_measurer"] = True
        return primary, scored, ["phase"]
    s2 = score(orb)
    s2["orb"] = orb
    if why:
        return orb, s2, ["orb"]
    if s2["good"]:
        s2["note"] = "phase correlation disagreed; ORB agrees with the expectation (phase fooled)"
        return orb, s2, ["orb"]
    # Both measurers say "not as expected". They AGREE if their readings are close in
    # pixels OR within 25% of each other's magnitude (a 6-deg offset read as 6.0 vs 5.3
    # is the same finding; a 6 vs 0.5 is a contradiction).
    def _close(u, v):
        return abs(u - v) <= max(3.0, 0.25 * max(abs(u), abs(v)))
    agree = _close(orb["dx"], primary["dx"]) and _close(orb["dy"], primary["dy"])
    if agree:
        scored["orb"] = orb
        return primary, scored, ["phase", "orb"]
    return primary, {"verdict_override": "uncertain",
                     "detail": "the two measurers contradict each other",
                     "orb": orb, "phase": primary}, []


# ── judgements ──────────────────────────────────────────────────────────────

def judge_move(d_pan: float, d_tilt: float, obs: dict, model: Optional[dict] = None,
               before=None, after=None) -> dict:
    """Compare a commanded move with the observed shift. Pure (no I/O). Pass the
    frames to enable the ORB cross-check of a mismatch."""
    m = model or load_model()
    pred = predict_shift(d_pan, d_tilt, width=obs.get("width", ANALYSIS_W),
                         hfov_deg=m.get("hfov_deg", 68.0), pan_sign=m.get("pan_sign", -1.0),
                         tilt_sign=m.get("tilt_sign"))
    tol_pan = max(MOVE_TOL_ABS_DEG, MOVE_TOL_REL * abs(d_pan))
    tol_tilt = max(MOVE_TOL_ABS_DEG, MOVE_TOL_REL * abs(d_tilt))

    def score(o):
        op, ot, signed = _obs_to_deg(o, m)
        ep = op - d_pan
        et = (ot - d_tilt) if signed else (ot - abs(d_tilt))
        return {"good": abs(ep) <= tol_pan and abs(et) <= tol_tilt,
                "observed_deg": {"pan": round(op, 2), "tilt": round(ot, 2)},
                "error_deg": {"pan": round(ep, 2), "tilt": round(et, 2)}}

    used, sc, measurers = _cascade(obs, before, after, score)
    out = {"kind": "move", "cmd": {"d_pan": d_pan, "d_tilt": d_tilt},
           "predicted": {k: round(v, 2) if isinstance(v, float) else v for k, v in pred.items()},
           "observed": used, "measurers": measurers,
           "tolerance_deg": {"pan": round(tol_pan, 2), "tilt": round(tol_tilt, 2)}}
    if "verdict_override" in sc:
        out["verdict"] = sc.pop("verdict_override")
        out.update(sc)
        return out
    good = sc.pop("good")
    out.update(sc)
    out["verdict"] = "match" if good else "mismatch"
    if not good:
        od, ed = sc["observed_deg"], sc["error_deg"]
        out["detail"] = (f"commanded pan {d_pan:+.1f}/tilt {d_tilt:+.1f} deg; the image says "
                         f"pan {od['pan']:+.1f}/tilt {od['tilt']:+.1f} "
                         f"(error {ed['pan']:+.1f}/{ed['tilt']:+.1f})")
    return out


def judge_home(ref, frame, model: Optional[dict] = None) -> dict:
    """How far is the current view from the stored home view? Pure (no I/O)."""
    m = model or load_model()
    obs = observe_shift(ref, frame)

    def score(o):
        op, ot, signed = _obs_to_deg(o, m)
        return {"good": abs(op) <= HOME_TOL_DEG and abs(ot) <= HOME_TOL_DEG,
                "offset_deg": {"pan": round(op, 2), "tilt": round(ot, 2),
                               "tilt_sign_known": signed}}

    used, sc, measurers = _cascade(obs, ref, frame, score)
    out = {"kind": "home", "observed": used, "measurers": measurers,
           "tolerance_deg": HOME_TOL_DEG}
    if "verdict_override" in sc:
        out["verdict"] = sc.pop("verdict_override")
        out.update(sc)
        return out
    good = sc.pop("good")
    out.update(sc)
    out["verdict"] = "at_home" if good else "off_home"
    if not good:
        od = sc["offset_deg"]
        out["detail"] = (f"view is {od['pan']:+.1f} deg pan / {od['tilt']:+.1f} deg tilt from the "
                         f"home reference — the registers may be stale")
    return out


# ── I/O wrappers used by tools ──────────────────────────────────────────────

def grab_frame(max_age_sec: float = 3.0):
    try:
        from brain.frame_store import get_buffered_frame
        return getattr(get_buffered_frame(max_age_sec=max_age_sec), "frame", None)
    except Exception:
        return None


def _log(row: dict) -> None:
    try:
        _ATT.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def _set_trust(m: dict, trust: str, reason: str) -> None:
    if m.get("register_trust") != trust:
        m["register_trust_since"] = time.time()
    m["register_trust"] = trust
    m["register_trust_reason"] = reason


def record(result: dict, *, source: str, bearing: Optional[dict] = None) -> dict:
    """Persist a judgement and update register_trust. Returns the result (+ts)."""
    with _LOCK:
        m = load_model()
        v = result.get("verdict")
        key = {"uncertain": "uncertain" if result.get("kind") == "move" else "home_uncertain"}.get(v, v)
        if key:
            m.setdefault("counts", {}).setdefault(key, 0)
            m["counts"][key] += 1
        if v == "off_home":
            _set_trust(m, "stale", result.get("detail", "off_home"))
        elif v == "at_home":
            _set_trust(m, "ok", "home view matched the reference")
        elif v == "mismatch":
            _set_trust(m, "suspect", result.get("detail", "move mismatch"))
        elif v == "match" and m.get("register_trust") in ("suspect", "unknown"):
            _set_trust(m, "ok", "a commanded move landed where predicted")
        if result.get("kind") == "move" and v in ("match", "mismatch"):
            obs = result.get("observed") or {}
            samples = m.setdefault("calib_samples", [])
            samples.append({"cmd_pan": result["cmd"]["d_pan"], "cmd_tilt": result["cmd"]["d_tilt"],
                            "obs_dx": round(obs.get("dx", 0.0), 2), "obs_dy": round(obs.get("dy", 0.0), 2),
                            "ts": round(time.time(), 1)})
            del samples[:-60]
        m["last_check"] = {"ts": time.time(), "kind": result.get("kind"), "verdict": v,
                           "source": source}
        save_model(m)
        row = dict(result)
        row.update(ts=time.time(), source=source, bearing=bearing)
        _log(row)
        result["register_trust"] = m["register_trust"]
        return result


def register_trust() -> dict:
    """Cheap read for consumers (room_map, servo, self-check)."""
    m = load_model()
    return {"trust": m.get("register_trust", "unknown"),
            "since": m.get("register_trust_since"),
            "reason": m.get("register_trust_reason")}


def home_ref_load():
    if not HOME_REF_PNG.exists():
        return None
    try:
        return cv2.imread(str(HOME_REF_PNG), cv2.IMREAD_GRAYSCALE)
    except Exception:
        return None


def home_ref_save(frame, meta: dict) -> dict:
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    h, w = g.shape[:2]
    small = cv2.resize(g, (ANALYSIS_W * 2, int(round(h * ANALYSIS_W * 2 / w))),
                       interpolation=cv2.INTER_AREA)
    _ATT.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(HOME_REF_PNG), small)
    meta = dict(meta)
    meta.update(ts=time.time(), shape=list(small.shape), src_shape=[int(h), int(w)])
    HOME_REF_META.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    return meta


def calibrate_from_samples(m: Optional[dict] = None) -> dict:
    """Self-calibration: estimate the focal length (=> effective HFOV) and the tilt
    sign from logged (commanded, observed) pairs. Advisory — written to the model
    under 'calibration', NOT applied to hfov_deg until a human-visible step does so.
    """
    m = m or load_model()
    pan_f, tilt_f, tilt_signs = [], [], []
    for s in m.get("calib_samples", []):
        if abs(s["cmd_pan"]) >= 3 and abs(s["obs_dx"]) > 2:
            pan_f.append(abs(s["obs_dx"]) / math.tan(math.radians(abs(s["cmd_pan"]))))
        if abs(s["cmd_tilt"]) >= 3 and abs(s["obs_dy"]) > 2:
            tilt_f.append(abs(s["obs_dy"]) / math.tan(math.radians(abs(s["cmd_tilt"]))))
            # content moves opposite to head: sign = -sign(obs_dy)*sign(cmd)
            tilt_signs.append(-math.copysign(1, s["obs_dy"]) * math.copysign(1, s["cmd_tilt"]))
    out: dict[str, Any] = {"n_pan": len(pan_f), "n_tilt": len(tilt_f)}
    if pan_f:
        fpan = float(np.median(pan_f))
        out["f_pan_px"] = round(fpan, 1)
        out["hfov_from_pan_deg"] = round(math.degrees(2 * math.atan((ANALYSIS_W / 2) / fpan)), 2)
    if tilt_f:
        out["f_tilt_px"] = round(float(np.median(tilt_f)), 1)
    if tilt_signs:
        agree = sum(1 for s in tilt_signs if s == tilt_signs[0])
        out["tilt_sign_votes"] = {"+1": tilt_signs.count(1.0), "-1": tilt_signs.count(-1.0)}
        out["tilt_sign_consistent"] = agree == len(tilt_signs)
    return out
