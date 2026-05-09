"""Action-tag router (Jarvis Pattern 1).

When the regex voice_command_router misses, this module asks the fast LLM
to emit one or more action tags describing the user's request. We then
dispatch each tag through existing handlers — open_app, close_app,
type_text, weather, time, date — and return a single aggregated reply
without entering the slow deep-path build_prompt + LLM invoke cycle.

Why this exists:
- Compound voice commands like "Open Notes and type the Dartmouth passage"
  defeat the regex router (the catch-all `\\bopen (.+?)$` captures the
  whole tail as the app name).
- Phrasing variations like "could you launch Chrome for me?" miss the
  strict `^open ` patterns.
- Both fall through to deep-path run_ava which is 12-30s build_prompt +
  5-30s invoke. For pure command intents that's all wasted time.

Tag vocabulary (strict whitelist):
  [OPEN_APP:name]      launch an application
  [CLOSE_APP:name]     close an application
  [TYPE_TEXT:text]     copy text to clipboard and Ctrl+V into focused window
  [WEATHER]            fetch current weather
  [TIME]               current time
  [DATE]               current date
  [CONVERSATION]       none of the above; let deep-path handle as chat

Multiple tags allowed for compound requests. If [CONVERSATION] is present
or no tags emit, we fall through to deep-path.

LLM choice: ava-personal:latest (already in VRAM for fast path → no swap),
temperature 0.0, num_predict 80. Latency target: under 5s.
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from typing import Any

_CLASSIFIER_LLM = None  # cached + pinned classifier instance (set lazily)

_ACTION_TAG_SYSTEM_PROMPT = """You are an action classifier for an AI assistant. Output ONLY action tags, no other text, no explanation.

Available tags (use ONLY these):
[OPEN_APP:name]      launch an app
[CLOSE_APP:name]     close an app
[TYPE_TEXT:text]     type text into the focused window
[WEATHER]            fetch current weather
[TIME]               current time
[DATE]               current date
[CONVERSATION]       just chat / question, none of the above

Multiple tags allowed for compound requests. Tag names are EXACT.

Examples:
User: "could you launch chrome for me?"
Output: [OPEN_APP:Chrome]

User: "open notes and then type: hello world"
Output: [OPEN_APP:Notes][TYPE_TEXT:hello world]

User: "what's the weather like?"
Output: [WEATHER]

User: "tell me about polar bears"
Output: [CONVERSATION]

User: "close chrome please"
Output: [CLOSE_APP:Chrome]

User: "what time is it and what's the date"
Output: [TIME][DATE]

User: "open obs through steam"
Output: [OPEN_APP:OBS]
"""

_TAG_RE = re.compile(
    r"\[(?P<tag>OPEN_APP|CLOSE_APP|TYPE_TEXT|WEATHER|TIME|DATE|CONVERSATION)(?::(?P<arg>[^\]]*))?\]"
)

# Action-verb heuristic — skip the classifier entirely if the input has no
# command-shaped verb. The classifier costs ~5-30s on cold ava-personal which
# is a regression for pure conversation queries. Only invoke the LLM
# classifier when the input PLAUSIBLY contains an action.
_ACTION_HINTS = re.compile(
    r"\b(?:open|close|quit|kill|launch|start|run|play|stop|end|"
    r"type|paste|copy|search|find|"
    r"weather|raining|snowing|sunny|temperature|"
    r"time|date|today|tomorrow|"
    r"remind|reminder|note|save"
    r")\b",
    re.IGNORECASE,
)


def _has_action_hint(text: str) -> bool:
    return bool(_ACTION_HINTS.search(text or ""))


# Pre-classifier shortcut for the most common compound patterns. Avoids the
# 5-30s LLM invoke and avoids the num_predict=80 truncation that mangles
# long TYPE_TEXT bodies. If we recognize the shape, build the action list
# inline.
_OPEN_AND_TYPE = re.compile(
    r"^\s*(?:hey\s+ava[,\s]+)?(?:please\s+)?open\s+(?P<app>[a-zA-Z0-9 _\-+&]+?)\s+(?:and\s+(?:then\s+)?)?(?:type|paste|write)(?:\s*:|\s+the\s+following[:,]?|\s+this[:,]?)?\s+(?P<text>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _try_pattern_shortcut(text: str) -> list[tuple[str, str | None]] | None:
    """Match well-known compound shapes without invoking the LLM.

    Returns None if no pattern matches; caller falls through to the LLM
    classifier. Returns the action list verbatim on a hit.
    """
    if not text:
        return None
    m = _OPEN_AND_TYPE.match(text.strip())
    if m:
        app = (m.group("app") or "").strip().rstrip(".,;:")
        body = (m.group("text") or "").strip()
        if app and body:
            return [("OPEN_APP", app), ("TYPE_TEXT", body)]
    return None


def parse_tags(reply: str) -> list[tuple[str, str | None]]:
    """Extract (tag, arg) pairs from the model's response. Strict whitelist."""
    if not reply:
        return []
    out = []
    for m in _TAG_RE.finditer(reply):
        tag = m.group("tag")
        arg = (m.group("arg") or "").strip() or None
        out.append((tag, arg))
    return out


def _build_context_block(g: dict[str, Any]) -> str:
    """A4: cross-turn context. Build a small snapshot of recent state so
    the classifier can resolve pronouns ("close it", "another tab")
    without losing context across turns.

    Pulls from the per-turn state Ava already tracks (set by _do_open,
    _do_close, voice_command handlers). Empty string if no useful
    recent state.
    """
    parts = []
    last_opened = g.get("_last_opened_app")
    if last_opened:
        parts.append(f"- last app opened: {last_opened}")
    last_closed = g.get("_last_closed_app")
    if last_closed:
        parts.append(f"- last app closed: {last_closed}")
    last_action = g.get("_last_action") or {}
    if isinstance(last_action, dict):
        kind = str(last_action.get("kind") or "")
        trigger = str(last_action.get("trigger") or "")
        if kind and trigger:
            parts.append(f"- last action: {kind} (trigger: {trigger[:80]!r})")
    if not parts:
        return ""
    return "\nRecent context (use to resolve pronouns like 'it', 'that', 'another', 'last'):\n" + "\n".join(parts)


def classify_actions(text: str, *, g: dict[str, Any] | None = None, timeout_s: float = 6.0) -> list[tuple[str, str | None]]:
    """Ask the fast LLM to emit action tags for the user's input.

    Returns list of (tag, arg) tuples. Empty list on failure or if the
    model didn't emit any recognized tags.

    A4: when `g` is supplied, prepends a cross-turn context block to
    the classifier prompt so pronouns resolve naturally ("close it"
    after opening Chrome → [CLOSE_APP:Chrome]).
    """
    if not (text or "").strip():
        return []
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import HumanMessage, SystemMessage
        from brain.ollama_lock import with_ollama
    except Exception as e:
        print(f"[action_tag] import error: {e!r}")
        return []

    context_block = _build_context_block(g) if g else ""

    # 2026-05-07 latency fix: cache + pin the classifier LLM. Was being
    # re-instantiated every call without keep_alive, costing 5-30s per turn
    # on idle-evicted model. Same pattern as fast/deep paths in reply_engine.
    try:
        global _CLASSIFIER_LLM
        if "_CLASSIFIER_LLM" not in globals() or _CLASSIFIER_LLM is None:
            _CLASSIFIER_LLM = ChatOllama(
                model="ava-personal:latest",
                temperature=0.0,
                num_predict=80,
                keep_alive=-1,
            )
        llm = _CLASSIFIER_LLM
        sys_msg = SystemMessage(content=_ACTION_TAG_SYSTEM_PROMPT + context_block)
        user_msg = HumanMessage(content=f'User: "{text.strip()}"\nOutput:')
        t0 = time.time()
        result = with_ollama(
            lambda: llm.invoke([sys_msg, user_msg]),
            label="action_tag:ava-personal",
        )
        ms = int((time.time() - t0) * 1000)
        reply = getattr(result, "content", str(result))
        tags = parse_tags(reply)
        print(f"[action_tag] classified in {ms}ms tags={tags} ctx={'yes' if context_block else 'no'} raw={reply[:120]!r}")
        return tags
    except Exception as e:
        print(f"[action_tag] classify error: {e!r}")
        return []


def _do_open(name: str, g: dict[str, Any]) -> str:
    """Open `name` and verify the side-effect happened (A1, self-awareness).

    The wrapper produces an honest reply: if opening apparently succeeded
    but no window appears within a few seconds, returns "I tried to open
    X but it didn't actually open." Catches the silent-success bugs
    (Edge .lnk failure, Chrome dedup false-positive, etc).
    """
    try:
        from brain.voice_commands import _route_open_app
        from brain.post_action_verifier import wrap_open_with_verification

        def _do():
            ok, msg = _route_open_app(name, g)
            return ok, (msg or (f"Opening {name}." if ok else f"I couldn't open {name}."))

        ok, msg = wrap_open_with_verification(name, _do)
        if ok:
            # Track last-opened app for "close my last app" pronoun recall.
            g["_last_opened_app"] = name
        return msg
    except Exception as e:
        print(f"[action_tag] open_app error: {e!r}")
        return f"I couldn't open {name}."


def _do_close(name: str, g: dict[str, Any]) -> str:
    """Close `name` and verify the windows actually went away (A1)."""
    try:
        from tools.system.app_launcher import _tool_close_app
        from brain.post_action_verifier import wrap_close_with_verification

        def _do():
            result = _tool_close_app({"app_name": name}, g) or {}
            if result.get("ok"):
                display = name.strip().title() if name else "It"
                return True, f"{display} is closed."
            return False, f"I couldn't close {name}."

        ok, msg = wrap_close_with_verification(name, _do)
        if ok:
            # A4: track for cross-turn pronoun resolution.
            g["_last_closed_app"] = name
        return msg
    except Exception as e:
        print(f"[action_tag] close_app error: {e!r}")
        return f"I couldn't close {name}."


def _do_type(text: str, g: dict[str, Any]) -> str:
    """Copy text to clipboard and send Ctrl+V to the focused window.

    With A1 self-awareness: verifies clipboard payload after the
    operation. The Ctrl+V can't be directly verified (would require
    OCR or app introspection), but we can confirm the clipboard
    holds what we intended to paste — that's the most we can know
    cheaply.
    """
    try:
        from brain.windows_use.primitives import set_clipboard
        from brain.post_action_verifier import verify_after_type
        if not set_clipboard(text):
            return "I couldn't put that on the clipboard."
        try:
            import ctypes
            import ctypes.wintypes as _wt
            VK_CONTROL = 0x11
            VK_V = 0x56
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_V, 0, 0, 0)
            time.sleep(0.05)
            ctypes.windll.user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
            ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        except Exception as e:
            print(f"[action_tag] paste keybd error: {e!r}")
            return "I copied that to the clipboard but couldn't paste it."
        # A1: verify the clipboard payload matches our intent.
        result = verify_after_type(text)
        if result.get("verified"):
            return "Pasted."
        return result.get("explanation") or "I tried to paste that, but I'm not sure it landed."
    except Exception as e:
        print(f"[action_tag] type_text error: {e!r}")
        return "I couldn't type that."


def _do_weather(g: dict[str, Any]) -> str:
    try:
        from tools.web.weather import _fetch_weather_text
        ok, msg = _fetch_weather_text()
        return msg
    except Exception as e:
        print(f"[action_tag] weather error: {e!r}")
        return "I couldn't reach the weather service."


def _do_time(g: dict[str, Any]) -> str:
    return _dt.datetime.now().strftime("It's %I:%M %p.").lstrip("0")


def _do_date(g: dict[str, Any]) -> str:
    now = _dt.datetime.now()
    return now.strftime("Today is %A, %B ") + str(now.day) + "."


def dispatch_actions(
    actions: list[tuple[str, str | None]], g: dict[str, Any]
) -> tuple[bool, str]:
    """Execute the action tags in order. Returns (handled, spoken_reply).

    handled=False means we should fall through to the deep path:
    - empty action list (LLM emitted nothing recognized)
    - only [CONVERSATION] tags

    handled=True means we executed at least one real action and the
    spoken_reply aggregates the results in user-readable order.
    """
    if not actions:
        return False, ""
    parts: list[str] = []
    any_real = False
    open_inter_delay = 1.5  # post-open settle so the new window grabs focus before TYPE_TEXT
    for tag, arg in actions:
        if tag == "CONVERSATION":
            continue
        any_real = True
        if tag == "OPEN_APP" and arg:
            parts.append(_do_open(arg, g))
            time.sleep(open_inter_delay)
        elif tag == "CLOSE_APP" and arg:
            parts.append(_do_close(arg, g))
        elif tag == "TYPE_TEXT" and arg:
            parts.append(_do_type(arg, g))
        elif tag == "WEATHER":
            parts.append(_do_weather(g))
        elif tag == "TIME":
            parts.append(_do_time(g))
        elif tag == "DATE":
            parts.append(_do_date(g))
        else:
            # Unknown / malformed tag — skip silently.
            any_real = False
            continue
    if not any_real:
        return False, ""
    spoken = " ".join(p for p in parts if p).strip()
    return True, spoken


def route(text: str, g: dict[str, Any]) -> tuple[bool, str]:
    """Single entry point. Classifies + dispatches. Returns (handled, reply).

    On classification failure or [CONVERSATION] result, returns (False, "")
    so the caller falls through to deep-path run_ava.

    Heuristic short-circuit: if the input has no command-shaped verb
    (open/close/launch/weather/time/etc), skip the classifier — it would
    just emit [CONVERSATION] anyway and that takes 5-30s on cold model.
    Pure conversational queries shouldn't pay the classifier latency.
    """
    if not _has_action_hint(text):
        print(f"[action_tag] heuristic skip — no action-hint in {text[:60]!r}")
        return False, ""
    # Skill recall (Hermes-pattern) — pre-classifier shortcut for known
    # procedural skills. Trigger phrases are auto-stored on successful
    # compound dispatches; recall is a Jaccard match on normalized
    # tokens. Conservatively threshold-gated to avoid false positives.
    try:
        from pathlib import Path as _Path
        from brain import skills as _skills
        base = _Path(g.get("BASE_DIR") or ".")
        recalled = _skills.recall(base, text)
        if recalled is not None:
            skill, score = recalled
            actions_in = skill.get("actions") or []
            actions = [(t, a) for (t, a) in actions_in]
            print(f"[action_tag] skill recall: {skill.get('slug')!r} score={score:.2f} actions={actions}")
            handled, reply = dispatch_actions(actions, g)
            if handled:
                # Track usage on the skill record.
                try:
                    skill["success_count"] = int(skill.get("success_count") or 0) + 1
                    import time as _t
                    skill["last_used"] = _t.time()
                    _skills.save_skill(base, skill)
                except Exception:
                    pass
                return handled, reply
    except Exception as e:
        print(f"[action_tag] skill recall error (non-fatal): {e!r}")
    # Pattern-shortcut for common compounds (e.g., "open X and type Y").
    # Avoids the LLM classifier latency AND num_predict truncation that
    # mangles long TYPE_TEXT bodies.
    shortcut = _try_pattern_shortcut(text)
    if shortcut is not None:
        print(f"[action_tag] pattern-shortcut: {shortcut}")
        handled, reply = dispatch_actions(shortcut, g)
        if handled:
            _try_persist_skill(g, text, shortcut)
        return handled, reply
    # A4: pass `g` so the classifier sees recent context (last opened/
    # closed app, last action). Lets pronouns resolve.
    actions = classify_actions(text, g=g)
    if not actions:
        return False, ""
    handled, reply = dispatch_actions(actions, g)
    if handled:
        _try_persist_skill(g, text, actions)
    return handled, reply


def _try_persist_skill(
    g: dict[str, Any],
    text: str,
    actions: list[tuple[str, str | None]],
) -> None:
    """Best-effort skill auto-creation after a successful dispatch."""
    try:
        from pathlib import Path as _Path
        from brain import skills as _skills
        base = _Path(g.get("BASE_DIR") or ".")
        slug = _skills.auto_create_or_update(base, text, actions)
        if slug:
            print(f"[action_tag] persisted skill: {slug}")
    except Exception as e:
        print(f"[action_tag] skill persist error (non-fatal): {e!r}")
