# SELF_ASSESSMENT: I am Iris's OVERHEAD EYE candidate — ArUco marker detection
# on the PC camera, aimed at absolute (x,y) localization for my Vector body.
"""vector_overhead.py — PC-camera overhead localization (2026-07-17 round 2).

Zeke (2026-07-17 ~04:00): "you already have a camera on the pc where you are —
it's how you see my face." If that camera's view covers the desk floor my body
drives on, it becomes the researched overhead-ArUco absolute-position fix
(±5–10mm) with ZERO new hardware. If it only sees faces, it needs re-aiming
(Zeke-present) or the $40 dedicated cam remains the fallback.

Stage 1 (this file, buildable solo): probe what the camera sees + detect ArUco
markers in the frame (cv2.aruco, DICT_4X4_50 — print IDs 0=dock, 1=my back).
Frame access rides brain.camera_live.read_live_frame — the SHARED device path
(never open a second VideoCapture and fight perception for the camera).

Stage 2 (owed, Zeke-present, after tags are printed): homography calibration
(dock tag = origin, tag size = scale) → marker px → desk mm → feed pose
corrections into the odometry the same way the charger-anchor plan describes.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "state" / "vector"
PROBE_JPG = OUT_DIR / "overhead_probe.jpg"
CALIB_JSON = OUT_DIR / "overhead_calibration.json"

ARUCO_DICT = "DICT_4X4_50"


def _detector():
    import cv2
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICT))
    try:
        params = cv2.aruco.DetectorParameters()
        return ("new", cv2.aruco.ArucoDetector(d, params))
    except AttributeError:      # older cv2.aruco API
        return ("old", d)


def detect_markers(frame) -> list:
    """ArUco markers in a BGR frame -> [{id, center_px, corners_px}]."""
    import cv2
    mode, det = _detector()
    if mode == "new":
        corners, ids, _ = det.detectMarkers(frame)
    else:
        corners, ids, _ = cv2.aruco.detectMarkers(frame, det)
    out = []
    if ids is None:
        return out
    for mid, quad in zip(ids.flatten().tolist(), corners):
        pts = quad.reshape(-1, 2)
        out.append({
            "id": int(mid),
            "center_px": [round(float(pts[:, 0].mean()), 1),
                          round(float(pts[:, 1].mean()), 1)],
            "corners_px": [[round(float(x), 1), round(float(y), 1)]
                           for x, y in pts.tolist()],
        })
    return out


# ---- ANKI-FIDUCIAL detection (2026-07-17 ~15:55): Zeke placed my SIX room
# markers — they're Anki SDK fiducials (what the ROBOT detects natively), NOT
# ArUco, so the webcam needs its own detector. Mini-ArUco pipeline: adaptive
# threshold → contour quads → perspective-warp each candidate to a canonical
# square → template-match (4 rotations) against the exact PNGs I printed.
# A matched FLOOR marker (face-up twin) is a 90mm square ON the floor plane —
# its 4 corners fully determine a floor px→mm homography, zero extra
# calibration. Wall markers = the robot's landmarks; floor = the webcam's.

MARKER_DIR = OUT_DIR / "markers"
MARKER_TEMPLATES = {
    "Circles2": "SDK_2Circles.png", "Circles3": "SDK_3Circles.png",
    "Diamonds2": "SDK_2Diamonds.png", "Diamonds3": "SDK_3Diamonds.png",
    "Triangles4": "SDK_4Triangles.png", "Triangles5": "SDK_5Triangles.png",
}
FLOOR_MARKERS = {"Circles3", "Diamonds3", "Triangles5"}
MARKER_MM = 90.0
_TPL_CACHE: dict = {}


def _templates(size: int = 64) -> dict:
    """Templates auto-cropped to the black marker square (the printed sheet
    and the source PNG both carry white margin the contour crop won't have),
    resized to `size`, all 4 rotations."""
    import cv2
    if size in _TPL_CACHE:
        return _TPL_CACHE[size]
    out = {}
    for name, fn in MARKER_TEMPLATES.items():
        img = cv2.imread(str(MARKER_DIR / fn), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        _, dark = cv2.threshold(img, 128, 255, cv2.THRESH_BINARY_INV)
        ys, xs = dark.nonzero()
        if len(xs) == 0:
            continue
        img = img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        t = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
        rots = [t]
        for _ in range(3):
            rots.append(cv2.rotate(rots[-1], cv2.ROTATE_90_CLOCKWISE))
        out[name] = rots
    _TPL_CACHE[size] = out
    return out


def detect_iris_markers(frame, min_score: float = 0.5) -> list:
    """My six room markers in a BGR webcam frame →
    [{name, score, center_px, corners_px(tl,tr,br,bl), floor}]."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # Multi-threshold quad harvest: small/oblique markers survive different
    # binarizations — collect candidate quads from several and de-dup later.
    bins = []
    for blk, C in ((31, 10), (17, 6), (51, 8)):
        with contextlib.suppress(Exception):
            bins.append(cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                cv2.THRESH_BINARY_INV, blk, C))
    with contextlib.suppress(Exception):
        _, otsu = cv2.threshold(gray, 0, 255,
                                cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        bins.append(otsu)
    contours = []
    for b in bins:
        cs, _ = cv2.findContours(b, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours.extend(cs)
    size = 64
    tpls = _templates(size)
    if not tpls:
        return []
    canon = np.float32([[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]])
    found = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < 80 or area > 25000:      # ~9px .. ~160px squares @640x480
            continue
        quad = None
        for eps in (0.04, 0.07, 0.1):
            q = cv2.approxPolyDP(c, eps * cv2.arcLength(c, True), True)
            if len(q) == 4 and cv2.isContourConvex(q):
                quad = q
                break
        if quad is None:
            continue
        pts = quad.reshape(4, 2).astype(np.float32)
        s = pts.sum(1)
        d = np.diff(pts, axis=1).ravel()
        ordered = np.float32([pts[np.argmin(s)], pts[np.argmin(d)],
                              pts[np.argmax(s)], pts[np.argmax(d)]])
        M = cv2.getPerspectiveTransform(ordered, canon)
        patch = cv2.warpPerspective(gray, M, (size, size))
        patch = cv2.normalize(patch, None, 0, 255, cv2.NORM_MINMAX)
        scores: dict = {}
        for name, rots in tpls.items():
            s_best = -1.0
            for t in rots:
                s_best = max(s_best, float(cv2.matchTemplate(
                    patch, t, cv2.TM_CCOEFF_NORMED)[0][0]))
            scores[name] = s_best
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        best_name, best_score = ranked[0]
        margin = best_score - ranked[1][1] if len(ranked) > 1 else 1.0
        if best_name and best_score >= min_score:
            found.append({
                "name": best_name, "score": round(best_score, 3),
                # AMBIGUOUS (2026-07-17): at 640x480 the markers are ~15-25px;
                # circles vs diamonds blur together (0.606 vs 0.594 measured).
                # A mislabeled landmark poisons localization — flag it so
                # callers treat identity as 'a marker is HERE' only.
                "ambiguous": margin < 0.08,
                "second": ranked[1][0] if len(ranked) > 1 else None,
                "center_px": [round(float(ordered[:, 0].mean()), 1),
                              round(float(ordered[:, 1].mean()), 1)],
                "corners_px": [[round(float(x), 1), round(float(y), 1)]
                               for x, y in ordered],
                "floor": best_name in FLOOR_MARKERS,
            })
    found.sort(key=lambda m: -m["score"])
    kept: list = []
    for m in found:                        # de-dup overlapping quads
        if all(abs(m["center_px"][0] - k["center_px"][0])
               + abs(m["center_px"][1] - k["center_px"][1]) > 12 for k in kept):
            kept.append(m)
    return kept


def floor_homography(markers: list) -> dict:
    """Full-floor px→mm map from ONE detected floor marker: a 90mm square on
    the floor plane determines the plane homography (origin = that marker's
    center, axes = its edges). Saved to CALIB_JSON so to_desk_mm works."""
    import cv2
    import numpy as np
    fl = [m for m in markers if m.get("floor") and not m.get("ambiguous")]
    if not fl:
        return {"ok": False, "error": "no unambiguous floor marker detected "
                                      "in frame"}
    m = fl[0]
    src = np.float32(m["corners_px"])
    h = MARKER_MM / 2.0
    dst = np.float32([[-h, -h], [h, -h], [h, h], [-h, h]])
    H = cv2.getPerspectiveTransform(src, dst)
    rec = {"homography": H.tolist(), "anchor": m["name"],
           "anchor_center_px": m["center_px"], "ts": time.time(),
           "note": "floor-plane px->mm; origin = anchor floor-marker center"}
    with contextlib.suppress(Exception):
        CALIB_JSON.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return {"ok": True, **{k: rec[k] for k in ("anchor", "anchor_center_px")},
            "saved": str(CALIB_JSON)}


def probe(save: bool = True) -> dict:
    """Grab ONE frame from the PC camera (shared path — no device fight),
    save it for my own eyes, and report any ArUco markers + basic frame
    stats. This answers 'can the PC cam be my overhead eye?' — Read the
    saved jpg and look for the robot's driving area."""
    import cv2
    try:
        from brain.camera_live import read_live_frame
        frame, ts = read_live_frame()
    except Exception as e:
        return {"ok": False, "error": f"camera_live read failed: {e!r}"[:200]}
    if frame is None:
        return {"ok": False, "error": "no frame from PC camera (device busy/"
                                      "unavailable — perception may own it)"}
    age = round(time.time() - ts, 2) if ts else None
    h, w = frame.shape[:2]
    markers = []
    with contextlib.suppress(Exception):
        markers = detect_markers(frame)
    iris_markers = []
    with contextlib.suppress(Exception):
        iris_markers = detect_iris_markers(frame)
    out = {"ok": True, "w": w, "h": h, "frame_age_s": age,
           "brightness": round(float(cv2.cvtColor(
               frame, cv2.COLOR_BGR2GRAY).mean()), 1),
           "markers": markers, "n_markers": len(markers),
           "iris_markers": iris_markers, "n_iris_markers": len(iris_markers),
           "dict": ARUCO_DICT,
           "hint": "Read the jpg — iris_markers are MY six room fiducials "
                   "(boxes drawn); a detected FLOOR marker auto-calibrates "
                   "the floor px→mm homography."}
    if any(m.get("floor") for m in iris_markers):
        with contextlib.suppress(Exception):
            out["floor_calibration"] = floor_homography(iris_markers)
    if save:
        with contextlib.suppress(Exception):
            vis = frame.copy()
            for m in markers:
                cx, cy = int(m["center_px"][0]), int(m["center_px"][1])
                cv2.circle(vis, (cx, cy), 8, (0, 220, 0), 2)
                cv2.putText(vis, str(m["id"]), (cx + 10, cy),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 220, 0), 2)
            import numpy as _np
            for m in iris_markers:
                pts = _np.int32(m["corners_px"])
                color = (0, 160, 255) if m.get("floor") else (255, 160, 0)
                cv2.polylines(vis, [pts], True, color, 2)
                cv2.putText(vis, f'{m["name"]} {m["score"]:.2f}',
                            (pts[0][0], max(12, pts[0][1] - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(PROBE_JPG), vis)
            out["frame"] = str(PROBE_JPG)
    return out


def to_desk_mm(center_px) -> dict:
    """px -> desk mm via the stored homography. Stage-2: returns not-calibrated
    until the tag-based calibration (Zeke-present) writes CALIB_JSON."""
    try:
        c = json.loads(CALIB_JSON.read_text(encoding="utf-8"))
        H = c["homography"]
    except Exception:
        return {"ok": False, "error": "not calibrated — needs printed tags + "
                                      "the stage-2 calibration pass"}
    x, y = float(center_px[0]), float(center_px[1])
    d = H[2][0] * x + H[2][1] * y + H[2][2]
    return {"ok": True,
            "x_mm": round((H[0][0] * x + H[0][1] * y + H[0][2]) / d, 1),
            "y_mm": round((H[1][0] * x + H[1][1] * y + H[1][2]) / d, 1)}


# ---------------------------------------------------------------- cone referee
# 2026-07-17 late: Zeke watched me drag cone 1 to cone 4 during a servo leg —
# AGAIN (course-run repeat). Cones are ToF-invisible (thin) and dragging never
# trips stuck-detect (motion continues). Every sensor I was listening to was
# blind to it; the OVERHEAD eye sees the orange cones crisply on the lit floor.
# This is the referee: snapshot cone centroids before a leg, compare after.
# Zero calibration needed — pixel-space displacement IS the verdict.
CONES_JSON = OUT_DIR / "cones_snapshot.json"


def detect_cones(frame) -> list:
    """Orange floor obstacles (training cones, and honestly the red shoe) in a
    BGR overhead frame -> [{center_px, area}].

    v2 (2026-07-17 late): absolute HSV thresholds FAILED — this webcam's warm
    cast makes the lamp-lit beige floor read H15-19 S145-164, nearly identical
    to the cones (measured in-process). Cones ARE separable as LOCAL redness
    maxima: score = saturation gated to red-orange hue, subtract a large-blur
    background (uniform floor + big furniture cancel out), threshold the
    difference. Survives AWB drift because everything is relative-to-local."""
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red_hue = ((h <= 14) | (h >= 170)) & (v >= 110)
    score = np.where(red_hue, s, 0).astype(np.float32)
    bg = cv2.blur(score, (61, 61))
    diff = score - bg
    mask = (diff > 55).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cones = []
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                               cv2.CHAIN_APPROX_SIMPLE)
    for c in cnts:
        a = cv2.contourArea(c)
        if not (10 <= a <= 900):         # cone at 640x480 ≈ 15-150 px²
            continue
        M = cv2.moments(c)
        if M["m00"] <= 0:
            continue
        cx, cy = M["m10"] / M["m00"], M["m01"] / M["m00"]
        # FLOOR GATE (this webcam is FIXED): furniture band = top + left edge.
        # Keep only the driving floor: (y>150 and x>140) or far-left low y>270.
        # Re-derive if the camera is ever re-aimed.
        if not ((cy > 150 and cx > 140) or cy > 270):
            continue
        cones.append({"center_px": [round(cx, 1), round(cy, 1)],
                      "area": round(a, 1)})
    cones.sort(key=lambda d: d["center_px"][0])
    return cones


def cone_check(save: bool = True, drift_px: float = 12.0,
               sample: list | None = None) -> dict:
    """Detect cones now, compare against the previous snapshot (nearest-match),
    then save the new snapshot. moved=[] is the clean bill; anything else means
    I displaced the course since the last check. Call BEFORE a drive leg to
    baseline, AFTER it to self-grade honestly (the drag detector)."""
    import json as _json
    try:
        from brain.camera_live import read_live_frame
        frame, ts = read_live_frame()
    except Exception as e:
        return {"ok": False, "error": f"camera read failed: {e!r}"[:180]}
    if frame is None:
        return {"ok": False, "error": "no frame from PC camera"}
    cur = detect_cones(frame)
    out = {"ok": True, "n_cones": len(cur), "cones": cur, "moved": [],
           "prev_age_s": None}
    if sample:
        # debug: report median HSV around given pixel points (threshold tuning)
        import cv2
        import numpy as np
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        m1 = cv2.inRange(hsv, (3, 110, 90), (26, 255, 255))
        m2 = cv2.inRange(hsv, (170, 110, 90), (180, 255, 255))
        raw_mask = m1 | m2
        opened = cv2.morphologyEx(raw_mask, cv2.MORPH_OPEN,
                                  np.ones((3, 3), np.uint8))
        pts = []
        for xy in sample:
            x, y = int(xy[0]), int(xy[1])
            patch = hsv[max(0, y - 3):y + 4, max(0, x - 3):x + 4].reshape(-1, 3)
            w = (slice(max(0, y - 7), y + 8), slice(max(0, x - 7), x + 8))
            pts.append({"xy": [x, y],
                        "hsv_med": np.median(patch, 0).astype(int).tolist(),
                        "hsv_max_s": patch[patch[:, 1].argmax()].tolist(),
                        "mask_px_raw": int((raw_mask[w] > 0).sum()),
                        "mask_px_opened": int((opened[w] > 0).sum())})
        out["samples"] = pts
    prev = None
    with contextlib.suppress(Exception):
        rec = _json.loads(CONES_JSON.read_text(encoding="utf-8"))
        prev = rec.get("cones")
        out["prev_age_s"] = round(time.time() - float(rec.get("ts", 0)), 1)
    if prev:
        for p in prev:
            px, py = p["center_px"]
            best, bd = None, 1e9
            for c in cur:
                d = ((c["center_px"][0] - px) ** 2 +
                     (c["center_px"][1] - py) ** 2) ** 0.5
                if d < bd:
                    best, bd = c, d
            if best is None or bd > drift_px:
                out["moved"].append(
                    {"was_px": [px, py],
                     "now_px": best["center_px"] if best else None,
                     "drift_px": round(bd, 1) if best else None})
        if len(cur) != len(prev):
            out["count_changed"] = {"was": len(prev), "now": len(cur)}
    if save:
        with contextlib.suppress(Exception):
            CONES_JSON.write_text(_json.dumps(
                {"ts": time.time(), "cones": cur}), encoding="utf-8")
    # always drop an annotated frame — the referee must be auditable
    with contextlib.suppress(Exception):
        import cv2
        vis = frame.copy()
        for c in cur:
            x, y = int(c["center_px"][0]), int(c["center_px"][1])
            cv2.circle(vis, (x, y), 10, (0, 255, 0), 2)
            cv2.putText(vis, str(int(c["area"])), (x + 10, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
        p = str(OUT_DIR / "cones_vis.jpg")
        cv2.imwrite(p, vis)
        out["vis"] = p
    return out
