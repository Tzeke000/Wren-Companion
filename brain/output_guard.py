import re

_INTERNAL_BLOCK_PATTERNS = [
    re.compile(r"```\s*(?:MEMORY|GOAL|ACTION|WORKBENCH|REFLECTION|DEBUG)[\s\S]*?```", re.IGNORECASE),
    re.compile(r"`\s*(?:MEMORY|GOAL|ACTION|WORKBENCH)[\s\S]*?`", re.IGNORECASE),
    re.compile(r"\*\*(?:MEMORY|GOAL|ACTION|WORKBENCH)\*\*[\s\S]*?(?=(?:\n\s*\n)|$)", re.IGNORECASE),
    re.compile(r"(?im)^\s*(?:MEMORY|GOAL|ACTION|WORKBENCH)\s*:?[ \t]*$[\s\S]*?(?=(?:\n\s*\n)|$)"),
    re.compile(r"(?im)^\s*(?:MEMORY|GOAL|ACTION|WORKBENCH)\s+action\s*:[\s\S]*?(?=(?:\n\s*\n)|$)"),
    re.compile(r"(?im)^\s*(?:MEMORY|GOAL|ACTION|WORKBENCH).*?$"),
    re.compile(r"Active goal expression:[^\n]+", re.IGNORECASE),
    re.compile(r"\bACTIVE PERSON\s*:[\s\S]*?(?=\n\s*\n|$)", re.IGNORECASE),
    re.compile(r"\bMEMORY\s+action\s*:.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(
        r"\b(?:MEMORY|GOAL|ACTION|WORKBENCH)\s+\w+\s*:.*?(?:category|importance|tags|text)\s*:.*$",
        re.IGNORECASE | re.MULTILINE,
    ),
]

def scrub_visible_reply(text: str) -> str:
    if not isinstance(text, str):
        return text
    cleaned = text
    for pat in _INTERNAL_BLOCK_PATTERNS:
        cleaned = pat.sub("", cleaned)
    # Strip inner-monologue 💭 lines — these belong on the snapshot's
    # inner_life.current_thought surface (rendered as text under the orb
    # in the UI), not in the chat reply or TTS playback. Hardware test
    # 2026-04-30 night: the chat showed
    #   💭 "Wondering if repetition in conversation often stems..."
    # appended to Ava's reply and being spoken out loud.
    cleaned = "\n".join(ln for ln in cleaned.splitlines() if "💭" not in ln)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if cleaned and cleaned[-1] not in '.!?"\'':
        tail = cleaned.rsplit('\n', 1)[-1]
        if len(tail.split()) <= 8:
            cleaned = cleaned[: -len(tail)].rstrip()
    return cleaned.strip() or "I'm here."


def scrub_history(history):
    if not isinstance(history, list):
        return history
    out = []
    for item in history:
        if isinstance(item, dict):
            new_item = dict(item)
            if isinstance(new_item.get('content'), str):
                new_item['content'] = scrub_visible_reply(new_item['content'])
            out.append(new_item)
        else:
            out.append(item)
    return out


def _coerce_chat_messages(history):
    """Gradio Chatbot(type='messages') expects list[dict] with role + content; convert legacy tuples."""
    if not isinstance(history, list):
        return history
    out = []
    for item in history:
        if isinstance(item, dict):
            role = str(item.get("role") or "assistant")
            raw = item.get("content", "")
            if isinstance(raw, str):
                content = raw
            elif isinstance(raw, list):
                content = " ".join(
                    str((part or {}).get("text", part) if isinstance(part, dict) else part)
                    for part in raw
                ).strip()
            else:
                content = str(raw or "")
            out.append({"role": role, "content": content})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append(
                {
                    "role": str(item[0] or "user"),
                    "content": str(item[1] if item[1] is not None else ""),
                }
            )
    return out


def scrub_chat_callback_result(result):
    if not isinstance(result, tuple):
        return result
    items = list(result)
    # Only scrub chat transcript (index 0). Status strings and the cleared input (index 1)
    # are not model replies — scrub_visible_reply would mangle or replace them with "I'm here."
    if items and isinstance(items[0], list):
        items[0] = scrub_history(_coerce_chat_messages(items[0]))
    return tuple(items)
