"""Shared perception helpers (no pipeline imports — safe for perception_pipeline)."""
from __future__ import annotations

import re


def lbph_distance_to_identity_confidence(recognized_text: str, threshold: float = 80.0) -> float:
    """Map LBPH distance in parentheses to [0,1]; higher = stronger match signal."""
    m = re.search(r"\((\d+\.?\d*)\)\s*$", (recognized_text or "").strip())
    if not m:
        return 0.45
    dist = float(m.group(1))
    if dist <= threshold:
        return max(0.35, min(1.0, 1.0 - (dist / (threshold * 2.2))))
    return max(0.0, 0.25 * (1.0 - min(1.0, (dist - threshold) / 80.0)))


def compute_salience(face_detected: bool, face_emotion: str | None, user_text: str) -> float:
    """Legacy engagement scalar (face + emotion + user text). Blended into Phase 6 structured salience in ``brain.salience``."""
    if not face_detected:
        return 0.2
    em = (face_emotion or "").lower()
    if em in ("angry", "fear", "disgust"):
        return 1.0
    if user_text:
        return 0.9
    return 0.6
