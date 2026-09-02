# SELF_ASSESSMENT: I give the eyes a BODY — 17 joints per person from YOLO-pose on the live frame — and turn them into the three things Zeke asked for: how much of a person I can see, roughly how far away they are, and in plain words what they are doing.
"""
brain/body_pose.py — human body pose for the eyes (Zeke, Discord, 2026-09-02 ~16:1x):

    "human eyes can recognize a lot of things and your eyes might not recognize
     them all like what human bodies look like where a human elbow is compared
     to the head or the knee or the foot … if you can only see from the torso up
     compared to … their feet all the way to their head and you know roughly
     about how tall they are you can tell how close or far away they are …
     or even just trying to understand what said person is doing."

Model: Ultralytics YOLO11n-pose (COCO 17 keypoints), weights in models/pose/,
GPU device 0. Measured on the 3060 at 1280x720: ~20 ms median per frame.
Not reinvented: pose estimation is a solved problem; this file is the glue —
geometry + posture words — not the estimator.

Distance: pinhole model with the camera's MEASURED horizontal FOV (68 deg,
state/room_geometry.json) → f_px = (W/2) / tan(HFOV/2). distance = f_px * H_real
/ h_px, using the best body segment actually visible: nose→ankle (0.87 of
height), shoulder→hip torso (0.30), shoulder width (0.25, facing only). The
person's true height is the ruler — Zeke's is stored once he states it
(state/body_pose_config.json); until then the estimate is flagged `assumed`.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "models" / "pose" / "yolo11n-pose.pt"
CONFIG_PATH = ROOT / "state" / "body_pose_config.json"
GEOMETRY_PATH = ROOT / "state" / "room_geometry.json"

KP_NAMES = ["nose", "l_eye", "r_eye", "l_ear", "r_ear", "l_sho", "r_sho",
            "l_elb", "r_elb", "l_wri", "r_wri", "l_hip", "r_hip",
            "l_knee", "r_knee", "l_ank", "r_ank"]
KP_CONF = 0.5                 # a joint below this is "not seen", not a guess
DEFAULT_HEIGHT_M = 1.75       # only ever used with assumed=True
# Anthropometric fractions of standing height (Drillis & Contini, rounded):
FRAC_NOSE_TO_ANKLE = 0.87
FRAC_TORSO = 0.30             # shoulder centre -> hip centre
FRAC_SHOULDER_WIDTH = 0.25    # biacromial, facing the camera

_LOCK = threading.Lock()
_MODEL: Any = None
_LOAD_ERROR: str | None = None
_STATS: dict[str, Any] = {"calls": 0, "last_ms": None, "avg_ms": None}


def _config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_height(person: str, height_m: float, source: str = "zeke") -> dict[str, Any]:
    """Store a person's true height (their word, never a guess)."""
    cfg = _config()
    heights = cfg.setdefault("heights_m", {})
    heights[str(person).lower()] = {"height_m": float(height_m), "source": source,
                                     "ts": time.time()}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return {"ok": True, "person": person, "height_m": float(height_m)}


def known_height(person: str | None) -> tuple[float, bool]:
    """(height_m, assumed). assumed=True means nobody told me."""
    if person:
        h = (_config().get("heights_m") or {}).get(str(person).lower())
        if h and h.get("height_m"):
            return float(h["height_m"]), False
    return DEFAULT_HEIGHT_M, True


def hfov_deg() -> float:
    try:
        return float(json.loads(GEOMETRY_PATH.read_text(encoding="utf-8")).get("hfov_deg") or 68.0)
    except Exception:
        return 68.0


def focal_px(frame_w: int) -> float:
    return (frame_w / 2.0) / math.tan(math.radians(hfov_deg() / 2.0))


def _load() -> Any:
    global _MODEL, _LOAD_ERROR
    if _MODEL is not None:
        return _MODEL
    with _LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from ultralytics import YOLO  # type: ignore
            if not MODEL_PATH.exists():
                _LOAD_ERROR = f"weights missing: {MODEL_PATH}"
                return None
            m = YOLO(str(MODEL_PATH))
            _MODEL = m
            _LOAD_ERROR = None
        except Exception as e:  # noqa: BLE001
            _LOAD_ERROR = repr(e)[:200]
            return None
    return _MODEL


def status() -> dict[str, Any]:
    return {"loaded": _MODEL is not None, "error": _LOAD_ERROR,
            "weights": str(MODEL_PATH), "weights_present": MODEL_PATH.exists(),
            "hfov_deg": hfov_deg(), "heights_m": _config().get("heights_m") or {},
            **_STATS}


# ── geometry helpers ─────────────────────────────────────────────────────────
def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angle(a, b, c) -> float | None:
    """Angle at b (deg) between ba and bc."""
    try:
        v1 = (a[0] - b[0], a[1] - b[1]); v2 = (c[0] - b[0], c[1] - b[1])
        n1 = math.hypot(*v1); n2 = math.hypot(*v2)
        if n1 < 1e-6 or n2 < 1e-6:
            return None
        cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        return math.degrees(math.acos(cosang))
    except Exception:
        return None


def _vertical_deg(top, bottom) -> float | None:
    """How far a top→bottom segment is from vertical, in degrees (0 = upright)."""
    dx = bottom[0] - top[0]; dy = bottom[1] - top[1]
    if abs(dx) + abs(dy) < 1e-6:
        return None
    return abs(math.degrees(math.atan2(dx, dy)))


def _posture(kp: dict[str, tuple[float, float]]) -> tuple[str, list[str]]:
    """Plain-words posture from what is actually visible. Returns (label, evidence)."""
    ev: list[str] = []
    sho = _mid(kp["l_sho"], kp["r_sho"]) if "l_sho" in kp and "r_sho" in kp else (kp.get("l_sho") or kp.get("r_sho"))
    hip = _mid(kp["l_hip"], kp["r_hip"]) if "l_hip" in kp and "r_hip" in kp else (kp.get("l_hip") or kp.get("r_hip"))
    torso_tilt = _vertical_deg(sho, hip) if (sho and hip) else None
    if torso_tilt is not None:
        ev.append(f"torso {torso_tilt:.0f} deg from vertical")
    # knee angles (either side)
    knee_angles = []
    for s in ("l", "r"):
        h, k, a = kp.get(f"{s}_hip"), kp.get(f"{s}_knee"), kp.get(f"{s}_ank")
        if h and k and a:
            ang = _angle(h, k, a)
            if ang is not None:
                knee_angles.append(ang)
    if knee_angles:
        ev.append(f"knee {max(knee_angles):.0f} deg")
    if torso_tilt is not None and torso_tilt > 60:
        return "lying down", ev
    if knee_angles:
        k = max(knee_angles)
        if k > 150:
            return "standing", ev
        if k < 140:
            # hips roughly level with the knees = sitting; hips well above = crouch
            hips_y = hip[1] if hip else None
            knees = [kp[f"{s}_knee"][1] for s in ("l", "r") if f"{s}_knee" in kp]
            torso_px = _dist(sho, hip) if (sho and hip) else 100.0
            if hips_y is not None and knees and abs(min(knees) - hips_y) < 0.6 * torso_px:
                return "sitting", ev
            return "crouching or sitting", ev
    if sho and hip:
        if torso_tilt is not None and torso_tilt < 30:
            return "upright (legs not visible)", ev
        return "leaning", ev
    if sho:
        return "only head and shoulders visible", ev
    return "unknown", ev


def _hands(kp: dict[str, tuple[float, float]]) -> list[str]:
    out = []
    nose = kp.get("nose")
    for s, word in (("l", "left"), ("r", "right")):
        w = kp.get(f"{s}_wri")
        if w and nose and w[1] < nose[1]:
            out.append(f"{word} hand raised above the head")
    lw, rw = kp.get("l_wri"), kp.get("r_wri")
    if lw and rw and _dist(lw, rw) < 0.6 * (_dist(kp["l_sho"], kp["r_sho"]) if "l_sho" in kp and "r_sho" in kp else 80):
        out.append("hands together")
    return out


def _distance(kp: dict[str, tuple[float, float]], frame_w: int,
              height_m: float) -> dict[str, Any]:
    """Best-available body ruler -> metres. Reports which ruler was used."""
    f = focal_px(frame_w)
    nose = kp.get("nose")
    ank = [kp[k] for k in ("l_ank", "r_ank") if k in kp]
    sho = [kp[k] for k in ("l_sho", "r_sho") if k in kp]
    hip = [kp[k] for k in ("l_hip", "r_hip") if k in kp]
    # Eyes first: interpupillary distance is ~63 mm for adults regardless of
    # height, and it survives sitting/lying/foreshortening (a lying torso
    # read 7.4 m for a man 4 m away). Turned faces shrink it → overestimate.
    if "l_eye" in kp and "r_eye" in kp:
        e_px = _dist(kp["l_eye"], kp["r_eye"])
        if e_px >= 6:
            return {"m": round(f * 0.063 / e_px, 2), "ruler": "eyes (63 mm IPD; turned face = overestimates)",
                    "px": round(e_px, 1), "height_independent": True}
    if nose and ank:
        h_px = max(a[1] for a in ank) - nose[1]
        if h_px > 20:
            return {"m": round(f * (FRAC_NOSE_TO_ANKLE * height_m) / h_px, 2),
                    "ruler": "nose-to-ankle", "px": round(h_px, 1)}
    if sho and hip:
        h_px = _dist(_mid(*sho) if len(sho) == 2 else sho[0], _mid(*hip) if len(hip) == 2 else hip[0])
        if h_px > 10:
            return {"m": round(f * (FRAC_TORSO * height_m) / h_px, 2),
                    "ruler": "torso", "px": round(h_px, 1)}
    if len(sho) == 2:
        w_px = _dist(sho[0], sho[1])
        if w_px > 10:
            return {"m": round(f * (FRAC_SHOULDER_WIDTH * height_m) / w_px, 2),
                    "ruler": "shoulder-width (facing only; turned = overestimates)",
                    "px": round(w_px, 1)}
    return {"m": None, "ruler": None, "px": None}


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a; bx1, by1, bx2, by2 = b
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1)); ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    ua = max(1.0, (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter)
    return inter / ua


def static_shapes() -> list[dict[str, Any]]:
    return list(_config().get("static_shapes") or [])


def mark_static(box: list[int], head: dict[str, Any] | None, label: str) -> dict[str, Any]:
    """Remember a humanoid-looking NON-person (statue, coat on a chair) at this
    head bearing, so it stops being reported as somebody. 'Unknown outranks
    known' (Zeke 09-02) means study it ONCE, then know it."""
    cfg = _config()
    shapes = cfg.setdefault("static_shapes", [])
    shapes.append({"box": [int(v) for v in box], "label": str(label),
                   "head_pan": (head or {}).get("pan_deg"), "head_tilt": (head or {}).get("tilt_deg"),
                   "ts": time.time(), "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S")})
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return {"ok": True, "static_shapes": len(shapes), "label": label}


def _matches_static(box, head) -> str | None:
    """Label of a remembered static shape at (about) this head bearing, else None."""
    hp = (head or {}).get("pan_deg"); ht = (head or {}).get("tilt_deg")
    for s in static_shapes():
        sp, stt = s.get("head_pan"), s.get("head_tilt")
        if hp is not None and sp is not None and (abs(float(hp) - float(sp)) > 4.0 or abs(float(ht or 0) - float(stt or 0)) > 4.0):
            continue          # head is pointed somewhere else; pixel boxes don't transfer
        if _iou(box, s["box"]) >= 0.45:
            return str(s.get("label") or "static shape")
    return None


def analyze(frame: Any, *, person_hint: str | None = None, conf: float = 0.25,
            height_m: float | None = None, faces: list[dict] | None = None,
            tracks: list[list[float]] | None = None, head: dict[str, Any] | None = None,
            imgsz: int = 1280) -> dict[str, Any]:
    """Run pose on a BGR frame. Returns {ok, persons:[...], ms, sentence}.

    imgsz 1280 (not the 640 default): measured 2026-09-02 on the same frame,
    n@640 saw only a statue; n@1280 found Zeke lying on the bed at 0.76 for
    +6 ms. Corroboration: a pose box counts as a PERSON when a recognised face
    sits in its upper part or a body track overlaps it; otherwise it is
    'unverified' — a humanoid statue on the dresser scored 0.75 at both sizes.
    """
    m = _load()
    if m is None:
        return {"ok": False, "error": _LOAD_ERROR or "model not loaded", "persons": []}
    t0 = time.time()
    # Detect LOW (0.15) and filter AFTER corroboration: a dim, occluded man
    # lying on the bed scored 0.76 / 0.35 / nothing across three reads while a
    # face sat inside his box every time. A weak body + a face = a person;
    # a weak body alone is dropped below `conf`.
    det_conf = min(conf, 0.15)
    try:
        res = m.predict(frame, device=0, verbose=False, conf=det_conf, imgsz=imgsz)[0]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200], "persons": []}
    ms = (time.time() - t0) * 1000.0
    _STATS["calls"] += 1
    _STATS["last_ms"] = round(ms, 1)
    _STATS["avg_ms"] = round(ms if _STATS["avg_ms"] is None else 0.9 * _STATS["avg_ms"] + 0.1 * ms, 1)
    fh, fw = frame.shape[:2]
    h_true, assumed = (height_m, False) if height_m else known_height(person_hint)
    persons: list[dict[str, Any]] = []
    n = 0 if res.keypoints is None or res.boxes is None else len(res.boxes)
    for i in range(n):
        try:
            xy = res.keypoints.xy[i].cpu().numpy()
            kc = res.keypoints.conf[i].cpu().numpy() if res.keypoints.conf is not None else [1.0] * 17
            box = [int(v) for v in res.boxes.xyxy[i].cpu().numpy().tolist()]
            bconf = float(res.boxes.conf[i])
        except Exception:
            continue
        kp: dict[str, tuple[float, float]] = {}
        joints: dict[str, Any] = {}
        for j, name in enumerate(KP_NAMES):
            c = float(kc[j])
            joints[name] = {"x": round(float(xy[j][0]), 1), "y": round(float(xy[j][1]), 1), "c": round(c, 2)}
            if c >= KP_CONF:
                kp[name] = (float(xy[j][0]), float(xy[j][1]))
        posture, ev = _posture(kp)
        hands = _hands(kp)
        dist = _distance(kp, fw, h_true)
        visible = sorted(kp.keys(), key=KP_NAMES.index)
        lowest = max((kp[k][1] for k in kp), default=None)
        extent = ("full body" if any(k in kp for k in ("l_ank", "r_ank"))
                  else "to the knees" if any(k in kp for k in ("l_knee", "r_knee"))
                  else "to the hips" if any(k in kp for k in ("l_hip", "r_hip"))
                  else "torso up" if any(k in kp for k in ("l_sho", "r_sho"))
                  else "head only")
        cut_by_frame = bool(box[3] >= fh - 6 or (lowest is not None and lowest > fh - 12))
        # corroboration — what else in the eyes agrees this is a person?
        corroborated: list[str] = []
        face_id = None
        for f in faces or []:
            fb = f.get("bbox") or f.get("box")
            if not fb:
                continue
            fx = (fb[0] + fb[2]) / 2.0; fy = (fb[1] + fb[3]) / 2.0
            if box[0] <= fx <= box[2] and box[1] <= fy <= box[1] + 0.6 * (box[3] - box[1]):
                corroborated.append("face")
                pid = str(f.get("person_id") or "unknown")
                face_id = pid
                break
        for tb in tracks or []:
            try:
                tx1, ty1, tw, th = tb[:4]
                if _iou(box, [tx1, ty1, tx1 + tw, ty1 + th]) >= 0.3:
                    corroborated.append("track")
                    break
            except Exception:
                continue
        static_label = _matches_static(box, head)
        # A body TRACK is the same kind of detector fooled the same way (the
        # statue had a track). Only a FACE (InsightFace, a different model)
        # verifies; track-only is "likely".
        verified = ("face" in corroborated) and not static_label
        likely = ("track" in corroborated) and not verified and not static_label
        if bconf < conf and not verified:
            continue          # weak and nothing independent agrees — not a person
        persons.append({
            "index": i, "box": box, "box_conf": round(bconf, 2),
            "corroborated_by": corroborated, "face_id": face_id,
            "static_shape": static_label,
            "verified_person": verified, "likely_person": likely,
            "visible_joints": visible, "n_visible": len(visible),
            "extent": extent, "cut_off_at_frame_bottom": cut_by_frame,
            "posture": posture, "posture_evidence": ev, "hands": hands,
            "distance": {**dist, "height_used_m": h_true, "assumed_height": assumed},
            "joints": joints,
        })
    persons.sort(key=lambda p: -(p["box"][2] - p["box"][0]) * (p["box"][3] - p["box"][1]))
    return {"ok": True, "persons": persons, "count": len(persons), "ms": round(ms, 1),
            "frame": [fw, fh], "sentence": describe(persons, person_hint)}


def describe(persons: list[dict[str, Any]], person_hint: str | None = None) -> str:
    if not persons:
        return "no body in view"
    real = [p for p in persons if not p.get("static_shape")]
    if not real:
        return f"no body in view ({len(persons)} known non-person shape(s) ignored)"
    parts = []
    for p in real:
        who = (p.get("face_id") if p.get("face_id") and p["face_id"] != "unknown"
               else person_hint if (person_hint and p["index"] == 0) else "someone")
        if not p.get("verified_person"):
            who = (f"probably a person (body track agrees, no face; conf {p['box_conf']})"
                   if p.get("likely_person")
                   else f"something humanoid (nothing else agrees; conf {p['box_conf']})")
        d = p["distance"]
        dist = (f"about {d['m']:.1f} m away" + (" (assuming a 1.75 m person)" if d.get("assumed_height") else "")
                if d.get("m") else "distance unknown")
        extra = (", " + ", ".join(p["hands"])) if p["hands"] else ""
        cut = " (cut off by the bottom of the frame)" if p["cut_off_at_frame_bottom"] else ""
        parts.append(f"{who}: {p['posture']}, visible {p['extent']}{cut}, {dist}{extra}")
    return "; ".join(parts)


def draw(frame: Any, result: dict[str, Any]) -> Any:
    """Skeleton overlay for a look-at-it check (returns a copy)."""
    import cv2  # type: ignore
    out = frame.copy()
    pairs = [("l_sho", "r_sho"), ("l_sho", "l_elb"), ("l_elb", "l_wri"), ("r_sho", "r_elb"),
             ("r_elb", "r_wri"), ("l_sho", "l_hip"), ("r_sho", "r_hip"), ("l_hip", "r_hip"),
             ("l_hip", "l_knee"), ("l_knee", "l_ank"), ("r_hip", "r_knee"), ("r_knee", "r_ank"),
             ("nose", "l_eye"), ("nose", "r_eye"), ("l_eye", "l_ear"), ("r_eye", "r_ear")]
    for p in result.get("persons") or []:
        j = p["joints"]
        for a, b in pairs:
            if j[a]["c"] >= KP_CONF and j[b]["c"] >= KP_CONF:
                cv2.line(out, (int(j[a]["x"]), int(j[a]["y"])), (int(j[b]["x"]), int(j[b]["y"])), (0, 220, 255), 2)
        for name, v in j.items():
            if v["c"] >= KP_CONF:
                cv2.circle(out, (int(v["x"]), int(v["y"])), 4, (0, 255, 0), -1)
        x1, y1, x2, y2 = p["box"]
        cv2.rectangle(out, (x1, y1), (x2, y2), (255, 160, 0), 1)
        d = p["distance"].get("m")
        cv2.putText(out, f"{p['posture']} {d if d else '?'}m", (x1, max(14, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return out


# =============================================================================
# LIVE LOOP + ACTIVITY OVER TIME (task 2 of Zeke's "do all three", 2026-09-02)
# A ~2 Hz thread keeps g["_human_pose_live"] fresh (faces/tracks/head attached)
# and a short per-person history so "what is he doing" has a memory:
# walking / still / sat down / stood up / reaching. person_track, room_map,
# the HUD and scene_memory read this instead of running the model themselves.
# =============================================================================
_LAST_HEAD: dict[str, Any] | None = None
HISTORY_S = 12.0
LOOP_ACTIVE_S = 0.5      # someone in view
LOOP_IDLE_S = 2.0        # nobody in view


def current_head() -> dict[str, Any] | None:
    return _LAST_HEAD


def is_static_box(xyxy) -> bool:
    """For person_track: does this detection sit on a remembered non-person
    shape at the current head bearing? (The statue had a body track.)"""
    try:
        return _matches_static([float(v) for v in xyxy[:4]], _LAST_HEAD) is not None
    except Exception:
        return False


def _torso_px(p: dict[str, Any]) -> float:
    j = p.get("joints") or {}
    try:
        sho = [(j[k]["x"], j[k]["y"]) for k in ("l_sho", "r_sho") if j[k]["c"] >= KP_CONF]
        hip = [(j[k]["x"], j[k]["y"]) for k in ("l_hip", "r_hip") if j[k]["c"] >= KP_CONF]
        if sho and hip:
            s = _mid(*sho) if len(sho) == 2 else sho[0]
            h = _mid(*hip) if len(hip) == 2 else hip[0]
            return max(20.0, _dist(s, h))
    except Exception:
        pass
    return 80.0


def _centre(p: dict[str, Any]) -> tuple[float, float]:
    j = p.get("joints") or {}
    pts = [(j[k]["x"], j[k]["y"]) for k in ("l_hip", "r_hip", "l_sho", "r_sho") if j[k]["c"] >= KP_CONF]
    if pts:
        return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))
    x1, y1, x2, y2 = p["box"]
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _reach_px(p: dict[str, Any]) -> float:
    j = p.get("joints") or {}
    best = 0.0
    for s in ("l", "r"):
        try:
            if j[f"{s}_wri"]["c"] >= KP_CONF and j[f"{s}_sho"]["c"] >= KP_CONF:
                best = max(best, _dist((j[f"{s}_wri"]["x"], j[f"{s}_wri"]["y"]),
                                       (j[f"{s}_sho"]["x"], j[f"{s}_sho"]["y"])))
        except Exception:
            continue
    return best


class PoseLoop:
    def __init__(self, g: dict[str, Any]):
        self._g = g
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.stats = {"ticks": 0, "with_person": 0, "errors": 0, "last_ms": None}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="human_pose_loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=4.0)

    def alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _run(self) -> None:
        global _LAST_HEAD
        while not self._stop.is_set():
            period = LOOP_IDLE_S
            try:
                from brain import frame_store
                g = self._g
                head = (g.get("_attention_state_obj") or {}).get("bearing")
                _LAST_HEAD = dict(head) if isinstance(head, dict) else None
                faces = list(g.get("_face_results") or [])
                tracks: list = []
                try:
                    from brain import person_track
                    tracks, _sz = person_track.track_boxes()
                except Exception:
                    tracks = []
                if faces or tracks:
                    res = frame_store.get_buffered_frame(max_age_sec=2.0)
                    if res.frame is not None:
                        known = [str(f.get("person_id")) for f in faces
                                 if str(f.get("person_id") or "unknown") not in ("unknown", "")]
                        hint = known[0] if len(known) == 1 else None
                        out = analyze(res.frame, person_hint=hint, faces=faces, tracks=tracks,
                                      head=_LAST_HEAD)
                        self.stats["ticks"] += 1
                        self.stats["last_ms"] = out.get("ms")
                        if out.get("ok"):
                            out["captured_ts"] = res.capture_ts
                            self._record(out)
                            g["_human_pose_live"] = out
                            if any(p.get("verified_person") or p.get("likely_person") for p in out["persons"]):
                                self.stats["with_person"] += 1
                                period = LOOP_ACTIVE_S
                else:
                    self.stats["ticks"] += 1
            except Exception:  # noqa: BLE001
                self.stats["errors"] += 1
            self._stop.wait(period)

    def _key(self, p: dict[str, Any]) -> str:
        fid = p.get("face_id")
        if fid and fid != "unknown":
            return str(fid)
        return "primary" if p.get("verified_person") or p.get("likely_person") else f"shape{p['index']}"

    def _record(self, out: dict[str, Any]) -> None:
        now = time.time()
        for p in out.get("persons") or []:
            if p.get("static_shape"):
                continue
            k = self._key(p)
            h = self.history.setdefault(k, [])
            h.append({"ts": now, "c": _centre(p), "torso": _torso_px(p),
                      "posture": p.get("posture"), "reach": _reach_px(p),
                      "extent": p.get("extent"), "dist": (p.get("distance") or {}).get("m")})
            cutoff = now - HISTORY_S
            self.history[k] = [r for r in h if r["ts"] >= cutoff]
        for k in list(self.history):
            if self.history[k] and self.history[k][-1]["ts"] < now - HISTORY_S:
                del self.history[k]
        for p in out.get("persons") or []:
            p["activity"] = None if p.get("static_shape") else self.activity(self._key(p))

    def activity(self, key: str) -> str | None:
        h = self.history.get(key) or []
        if len(h) < 2:
            return None
        now = h[-1]["ts"]
        recent = [r for r in h if r["ts"] >= now - 3.0]
        if len(recent) < 2:
            return None
        a, b = recent[0], recent[-1]
        dt = max(0.25, b["ts"] - a["ts"])
        torso = max(20.0, (a["torso"] + b["torso"]) / 2.0)
        speed = _dist(a["c"], b["c"]) / dt / torso      # torso-lengths per second
        reach_rate = (b["reach"] - a["reach"]) / dt / torso
        postures = [r["posture"] for r in h if r.get("posture")]
        words: list[str] = []
        if speed > 0.6:
            legs = b.get("extent") in ("full body", "to the knees")
            words.append("walking" if legs and str(b.get("posture")).startswith("standing") else "moving")
        elif speed < 0.15:
            words.append("still")
        if reach_rate > 0.8:
            words.append("reaching")
        if len(postures) >= 4:
            first = postures[0]
            last = postures[-1]
            if first.startswith("standing") and last.startswith("sitting"):
                words.append("sat down")
            elif first.startswith("sitting") and last.startswith("standing"):
                words.append("stood up")
            elif first != "lying down" and last == "lying down":
                words.append("lay down")
        return ", ".join(words) if words else None


def start_loop(g: dict[str, Any]) -> dict[str, Any]:
    lp = g.get("_human_pose_loop")
    if not isinstance(lp, PoseLoop):
        lp = PoseLoop(g)
        g["_human_pose_loop"] = lp
    lp.start()
    return {"alive": lp.alive(), **lp.stats}


def loop_status(g: dict[str, Any]) -> dict[str, Any]:
    lp = g.get("_human_pose_loop")
    if not isinstance(lp, PoseLoop):
        return {"alive": False, "note": "not started"}
    return {"alive": lp.alive(), **lp.stats, "history_keys": list(lp.history.keys())}


def live(g: dict[str, Any], max_age_s: float = 3.0) -> dict[str, Any] | None:
    out = g.get("_human_pose_live")
    if not isinstance(out, dict):
        return None
    if time.time() - float(out.get("captured_ts") or 0.0) > max_age_s:
        return None
    return out


def live_person(g: dict[str, Any], face_id: str, max_age_s: float = 3.0) -> dict[str, Any] | None:
    out = live(g, max_age_s)
    if not out:
        return None
    for p in out.get("persons") or []:
        if str(p.get("face_id") or "").lower() == str(face_id).lower():
            return p
    return None
