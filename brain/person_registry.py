"""brain/person_registry.py — Unified per-person facade.

Architecture sweep #6 from designs/ava-roadmap-personhood.md.

Today, per-person data is scattered across:
- profiles/<person_id>.json (main profile via avaagent.load_profile_by_id)
- state/trust_scores.json (per-person trust accumulator)
- state/zeke_mind_model.json (Zeke-specific mind model)
- state/expression_baseline_zeke.json (calibration)
- state/chat_history.jsonl (interactions, filterable by person_id)
- ad-hoc keys in g (e.g. _last_claude_code_seen_ts,
  _claude_code_interaction_count)

Every feature that's per-person currently has to know all of these
storage shapes. That's coupling that won't scale to ~50 features.

This module is the FACADE — one API for "get me everything about
person X." Today it reads from the existing sources (no migration).
Future code writes through this facade so the underlying storage
can evolve without breaking callers.

API:
  get_person(person_id) → PersonView (dataclass with all the per-person stuff)
  record_seen(person_id, source) — updates last_seen + interaction_count
  record_told(person_id, topic, ts=None) — theory of mind: what we've told them
  has_been_told(person_id, topic) → bool
  set_preference(person_id, key, value) — store a learned preference
  get_preferences(person_id) → dict
  add_to_shared_lexicon(person_id, term, meaning)
  get_shared_lexicon(person_id) → dict[term, meaning]
  list_known_persons() → list[person_id]

This is the seam B5 (theory of mind), B6 (per-person preference
learning), C11 (shared lexicon), and C12 (discretion graph) will plug
into.

Today: facade only, no behavior change. Existing code paths still
work. Future code can adopt the facade incrementally.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Storage paths ─────────────────────────────────────────────────────────


def _registry_dir(base_dir: Path) -> Path:
    p = base_dir / "state" / "person_registry"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _person_extras_path(base_dir: Path, person_id: str) -> Path:
    """Per-person extras file — preferences, shared lexicon, told-about
    state. Things the legacy profile doesn't track."""
    safe = "".join(c for c in person_id if c.isalnum() or c in "-_") or "unknown"
    return _registry_dir(base_dir) / f"{safe}.json"


# ── Data shape ────────────────────────────────────────────────────────────


@dataclass
class PersonView:
    """Unified view of everything we know about a person.

    This is a SNAPSHOT — modifications happen through registry methods,
    not by mutating this object.
    """

    person_id: str
    profile: dict[str, Any] = field(default_factory=dict)  # legacy profiles/*.json content
    trust_level: str = "unknown"  # "unknown" | "low" | "medium" | "high"
    last_seen_ts: float = 0.0
    interaction_count: int = 0
    # Learned per-person things (lives in state/person_registry/<id>.json):
    preferences: dict[str, Any] = field(default_factory=dict)
    shared_lexicon: dict[str, str] = field(default_factory=dict)
    told_about: dict[str, float] = field(default_factory=dict)  # topic → ts when last surfaced
    notes: list[str] = field(default_factory=list)


# ── Registry singleton ────────────────────────────────────────────────────


class PersonRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._base_dir: Path | None = None
        self._cache: dict[str, dict[str, Any]] = {}  # person_id → extras dict

    def configure(self, base_dir: Path) -> None:
        with self._lock:
            self._base_dir = base_dir

    def _ensure_configured(self, g: dict[str, Any] | None = None) -> Path:
        if self._base_dir is not None:
            return self._base_dir
        if g is not None:
            base = Path(g.get("BASE_DIR") or ".")
            self._base_dir = base
            return base
        return Path.cwd()

    # ── Internal storage helpers ─────────────────────────────────────────

    def _load_extras(self, person_id: str) -> dict[str, Any]:
        with self._lock:
            cached = self._cache.get(person_id)
            if cached is not None:
                return dict(cached)  # defensive copy
            base = self._ensure_configured()
            path = _person_extras_path(base, person_id)
            if not path.exists():
                empty: dict[str, Any] = {
                    "person_id": person_id,
                    "preferences": {},
                    "shared_lexicon": {},
                    "told_about": {},
                    "notes": [],
                    "last_seen_ts": 0.0,
                    "interaction_count": 0,
                    "created_ts": time.time(),
                }
                self._cache[person_id] = empty
                return dict(empty)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            data.setdefault("person_id", person_id)
            data.setdefault("preferences", {})
            data.setdefault("shared_lexicon", {})
            data.setdefault("told_about", {})
            data.setdefault("notes", [])
            data.setdefault("last_seen_ts", 0.0)
            data.setdefault("interaction_count", 0)
            self._cache[person_id] = data
            return dict(data)

    def _save_extras(self, person_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            base = self._ensure_configured()
            path = _person_extras_path(base, person_id)
            try:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                self._cache[person_id] = dict(data)
            except Exception as e:
                print(f"[person_registry] save error for {person_id!r}: {e!r}")

    # ── Profile resolution (reads existing profiles/*.json) ──────────────

    def _load_profile(self, person_id: str) -> dict[str, Any]:
        try:
            import avaagent
            return avaagent.load_profile_by_id(person_id) or {}
        except Exception:
            return {}

    def _resolve_trust(self, person_id: str, profile: dict[str, Any]) -> str:
        """Trust resolution: profile.trust_level if set, else heuristic by id.

        Mirrors the logic in brain/safety_layer.py. Centralizing here so
        safety + per-person features see the same trust map.
        """
        prof_trust = str(profile.get("trust_level") or "").strip().lower()
        if prof_trust in ("high", "medium", "low", "unknown"):
            return prof_trust
        defaults = {
            "zeke": "high",
            "claude_code": "high",
            "shonda": "medium",
        }
        return defaults.get(str(person_id or "").lower(), "unknown")

    # ── Public API ───────────────────────────────────────────────────────

    def get_person(self, person_id: str) -> PersonView:
        """Snapshot view of person_id. Always returns a PersonView; for
        unknown ids, returns minimal data with trust_level='unknown'."""
        person_id = str(person_id or "").strip()
        if not person_id:
            return PersonView(person_id="")
        profile = self._load_profile(person_id)
        extras = self._load_extras(person_id)
        return PersonView(
            person_id=person_id,
            profile=profile,
            trust_level=self._resolve_trust(person_id, profile),
            last_seen_ts=float(extras.get("last_seen_ts") or 0.0),
            interaction_count=int(extras.get("interaction_count") or 0),
            preferences=dict(extras.get("preferences") or {}),
            shared_lexicon=dict(extras.get("shared_lexicon") or {}),
            told_about=dict(extras.get("told_about") or {}),
            notes=list(extras.get("notes") or []),
        )

    def record_seen(self, person_id: str, *, source: str = "") -> None:
        """Update last_seen + interaction_count. Called by the session
        bootstrap (voice loop, inject_transcript, runtime_presence)."""
        person_id = str(person_id or "").strip()
        if not person_id:
            return
        extras = self._load_extras(person_id)
        extras["last_seen_ts"] = time.time()
        extras["interaction_count"] = int(extras.get("interaction_count") or 0) + 1
        if source:
            extras["last_source"] = source
        self._save_extras(person_id, extras)

    def record_told(self, person_id: str, topic: str, *, ts: float | None = None) -> None:
        """Mark that we've told person_id about `topic` at time ts.

        Used by B5 theory-of-mind features: "have I told Zeke about the
        Hermes eval?" → look up told_about[topic]."""
        person_id = str(person_id or "").strip()
        topic = str(topic or "").strip()
        if not person_id or not topic:
            return
        extras = self._load_extras(person_id)
        told = dict(extras.get("told_about") or {})
        told[topic.lower()] = float(ts or time.time())
        extras["told_about"] = told
        self._save_extras(person_id, extras)

    def has_been_told(self, person_id: str, topic: str) -> bool:
        person_id = str(person_id or "").strip()
        topic = str(topic or "").strip().lower()
        if not person_id or not topic:
            return False
        extras = self._load_extras(person_id)
        return topic in (extras.get("told_about") or {})

    def set_preference(self, person_id: str, key: str, value: Any) -> None:
        """Store a learned preference for person_id (B6).

        e.g. set_preference("zeke", "reply_length", "short")
             set_preference("zeke", "addressed_as", "Zeke")  # not "sir"
        """
        person_id = str(person_id or "").strip()
        key = str(key or "").strip()
        if not person_id or not key:
            return
        extras = self._load_extras(person_id)
        prefs = dict(extras.get("preferences") or {})
        prefs[key] = value
        extras["preferences"] = prefs
        self._save_extras(person_id, extras)

    def get_preferences(self, person_id: str) -> dict[str, Any]:
        return dict(self.get_person(person_id).preferences)

    def add_to_shared_lexicon(self, person_id: str, term: str, meaning: str) -> None:
        """Inside-jokes / private references that grow between Ava and a
        specific person (C11)."""
        person_id = str(person_id or "").strip()
        term = str(term or "").strip()
        meaning = str(meaning or "").strip()
        if not person_id or not term:
            return
        extras = self._load_extras(person_id)
        lex = dict(extras.get("shared_lexicon") or {})
        lex[term.lower()] = meaning
        extras["shared_lexicon"] = lex
        self._save_extras(person_id, extras)

    def get_shared_lexicon(self, person_id: str) -> dict[str, str]:
        return dict(self.get_person(person_id).shared_lexicon)

    def list_known_persons(self) -> list[str]:
        """All person_ids we have either an extras file OR a profile for."""
        seen: set[str] = set()
        # extras files
        try:
            base = self._ensure_configured()
            d = _registry_dir(base)
            for p in d.glob("*.json"):
                seen.add(p.stem)
        except Exception:
            pass
        # legacy profiles
        try:
            base = self._ensure_configured()
            profiles_dir = base / "profiles"
            if profiles_dir.is_dir():
                for p in profiles_dir.glob("*.json"):
                    seen.add(p.stem)
        except Exception:
            pass
        return sorted(seen)


# Process-singleton.
registry = PersonRegistry()


def configure_person_registry(base_dir: Path) -> None:
    """Called once at startup to point the registry at the project base."""
    registry.configure(base_dir)
