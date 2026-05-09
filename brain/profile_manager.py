import re
from typing import Dict, Any, Tuple

GENERIC_PROFILE_TOKENS = {
    "in","front","of","the","camera","currently","now","concerned","about","being","right",
    "yes","it","is","me","i","am","here","at","screen","present","current","person"
}
GENERIC_PROFILE_PHRASES = {
    "in front of the camera currently",
    "right now",
    "concerned about being",
    "yes it is me",
    "i am here",
    "at the camera",
    "in front of the camera",
}
PROTECTED_PROFILE_KEYS = {"zeke", "ezekiel", "creator", "your_creator"}
DEFAULT_ALIASES = {
    "zeke": ["zeke", "ezekiel", "creator", "your_creator", "your creator"],
    "shonda": ["shonda", "mom", "my mom", "my_mom", "mother", "my mother"],
}


def normalize_person_key(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def clean_display_name(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def looks_like_phrase_profile(name: str) -> bool:
    n = clean_display_name(name).lower()
    if not n:
        return True
    if normalize_person_key(n) in {normalize_person_key(p) for p in GENERIC_PROFILE_PHRASES}:
        return True
    words = re.findall(r"[a-zA-Z]+", n)
    if len(words) >= 4:
        return True
    if len(words) == 0:
        return True
    stopish = sum(1 for w in words if w in GENERIC_PROFILE_TOKENS)
    if stopish >= max(2, len(words) - 1):
        return True
    if len(n) > 32:
        return True
    return False


def is_valid_profile_name(name: str) -> bool:
    n = clean_display_name(name)
    if looks_like_phrase_profile(n):
        return False
    if len(n) < 2 or len(n) > 32:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", n)
    if not words or len(words) > 3:
        return False
    alpha_ratio = sum(ch.isalpha() for ch in n) / max(1, len(n))
    return alpha_ratio >= 0.5


def protected_profile_match(profile_name: str) -> bool:
    key = normalize_person_key(profile_name)
    return key in PROTECTED_PROFILE_KEYS


def ensure_aliases_in_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    aliases = profile.get("aliases") if isinstance(profile.get("aliases"), list) else []
    seen = set()
    out = []
    for item in aliases:
        k = normalize_person_key(str(item))
        if k and k not in seen:
            out.append(k)
            seen.add(k)
    name_key = normalize_person_key(profile.get("name", profile.get("person_id", "")))
    if name_key and name_key not in seen:
        out.append(name_key)
        seen.add(name_key)
    for base, alias_list in DEFAULT_ALIASES.items():
        if name_key == base or base in out:
            for alias in alias_list:
                ak = normalize_person_key(alias)
                if ak and ak not in seen:
                    out.append(ak)
                    seen.add(ak)
    profile["aliases"] = out
    return profile


def resolve_profile_key_from_text(text: str, profiles: Dict[str, Dict[str, Any]]) -> str | None:
    low = clean_display_name(text).lower()
    key = normalize_person_key(low)
    if key in profiles:
        return key
    for base, alias_list in DEFAULT_ALIASES.items():
        norm_aliases = {normalize_person_key(a) for a in alias_list}
        if key in norm_aliases and base in profiles:
            return base
    for pid, profile in (profiles or {}).items():
        ensure_aliases_in_profile(profile)
        if key in set(profile.get("aliases", [])):
            return pid
        name_key = normalize_person_key(profile.get("name", ""))
        if key == name_key:
            return pid
    return None


def maybe_add_alias_to_profile(profile: Dict[str, Any], alias_text: str) -> bool:
    if not isinstance(profile, dict) or not alias_text:
        return False
    if looks_like_phrase_profile(alias_text):
        return False
    ensure_aliases_in_profile(profile)
    alias_key = normalize_person_key(alias_text)
    if alias_key and alias_key not in profile["aliases"]:
        profile["aliases"].append(alias_key)
        return True
    return False


def merge_profiles(profiles: Dict[str, Dict[str, Any]], primary_key: str, duplicate_key: str) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    if not primary_key or not duplicate_key or primary_key == duplicate_key:
        return profiles, False
    if duplicate_key not in profiles or primary_key not in profiles:
        return profiles, False
    if protected_profile_match(duplicate_key) and not protected_profile_match(primary_key):
        return profiles, False
    primary = ensure_aliases_in_profile(profiles[primary_key])
    duplicate = ensure_aliases_in_profile(profiles[duplicate_key])
    for alias in duplicate.get("aliases", []):
        if alias not in primary["aliases"]:
            primary["aliases"].append(alias)
    dname = normalize_person_key(duplicate.get("name", ""))
    if dname and dname not in primary["aliases"]:
        primary["aliases"].append(dname)
    primary["merged_from"] = sorted(set(primary.get("merged_from", []) + [duplicate_key]))
    profiles.pop(duplicate_key, None)
    return profiles, True


def delete_profile(profiles: Dict[str, Dict[str, Any]], key: str) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    if not key or key not in profiles:
        return profiles, False
    profile = profiles.get(key, {})
    if protected_profile_match(key) or protected_profile_match(profile.get("name", "")):
        return profiles, False
    profiles.pop(key, None)
    return profiles, True
