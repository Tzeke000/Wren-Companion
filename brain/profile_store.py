"""
profile_store.py  —  Ava Brain Stage 7
Manages per-person living profile files stored in the repo ``profiles/`` directory.
Each person gets their own <person_id>.json file that grows over time.

Profile structure:
{
  "person_id": "zeke",
  "name": "Ezekiel",
  "aliases": ["zeke", "ezekiel", "creator"],
  "relationship": "creator and owner",
  "trust_level": 5,
  "notes": "Free-form notes Ava has learned about this person.",
  "last_topic": "...",
  "last_seen": "2026-04-01T01:00:00",
  "trust_history": [],
  "created_at": "...",
  "updated_at": "..."
}
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

_REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = _REPO_ROOT / "profiles"


def _profiles_path() -> Path:
    return PROFILES_DIR


def _profile_file(person_id: str) -> Path:
    safe_id = _safe_id(person_id)
    return _profiles_path() / f"{safe_id}.json"


def _safe_id(person_id: str) -> str:
    import re
    pid = (person_id or "unknown").strip().lower()
    pid = re.sub(r"[^a-z0-9_]", "_", pid)
    pid = re.sub(r"_+", "_", pid).strip("_")
    return pid or "unknown"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_profile(person_id: str) -> Optional[Dict[str, Any]]:
    """Load a profile from disk. Returns None if not found."""
    path = _profile_file(person_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[profile-store] failed to load {person_id}: {e}")
        return None


def save_profile(profile: Dict[str, Any]) -> bool:
    """Save a profile to disk. Returns True on success."""
    person_id = profile.get("person_id") or profile.get("id") or "unknown"
    path = _profile_file(person_id)
    try:
        _profiles_path().mkdir(parents=True, exist_ok=True)
        profile["updated_at"] = _now()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[profile-store] failed to save {person_id}: {e}")
        return False


def list_profiles() -> List[Dict[str, Any]]:
    """Load and return all profile dicts."""
    out = []
    try:
        for path in _profiles_path().glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    out.append(json.load(f))
            except Exception:
                pass
    except Exception:
        pass
    return out


def create_profile(person_id: str, name: str, relationship: str = "",
                   trust_level: int = 2) -> Dict[str, Any]:
    """Create and save a new profile. Returns the new profile dict."""
    safe = _safe_id(person_id)
    profile = {
        "person_id": safe,
        "name": name,
        "aliases": [safe],
        "relationship": relationship,
        "trust_level": trust_level,
        "notes": "",
        "last_topic": "",
        "last_seen": _now(),
        "trust_history": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    save_profile(profile)
    print(f"[profile-store] created new profile: {name} ({safe}) trust={trust_level}")
    return profile


def get_or_create_profile(person_id: str, name: str = "",
                           relationship: str = "", trust_level: int = 2) -> Dict[str, Any]:
    """Load existing profile or create a new one."""
    existing = load_profile(person_id)
    if existing:
        return existing
    display_name = name or person_id.replace("_", " ").title()
    return create_profile(person_id, display_name, relationship, trust_level)


def update_profile_notes(person_id: str, new_fact: str) -> bool:
    """Append a learned fact to a profile's notes. Used by reflection loop."""
    profile = load_profile(person_id)
    if not profile:
        return False
    existing = profile.get("notes") or ""
    timestamp = datetime.now().strftime("%Y-%m-%d")
    separator = "\n" if existing else ""
    profile["notes"] = existing + separator + f"[{timestamp}] {new_fact}"
    return save_profile(profile)


def touch_last_seen(person_id: str, topic: str = "") -> bool:
    """Update last_seen timestamp and optionally last_topic."""
    profile = load_profile(person_id)
    if not profile:
        return False
    profile["last_seen"] = _now()
    if topic:
        profile["last_topic"] = topic[:200]
    return save_profile(profile)


def seed_default_profiles():
    """
    Seed the owner and known family profiles if they don't exist yet.
    Safe to call at startup — skips profiles that already exist.
    """
    defaults = [
        {
            "person_id": "zeke",
            "name": "Zeke",
            "aliases": ["zeke", "ezekiel", "creator", "your_creator"],
            "relationship": "creator and owner",
            "trust_level": 5,
            "notes": "Zeke is Ava's creator. He built and maintains her. Full trust.",
        },
        {
            "person_id": "shonda",
            "name": "Shonda",
            "aliases": ["shonda", "mom", "mother", "my_mom"],
            "relationship": "Zeke's mother",
            "trust_level": 4,
            "notes": "",
        },
    ]
    for d in defaults:
        pid = d["person_id"]
        if not _profile_file(pid).exists():
            d.setdefault("last_topic", "")
            d.setdefault("last_seen", _now())
            d.setdefault("trust_history", [])
            d.setdefault("created_at", _now())
            d.setdefault("updated_at", _now())
            save_profile(d)
            print(f"[profile-store] seeded default profile: {d['name']}")
        else:
            print(f"[profile-store] profile already exists, skipping seed: {pid}")
