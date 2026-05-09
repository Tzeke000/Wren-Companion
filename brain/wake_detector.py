"""
Wake-word / direct-address detector.

Distinguishes three cases for any transcribed phrase:

  Clap-triggered  — Zeke double-clapped to wake Ava. Always direct, never
                    classified. Returns (True, 1.0, "clap_triggered").
  Direct address  — Zeke is talking TO Ava ("Hey Ava, what's up?")
  Indirect mention — Zeke is talking ABOUT Ava ("Ava said something funny")

Falls back to gaze + attention context when phrasing is ambiguous: if Zeke
is looking at the screen and focused, the bias is toward direct; if he's
looking away or absent, indirect.

Whisper sometimes mishears "Ava" as "Eva" / "Aye va" / "A va". We normalize
those to "Ava" before pattern matching so the wake fires for natural speech.

Returns confidence so the wake learner can ask for clarification on
borderline cases.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Optional


# ── Name normalization ────────────────────────────────────────────────────────
# Whisper variants that should be treated as "ava". We replace before regex
# matching so all the existing patterns work unchanged.

_NAME_NORMALIZATIONS: list[tuple[re.Pattern[str], str]] = [
    # "Eva" or "eva" as a standalone word with optional comma/punctuation.
    (re.compile(r"\beva([,.!?]|\b)", re.IGNORECASE), r"ava\1"),
    # "Aye va" (two words) → "Ava"
    (re.compile(r"\baye[ \-]?va\b", re.IGNORECASE), "ava"),
    # "A va" (split with space) → "Ava"
    (re.compile(r"\ba va\b", re.IGNORECASE), "ava"),
    # "Ada" only when right after a wake greeting — Whisper occasionally hears this.
    (re.compile(r"\b(hey|hi|hello|yo|okay|ok)\s+ada\b", re.IGNORECASE), r"\1 ava"),
]


def normalize_name(text: str) -> str:
    """Substitute common Whisper mishearings of 'Ava' before classification."""
    if not text:
        return ""
    out = text
    for pat, repl in _NAME_NORMALIZATIONS:
        out = pat.sub(repl, out)
    return out


# Regexes are case-insensitive; the input is lower-cased before matching.

_DIRECT_PATTERNS = [
    r"^\s*ava\b",                # "Ava can you..."
    r"\bhey ava\b",              # "Hey Ava"
    r"\bhi ava\b",               # "Hi Ava"
    r"\bhello ava\b",            # "Hello Ava"
    r"\byo ava\b",               # "Yo Ava"
    r"\bok(?:ay)? ava\b",        # "Okay Ava" / "OK Ava"
    r"\bava[\?\!\.]+",           # "Ava?" / "Ava!" alone
    r"\bava\s*$",                # ends with Ava
    r"\bava please\b",
    r"\bava can you\b",
    r"\bava could you\b",
    r"\bcan you hear me\b",
]

_INDIRECT_PATTERNS = [
    r"\bava was\b",
    r"\bava is\b",
    r"\bava has\b",
    r"\bava had\b",
    r"\bava did\b",
    r"\bava said\b",
    r"\bava told\b",
    r"\bava thinks\b",
    r"\bava knows\b",
    r"\bava feels\b",
    r"\babout ava\b",
    r"\bfor ava\b",
    r"\bwith ava\b",
    r"\bava and\b",
    r"\band ava\b",
    r"\bava's\b",                # possessive — "Ava's idea"
]

# Phrases people commonly say TO an assistant without using its name. When
# Zeke is looking at the screen + focused, treat these as direct address at
# 0.6 confidence (which triggers wake-learner clarification on first hit).
_GAZE_DIRECT_HINTS = [
    r"^\s*(?:can|could|would) you\b",
    r"^\s*open\s+\S+",
    r"^\s*close\s+\S+",
    r"^\s*show me\b",
    r"^\s*tell me\b",
    r"^\s*what(?:'s|s)? the (?:time|date|weather)\b",
    r"^\s*what time\b",
    r"^\s*what(?:'s|s)? today\b",
    r"^\s*what day\b",
    r"^\s*remind me\b",
    r"^\s*remember (?:that|to)\b",
    r"^\s*play\s+\S+",
    r"^\s*stop\b",
    r"^\s*mute\b",
]


class WakeDetector:
    DIRECT_PATTERNS = _DIRECT_PATTERNS
    INDIRECT_PATTERNS = _INDIRECT_PATTERNS

    def __init__(self) -> None:
        self._direct_re = [re.compile(p, re.IGNORECASE) for p in _DIRECT_PATTERNS]
        self._indirect_re = [re.compile(p, re.IGNORECASE) for p in _INDIRECT_PATTERNS]
        self._gaze_hint_re = [re.compile(p, re.IGNORECASE) for p in _GAZE_DIRECT_HINTS]
        self._learned_direct: list[re.Pattern[str]] = []
        self._learned_indirect: list[re.Pattern[str]] = []
        self._lock = threading.Lock()

    # ── learning hooks (called by WakeLearner) ────────────────────────────────

    def add_learned(self, pattern: str, was_direct: bool) -> None:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return
        with self._lock:
            if was_direct:
                self._learned_direct.append(compiled)
            else:
                self._learned_indirect.append(compiled)

    # ── classification ─────────────────────────────────────────────────────────

    def classify(self, text: str, g: Optional[dict[str, Any]] = None) -> tuple[bool, float, str]:
        """Return (is_direct, confidence_0_1, reason).

        Wake source override: if g["_wake_source"] == "clap", we ALWAYS treat
        the input as direct address — the user already explicitly woke Ava
        with a double clap, so anything they say next is for her. No
        classification, no clarification.
        """
        # Explicit wake sources are always direct: clap or openWakeWord.
        if g is not None:
            ws = str(g.get("_wake_source") or "")
            if ws == "clap":
                return True, 1.0, "clap_triggered"
            if ws == "openwakeword":
                return True, 1.0, "openwakeword_triggered"

        # Normalize Whisper mishearings of "Ava" → "ava" first.
        normalised_raw = normalize_name(text or "")
        t = normalised_raw.lower().strip()
        if not t:
            return False, 0.0, "empty"

        # No "ava" mentioned anywhere. Try gaze-trust hints before giving up.
        if "ava" not in t:
            if g is not None:
                attn = str(g.get("_attention_state") or "").lower()
                looking_at_screen = bool(g.get("_looking_at_screen", True))
                if looking_at_screen and attn in ("focused", ""):
                    for pat in self._gaze_hint_re:
                        if pat.search(t):
                            return True, 0.6, f"gaze_hint:{pat.pattern[:30]}"
            return False, 1.0, "no_ava_token"

        # Indirect first — phrases like "Ava said" should never trigger wake.
        with self._lock:
            indirect = list(self._indirect_re) + list(self._learned_indirect)
            direct = list(self._direct_re) + list(self._learned_direct)

        for pat in indirect:
            if pat.search(t):
                return False, 0.92, f"indirect:{pat.pattern}"

        for pat in direct:
            if pat.search(t):
                return True, 0.92, f"direct:{pat.pattern}"

        # Ambiguous — "ava" present but no pattern matched. Use gaze/attention.
        if g is not None:
            attn = str(g.get("_attention_state") or "").lower()
            looking_at_screen = bool(g.get("_looking_at_screen", True))
            if looking_at_screen and attn in ("focused", ""):
                return True, 0.55, "ambiguous_looking_at_screen"
            if attn in ("away", "absent"):
                return False, 0.55, f"ambiguous_attn={attn}"
        return True, 0.50, "ambiguous_default_direct"

    def is_direct_address(self, text: str, g: Optional[dict[str, Any]] = None) -> bool:
        return self.classify(text, g)[0]


# ── singleton ─────────────────────────────────────────────────────────────────

_SINGLETON: Optional[WakeDetector] = None
_LOCK = threading.Lock()


def get_wake_detector() -> WakeDetector:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = WakeDetector()
    return _SINGLETON
