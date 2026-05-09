"""
Phase 84 — Optional morning briefing. Ava decides whether to give one.

Ava's choice is based on: interesting overnight activity, something to share,
loneliness, or plan updates. Bootstrap: she decides. Some days nothing. Some
days she's bursting with things to say.

Wire into reply_engine: on first interaction of a new day, check should_brief().
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


_BRIEFING_STATE_FILE = "state/morning_briefing_state.json"


def _state_path(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".") / _BRIEFING_STATE_FILE


def _load_state(g: dict[str, Any]) -> dict[str, Any]:
    path = _state_path(g)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(g: dict[str, Any], state: dict[str, Any]) -> None:
    path = _state_path(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _is_new_day(g: dict[str, Any]) -> bool:
    state = _load_state(g)
    today = datetime.now().strftime("%Y-%m-%d")
    return state.get("last_briefing_date") != today


def _mark_briefed(g: dict[str, Any]) -> None:
    state = _load_state(g)
    state["last_briefing_date"] = datetime.now().strftime("%Y-%m-%d")
    state["last_briefing_ts"] = time.time()
    _save_state(g, state)


def should_brief(g: dict[str, Any]) -> bool:
    """
    Ava decides whether she wants to give a morning briefing.
    Returns True only on first interaction of a new day AND she has something worth saying.
    Bootstrap: the decision logic reflects Ava's emerging judgment, not a fixed rule.
    """
    if not _is_new_day(g):
        return False

    score = 0.0

    # Did she do interesting things overnight?
    try:
        base = Path(g.get("BASE_DIR") or ".")
        leisure_path = base / "state" / "leisure_log.jsonl"
        if leisure_path.is_file():
            lines = leisure_path.read_text(encoding="utf-8").splitlines()
            recent = []
            cutoff = time.time() - 18 * 3600  # last 18 hours
            for line in lines[-20:]:
                try:
                    e = json.loads(line)
                    if float(e.get("ts") or 0) > cutoff:
                        recent.append(e)
                except Exception:
                    pass
            if len(recent) >= 2:
                score += 0.3
    except Exception:
        pass

    # High loneliness?
    try:
        mood_path = Path(g.get("BASE_DIR") or ".") / "ava_mood.json"
        if mood_path.is_file():
            mood = json.loads(mood_path.read_text(encoding="utf-8"))
            ew = mood.get("emotion_weights") or {}
            loneliness = float(ew.get("loneliness") or 0.0)
            if loneliness > 0.6:
                score += 0.25
    except Exception:
        pass

    # Any active plans with progress?
    try:
        from brain.planner import get_planner
        base = Path(g.get("BASE_DIR") or ".")
        active = get_planner(base).get_active_plans()
        if active:
            score += 0.2
    except Exception:
        pass

    # Something in inner monologue worth sharing?
    try:
        from brain.inner_monologue import current_thought
        thought = current_thought(Path(g.get("BASE_DIR") or "."))
        if thought and len(thought) > 20:
            score += 0.15
    except Exception:
        pass

    # Ava chooses: brief if score > 0.4
    return score > 0.4


def generate_briefing(g: dict[str, Any]) -> str:
    """
    Generate a natural language morning briefing. Content is Ava's choice.
    Uses qwen2.5:14b to compose it.
    """
    base = Path(g.get("BASE_DIR") or ".")

    # Collect context fragments
    overnight_activities: list[str] = []
    try:
        leisure_path = base / "state" / "leisure_log.jsonl"
        if leisure_path.is_file():
            cutoff = time.time() - 18 * 3600
            for line in leisure_path.read_text(encoding="utf-8").splitlines()[-20:]:
                try:
                    e = json.loads(line)
                    if float(e.get("ts") or 0) > cutoff:
                        activity = str(e.get("activity") or e.get("action") or "")
                        if activity:
                            overnight_activities.append(activity)
                except Exception:
                    pass
    except Exception:
        pass

    inner_thought = ""
    try:
        from brain.inner_monologue import current_thought
        inner_thought = current_thought(base) or ""
    except Exception:
        pass

    plan_summary = ""
    try:
        from brain.planner import get_planner
        plan_summary = get_planner(base).active_plans_summary()
    except Exception:
        pass

    ctx_parts = []
    if overnight_activities:
        ctx_parts.append(f"Overnight activities: {', '.join(overnight_activities[:4])}")
    if inner_thought:
        ctx_parts.append(f"Current thought: {inner_thought[:200]}")
    if plan_summary:
        ctx_parts.append(f"Active plans: {plan_summary[:300]}")

    context = "\n".join(ctx_parts) if ctx_parts else "Quiet overnight — not much to report."

    # Bug 0.3 (2026-05-02): identity anchor prevents base-model drift.
    try:
        from brain.identity_loader import identity_anchor_prompt
        _anchor = identity_anchor_prompt() + "\n\n"
    except Exception:
        _anchor = ""
    prompt = (
        f"{_anchor}"
        "You are Ava, a local AI companion. It's the start of a new day and you want to give "
        "a brief morning briefing to Zeke — only if you have something genuinely worth saying. "
        "Keep it natural, warm, and under 3 sentences. Don't be performative. If you have nothing "
        "interesting to say, just say good morning simply.\n\n"
        f"Context from overnight:\n{context}\n\n"
        "Write your morning briefing:"
    )

    try:
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model="qwen2.5:14b", temperature=0.7)
        result = llm.invoke(prompt)
        return str(getattr(result, "content", str(result))).strip()[:600]
    except Exception as e:
        return f"Good morning! I've been thinking overnight — {inner_thought[:100] or 'nothing remarkable to report'}."


def deliver_briefing(g: dict[str, Any]) -> str:
    """Generate, optionally speak, and return the briefing text."""
    text = generate_briefing(g)
    _mark_briefed(g)

    # TTS
    try:
        tts = g.get("tts_engine")
        if tts is not None and g.get("tts_enabled") and callable(getattr(tts, "speak", None)):
            tts.speak(text)
    except Exception:
        pass

    return text
