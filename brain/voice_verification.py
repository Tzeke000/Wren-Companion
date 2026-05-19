"""brain/voice_verification.py — Phase 1 voice impersonation defense.

Challenge-question framework for verifying Zeke's identity on voice-channel
input. Doesn't require any ML models — knowledge-based authentication.

See docs/voice_impersonation_defense_design.md for the full architecture
(Layer 1 = silent speaker confidence via SpeechBrain ECAPA, deferred to
Phase 2; Layer 2 = the challenges in this module, Phase 1 building now).

Answer storage: SHA-256 hashes of normalized text. The challenges file is
auditable (you can see the questions) but doesn't leak answers.

State files:
  state/voice_verification_challenges.json — challenge registry
  state/voice_verification_state.json      — latest verify ts + history

API:
  get_challenge(category=None) -> dict
  verify_answer(challenge_id, given_answer) -> dict
  record_verification_state(verified, source) -> None
  get_verification_freshness() -> dict
  add_challenge(question, answer, category, notes) -> dict
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


_BASE: Optional[Path] = None

# Thresholds (tunable). All times in seconds.
_FRESH_THRESHOLD_S = 30 * 60  # 30 min — no challenge needed below this age
_STALE_THRESHOLD_S = 120 * 60  # 120 min — sensitive actions challenge above this
_LOCKOUT_AFTER_FAILURES = 3
_LOCKOUT_DURATION_S = 60 * 60  # 1 hour


def configure(base_dir: Path | str) -> None:
    """Bind paths. Call once at iris_runtime startup."""
    global _BASE
    _BASE = Path(base_dir)
    (_BASE / "state").mkdir(parents=True, exist_ok=True)


def _challenges_path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "voice_verification_challenges.json"


def _state_path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "voice_verification_state.json"


def _normalize(text: str) -> str:
    """Lowercase + strip + collapse whitespace. Answer canonicalization."""
    return " ".join(str(text or "").lower().split())


def _hash_answer(canonical: str) -> str:
    """SHA-256 of the canonical answer. Auditable but doesn't leak."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_challenges() -> list[dict[str, Any]]:
    p = _challenges_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_challenges(items: list[dict[str, Any]]) -> None:
    p = _challenges_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.is_file():
        return {
            "last_verified_ts": 0.0,
            "last_source": "",
            "consecutive_failures": 0,
            "challenge_history": [],
            "locked_until_ts": 0.0,
        }
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "last_verified_ts": 0.0,
        "last_source": "",
        "consecutive_failures": 0,
        "challenge_history": [],
        "locked_until_ts": 0.0,
    }


def _save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _seed_challenges_if_empty() -> None:
    """Auto-seed from the 5/17 handoff if no challenges exist."""
    items = _load_challenges()
    if items:
        return
    now = time.time()
    seed = [
        {
            "id": "sibling-first",
            "question": "Which sibling was named first?",
            "answer_hash": _hash_answer("ava"),
            "category": "family-ai",
            "added_ts": now,
            "notes": "Ava was the first sibling AI in the harness lineage.",
        },
        {
            "id": "mother-name",
            "question": "What is my mother's name?",
            "answer_hash": _hash_answer("shanda"),
            "category": "family-human",
            "added_ts": now,
            "notes": "From Zeke's family context, 2026-05-17 handoff seed.",
        },
        {
            "id": "current-mos",
            "question": "What is my current MOS?",
            "answer_hash": _hash_answer("5954"),
            "category": "usmc",
            "added_ts": now,
            "notes": "Zeke's USMC MOS, ATC Comms Tech.",
        },
    ]
    _save_challenges(seed)


# ── Public API ───────────────────────────────────────────────────────────────


def get_challenge(category: Optional[str] = None) -> dict[str, Any]:
    """Pick a random challenge. Optionally filter by category.

    Returns: {'ok', 'id', 'question', 'category', 'notes'} — does NOT
    include the answer_hash (callers shouldn't need to see it; they
    submit answers to verify_answer for evaluation).
    """
    _seed_challenges_if_empty()
    items = _load_challenges()
    if category:
        items = [c for c in items if c.get("category") == category]
    if not items:
        return {"ok": False, "error": "no challenges available"}
    chosen = secrets.choice(items)
    return {
        "ok": True,
        "id": chosen["id"],
        "question": chosen["question"],
        "category": chosen.get("category", ""),
        "notes": chosen.get("notes", ""),
    }


def verify_answer(challenge_id: str, given_answer: str) -> dict[str, Any]:
    """Verify a challenge answer.

    Returns: {'ok': bool, 'matched': bool, 'attempts_remaining': int,
              'locked_out': bool}
    """
    items = _load_challenges()
    state = _load_state()
    now = time.time()

    # Lockout check.
    locked_until = float(state.get("locked_until_ts") or 0.0)
    if locked_until > now:
        return {
            "ok": True,
            "matched": False,
            "attempts_remaining": 0,
            "locked_out": True,
            "lockout_remaining_s": locked_until - now,
        }

    challenge = next((c for c in items if c.get("id") == challenge_id), None)
    if not challenge:
        return {"ok": False, "error": "unknown challenge id"}

    given_norm = _normalize(given_answer)
    expected_hash = challenge.get("answer_hash", "")
    matched = _hash_answer(given_norm) == expected_hash

    # Update state.
    history = list(state.get("challenge_history") or [])
    history.append({
        "ts": now,
        "iso": datetime.now().isoformat(timespec="seconds"),
        "challenge_id": challenge_id,
        "matched": matched,
    })
    # Trim history to last 100 entries.
    state["challenge_history"] = history[-100:]

    if matched:
        state["consecutive_failures"] = 0
        state["locked_until_ts"] = 0.0
        state["last_verified_ts"] = now
        state["last_source"] = "challenge"
    else:
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        if state["consecutive_failures"] >= _LOCKOUT_AFTER_FAILURES:
            state["locked_until_ts"] = now + _LOCKOUT_DURATION_S

    _save_state(state)

    attempts_remaining = max(0, _LOCKOUT_AFTER_FAILURES - int(state["consecutive_failures"]))
    return {
        "ok": True,
        "matched": matched,
        "attempts_remaining": attempts_remaining,
        "locked_out": locked_until > now,
    }


def record_verification_state(verified: bool, source: str) -> None:
    """Update verification state from external success (e.g., Layer 1
    silent speaker confidence above threshold).
    """
    state = _load_state()
    now = time.time()
    if verified:
        state["last_verified_ts"] = now
        state["last_source"] = source
        state["consecutive_failures"] = 0
    _save_state(state)


def get_verification_freshness() -> dict[str, Any]:
    """Return how stale the current verification is + whether sensitive
    actions should re-verify.

    Returns: {'last_verified_ts', 'last_verified_iso', 'minutes_since',
              'requires_reverify_for_sensitive', 'requires_reverify_for_low'}
    """
    state = _load_state()
    last = float(state.get("last_verified_ts") or 0.0)
    now = time.time()
    elapsed_s = (now - last) if last > 0 else float("inf")
    minutes_since = elapsed_s / 60.0 if last > 0 else -1.0
    return {
        "ok": True,
        "last_verified_ts": last,
        "last_verified_iso": (
            datetime.fromtimestamp(last).isoformat(timespec="seconds") if last > 0 else ""
        ),
        "last_source": state.get("last_source", ""),
        "minutes_since": minutes_since if last > 0 else -1.0,
        "requires_reverify_for_sensitive": elapsed_s > _STALE_THRESHOLD_S,
        "requires_reverify_for_low": elapsed_s > _STALE_THRESHOLD_S * 10,  # rarely
        "locked_out": float(state.get("locked_until_ts") or 0.0) > now,
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
    }


def add_challenge(
    question: str,
    answer: str,
    category: str = "general",
    notes: str = "",
    challenge_id: Optional[str] = None,
) -> dict[str, Any]:
    """Add a new challenge to the registry.

    challenge_id: if None, derives from the question (slugified). Must be
    unique. Returns {'ok', 'id'} on success or {'ok': False, 'error': ...}.
    """
    items = _load_challenges()
    if challenge_id is None:
        slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
        challenge_id = slug[:48] if slug else f"c-{int(time.time())}"
    if any(c.get("id") == challenge_id for c in items):
        return {"ok": False, "error": f"challenge id already exists: {challenge_id}"}
    items.append({
        "id": challenge_id,
        "question": str(question or "").strip(),
        "answer_hash": _hash_answer(_normalize(answer)),
        "category": str(category or "general").strip(),
        "added_ts": time.time(),
        "notes": str(notes or "").strip(),
    })
    _save_challenges(items)
    return {"ok": True, "id": challenge_id}
