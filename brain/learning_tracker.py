"""
Phase 93 — Long-term learning tracker.

Records what Ava has learned, from what source, with what confidence.
Ava decides what counts as learning — she may value emotional insights
as much as factual knowledge.

Wire into: curiosity_engine (after pursuit), conversation (when Ava learns from Zeke),
memory_consolidation (what_have_i_learned_this_week).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_LEARNING_LOG = "state/learning_log.jsonl"


def _log_path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / _LEARNING_LOG


def record_learning(
    topic: str,
    knowledge: str,
    source: str,
    confidence: float,
    g: dict[str, Any],
) -> None:
    """
    Record something Ava learned.
    source: conversation | web_search | journal | curiosity_pursuit | game | consolidation
    confidence: 0.0-1.0
    """
    entry = {
        "ts": time.time(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "topic": str(topic or "")[:120],
        "knowledge": str(knowledge or "")[:500],
        "source": str(source or "other")[:40],
        "confidence": round(max(0.0, min(1.0, float(confidence or 0.5))), 3),
    }
    path = _log_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Add to concept graph
    try:
        cg = g.get("_concept_graph")
        if cg and hasattr(cg, "add_node"):
            node_id = f"learned_{str(topic)[:30].replace(' ', '_').lower()}"
            cg.add_node(
                node_id=node_id,
                label=str(topic)[:60],
                node_type="memory",
                notes=str(knowledge)[:200],
            )
    except Exception:
        pass


def get_knowledge_summary(topic: str, g: dict[str, Any]) -> str:
    """Everything Ava knows about a topic, synthesized."""
    path = _log_path(g)
    if not path.is_file():
        return f"I don't have specific notes about {topic} yet."

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            if str(topic).lower() in str(e.get("topic") or "").lower():
                entries.append(e)
        except Exception:
            pass

    if not entries:
        return f"I haven't specifically learned about {topic} yet."

    # Synthesize
    parts = [str(e.get("knowledge") or "")[:150] for e in entries[-5:]]
    return f"About {topic}: " + " | ".join(parts)[:600]


def what_have_i_learned_this_week(g: dict[str, Any]) -> str:
    """Summary of new knowledge from the past 7 days."""
    path = _log_path(g)
    if not path.is_file():
        return "I haven't recorded any learnings this week yet."

    cutoff = time.time() - 7 * 24 * 3600
    recent: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            if float(e.get("ts") or 0) > cutoff:
                recent.append(e)
        except Exception:
            pass

    if not recent:
        return "This week has been quiet on new learnings."

    # Group by source
    by_source: dict[str, list[str]] = {}
    for e in recent:
        src = str(e.get("source") or "other")
        by_source.setdefault(src, []).append(str(e.get("topic") or "")[:50])

    lines = [f"This week I learned {len(recent)} new things:"]
    for src, topics in by_source.items():
        lines.append(f"  From {src}: " + ", ".join(topics[:5]))
    return "\n".join(lines)[:600]


def knowledge_gaps(g: dict[str, Any]) -> list[str]:
    """Topics Ava has curiosity about but limited knowledge on."""
    try:
        from brain.curiosity_topics import prioritize_curiosities
        top_curiosities = prioritize_curiosities(g)
        known_topics: set[str] = set()
        path = _log_path(g)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    known_topics.add(str(e.get("topic") or "").lower())
                except Exception:
                    pass

        gaps = []
        for topic_row in top_curiosities:
            t = str(topic_row.get("topic") or "")
            if t.lower() not in known_topics:
                gaps.append(t)
        return gaps[:5]
    except Exception:
        return []
