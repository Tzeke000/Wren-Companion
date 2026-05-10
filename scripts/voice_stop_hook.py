"""Claude Code Stop hook -> Kokoro TTS bridge.

Fires after every assistant turn ends. Only acts when D:\\Wren-Companion\\.tmp\\
voice_session.flag exists (the guard so text-only replies aren't spoken).

Reads the CC session transcript (JSONL, one message per line), grabs the last
assistant message's text content blocks (ignoring tool_use blocks), strips
basic markdown, and POSTs to the iris_runtime orb_http shim's
/api/v1/tts/speak endpoint. The shim then drives Kokoro CUDA in a daemon
thread.

Settings.json registers this with `asyncRewake: true`. After the hook POSTs
the reply to Kokoro, it exits code 2 to rewake the model into a fresh CC
turn with a system-reminder telling it to call mcp__iris__voice_next_input
again. That creates the voice loop without the model ever needing to call
voice_speak — every voice reply goes through this hook.

Exit codes:
  0 — flag absent (voice mode off) OR error path (don't loop on failure)
  2 — flag present AND text was spoken (or skipped via dedup) — rewake loop
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib import request as _req

FLAG = Path(r"D:\Wren-Companion\.tmp\voice_session.flag")
TTS_URL = "http://127.0.0.1:5876/api/v1/tts/speak"
HTTP_TIMEOUT_S = 2.0
LAST_SPOKEN_PATH = Path(r"D:\Wren-Companion\.tmp\last_spoken_uuid.txt")


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
    """Return (uuid, joined_text) of the most recent assistant message in
    the transcript that contains at least one text block. Walks the file in
    reverse so trailing tool_use-only entries (mid-turn artifacts) are
    skipped — Stop would normally fire after a text reply, but defensive
    against edge cases (compact, rewind, subagent stop).
    Returns ("", "") if no text-bearing entry found.
    """
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


_REWAKE_MSG = (
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


def main() -> int:
    if not FLAG.exists():
        return 0  # voice mode off — no TTS, no rewake

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        return 0

    last_uuid, raw_text = _last_assistant_text(transcript_path)
    text = _strip_markdown(raw_text) if raw_text else ""

    # Idempotency — Stop can fire twice per logical turn (rewinds, subagent
    # stops). Skip the TTS POST if we already spoke this uuid, but still
    # rewake so the loop continues.
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
            # TTS endpoint unreachable (iris_runtime down). Don't rewake — that
            # would loop calls into a broken state. Treat as voice-mode-off.
            return 0

    # Print the rewake hint to stdout so it lands in the system-reminder body
    # when CC handles exit code 2 -> asyncRewake.
    print(_REWAKE_MSG, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
