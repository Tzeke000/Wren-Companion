"""brain/skill_sandbox.py — Sandbox + safety validator for auto-learned skills (#20).

Per Zeke 2026-05-06: "Sandboxed auto-learned skills should NOT have
permission to do destructive things. An auto-skill can OPEN apps,
TYPE text, SWITCH windows, TAKE screenshots — read-only + creation
operations only. It MUST NOT be allowed to: delete files, close apps,
modify system state, send messages, make purchases."

This module is the SAFETY GATE between Ava observing a successful
action sequence and persisting that sequence as an auto-learned skill
she can re-fire. It is also the SAFETY GATE at execution time — even
if a skill somehow contains forbidden actions (manual edit, legacy
data, future-action-type addition), the execution wrapper refuses to
run the destructive step.

The whitelist is INTENTIONALLY narrow. New action types added later
default to FORBIDDEN — they have to be explicitly added to the
allowlist after a safety review. This is fail-closed.

Distinction from safety_layer:
- safety_layer (#7) is for first-class actions the harness performs in
  response to user requests. Trust resolution + rule registry.
- skill_sandbox (this module) is for auto-learned compound skills that
  Ava acquires by observation and re-fires later. Stricter — Ava is
  initiating these without a fresh user prompt.

API:

    from brain.skill_sandbox import (
        validate_skill_actions, sandbox_filter,
        is_action_type_allowed, is_action_type_forbidden,
        ALLOWED_ACTION_TYPES, FORBIDDEN_ACTION_TYPES,
    )

    ok, reason = validate_skill_actions([("OPEN_APP", "Notes"), ("TYPE_TEXT", "...")])
    # ok=True

    ok, reason = validate_skill_actions([("CLOSE_APP", "Edge")])
    # ok=False, reason="CLOSE_APP is forbidden in auto-learned skills (destructive)"

    safe_actions = sandbox_filter(actions)  # drops forbidden, keeps allowed
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


# Allowed action types — read/create only, per Zeke's spec.
# Each entry: action_type → human-readable description of why it's safe
ALLOWED_ACTION_TYPES: dict[str, str] = {
    "OPEN_APP": "Opens an application — additive, no destruction",
    "TYPE_TEXT": "Types text — input creation, no deletion",
    "SWITCH_WINDOW": "Changes window focus — non-destructive UI navigation",
    "TAKE_SCREENSHOT": "Reads screen — purely observational",
    "TIME": "Returns current time — read-only system query",
    "DATE": "Returns current date — read-only system query",
    "WEATHER": "Returns weather — read-only external query",
    "CONVERSATION": "Generates a verbal reply — speech, no system mutation",
    "READ_FILE": "Reads a file — non-destructive (if added later)",
    "LIST_FILES": "Lists directory contents — non-destructive (if added later)",
}

# Explicitly forbidden — these are destructive or external-effecting
# even if a skill author thinks they're harmless. The descriptions are
# the reasons surfaced to operators when a skill is rejected.
FORBIDDEN_ACTION_TYPES: dict[str, str] = {
    "CLOSE_APP": "Closing apps can lose unsaved work — destructive",
    "DELETE_FILE": "File deletion is irreversible — destructive",
    "DELETE": "Generic delete — destructive",
    "REMOVE": "Generic remove — destructive",
    "WRITE_FILE": "File write can overwrite existing data — needs explicit consent",
    "MODIFY_FILE": "File modification — destructive without backup",
    "SEND_MESSAGE": "Outbound communication — has external effects, social risk",
    "SEND_EMAIL": "Outbound email — has external effects",
    "POST_MESSAGE": "Posting to chat/social — has external effects",
    "MAKE_PURCHASE": "Financial transaction — irreversible external effect",
    "BUY": "Financial transaction — irreversible external effect",
    "EXECUTE_COMMAND": "Arbitrary shell execution — unbounded risk",
    "RUN_SHELL": "Arbitrary shell execution — unbounded risk",
    "INSTALL": "Software install — system mutation",
    "UNINSTALL": "Software removal — destructive",
    "MODIFY_REGISTRY": "Windows registry modification — system-wide effect",
    "MODIFY_SYSTEM": "System configuration change — wide blast radius",
    "SHUTDOWN": "System shutdown — affects entire machine",
    "REBOOT": "System reboot — affects entire machine",
    "KILL_PROCESS": "Process termination — can destroy work",
}

# State-file location for the audit log of rejected skills
_lock = threading.RLock()
_base_dir: Path | None = None


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir


def _audit_log_path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "skill_sandbox_audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _log_rejection(action_type: str, reason: str, context: str = "") -> None:
    """Audit log: every rejection is recorded so we can see what auto-skills
    were tried and blocked over time."""
    p = _audit_log_path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "action_type": action_type,
                "reason": reason,
                "context": context[:300],
                "kind": "rejection",
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[skill_sandbox] audit log error: {e!r}")


def is_action_type_allowed(action_type: str) -> bool:
    """Whitelist check. Action types not on the allowlist default to NOT allowed."""
    return str(action_type or "").strip().upper() in ALLOWED_ACTION_TYPES


def is_action_type_forbidden(action_type: str) -> bool:
    """Explicit blacklist check. Used for clearer error messages."""
    return str(action_type or "").strip().upper() in FORBIDDEN_ACTION_TYPES


def validate_skill_actions(
    actions: list[tuple[str, str | None]] | list[list[Any]],
) -> tuple[bool, str]:
    """Check that every action in `actions` is on the allowlist.

    Returns (ok, reason). ok=True iff EVERY action passes the check.
    First failed action's reason is returned. Empty action list = ok.

    `actions` is the standard skill action format: list of (TYPE, ARG)
    tuples or [TYPE, ARG] lists. Action types are uppercased + checked
    against the allowlist.
    """
    if not actions:
        return True, "no actions to validate"
    for i, action in enumerate(actions):
        if not action:
            continue
        try:
            action_type = str(action[0] or "").strip().upper()
        except (IndexError, TypeError):
            return False, f"action #{i} is malformed: {action!r}"

        if is_action_type_forbidden(action_type):
            reason = (
                f"{action_type} is forbidden in auto-learned skills: "
                f"{FORBIDDEN_ACTION_TYPES[action_type]}"
            )
            _log_rejection(action_type, reason, str(action))
            return False, reason

        if not is_action_type_allowed(action_type):
            reason = (
                f"{action_type} is not on the auto-skill allowlist (default-deny). "
                f"If this should be allowed, add it to ALLOWED_ACTION_TYPES "
                f"after safety review."
            )
            _log_rejection(action_type, reason, str(action))
            return False, reason

    return True, "all actions allowed"


def sandbox_filter(
    actions: list[tuple[str, str | None]] | list[list[Any]],
) -> list[tuple[str, str | None]]:
    """Filter mode: returns only the allowed actions, drops the rest.

    Used at EXECUTION time when a skill has been previously stored but
    we want to be defensive about running it. The dropped actions are
    audit-logged.

    Different from validate_skill_actions which is all-or-nothing.
    sandbox_filter is permissive of the safe subset.
    """
    out: list[tuple[str, str | None]] = []
    for action in actions or []:
        if not action:
            continue
        try:
            action_type = str(action[0] or "").strip().upper()
            arg = action[1] if len(action) > 1 else None
        except (IndexError, TypeError):
            continue

        if is_action_type_allowed(action_type):
            out.append((action_type, arg))
        else:
            reason = "filtered at execution: " + (
                FORBIDDEN_ACTION_TYPES.get(action_type)
                or "not on allowlist"
            )
            _log_rejection(action_type, reason, str(action))
    return out


def validate_skill_dict(skill: dict[str, Any]) -> tuple[bool, str]:
    """Validate a complete skill dict (the kind passed to skills.save_skill).

    Checks: actions field exists + every action passes the allowlist.
    Returns (ok, reason).
    """
    actions = skill.get("actions")
    if not isinstance(actions, list):
        return False, "skill is missing 'actions' list"
    return validate_skill_actions(actions)


def sandbox_summary() -> dict[str, Any]:
    """Operator-facing summary of the sandbox state."""
    allowed = list(ALLOWED_ACTION_TYPES.keys())
    forbidden = list(FORBIDDEN_ACTION_TYPES.keys())

    rejection_count = 0
    p = _audit_log_path()
    if p is not None and p.exists():
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rejection_count += 1
        except Exception:
            pass

    return {
        "allowed_action_types": sorted(allowed),
        "forbidden_action_types": sorted(forbidden),
        "rejection_audit_count": rejection_count,
        "policy": "fail-closed: action types not on allowlist default to FORBIDDEN",
    }
