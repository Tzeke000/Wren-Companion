from __future__ import annotations
import json, os, tempfile, time
from copy import deepcopy
from pathlib import Path

def clamp01(v):
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return 0.0

def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return float(default)

def now_ts():
    return time.time()

def now_iso():
    try:
        from datetime import datetime
        return datetime.now().isoformat(timespec='seconds')
    except Exception:
        return ''

def iso_to_ts(value):
    if not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0

def deepcopy_jsonable(v):
    try:
        return deepcopy(v)
    except Exception:
        try:
            return json.loads(json.dumps(v))
        except Exception:
            return v

def json_load(path, default):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        pass
    return deepcopy_jsonable(default)

def atomic_json_save(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.stem + '_', suffix='.tmp', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def extract_text(obj):
    if obj is None:
        return ''
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if 'content' in obj:
            return extract_text(obj.get('content'))
        if obj.get('type') == 'text' and 'text' in obj:
            return str(obj.get('text') or '')
        if 'text' in obj:
            return str(obj.get('text') or '')
        return ' '.join(extract_text(v) for v in obj.values() if isinstance(v, (str, dict, list)))
    if isinstance(obj, list):
        return ' '.join(x for x in (extract_text(i) for i in obj) if x).strip()
    return str(obj)

def latest_user_text(history):
    for item in reversed(history or []):
        if isinstance(item, dict) and str(item.get('role','')).lower() == 'user':
            text = extract_text(item.get('content','')).strip()
            if text:
                return text
    return ''

def jaccard(a, b):
    sa = set(str(a).lower().split())
    sb = set(str(b).lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))

def normalize_history(history):
    out=[]
    for item in history or []:
        if isinstance(item, dict):
            role = str(item.get('role') or item.get('speaker') or '').lower() or 'user'
            content = extract_text(item.get('content', item.get('text', ''))).strip()
            if content:
                out.append({'role': role, 'content': content})
    return out
