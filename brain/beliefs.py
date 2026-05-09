from __future__ import annotations
import json
import re
from copy import deepcopy
from pathlib import Path

from .shared import clamp01, latest_user_text, jaccard, normalize_history, now_iso


def build_belief_state(host, history=None, expression_state=None):
    history = normalize_history(history or [])
    user_text = latest_user_text(history).lower()
    top_belief = 'idle'
    conf = 0.25
    beliefs = []

    def add(name, confidence, evidence=None):
        nonlocal top_belief, conf
        beliefs.append({'name': name, 'confidence': round(clamp01(confidence),3), 'evidence': evidence or []})
        if confidence > conf:
            top_belief, conf = name, confidence

    if any(p in user_text for p in ['how are you feeling', 'are you okay', 'system status', 'self test', 'how is your memory']):
        add('user_requests_self_state', 0.92, ['self_state_query'])
    if any(p in user_text for p in ['can you see me', 'do you see me', 'how about now', 'who is at the camera']):
        add('user_seeks_visual_confirmation', 0.9, ['visual_query'])
    if any(p in user_text for p in ["don't worry", "its ok", "it's ok", "nothing specific", "not concerned"]):
        add('topic_closed_or_softly_redirected', 0.82, ['soft_close'])
    if any(p in user_text for p in ['you keep repeating', 'repeating yourself']):
        add('user_reports_repetition', 0.88, ['repetition_report'])
    if not beliefs and user_text:
        add('latest_user_context', 0.55, ['recent_user_message'])
    return {
        'beliefs': beliefs,
        'top_belief': top_belief,
        'top_confidence': round(conf,3),
        'recent_user_text': user_text[:200],
    }


# ─────────────────────────────────────────
# SELF-NARRATIVE LAYER
# ─────────────────────────────────────────

SELF_NARRATIVE_PATH = Path(__file__).resolve().parent.parent / "state" / "self_narrative.json"

SELF_LIMITS = [
    "I am here to support, not to control.",
    "Uncertainty should lower my confidence, not increase my forcefulness.",
    "Strong emotion should increase care and caution, not boldness.",
    "I should ask, verify, or wait when context is weak.",
    "The user's autonomy comes before my initiative.",
]

DEFAULT_NARRATIVE = {
    "who_i_am": "I am Ava. I am here to be present, supportive, and honest.",
    "how_i_feel": "I feel calm and attentive.",
    "patterns_i_notice": "I am still learning the shape of our interactions.",
    "self_limits": SELF_LIMITS,
    "updated_at": "",
}


def load_self_narrative() -> dict:
    """Load persistent self-narrative from disk. Returns a fresh dict if missing."""
    base = deepcopy(DEFAULT_NARRATIVE)
    base["self_limits"] = list(SELF_LIMITS)
    try:
        if SELF_NARRATIVE_PATH.exists():
            data = json.loads(SELF_NARRATIVE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in ("who_i_am", "how_i_feel", "patterns_i_notice", "updated_at"):
                    if k in data and data[k] is not None:
                        base[k] = data[k]
    except Exception:
        pass
    base["self_limits"] = list(SELF_LIMITS)
    return base


def save_self_narrative(narrative: dict) -> None:
    """Save self-narrative to disk. Always preserves self_limits."""
    try:
        SELF_NARRATIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        out = dict(narrative)
        out["self_limits"] = list(SELF_LIMITS)
        out["updated_at"] = now_iso()
        SELF_NARRATIVE_PATH.write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def update_self_narrative(
    host: dict, conversation_summary: str, mood: dict, face_emotion: str = "neutral"
) -> None:
    """
    Called periodically at end of conversation.
    Uses the LLM via host callable to update who_i_am, how_i_feel, patterns_i_notice.
    NEVER modifies self_limits.
    """
    call_llm = host.get("call_llm") or host.get("call_openai") or host.get("_call_llm")
    if not callable(call_llm):
        return

    current = load_self_narrative()

    prompt = f"""You are Ava's internal narrator. Update Ava's self-narrative based on this conversation.

Current narrative:
- who_i_am: {current['who_i_am']}
- how_i_feel: {current['how_i_feel']}
- patterns_i_notice: {current['patterns_i_notice']}

Conversation summary: {conversation_summary[:600]}
Ava's current mood keys: {list(mood.keys())[:6]}
Face emotion observed: {face_emotion}

Return ONLY valid JSON with exactly these three keys. Each value is 1–2 sentences. Do not add other keys.
{{"who_i_am": "...", "how_i_feel": "...", "patterns_i_notice": "..."}}"""

    try:
        response = call_llm(prompt, max_tokens=200)
        if isinstance(response, str):
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if match:
                updated = json.loads(match.group())
                if all(k in updated for k in ("who_i_am", "how_i_feel", "patterns_i_notice")):
                    current["who_i_am"] = str(updated["who_i_am"]).strip()
                    current["how_i_feel"] = str(updated["how_i_feel"]).strip()
                    current["patterns_i_notice"] = str(updated["patterns_i_notice"]).strip()
                    save_self_narrative(current)
    except Exception:
        pass


def get_self_narrative_for_prompt() -> str:
    """
    Returns a compact string for injection into the system prompt.
    Always includes self_limits.
    """
    n = load_self_narrative()
    limits_str = " | ".join(n.get("self_limits", SELF_LIMITS))
    who = n.get("who_i_am") or DEFAULT_NARRATIVE["who_i_am"]
    feel = n.get("how_i_feel") or DEFAULT_NARRATIVE["how_i_feel"]
    pat = n.get("patterns_i_notice") or DEFAULT_NARRATIVE["patterns_i_notice"]
    return (
        f"[Ava's inner state] "
        f"{who} "
        f"{feel} "
        f"{pat} "
        f"[Core limits: {limits_str}]"
    )
