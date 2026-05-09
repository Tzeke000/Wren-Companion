"""brain/plugin_manifest.py — Versioned plugin manifests (architecture #19).

Every skill / tool / plugin / extension declares a manifest:
- name (stable identifier)
- version (semver-like string)
- description
- depends_on (list of other plugins by name)
- hooks (which lifecycle hooks it subscribes to)
- capabilities_required (what surface-area access it needs)
- events_emitted (events it publishes — should appear in event_schema)
- events_consumed (events it listens for — should appear in event_schema)

Why this matters: as plugins accumulate, dependency graphs and
incompatibilities become real. Plugin A that depends on Plugin B's
output silently breaks if B is removed or renamed. Manifest gives us
explicit dependency declarations that the loader can validate.

Today: scaffold + registration mechanism. Existing tools/voice
commands aren't manifested yet — they keep their inline registration.
Future plugins (especially the auto-learned skills, the world-model
integrations, etc) declare manifests for safer co-existence.

Usage:

    from brain.plugin_manifest import PluginManifest, register_plugin

    register_plugin(PluginManifest(
        name="weather_tool",
        version="1.0.0",
        description="Fetches weather via wttr.in",
        capabilities_required=["network"],
        events_emitted=[],
    ))

The registry can answer:
- list_plugins() — what's loaded
- depends_on(plugin) — direct dependencies
- dependents_of(plugin) — what would break if this plugin removed
- validate_all() — find missing dependencies, version conflicts

Manifest entries are optional — plugins without manifests still work
(legacy plugins). Manifest gives the structured visibility but isn't
gating anything by default.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PluginManifest:
    name: str
    version: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)  # hook names from brain/hooks.py
    capabilities_required: list[str] = field(default_factory=list)
    events_emitted: list[str] = field(default_factory=list)
    events_consumed: list[str] = field(default_factory=list)
    notes: str = ""


_lock = threading.RLock()
_registered: dict[str, PluginManifest] = {}


def register_plugin(manifest: PluginManifest) -> None:
    """Register a plugin's manifest. Idempotent on (name, version)."""
    if not manifest.name:
        print("[plugin_manifest] register: empty name, skipping")
        return
    with _lock:
        existing = _registered.get(manifest.name)
        if existing is not None and existing.version == manifest.version:
            return  # already registered at same version
        _registered[manifest.name] = manifest
    print(f"[plugin_manifest] registered {manifest.name!r} v{manifest.version}")


def get_plugin(name: str) -> PluginManifest | None:
    with _lock:
        return _registered.get(name)


def list_plugins() -> list[PluginManifest]:
    with _lock:
        return list(_registered.values())


def depends_on(name: str) -> list[str]:
    """Direct dependencies of `name`."""
    p = get_plugin(name)
    if p is None:
        return []
    return list(p.depends_on)


def dependents_of(name: str) -> list[str]:
    """All plugins that depend on `name`. What would break if removed?"""
    out = []
    for p in list_plugins():
        if name in (p.depends_on or []):
            out.append(p.name)
    return out


def validate_all() -> dict[str, list[str]]:
    """Validate every registered plugin against the others.

    Returns a dict of issues:
      missing_deps: dependencies that aren't registered
      missing_event_schemas: events_emitted not in event_schema registry
      missing_hooks: hooks that aren't in hooks.KNOWN_HOOKS
    """
    issues: dict[str, list[str]] = {
        "missing_deps": [],
        "missing_event_schemas": [],
        "missing_hooks": [],
    }
    plugins = list_plugins()
    plugin_names = {p.name for p in plugins}

    # Try to import schema + known hooks
    try:
        from brain.event_schema import EVENTS as _EVENTS
        known_events = set(_EVENTS.keys())
    except Exception:
        known_events = set()
    try:
        from brain.hooks import KNOWN_HOOKS as _KNOWN_HOOKS
        known_hooks = set(_KNOWN_HOOKS)
    except Exception:
        known_hooks = set()

    for p in plugins:
        for dep in (p.depends_on or []):
            if dep not in plugin_names:
                issues["missing_deps"].append(f"{p.name}@{p.version} depends on {dep!r} (not registered)")
        for ev in (p.events_emitted or []) + (p.events_consumed or []):
            if known_events and ev not in known_events:
                issues["missing_event_schemas"].append(f"{p.name}@{p.version} references undeclared event {ev!r}")
        for hk in (p.hooks or []):
            if known_hooks and hk not in known_hooks:
                issues["missing_hooks"].append(f"{p.name}@{p.version} subscribes to non-standard hook {hk!r}")

    return issues


def summary() -> dict[str, Any]:
    plugins = list_plugins()
    return {
        "total_plugins": len(plugins),
        "names": [p.name for p in plugins],
        "issues": validate_all(),
    }


# ── Self-register the architecture modules shipped today ─────────────────
# These don't NEED manifests (they're internal), but registering them
# demonstrates the manifest system in action and shows them in the
# plugin catalog for visibility.

def _bootstrap_self_register() -> None:
    """Register manifests for the architecture modules shipped 2026-05-06."""
    self_manifests = [
        PluginManifest(
            name="post_action_verifier",
            version="0.1.0",
            description="Self-awareness of failure (A1) — verifies side-effects of actions.",
        ),
        PluginManifest(
            name="action_tag_router",
            version="0.2.0",
            description="LLM action-tag classifier fallback for voice commands.",
            depends_on=["post_action_verifier"],
        ),
        PluginManifest(
            name="claude_code_recognition",
            version="0.1.0",
            description="Recognizes Claude Code identity, greets on session start, shifts register.",
        ),
        PluginManifest(
            name="constraints_honesty",
            version="0.1.0",
            description="B8 — honest answers about Ava's constraints.",
        ),
        PluginManifest(
            name="introspection",
            version="0.2.0",
            description="Authentic 'how are you feeling' composer.",
        ),
        PluginManifest(
            name="scheduler",
            version="0.1.0",
            description="Reminder + scheduled-action watcher.",
            events_emitted=["reminder_due"],
        ),
        PluginManifest(
            name="subagent",
            version="0.1.0",
            description="Background delegation for knowledge queries.",
        ),
        PluginManifest(
            name="claude_code_subagent",
            version="0.1.0",
            description="Spawns Claude CLI for build-software intents.",
        ),
        PluginManifest(
            name="skills",
            version="0.1.0",
            description="Procedural skill memory (Hermes pattern).",
            events_emitted=["skill_auto_created"],
        ),
        PluginManifest(
            name="fts_memory",
            version="0.1.0",
            description="SQLite FTS5 fast-path memory.",
        ),
        PluginManifest(
            name="weather",
            version="0.1.0",
            description="wttr.in weather lookup.",
            capabilities_required=["network"],
        ),
        PluginManifest(
            name="telemetry",
            version="0.1.0",
            description="Per-turn pipeline-stage timing.",
        ),
        PluginManifest(
            name="lifecycle",
            version="0.1.0",
            description="High-level operating state machine.",
            events_emitted=["lifecycle_transition"],
        ),
        PluginManifest(
            name="provenance",
            version="0.1.0",
            description="Belief sourcing graph.",
            events_emitted=["claim_recorded"],
        ),
        PluginManifest(
            name="person_registry",
            version="0.1.0",
            description="Unified per-person facade.",
        ),
        PluginManifest(
            name="memory_hierarchy",
            version="0.1.0",
            description="L1-L5 memory layer facade.",
            depends_on=["fts_memory", "skills"],
        ),
        PluginManifest(
            name="safety_layer",
            version="0.1.0",
            description="Trust + impact + values gate (skeleton).",
        ),
        PluginManifest(
            name="event_schema",
            version="0.1.0",
            description="Central event catalog.",
        ),
        PluginManifest(
            name="hooks",
            version="0.1.0",
            description="Lifecycle hook decorator system.",
        ),
        PluginManifest(
            name="feature_flags",
            version="0.1.0",
            description="Staged-rollout flag resolution.",
        ),
        PluginManifest(
            name="external_service",
            version="0.1.0",
            description="Retry / backoff / circuit-breaker for external services.",
        ),
        PluginManifest(
            name="contracts",
            version="0.1.0",
            description="Typed Protocols for extension points.",
        ),
        PluginManifest(
            name="state_classification",
            version="0.1.0",
            description="state/ file lifecycle categorization.",
        ),
        PluginManifest(
            name="decision_router",
            version="0.0.1",
            description="Decision Router scaffolding (Wave 2 not migrated yet).",
            notes="Migration W2-1 through W2-7 still pending.",
        ),
    ]
    for m in self_manifests:
        register_plugin(m)


_BOOTSTRAPPED = False


def configure(base_dir: Any = None) -> None:
    """Called once at startup. Self-registers the architecture modules."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _bootstrap_self_register()
    _BOOTSTRAPPED = True
