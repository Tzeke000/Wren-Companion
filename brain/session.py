"""
brain/session.py — Session, initiative, and expression state I/O.

Extracted from avaagent.py. Uses deferred avaagent import so the
path constants (SESSION_STATE_PATH, INITIATIVE_STATE_PATH, etc.)
are resolved at call time after avaagent is fully loaded.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _paths() -> dict[str, Path]:
    """Return avaagent path constants (called at runtime, not import time)."""
    import avaagent as _av
    return {
        "SESSION_STATE_PATH": _av.SESSION_STATE_PATH,
        "INITIATIVE_STATE_PATH": _av.INITIATIVE_STATE_PATH,
        "EXPRESSION_STATE_PATH": _av.EXPRESSION_STATE_PATH,
    }


# ─────────────── expression state ───────────────

def default_expression_state() -> dict[str, Any]:
    return {
        "dominant_expression": "neutral",
        "confidence": 0.0,
        "recent_expressions": [],
        "expression_history": [],
        "last_updated": "",
        "stability_score": 0.0,
    }


def load_expression_state() -> dict[str, Any]:
    path = _paths()["EXPRESSION_STATE_PATH"]
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default_expression_state()


def save_expression_state(state: dict[str, Any]) -> None:
    path = _paths()["EXPRESSION_STATE_PATH"]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Expression state save error: {e}")


# ─────────────── initiative state ───────────────

def default_initiative_state() -> dict[str, Any]:
    return {
        "last_face_seen_ts": 0.0,
        "last_interaction_ts": 0.0,
        "last_initiation_ts": 0.0,
        "last_initiation_message": "",
        "last_initiated_topic": "",
        "last_presence_reason": "none",
        "presence_score": 0.0,
        "last_busy_score": 0.0,
        "recent_initiated_topics": {},
        "recent_initiated_texts": [],
        "recent_candidate_kinds": [],
        "recent_active_goals": [],
        "pending_initiation": None,
        "initiative_history": [],
        "consecutive_ignored_initiations": 0,
        "last_user_message_length": 0,
        "last_user_response_brief": False,
        "interaction_energy": 0.58,
        "face_visible": False,
        "face_left_at": None,
        "face_returned_at": None,
        "was_absent": False,
        "absent_duration_seconds": 0,
        "last_calibration_at_msg": 0,
    }


def load_initiative_state() -> dict[str, Any]:
    path = _paths()["INITIATIVE_STATE_PATH"]
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                base = default_initiative_state()
                loaded = json.load(f)
                base.update(loaded if isinstance(loaded, dict) else {})
                return base
        except Exception:
            pass
    return default_initiative_state()


def save_initiative_state(state: dict[str, Any]) -> None:
    path = _paths()["INITIATIVE_STATE_PATH"]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Initiative state save error: {e}")


# ─────────────── session state ───────────────

_SESSION_STATE_MIGRATED = False


def load_session_state() -> dict[str, Any]:
    global _SESSION_STATE_MIGRATED
    pp = _paths()
    SESSION_STATE_PATH = pp["SESSION_STATE_PATH"]
    INITIATIVE_STATE_PATH = pp["INITIATIVE_STATE_PATH"]

    import avaagent as _av
    now_iso = _av.now_iso

    base: dict[str, Any] = {
        "total_message_count": 0,
        "session_start_at": "",
        "last_session_end_at": "",
    }
    if SESSION_STATE_PATH.exists():
        try:
            with open(SESSION_STATE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                base.update(loaded)
        except Exception:
            pass
    if not _SESSION_STATE_MIGRATED:
        _SESSION_STATE_MIGRATED = True
        try:
            if INITIATIVE_STATE_PATH.exists():
                with open(INITIATIVE_STATE_PATH, "r", encoding="utf-8") as f:
                    init_raw = json.load(f)
                if isinstance(init_raw, dict) and "total_message_count" in init_raw:
                    ic = int(init_raw.get("total_message_count") or 0)
                    cur = int(base.get("total_message_count") or 0)
                    base["total_message_count"] = max(cur, ic)
                    istate = load_initiative_state()
                    if "total_message_count" in istate:
                        del istate["total_message_count"]
                        save_initiative_state(istate)
                    save_session_state(base)
        except Exception:
            pass
    if not base.get("session_start_at"):
        base["session_start_at"] = now_iso()
    return base


def save_session_state(state: dict[str, Any]) -> None:
    path = _paths()["SESSION_STATE_PATH"]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Session state save error: {e}")


def bump_session_message_count() -> int:
    st = load_session_state()
    n = int(st.get("total_message_count", 0)) + 1
    st["total_message_count"] = n
    save_session_state(st)
    return n
