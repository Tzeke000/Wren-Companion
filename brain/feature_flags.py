"""brain/feature_flags.py — Feature flags for staged rollout (architecture #21).

New behavior lives behind a flag. Off-by-default → testing → on-by-default
→ flag removed. Lets us land big changes without all-or-nothing risk.

Flags resolve in priority order:
1. env var `AVA_FEATURE_<UPPERCASE_NAME>` — hard override (set/unset
   per session). Useful for dev / CI / smoke tests.
2. state/feature_flags.json — persistent per-deployment config Zeke
   can edit.
3. Default declared in this module (the safe value).

API:

    from brain.feature_flags import flag_enabled, flag_value

    if flag_enabled("decision_router"):
        # Use the new dispatcher
        ...
    else:
        # Use the legacy run_ava sequence
        ...

    timeout = flag_value("introspection_timeout_s", default=14.0)

For staged rollout of a new feature:
1. Add the flag to FLAGS dict here with default=False
2. Wrap the new code path in `if flag_enabled("..."): ...`
3. Test by setting AVA_FEATURE_<NAME>=1
4. When stable, change the default to True in this module
5. After a few sessions, remove the flag entirely (delete the
   conditional, the legacy code path, and the FLAGS entry)
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FlagDefinition:
    name: str
    default: Any
    description: str
    type_hint: str = "bool"  # "bool", "int", "float", "str"
    notes: str = ""


# ── Flag catalog ──────────────────────────────────────────────────────────


FLAGS: dict[str, FlagDefinition] = {
    # Streaming TTS — already shipped behind AVA_STREAMING_ENABLED env var.
    # Kept here so it shows in the catalog.
    "streaming_tts": FlagDefinition(
        name="streaming_tts",
        default=True,
        description="Sentence-by-sentence TTS streaming during deep-path replies.",
        type_hint="bool",
        notes="Env override: AVA_STREAMING_ENABLED=0 disables.",
    ),
    # Decision Router (architecture #2) — currently scaffolded; default off
    # until handler extraction is complete and runtime-tested.
    "decision_router": FlagDefinition(
        name="decision_router",
        default=False,
        description="Use the new DecisionRouter handler chain instead of run_ava's legacy branching.",
        type_hint="bool",
        notes="Off until W2-1 through W2-7 migrations are runtime-tested.",
    ),
    # Skill recall — currently shipped (action_tag_router.route consults it).
    "skill_recall": FlagDefinition(
        name="skill_recall",
        default=True,
        description="Pre-classifier skill recall in action_tag_router.",
        type_hint="bool",
    ),
    # Action verifier — A1 self-awareness of failure.
    "action_verifier": FlagDefinition(
        name="action_verifier",
        default=True,
        description="Wrap open/close/type actions through post_action_verifier.",
        type_hint="bool",
    ),
    # Per-turn telemetry recording.
    "telemetry": FlagDefinition(
        name="telemetry",
        default=True,
        description="Record per-turn pipeline-stage timing.",
        type_hint="bool",
    ),
    # FTS5 fast-path memory in build_prompt.
    "fts5_memory": FlagDefinition(
        name="fts5_memory",
        default=True,
        description="Use FTS5 fast-path before mem0 vector search.",
        type_hint="bool",
    ),
    # Subagent delegation for knowledge queries.
    "subagent": FlagDefinition(
        name="subagent",
        default=True,
        description="Delegate knowledge queries to background subagent for ack-then-deliver.",
        type_hint="bool",
    ),
    # Claude Code recognition + greeting.
    "claude_code_greeting": FlagDefinition(
        name="claude_code_greeting",
        default=True,
        description="Recognize Claude Code by as_user; greet on first interaction or after gap.",
        type_hint="bool",
    ),
    # Constraint honesty (B8).
    "constraint_honesty": FlagDefinition(
        name="constraint_honesty",
        default=True,
        description="Honest answers to 'can you see/access X' questions.",
        type_hint="bool",
    ),
    # Tunables (numeric flags) — useful for staged rollout of cooldowns / thresholds.
    "introspection_timeout_s": FlagDefinition(
        name="introspection_timeout_s",
        default=14.0,
        description="Wallclock timeout for introspection LLM compose. Falls to hand-composed digest beyond this.",
        type_hint="float",
    ),
    "subagent_timeout_s": FlagDefinition(
        name="subagent_timeout_s",
        default=120.0,
        description="Wallclock timeout for subagent background LLM call.",
        type_hint="float",
    ),
}


_lock = threading.RLock()
_runtime_overrides: dict[str, Any] = {}
_persistent_loaded = False
_persistent_cache: dict[str, Any] = {}
_state_path: Path | None = None


def configure(base_dir: Path) -> None:
    """Called once at startup."""
    global _state_path, _persistent_loaded, _persistent_cache
    with _lock:
        _state_path = base_dir / "state" / "feature_flags.json"
        _persistent_loaded = False
        _persistent_cache = {}
        _load_persistent_locked()


def _load_persistent_locked() -> None:
    global _persistent_loaded, _persistent_cache
    if _state_path is None:
        return
    if not _state_path.exists():
        _persistent_loaded = True
        _persistent_cache = {}
        return
    try:
        data = json.loads(_state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _persistent_cache = dict(data)
    except Exception as e:
        print(f"[feature_flags] persistent load error: {e!r}")
        _persistent_cache = {}
    _persistent_loaded = True


def _coerce(value: Any, type_hint: str) -> Any:
    if type_hint == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if type_hint == "int":
        try:
            return int(value)
        except Exception:
            return 0
    if type_hint == "float":
        try:
            return float(value)
        except Exception:
            return 0.0
    return str(value)


def flag_value(name: str, *, default: Any = None) -> Any:
    """Resolve a flag's current value.

    Priority: env var → state/feature_flags.json → declared default.

    If `name` isn't in FLAGS, returns `default` (so callers can use
    ad-hoc flags too).
    """
    declared = FLAGS.get(name)

    # 1. env var
    env_key = f"AVA_FEATURE_{name.upper()}"
    env_val = os.environ.get(env_key)
    if env_val is not None:
        if declared is not None:
            return _coerce(env_val, declared.type_hint)
        return env_val

    # 2. persistent file
    with _lock:
        if not _persistent_loaded:
            _load_persistent_locked()
        if name in _persistent_cache:
            v = _persistent_cache[name]
            if declared is not None:
                return _coerce(v, declared.type_hint)
            return v

    # 3. declared default
    if declared is not None:
        return declared.default
    return default


def flag_enabled(name: str) -> bool:
    """Convenience wrapper for boolean flags."""
    return bool(flag_value(name, default=False))


def all_resolved() -> dict[str, Any]:
    """Resolve every declared flag to its current value. Useful for
    diagnostic surfaces."""
    return {name: flag_value(name) for name in FLAGS}


def set_runtime_override(name: str, value: Any) -> None:
    """Set a process-local override (not persisted). Intended for tests
    and short-lived experiments."""
    # Currently we override via env var; this writes the env var.
    env_key = f"AVA_FEATURE_{name.upper()}"
    os.environ[env_key] = str(value)


def clear_runtime_override(name: str) -> None:
    env_key = f"AVA_FEATURE_{name.upper()}"
    os.environ.pop(env_key, None)


def list_flags() -> list[FlagDefinition]:
    return list(FLAGS.values())
