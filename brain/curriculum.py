"""brain/curriculum.py — Ava's moral curriculum module.

Exposes a small API over `curriculum/foundation/*.txt` (and future tiers).
Used by sleep mode Phase 2 (learning processing) to slowly read entries and
extract lessons. Also callable directly during idle time.

State persistence:
- `curriculum/foundation/_index.json` — generated index of entries with
  `reading_status` and `lessons_extracted` fields. Updated on `mark_read`.
- `state/learning/lessons.jsonl` — append-only log of lessons Ava has
  generated. One JSON object per line.

Performance budget: list/read/mark_read are <50 ms (file I/O on small
files). consolidation_hook is bounded by `time_budget_seconds`; it should
yield cleanly when the budget is exhausted, even mid-entry.

Personhood-frame note (per CONTINUOUS_INTERIORITY.md): comments here
describe testable mechanism. When a docstring says "she reads slowly,"
that's framing language for what the formula does — paced LLM calls — it
doesn't claim subjective experience.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

FOUNDATION_DIR = ROOT / "curriculum" / "foundation"
INDEX_PATH = FOUNDATION_DIR / "_index.json"
LESSONS_LOG = ROOT / "state" / "learning" / "lessons.jsonl"


# ── Config (reasonable defaults; consolidation tuning lives in sleep_mode config) ──

DEFAULT_PARAGRAPH_SECONDS = 10.0
"""Approximate time-per-paragraph budget for the slow reading pass.
Used by consolidation_hook to pace its work."""


# ── Parsing helpers ──────────────────────────────────────────────────────


_HEADER_RE = re.compile(r"^---\n(.+?)\n---\n", re.DOTALL)


def _parse_entry_file(path: Path) -> dict[str, Any]:
    """Read an entry file. Returns {meta: {...}, body: "..."}."""
    text = path.read_text(encoding="utf-8")
    m = _HEADER_RE.match(text)
    meta: dict[str, Any] = {}
    if m:
        header = m.group(1)
        for line in header.split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        body = text[m.end():]
    else:
        body = text
    # Convert known list-typed fields
    if isinstance(meta.get("themes"), str):
        meta["themes"] = [t.strip() for t in meta["themes"].split(",") if t.strip()]
    if isinstance(meta.get("lessons_extracted"), str):
        # Stored as JSON-ish list in header — try to parse, else empty.
        try:
            meta["lessons_extracted"] = json.loads(meta["lessons_extracted"]) if meta["lessons_extracted"] else []
        except Exception:
            meta["lessons_extracted"] = []
    return {"meta": meta, "body": body.strip(), "path": str(path)}


def _write_entry_file(path: Path, meta: dict[str, Any], body: str) -> None:
    """Re-write entry file with updated metadata, preserving body."""
    themes_str = ", ".join(meta.get("themes") or []) if isinstance(meta.get("themes"), list) else (meta.get("themes") or "")
    lessons = meta.get("lessons_extracted") or []
    lessons_str = json.dumps(lessons, ensure_ascii=False) if lessons else "[]"
    header_lines = [
        f"title: {meta.get('title', '')}",
        f"source: {meta.get('source', '')}",
        f"source_url: {meta.get('source_url', '')}",
        f"themes: {themes_str}",
        f"moral: {meta.get('moral', '')}",
        f"reading_status: {meta.get('reading_status', 'unread')}",
        f"lessons_extracted: {lessons_str}",
    ]
    out = "---\n" + "\n".join(header_lines) + "\n---\n\n" + body.strip() + "\n"
    path.write_text(out, encoding="utf-8")


def _load_index() -> list[dict[str, Any]]:
    if not INDEX_PATH.is_file():
        return []
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8")) or []
    except Exception:
        return []


def _save_index(index: list[dict[str, Any]]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def _entry_path(slug: str) -> Path:
    return FOUNDATION_DIR / f"{slug}.txt"


def _slugify(title: str) -> str:
    """Same logic as scripts/_parse_aesop.py — title → slug."""
    s = re.sub(r"[^a-zA-Z0-9 ]", "", str(title).replace("Æ", "ae").replace("æ", "ae"))
    return re.sub(r"\s+", "_", s.strip().lower())


# ── Public API ───────────────────────────────────────────────────────────


def list_curriculum(g: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return list of entries with status. Sorted unread-first.

    Each item: {slug, title, themes, moral, reading_status, lessons_extracted}.
    """
    index = _load_index()
    # Stable sort: unread → reading → read.
    order = {"unread": 0, "reading": 1, "read": 2}
    return sorted(index, key=lambda e: (order.get(str(e.get("reading_status") or "unread"), 9), str(e.get("title") or "")))


def read_curriculum_entry(g: dict[str, Any] | None = None, *, title: str | None = None, slug: str | None = None) -> str:
    """Return the body text of an entry. Pass title or slug.

    Raises ValueError if neither is given or the entry isn't found.
    """
    if not title and not slug:
        raise ValueError("must pass title or slug")
    if not slug:
        slug = _slugify(title or "")
    p = _entry_path(slug)
    if not p.is_file():
        raise ValueError(f"entry not found: {slug}")
    parsed = _parse_entry_file(p)
    return parsed["body"]


def get_entry(g: dict[str, Any] | None = None, *, title: str | None = None, slug: str | None = None) -> dict[str, Any]:
    """Return full entry dict {meta, body, path}."""
    if not title and not slug:
        raise ValueError("must pass title or slug")
    if not slug:
        slug = _slugify(title or "")
    p = _entry_path(slug)
    if not p.is_file():
        raise ValueError(f"entry not found: {slug}")
    return _parse_entry_file(p)


def mark_read(g: dict[str, Any] | None = None, *, title: str | None = None, slug: str | None = None,
              lessons_extracted: list[str] | None = None) -> dict[str, Any]:
    """Mark an entry as read; persist lessons to both the entry's metadata
    and to state/learning/lessons.jsonl. Returns the updated index entry.
    """
    if not title and not slug:
        raise ValueError("must pass title or slug")
    if not slug:
        slug = _slugify(title or "")
    p = _entry_path(slug)
    if not p.is_file():
        raise ValueError(f"entry not found: {slug}")
    parsed = _parse_entry_file(p)
    meta = parsed["meta"]
    meta["reading_status"] = "read"
    existing_lessons = meta.get("lessons_extracted") or []
    new_lessons = list(existing_lessons) + list(lessons_extracted or [])
    meta["lessons_extracted"] = new_lessons
    _write_entry_file(p, meta, parsed["body"])

    # Update index
    index = _load_index()
    for entry in index:
        if str(entry.get("slug")) == slug:
            entry["reading_status"] = "read"
            entry["lessons_extracted"] = new_lessons
            entry["read_at"] = time.time()
            break
    _save_index(index)

    # Append to lessons log
    LESSONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LESSONS_LOG.open("a", encoding="utf-8") as f:
        for lesson in (lessons_extracted or []):
            f.write(json.dumps({
                "ts": time.time(),
                "source": f"curriculum:{slug}",
                "lesson": str(lesson),
                "title": meta.get("title", ""),
                "themes": meta.get("themes", []),
            }, ensure_ascii=False) + "\n")

    return next((e for e in index if str(e.get("slug")) == slug), {})


def mark_reading(g: dict[str, Any] | None = None, *, slug: str) -> None:
    """Set reading_status to 'reading' on an entry (for in-flight consolidation)."""
    p = _entry_path(slug)
    if not p.is_file():
        return
    parsed = _parse_entry_file(p)
    meta = parsed["meta"]
    meta["reading_status"] = "reading"
    _write_entry_file(p, meta, parsed["body"])
    index = _load_index()
    for entry in index:
        if str(entry.get("slug")) == slug:
            entry["reading_status"] = "reading"
            break
    _save_index(index)


def _next_unread() -> dict[str, Any] | None:
    """Pick the next unread entry. Prefers 'reading' (in-flight) → 'unread'.
    Returns None if all are read.
    """
    index = _load_index()
    # 'reading' first (resume), then 'unread' (new).
    for status in ("reading", "unread"):
        for entry in index:
            if str(entry.get("reading_status") or "unread") == status:
                return entry
    return None


def consolidation_hook(g: dict[str, Any], time_budget_seconds: float = 60.0) -> dict[str, Any]:
    """Sleep-mode Phase 2 entry point.

    Picks the next unread entry, reads it slowly (paced one paragraph per
    DEFAULT_PARAGRAPH_SECONDS via LLM call), generates lesson notes,
    marks it read. Yields when time budget exhausted.

    For the first ship, we don't actually call an LLM here — we just pace
    the work and produce a placeholder lesson per paragraph based on the
    moral (if present). The wiring to a real LLM lives in sleep_mode.py
    where the dual_brain background stream is accessible. This function
    exposes a stable shape so sleep_mode can call it identically once that
    wiring lands.

    Returns:
        {
            "entry_processed": str | None,
            "lessons_generated": list[str],
            "time_used_s": float,
            "time_remaining_s": float,
            "fully_read": bool,
        }
    """
    started = time.time()
    entry = _next_unread()
    if entry is None:
        return {
            "entry_processed": None,
            "lessons_generated": [],
            "time_used_s": 0.0,
            "time_remaining_s": time_budget_seconds,
            "fully_read": False,
            "skipped_reason": "no_unread_entries",
        }

    slug = str(entry.get("slug") or "")
    parsed = _parse_entry_file(_entry_path(slug))
    body = parsed["body"]
    meta = parsed["meta"]

    # Mark in-flight so a crash mid-consolidation doesn't lose state
    mark_reading(g, slug=slug)

    # Pace through paragraphs.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    paragraph_seconds = float(_cfg(g, "consolidation", "paragraph_seconds", default=DEFAULT_PARAGRAPH_SECONDS))
    lessons: list[str] = []
    fully_read = True
    for i, paragraph in enumerate(paragraphs):
        elapsed = time.time() - started
        if elapsed + paragraph_seconds > time_budget_seconds:
            # Budget about to run out; stop cleanly.
            fully_read = False
            break
        # Pace: simulate the time the LLM call would take.
        # (When wired to real LLM, replace with brain.dual_brain background
        # call that asks "what's the lesson from this paragraph?")
        sleep_dur = max(0.0, min(paragraph_seconds, time_budget_seconds - elapsed))
        time.sleep(sleep_dur)

    # Generate at least one lesson based on the moral (placeholder).
    moral = meta.get("moral") or ""
    if moral:
        lessons.append(moral)

    if fully_read:
        mark_read(g, slug=slug, lessons_extracted=lessons)
    # else: stays in 'reading' state, resumes on next consolidation cycle.

    return {
        "entry_processed": meta.get("title", ""),
        "slug": slug,
        "lessons_generated": lessons,
        "time_used_s": round(time.time() - started, 2),
        "time_remaining_s": round(max(0.0, time_budget_seconds - (time.time() - started)), 2),
        "fully_read": fully_read,
    }


# ── Internal: config loader (decoupled from temporal_sense) ──


def _cfg(g: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    """Walk config/curriculum.json by keys. Returns default if missing.
    Caller may pass `g` for future per-instance overrides; not used today."""
    cfg_path = ROOT / "config" / "curriculum.json"
    if not cfg_path.is_file():
        return default
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return default
    cur: Any = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def get_readme_content() -> str:
    """Return the curriculum README contents — Ava's framing for what this is.
    Used by avaagent.py at boot to inject curriculum awareness into the
    inner-monologue / system context without editing IDENTITY.md."""
    p = ROOT / "curriculum" / "README.md"
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")
