"""
Phase 7 — lightweight temporal continuity for faces / primary salient subject.

Keeps a small module-level memory of the last trusted primary face (geometry + identity +
salience label) and scores each tick for spatial match, size stability, and time decay.
Does **not** replace LBPH recognition output; exposes ``ContinuityResult`` for stable
``identity_state`` and ``continuity_confidence`` (fallback hierarchy / prompts / memory hooks).
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

from config.ava_tuning import CONTINUITY_CONFIG

from .perception_types import ContinuityResult
from .perception_utils import lbph_distance_to_identity_confidence

cc = CONTINUITY_CONFIG
# Backward-compatible module aliases (see :mod:`config.ava_tuning` ``CONTINUITY_CONFIG``)
MAX_FRAME_GAP = cc.max_frame_gap
MAX_SECONDS_GAP = cc.max_seconds_gap
SPATIAL_DIST_REF = cc.spatial_dist_ref
LIKELY_SAME_SPATIAL_MIN = cc.likely_same_spatial_min
SIZE_RATIO_REF = cc.size_ratio_ref

_memory: dict[str, Any] = {
    "frame_seq": -1,
    "ts_wall": 0.0,
    "cx": 0.5,
    "cy": 0.5,
    "area_ratio": 0.05,
    "identity": None,
    "salience_top_label": "",
}


def reset_continuity_memory() -> None:
    """Test / hot-reload hook."""
    _memory.update(
        {
            "frame_seq": -1,
            "ts_wall": 0.0,
            "cx": 0.5,
            "cy": 0.5,
            "area_ratio": 0.05,
            "identity": None,
            "salience_top_label": "",
        }
    )


def _primary_rect(rects: list[tuple[int, int, int, int]]) -> Optional[tuple[int, int, int, int]]:
    if not rects:
        return None
    return max(rects, key=lambda r: int(r[2]) * int(r[3]))


def _norm_geometry(
    rect: tuple[int, int, int, int], fw: int, fh: int
) -> tuple[float, float, float]:
    x, y, w, h = rect
    cx = (x + w / 2.0) / max(fw, 1)
    cy = (y + h / 2.0) / max(fh, 1)
    ar = (w * h) / float(max(fw * fh, 1))
    return cx, cy, max(0.0, min(1.0, ar))


def _spatial_scores(
    cur: tuple[float, float, float], prior: tuple[float, float, float]
) -> tuple[float, dict[str, float]]:
    dist = math.hypot(cur[0] - prior[0], cur[1] - prior[1])
    dist_leg = max(0.0, min(1.0, 1.0 - dist / max(SPATIAL_DIST_REF, 1e-6)))
    ar_min = min(cur[2], prior[2])
    ar_max = max(cur[2], prior[2], 1e-8)
    size_ratio = ar_min / ar_max
    size_leg = max(0.0, min(1.0, (size_ratio - SIZE_RATIO_REF) / max(1e-6, 1.0 - SIZE_RATIO_REF)))
    combined = cc.spatial_combine_dist_weight * dist_leg + cc.spatial_combine_size_weight * size_leg
    fac = {
        "center_dist_norm": round(dist, 4),
        "spatial_leg": round(dist_leg, 3),
        "size_ratio": round(size_ratio, 3),
        "size_leg": round(size_leg, 3),
        "spatial_combined": round(combined, 3),
    }
    return combined, fac


def update_continuity(
    *,
    trusted: bool,
    frame_seq: int,
    frame_ts: float,
    frame_shape: Optional[tuple[int, ...]],
    face_detected: bool,
    face_rects: list[tuple[int, int, int, int]],
    recognized_text: str,
    face_identity: Optional[str],
    salience_top_label: str,
    salience_top_type: str,
) -> ContinuityResult:
    """
    Compare the current trusted tick to module memory; update memory on success paths.
    Safe on any input; never raises.
    """
    notes: list[str] = []
    matched: dict[str, float] = {}
    prior_id = _memory.get("identity")
    if isinstance(prior_id, str) and not prior_id.strip():
        prior_id = None

    t_wall = time.time()
    ts_ref = float(frame_ts) if frame_ts and frame_ts > 1e6 else t_wall

    if not trusted:
        return ContinuityResult(
            identity_state="no_face",
            continuity_confidence=0.0,
            prior_identity=prior_id,
            current_identity=None,
            matched_factors={},
            matched_notes=["untrusted_deferred"],
            frame_gap=-1,
            seconds_since_prior=-1.0,
            suppress_flip=False,
            last_stable_identity=prior_id,
        )

    if not face_detected or frame_shape is None or len(frame_shape) < 2:
        fg = max(0, frame_seq - int(_memory.get("frame_seq", -1))) if frame_seq >= 0 else -1
        _maybe_decay_identity_no_face()
        print(
            f"[continuity] current=None prior={prior_id!r} state=no_face conf=0.00 "
            f"notes=no_face gap={fg}"
        )
        return ContinuityResult(
            identity_state="no_face",
            continuity_confidence=(cc.no_face_prior_confidence if prior_id else 0.0),
            prior_identity=prior_id,
            current_identity=None,
            matched_factors={},
            matched_notes=["no_face"],
            frame_gap=fg,
            seconds_since_prior=_seconds_since_prior(t_wall),
            suppress_flip=False,
            last_stable_identity=prior_id,
        )

    fh, fw = int(frame_shape[0]), int(frame_shape[1])
    primary = _primary_rect(list(face_rects or []))
    if primary is None or fw <= 0 or fh <= 0:
        _maybe_decay_identity_no_face()
        return ContinuityResult(
            identity_state="unknown_face",
            continuity_confidence=cc.unknown_face_no_rect_confidence,
            prior_identity=prior_id,
            current_identity=None,
            matched_factors={},
            matched_notes=["face_flag_no_rect"],
            frame_gap=max(0, frame_seq - int(_memory.get("frame_seq", -1))),
            seconds_since_prior=_seconds_since_prior(t_wall),
            suppress_flip=False,
            last_stable_identity=prior_id,
        )

    cx, cy, ar = _norm_geometry(primary, fw, fh)
    cur_geom = (cx, cy, ar)

    prev_seq = int(_memory.get("frame_seq", -1))
    frame_gap = max(0, frame_seq - prev_seq) if prev_seq >= 0 else 0
    sec_gap = _seconds_since_prior(t_wall)
    matched["frame_gap"] = float(frame_gap)
    matched["seconds_since_prior"] = round(float(sec_gap), 3)

    time_decay = 1.0
    if sec_gap > 0:
        time_decay = float(
            math.exp(-cc.time_decay_lambda * min(sec_gap, cc.time_decay_cap_sec))
        )
    if frame_gap > MAX_FRAME_GAP or sec_gap > MAX_SECONDS_GAP:
        time_decay *= cc.gap_penalty_scale
        notes.append("gap_penalty")

    prior_cx = float(_memory.get("cx", 0.5))
    prior_cy = float(_memory.get("cy", 0.5))
    prior_ar = float(_memory.get("area_ratio", 0.05))
    prior_geom = (prior_cx, prior_cy, prior_ar)

    spatial, s_fac = _spatial_scores(cur_geom, prior_geom)
    matched.update(s_fac)
    spatial_eff = spatial * time_decay
    matched["time_decay"] = round(time_decay, 3)
    matched["spatial_effective"] = round(spatial_eff, 3)

    sal_match = 0.0
    if salience_top_type == "face" and salience_top_label:
        plab = str(_memory.get("salience_top_label") or "")
        if plab and (plab == salience_top_label or (prior_id and salience_top_label == prior_id)):
            sal_match = 1.0
            notes.append("salience_top_match")
        elif plab == salience_top_label:
            sal_match = cc.salience_label_partial_match
            notes.append("salience_label_match")
    matched["salience_continuity"] = sal_match

    lbph_raw = lbph_distance_to_identity_confidence(recognized_text or "")
    matched["lbph_signal"] = round(lbph_raw, 3)

    suppress_flip = False
    current_id: Optional[str] = None
    state: str
    conf: float

    if face_identity:
        current_id = face_identity
        state = "confirmed_recognition"
        conf = float(
            min(
                1.0,
                max(
                    lbph_raw,
                    cc.confirmed_lbph_base
                    + cc.confirmed_lbph_spatial * spatial_eff
                    + cc.confirmed_lbph_salience * sal_match,
                ),
            )
        )
        notes.append("recognition_confirmed")
        _commit_memory(frame_seq, t_wall, ts_ref, cx, cy, ar, face_identity, salience_top_label)
        print(
            f"[continuity] current={current_id!r} prior={prior_id!r} state={state} conf={conf:.2f} "
            f"factors={{'spatial_eff': {spatial_eff:.2f}, 'lbph': {lbph_raw:.2f}}} notes={notes}"
        )
        return ContinuityResult(
            identity_state=state,
            continuity_confidence=conf,
            prior_identity=prior_id,
            current_identity=current_id,
            matched_factors=dict(matched),
            matched_notes=notes,
            frame_gap=frame_gap,
            seconds_since_prior=sec_gap,
            suppress_flip=suppress_flip,
            last_stable_identity=current_id,
        )

    # No recognition this frame — carry prior if geometry + time agree
    if prior_id and spatial_eff >= LIKELY_SAME_SPATIAL_MIN and frame_gap <= MAX_FRAME_GAP and sec_gap <= MAX_SECONDS_GAP:
        current_id = prior_id
        state = "likely_identity_by_continuity"
        conf = float(
            min(
                cc.carry_conf_cap,
                cc.carry_conf_base
                + cc.carry_conf_spatial * spatial_eff
                + cc.carry_conf_salience * sal_match,
            )
        )
        notes.append("spatial_carry_prior")
        suppress_flip = True
        print(
            f"[continuity] current={current_id!r} prior={prior_id!r} state={state} conf={conf:.2f} "
            f"suppress_flip=True spatial_eff={spatial_eff:.2f} notes={notes}"
        )
        _commit_memory(frame_seq, t_wall, ts_ref, cx, cy, ar, prior_id, salience_top_label or prior_id)
        return ContinuityResult(
            identity_state=state,
            continuity_confidence=conf,
            prior_identity=prior_id,
            current_identity=current_id,
            matched_factors=dict(matched),
            matched_notes=notes,
            frame_gap=frame_gap,
            seconds_since_prior=sec_gap,
            suppress_flip=suppress_flip,
            last_stable_identity=current_id,
        )

    state = "unknown_face"
    conf = float(max(cc.unknown_face_conf_floor, cc.unknown_face_conf_scale * spatial_eff))
    notes.append("no_recognition_weak_carry")
    current_id = None
    _commit_geometry_only(frame_seq, t_wall, ts_ref, cx, cy, ar, salience_top_label)
    print(
        f"[continuity] current=None prior={prior_id!r} state={state} conf={conf:.2f} "
        f"spatial_eff={spatial_eff:.2f} notes={notes}"
    )
    return ContinuityResult(
        identity_state=state,
        continuity_confidence=conf,
        prior_identity=prior_id,
        current_identity=current_id,
        matched_factors=dict(matched),
        matched_notes=notes,
        frame_gap=frame_gap,
        seconds_since_prior=sec_gap,
        suppress_flip=False,
        last_stable_identity=prior_id,
    )


def _seconds_since_prior(t_wall: float) -> float:
    pt = float(_memory.get("ts_wall", 0.0))
    if pt <= 0:
        return -1.0
    return max(0.0, t_wall - pt)


def _commit_geometry_only(
    frame_seq: int,
    t_wall: float,
    frame_ts: float,
    cx: float,
    cy: float,
    ar: float,
    sal_label: str,
) -> None:
    """Update last-seen geometry; do not clear stored identity (unknown_face path)."""
    _memory["frame_seq"] = int(frame_seq)
    _memory["ts_wall"] = t_wall
    _memory["frame_ts_ref"] = frame_ts
    _memory["cx"] = cx
    _memory["cy"] = cy
    _memory["area_ratio"] = ar
    _memory["salience_top_label"] = sal_label or ""


def _commit_memory(
    frame_seq: int,
    t_wall: float,
    frame_ts: float,
    cx: float,
    cy: float,
    ar: float,
    identity: Optional[str],
    sal_label: str,
) -> None:
    _commit_geometry_only(frame_seq, t_wall, frame_ts, cx, cy, ar, sal_label)
    if identity is not None:
        _memory["identity"] = identity


def _maybe_decay_identity_no_face() -> None:
    """After no face, soften carry (identity cleared only after this tick for memory)."""
    _memory["identity"] = None
    _memory["salience_top_label"] = ""
