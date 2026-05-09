from __future__ import annotations
from .shared import clamp01, safe_float, now_iso, atomic_json_save, json_load, deepcopy_jsonable

def _goal_file(host):
    return host.get('GOAL_SYSTEM_FILE') or host.get('goal_system_file') or 'state/goal_system.json'

def _health(host):
    fn = host.get('load_health_state')
    if callable(fn):
        try: return dict(fn() or {})
        except Exception: return {}
    return {}

def _mood(host):
    fn = host.get('load_mood')
    if callable(fn):
        try: return dict(fn() or {})
        except Exception: return {}
    return {}

def _camera_state(host):
    fn = host.get('load_camera_state')
    if callable(fn):
        try: return dict(fn() or {})
        except Exception: return {}
    return {}

def default_goal_system(host) -> dict:
    return {
        'version': 3,
        'active_goal': 'maintain_connection',
        'goal_strength': 0.5,
        'goal_blend': {
            'reduce_stress': 0.10, 'increase_engagement': 0.20, 'explore_topic': 0.20,
            'clarify': 0.10, 'maintain_connection': 0.25, 'observe_silently': 0.08, 'wait_for_user': 0.07,
        },
        'meta_state': {
            'interaction_mode': 'balanced', 'force_mode': 'balanced', 'mode_strength': 0.5,
            'mode_confidence': 0.5, 'mode_started_at': now_iso(), 'last_mode_switch_at': now_iso(), 'time_in_mode': 0.0,
        },
        'meta_feedback': {'recent': [], 'success_rate': 0.5, 'ignored_rate': 0.0, 'confusion_rate': 0.0, 'window_total': 0},
        'custom_meta_modes': {},
        'health_overlay': {'mode': 'none', 'initiative_scale': 1.0, 'confidence_scale': 1.0},
        'last_updated': now_iso(),
    }

def load_goal_system(host) -> dict:
    base = default_goal_system(host)
    system = json_load(_goal_file(host), base)
    for k, v in base.items():
        system.setdefault(k, deepcopy_jsonable(v) if isinstance(v, (dict, list)) else v)
    return system

def save_goal_system(host, system: dict):
    merged = default_goal_system(host)
    merged.update(system or {})
    merged['last_updated'] = now_iso()
    atomic_json_save(_goal_file(host), merged)

def _compute_meta_control(host, system=None, mood=None, camera_state=None, initiative_state=None) -> dict:
    system = system or load_goal_system(host)
    mood = mood or _mood(host)
    camera_state = camera_state or _camera_state(host)
    health = _health(host)
    mods = dict(health.get('behavior_modifiers') or {})
    initiative_scale = clamp01(mods.get('initiative_scale', 1.0))
    confidence_scale = clamp01(mods.get('confidence_scale', 1.0))
    support_bias = clamp01(mods.get('support_bias', 0.0))
    silence_bias = clamp01(mods.get('silence_bias', 0.0))
    mode = 'balanced'
    if str(health.get('degraded_mode','none')) == 'support_only':
        mode = 'supportive'
    elif str(health.get('degraded_mode','none')) in {'low_initiative','cautious'}:
        mode = 'low_initiative'
    return {
        'interaction_mode': mode,
        'force_mode': mode if mode != 'balanced' else 'balanced',
        'initiative_bias': 0.10 - (1.0 - initiative_scale) * 0.25,
        'silence_bias': silence_bias,
        'support_bias': support_bias,
        'variation_chance': 0.06 * (0.8 + confidence_scale * 0.2),
        'health_mode': str(health.get('degraded_mode', 'none')),
        'health_confidence_scale': confidence_scale,
        'health_initiative_scale': initiative_scale,
        'reason': f"mode={mode} health={health.get('overall','unknown')}",
        'last_updated': now_iso(),
    }

def recalculate_operational_goals(host, system=None, context_text='', mood=None) -> dict:
    system = system or load_goal_system(host)
    mood = mood or _mood(host)
    camera_state = _camera_state(host)
    health = _health(host)
    user_open = 1.0 - clamp01(safe_float(camera_state.get('busy_score', 0.0)))
    stress = clamp01(safe_float(health.get('stress', 0.0)))
    instability = clamp01(safe_float(health.get('instability', 0.0)))
    caring = clamp01(safe_float((mood or {}).get('care', 0.0)))
    curiosity = clamp01(safe_float((mood or {}).get('curiosity', 0.0)))
    blend = {
        'maintain_connection': 0.20 + user_open * 0.18 + caring * 0.12,
        'explore_topic': 0.14 + curiosity * 0.20,
        'increase_engagement': 0.12 + user_open * 0.10,
        'reduce_stress': 0.08 + stress * 0.30,
        'clarify': 0.07,
        'observe_silently': 0.06 + instability * 0.24,
        'wait_for_user': 0.06 + (1.0 - user_open) * 0.18,
    }
    total = sum(max(0.0, safe_float(v)) for v in blend.values()) or 1.0
    blend = {k: max(0.0, safe_float(v)) / total for k, v in blend.items()}
    active_goal, goal_strength = max(blend.items(), key=lambda kv: kv[1])
    system['goal_blend'] = blend
    system['active_goal'] = active_goal
    system['goal_strength'] = goal_strength
    system['meta_state'] = _compute_meta_control(host, system=system, mood=mood, camera_state=camera_state)
    system['last_updated'] = now_iso()
    return system
