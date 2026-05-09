"""
Long-term life rhythm synthesis: recurring patterns from reflections + memory (Phase 5).
Emerges once there is enough session history; refreshed on a cooldown via startup scheduler.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .shared import atomic_json_save, json_load, now_iso

MIN_SESSIONS_FOR_LIFE_MODEL = 50
DEFAULT_WINDOW_DAYS = 30
MIN_HOURS_BETWEEN_RUNS = 24
MAX_PATTERNS = 12


def life_model_path(host: dict | None = None) -> Path:
    if host and host.get("LIFE_MODEL_PATH"):
        return Path(host["LIFE_MODEL_PATH"])
    return Path(__file__).resolve().parent.parent / "state" / "life_model.json"


def load_life_model(host: dict | None = None) -> dict[str, Any]:
    p = life_model_path(host)
    return json_load(str(p), {})


def save_life_model(doc: dict[str, Any], host: dict | None = None) -> None:
    p = life_model_path(host)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_save(str(p), doc)


def _parse_iso_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")[:19])
    except ValueError:
        return None


def filter_reflections_by_age(rows: list[dict], days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    cutoff = datetime.now() - timedelta(days=days)
    out: list[dict] = []
    for r in rows or []:
        dt = _parse_iso_dt(str(r.get("timestamp") or ""))
        if dt and dt >= cutoff:
            out.append(r)
    return out


def filter_memories_by_age(rows: list[dict], days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    cutoff = datetime.now() - timedelta(days=days)
    out: list[dict] = []
    for r in rows or []:
        meta = r.get("metadata") or {}
        dt = _parse_iso_dt(str(meta.get("created_at") or ""))
        if dt and dt >= cutoff:
            out.append(r)
    return out


def should_run_analysis(
    total_sessions: int,
    last_doc: dict | None,
    min_sessions: int = MIN_SESSIONS_FOR_LIFE_MODEL,
    min_hours: float = MIN_HOURS_BETWEEN_RUNS,
) -> bool:
    if total_sessions < min_sessions:
        return False
    raw = (last_doc or {}).get("last_run_at") or ""
    if not raw:
        return True
    last = _parse_iso_dt(str(raw))
    if not last:
        return True
    return (datetime.now() - last) >= timedelta(hours=min_hours)


def reflections_to_corpus(rows: list[dict], budget: int) -> str:
    parts: list[str] = []
    used = 0
    for r in rows:
        line = (
            f"[{r.get('timestamp', '')}] {r.get('summary', '')} "
            f"tags={','.join(r.get('tags') or [])} imp={r.get('importance', 0)}"
        )
        line = re.sub(r"\s+", " ", line.strip())[:400]
        if used + len(line) + 1 > budget:
            break
        parts.append(line)
        used += len(line) + 1
    return "\n".join(parts)


def memories_to_corpus(rows: list[dict], budget: int) -> str:
    parts: list[str] = []
    used = 0
    for r in rows:
        meta = r.get("metadata") or {}
        tone = meta.get("emotional_tone", "")
        txt = (meta.get("raw_text") or r.get("text") or "").strip()[:320]
        line = f"[{meta.get('created_at', '')}] tone={tone} {txt}"
        line = re.sub(r"\s+", " ", line.strip())[:400]
        if used + len(line) + 1 > budget:
            break
        parts.append(line)
        used += len(line) + 1
    return "\n".join(parts)


def run_life_rhythm_llm(
    call_llm: Callable[[str, int], str],
    person_name: str,
    reflections_corpus: str,
    memories_corpus: str,
    total_sessions: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any] | None:
    if not callable(call_llm):
        return None
    prompt = f"""You are analyzing long-term conversational evidence about {person_name} for Ava, a companion AI.
You only see excerpts from the last ~{window_days} days (reflections + stored memories). Infer soft hypotheses about rhythms and recurring themes — not certain facts.

Reflections / summaries:
{reflections_corpus[:6000]}

Memory excerpts:
{memories_corpus[:6000]}

Reported total_sessions (all time, approximate): {total_sessions}

Return ONLY valid JSON with these keys:
- "patterns": array of up to {MAX_PATTERNS} short strings (e.g. "Often mentions work stress mid-week", "Seems more energized on weekends") — tentative language, no absolutes
- "summary": one short paragraph tying themes together (3-5 sentences max)
- "relationships_note": one or two sentences about important people or social context if visible, else ""

No markdown, no other keys."""
    try:
        raw = call_llm(prompt, max_tokens=500)
        if not raw:
            return None
        m = re.search(r"\{[\s\S]*\}", raw.strip())
        if not m:
            return None
        data = json.loads(m.group())
        if not isinstance(data, dict):
            return None
        patterns = data.get("patterns")
        if not isinstance(patterns, list):
            patterns = []
        clean_patterns: list[str] = []
        for p in patterns[:MAX_PATTERNS]:
            if isinstance(p, str) and p.strip():
                clean_patterns.append(p.strip()[:240])
        summary = str(data.get("summary") or "").strip()[:1200]
        rel_note = str(data.get("relationships_note") or "").strip()[:600]
        return {
            "patterns": clean_patterns,
            "summary": summary,
            "relationships_note": rel_note,
        }
    except Exception:
        return None


def merge_output_doc(
    person_id: str,
    person_name: str,
    total_sessions: int,
    window_days: int,
    refl_rows: list[dict],
    mem_rows: list[dict],
    llm_part: dict[str, Any] | None,
) -> dict[str, Any]:
    llm_part = llm_part or {}
    patterns = list(llm_part.get("patterns") or [])
    summary = str(llm_part.get("summary") or "").strip()
    rel = str(llm_part.get("relationships_note") or "").strip()
    lines = [
        "[Life rhythm — long-term hypotheses from recent weeks; not guaranteed facts]",
    ]
    for p in patterns:
        lines.append(f"- {p}")
    if summary:
        lines.append(summary)
    if rel:
        lines.append(f"Relationships: {rel}")
    prompt_inject = "\n".join(lines)[:2400]
    return {
        "last_updated": now_iso(),
        "last_run_at": now_iso(),
        "subject_person_id": person_id,
        "subject_name": person_name,
        "sessions_at_run": int(total_sessions),
        "window_days": window_days,
        "reflections_used": len(refl_rows),
        "memories_used": len(mem_rows),
        "patterns": patterns,
        "summary": summary,
        "relationships_note": rel,
        "prompt_inject": prompt_inject,
    }


def get_prompt_block_for_person(
    active_person_id: str,
    owner_person_id: str,
    host: dict | None = None,
) -> str:
    if not active_person_id or active_person_id != owner_person_id:
        return ""
    doc = load_life_model(host)
    if not doc.get("prompt_inject") and not doc.get("patterns"):
        return ""
    inj = str(doc.get("prompt_inject") or "").strip()
    if inj:
        return inj[:2400]
    lines = ["[Life rhythm — long-term hypotheses from recent weeks]"]
    for p in doc.get("patterns") or []:
        if isinstance(p, str) and p.strip():
            lines.append(f"- {p.strip()}")
    if doc.get("summary"):
        lines.append(str(doc["summary"]).strip())
    return "\n".join(lines)[:2400]
