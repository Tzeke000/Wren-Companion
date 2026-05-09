"""
Phase 8 — explicit identity fallback hierarchy on top of Phase 7 continuity.

Separates **raw** LBPH output from **resolved** identity for prompts/UI/memory hooks.
Canonical ``identity_state`` values: ``confirmed_recognition``,
``likely_identity_by_continuity``, ``unknown_face``, ``no_face``.
"""
from __future__ import annotations

from typing import Optional

from config.ava_tuning import IDENTITY_CONFIG

from .perception_types import ContinuityResult, IdentityResolutionResult
from .perception_utils import lbph_distance_to_identity_confidence

idcfg = IDENTITY_CONFIG
CONFIRM_LBPH_MIN = idcfg.confirm_lbph_min


def resolve_identity_fallback(
    *,
    trusted: bool,
    face_detected: bool,
    raw_identity: Optional[str],
    recognized_text: str,
    recognition_confidence_scale: float,
    continuity: Optional[ContinuityResult],
) -> IdentityResolutionResult:
    """
    Choose public identity state and resolved/stable fields. Never raises.

    **Hierarchy** (first match wins):
    1. ``no_face`` — untrusted or no face in frame
    2. ``confirmed_recognition`` — non-empty raw id + scaled LBPH ≥ :data:`CONFIRM_LBPH_MIN`
    3. ``likely_identity_by_continuity`` — continuity carries prior known id (spatial/time/salience)
    4. ``unknown_face`` — face geometry present but no safe assignment
    """
    cont = continuity or ContinuityResult(identity_state="no_face")
    raw = raw_identity if (raw_identity and str(raw_identity).strip()) else None
    lbph_scaled = lbph_distance_to_identity_confidence(recognized_text or "") * float(
        recognition_confidence_scale or 1.0
    )
    notes: list[str] = []
    prior = cont.prior_identity

    if not trusted:
        print(
            f"[identity_fallback] raw={raw!r} continuity={cont.identity_state!r} "
            f"resolved=None state=no_face source=none (untrusted)"
        )
        return IdentityResolutionResult(
            identity_state="no_face",
            raw_identity=raw,
            resolved_identity=None,
            stable_identity=prior,
            identity_confidence=0.0,
            fallback_source="none",
            fallback_notes=["untrusted"],
        )

    if not face_detected:
        print(
            f"[identity_fallback] raw={raw!r} continuity={cont.identity_state!r} "
            f"resolved=None state=no_face source=none"
        )
        return IdentityResolutionResult(
            identity_state="no_face",
            raw_identity=raw,
            resolved_identity=None,
            stable_identity=None,
            identity_confidence=0.0,
            fallback_source="none",
            fallback_notes=["no_face"],
        )

    if raw is not None and lbph_scaled >= CONFIRM_LBPH_MIN:
        notes.append("recognition_meets_threshold")
        print(
            f"[identity_fallback] raw={raw!r} continuity={cont.identity_state!r} "
            f"resolved={raw!r} state=confirmed_recognition source=recognition conf={lbph_scaled:.2f}"
        )
        return IdentityResolutionResult(
            identity_state="confirmed_recognition",
            raw_identity=raw,
            resolved_identity=raw,
            stable_identity=raw,
            identity_confidence=float(min(1.0, lbph_scaled)),
            fallback_source="recognition",
            fallback_notes=notes,
        )

    if cont.identity_state == "likely_identity_by_continuity" and cont.current_identity:
        notes.append("continuity_carries_prior")
        carry_notes = notes + list(cont.matched_notes[:4])
        conf = float(
            min(
                idcfg.likely_carry_conf_cap,
                max(
                    lbph_scaled,
                    cont.continuity_confidence * idcfg.likely_carry_continuity_scale,
                    idcfg.likely_carry_floor,
                ),
            )
        )
        print(
            f"[identity_fallback] raw={raw!r} continuity={cont.identity_state!r} "
            f"resolved={cont.current_identity!r} state=likely_identity_by_continuity "
            f"source=continuity conf={conf:.2f} (carry_not_flip)"
        )
        return IdentityResolutionResult(
            identity_state="likely_identity_by_continuity",
            raw_identity=raw,
            resolved_identity=cont.current_identity,
            stable_identity=cont.current_identity,
            identity_confidence=conf,
            fallback_source="continuity",
            fallback_notes=carry_notes,
        )

    if raw is not None and lbph_scaled < CONFIRM_LBPH_MIN:
        notes.append("recognizer_label_below_confirm_threshold")
    if cont.identity_state == "confirmed_recognition" and raw and lbph_scaled < CONFIRM_LBPH_MIN:
        notes.append("demoted_despite_continuity_pre_label")

    conf_u = float(
        max(
            lbph_scaled,
            idcfg.unknown_face_lbph_floor,
            min(
                idcfg.unknown_face_lbph_cap,
                cont.continuity_confidence * idcfg.unknown_face_lbph_scale,
            ),
        )
    )
    print(
        f"[identity_fallback] raw={raw!r} continuity={cont.identity_state!r} "
        f"resolved=None state=unknown_face source=none conf={conf_u:.2f}"
    )
    return IdentityResolutionResult(
        identity_state="unknown_face",
        raw_identity=raw,
        resolved_identity=None,
        stable_identity=prior,
        identity_confidence=min(idcfg.unknown_face_identity_cap, conf_u),
        fallback_source="none",
        fallback_notes=notes or ["unknown_face"],
    )
