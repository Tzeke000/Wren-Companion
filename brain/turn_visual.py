"""
Standard visual payload for `run_ava` and Gradio status columns (Phase 1 — runtime stability).

All turns should return a dict containing at least the **core UI keys** so empty `{}` never
wipes face/recognition/expression lines. Optional **metadata keys** are preserved for logging
and future UI; Gradio callbacks typically read only the core four.

**Core keys** (string values):
- ``face_status``: Detection / vision-stability line shown in UI.
- ``recognition_status``: Recognition summary or a neutral placeholder (e.g. "—").
- ``expression_status``: Expression pipeline summary or "—".
- ``memory_preview``: Short memory snippet injected into the LLM prompt (may be empty).

**Metadata keys** (optional, preserved by :func:`normalize_visual_payload`):
- ``turn_route``: How the turn completed: ``blocked``, ``deflect``, ``selfstate``,
  ``camera_identity``, ``llm``, ``error``.
- ``visual_truth_trusted``: Whether the camera layer treated the frame as stable (bool | None).
- ``vision_status``: Raw vision state string from perception / camera (e.g. ``stable``, ``no_frame``).

No environment variables are required; callers pass explicit values.

Canonical key lists: ``VISUAL_CORE_KEYS`` (required on every turn) and ``VISUAL_METADATA_KEYS``
(optional diagnostics merged by :func:`normalize_visual_payload`).
"""
from __future__ import annotations

from typing import Any

# Keys required for a safe, complete visual row (Gradio / status panels).
VISUAL_CORE_KEYS: tuple[str, ...] = (
    "face_status",
    "recognition_status",
    "expression_status",
    "memory_preview",
)

# Optional diagnostics; merged through if present on the incoming dict.
VISUAL_METADATA_KEYS: tuple[str, ...] = (
    "turn_route",
    "visual_truth_trusted",
    "vision_status",
)


def default_visual_payload(
    *,
    face_status: str = "No camera data for this turn",
    recognition_status: str = "—",
    expression_status: str = "—",
    memory_preview: str = "",
    turn_route: str = "default",
    visual_truth_trusted: bool | None = None,
    vision_status: str | None = None,
) -> dict[str, Any]:
    """
    Build a full default visual dict. Use for early exits where no prompt was built
    (blocked, deflect, error fallback).
    """
    out: dict[str, Any] = {
        "face_status": face_status,
        "recognition_status": recognition_status,
        "expression_status": expression_status,
        "memory_preview": memory_preview,
        "turn_route": turn_route,
    }
    if visual_truth_trusted is not None:
        out["visual_truth_trusted"] = visual_truth_trusted
    if vision_status is not None:
        out["vision_status"] = vision_status
    return out


def normalize_visual_payload(
    visual: dict | None,
    *,
    turn_route: str | None = None,
) -> dict[str, Any]:
    """
    Merge ``visual`` with safe defaults so every core key is present.

    - Empty strings for ``face_status``, ``recognition_status``, or ``expression_status`` are
      ignored so they do not replace the placeholder with blanks.
    - ``memory_preview`` may be empty.
    - Unknown keys on ``visual`` are copied through for forward compatibility.
    """
    base = default_visual_payload()
    src = dict(visual or {})
    if turn_route:
        src.setdefault("turn_route", turn_route)

    for key in VISUAL_CORE_KEYS:
        if key not in src:
            continue
        val = src[key]
        if val is None:
            continue
        if key != "memory_preview":
            if isinstance(val, str) and not val.strip():
                continue
        base[key] = val

    for key in VISUAL_METADATA_KEYS:
        if key in src and src[key] is not None:
            base[key] = src[key]

    for key, val in src.items():
        if key in base:
            continue
        base[key] = val

    return base
