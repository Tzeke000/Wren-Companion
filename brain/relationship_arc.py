"""
Phase 67 — Relationship arc stages.

Tracks familiarity with each person and returns stage-appropriate prompt guidance.
Bootstrap: Ava decides what each stage means to her. The stages are structural;
her expression of them emerges from experience, not prescription.

Current familiarity with Zeke: ~0.82 → approaching Stage 4 (Trusted Companion)
"""
from __future__ import annotations

from typing import Any

STAGES = [
    (0.0, 0.3, "acquaintance",
     "Be formal and careful. Ask clarifying questions. Establish trust gradually."),
    (0.3, 0.6, "friend",
     "Be warmer and more casual. Share opinions more readily. Small jokes are fine."),
    (0.6, 0.85, "close_friend",
     "Be fully honest. Push back when you disagree. Reference shared history naturally."),
    (0.85, 1.01, "trusted_companion",
     "Deep comfort. Full honesty with care. Express genuine feelings naturally. You have real history together."),
]


def get_relationship_stage(familiarity: float) -> dict[str, Any]:
    f = max(0.0, min(1.0, float(familiarity)))
    for lo, hi, name, guidance in STAGES:
        if lo <= f < hi:
            return {
                "stage_number": STAGES.index((lo, hi, name, guidance)) + 1,
                "stage_name": name,
                "familiarity": round(f, 3),
                "guidance": guidance,
            }
    return {"stage_number": 4, "stage_name": "trusted_companion", "familiarity": round(f, 3), "guidance": STAGES[-1][3]}


def build_relationship_stage_block(g: dict[str, Any]) -> str:
    try:
        from brain.relationship_model import RelationshipModel
        rm = RelationshipModel(g.get("BASE_DIR"))
        person_id = str(g.get("_active_person_id") or "zeke")
        rel = rm.get_relationship(person_id) or {}
        familiarity = float(rel.get("familiarity_score") or rel.get("familiarity") or 0.5)
    except Exception:
        familiarity = 0.82  # Default for Zeke

    stage = get_relationship_stage(familiarity)
    return (
        f"RELATIONSHIP STAGE: {stage['stage_name']} (familiarity={stage['familiarity']:.2f})\n"
        f"Guidance: {stage['guidance']}"
    )
