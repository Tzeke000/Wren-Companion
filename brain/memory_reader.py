from typing import Any, Dict, List, Tuple

_PROFILE_KEYS = ("person_id", "id", "profile_id", "slug", "name")
_GLOBAL_FALLBACK_KEYS = ("active_person", "ACTIVE_PERSON_ID", "current_person_id")

def resolve_profile_person_key(active_profile: dict | None, g: dict | None = None) -> Tuple[str | None, str | None]:
    if isinstance(active_profile, dict):
        for key in _PROFILE_KEYS:
            value = active_profile.get(key)
            if value:
                return str(value), key
    if isinstance(g, dict):
        for key in _GLOBAL_FALLBACK_KEYS:
            value = g.get(key)
            if value:
                return str(value), f"global:{key}"
    return None, None

def _call_memory_fn(fn, user_input: str, person_key: str, limit: int):
    attempts = [
        lambda: fn(user_input, person_key, limit),
        lambda: fn(user_input, person_id=person_key, k=limit),
        lambda: fn(user_input, person_id=person_key, limit=limit),
        lambda: fn(query=user_input, person_id=person_key, k=limit),
        lambda: fn(query=user_input, person_id=person_key, limit=limit),
    ]
    for attempt in attempts:
        try:
            result = attempt()
            return result or []
        except TypeError:
            continue
    return []

def _call_reflection_fn(fn, person_key: str, limit: int):
    attempts = [
        lambda: fn(limit=limit, person_id=person_key),
        lambda: fn(person_id=person_key, limit=limit),
        lambda: fn(person_key, limit),
        lambda: fn(limit),
    ]
    for attempt in attempts:
        try:
            result = attempt()
            return result or []
        except TypeError:
            continue
    return []

def build_memory_reader_summary(g: Dict[str, Any], user_input: str, active_profile: dict | None) -> str:
    person_key, used_key = resolve_profile_person_key(active_profile, g)
    memories: List[dict] = []
    reflections: List[dict] = []
    self_model: dict = {}
    debug_lines: List[str] = []

    if used_key:
        debug_lines.append(f"[memory-reader] using profile key: {used_key}={person_key}")
    else:
        debug_lines.append("[memory-reader] no usable profile key found")

    try:
        if 'search_memories' in g and person_key:
            memories = _call_memory_fn(g['search_memories'], user_input, person_key, 5)
            debug_lines.append(f"[memory-reader] memories retrieved: {len(memories)}")
    except Exception as e:
        debug_lines.append(f"[memory-reader] search_memories failed: {e}")
    try:
        if 'load_recent_reflections' in g and person_key:
            reflections = _call_reflection_fn(g['load_recent_reflections'], person_key, 5)
            debug_lines.append(f"[memory-reader] reflections retrieved: {len(reflections)}")
    except Exception as e:
        debug_lines.append(f"[memory-reader] load_recent_reflections failed: {e}")
    try:
        if 'load_self_model' in g:
            self_model = g['load_self_model']() or {}
    except Exception:
        self_model = {}

    for line in debug_lines:
        try:
            print(line)
        except Exception:
            pass

    mem_lines = []
    for row in memories[:3]:
        text = str(row.get('text', row.get('content', '')))[:180]
        if text:
            mem_lines.append(f"- {text}")
    ref_lines = []
    for row in reflections[:2]:
        text = str(row.get('reflection_text', row.get('text', row.get('content', ''))))[:180]
        if text:
            ref_lines.append(f"- {text}")

    drives = self_model.get('core_drives', [])[:4] if isinstance(self_model, dict) else []
    patterns = self_model.get('behavior_patterns', [])[-4:] if isinstance(self_model, dict) else []
    return (
        "DYNAMIC SELF / MEMORY READER:\n"
        f"Recent relevant memories:\n{chr(10).join(mem_lines) if mem_lines else '- none retrieved'}\n"
        f"Recent reflections:\n{chr(10).join(ref_lines) if ref_lines else '- none retrieved'}\n"
        f"Current core drives: {drives if drives else 'unknown'}\n"
        f"Recent behavior patterns: {patterns if patterns else 'unknown'}\n"
        "Use this dynamic section above the static self model if they conflict."
    )