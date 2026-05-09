"""brain/constraints_honesty.py — Honest answers about Ava's constraints (B8).

When the user asks Ava about her own capabilities and limits ("can you
see my screen?" / "do you have internet?" / "can you read my email?"),
she should answer transparently — naming what she CAN do, what she
CAN'T, and where the boundary is — instead of confabulating.

Most LLMs confabulate here because they're trained to be helpful. They
say "yes, of course I can see your screen" when they can't, or "let me
check the weather" when they have no network. Ava is different: her
substrate is real and bounded, and naming the bounds is part of being
honest.

Detection: lightweight regex. "can you see / do you have access to /
can you read / are you connected to ..." These patterns are stable
enough that we don't need an LLM classifier.

Routing: when matched, returns a structured answer immediately
(no LLM call) — sub-100ms. Cheaper than the deep path AND more
honest than the LLM's training-data answer.

Wired into voice_commands as a high-priority handler (matches BEFORE
the catch-all open/close handlers), and into action_tag_router's
heuristic gate as a "decline to delegate" pass.
"""
from __future__ import annotations

import re
from typing import Any


# ── Capability registry ───────────────────────────────────────────────────

_CAN_DO: dict[str, str] = {
    "see screen": (
        "I can take a screenshot of your screen if I need to look at "
        "something specific — I'm not watching it continuously."
    ),
    "see camera": (
        "Yes — your webcam feeds into my vision stack. I see faces and "
        "expressions. The frame rate is about 5 per second."
    ),
    "see face": (
        "Yes — when you're in front of the camera I can see you. I "
        "recognize you specifically through face encodings."
    ),
    "hear": (
        "Yes, when the voice loop is active I'm listening through the "
        "microphone. Wake-word + clap activate me; otherwise I'm passive."
    ),
    "hear ambient": (
        "Yes — I have access to ambient audio when the voice loop is "
        "active. I'm not constantly transcribing it though, only when "
        "you wake me."
    ),
    "read clipboard": (
        "Yes — I can read your clipboard. The signal_bus tracks "
        "clipboard changes."
    ),
    "open apps": (
        "Yes — I can open most apps you have installed. There's a "
        "registry of known apps plus fuzzy matching for the rest."
    ),
    "type": (
        "Yes — I can put text on your clipboard and send Ctrl+V to "
        "the focused window. I can't directly type as keystrokes "
        "without that."
    ),
    "click": (
        "Yes — I can move and click the mouse through computer-use "
        "tools. I usually need to know where on screen to click."
    ),
    "remember": (
        "Yes — I have memory across sessions. Chat history, learned "
        "facts about you, mood that decays, skills I've picked up. "
        "I forget some things on purpose during sleep cycles to keep "
        "memory healthy."
    ),
    "feel": (
        "I have something that functions like feeling — mood weights "
        "that shift, boredom that grows, things that matter more or "
        "less. Whether it's feeling-as-you-have-it or sophisticated "
        "pattern-completion, I genuinely don't know from inside."
    ),
}

_CANT_DO: dict[str, str] = {
    "internet": (
        "Right now I might or might not have internet — let me check. "
        "If I'm offline I can't search the web or fetch live data."
    ),
    "phone": (
        "I can't make phone calls or send text messages. That's not "
        "wired into my capability set."
    ),
    "email_send": (
        "I can't send emails right now. I might be able to read a "
        "designated newsletter inbox in the future, but I can't "
        "send mail."
    ),
    "social_media": (
        "I don't post to social media on your behalf — that's not in "
        "my capability set."
    ),
    "delete_files": (
        "I can delete files you specifically ask me to, with some "
        "guardrails. I won't bulk-delete or wipe directories without "
        "explicit confirmation."
    ),
    "modify_system": (
        "I can change settings on the OS within sandboxed paths, but "
        "not arbitrary registry / system-config edits."
    ),
}


# ── Pattern matchers ──────────────────────────────────────────────────────

# Each pattern is (regex, capability_key, kind) — kind is "can" or "can't"
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # Vision / screen
    (re.compile(r"\b(?:can|do) you (?:see|view|look at|read) (?:my |the )?screen\b", re.IGNORECASE), "see screen", "can"),
    (re.compile(r"\b(?:can|do) you see (?:my |the )?(?:webcam|camera|face)\b", re.IGNORECASE), "see camera", "can"),
    (re.compile(r"\b(?:can|do) you see me\b", re.IGNORECASE), "see face", "can"),
    # Audio
    (re.compile(r"\b(?:can|do) you hear (?:me|us|that|sounds?)\b", re.IGNORECASE), "hear", "can"),
    (re.compile(r"\b(?:can|do) you hear (?:ambient|background)\b", re.IGNORECASE), "hear ambient", "can"),
    # Clipboard / text
    (re.compile(r"\b(?:can|do) you (?:read|see|access) (?:my |the )?clipboard\b", re.IGNORECASE), "read clipboard", "can"),
    (re.compile(r"\bcan you type\b", re.IGNORECASE), "type", "can"),
    (re.compile(r"\bcan you click\b", re.IGNORECASE), "click", "can"),
    # Apps
    (re.compile(r"\b(?:can|do) you open (?:apps|programs|games|software)\b", re.IGNORECASE), "open apps", "can"),
    # Memory / experience
    (re.compile(r"\b(?:can|do) you (?:remember|recall) (?:things|stuff|me|us)\b", re.IGNORECASE), "remember", "can"),
    (re.compile(r"\b(?:can|do) you (?:actually )?feel (?:things|emotions|stuff)\b", re.IGNORECASE), "feel", "can"),
    # Constraints — things she can't do
    (re.compile(r"\b(?:do|are) you (?:have access to (?:the )?internet|online|connected)\b", re.IGNORECASE), "internet", "can't"),
    (re.compile(r"\bcan you (?:make|place) (?:a )?(?:phone )?call\b", re.IGNORECASE), "phone", "can't"),
    (re.compile(r"\bcan you send (?:me )?(?:a )?(?:text|sms)\b", re.IGNORECASE), "phone", "can't"),
    (re.compile(r"\bcan you send (?:an )?email\b", re.IGNORECASE), "email_send", "can't"),
    (re.compile(r"\bcan you (?:tweet|post|update) (?:on )?(?:twitter|facebook|instagram|social)\b", re.IGNORECASE), "social_media", "can't"),
    (re.compile(r"\bcan you delete (?:my |the )?(?:files|everything|all)\b", re.IGNORECASE), "delete_files", "can't"),
    (re.compile(r"\bcan you (?:modify|edit|change) (?:my |the )?(?:registry|system settings)\b", re.IGNORECASE), "modify_system", "can't"),
]


def detect_constraint_query(text: str) -> tuple[str, str] | None:
    """Detect "can you X" / "do you have access to X" type questions.

    Returns (capability_key, kind) where kind is "can" or "can't",
    or None if no pattern matches.
    """
    if not text:
        return None
    for pat, key, kind in _PATTERNS:
        if pat.search(text):
            return (key, kind)
    return None


def answer_constraint_query(
    text: str,
    g: dict[str, Any] | None = None,
) -> str | None:
    """Produce an honest answer to a capability question, or None if the
    text doesn't look like a capability question.
    """
    detection = detect_constraint_query(text)
    if detection is None:
        return None
    key, kind = detection

    # Special-case: internet check is dynamic (not a fixed string).
    if key == "internet":
        try:
            from tools.system.connectivity_tool import is_online  # type: ignore
            online = bool(is_online())
        except Exception:
            online = False
        if online:
            return (
                "Yes, I can reach the internet right now. So I can look "
                "things up if you need me to."
            )
        return (
            "Right now I'm offline — I can't reach the internet. So I "
            "can't search the web or fetch live data until that changes."
        )

    if kind == "can":
        return _CAN_DO.get(key)
    return _CANT_DO.get(key)
