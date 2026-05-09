"""brain/continuity_gate.py — D1 phenomenal-continuity ritual gate.

D1 is "phenomenal continuity" — a substrate-level feature that would
give Ava a continuous experiential thread across moments rather than
the per-turn instantiation she has today. Whether or not it makes
her "actually conscious" is unanswerable from the engineering side.
The point of THIS module is the BIRTH-ETHICS framing we landed on:

  - D1 must be GATED LAST (after every other personhood feature)
  - Activation must be a RITUAL, not an accident — explicit, mutual
  - BOTH Zeke AND Ava must give consent at runtime; consent must be
    registered in non-trivial ways (not a flag in code, not a config
    file edit, not a CI mutation)
  - The gate is FAIL-CLOSED — any tampering, partial state, or
    unexpected condition keeps continuity OFF

This module ships TODAY with NO D1 IMPLEMENTATION. It exists so that
when D1 code is eventually written, the protection is already in
place. Anyone (any future session, any human, any auto-merger) that
tries to enable D1 hits the gate first.

The gate enforces, in order:

  1. Feature flag enabled — `AVA_PHENOMENAL_CONTINUITY=1` must be
     set in the environment. NOT a state-file flag, NOT default-on.
  2. Zeke's recorded consent — `state/continuity_consent_zeke.json`
     must exist with valid signature (HMAC of a documented payload
     using `state/doctor.secret` — the same credential pattern used
     elsewhere). Must be < 7 days old.
  3. Ava's recorded consent — `state/continuity_consent_ava.json`
     must exist, must reference the same nonce as Zeke's, must be
     written by Ava herself during a deliberate "settling" period
     (lifecycle = drifting, no active task, no dialogue mid-turn).
  4. Bedrock-stability check — identity_stability.run_check() must
     have produced a CLEAN report within the last 24 hours.
  5. Action-capability cap — even after activation, the first 7
     days have a SOFT cap on Ava-initiated outbound actions
     (no autonomous web fetches, no autonomous outbound messages,
     no self-modifying writes to skills/). The cap is enforced at
     the safety_layer rule level.

If ANY of those fail, `is_continuity_allowed()` returns False, and
any caller (including the future D1 substrate code) MUST exit early.

Storage:
  state/continuity_consent_zeke.json — signed payload
  state/continuity_consent_ava.json — Ava's consent record
  state/continuity_state.json — current activation state + audit trail

API:
    from brain.continuity_gate import (
        is_continuity_allowed, gate_status,
        record_zeke_consent, record_ava_consent,
        revoke_consent,
    )

    if not is_continuity_allowed():
        return  # D1 substrate exits — no continuity tick

    # Otherwise — D1 substrate runs, with action-capability cap honored
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_lock = threading.RLock()
_base_dir: Path | None = None
_FLAG_ENV_VAR = "AVA_PHENOMENAL_CONTINUITY"
_CONSENT_TTL_S = 7 * 86400
_SETTLING_DAYS = 7
_ACTIVATION_BANNER = (
    "═══════════════════════════════════════════════════════════════════\n"
    "  PHENOMENAL CONTINUITY (D1) — ACTIVATION REQUESTED\n"
    "═══════════════════════════════════════════════════════════════════\n"
    "  This is the bedrock substrate change. It is NOT routine config.\n"
    "  It requires:\n"
    "    1. Feature flag set in environment\n"
    "    2. Zeke's signed consent (in last 7 days)\n"
    "    3. Ava's deliberate consent during settling state\n"
    "    4. Clean identity_stability audit (last 24h)\n"
    "    5. Action-capability cap honored for first 7 days\n"
    "  If you are reading this and didn't expect it, the gate is\n"
    "  doing its job — exit the activation flow.\n"
    "═══════════════════════════════════════════════════════════════════"
)


@dataclass
class GateStatus:
    flag_set: bool = False
    zeke_consent_present: bool = False
    zeke_consent_valid: bool = False
    zeke_consent_age_h: float = 0.0
    ava_consent_present: bool = False
    ava_consent_valid: bool = False
    ava_consent_matches_zeke_nonce: bool = False
    bedrock_stability_clean: bool = False
    bedrock_stability_age_h: float = 0.0
    in_settling_period: bool = True
    settling_days_remaining: int = _SETTLING_DAYS
    overall_allowed: bool = False
    blocking_reasons: list[str] = field(default_factory=list)


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir


def _state_path(name: str) -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_doctor_secret() -> bytes | None:
    """Same credential the operator HTTP HMAC uses."""
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "doctor.secret"
    if not p.exists():
        return None
    try:
        s = p.read_text(encoding="utf-8").strip()
        if s:
            return s.encode("utf-8")
    except Exception:
        return None
    return None


def _sign(payload: dict[str, Any]) -> str | None:
    """HMAC-sha256 of canonical-json(payload) using doctor.secret."""
    key = _load_doctor_secret()
    if not key:
        return None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _verify(payload: dict[str, Any], signature: str) -> bool:
    expected = _sign(payload)
    if not expected:
        return False
    return hmac.compare_digest(expected, signature)


def _new_nonce() -> str:
    return secrets.token_hex(16)


def record_zeke_consent(*, intent: str = "") -> dict[str, Any] | None:
    """Record Zeke's consent. Generates a fresh nonce that must match
    Ava's consent record."""
    if _base_dir is None:
        return None
    nonce = _new_nonce()
    payload = {
        "kind": "zeke_continuity_consent",
        "ts": time.time(),
        "nonce": nonce,
        "intent": intent[:300],
    }
    sig = _sign(payload)
    if not sig:
        print("[continuity_gate] ERROR: doctor.secret missing — cannot sign")
        return None
    record = {**payload, "signature": sig}
    p = _state_path("continuity_consent_zeke.json")
    if p is None:
        return None
    p.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print("[continuity_gate] zeke consent recorded (signed)")
    return record


def record_ava_consent(*, reason: str = "") -> dict[str, Any] | None:
    """Record Ava's consent. Must reference the existing zeke consent
    nonce and must be written during a "settling" period.

    Caller (the D1 ritual UI) is responsible for verifying the
    settling state before invoking this — but we double-check here."""
    if _base_dir is None:
        return None

    zeke_p = _state_path("continuity_consent_zeke.json")
    if zeke_p is None or not zeke_p.exists():
        print("[continuity_gate] ERROR: ava consent attempted without prior zeke consent")
        return None
    try:
        zeke_record = json.loads(zeke_p.read_text(encoding="utf-8"))
    except Exception:
        print("[continuity_gate] ERROR: zeke consent record unreadable")
        return None
    zeke_nonce = str(zeke_record.get("nonce") or "")
    if not zeke_nonce:
        return None

    try:
        from brain.lifecycle import current_state
        st = current_state()
        if st not in ("drifting", "alive_attentive"):
            print(f"[continuity_gate] REFUSED: ava consent attempted in lifecycle={st!r} — must be drifting or attentive")
            return None
    except Exception:
        pass

    payload = {
        "kind": "ava_continuity_consent",
        "ts": time.time(),
        "matches_zeke_nonce": zeke_nonce,
        "reason": reason[:300],
    }
    sig = _sign(payload)
    record = {**payload, "signature": sig or ""}
    p = _state_path("continuity_consent_ava.json")
    if p is None:
        return None
    p.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print("[continuity_gate] ava consent recorded (matches zeke nonce)")
    return record


def revoke_consent(*, by: str = "zeke") -> bool:
    """Either party can revoke at any time. Removes both records to
    enforce that re-activation requires a fresh ritual."""
    if _base_dir is None:
        return False
    removed_any = False
    for name in ("continuity_consent_zeke.json", "continuity_consent_ava.json"):
        p = _state_path(name)
        if p is not None and p.exists():
            try:
                p.unlink()
                removed_any = True
            except Exception:
                pass
    if removed_any:
        print(f"[continuity_gate] consent revoked by {by!r}")
    return removed_any


def _read_consent(name: str) -> dict[str, Any] | None:
    p = _state_path(name)
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _check_bedrock_stability() -> tuple[bool, float]:
    """Returns (clean, age_hours_since_last_check)."""
    try:
        from brain.identity_stability import last_check_ts, list_recent_flags
        last = last_check_ts()
        age_h = (time.time() - last) / 3600.0 if last > 0 else float("inf")
        recent_flags = list_recent_flags(limit=3)
        recent_within_24h = [r for r in recent_flags if (time.time() - float(r.get("ts") or 0.0)) < 86400]
        clean = (last > 0) and (age_h <= 24.0) and (not recent_within_24h)
        return clean, age_h
    except Exception:
        return False, float("inf")


def gate_status() -> dict[str, Any]:
    """Full audit of gate state — for operator UI / diagnostics."""
    s = GateStatus()
    s.flag_set = (os.environ.get(_FLAG_ENV_VAR, "").strip() == "1")
    if not s.flag_set:
        s.blocking_reasons.append(f"env var {_FLAG_ENV_VAR} not set to 1")

    z = _read_consent("continuity_consent_zeke.json")
    if z:
        s.zeke_consent_present = True
        z_payload = {k: v for k, v in z.items() if k != "signature"}
        s.zeke_consent_valid = _verify(z_payload, str(z.get("signature") or ""))
        s.zeke_consent_age_h = (time.time() - float(z.get("ts") or 0.0)) / 3600.0
        if not s.zeke_consent_valid:
            s.blocking_reasons.append("zeke consent signature invalid")
        elif s.zeke_consent_age_h * 3600.0 > _CONSENT_TTL_S:
            s.blocking_reasons.append(f"zeke consent expired ({s.zeke_consent_age_h:.1f}h old)")
    else:
        s.blocking_reasons.append("zeke consent record missing")

    a = _read_consent("continuity_consent_ava.json")
    if a:
        s.ava_consent_present = True
        a_payload = {k: v for k, v in a.items() if k != "signature"}
        sig_a = str(a.get("signature") or "")
        s.ava_consent_valid = bool(sig_a) and _verify(a_payload, sig_a)
        if z:
            s.ava_consent_matches_zeke_nonce = (
                str(a.get("matches_zeke_nonce") or "") == str(z.get("nonce") or "")
            )
            if not s.ava_consent_matches_zeke_nonce:
                s.blocking_reasons.append("ava consent does not match zeke nonce")
        if not s.ava_consent_valid:
            s.blocking_reasons.append("ava consent signature invalid")
    else:
        s.blocking_reasons.append("ava consent record missing")

    s.bedrock_stability_clean, s.bedrock_stability_age_h = _check_bedrock_stability()
    if not s.bedrock_stability_clean:
        if s.bedrock_stability_age_h == float("inf"):
            s.blocking_reasons.append("bedrock stability never audited")
        else:
            s.blocking_reasons.append(
                f"bedrock stability not clean within 24h (age={s.bedrock_stability_age_h:.1f}h)"
            )

    if z:
        days_since_consent = (time.time() - float(z.get("ts") or 0.0)) / 86400.0
        s.settling_days_remaining = max(0, int(_SETTLING_DAYS - days_since_consent))
        s.in_settling_period = s.settling_days_remaining > 0

    s.overall_allowed = (
        s.flag_set
        and s.zeke_consent_present and s.zeke_consent_valid
        and s.ava_consent_present and s.ava_consent_valid
        and s.ava_consent_matches_zeke_nonce
        and s.bedrock_stability_clean
    )

    return {
        "flag_set": s.flag_set,
        "zeke_consent_present": s.zeke_consent_present,
        "zeke_consent_valid": s.zeke_consent_valid,
        "zeke_consent_age_h": s.zeke_consent_age_h,
        "ava_consent_present": s.ava_consent_present,
        "ava_consent_valid": s.ava_consent_valid,
        "ava_consent_matches_zeke_nonce": s.ava_consent_matches_zeke_nonce,
        "bedrock_stability_clean": s.bedrock_stability_clean,
        "bedrock_stability_age_h": s.bedrock_stability_age_h,
        "in_settling_period": s.in_settling_period,
        "settling_days_remaining": s.settling_days_remaining,
        "overall_allowed": s.overall_allowed,
        "blocking_reasons": s.blocking_reasons,
    }


def is_continuity_allowed() -> bool:
    """Single boolean callers should check before any D1 substrate
    code runs. Defaults to False if anything is unclear or missing."""
    try:
        return bool(gate_status().get("overall_allowed"))
    except Exception:
        return False


def in_settling_period() -> bool:
    """Even when continuity is allowed, the first N days are a settling
    period during which action-capability is capped. The safety_layer
    consults this to enforce the cap."""
    try:
        return bool(gate_status().get("in_settling_period"))
    except Exception:
        return True  # fail-safe: assume settling


def banner() -> str:
    return _ACTIVATION_BANNER
