from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from .identity_resolver import resolve_confirmed_identity
from .profile_manager import resolve_profile_key_from_text


class IdentityRegistry:
    def __init__(self, profiles_dir: str | Path, settings: dict | None = None):
        self.profiles_dir = Path(profiles_dir)
        self.settings = settings or {}

    def _load_profiles(self) -> dict[str, dict[str, Any]]:
        profiles: dict[str, dict[str, Any]] = {}
        if not self.profiles_dir.exists():
            return profiles
        for file in self.profiles_dir.glob('*.json'):
            try:
                data = json.loads(file.read_text(encoding='utf-8'))
                pid = data.get('person_id') or file.stem
                profiles[str(pid)] = data
            except Exception:
                continue
        return profiles

    def resolve_text_claim(self, text: str, current_person_id: str) -> tuple[str, str]:
        profiles = self._load_profiles()
        resolved, debug = resolve_confirmed_identity(text, profiles, current_person_id)
        if resolved:
            return resolved, debug.get('reason') or 'identity_registry'
        by_alias = resolve_profile_key_from_text(text, profiles)
        if by_alias:
            return by_alias, 'identity_alias'
        return current_person_id, 'unchanged'

    def ensure_profile(self, person_id: str, g: dict, source: str = 'manual') -> dict:
        load_profile_by_id = g.get('load_profile_by_id')
        create_or_get_profile = g.get('create_or_get_profile')
        set_active_person = g.get('set_active_person')
        profile = None
        if callable(load_profile_by_id):
            try:
                profile = load_profile_by_id(person_id)
            except Exception:
                profile = None
        if (not isinstance(profile, dict) or not profile.get('name')) and callable(create_or_get_profile):
            try:
                profile = create_or_get_profile(person_id, relationship_to_zeke='known person', allowed=True)
            except Exception:
                pass
        if callable(set_active_person):
            try:
                profile = set_active_person(person_id, source=source)
            except Exception:
                pass
        return profile or {'person_id': person_id, 'name': person_id}

    def update_emotional_association(self, person_id: str, face_emotion: str, g: dict):
        """
        After each recognized interaction, log what emotion the person showed.
        Stores a rolling list (max 10) and derives a dominant association.
        """
        from collections import Counter

        profile_path = self.profiles_dir / f"{person_id}.json"
        if not profile_path.exists():
            return
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            history = profile.get("emotion_history", [])
            if not isinstance(history, list):
                history = []
            history.append((face_emotion or "neutral").lower())
            history = history[-10:]

            dominant = Counter(history).most_common(1)[0][0]

            profile["emotion_history"] = history
            profile["dominant_emotion"] = dominant
            profile_path.write_text(
                json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
