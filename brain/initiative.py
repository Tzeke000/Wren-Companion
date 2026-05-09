from __future__ import annotations
from .attention import AttentionState
from .shared import clamp01, safe_float, latest_user_text, normalize_history
from .beliefs import build_belief_state
from .goals import load_goal_system, recalculate_operational_goals

def _history(host):
    fn = host.get('_get_canonical_history')
    if callable(fn):
        try: return normalize_history(fn() or [])
        except Exception: return []
    return []

def _health(host):
    fn = host.get('load_health_state')
    if callable(fn):
        try: return dict(fn() or {})
        except Exception: return {}
    return {}

def choose_initiative_candidate(host, person_id: str, expression_state=None, attention_state: AttentionState | None = None):
    if attention_state is not None and not attention_state.should_speak:
        return None, attention_state.suppression_reason, {}
    history = _history(host)
    belief_state = build_belief_state(host, history=history, expression_state=expression_state)
    system = recalculate_operational_goals(host, load_goal_system(host))
    health = _health(host)
    mods = dict(health.get('behavior_modifiers') or {})
    initiative_scale = clamp01(mods.get('initiative_scale', 1.0))
    confidence_scale = clamp01(mods.get('confidence_scale', 1.0))
    active_goal = system.get('active_goal', 'maintain_connection')
    top_belief = belief_state.get('top_belief', 'none')
    base_score = 0.18 + safe_float(system.get('goal_strength', 0.3)) * 0.55 + safe_float(belief_state.get('top_confidence', 0.0)) * 0.18
    debug = {'belief_state': belief_state, 'health_mode': health.get('degraded_mode', 'none')}
    if top_belief == 'topic_closed_or_softly_redirected':
        return None, 'user_closed_topic', debug
    if top_belief == 'user_requests_self_state':
        return {'kind': 'self_state_answer', 'score': base_score, 'topic': 'self_state', 'style_hint': 'grounded and honest'}, 'belief_trigger', debug
    if top_belief == 'user_seeks_visual_confirmation':
        return {'kind': 'visual_confirmation', 'score': base_score, 'topic': 'camera_presence', 'style_hint': 'direct and perceptual'}, 'belief_trigger', debug
    if initiative_scale < 0.35:
        return None, 'health_low_initiative', debug
    last_user = latest_user_text(history)
    if last_user:
        return {
            'kind': 'current_goal', 'goal': active_goal,
            'score': base_score * (0.65 + initiative_scale * 0.35),
            'topic': last_user[:120],
            'style_hint': 'natural and concise' if confidence_scale < 0.8 else 'engaged',
        }, 'latest_user_context', debug
    return None, 'no_candidate', debug
