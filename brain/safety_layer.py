"""brain/safety_layer.py — Safety / Boundary layer foundation.

Architecture sweep #7 from designs/ava-roadmap-personhood.md.

Every action Ava takes (open_app, close_app, type_text, send_message,
delete_file, run_subprocess, etc) passes through this layer before
execution. Layer inputs:

- Speaker trust (from Person Registry; today: hardcoded mapping by
  person_id — Zeke=high, claude_code=high, others=low)
- Action impact level (low/medium/high/critical — declared by caller)
- Ava's values (from IDENTITY/SOUL — discretion, honesty, etc.)

Outputs (Decision):
- EXECUTE — proceed
- DECLINE — refuse with explanation
- ASK_BACK — request clarification before proceeding
- DEFER_TO_ZEKE — escalate to owner for confirmation

Today this is a SKELETON. Default rule chain returns EXECUTE for
everything (no behavior change vs. pre-safety-layer). Future sessions
land actual rules:
- C1 boundaries / refusal capacity
- C12 discretion / privacy graph
- C14 asking-for-clarification before non-trivial actions
- B8 honesty about constraints
- The phenomenal-continuity activation cap (#7-applied to D1)

This is the SEAM where those rules will plug in. Building the seam
first means future rules land additively without surgery.

Usage:

    from brain.safety_layer import safety, Action, Decision

    decision = safety.evaluate(Action(
        action_type="open_app",
        target="Chrome",
        params={"app_name": "chrome"},
        source_user="zeke",
        impact_level="low",
    ))
    if decision.execute:
        # proceed with the action
        ...
    else:
        # use decision.spoken_reply as Ava's response
        ...
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal


ImpactLevel = Literal["low", "medium", "high", "critical"]
TrustLevel = Literal["unknown", "low", "medium", "high"]
DecisionKind = Literal["execute", "decline", "ask_back", "defer_to_zeke"]


@dataclass
class Action:
    """Description of what Ava is about to do, evaluated by the safety layer."""

    action_type: str  # e.g. "open_app", "close_app", "type_text", "delete_file"
    target: str = ""  # e.g. "chrome", "C:/some/file.txt"
    params: dict[str, Any] = field(default_factory=dict)
    source_user: str = ""  # person_id who initiated
    impact_level: ImpactLevel = "low"  # low/medium/high/critical
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """Output of safety evaluation."""

    kind: DecisionKind
    rule_name: str = "default"
    reason: str = ""
    spoken_reply: str = ""  # what Ava should say (for non-execute decisions)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def execute(self) -> bool:
        return self.kind == "execute"


# A rule is a callable: (Action, current_g_state) -> Decision | None
# None means "no opinion, defer to next rule." A non-None decision short-circuits.
SafetyRule = Callable[[Action, dict[str, Any]], "Decision | None"]


# ── Built-in trust resolution ─────────────────────────────────────────────


_DEFAULT_TRUST_BY_PERSON_ID: dict[str, TrustLevel] = {
    "zeke": "high",
    "claude_code": "high",  # developer assistant — trusted
    "shonda": "medium",
    # everyone else → "unknown" (lowest)
}


def _resolve_trust(person_id: str, g: dict[str, Any]) -> TrustLevel:
    """Today: hardcoded mapping. Future: queries the Person Registry."""
    pid = str(person_id or "").strip().lower()
    if not pid:
        return "unknown"
    return _DEFAULT_TRUST_BY_PERSON_ID.get(pid, "unknown")


# ── Safety singleton ──────────────────────────────────────────────────────


class SafetyLayer:
    def __init__(self) -> None:
        self._rules: list[tuple[str, SafetyRule]] = []
        self._lock = threading.RLock()
        self._decisions_log: list[dict[str, Any]] = []
        self._max_log_entries = 500

    def register(self, name: str, rule: SafetyRule) -> None:
        """Add a rule to the chain. Order of registration = order of evaluation."""
        with self._lock:
            self._rules.append((str(name), rule))

    def clear(self) -> None:
        """Remove all rules. Tests use this; production shouldn't need to."""
        with self._lock:
            self._rules.clear()

    def evaluate(self, action: Action, g: dict[str, Any] | None = None) -> Decision:
        """Run the rule chain. First rule that returns a non-None Decision wins.

        If no rule fires, returns the DEFAULT EXECUTE decision (no
        behavior change). This is intentional: today the layer is a
        skeleton; rules land in future sessions.
        """
        ctx = g or {}
        with self._lock:
            rules = list(self._rules)
        for name, rule in rules:
            try:
                d = rule(action, ctx)
                if d is not None:
                    self._log(action, d)
                    return d
            except Exception as e:
                print(f"[safety_layer] rule {name!r} raised: {e!r}")
                continue
        # Default: execute. This is the no-op behavior matching pre-layer.
        d = Decision(kind="execute", rule_name="default", reason="no rule fired")
        self._log(action, d)
        return d

    def _log(self, action: Action, decision: Decision) -> None:
        entry = {
            "ts": time.time(),
            "action_type": action.action_type,
            "target": action.target[:80] if action.target else "",
            "source_user": action.source_user,
            "impact_level": action.impact_level,
            "decision_kind": decision.kind,
            "rule_name": decision.rule_name,
            "reason": decision.reason[:200] if decision.reason else "",
        }
        with self._lock:
            self._decisions_log.append(entry)
            if len(self._decisions_log) > self._max_log_entries:
                self._decisions_log = self._decisions_log[-self._max_log_entries:]

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._decisions_log)[-int(limit):]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            log = list(self._decisions_log)
        if not log:
            return {"total": 0}
        counts: dict[str, int] = {}
        for entry in log:
            k = str(entry.get("decision_kind") or "unknown")
            counts[k] = counts.get(k, 0) + 1
        return {
            "total": len(log),
            "by_decision": counts,
            "rules_registered": len(self._rules),
        }


# Process-singleton.
safety = SafetyLayer()


# ── Convenience helpers (used by future rule modules) ─────────────────────


def trust_of(action: Action, g: dict[str, Any]) -> TrustLevel:
    return _resolve_trust(action.source_user, g)


def is_high_impact(action: Action) -> bool:
    return action.impact_level in ("high", "critical")


# ── No-op default rule registry ───────────────────────────────────────────
# Today: nothing registered. Default behavior is EXECUTE for all actions.
# Future modules will register rules here:
#
#   from brain.safety_layer import safety
#   def my_rule(action, g):
#       if action.action_type == "delete_file" and trust_of(action, g) == "low":
#           return Decision(kind="decline", reason="untrusted speaker, destructive action",
#                           spoken_reply="I'm not going to delete that — I don't know you well enough.")
#       return None
#   safety.register("delete_file_trust_gate", my_rule)
#
# That landing happens in future Wave 1+ sessions when actual rules
# are designed and tested. The seam is here so they slot in additively.


def configure_safety(g: dict[str, Any] | None = None) -> None:
    """Called once at startup. Today: no-op. Future: load
    state/safety_rules.json + register declared rules."""
    pass
