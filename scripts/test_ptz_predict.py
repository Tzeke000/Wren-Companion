"""Offline proof for brain/ptz_predict.py — no hardware moves.

Synthetic scene = a large canvas of textured blobs; a camera "view" is a crop. A pan
of d degrees is simulated by sliding the crop by exactly the pinhole shift the model
predicts, so the tests prove the maths AND the measurement end to end. The key case
is #7: the 10-02 failure — registers say home, the view is 15 deg off — must read
off_home with an offset near 15 deg.

Run: .venv/Scripts/python.exe scripts/test_ptz_predict.py
"""
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2  # noqa: E402
import numpy as np  # noqa: E402
from brain import ptz_predict as pp  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {info}")


W, H = 1280, 720
rng = np.random.default_rng(7)
canvas = np.full((H + 400, W * 3, 3), 90, np.uint8)
for _ in range(900):
    x, y = int(rng.integers(0, canvas.shape[1])), int(rng.integers(0, canvas.shape[0]))
    r = int(rng.integers(6, 60))
    col = tuple(int(c) for c in rng.integers(0, 255, 3))
    if rng.random() < 0.5:
        cv2.circle(canvas, (x, y), r, col, -1)
    else:
        cv2.rectangle(canvas, (x, y), (x + r, y + int(r * 0.7)), col, -1)
canvas = cv2.GaussianBlur(canvas, (5, 5), 0)
X0, Y0 = W, 200
MODEL = {"hfov_deg": 68.0, "pan_sign": -1.0, "tilt_sign": None}
F_FULL = pp.focal_px(W, 68.0)


def view(pan_deg=0.0, content_dy_px=0):
    """The view after the head pans `pan_deg` from home (pan_sign=-1 rig):
    content shifts right by f*tan(pan) => the crop window slides LEFT."""
    s = int(round(F_FULL * math.tan(math.radians(pan_deg))))
    x = X0 - s
    y = Y0 - content_dy_px
    return canvas[y:y + H, x:x + W].copy()


print("== 1. prediction: sign + magnitude ==")
p = pp.predict_shift(8.0, 0.0, width=320, hfov_deg=68.0, pan_sign=-1.0)
exp = pp.focal_px(320, 68.0) * math.tan(math.radians(8))
check("pan +8 predicts content +x", p["dx"] > 0, f"dx={p['dx']:.2f}")
check("magnitude = f*tan(8)", abs(p["dx"] - exp) < 1e-6, f"({exp:.2f})")
check("tilt sign unknown until calibrated", p["dy_sign_known"] is False)

print("== 2. observation recovers a known shift ==")
o = pp.observe_shift(view(0), view(8))
check("observe ok", o["ok"], str(o))
check("dx within 1px of prediction", abs(o["dx"] - exp) < 1.0, f"dx={o['dx']:.2f} vs {exp:.2f}")
check("dy ~0", abs(o["dy"]) < 1.0, f"dy={o['dy']:.2f}")
check("response healthy", o["response"] > 0.2, f"resp={o['response']}")

print("== 3. a correct move matches ==")
j = pp.judge_move(8.0, 0.0, pp.observe_shift(view(0), view(8)), MODEL)
check("verdict match", j["verdict"] == "match", str(j.get("error_deg")))

print("== 4. a LYING actuator (said moved, image still) is a mismatch ==")
j = pp.judge_move(8.0, 0.0, pp.observe_shift(view(0), view(0)), MODEL)
check("verdict mismatch", j["verdict"] == "mismatch", j.get("detail", ""))

print("== 5. moved the wrong amount (15 instead of 8) is a mismatch ==")
j = pp.judge_move(8.0, 0.0, pp.observe_shift(view(0), view(15)), MODEL)
check("verdict mismatch", j["verdict"] == "mismatch", j.get("detail", ""))
check("observed ~15", abs(j["observed_deg"]["pan"] - 15) < 1.0, str(j.get("observed_deg")))

print("== 6. moved the WRONG WAY is a mismatch ==")
j = pp.judge_move(8.0, 0.0, pp.observe_shift(view(0), view(-8)), MODEL)
check("verdict mismatch", j["verdict"] == "mismatch", str(j.get("observed_deg")))

print("== 7. THE 10-02 CASE: registers say home, view is 15 deg off ==")
ref = view(0)
j = pp.judge_home(ref, view(15), MODEL)
check("verdict off_home", j["verdict"] == "off_home", j.get("detail", ""))
check("offset ~ +15", abs(j["offset_deg"]["pan"] - 15) < 1.0, str(j.get("offset_deg")))
j = pp.judge_home(ref, view(-6), MODEL)
check("-6 also off_home, offset ~ -6", j["verdict"] == "off_home" and abs(j["offset_deg"]["pan"] + 6) < 1.0,
      str(j.get("offset_deg")))
j = pp.judge_home(ref, view(1), MODEL)
check("1 deg off is still at_home", j["verdict"] == "at_home", str(j.get("offset_deg")))

print("== 8. lighting change does not break the home check (gradients) ==")
dim = cv2.convertScaleAbs(view(0), alpha=0.45, beta=20)
j = pp.judge_home(ref, dim, MODEL)
check("dimmed home view still at_home", j["verdict"] == "at_home", str(j.get("offset_deg")))
dim15 = cv2.convertScaleAbs(view(15), alpha=0.45, beta=20)
j = pp.judge_home(ref, dim15, MODEL)
check("dimmed 15-off still off_home", j["verdict"] == "off_home", str(j.get("offset_deg")))

print("== 9. a dark room is UNCERTAIN, never a pass ==")
dark = np.full((H, W, 3), 6, np.uint8) + rng.integers(0, 3, (H, W, 3), dtype=np.uint8)
j = pp.judge_home(ref, dark, MODEL)
check("dark -> uncertain", j["verdict"] == "uncertain", j.get("detail", ""))
j = pp.judge_move(8.0, 0.0, pp.observe_shift(dark, dark), MODEL)
check("dark move -> uncertain", j["verdict"] == "uncertain", j.get("detail", ""))

print("== 10. a person walking through (big local change) ==")
busy = view(0)
cv2.rectangle(busy, (500, 120), (760, 720), (40, 60, 200), -1)  # a figure-sized occluder
j = pp.judge_home(ref, busy, MODEL)
check("occluded home not misread as off_home", j["verdict"] in ("at_home", "uncertain"),
      f"{j['verdict']} {j.get('offset_deg')}")

print("== 11. record() moves register_trust ==")
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    pp.MODEL_PATH, pp.LOG_PATH = tdp / "m.json", tdp / "l.jsonl"
    pp._ATT = tdp
    r = pp.record(pp.judge_home(ref, view(15), MODEL), source="test")
    check("off_home -> stale", r["register_trust"] == "stale")
    r = pp.record(pp.judge_home(ref, view(0), MODEL), source="test")
    check("at_home -> ok", r["register_trust"] == "ok")
    r = pp.record(pp.judge_move(8.0, 0.0, pp.observe_shift(view(0), view(0)), MODEL), source="test")
    check("mismatch -> suspect", r["register_trust"] == "suspect")
    check("log written", pp.LOG_PATH.exists() and len(pp.LOG_PATH.read_text().splitlines()) == 3)

    print("== 12. self-calibration recovers the HFOV ==")
    m = pp.load_model()
    m["calib_samples"] = []
    for d in (6, -6, 10, -10, 8):
        o = pp.observe_shift(view(0), view(d))
        m["calib_samples"].append({"cmd_pan": d, "cmd_tilt": 0, "obs_dx": o["dx"], "obs_dy": o["dy"]})
    c = pp.calibrate_from_samples(m)
    check("hfov from pan ~68", abs(c.get("hfov_from_pan_deg", 0) - 68) < 1.5, str(c))

print("== 13. ORB cross-check: a lying actuator is confirmed by BOTH measurers ==")
b0, b1 = view(0), view(0)
j = pp.judge_move(8.0, 0.0, pp.observe_shift(b0, b1), MODEL, before=b0, after=b1)
check("mismatch confirmed by phase+orb", j["verdict"] == "mismatch" and j["measurers"] == ["phase", "orb"],
      f"{j['verdict']} {j['measurers']}")
b1 = view(8)
j = pp.judge_move(8.0, 0.0, pp.observe_shift(b0, b1), MODEL, before=b0, after=b1)
check("good move stays phase-only (fast path)", j["verdict"] == "match" and j["measurers"] == ["phase"],
      f"{j['verdict']} {j['measurers']}")
o = pp.observe_shift_orb(view(0), view(8))
check("orb alone recovers the 8-deg shift", o["ok"] and abs(o["dx"] - exp) < 1.5,
      f"{o.get('dx')} inl={o.get('inliers')}")

print("== 14. dim room (15% brightness + noise): home check never lies ==")


def dimmed(img, a):
    n = rng.normal(0, 2, img.shape)
    return np.clip(img.astype(np.float32) * a + n, 0, 255).astype(np.uint8)


j = pp.judge_home(ref, dimmed(view(0), 0.15), MODEL)
check("dim at home -> at_home or uncertain (never off_home)", j["verdict"] in ("at_home", "uncertain"),
      f"{j['verdict']} {j.get('offset_deg')} {j.get('measurers')}")
j = pp.judge_home(ref, dimmed(view(15), 0.15), MODEL)
check("dim 15-off -> off_home or uncertain (never at_home)", j["verdict"] in ("off_home", "uncertain"),
      f"{j['verdict']} {j.get('offset_deg')} {j.get('measurers')}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
