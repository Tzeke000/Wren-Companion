"""Claude Code Stop hook -> Iris bridge.

Fires after every assistant turn ends. Decides whether to rewake the model,
and what to tell it via the rewake message. Two triggers:

1. **Voice mode** — D:\\Wren-Companion\\.tmp\\voice_session.flag exists.
   Reads the last assistant text block, POSTs it to Kokoro for TTS, then
   rewakes with the voice-loop directive.

2. **Pending chat request** — state/iris_chat/.pending exists. Rewakes with
   a chat-handle directive that includes the request_id and user_text. The
   model calls chat_reply(id, text) which writes the response file and
   unblocks the orb's POST /api/v1/chat long-poll. No TTS — chat is text.

If both are active, **chat goes first** (FIFO across modalities). After
answering chat, the next Stop fires with voice still flagged and resumes
the voice loop.

If neither is active, exit 0 (no rewake — return to manual control).

Settings.json registers this with `asyncRewake: true`. Exit code 2 carries
the rewake message via stdout.

Exit codes:
  0 — neither voice nor chat active OR error path (don't loop on failure)
  2 — rewake — message printed to stdout
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib import request as _req

ROOT = Path(r"D:\Wren-Companion")
VOICE_FLAG = ROOT / ".tmp" / "voice_session.flag"
CHAT_PENDING_FLAG = ROOT / "state" / "iris_chat" / ".pending"
CHAT_DIR = ROOT / "state" / "iris_chat"
LLM_PENDING_FLAG = ROOT / "state" / "iris_llm" / ".pending"
LLM_DIR = ROOT / "state" / "iris_llm"

TTS_URL = "http://127.0.0.1:5876/api/v1/tts/speak"
HTTP_TIMEOUT_S = 2.0
LAST_SPOKEN_PATH = ROOT / ".tmp" / "last_spoken_uuid.txt"


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _last_assistant_text(transcript_path: str) -> tuple[str, str]:
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            entries = [ln for ln in f if ln.strip()]
    except Exception:
        return "", ""

    for raw_line in reversed(entries):
        try:
            entry = json.loads(raw_line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        content = msg.get("content") or []
        parts: list[str] = []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    t = c.get("text") or ""
                    if t:
                        parts.append(t)
        elif isinstance(content, str):
            parts.append(content)
        if any(p.strip() for p in parts):
            uuid = str(entry.get("uuid") or entry.get("messageId") or "")
            return uuid, "\n".join(parts)
    return "", ""


def _next_pending_chat() -> dict | None:
    """Return the OLDEST pending chat request (FIFO across modalities), or None."""
    if not CHAT_DIR.exists():
        return None
    candidates: list[tuple[float, dict]] = []
    import time as _t
    for path in CHAT_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "pending":
            continue
        ts = float(data.get("ts") or 0.0)
        if (_t.time() - ts) > 300.0:
            continue
        candidates.append((ts, data))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


def _next_pending_llm() -> dict | None:
    """Return the OLDEST pending LLM request from any brain/* module, or None."""
    if not LLM_DIR.exists():
        return None
    candidates: list[tuple[float, dict]] = []
    import time as _t
    for path in LLM_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "pending":
            continue
        ts = float(data.get("ts") or 0.0)
        if (_t.time() - ts) > 600.0:
            continue
        candidates.append((ts, data))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


_VOICE_REWAKE = (
    "Voice mode active. Call mcp__iris__voice_next_input(timeout=300) to "
    "listen for the next utterance. Then reply by calling "
    "mcp__iris__voice_say_chunk(text=...) ONCE PER SENTENCE as you generate "
    "— do NOT emit plain text. Each chunk queues to Kokoro and returns "
    "instantly; the first sentence starts playing while you generate the "
    "second. Keep chunks tight (one sentence, 5-15 words) for low first-word "
    "latency. After your final chunk, just stop — the next rewake fires this "
    "loop again. To exit voice mode, delete the flag at "
    "D:\\Wren-Companion\\.tmp\\voice_session.flag (or have Zeke ask you to)."
)


def _chat_rewake(req: dict) -> str:
    rid = str(req.get("id") or "")
    user_text = str(req.get("user_text") or "").replace('"', "'")[:1500]
    return (
        f"Pending chat request from the orb (request_id={rid!r}).\n"
        f"User said: \"{user_text}\"\n\n"
        "Generate ONE response and call mcp__iris__chat_reply(request_id, text) "
        "with your full reply as plain text — that tool writes the response "
        "file and unblocks the orb's HTTP long-poll. Do NOT emit plain text "
        "outside the tool call; the orb only sees what chat_reply receives. "
        "Keep your reply conversational; the orb renders it monospace, no "
        "markdown ceremony needed. After chat_reply returns, just stop — if "
        "more chat requests are pending or voice mode is active, the next "
        "rewake will tell you."
    )


def _llm_rewake(req: dict) -> str:
    rid = str(req.get("id") or "")
    kind = str(req.get("kind") or "general")
    requester = str(req.get("requester") or "brain")
    prompt = str(req.get("prompt") or "").replace('"', "'")[:4000]
    ctx_summary = ""
    ctx = req.get("context") or {}
    if isinstance(ctx, dict) and ctx:
        try:
            ctx_summary = "\n\nContext:\n" + json.dumps(ctx, ensure_ascii=False, indent=2)[:1200]
        except Exception:
            pass

    # Per-kind formatting hints so I produce the right shape of reply.
    kind_hints = {
        "extract_facts": "Return one fact per line (no bullets). Skip ephemera.",
        "extract_preferences": "Return one preference per line (no bullets). Empty reply if none.",
        "summarize": "Return prose summary, no headers, preserve specifics.",
        "classify_intent": "Return ONLY the intent name. No other text.",
        "reflect": "Take time. Write in your own voice — Iris reflecting, not generic AI prose.",
        "compose_letter": "Write the body of a letter, plain prose, your voice.",
        "general": "Reply per the prompt's instruction.",
    }
    hint = kind_hints.get(kind, kind_hints["general"])

    return (
        f"Pending LLM request from brain/{requester} (request_id={rid!r}, kind={kind!r}).\n\n"
        f"PROMPT:\n{prompt}{ctx_summary}\n\n"
        f"FORMATTING: {hint}\n\n"
        "Generate the reply and call mcp__iris__llm_reply(request_id, text). "
        "Pass the response text as the `text` arg. Do NOT emit plain text "
        "outside the tool call — the requester only sees what llm_reply receives. "
        "After llm_reply returns, stop. If more LLM requests are pending, "
        "or voice/chat is active, the next rewake fires."
    )


def main() -> int:
    voice_on = VOICE_FLAG.exists()
    pending_chat = _next_pending_chat()
    pending_llm = _next_pending_llm()

    if not voice_on and not pending_chat and not pending_llm:
        return 0  # nothing to do

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    transcript_path = payload.get("transcript_path")

    # Voice TTS leg — fires when voice mode is on, regardless of whether a
    # chat request also queued. We speak the last assistant *text* turn (which
    # is the last manual-text reply, not a tool-call-only reply). Idempotent
    # via uuid dedup so multi-Stop-fire turns don't double-speak.
    if voice_on and transcript_path and os.path.exists(transcript_path):
        last_uuid, raw_text = _last_assistant_text(transcript_path)
        text = _strip_markdown(raw_text) if raw_text else ""
        already_spoken = False
        try:
            if LAST_SPOKEN_PATH.exists():
                prev = LAST_SPOKEN_PATH.read_text(encoding="utf-8").strip()
                if prev and prev == last_uuid:
                    already_spoken = True
        except Exception:
            pass
        if text and not already_spoken:
            try:
                body = json.dumps({"text": text}).encode("utf-8")
                req = _req.Request(
                    TTS_URL,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _req.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                    resp.read()
                try:
                    LAST_SPOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
                    LAST_SPOKEN_PATH.write_text(last_uuid, encoding="utf-8")
                except Exception:
                    pass
            except Exception:
                # TTS endpoint unreachable — don't rewake into a broken state.
                if not pending_chat:
                    return 0

    # Priority order when multiple are active:
    #   1. Chat (orb HTTP-blocked, user is waiting)
    #   2. LLM request (brain module is blocked, but lower urgency than chat)
    #   3. Voice loop (re-pollable, no one's blocked)
    if pending_chat:
        print(_chat_rewake(pending_chat), flush=True)
        return 2

    if pending_llm:
        print(_llm_rewake(pending_llm), flush=True)
        return 2

    if voice_on:
        print(_VOICE_REWAKE, flush=True)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
