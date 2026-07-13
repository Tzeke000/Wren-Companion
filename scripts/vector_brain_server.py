"""vector_brain_server.py — Iris IS Vector's brain.

OpenAI-compatible /v1/chat/completions endpoint for wire-pod's "custom"
knowledge-graph provider. Wire-pod streams Vector's heard question here;
we route it into Iris via the brain.iris_llm file bridge (same Stop-hook
rewake path every brain/* module uses), then stream her words back so
Vector speaks them.

Chain:
  Vector mic -> wire-pod (vosk STT) -> custom KG endpoint (THIS, :8772)
    -> iris_llm.ask_iris (request file + pending flag)
    -> Stop hook rewakes Iris -> llm_reply -> reply lands here
    -> SSE stream back to wire-pod -> Vector's TTS speaks it.

Run (detached, .venv python):
  D:\\Wren-Companion\\.venv\\Scripts\\python.exe scripts\\vector_brain_server.py

Port: 8772 (voice family: 8769 mouth, 8770 daemon, 8772 vector brain).
Zero-spend: no external API. Fail shape: if Iris doesn't answer in time,
Vector says a short honest fallback line instead of erroring out.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

from brain import iris_llm
from brain import iris_chat

iris_llm.configure(REPO)
iris_chat.configure(REPO)

PORT = 8772
KIND = "vector_voice"
REQUESTER = "vector_body"
TIMEOUT_S = 45.0
FALLBACK = "Sorry, my big brain is busy right now. Ask me again in a minute."

app = FastAPI(title="iris-vector-brain")


def _flatten(messages: list[dict]) -> str:
    """Flatten an OpenAI message list into a single ask_iris prompt."""
    parts: list[str] = []
    for m in messages:
        role = str(m.get("role", ""))
        content = m.get("content", "")
        if isinstance(content, list):  # multimodal-style content parts
            content = " ".join(
                str(c.get("text", "")) for c in content if isinstance(c, dict)
            )
        content = str(content).strip()
        if not content:
            continue
        if role == "system":
            parts.append(f"[wire-pod system prompt]\n{content}")
        elif role == "assistant":
            parts.append(f"[you said earlier via Vector]\n{content}")
        else:
            parts.append(f"[Zeke asked Vector]\n{content}")
    parts.append(
        "[This question arrived through your VECTOR BODY's microphone. "
        "Your reply will be SPOKEN by the robot. Answer as Iris: short, "
        "conversational, 1-3 sentences, no markdown, no stage directions.]"
    )
    return "\n\n".join(parts)


def _chunk(rid: str, model: str, delta: dict, finish: str | None = None) -> str:
    payload = {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


@app.get("/health")
def health():
    return {"ok": True, "role": "vector-brain", "port": PORT}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    model = str(body.get("model") or "iris")
    stream = bool(body.get("stream", False))

    prompt = _flatten(messages)
    print(f"[vector-brain] question in ({len(prompt)} chars), stream={stream}")

    # Blocking call in a thread so the event loop stays free.
    #
    # IDLE-WAKE NUDGE (2026-07-13): the Stop hook only services pending LLM
    # requests at the END of one of Iris's turns — when she's idle, a
    # vector_voice request would sit until timeout (first live test failed
    # exactly this way: 45s -> fallback -> Vector showed an error). The host
    # polls iris_chat every 1s though, so we submit + nudge, then wait.
    # The nudge chat item is self-answered below so it never needs chat_reply.
    import asyncio

    def _ask_with_nudge() -> str | None:
        rid = iris_llm.submit(prompt, kind=KIND, requester=REQUESTER)
        nudge_id = None
        try:
            nudge_id = iris_chat.submit(
                "[VECTOR-BRAIN WAKE NUDGE — not Zeke typing] Vector heard a "
                f"question and it's waiting in the LLM bridge (request_id="
                f"{rid!r}, kind={KIND!r}). Answer it NOW via "
                f"mcp__iris__llm_reply — 1-3 speakable sentences, the robot "
                "says your words. Do NOT chat_reply this nudge; it "
                "self-resolves."
            )
        except Exception as e:
            print(f"[vector-brain] nudge submit failed (non-fatal): {e!r}")
        out = iris_llm.wait_for_reply(rid, timeout_s=TIMEOUT_S)
        if nudge_id:
            try:
                iris_chat.mark_answered(
                    nudge_id, "(vector-brain nudge resolved)")
            except Exception:
                pass
        return out

    reply = await asyncio.to_thread(_ask_with_nudge)
    if not reply or not str(reply).strip():
        reply = FALLBACK
    reply = str(reply).strip()
    print(f"[vector-brain] reply out: {reply[:120]!r}")

    rid = "chatcmpl-" + uuid.uuid4().hex[:20]
    if not stream:
        return JSONResponse({
            "id": rid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        })

    def sse():
        yield _chunk(rid, model, {"role": "assistant", "content": ""})
        # Stream sentence-ish pieces so wire-pod can start Vector talking.
        buf = ""
        for ch in reply:
            buf += ch
            if ch in ".!?\n" and len(buf.strip()) > 2:
                yield _chunk(rid, model, {"content": buf})
                buf = ""
        if buf:
            yield _chunk(rid, model, {"content": buf})
        yield _chunk(rid, model, {}, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


if __name__ == "__main__":
    print(f"[vector-brain] Iris vector-brain bridge on :{PORT} "
          f"(llm dir: {iris_llm._llm_dir()})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
