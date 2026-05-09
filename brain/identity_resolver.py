import re
from typing import Dict, Any, Optional, Tuple
from .profile_manager import (
    normalize_person_key, resolve_profile_key_from_text, is_valid_profile_name,
    maybe_add_alias_to_profile, looks_like_phrase_profile
)

SELF_PATTERNS = [
    r"\bit(?:'s| is)\s+me,\s*([A-Za-z][A-Za-z'\- ]{1,30})\b",
    r"\bi am\s+([A-Za-z][A-Za-z'\- ]{1,30})\b",
    r"\bi'm\s+([A-Za-z][A-Za-z'\- ]{1,30})\b",
    r"\bit is\s+([A-Za-z][A-Za-z'\- ]{1,30})\b",
]


def extract_identity_claim(text: str) -> Optional[str]:
    t = (text or "").strip()
    for pat in SELF_PATTERNS:
        m = re.search(pat, t, flags=re.I)
        if m:
            return m.group(1).strip()
    return None  # only explicit self-ID patterns; no short-phrase fallback


def resolve_confirmed_identity(text: str, profiles: Dict[str, Dict[str, Any]], current_active: Optional[str] = None) -> Tuple[Optional[str], Dict[str, Any]]:
    claim = extract_identity_claim(text)
    debug = {"claim": claim, "resolved": None, "reason": None}
    if not claim:
        return None, debug
    if looks_like_phrase_profile(claim):
        debug["reason"] = "rejected_phrase_like_claim"
        return None, debug
    existing = resolve_profile_key_from_text(claim, profiles)
    if existing:
        maybe_add_alias_to_profile(profiles[existing], claim)
        debug["resolved"] = existing
        debug["reason"] = "matched_existing_profile_or_alias"
        return existing, debug
    if is_valid_profile_name(claim):
        norm = normalize_person_key(claim)
        if norm:
            profiles.setdefault(norm, {"name": claim, "aliases": [norm], "created_by_identity_claim": True})
            maybe_add_alias_to_profile(profiles[norm], claim)
            debug["resolved"] = norm
            debug["reason"] = "created_compact_valid_profile"
            return norm, debug
    debug["reason"] = "no_valid_identity_resolution"
    return None, debug
