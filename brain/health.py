from __future__ import annotations
from pathlib import Path
from .shared import clamp01, safe_float, now_iso, now_ts, iso_to_ts, atomic_json_save, json_load

def _health_path(host):
    p = host.get('HEALTH_STATE_PATH')
    if p:
        return str(p)
    return 'state/health_state.json'

def default_health_state(host=None):
    return {
        'overall': 'unknown', 'last_startup_check': '', 'last_runtime_check': '', 'last_light_check': '',
        'subsystems': {}, 'issues': [], 'history': [], 'startup_summary': '',
        'signals': {'instability': 0.0, 'overload': 0.0, 'error_load': 0.0, 'stress': 0.0, 'recovery': 1.0},
        'behavior_modifiers': {'initiative_scale': 1.0, 'confidence_scale': 1.0, 'support_bias': 0.0, 'silence_bias': 0.0, 'tone_caution': 0.0},
        'degraded_mode': 'none',
    }

def load_health_state(host):
    base = default_health_state(host)
    state = json_load(_health_path(host), base)
    for k, v in base.items():
        state.setdefault(k, v if not isinstance(v, dict) else dict(v))
    return state

def save_health_state(host, state: dict):
    base = default_health_state(host)
    base.update(state or {})
    atomic_json_save(_health_path(host), base)

def _severity_rank(sev):
    return {'info':0,'warning':1,'error':2,'critical':3}.get(str(sev or 'info').lower(),0)

def _health_overall_from_issues(issues):
    level = max([_severity_rank((i or {}).get('severity')) for i in (issues or [])] or [0])
    return {0:'healthy',1:'degraded',2:'error',3:'critical'}.get(level,'unknown')

def _issue(severity, subsystem, message):
    return {'severity': str(severity), 'subsystem': str(subsystem), 'message': str(message), 'ts': now_iso()}

def _camera_status(host):
    # Prefer the live frame buffer maintained by the persistent capture thread
    # (brain/background_ticks._video_frame_capture_thread). The legacy
    # CAMERA_LATEST_JSON_PATH path was a stale on-disk JSON that this loop
    # doesn't write any more — falling back to it caused false "error" reports.
    try:
        from brain.frame_store import peek_buffer_age_sec
        age = peek_buffer_age_sec()
        if age is not None:
            out = {'age_seconds': round(age, 1)}
            if age <= 5.0:
                out.update({'status': 'healthy', 'detail': f'fresh frame {round(age,1)}s ago'})
                return out, []
            if age <= 10.0:
                out.update({'status': 'degraded', 'detail': f'frame {round(age,1)}s ago'})
                return out, [_issue('warning', 'camera', out['detail'])]
            out.update({'status': 'error', 'detail': f'no frames for {round(age,1)}s'})
            return out, [_issue('error', 'camera', out['detail'])]
    except Exception:
        pass

    # Fallback: legacy CAMERA_LATEST_JSON_PATH check.
    path = host.get('CAMERA_LATEST_JSON_PATH')
    if not path:
        return {'status': 'warning', 'detail': 'camera latest path unavailable'}, [_issue('warning', 'camera', 'camera latest path unavailable')]
    p = Path(path)
    if not p.exists():
        # Frame buffer was empty AND no legacy path — capture thread hasn't run yet.
        return {'status': 'warning', 'detail': 'no captured frame yet'}, [_issue('warning', 'camera', 'no captured frame yet')]
    try:
        import json
        cam = json.loads(p.read_text(encoding='utf-8'))
        age = now_ts() - iso_to_ts((cam or {}).get('time'))
        out = {'age_seconds': round(age, 1)}
        stale = safe_float(host.get('HEALTH_CAMERA_STALE_SECONDS', 25.0), 25.0)
        light = safe_float(host.get('HEALTH_LIGHT_STALE_SECONDS', 120.0), 120.0)
        if age <= stale:
            out.update({'status': 'healthy', 'detail': f'fresh frame {round(age,1)}s ago'})
            return out, []
        if age <= light:
            out.update({'status': 'degraded', 'detail': f'stale frame {round(age,1)}s ago'})
            return out, [_issue('warning', 'camera', out['detail'])]
        out.update({'status': 'error', 'detail': f'very stale frame {round(age,1)}s ago'})
        return out, [_issue('error', 'camera', out['detail'])]
    except Exception as e:
        return {'status': 'error', 'detail': f'camera state unreadable: {e}'}, [_issue('error', 'camera', f'camera state unreadable: {e}')]

def _memory_status(host):
    fn = host.get('get_memory_status')
    detail = ''
    try:
        if callable(fn):
            detail = str(fn())
    except Exception as e:
        detail = f'error: {e}'
    status = 'healthy'
    issues = []
    low = detail.lower()
    if not detail:
        status = 'warning'; detail = 'memory status unavailable'; issues.append(_issue('warning','memory',detail))
    elif 'error' in low or 'unavailable' in low:
        status = 'error'; issues.append(_issue('error','memory',detail))
    return {'status': status, 'detail': detail}, issues

def _mood_status(host):
    fn = host.get('load_mood')
    try:
        mood = dict(fn() or {}) if callable(fn) else {}
        if mood:
            return {'status':'healthy','detail':'mood loaded'}, []
        return {'status':'warning','detail':'mood empty'}, [_issue('warning','mood','mood empty')]
    except Exception as e:
        return {'status':'error','detail':f'mood load failed: {e}'}, [_issue('error','mood',f'mood load failed: {e}')]

def _initiative_status(host):
    try:
        return {'status':'healthy','detail':'initiative available'}, []
    except Exception as e:
        return {'status':'error','detail':f'initiative unavailable: {e}'}, [_issue('error','initiative',f'initiative unavailable: {e}')]

def _models_status(host):
    issues=[]
    detail=[]
    if 'DeepFace' in host:
        detail.append('deepface')
    if 'Whisper' in str(host.get('__name__','')):
        detail.append('whisper?')
    return {'status':'healthy','detail':', '.join(detail) or 'models assumed available'}, issues

def _apply_decay_and_modifiers(state):
    sig = dict(state.get('signals') or {})
    sig['stress'] = clamp01(sig.get('stress',0.0) * 0.95)
    sig['error_load'] = clamp01(max(0.0, sig.get('error_load',0.0) - 0.05))
    sig['instability'] = clamp01(sig.get('instability',0.0) * 0.96)
    sig['overload'] = clamp01(sig.get('overload',0.0) * 0.96)
    sig['recovery'] = clamp01(1.0 - max(sig['instability'], sig['overload'], sig['error_load']))
    max_bad = max(sig['instability'], sig['overload'], sig['error_load'], sig['stress'])
    degraded = 'none'
    if max_bad >= 0.80:
        degraded = 'support_only'
    elif max_bad >= 0.60:
        degraded = 'low_initiative'
    elif max_bad >= 0.40:
        degraded = 'cautious'
    mods = {
        'initiative_scale': clamp01(1.0 - max_bad * 0.85),
        'confidence_scale': clamp01(1.0 - max_bad * 0.55),
        'support_bias': clamp01(sig['stress'] * 0.75 + sig['instability'] * 0.35),
        'silence_bias': clamp01(sig['overload'] * 0.70 + sig['error_load'] * 0.40),
        'tone_caution': clamp01(max_bad * 0.90),
    }
    state['signals'] = sig
    state['behavior_modifiers'] = mods
    state['degraded_mode'] = degraded
    return state

def run_system_health_check(host, kind='runtime'):
    issues=[]; subsystems={}
    for name, fn in [('camera', _camera_status), ('memory', _memory_status), ('mood', _mood_status), ('initiative', _initiative_status), ('models', _models_status)]:
        status, found = fn(host)
        subsystems[name]=status
        issues.extend(found)
    overall = _health_overall_from_issues(issues)
    state = load_health_state(host)
    state['overall']=overall
    now = now_iso()
    if kind=='startup': state['last_startup_check']=now
    elif kind=='runtime': state['last_runtime_check']=now
    else: state['last_light_check']=now
    state['issues']=issues[:20]
    state['subsystems']=subsystems
    hist = list(state.get('history') or [])
    hist.append({'kind': kind, 'ts': now, 'overall': overall, 'issues': issues[:8]})
    state['history']=hist[-30:]
    # reinforce strain from current issues before decay/recovery
    sig = dict(state.get('signals') or {})
    severity = max([_severity_rank(i.get('severity')) for i in issues] or [0])
    sig['error_load'] = clamp01(sig.get('error_load',0.0) + 0.18 * severity)
    sig['instability'] = clamp01(sig.get('instability',0.0) + (0.10 if overall in {'degraded','error','critical'} else -0.04))
    sig['overload'] = clamp01(sig.get('overload',0.0) + (0.08 if len(issues) >= 3 else -0.03))
    sig['stress'] = clamp01(sig.get('stress',0.0) + (0.06 if overall in {'error','critical'} else -0.02))
    state['signals'] = sig
    _apply_decay_and_modifiers(state)
    state['startup_summary'] = f"{state['overall'].upper()} | " + ', '.join(f"{k}:{(v or {}).get('status','unknown')}" for k, v in subsystems.items())
    save_health_state(host, state)

    # Task 2 (2026-05-02): sensor → emotion pipeline. For each issue
    # detected this tick, fire a SUBSYSTEM_DEGRADED signal AND apply the
    # corresponding emotion bump. This closes the gap where camera dying
    # / tool failures / model errors didn't shift Ava's mood unless she
    # verbalized them. Skip info-severity issues — they're noise.
    try:
        from brain.signal_bus import get_signal_bus, SIGNAL_SUBSYSTEM_DEGRADED
        bus = get_signal_bus()
        emotion_fn = host.get("update_internal_emotions_from_subsystem") if isinstance(host, dict) else getattr(host, "update_internal_emotions_from_subsystem", None)
        for issue in issues:
            sev = str(issue.get("severity") or "info").lower()
            if sev == "info":
                continue  # noise
            subsystem = str(issue.get("subsystem") or "unknown")
            message = str(issue.get("message") or "")[:200]
            if bus is not None:
                priority = "high" if sev == "critical" else ("medium" if sev == "error" else "low")
                bus.fire(
                    SIGNAL_SUBSYSTEM_DEGRADED,
                    {"subsystem": subsystem, "severity": sev, "message": message, "kind": kind},
                    priority=priority,
                )
            if callable(emotion_fn):
                try:
                    emotion_fn(subsystem, sev, message)
                except Exception as e:
                    print(f"[health] emotion bump for {subsystem} failed: {e!r}")
    except Exception as e:
        print(f"[health] sensor→emotion wire-up error: {e!r}")

    return state

def print_startup_health(host):
    state = run_system_health_check(host, kind='startup')
    try:
        print(f"Startup health: {state.get('startup_summary','UNKNOWN')}")
    except Exception:
        pass
    return state
