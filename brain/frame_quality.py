"""
Phase 4 — explicit frame quality scoring (OpenCV, local).
Phase 5 — dedicated blur detection (Laplacian variance) with labels and confidence scales.

Computes per-metric scores, an overall score **compatible with legacy camera gating**,
and labels ``usable`` | ``weak`` | ``unreliable``. Logs with prefix ``[frame_quality]``.

Tuning knobs live in :mod:`config.ava_tuning` (``QUALITY_CONFIG``, ``BLUR_CONFIG``, etc.).

**Overall score** uses the same recipe as the former ``assess_frame_quality_basic``:
``0.55 * blur_norm + 0.45 * lum_comfort``, then ×0.82 if any ``reason_flags`` apply.
**Motion** / **occlusion** metrics are diagnostic only and do not change ``overall_quality_score``.

Future hooks: blur → scene summaries, recognition fallback, visual memory worthiness — see
``FrameQualityAssessment.meta`` and ``blur_reason_flags``.
"""
from __future__ import annotations

import time
from typing import Any, Optional

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

from config.ava_tuning import BLUR_CONFIG, CONFIDENCE_SCALE_CONFIG, QUALITY_CONFIG

from .perception_types import FrameQualityAssessment

# Public aliases (backward compatible; values from :mod:`config.ava_tuning`).
WEAK_MIN_OVERALL = QUALITY_CONFIG.weak_min_overall
USABLE_MIN_OVERALL = QUALITY_CONFIG.usable_min_overall
BLUR_VAR_SOFT_MAX = BLUR_CONFIG.var_soft_max
BLUR_VAR_SHARP_MIN = BLUR_CONFIG.var_sharp_min
BLUR_VAR_SCALE = BLUR_CONFIG.var_scale

_prev_gray_small: Optional[Any] = None


def classify_blur_laplacian_var(blur_var: float) -> tuple[str, list[str]]:
    """
    Map raw Laplacian variance to ``blur_label`` and ``blur_reason_flags``.

    Thresholds from ``BLUR_CONFIG`` (``var_soft_max``, ``var_sharp_min``).
    """
    bc = BLUR_CONFIG
    if blur_var < bc.var_soft_max:
        return "blurry", ["blur_blurry"]
    if blur_var < bc.var_sharp_min:
        return "soft", ["blur_soft"]
    return "sharp", []


def blur_layer_confidence_scales(blur_label: str) -> tuple[float, float, float]:
    """
    Multipliers for (recognition, expression, interpretation).

    Interpretation uses a **lighter** penalty than expression (salience / scene hooks).
    """
    bc = BLUR_CONFIG
    if blur_label == "sharp":
        return bc.sharp_recognition, bc.sharp_expression, bc.sharp_interpretation
    if blur_label == "soft":
        return bc.soft_recognition, bc.soft_expression, bc.soft_interpretation
    return bc.blurry_recognition, bc.blurry_expression, bc.blurry_interpretation


def _label_from_overall(overall: float) -> str:
    qc = QUALITY_CONFIG
    if overall >= qc.usable_min_overall:
        return "usable"
    if overall >= qc.weak_min_overall:
        return "weak"
    return "unreliable"


def confidence_scales_from_label(label: str) -> tuple[float, float]:
    """(recognition_scale, expression_scale) from overall quality label — Phase 4."""
    cs = CONFIDENCE_SCALE_CONFIG
    if label == "usable":
        return cs.usable_recognition, cs.usable_expression
    if label == "weak":
        return cs.weak_recognition, cs.weak_expression
    return cs.unreliable_recognition, cs.unreliable_expression


def compute_frame_quality(frame: Any) -> FrameQualityAssessment:
    """
    Analyze ``frame`` (BGR). Safe on ``None`` or import failure.
    """
    global _prev_gray_small
    qc = QUALITY_CONFIG
    bc = BLUR_CONFIG
    empty = FrameQualityAssessment(
        blur_value=0.0,
        blur_label="blurry",
        blur_confidence_scale=bc.blurry_recognition,
        blur_recognition_scale=bc.blurry_recognition,
        blur_expression_scale=bc.blurry_expression,
        blur_interpretation_scale=bc.blurry_interpretation,
        blur_reason_flags=["blur_blurry"],
        overall_quality_score=qc.empty_assessment_overall,
        quality_label="unreliable",
        reason_flags=["no_frame"],
        meta={"ts": time.time()},
    )
    if frame is None or cv2 is None or np is None:
        print(
            "[frame_quality] blur_value=0.0 blur_score=0.000 blur_label=blurry "
            "blur_scales=(0.78,0.72,0.90) darkness=0.000 overexposure=0.000 "
            "overall=0.000 label=unreliable (no_frame_or_cv)"
        )
        return empty

    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        mean_lum = float(gray.mean()) / 255.0

        blur_label, blur_reason_flags = classify_blur_laplacian_var(blur_var)
        br, be, bi = blur_layer_confidence_scales(blur_label)

        reason_flags: list[str] = []
        if blur_var < bc.var_soft_max:
            reason_flags.append("blurry")
        if mean_lum < qc.mean_lum_very_dark:
            reason_flags.append("very_dark")
        elif mean_lum < qc.mean_lum_low_light:
            reason_flags.append("low_light")
        if mean_lum > qc.mean_lum_overexposed:
            reason_flags.append("overexposed")

        blur_score = min(1.0, blur_var / bc.var_scale)
        lum_comfort = 1.0 - min(
            1.0, abs(mean_lum - qc.lum_comfort_center) * qc.lum_comfort_abs_scale
        )
        darkness_score = min(1.0, mean_lum / qc.darkness_norm_divisor)
        if mean_lum < qc.darkness_floor_under_lum:
            darkness_score = min(darkness_score, qc.darkness_floor_cap)
        overexposure_score = 1.0
        if mean_lum > qc.overexposure_start_lum:
            overexposure_score = max(
                0.0,
                1.0 - (mean_lum - qc.overexposure_start_lum) / qc.overexposure_span,
            )

        # Legacy overall (matches former assess_frame_quality_basic).
        overall = qc.overall_blur_weight * blur_score + qc.overall_lum_weight * max(
            0.0, lum_comfort
        )
        if reason_flags:
            overall *= qc.reason_flags_overall_scale

        motion_smear_score = 1.0
        h, w = gray.shape[:2]
        scale = min(1.0, qc.motion_resize_ref_px / max(w, 1))
        small = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))))
        if _prev_gray_small is not None and _prev_gray_small.shape == small.shape:
            diff = np.mean(np.abs(small.astype(np.float32) - _prev_gray_small.astype(np.float32)))
            motion_smear_score = float(
                max(0.0, min(1.0, 1.0 - diff / qc.motion_diff_divisor))
            )
        _prev_gray_small = small.copy()

        occlusion_score = 1.0
        try:
            edges = cv2.Canny(gray, qc.canny_low, qc.canny_high)
            edge_density = float(np.mean(edges > 0))
            meta_occlusion = {"edge_density": round(edge_density, 4), "note": "provisional_occlusion_proxy"}
        except Exception:
            meta_occlusion = {"note": "occlusion_unavailable"}
            occlusion_score = qc.occlusion_fallback_score

        label = _label_from_overall(overall)
        meta = {
            "blur_var": round(blur_var, 2),
            "mean_lum": round(mean_lum, 4),
            "blur_label": blur_label,
            **meta_occlusion,
        }

        print(
            f"[frame_quality] blur_value={blur_var:.1f} blur_score={blur_score:.3f} blur_label={blur_label} "
            f"blur_scales(rec,expr,interp)=({br:.2f},{be:.2f},{bi:.2f}) "
            f"darkness={darkness_score:.3f} overexposure={overexposure_score:.3f} "
            f"motion_smear={motion_smear_score:.3f} occlusion={occlusion_score:.3f} "
            f"overall={overall:.3f} label={label} flags={reason_flags}"
        )

        return FrameQualityAssessment(
            blur_value=blur_var,
            blur_label=blur_label,
            blur_confidence_scale=br,
            blur_recognition_scale=br,
            blur_expression_scale=be,
            blur_interpretation_scale=bi,
            blur_reason_flags=list(blur_reason_flags),
            blur_score=blur_score,
            darkness_score=darkness_score,
            overexposure_score=overexposure_score,
            motion_smear_score=motion_smear_score,
            occlusion_score=occlusion_score,
            overall_quality_score=overall,
            quality_label=label,
            reason_flags=reason_flags,
            meta=meta,
        )
    except Exception as e:
        br, be, bi = blur_layer_confidence_scales("soft")
        print(f"[frame_quality] compute failed: {e} — provisional soft blur scales")
        return FrameQualityAssessment(
            blur_value=-1.0,
            blur_label="soft",
            blur_confidence_scale=br,
            blur_recognition_scale=br,
            blur_expression_scale=be,
            blur_interpretation_scale=bi,
            blur_reason_flags=["blur_quality_unavailable"],
            blur_score=0.5,
            darkness_score=0.5,
            overexposure_score=0.5,
            motion_smear_score=0.5,
            occlusion_score=0.5,
            overall_quality_score=0.5,
            quality_label="weak",
            reason_flags=["quality_unavailable"],
            meta={"error": str(e)},
        )


def assess_frame_quality_basic(frame: Any) -> tuple[float, list[str]]:
    """Backward-compatible (score, reasons) for callers expecting the old API."""
    q = compute_frame_quality(frame)
    return q.overall_quality_score, list(q.reason_flags)
