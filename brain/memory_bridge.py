from __future__ import annotations
from pathlib import Path


class MemoryBridge:
    def __init__(self, memory_dir: str | Path, settings: dict | None = None):
        self.memory_dir = Path(memory_dir)
        self.settings = settings or {}

    def _resolve_person_key(self, active_profile: dict | None, g: dict):
        if isinstance(active_profile, dict):
            for key in ('person_id', 'id', 'profile_id', 'slug', 'name'):
                value = active_profile.get(key)
                if value:
                    return str(value), key
        for key in ('active_person', 'ACTIVE_PERSON_ID', 'current_person_id'):
            value = g.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), key
        getter = g.get('get_active_person_id')
        if callable(getter):
            try:
                value = getter()
                if value:
                    return str(value), 'get_active_person_id()'
            except Exception:
                pass
        return None, None

    def build_summary(self, g: dict, user_input: str, active_profile: dict | None) -> str:
        person_key, used_key = self._resolve_person_key(active_profile, g)
        memories = []
        reflections = []
        debug = []
        debug.append(f"[memory-bridge] using profile key: {used_key}={person_key}" if used_key else '[memory-bridge] no usable profile key found')

        search_memories = g.get('search_memories')
        if callable(search_memories) and person_key:
            for args in ((user_input, person_key, 5), (user_input, person_key), (user_input,)):
                try:
                    memories = search_memories(*args) or []
                    break
                except TypeError:
                    continue
                except Exception as e:
                    debug.append(f'[memory-bridge] search_memories failed: {e}')
                    break

        load_recent_reflections = g.get('load_recent_reflections')
        if callable(load_recent_reflections) and person_key:
            for kwargs in ({'limit': 5, 'person_id': person_key}, {'person_id': person_key}, {'limit': 5}):
                try:
                    reflections = load_recent_reflections(**kwargs) or []
                    break
                except TypeError:
                    continue
                except Exception as e:
                    debug.append(f'[memory-bridge] load_recent_reflections failed: {e}')
                    break

        for line in debug:
            try:
                print(line)
            except Exception:
                pass

        mem_lines = []
        for row in memories[:3]:
            if isinstance(row, dict):
                txt = str(row.get('text', ''))[:180].strip()
                if txt:
                    mem_lines.append(f'- {txt}')
        ref_lines = []
        for row in reflections[:2]:
            if isinstance(row, dict):
                txt = str(row.get('reflection_text', row.get('text', '')))[:180].strip()
                if txt:
                    ref_lines.append(f'- {txt}')
        return (
            f"Recent relevant memories:\n{chr(10).join(mem_lines) if mem_lines else '- none retrieved'}\n"
            f"Recent reflections:\n{chr(10).join(ref_lines) if ref_lines else '- none retrieved'}"
        )
