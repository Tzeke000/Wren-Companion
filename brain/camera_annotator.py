"""
Camera annotator — overlays face bounding boxes, landmarks, head pose axes,
attention state, and labels on the live frame.

Reads g["_face_results"] (populated by background_ticks when InsightFace
is active) and g["_attention_state"] (populated by heartbeat eye tracker).

If face_results is empty the function is a no-op so plain video keeps
flowing through the pipeline.
"""
from __future__ import annotations

import math
from typing import Any


_BOX_KNOWN = (0, 220, 0)
_BOX_UNKNOWN = (0, 220, 220)
_LANDMARK_FAINT = (0, 180, 0)
_LANDMARK_KEY = (0, 255, 80)

_KEY_LANDMARK_INDICES = {
    # Approximate key landmark indices from buffalo_l 106-pt set.
    # Drawn at radius 2 instead of 1.
    0, 1, 2, 3, 4,        # nose
    16, 17,                # eyebrow ridges
    33, 35, 40, 41,        # left eye
    87, 89, 94, 95,        # right eye
    52, 61, 67, 76, 77,    # mouth
}

_ATTENTION_COLORS = {
    "focused": (0, 220, 0),
    "distracted": (0, 220, 220),
    "away": (0, 165, 255),
    "absent": (0, 0, 220),
}

# What the eye tracker's four states actually MEAN, in words a reader of the
# frame can't misread (2026-09-02). Raw "AWAY" covered two different truths —
# "face in frame, eyes off the screen >10s" and "face lost <30s ago" — and
# read as "he left" while Zeke sat three feet from the lens looking down.
_ATTENTION_LABELS = {
    "focused": "EYES ON SCREEN",
    "distracted": "GLANCED AWAY",
    "away": "LOOKING AWAY",        # face still in frame, eyes off screen >10s
    "away_noface": "FACE LOST <30s",
    "absent": "NOBODY IN VIEW",    # no face for >30s
}

# ── Hand overlay (2026-07-13, Zeke: "I can see what you see" — help me tune hands) ──
# MediaPipe 21-landmark hand skeleton connections (bone pairs). Drawn from
# g["_hand_results"] (brain/iris_hands.py) so Zeke sees exactly what the gesture
# recognizer sees — the same debug affordance as the face boxes.
_HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),            # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),            # index
    (5, 9), (9, 10), (10, 11), (11, 12),       # middle
    (9, 13), (13, 14), (14, 15), (15, 16),     # ring
    (13, 17), (17, 18), (18, 19), (19, 20),    # pinky
    (0, 17),                                    # palm base
)
_HAND_BONE = (255, 180, 0)      # cyan-ish (BGR) skeleton
_HAND_JOINT = (0, 230, 255)     # yellow joints
_HAND_LABEL = (0, 230, 255)

# ── Box/landmark smoothing (2026-07-08; RETIRED 2026-08-27) ──────────────────
# ORIGINAL (5fps-detection era, insight_every_n=6): drawing raw _face_results
# froze the box for 6 frames then snapped it, so alpha 0.35 turned freeze-and-
# jump into a glide. SINCE the 08-27 all-30fps rebuild the face worker runs
# ~22-30fps and easing only ADDED visible trail.
# ZEKE'S DIRECTIVE (08-27 evening): the orb mini-cam is a DIAGNOSTIC surface —
# "what I really want the little mini cam in your app to be is exactly what
# you're seeing... if it's not true and accurate to what you're seeing, I
# can't help you diagnose." Alpha 1.0 = raw detections, zero cosmetics. Do not
# re-add easing here; prettiness costs him diagnostic truth.
_SMOOTH_ALPHA = 1.0
# Snap (no easing) when the target center jumps more than this fraction of the
# frame diagonal — a real teleport (camera cut, person swap), not motion.
_SNAP_FRAC = 0.25
_smooth_state: dict[str, dict[str, Any]] = {}


def _smooth_face(face: dict[str, Any], frame_shape: Any) -> dict[str, Any]:
    """Return a copy of `face` with bbox+landmarks eased toward the detection.

    Keyed by person_id (single-face household; unknowns share one slot). Any
    error falls back to the raw face so annotation never breaks.
    """
    try:
        import numpy as np  # type: ignore

        pid = str(face.get("person_id") or "unknown")
        target_bbox = np.asarray(face.get("bbox") or [0, 0, 0, 0], dtype=np.float64)
        lm_raw = face.get("landmarks")
        target_lm = None if lm_raw is None else np.asarray(lm_raw, dtype=np.float64)

        h = float(frame_shape[0]) if frame_shape is not None else 480.0
        w = float(frame_shape[1]) if frame_shape is not None else 640.0
        diag = (w * w + h * h) ** 0.5

        st = _smooth_state.get(pid)
        snap = st is None
        if st is not None:
            prev_cx = (st["bbox"][0] + st["bbox"][2]) / 2.0
            prev_cy = (st["bbox"][1] + st["bbox"][3]) / 2.0
            cur_cx = (target_bbox[0] + target_bbox[2]) / 2.0
            cur_cy = (target_bbox[1] + target_bbox[3]) / 2.0
            jump = ((cur_cx - prev_cx) ** 2 + (cur_cy - prev_cy) ** 2) ** 0.5
            if jump > diag * _SNAP_FRAC:
                snap = True
            # Landmark count changed (different detector path) → don't lerp mismatched arrays.
            if (st.get("lm") is None) != (target_lm is None):
                snap = True
            elif target_lm is not None and st["lm"].shape != target_lm.shape:
                snap = True

        if snap:
            _smooth_state[pid] = {"bbox": target_bbox.copy(),
                                  "lm": None if target_lm is None else target_lm.copy()}
        else:
            st["bbox"] += (target_bbox - st["bbox"]) * _SMOOTH_ALPHA
            if target_lm is not None:
                st["lm"] += (target_lm - st["lm"]) * _SMOOTH_ALPHA

        st = _smooth_state[pid]
        smoothed = dict(face)
        smoothed["bbox"] = st["bbox"].tolist()
        if st.get("lm") is not None:
            smoothed["landmarks"] = st["lm"]
        return smoothed
    except Exception:
        return face


def _prune_smooth_state(seen_pids: set[str]) -> None:
    """Drop smoothing slots for faces no longer in the results."""
    for k in list(_smooth_state.keys()):
        if k not in seen_pids:
            _smooth_state.pop(k, None)


# ── CLEAN-BUFFER GATE (2026-08-21) ───────────────────────────────────────────
# The capture loops used to push ANNOTATED frames into frame_store — and the
# object tracker locked onto the DRAWN hand dots / face mesh instead of the
# real object (chased the graphics into the ceiling, live, during Pyraminx
# play). Vision consumers (TrackerVit, OWL-ViT, sentry) must see reality only.
# annotate_frame (the capture-path entry) now returns the frame UNTOUCHED;
# display surfaces call annotate_display on a COPY at serve time
# (operator_server.camera_live_frame). Flip only if you also re-route every
# vision consumer off the shared buffer — you almost certainly should not.
CAPTURE_PATH_CLEAN = True


def annotate_frame(frame: Any, face_results: list[dict[str, Any]] | None, g: dict[str, Any]) -> Any:
    """Capture-path entry. Since 2026-08-21 this is a PASS-THROUGH (see
    CAPTURE_PATH_CLEAN above) so the shared frame buffer stays clean for the
    vision stack. Display overlays live in annotate_display."""
    if CAPTURE_PATH_CLEAN:
        return frame
    return annotate_display(frame, face_results, g)


_OVERLAY_FLAG = None        # (path, mtime, value) cache — see _overlay_off
_OVERLAY_CHECKED = 0.0


def _overlay_off() -> bool:
    """True when `state/camera_overlay_off.json` says {"off": true}.

    Zeke 2026-08-28 asked for the tracking overlay off while using the camera
    in Discord. Both frame endpoints annotate at serve time, so the boxes are
    baked into the pixels before anything downstream sees them — there is no
    way to strip them later. A flag is the only live control.

    Deliberately a FILE, matching `voice_deliberately_off.json`: any process
    can toggle it, it survives restarts, and it is greppable when a future me
    wonders why the overlay vanished. Re-read at most once a second — this runs
    per served frame.
    """
    global _OVERLAY_FLAG, _OVERLAY_CHECKED
    import time as _t
    now = _t.time()
    if now - _OVERLAY_CHECKED < 1.0:
        return bool(_OVERLAY_FLAG)
    _OVERLAY_CHECKED = now
    try:
        import json as _j
        from pathlib import Path as _P
        p = _P(__file__).resolve().parent.parent / "state" / "camera_overlay_off.json"
        _OVERLAY_FLAG = bool(_j.loads(p.read_text(encoding="utf-8")).get("off"))
    except Exception:
        _OVERLAY_FLAG = False       # missing//unreadable flag = overlay ON
    return bool(_OVERLAY_FLAG)


def annotate_display(frame: Any, face_results: list[dict[str, Any]] | None, g: dict[str, Any]) -> Any:
    """Draw face overlays on `frame` and return the modified frame.

    Falls back to the original frame on any error so the camera pipeline
    never breaks.
    """
    if frame is None:
        return frame
    # ── OVERLAY KILL-SWITCH (Zeke 2026-08-28: "can you take the hud off").
    # Inlined rather than calling _overlay_off(): brain_hot_swap can REPLACE an
    # existing function but cannot ADD one, so a helper would need a full stack
    # restart to become reachable. Everything the switch needs lives here.
    try:
        import time as _t
        _now = _t.time()
        if _now - getattr(annotate_display, "_flag_ts", 0.0) >= 1.0:
            annotate_display._flag_ts = _now
            try:
                import json as _j
                from pathlib import Path as _P
                _p = (_P(__file__).resolve().parent.parent / "state"
                      / "camera_overlay_off.json")
                annotate_display._flag = bool(
                    _j.loads(_p.read_text(encoding="utf-8")).get("off"))
            except Exception:
                annotate_display._flag = False   # no flag = overlay ON
        if getattr(annotate_display, "_flag", False):
            return frame
    except Exception:
        pass                                     # never break the camera path
    if not face_results:
        _smooth_state.clear()  # returning faces snap fresh, no stale glide
        # No faces — but hands may still be up (person just off the face-detect
        # cadence, or holding hands to camera). Draw hands + attention.
        base = _draw_attention_only(frame, g)
        return _draw_hands(base, g)

    try:
        import cv2  # type: ignore
    except Exception:
        return frame

    out = frame.copy()
    h = int(out.shape[0]) if hasattr(out, "shape") else 0

    profiles = g.get("_profiles") or {}

    _prune_smooth_state({str(f.get("person_id") or "unknown") for f in face_results})

    for face in face_results:
        try:
            face = _smooth_face(face, out.shape if hasattr(out, "shape") else None)
            bbox = face.get("bbox") or [0, 0, 0, 0]
            x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            pid = str(face.get("person_id") or "unknown")
            conf = float(face.get("confidence") or 0.0)

            color = _BOX_KNOWN if pid != "unknown" else _BOX_UNKNOWN
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)

            display = pid.upper()
            if isinstance(profiles, dict):
                p = profiles.get(pid)
                if isinstance(p, dict):
                    name = p.get("name") or p.get("display_name")
                    if isinstance(name, str) and name:
                        display = name
            label = f"{display} {conf*100:.0f}%"
            cv2.putText(
                out, label, (x1, max(15, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2,
            )

            # Age/gender used to be drawn here. Removed 2026-09-02: they are
            # InsightFace GUESSES (the standing rule is never to treat them as
            # facts — they are Zeke's to state), and a number on the HUD reads
            # as a fact to whoever looks at the frame, me included.

            lm = face.get("landmarks")
            if lm is not None:
                for i, pt in enumerate(lm):
                    try:
                        lx, ly = int(pt[0]), int(pt[1])
                        if i in _KEY_LANDMARK_INDICES:
                            cv2.circle(out, (lx, ly), 2, _LANDMARK_KEY, -1)
                        else:
                            cv2.circle(out, (lx, ly), 1, _LANDMARK_FAINT, -1)
                    except Exception:
                        continue

                # Head pose arrows from nose tip (landmark 1 — closer to actual tip).
                try:
                    pose = face.get("pose") or [0.0, 0.0, 0.0]
                    pitch = float(pose[0])
                    yaw = float(pose[1])
                    roll = float(pose[2])
                    nose_idx = 1 if len(lm) > 1 else 0
                    nx, ny = int(lm[nose_idx][0]), int(lm[nose_idx][1])
                    L = 55
                    yaw_r = math.radians(yaw)
                    pitch_r = math.radians(pitch)
                    roll_r = math.radians(roll)
                    # Up axis (green)
                    cv2.arrowedLine(
                        out, (nx, ny),
                        (nx + int(L * math.sin(yaw_r)),
                         ny - int(L * math.cos(pitch_r))),
                        (0, 255, 0), 2, tipLength=0.3,
                    )
                    # Right axis (red)
                    cv2.arrowedLine(
                        out, (nx, ny),
                        (nx + int(L * math.cos(yaw_r)),
                         ny + int(L * math.sin(roll_r))),
                        (0, 0, 255), 2, tipLength=0.3,
                    )
                    # Forward axis (blue)
                    cv2.arrowedLine(
                        out, (nx, ny),
                        (nx - int(L * math.sin(roll_r)),
                         ny - int(L * math.sin(yaw_r))),
                        (255, 0, 0), 2, tipLength=0.3,
                    )
                except Exception:
                    pass
        except Exception as e:
            # Skip this face but keep going — robustness over completeness.
            print(f"[camera_annotator] face draw error: {e!r}")
            continue

    out = _draw_hands(out, g)
    out = _draw_pose(out, g)
    return _overlay_attention(out, h, g)



# 2026-08-20 (Zeke: "hand tracking is blinking in and out"): detection at room
# distance drops out on many cycles, and the overlay used to strobe with it.
# Keep the last non-empty result briefly so the skeleton persists through
# single-cycle dropouts. Short TTL on purpose — a stale skeleton lying about a
# hand that left the frame is worse than a blink.
_HANDS_HOLD_S = 0.8
_last_hands: dict[str, Any] = {"ts": 0.0, "hr": None}


_POSE_PAIRS = (("l_sho", "r_sho"), ("l_sho", "l_elb"), ("l_elb", "l_wri"), ("r_sho", "r_elb"),
               ("r_elb", "r_wri"), ("l_sho", "l_hip"), ("r_sho", "r_hip"), ("l_hip", "r_hip"),
               ("l_hip", "l_knee"), ("l_knee", "l_ank"), ("r_hip", "r_knee"), ("r_knee", "r_ank"))

# ── SKELETON GLIDE (2026-09-03, Zeke: "because the edge of limbs move faster it
# looks choppy ... 30 FPS please") ───────────────────────────────────────────
# The pose model produces ~6-7 poses/sec; this overlay is drawn at the camera's
# ~29 fps. Without smoothing the joints TELEPORT on every 4th or 5th drawn
# frame, which is exactly the choppiness he described at the fast-moving ends
# of limbs. So the drawn skeleton chases the newest pose instead of snapping to
# it: cheap float maths per joint per frame, NO extra inference, no extra GPU.
# It does not make the model faster — it makes the DRAWING continuous, which is
# the thing his eyes were complaining about.
#
# ★ ALL OF THIS LIVES INSIDE _draw_pose ON PURPOSE. brain_hot_swap can REPLACE
#   an existing function but cannot ADD one (see the kill-switch note above), so
#   a helper would need a full stack restart to become reachable.
_POSE_TAU_S = 0.08       # glide time-constant. Larger = smoother + laggier.
_POSE_SNAP_S = 0.5       # older than this ⇒ snap, don't slide across the room
_pose_smooth: dict[str, dict[str, Any]] = {}


def _draw_pose(frame: Any, g: dict[str, Any]) -> Any:
    """Skeleton + posture/distance/activity words for VERIFIED people from
    brain/body_pose's live loop (2026-09-02, task 2). Known non-person shapes
    are not drawn. Never raises — camera pipeline safety."""
    try:
        import time as _time
        import cv2  # type: ignore
        live = g.get("_human_pose_live")
        if not isinstance(live, dict) or _time.time() - float(live.get("captured_ts") or 0.0) > 1.5:
            return frame
        out = frame
        for p in live.get("persons") or []:
            if p.get("static_shape") or not (p.get("verified_person") or p.get("likely_person")):
                continue
            j = p.get("joints") or {}
            col = (0, 220, 255) if p.get("verified_person") else (0, 160, 200)
            # ── glide the DRAWN positions toward this pose (see note above) ──
            now = _time.time()
            key = str(p.get("face_id") or "") or "i%s" % p.get("index")
            st = _pose_smooth.get(key)
            if st is None or now - float(st.get("ts") or 0.0) > _POSE_SNAP_S:
                st = {"j": {}, "box": None, "ts": now}
                _pose_smooth[key] = st
            step = (now - float(st["ts"])) / _POSE_TAU_S if _POSE_TAU_S > 0 else 1.0
            alpha = 1.0 if step >= 1.0 else (0.0 if step < 0.0 else step)
            st["ts"] = now
            sj = st["j"]
            for _n, _v in j.items():
                _t = (float(_v["x"]), float(_v["y"]))
                _c = sj.get(_n)
                if _c is None or float(_v.get("c") or 0.0) < 0.5:
                    sj[_n] = [_t[0], _t[1]]          # new/low-confidence: snap
                else:
                    _c[0] += (_t[0] - _c[0]) * alpha
                    _c[1] += (_t[1] - _c[1]) * alpha
            for _n in [k for k in sj if k not in j]:
                sj.pop(_n, None)
            _tb = [float(v) for v in p["box"]]
            if st["box"] is None:
                st["box"] = _tb
            else:
                for _i in range(4):
                    st["box"][_i] += (_tb[_i] - st["box"][_i]) * alpha
            for a, b in _POSE_PAIRS:
                ja, jb = j.get(a), j.get(b)
                sa, sb = sj.get(a), sj.get(b)
                if ja and jb and sa and sb and ja["c"] >= 0.5 and jb["c"] >= 0.5:
                    cv2.line(out, (int(sa[0]), int(sa[1])), (int(sb[0]), int(sb[1])), col, 2)
            for _n, v in j.items():
                _s = sj.get(_n)
                if _s is not None and v["c"] >= 0.5:
                    cv2.circle(out, (int(_s[0]), int(_s[1])), 3, (0, 255, 0), -1)
            x1, y1, x2, y2 = (int(v) for v in st["box"])
            d = (p.get("distance") or {}).get("m")
            words = p.get("posture") or ""
            if p.get("activity"):
                words += f" · {p['activity']}"
            if d:
                words += f" · {d:.1f}m"
            cv2.putText(out, words, (x1, min(frame.shape[0] - 6, y2 + 16)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
        return out
    except Exception:
        return frame


def _draw_hands(frame: Any, g: dict[str, Any]) -> Any:
    """Overlay the hand skeleton + gesture label from g['_hand_results'].
    No-op when there are no hands. Never raises — camera pipeline safety."""
    import time as _time
    hr = g.get("_hand_results")
    hands = hr.get("hands") if isinstance(hr, dict) else None
    now = _time.time()
    if hands:
        _last_hands["ts"] = now
        _last_hands["hr"] = hr
    else:
        cached = _last_hands.get("hr")
        if cached and (now - float(_last_hands.get("ts") or 0.0)) <= _HANDS_HOLD_S:
            hr = cached
            hands = hr.get("hands")
    if not hr or not hands:
        return frame
    try:
        import cv2  # type: ignore
    except Exception:
        return frame
    try:
        for hand in hands:
            lms = hand.get("landmarks_px")
            if not lms or len(lms) < 21:
                continue
            # Bones first, joints on top.
            for a, b in _HAND_CONNECTIONS:
                try:
                    cv2.line(frame, (int(lms[a][0]), int(lms[a][1])),
                             (int(lms[b][0]), int(lms[b][1])), _HAND_BONE, 2)
                except Exception:
                    continue
            for pt in lms:
                try:
                    cv2.circle(frame, (int(pt[0]), int(pt[1])), 3, _HAND_JOINT, -1)
                except Exception:
                    continue
            # Label: handedness + gesture (+ WAVE flag) near the wrist.
            gesture = str(hand.get("gesture") or "None")
            handed = str(hand.get("handedness") or "?")
            score = float(hand.get("score") or 0.0)
            tag = f"{handed}: {gesture}"
            if gesture not in ("None", ""):
                tag += f" {score*100:.0f}%"
            if hr.get("wave"):
                tag += "  WAVE"
            wx, wy = lms[0][0], lms[0][1]
            cv2.putText(frame, tag, (int(wx) - 10, int(wy) + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, _HAND_LABEL, 2)
    except Exception as e:
        print(f"[camera_annotator] hand draw error: {e!r}")
    return frame


def _draw_attention_only(frame: Any, g: dict[str, Any]) -> Any:
    """When there are no faces, still surface attention/eye-tracking state."""
    try:
        import cv2  # type: ignore
    except Exception:
        return frame
    try:
        h = int(frame.shape[0]) if hasattr(frame, "shape") else 0
        out = frame.copy()
        return _overlay_attention(out, h, g)
    except Exception:
        return frame


def _overlay_attention(frame: Any, h: int, g: dict[str, Any]) -> Any:
    try:
        import cv2  # type: ignore
        raw = str(g.get("_attention_state") or "").strip().lower()
        if not raw:
            # Used to default to "FOCUSED" so the readout was always visible —
            # which is a claim the tracker never made. Say there's no read.
            label, color = "GAZE: no read yet", (200, 200, 200)
        else:
            key = raw
            if raw == "away" and not (g.get("_face_results") or []):
                key = "away_noface"
            label = _ATTENTION_LABELS.get(key, raw.upper())
            color = _ATTENTION_COLORS.get(raw, (200, 200, 200))
        y = max(15, h - 10)
        cv2.putText(
            frame, label,
            (10, y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2,
        )
    except Exception:
        pass
    return frame
