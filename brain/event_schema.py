"""brain/event_schema.py — Schema registry for events (architecture #18).

Today's signal_bus has ~20 event names declared as plain string
constants. As features land that emit and consume events, naming
collisions and silent payload drift become real risks.

This module is the CENTRAL CATALOG. Every event type is declared once
with:
- Stable name (string id)
- Typed payload dataclass
- Emitter category (who's expected to emit)
- Description (what does the event mean)

Consumers reference EventSchema entries instead of magic strings.
Producers can validate payloads against the schema. New event types
require explicit declaration in this module — no inventing names ad-hoc.

Today: scaffold + cataloging of EXISTING signal_bus events. Future
features add their event types here before emitting.

The schema is read-only after registration. Tests assert that emitted
event payloads conform to their declared shape (future #22 test
scaffold work).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


EmitterCategory = Literal[
    "system",      # OS / device-level (clipboard, window changes, etc)
    "perception",  # face / expression / gaze
    "voice",       # wake-word, STT, voice events
    "subsystem",   # health, sleep_mode, dual_brain
    "user",        # user-driven (turn started, command issued)
    "ava",         # Ava-driven (action taken, decision made)
    "test",        # test-only events
]


@dataclass
class EventSchema:
    name: str
    description: str
    emitter_category: EmitterCategory
    payload_keys: dict[str, str] = field(default_factory=dict)  # key_name -> type description
    optional_keys: dict[str, str] = field(default_factory=dict)
    notes: str = ""


# ── Catalog of declared events ────────────────────────────────────────────
# Add new entries here BEFORE emitting an event of that name elsewhere.

EVENTS: dict[str, EventSchema] = {}


def _declare(schema: EventSchema) -> None:
    if schema.name in EVENTS:
        # Don't crash on re-import; just skip.
        return
    EVENTS[schema.name] = schema


# ── Existing signal_bus events ────────────────────────────────────────────


_declare(EventSchema(
    name="clipboard_changed",
    description="The system clipboard text content changed.",
    emitter_category="system",
    payload_keys={"text": "str", "size_bytes": "int"},
))

_declare(EventSchema(
    name="face_appeared",
    description="A recognizable or unknown face appeared in the camera view.",
    emitter_category="perception",
    payload_keys={"person_id": "str", "confidence": "float"},
    optional_keys={"is_recognized": "bool"},
))

_declare(EventSchema(
    name="face_lost",
    description="The previously visible face is no longer detected.",
    emitter_category="perception",
    payload_keys={"person_id": "str"},
))

_declare(EventSchema(
    name="face_changed",
    description="The active recognized face changed identity (e.g., Zeke -> Shonda).",
    emitter_category="perception",
    payload_keys={"old_person_id": "str", "new_person_id": "str"},
))

_declare(EventSchema(
    name="expression_changed",
    description="Facial expression weights shifted significantly.",
    emitter_category="perception",
    payload_keys={"person_id": "str", "weights": "dict[str, float]"},
))

_declare(EventSchema(
    name="attention_changed",
    description="Gaze attention state shifted (focused / distracted / away).",
    emitter_category="perception",
    payload_keys={"state": "str"},
))

_declare(EventSchema(
    name="app_opened",
    description="A desktop application window appeared.",
    emitter_category="system",
    payload_keys={"app_name": "str", "process_name": "str"},
    optional_keys={"window_title": "str", "pid": "int"},
))

_declare(EventSchema(
    name="app_closed",
    description="A desktop application window disappeared.",
    emitter_category="system",
    payload_keys={"app_name": "str", "process_name": "str"},
))

_declare(EventSchema(
    name="window_changed",
    description="The currently focused (foreground) window changed.",
    emitter_category="system",
    payload_keys={"window_title": "str", "process_name": "str"},
))

_declare(EventSchema(
    name="screen_idle",
    description="The screen has been idle (no input) past the threshold.",
    emitter_category="system",
    payload_keys={"idle_seconds": "float"},
))

_declare(EventSchema(
    name="screen_active",
    description="Input resumed after a period of idle.",
    emitter_category="system",
    payload_keys={},
))

_declare(EventSchema(
    name="app_installed",
    description="A new desktop application was discovered (likely freshly installed).",
    emitter_category="system",
    payload_keys={"app_name": "str", "exe_path": "str"},
))

_declare(EventSchema(
    name="file_created",
    description="A file was created in a watched directory.",
    emitter_category="system",
    payload_keys={"path": "str"},
))

_declare(EventSchema(
    name="voice_detected",
    description="Silero VAD detected speech start.",
    emitter_category="voice",
    payload_keys={},
))

_declare(EventSchema(
    name="clap_detected",
    description="Double-clap wake signal fired.",
    emitter_category="voice",
    payload_keys={"intensity": "float"},
))

_declare(EventSchema(
    name="reminder_due",
    description="A scheduled reminder reached its trigger time.",
    emitter_category="subsystem",
    payload_keys={"reminder_id": "str", "what": "str"},
    optional_keys={"source_user": "str"},
))

_declare(EventSchema(
    name="battery_low",
    description="Battery dropped below a configured threshold.",
    emitter_category="system",
    payload_keys={"percent": "int"},
))

_declare(EventSchema(
    name="network_changed",
    description="Internet connectivity state changed.",
    emitter_category="system",
    payload_keys={"online": "bool"},
))

_declare(EventSchema(
    name="usb_connected",
    description="A new USB device was connected.",
    emitter_category="system",
    payload_keys={"device_name": "str"},
))

_declare(EventSchema(
    name="subsystem_degraded",
    description="A subsystem reported degraded health (camera offline, STT failed init, etc).",
    emitter_category="subsystem",
    payload_keys={"subsystem": "str", "reason": "str"},
))


# ── New events for shipped-this-session features ─────────────────────────


_declare(EventSchema(
    name="action_verified",
    description="Post-action verifier confirmed the side-effect happened (A1).",
    emitter_category="ava",
    payload_keys={"action_type": "str", "target": "str", "verified": "bool"},
    notes="Emitted after open/close/type to surface success/failure to listeners.",
))

_declare(EventSchema(
    name="action_failed_silently",
    description="Action claimed success but verifier disagrees (A1).",
    emitter_category="ava",
    payload_keys={"action_type": "str", "target": "str", "explanation": "str"},
    notes="The Edge .lnk silently-fails class. Emitted by post_action_verifier.",
))

_declare(EventSchema(
    name="lifecycle_transition",
    description="High-level Ava lifecycle state changed (architecture #8).",
    emitter_category="ava",
    payload_keys={"from_state": "str", "to_state": "str"},
    optional_keys={"reason": "str"},
))

_declare(EventSchema(
    name="claim_recorded",
    description="A new claim was added to the provenance graph (architecture #5).",
    emitter_category="ava",
    payload_keys={"claim_id": "str", "source_kind": "str"},
    optional_keys={"person_id": "str"},
))

_declare(EventSchema(
    name="skill_auto_created",
    description="A new procedural skill was auto-stored after a successful compound action.",
    emitter_category="ava",
    payload_keys={"slug": "str", "actions_count": "int"},
))

_declare(EventSchema(
    name="claude_code_recognized",
    description="Claude Code identity detected; greeting due (or already given).",
    emitter_category="ava",
    payload_keys={"is_first_session": "bool"},
))


# ── API ───────────────────────────────────────────────────────────────────


def get(event_name: str) -> EventSchema | None:
    return EVENTS.get(event_name)


def all_events() -> list[EventSchema]:
    return list(EVENTS.values())


def by_category(category: EmitterCategory) -> list[EventSchema]:
    return [s for s in EVENTS.values() if s.emitter_category == category]


def is_declared(event_name: str) -> bool:
    return event_name in EVENTS


def validate_payload(event_name: str, payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """Lightweight payload validation against the declared schema.

    Returns (valid, missing_keys). Missing required keys list non-empty
    means invalid; type checking is NOT done (the payload_keys map is
    descriptive, not enforced — keeps the registry lightweight).

    Use case: in tests, assert that emitted events match their declared
    schema. In production, optional warning when payload diverges.
    """
    schema = EVENTS.get(event_name)
    if schema is None:
        return False, [f"event '{event_name}' not declared in schema registry"]
    missing = [k for k in schema.payload_keys if k not in payload]
    return len(missing) == 0, missing


def summary() -> dict[str, Any]:
    by_cat: dict[str, int] = {}
    for s in EVENTS.values():
        by_cat[s.emitter_category] = by_cat.get(s.emitter_category, 0) + 1
    return {
        "total_events": len(EVENTS),
        "by_category": by_cat,
    }
