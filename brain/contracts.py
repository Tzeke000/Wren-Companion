"""brain/contracts.py — Typed Protocol contracts (architecture #16).

Most subsystems today are duck-typed. "The thing in `g['_ava_memory']`
is whatever class it is, callers reach in and hope." Typed Protocols
make the expected shape explicit:

- New implementation satisfies the Protocol → drop in, runs
- Static check via `isinstance(obj, Protocol)` (PEP 544 runtime
  Protocols) catches contract violations early
- Documentation lives WITH the contract (docstrings + type hints)
  instead of scattered across consumer call sites

Today: define the Protocols. No existing classes are modified.
Future code references the contracts. Implementations gradually
adopt Protocol inheritance as they're touched.

Protocols defined:

- MemoryStore: anything that stores + retrieves memories (mem0 wrapper,
  concept_graph wrapper, FTS5 wrapper)
- ActionHandler: anything that executes a user-requested action
  (open_app, close_app, type_text, etc)
- IntentClassifier: anything that maps text → intent labels
  (regex voice command router, LLM action-tag classifier, skills
  recall, future LLM-based classifiers)
- Verifier: anything that confirms a side-effect happened
  (post_action_verifier, future visual / audio / network verifiers)
- PersonProfile: anything that exposes per-person info
  (legacy profiles/*.json, person_registry.PersonView)
- SkillProvider: anything that produces / recalls / persists skills

Usage:

    from brain.contracts import MemoryStore

    def some_function(store: MemoryStore) -> ...:
        results = store.search("query", limit=4)
        ...

    # Adoption:
    class MyMemoryImpl:
        def search(self, query: str, *, limit: int = 4) -> list[dict]:
            ...
        def append(self, role: str, content: str, **meta) -> None:
            ...
        def is_available(self) -> bool:
            ...
    isinstance(MyMemoryImpl(), MemoryStore)  # True — duck-conforms

These Protocols are intentionally LOOSE — only the methods we care
about are required. New consumers learn what surface they need; new
implementations only have to provide that surface.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, runtime_checkable


# ── MemoryStore ───────────────────────────────────────────────────────────


@runtime_checkable
class MemoryStore(Protocol):
    """Anything that stores + retrieves memory entries.

    Examples: mem0 vector store, FTS5 wrapper, concept_graph,
    chat_history.jsonl reader.
    """

    def search(self, query: str, *, limit: int = 4, **kwargs: Any) -> list[dict[str, Any]]:
        """Search memory for entries matching the query.

        Returns a list of result dicts. Each dict at minimum has a
        `content` key with the text. Other keys vary by store
        (`role`, `ts`, `score`, `source`, etc).
        """
        ...

    def is_available(self) -> bool:
        """True if this store is currently usable (loaded, connected,
        not in degraded state)."""
        ...


# ── ActionHandler ─────────────────────────────────────────────────────────


@runtime_checkable
class ActionHandler(Protocol):
    """Anything that executes a user-requested action.

    The `execute` method returns (ok, msg) where `ok` is whether the
    action succeeded and `msg` is what Ava would say about it.
    Implementations should run their action through the post-action
    verifier (A1) where applicable.
    """

    def execute(self, params: dict[str, Any], g: dict[str, Any]) -> tuple[bool, str]:
        """Execute the action. Returns (ok, spoken_message)."""
        ...

    @property
    def name(self) -> str:
        """Stable name for telemetry / safety-layer logs."""
        ...


# ── IntentClassifier ──────────────────────────────────────────────────────


@runtime_checkable
class IntentClassifier(Protocol):
    """Anything that maps user text -> structured intent.

    Examples: regex voice command router, LLM action-tag classifier,
    skills-recall via Jaccard match, the future Decision Router
    handlers.

    Intent representation is loose — implementations return whatever
    makes sense (string label, list of (action, target) tuples,
    None for "no opinion").
    """

    def classify(self, text: str, g: dict[str, Any]) -> Optional[Any]:
        """Classify the user's text. Returns implementation-specific
        intent representation, or None for "I don't claim this input."
        """
        ...

    @property
    def name(self) -> str:
        ...


# ── Verifier ──────────────────────────────────────────────────────────────


@runtime_checkable
class Verifier(Protocol):
    """Anything that confirms a side-effect happened.

    Examples: post_action_verifier (Win32 enum + foreground check),
    clipboard_verifier (verify_after_type), future visual_verifier
    (screenshot OCR), audio_verifier, network_verifier.

    The `verify` method returns a structured result with at minimum
    `verified: bool` and `explanation: str`.
    """

    def verify(self, action_type: str, target: str, **context: Any) -> dict[str, Any]:
        """Verify the side-effect of `action_type` on `target`.

        Returns at minimum {"verified": bool, "explanation": str}.
        """
        ...


# ── PersonProfile ─────────────────────────────────────────────────────────


@runtime_checkable
class PersonProfile(Protocol):
    """Anything that exposes per-person info.

    Examples: legacy profiles/*.json (loaded via avaagent.load_profile_by_id),
    PersonView (from person_registry), future per-channel profiles.
    """

    @property
    def person_id(self) -> str:
        ...

    @property
    def trust_level(self) -> str:
        """One of: 'unknown', 'low', 'medium', 'high'."""
        ...


# ── SkillProvider ─────────────────────────────────────────────────────────


@runtime_checkable
class SkillProvider(Protocol):
    """Anything that produces / recalls / persists procedural skills.

    Examples: brain/skills.py recall + auto_create_or_update, future
    skill subsystems, agentskills.io-format imports.
    """

    def recall(self, base_dir: Any, user_input: str) -> Optional[Any]:
        """Find best-matching skill for input. Returns (skill_dict, score)
        or None."""
        ...

    def auto_create_or_update(
        self,
        base_dir: Any,
        user_input: str,
        actions: list[Any],
    ) -> Optional[str]:
        """Persist a skill from a successful action sequence. Returns
        slug or None."""
        ...


# ── EventEmitter ──────────────────────────────────────────────────────────


@runtime_checkable
class EventEmitter(Protocol):
    """Anything that emits events into the signal bus.

    Examples: signal_bus, future component-specific emitters.
    Schema validation happens via brain/event_schema.py.
    """

    def emit(self, event_name: str, payload: dict[str, Any]) -> None:
        ...


# ── Helper: contract-conformance check for tests ─────────────────────────


def assert_conforms(obj: Any, protocol_class: type) -> tuple[bool, str]:
    """Test helper: asserts `obj` satisfies the runtime Protocol.

    Returns (ok, missing_methods_or_empty). Used by future #22 test
    scaffold work to verify new implementations conform before they
    ship.
    """
    if isinstance(obj, protocol_class):
        return True, ""
    # Build a list of missing or non-callable members
    missing = []
    for member in dir(protocol_class):
        if member.startswith("_"):
            continue
        if not hasattr(obj, member):
            missing.append(member)
    if not missing:
        return True, ""
    return False, f"missing: {', '.join(missing)}"
