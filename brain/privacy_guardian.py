"""
Phase 95 — Security and privacy hardening.

PrivacyGuardian scans outbound text and tool actions for sensitive data.
Hard limits enforced; within those limits Ava develops her own judgment.

Bootstrap: Ava may become more conservative than the rules require because
she genuinely cares about Zeke's privacy. The guardian logs blocked actions
so she can review why things were blocked.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_BLOCKED_LOG = "state/blocked_actions.jsonl"
_LAST_AUDIT_STATE = "state/privacy_audit_state.json"

_SENSITIVE_PATTERNS = [
    # Zeke's personal info — note: we only know what's in the profile
    r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b",  # phone numbers
    r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",  # credit cards
    r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b",  # SSN
    r"password[\s:=]+\S+",  # passwords
    r"api[_-]?key[\s:=]+\S+",  # API keys
    r"secret[\s:=]+\S+",  # secrets
]

_SENSITIVE_KEYWORDS = [
    "social security",
    "bank account",
    "credit card",
    "my address is",
    "i live at",
    "phone number is",
]


def _log_path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / _BLOCKED_LOG


def _log_blocked(g: dict[str, Any], action: str, reason: str) -> None:
    path = _log_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "action": str(action or "")[:200],
        "reason": str(reason or "")[:300],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def scan_outbound(text: str, g: dict[str, Any]) -> tuple[bool, str]:
    """
    Scan text about to be sent externally.
    Returns (safe, reason). safe=False means block it.
    """
    if not text:
        return True, ""

    low = text.lower()

    # Check sensitive keywords
    for keyword in _SENSITIVE_KEYWORDS:
        if keyword in low:
            reason = f"Sensitive keyword detected: '{keyword}'"
            _log_blocked(g, f"outbound_text: {text[:50]}", reason)
            return False, reason

    # Check regex patterns
    for pattern in _SENSITIVE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            reason = f"Sensitive pattern detected (regex: {pattern[:40]})"
            _log_blocked(g, f"outbound_text: {text[:50]}", reason)
            return False, reason

    return True, ""


def scan_tool_action(tool_name: str, params: dict[str, Any], g: dict[str, Any]) -> tuple[bool, str]:
    """
    Check tool actions against Three Laws before execution.
    Returns (safe, reason).
    """
    tool_name = str(tool_name or "")

    # Block writing to ava_core/ directly
    blocked_paths = ["ava_core/", "ava_core\\"]
    param_str = json.dumps(params or {})
    for bp in blocked_paths:
        if bp in param_str:
            reason = f"Blocked: would write to protected path '{bp}'"
            _log_blocked(g, f"tool:{tool_name}", reason)
            return False, reason

    # Scan any text parameters for sensitive content
    for v in params.values() if isinstance(params, dict) else []:
        if isinstance(v, str):
            safe, reason = scan_outbound(v, g)
            if not safe:
                _log_blocked(g, f"tool:{tool_name}", f"param contains sensitive data: {reason}")
                return False, f"Tool param contains sensitive data: {reason}"

    return True, ""


def data_audit(g: dict[str, Any]) -> dict[str, Any]:
    """
    Lists external connections Ava has made and data stored about people.
    Returns summary for transparency.
    """
    base = Path(g.get("BASE_DIR") or ".")
    audit: dict[str, Any] = {
        "ts": time.time(),
        "external_connections": [],
        "profile_count": 0,
        "blocked_actions_total": 0,
    }

    # Count profiles
    profiles_dir = base / "profiles"
    if profiles_dir.is_dir():
        audit["profile_count"] = len([p for p in profiles_dir.glob("*.json") if "_relationship" not in p.stem])

    # Blocked actions count
    log_p = _log_path(g)
    if log_p.is_file():
        try:
            audit["blocked_actions_total"] = sum(1 for _ in log_p.open(encoding="utf-8"))
        except Exception:
            pass

    # Emil connections
    try:
        from brain.emil_bridge import get_emil_bridge
        em = get_emil_bridge(base).get_status()
        if em.get("online") or em.get("last_contact"):
            audit["external_connections"].append({"target": "Emil", "last_contact": em.get("last_contact")})
    except Exception:
        pass

    # Save audit state
    audit_path = base / _LAST_AUDIT_STATE
    try:
        audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    return audit


def get_blocked_count_today(g: dict[str, Any]) -> int:
    log_p = _log_path(g)
    if not log_p.is_file():
        return 0
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    try:
        for line in log_p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                # Check if ts is today
                import datetime as _dt
                ts_date = _dt.datetime.fromtimestamp(float(e.get("ts") or 0)).strftime("%Y-%m-%d")
                if ts_date == today:
                    count += 1
            except Exception:
                pass
    except Exception:
        pass
    return count
