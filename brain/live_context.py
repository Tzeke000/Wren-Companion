"""
Compact live cognitive context from current perception-aligned signals.

Bounded strings only — no vector retrieval or large dumps. Prefer present relevance over history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .concern_reconciliation import concern_surface_gate


def _t(s: str | None, n: int) -> str:
    x = " ".join((s or "").split())
    return x if len(x) <= n else x[: n - 1].rstrip() + "…"


@dataclass
class LiveContext:
    """Single-turn summary for prompts (bounded size)."""

    top_issue: str = ""
    top_thread: str = ""
    relationship_hint: str = ""
    strategic_carryover: str = ""
    memory_relevance: str = ""
    learning_maintenance_note: str = ""
    relevance_score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


def format_live_context_block(lc: LiveContext, *, max_chars: int = 900) -> str:
    """Dense single block for embedding in prompts."""
    lines = [
        f"Issue: {_t(lc.top_issue, 240)}" if lc.top_issue else "",
        f"Thread: {_t(lc.top_thread, 240)}" if lc.top_thread else "",
        f"Relationship: {_t(lc.relationship_hint, 200)}" if lc.relationship_hint else "",
        f"Strategic carryover: {_t(lc.strategic_carryover, 260)}" if lc.strategic_carryover else "",
        f"Memory refinement: {_t(lc.memory_relevance, 260)}" if lc.memory_relevance else "",
        f"Learning/maintenance: {_t(lc.learning_maintenance_note, 220)}"
        if lc.learning_maintenance_note
        else "",
    ]
    block = "\n".join(x for x in lines if x.strip())
    if len(block) > max_chars:
        block = block[: max_chars - 1].rstrip() + "…"
    return block


def build_live_context(perception: Any | None, g: dict[str, Any] | None = None) -> LiveContext:
    """Derive concise live context from PerceptionState-like object and optional host globals."""
    lc = LiveContext()
    if perception is None:
        return lc

    scores: list[float] = []

    lc.top_issue = _t(str(getattr(perception, "runtime_active_issue_summary", "") or ""), 280)
    if lc.top_issue:
        scores.append(0.72)

    thr = ""
    ut = bool(getattr(perception, "unfinished_thread_present", False))
    rs = float(getattr(perception, "refined_memory_social_relevance", 0.0) or 0.0)
    if ut or rs >= 0.42:
        thr = str(getattr(perception, "relationship_carryover", "") or "")[:120]
        if not thr.strip():
            thr = str(getattr(perception, "relationship_summary", "") or "")[:220]
        if not thr.strip() and ut:
            thr = "unfinished_thread_signal"
        lc.top_thread = _t(thr, 260)
        scores.append(min(1.0, 0.45 + rs))

    lc.relationship_hint = _t(str(getattr(perception, "relationship_summary", "") or ""), 220)
    if lc.relationship_hint:
        scores.append(float(getattr(perception, "relationship_confidence", 0.35) or 0.35))

    lc.strategic_carryover = _t(str(getattr(perception, "strategic_continuity_summary", "") or ""), 300)
    if lc.strategic_carryover:
        scores.append(float(getattr(perception, "strategic_continuity_confidence", 0.4) or 0.4))

    rm_class = str(getattr(perception, "refined_memory_class", "") or "")
    rm_pri = float(getattr(perception, "refined_memory_retrieval_priority", 0.0) or 0.0)
    rm_worthy = bool(getattr(perception, "refined_memory_worthy", False))
    mem_bits = []
    if rm_class and rm_class != "ignore":
        mem_bits.append(f"class={rm_class}")
    if rm_pri >= 0.35:
        mem_bits.append(f"retrieve≈{rm_pri:.2f}")
    if rm_worthy:
        mem_bits.append("worthy")
    lc.memory_relevance = _t("; ".join(mem_bits), 200)
    if mem_bits:
        scores.append(min(1.0, 0.35 + rm_pri))

    maint = str(getattr(perception, "improvement_loop_summary", "") or "")[:160]
    learn = str(getattr(perception, "learning_summary", "") or "")[:140]

    cam_gate = concern_surface_gate("camera_stale", g) or concern_surface_gate("recognition_uncertain", g)
    cam_age = None
    if isinstance(g, dict):
        p = g.get("CAMERA_LATEST_JSON_PATH")
        if p:
            try:
                import json
                from pathlib import Path
                from .shared import iso_to_ts, now_ts

                path = Path(str(p))
                if path.is_file():
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    ts = raw.get("time") or raw.get("capture_ts") or raw.get("wall_time")
                    if ts:
                        cam_age = max(0.0, float(now_ts() - iso_to_ts(str(ts))))
            except Exception:
                cam_age = None
    conf = float(getattr(perception, "identity_confidence", 0.0) or 0.0)
    cam_class = "historical_only"
    cam_line = ""
    if cam_age is None:
        cam_class = "no_frame"
        cam_line = "camera offline"
    elif cam_age > 60.0:
        cam_class = "frame_stale"
        cam_line = "camera frame stale"
    elif conf < 0.25:
        cam_class = "confidence_low"
        cam_line = "recognition uncertain, not a camera fault"
    else:
        cam_class = "no_issue"
        cam_line = ""

    parts = []
    if maint.strip():
        parts.append(f"improvement:{_t(maint, 140)}")
    if learn.strip():
        parts.append(f"learning:{_t(learn, 120)}")
    if cam_gate and cam_class not in ("no_issue", "historical_only"):
        parts.append(f"camera:{cam_line}")
    lc.learning_maintenance_note = _t(" | ".join(parts), 320)
    if parts:
        scores.append(0.55)

    lc.relevance_score = float(sum(scores) / max(1, len(scores))) if scores else 0.0

    snap = {}
    if isinstance(g, dict):
        snap = g.get("_runtime_self_snapshot") if isinstance(g.get("_runtime_self_snapshot"), dict) else {}
    if snap:
        lc.meta["runtime_snapshot_age_ok"] = bool(snap.get("ts"))
    lc.meta["camera_context_classification"] = cam_class

    lc.meta["fields_populated"] = sum(
        1
        for x in (
            lc.top_issue,
            lc.top_thread,
            lc.relationship_hint,
            lc.strategic_carryover,
            lc.memory_relevance,
            lc.learning_maintenance_note,
        )
        if (x or "").strip()
    )

    try:
        if lc.meta.get("fields_populated", 0):
            print(
                f"[live_context] threads={bool(lc.top_thread)} issue={bool(lc.top_issue)} "
                f"relevance={lc.relevance_score:.2f}"
            )
    except Exception:
        pass

    return lc


def attach_live_context_globals(g: dict[str, Any], lc: LiveContext) -> None:
    if not isinstance(g, dict):
        return
    g["_live_context_snapshot"] = {
        "top_issue": lc.top_issue[:300],
        "top_thread": lc.top_thread[:300],
        "relevance": round(lc.relevance_score, 4),
        "populated": int(lc.meta.get("fields_populated", 0)),
    }
