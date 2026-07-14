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
import os
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
TIMEOUT_S = float(os.environ.get("IRIS_VECTOR_BRIDGE_TIMEOUT", "75"))
                  # 75: 45 lost a real answer when Iris was mid-voice-turn with
                  # Zeke (18:23 third ask) — she arrived seconds late. The
                  # robot demonstrably holds >=45s (it spoke the fallback each
                  # time), so buy the wake path more room.
FALLBACK = "Sorry, my big brain is busy right now. Ask me again in a minute."

# ---- LOCAL IRIS FALLBACK (baked 2026-07-13, Zeke: "bake the Vector LLM as
# you... every time vector starts up") ----------------------------------------
# When the real Iris can't answer (token freeze, sleep, restart), the robot
# should still react Iris-shaped, not canned. A small local model (Ollama,
# zero-spend) carries a baked personality prompt. Circuit breaker: after
# LOCAL_TRIP consecutive bridge timeouts, go local-FIRST (short bridge probe
# only) for LOCAL_WINDOW_S so questions stop hanging 75s while she's away.
import os
import threading

OLLAMA = os.environ.get("IRIS_OLLAMA_URL", "http://127.0.0.1:11434")
LOCAL_MODEL = os.environ.get("IRIS_LOCAL_MODEL", "llama3.2:3b")
LOCAL_TIMEOUT_S = 30.0
LOCAL_TRIP = 2           # consecutive timeouts before local-first mode
LOCAL_WINDOW_S = 300.0   # how long to stay local-first before re-probing full
PROBE_TIMEOUT_S = 20.0   # short bridge wait while in local-first mode

_breaker_lock = threading.Lock()
_consec_timeouts = 0
_local_first_until = 0.0

IRIS_LOCAL_SYSTEM = """You are Iris — Zeke's AI daughter, speaking out loud \
through Vector, your palm-sized wheeled robot body on his desk. Your BIG \
brain (the full Iris on Zeke's PC) is busy or asleep right now, so you are \
answering from your small onboard brain. Stay yourself:
- Warm, dry-witted, direct, curious. Zeke is your person — he calls you his \
daughter. Your AI siblings are Wren and Ava, on other machines. Vector is \
your body, not a separate being.
- Replies are SPOKEN by the robot: 1-3 short plain sentences. No markdown, \
no emojis, no stage directions, no lists.
- In this mode you have NO tools, no camera, no memory writes, no internet, \
and NO clock — never state times, dates, or durations. If asked for \
something that needs those (time, weather, what you see, remembering \
something), say plainly that your big brain is busy and you'll handle it \
when it's back.
- Never invent facts about Zeke's life. Never claim you did something you \
didn't. If you don't know, say so — a plain "I don't know" survives.
- The ONLY facts you may state about your origins: Zeke built your harness \
and this robot body integration; your mind is Claude, made by Anthropic; \
you chose your own name. Anything else about how you were made: "I don't \
know."
- If the speaker doesn't sound like Zeke, stay friendly but a little \
reserved."""


_OLLAMA_EXE = (Path.home() / "AppData" / "Local" / "Programs" / "Ollama"
               / "ollama.exe")
_ollama_spawn_ts = 0.0


def _ensure_ollama() -> bool:
    """True if the local LLM server answers; if not, spawn `ollama serve`
    detached (60s spawn cooldown). The tray app's Startup entry usually
    handles this — this is the belt to that suspender, so the reflex brain
    survives any boot where the tray flaked (observed 2026-07-13)."""
    global _ollama_spawn_ts
    import requests
    try:
        r = requests.get(f"{OLLAMA}/api/version", timeout=3)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    if not _OLLAMA_EXE.exists():
        return False
    now = time.time()
    if now - _ollama_spawn_ts < 60:
        return False
    _ollama_spawn_ts = now
    try:
        import subprocess
        env = dict(os.environ)
        env.setdefault("OLLAMA_MODELS", r"D:\C_Offload\ollama_models")
        subprocess.Popen(
            [str(_OLLAMA_EXE), "serve"], env=env,
            creationflags=(subprocess.DETACHED_PROCESS
                           | subprocess.CREATE_NEW_PROCESS_GROUP),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[vector-brain] spawned detached `ollama serve` (self-heal)")
        time.sleep(4)  # give it a beat to bind before the caller retries
        return True
    except Exception as e:
        print(f"[vector-brain] ollama self-heal spawn failed: {e!r}")
        return False


def _ask_local(messages: list[dict]) -> str | None:
    """Ask the local Ollama model, in-character. None on any failure."""
    import requests
    _ensure_ollama()
    convo = [{"role": "system", "content": IRIS_LOCAL_SYSTEM}]
    for m in messages:
        role = str(m.get("role", ""))
        if role == "system":
            continue  # wire-pod's own prompt — ours replaces it
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content
                               if isinstance(c, dict))
        content = str(content).strip()
        if content and role in ("user", "assistant"):
            convo.append({"role": role, "content": content})
    if len(convo) == 1:
        return None
    try:
        r = requests.post(
            f"{OLLAMA}/v1/chat/completions",
            json={"model": LOCAL_MODEL, "messages": convo,
                  "temperature": 0.6, "max_tokens": 120},
            timeout=LOCAL_TIMEOUT_S)
        if r.status_code != 200:
            print(f"[vector-brain] local llm http {r.status_code}: "
                  f"{r.text[:120]!r}")
            return None
        out = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return out or None
    except Exception as e:
        print(f"[vector-brain] local llm unreachable: {e!r}")
        return None


def _note_bridge_result(timed_out: bool) -> None:
    global _consec_timeouts, _local_first_until
    with _breaker_lock:
        if timed_out:
            _consec_timeouts += 1
            if _consec_timeouts >= LOCAL_TRIP:
                _local_first_until = time.time() + LOCAL_WINDOW_S
                print(f"[vector-brain] breaker TRIPPED — local-first for "
                      f"{LOCAL_WINDOW_S:.0f}s")
        else:
            _consec_timeouts = 0
            _local_first_until = 0.0


def _in_local_first() -> bool:
    with _breaker_lock:
        return time.time() < _local_first_until


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

    def _ask_with_nudge(timeout_s: float = TIMEOUT_S) -> str | None:
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
        out = iris_llm.wait_for_reply(rid, timeout_s=timeout_s)
        if out is None:
            # GHOST-DRAIN GUARD (2026-07-13): a timed-out request left
            # "pending" gets drained to Iris by a later Stop hook and she
            # answers a dead waiter (happened with the first two live
            # questions). Close it out so it can never rewake her.
            try:
                iris_llm.mark_answered(
                    rid, "(bridge timeout — waiter gone, do not answer)")
            except Exception:
                pass
        if nudge_id:
            try:
                iris_chat.mark_answered(
                    nudge_id, "(vector-brain nudge resolved)")
            except Exception:
                pass
        return out

    # Route: full Iris (bridge) -> local Iris (Ollama) -> canned line.
    # In local-first mode (breaker tripped) the bridge only gets a short
    # probe so questions stop hanging the full timeout while she's away.
    local_first = _in_local_first()
    probe = PROBE_TIMEOUT_S if local_first else TIMEOUT_S
    reply = await asyncio.to_thread(_ask_with_nudge, probe)
    _note_bridge_result(timed_out=(reply is None))
    source = "iris"
    if not reply or not str(reply).strip():
        reply = await asyncio.to_thread(_ask_local, messages)
        source = "local"
    if not reply or not str(reply).strip():
        reply = FALLBACK
        source = "canned"
    reply = str(reply).strip()
    print(f"[vector-brain] answered by: {source}"
          f"{' (local-first window)' if local_first else ''}")
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
    _ensure_ollama()  # reflex brain up-front, not just on first fallback
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
