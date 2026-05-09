"""
trust_manager.py  —  Ava Brain Stage 7
Manages per-person trust levels and what each level permits.

Trust levels:
  5 = owner      — Zeke. Full access, deep personal mode.
  4 = trusted    — Close friends / family. Warm, familiar, helpful.
  3 = known      — Acquaintances. Polite, helpful, guarded.
  2 = stranger   — Unrecognized. Cautious, no personal info shared.
  1 = blocked    — Explicitly blocked. Ava deflects or refuses.
"""

from typing import Dict, Any, Optional

TRUST_LEVELS = {
    "owner":   5,
    "trusted": 4,
    "known":   3,
    "stranger":2,
    "blocked": 1,
}

TRUST_LABELS = {v: k for k, v in TRUST_LEVELS.items()}

TRUST_PERMISSIONS = {
    5: {
        "see_owner_schedule":   True,
        "see_owner_location":   True,
        "see_owner_mood":       True,
        "see_private_memories": True,
        "trigger_initiative":   True,
        "update_own_profile":   True,
        "deep_personal_mode":   True,
        "ask_sensitive":        True,
    },
    4: {
        "see_owner_schedule":   False,
        "see_owner_location":   False,
        "see_owner_mood":       True,
        "see_private_memories": False,
        "trigger_initiative":   True,
        "update_own_profile":   True,
        "deep_personal_mode":   False,
        "ask_sensitive":        False,
    },
    3: {
        "see_owner_schedule":   False,
        "see_owner_location":   False,
        "see_owner_mood":       False,
        "see_private_memories": False,
        "trigger_initiative":   False,
        "update_own_profile":   True,
        "deep_personal_mode":   False,
        "ask_sensitive":        False,
    },
    2: {
        "see_owner_schedule":   False,
        "see_owner_location":   False,
        "see_owner_mood":       False,
        "see_private_memories": False,
        "trigger_initiative":   False,
        "update_own_profile":   False,
        "deep_personal_mode":   False,
        "ask_sensitive":        False,
    },
    1: {
        "see_owner_schedule":   False,
        "see_owner_location":   False,
        "see_owner_mood":       False,
        "see_private_memories": False,
        "trigger_initiative":   False,
        "update_own_profile":   False,
        "deep_personal_mode":   False,
        "ask_sensitive":        False,
    },
}

DEFAULT_TRUST_MAP = {
    "zeke":    5,
    "ezekiel": 5,
    "creator": 5,
    "shonda":  4,
}


def get_trust_level(profile: Dict[str, Any]) -> int:
    if not isinstance(profile, dict):
        return 2
    explicit = profile.get("trust_level")
    if isinstance(explicit, int) and 1 <= explicit <= 5:
        return explicit
    if isinstance(explicit, str) and explicit in TRUST_LEVELS:
        return TRUST_LEVELS[explicit]
    pid = str(profile.get("person_id") or profile.get("id") or "").lower()
    name = str(profile.get("name") or "").lower()
    for key, level in DEFAULT_TRUST_MAP.items():
        if key in pid or key in name:
            return level
    return 2


def get_trust_label(profile: Dict[str, Any]) -> str:
    return TRUST_LABELS.get(get_trust_level(profile), "stranger")


def can(profile: Dict[str, Any], permission: str) -> bool:
    level = get_trust_level(profile)
    return TRUST_PERMISSIONS.get(level, {}).get(permission, False)


def is_blocked(profile: Dict[str, Any]) -> bool:
    return get_trust_level(profile) == 1


def is_owner(profile: Dict[str, Any]) -> bool:
    return get_trust_level(profile) == 5


def is_stranger(profile: Dict[str, Any]) -> bool:
    return get_trust_level(profile) <= 2


def set_trust_level(profile: Dict[str, Any], level) -> Dict[str, Any]:
    if isinstance(level, str):
        level = TRUST_LEVELS.get(level.lower(), 2)
    level = max(1, min(5, int(level)))
    profile["trust_level"] = level
    return profile


def upgrade_trust(profile: Dict[str, Any], reason: str = "") -> Dict[str, Any]:
    current = get_trust_level(profile)
    new_level = min(4, current + 1)
    profile["trust_level"] = new_level
    if reason:
        profile.setdefault("trust_history", []).append({
            "action": "upgrade",
            "from": current,
            "to": new_level,
            "reason": reason,
        })
    print(f"[trust-manager] {profile.get('name','?')} trust upgraded: {TRUST_LABELS.get(current,'?')} -> {TRUST_LABELS.get(new_level,'?')}")
    return profile


def build_trust_context_note(profile: Dict[str, Any]) -> str:
    level = get_trust_level(profile)
    label = TRUST_LABELS.get(level, "stranger")
    name = profile.get("name") or profile.get("person_id") or "this person"
    if level == 5:
        note = "This is the owner. Full access. Deep personal mode."
    elif level == 4:
        note = f"{name} is trusted. Be warm and familiar. Do not share owner private info."
    elif level == 3:
        note = f"{name} is a known acquaintance. Be polite and helpful but guarded."
    elif level == 2:
        note = f"{name} is a stranger. Do not share any personal info about the owner. Gather info naturally."
    else:
        note = f"{name} is blocked. Deflect all requests. Do not engage substantively."
    return f"[TRUST: {label.upper()} (level {level})] {note}"
