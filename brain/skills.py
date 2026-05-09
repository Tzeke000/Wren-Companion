"""brain/skills.py — Procedural skill memory (Hermes-pattern, lite).

When Ava successfully dispatches a compound action (via action_tag_router),
we save the trigger phrase + action sequence as a "skill." On future
similar requests, the skill recall pass runs BEFORE the LLM action-tag
classifier — fuzzy-match user input against known skill triggers,
dispatch the stored actions immediately if there's a match.

Why:
- Speeds up repeated compound commands by 3-15s (no LLM classifier hop).
- Gives Ava procedural memory: "open OBS through Steam" becomes a known
  skill she just does, instead of re-deriving every time.
- Bootstrap-friendly: starts empty, populated only by actual successful
  compounds. No seeded skill catalog.

Storage: state/skills/<slug>.json — one file per skill so they're
human-readable and easy to inspect/edit/delete. Index file
state/skills/_index.json for fast-load + name lookups.

Skill schema:
{
  "slug": "open-obs-through-steam",
  "trigger_phrases": ["open obs through steam", "launch obs via steam"],
  "actions": [["OPEN_APP", "Steam"], ["OPEN_APP", "OBS"]],
  "success_count": 3,
  "last_used": 1777993200.0,
  "created": 1777983200.0
}

Recall is a similarity test on normalized trigger phrases vs the user's
input. We use a simple bag-of-words Jaccard for the lite version —
no embeddings, no FTS5 dependency. Threshold tuned conservatively to
avoid false matches.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_SKILL_TRIGGER_JACCARD_THRESHOLD = 0.7  # conservative — stricter avoids false positives
_MAX_TRIGGER_PHRASES_PER_SKILL = 8
_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "in", "on", "and", "or", "i",
    "you", "me", "ava", "please", "hey", "could", "would", "can", "now",
    "then", "this", "that", "is", "be", "have", "do", "does",
}


def _skills_dir(base_dir: Path) -> Path:
    p = base_dir / "state" / "skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _index_path(base_dir: Path) -> Path:
    return _skills_dir(base_dir) / "_index.json"


def _slug(text: str) -> str:
    """Slug for a skill — lowercase, hyphens, alphanum only."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:64] or "unnamed"


def _normalize(text: str) -> set[str]:
    """Lowercase, strip punct, drop stopwords. Return a token set."""
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / max(1, len(union))


# ── Storage ────────────────────────────────────────────────────────────────


def _load_index(base_dir: Path) -> dict[str, Any]:
    p = _index_path(base_dir)
    if not p.exists():
        return {"skills": {}, "version": 1}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"skills": {}, "version": 1}


def _save_index(base_dir: Path, idx: dict[str, Any]) -> None:
    p = _index_path(base_dir)
    p.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


def _skill_path(base_dir: Path, slug: str) -> Path:
    return _skills_dir(base_dir) / f"{slug}.json"


def load_skill(base_dir: Path, slug: str) -> dict[str, Any] | None:
    p = _skill_path(base_dir, slug)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_skill(base_dir: Path, skill: dict[str, Any]) -> None:
    slug = str(skill.get("slug") or "").strip()
    if not slug:
        return
    p = _skill_path(base_dir, slug)
    p.write_text(json.dumps(skill, ensure_ascii=False, indent=2), encoding="utf-8")
    idx = _load_index(base_dir)
    idx.setdefault("skills", {})[slug] = {
        "slug": slug,
        "trigger_phrases": skill.get("trigger_phrases") or [],
        "success_count": int(skill.get("success_count") or 0),
        "last_used": float(skill.get("last_used") or 0.0),
    }
    _save_index(base_dir, idx)


# ── Recall ─────────────────────────────────────────────────────────────────


def recall(base_dir: Path, user_input: str) -> tuple[dict[str, Any], float] | None:
    """Find the best-matching skill for the user's input.

    Returns (skill_dict, score) or None if no skill clears the
    threshold. Score is the max-jaccard across the skill's trigger
    phrases.
    """
    inp_tokens = _normalize(user_input)
    if not inp_tokens:
        return None
    idx = _load_index(base_dir)
    skills_meta = (idx.get("skills") or {}).items()
    best: tuple[str, float] | None = None
    for slug, meta in skills_meta:
        for phrase in (meta.get("trigger_phrases") or []):
            score = _jaccard(inp_tokens, _normalize(phrase))
            if score >= _SKILL_TRIGGER_JACCARD_THRESHOLD:
                if best is None or score > best[1]:
                    best = (slug, score)
    if best is None:
        return None
    skill = load_skill(base_dir, best[0])
    if skill is None:
        return None
    return (skill, best[1])


# ── Auto-creation ──────────────────────────────────────────────────────────


def auto_create_or_update(
    base_dir: Path,
    user_input: str,
    actions: list[tuple[str, str | None]],
) -> str | None:
    """Persist a skill from a successful compound dispatch.

    Single-action sequences are not stored — those are already covered
    by the regex voice command router or the action-tag classifier
    fast path. Skills are for COMPOUND sequences (≥2 actions) or
    long-tail phrasings the regex misses.

    Sandboxed (architecture #20, 2026-05-07): every action is checked
    against the skill_sandbox allowlist before persisting. Skills with
    forbidden actions (CLOSE_APP, DELETE_FILE, SEND_EMAIL, etc.) are
    rejected entirely — Ava cannot auto-learn destructive sequences.
    Per Zeke's spec: "no destructive permissions for auto-learned skills."

    Returns the slug if a skill was created/updated, None if skipped or
    rejected.
    """
    if not actions:
        return None
    # Skip trivial single-action sequences UNLESS the user phrased it in a
    # way the regex router likely missed (long input).
    if len(actions) < 2 and len(user_input.split()) <= 6:
        return None
    # Skip [CONVERSATION]-only sequences.
    real = [(t, a) for (t, a) in actions if t != "CONVERSATION"]
    if not real:
        return None

    # Sandbox: refuse to persist skills containing forbidden actions.
    try:
        from brain.skill_sandbox import validate_skill_actions
        ok, reason = validate_skill_actions(real)
        if not ok:
            print(f"[skills] REJECTED auto-skill (sandbox): {reason} | input={user_input!r}")
            return None
    except Exception as _se:
        # Fail-closed: if validator can't run, reject the skill rather
        # than persisting unvalidated.
        print(f"[skills] sandbox validator error — rejecting auto-skill (fail-closed): {_se!r}")
        return None

    # Slug from the first non-conversation action (e.g., "open-obs-steam").
    head = real[0]
    slug_parts = [head[0].lower().replace("_", "-")]
    if head[1]:
        slug_parts.append(_slug(head[1]))
    slug = "-".join(slug_parts) or _slug(user_input)
    slug = slug[:64]

    existing = load_skill(base_dir, slug)
    if existing:
        # Update in place — bump count + add new trigger phrasing if novel.
        triggers = list(existing.get("trigger_phrases") or [])
        norm_existing = [_normalize(p) for p in triggers]
        if all(_jaccard(_normalize(user_input), ne) < 0.95 for ne in norm_existing):
            triggers.append(user_input.strip().lower()[:160])
            triggers = triggers[-_MAX_TRIGGER_PHRASES_PER_SKILL:]
        existing["trigger_phrases"] = triggers
        existing["success_count"] = int(existing.get("success_count") or 0) + 1
        existing["last_used"] = time.time()
        save_skill(base_dir, existing)
        return slug

    # New skill.
    skill = {
        "slug": slug,
        "trigger_phrases": [user_input.strip().lower()[:160]],
        "actions": [[t, a] for (t, a) in real],
        "success_count": 1,
        "created": time.time(),
        "last_used": time.time(),
    }
    save_skill(base_dir, skill)
    return slug
