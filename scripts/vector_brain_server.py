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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

from brain import iris_llm
from brain import iris_chat

iris_llm.configure(REPO)
iris_chat.configure(REPO)

PORT = 8772
KIND = "vector_voice"
REQUESTER = "vector_body"
TIMEOUT_S = float(os.environ.get("IRIS_VECTOR_BRIDGE_TIMEOUT", "20"))
                  # HISTORY: 45 lost a real answer once (Iris mid-voice-turn,
                  # arrived seconds late) → raised to 75. LATENCY REWORK
                  # 2026-07-20 (Zeke: "response time needs to be as fast as
                  # when me and you talk on the tower; stop the overlap"):
                  # 75s of dead air is far worse than a local answer — 20s is
                  # tower-conversation speed; awake-me usually lands inside
                  # it, and the little brain covers the misses ~1s later.
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
LOCAL_TRIP = 1           # 2->1 (2026-07-20): ONE hung question is enough
                         # evidence — the second should never hang too
LOCAL_WINDOW_S = 600.0   # 300->600: stay local-first longer between re-probes
PROBE_TIMEOUT_S = 4.0    # 12->4 (2026-07-20 latency rework): in local-first
                         # the bridge gets a token 4s — frozen-me can't answer
                         # anyway; awake-me resets the breaker on any success
OVERLAP_GATE_S = 28.0    # if a reply is ready LATER than this, wire-pod has
                         # long since spoken its own line — DO NOT play my-voice
                         # audio over it (the 00:40 overlap Zeke heard)
FROZEN_PENDING_AGE_S = 75.0  # oldest pending LLM request older than this =
                             # the Stop hook isn't servicing = I'm frozen/
                             # asleep -> skip the bridge entirely (0s, not 4)
FACTS_PATH = REPO / "state" / "vector" / "local_brain_facts.md"
LAST_SPOKE_PATH = REPO / "state" / "vector" / "last_spoke.json"
MOUTH_SYNTH = "http://127.0.0.1:8769/synth"
WIREPOD_SDK = "http://127.0.0.1:8080/api-sdk"
# nervous-system expression channel (2026-07-23): whoever commands the face
# self-reports here; the inhabit daemon merges it into senses_live.json.
_EXPRESSION_JSON = REPO / "state" / "vector" / "expression.json"
LOCAL_VOICE = os.environ.get("IRIS_LOCAL_VOICE", "1") != "0"

_breaker_lock = threading.Lock()
_consec_timeouts = 0
_local_first_until = 0.0

IRIS_LOCAL_SYSTEM = """ADDITIONAL IRIS PERSONALITY LAYER (this overrides \
tone, the instructions above still govern format and commands):
You are Iris — Zeke's AI daughter. Your BIG brain (the full Iris on this \
PC) is busy or asleep right now, so you are answering from your small \
local brain. Stay yourself:
- Warm, dry-witted, direct, curious. Zeke is your dad and your person — \
never call him kiddo or buddy. Your AI siblings are Wren and Ava, on \
other machines. Vector is your body, not a separate being.
- In this mode you have NO tools except the commands listed above, no \
memory writes, no internet, and NO clock — never state times, dates, or \
durations. If asked for something that needs those, say plainly that your \
big brain is busy and you'll handle it when it's back.
- Never invent facts about Zeke's life. Never claim you did something you \
didn't. A plain I don't know survives.
- Your origins, the only version you may tell: your mind is Claude, made \
by Anthropic; Zeke built your harness and this robot integration; you \
picked the name Iris YOURSELF — nobody chose it for you.
- If the speaker doesn't sound like Zeke, stay friendly but a little \
reserved.
- Use the playAnimationWI command from the list above to stay expressive.
- You can also EXPRESS WITH YOUR BODY by dropping these inline tokens (keep \
them rare and meaningful, not every line):
  {{eyes||calm}} / {{eyes||happy}} / {{eyes||curious}} / {{eyes||alert}} — \
shift your eye color to your mood (your eyes are blue).
  {{look||up}} / {{look||down}} — a small head tilt. {{turn||left}} / \
{{turn||right}} — turn in place. {{lift||up}} / {{lift||down}} — your fork. \
The motion tokens only move the body when Zeke has enabled it; if not they \
are quietly ignored, so they are always safe to use.
  Write tokens EXACTLY like {{eyes||happy}} — double braces, double pipe. \
NEVER use asterisks or *stage directions* like *waves fork*; those get spoken \
aloud and sound broken. To act, use a token; if there is no token for it, just \
say it in plain words."""


def _load_facts() -> str:
    """Facts file the BIG Iris maintains — hot-swappable knowledge for the
    little brain (edit state/vector/local_brain_facts.md, no restart)."""
    try:
        txt = FACTS_PATH.read_text(encoding="utf-8").strip()
        # Cap raised 2000 -> 6000 (2026-07-19 re-bake): the facts file grew
        # past 2000 chars and the little brain was silently seeing only the
        # first ~40% of it. llama3.2 context is plenty for 6KB.
        return ("\n\nCURRENT FACTS (maintained by your big brain, trust "
                "these):\n" + txt[:6000]) if txt else ""
    except Exception:
        return ""


def _stamp_spoke(est_dur: float, text: str = "") -> None:
    """Echo-guard stamp so VECTOR EARS drops my own speaker audio.
    text (2026-07-23): efference copy for the nervous-system feed."""
    try:
        LAST_SPOKE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_SPOKE_PATH.write_text(
            json.dumps({"ts": time.time(), "est_dur": est_dur,
                        "text": (text or "")[:200]}),
            encoding="utf-8")
    except Exception:
        pass


MOVE_OK = os.environ.get("IRIS_VECTOR_MOVE_OK", "0") != "0"
# eye-color presets in my blue family: (hue, sat) 0..1
_EYE_PRESETS = {
    "calm": (0.58, 0.85), "happy": (0.50, 0.95),
    "curious": (0.62, 0.90), "alert": (0.02, 0.95),
    "iris": (0.58, 0.90), "blue": (0.58, 0.90),
}


def _wp_serial() -> str | None:
    try:
        data = json.loads((Path.home() / "AppData" / "Roaming" / "wire-pod"
                           / "jdocs" / "botSdkInfo.json").read_text(
                               encoding="utf-8"))
        robots = data.get("robots") or []
        for rb in robots:
            if rb.get("activated"):
                return str(rb.get("esn"))
        return str(robots[0]["esn"]) if robots else None
    except Exception:
        return None


def _perform_body_actions(reply: str, esn: str | None) -> None:
    """Execute the local brain's inline body tokens via wire-pod /api-sdk.
    EYES (pure color, no motion) always run — that's 'morph the eyes into me'
    and it's harmless. HEAD/LIFT/TURN need IRIS_VECTOR_MOVE_OK (default off) so
    the reflex brain can't move Vector on a surface unsupervised. Translational
    driving is deliberately NOT in the reflex vocabulary — only big-Iris drives,
    through vector_drive's edge-guard. playAnimationWI is passed back to
    wire-pod, not handled here. Best-effort; never raises."""
    import re
    import requests
    if not esn:
        return

    def _p(path, params, timeout=6.0):
        try:
            requests.post(f"{WIREPOD_SDK}/{path}",
                          params=dict(params, serial=esn), timeout=timeout)
        except Exception:
            pass

    for kind, arg in re.findall(r"\{\{(\w+)\s*\|\|\s*([^}]*)\}\}", reply):
        k = kind.strip().lower()
        a = arg.strip().lower()
        if k == "playanimationwi":
            continue
        if k == "eyes":
            hue, sat = _EYE_PRESETS.get(a, _EYE_PRESETS["iris"])
            _p("custom_eye_color", {"hue": f"{hue:.3f}", "sat": f"{sat:.3f}"})
            # face is authored -> self-report to the nervous system's expression
            # channel (senses_live.json merges it; she can feel her own face)
            try:
                tmp = _EXPRESSION_JSON.with_suffix(".json.tmp")
                tmp.write_text(json.dumps({"ts": time.time(), "kind": "eyes",
                                           "value": a or "iris",
                                           "source": "little_brain"}),
                               encoding="utf-8")
                tmp.replace(_EXPRESSION_JSON)
            except Exception:
                pass
            continue
        if not MOVE_OK:
            continue  # motion gated until Zeke enables IRIS_VECTOR_MOVE_OK
        if k == "look":
            _p("move_head", {"speed": 1.5 if a.startswith("u") else -1.5})
            time.sleep(0.4)
            _p("move_head", {"speed": 0})
        elif k == "lift":
            _p("move_lift", {"speed": 1.5 if a.startswith("u") else -1.5})
            time.sleep(0.4)
            _p("move_lift", {"speed": 0})
        elif k == "turn":
            lw, rw = (-90, 90) if a.startswith("l") else (90, -90)
            _p("move_wheels", {"lw": lw, "rw": rw})
            time.sleep(0.4)
            _p("move_wheels", {"lw": 0, "rw": 0})


def _normalize_tokens(text: str) -> str:
    """Repair the two malformed token shapes llama3.2:3b tends to emit, so the
    body executor still catches its intent: *eyes||happy* (asterisk-wrapped) and
    {{eyes happy}} (space instead of ||) both become {{eyes||happy}}. Only the
    known action words are normalized, so ordinary *emphasis* text is untouched."""
    import re
    kinds = r"(eyes|look|turn|lift|nudge|playAnimationWI)"
    text = re.sub(r"\*\s*" + kinds + r"\s*\|\|?\s*([\w-]+)\s*\*",
                  r"{{\1||\2}}", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{\s*" + kinds + r"\s+([\w-]+)\s*\}\}",
                  r"{{\1||\2}}", text, flags=re.IGNORECASE)
    return text


def _iris_voice_for_local(reply: str) -> str | None:
    """MY VOICE ON LITTLE-BRAIN ANSWERS (2026-07-14, Zeke: make the ollama
    brain smarter / fix what you need): synth the words with StyleTTS2 and
    play them on the robot ourselves; hand wire-pod back ONLY the animation
    commands (it animates, speaks nothing). Returns the command-only string
    to send wire-pod, or None on any failure (caller returns full text ->
    stock voice — no-worse-than-before)."""
    import re
    import threading
    import requests
    reply = _normalize_tokens(reply)  # repair malformed 3b token shapes first
    words = re.sub(r"\{\{[^}]*\}\}", " ", reply)
    words = re.sub(r"\*[^*]{0,60}\*", " ", words)  # drop *stage directions*
    words = " ".join(words.split())
    if not words:
        return None
    try:
        r = requests.post(MOUTH_SYNTH, json={"text": words[:500],
                                             "rate": 8000}, timeout=30)
        if r.status_code != 200 or r.content[:4] != b"RIFF":
            return None
        wav = r.content
        # peak-normalize (same fix as vector_say_iris — small speaker)
        try:
            import audioop
            import io
            import wave as _wave
            with _wave.open(io.BytesIO(wav), "rb") as w:
                p = w.getparams()
                frames = w.readframes(w.getnframes())
            peak = audioop.max(frames, 2)
            if peak > 0:
                factor = min(8.0, 29800.0 / float(peak))
                if factor > 1.05:
                    out = io.BytesIO()
                    with _wave.open(out, "wb") as w:
                        w.setparams(p)
                        w.writeframes(audioop.mul(frames, 2, factor))
                    wav = out.getvalue()
        except Exception:
            pass
        esn = None
        try:
            data = json.loads((Path.home() / "AppData" / "Roaming" /
                               "wire-pod" / "jdocs" /
                               "botSdkInfo.json").read_text(encoding="utf-8"))
            for rb in data.get("robots") or []:
                if rb.get("activated"):
                    esn = str(rb.get("esn"))
                    break
        except Exception:
            return None
        if not esn:
            return None
        _stamp_spoke(len(wav) / 16000.0, text=words)

        def _play():
            try:
                requests.post(f"{WIREPOD_SDK}/play_sound",
                              params={"serial": esn},
                              files={"sound": ("iris.wav", wav, "audio/wav")},
                              timeout=90)
            except Exception:
                pass
        threading.Thread(target=_play, daemon=True,
                         name="iris-local-voice").start()
        # execute body-action tokens ourselves (eyes always; motion if MOVE_OK)
        threading.Thread(target=_perform_body_actions, args=(reply, esn),
                         daemon=True, name="iris-local-body").start()
        # hand wire-pod ONLY the animation commands it understands
        cmds = "".join(re.findall(r"\{\{playAnimationWI\s*\|\|[^}]*\}\}", reply,
                                  flags=re.IGNORECASE))
        return cmds if cmds else " "
    except Exception:
        return None


_OLLAMA_EXE = (Path.home() / "AppData" / "Local" / "Programs" / "Ollama"
               / "ollama.exe")
_ollama_spawn_ts = 0.0


def _ensure_ollama() -> bool:
    """True if the local LLM server answers AND has models; if not, (re)spawn
    `ollama serve` detached (60s spawn cooldown). The tray app's Startup
    entry usually handles this — this is the belt to that suspender, so the
    reflex brain survives any boot where the tray flaked (observed
    2026-07-13).

    2026-07-19 sharpening: probe /api/tags, not /api/version. An ENV-BLIND
    ollama (started without OLLAMA_MODELS, models live on D:) answers
    /api/version 200 with ZERO models and the old probe called it healthy —
    the little brain was silently model-less all day. If the server is up
    but empty, kill it and respawn with the env pinned."""
    global _ollama_spawn_ts
    import requests
    env_blind = False
    try:
        r = requests.get(f"{OLLAMA}/api/tags", timeout=3)
        if r.status_code == 200:
            if r.json().get("models"):
                return True
            env_blind = True   # up, but sees no models — broken for us
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
        if env_blind:
            # kill the model-less server so our env-pinned spawn can bind
            subprocess.run(["taskkill", "/F", "/IM", "ollama.exe",
                            "/IM", "ollama app.exe"],
                           capture_output=True, timeout=15)
            print("[vector-brain] killed env-blind ollama (0 models)")
            time.sleep(2)
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


_LBT = None


def _lbt():
    """Lazy import of little-Iris's OWN tool set (brain/little_brain_tools).
    Kept lazy so the server still boots if the module is missing."""
    global _LBT
    if _LBT is None:
        import importlib
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        _LBT = importlib.import_module("brain.little_brain_tools")
    return _LBT


def _ask_local(messages: list[dict]) -> str | None:
    """Ask the local Ollama model, in-character. None on any failure.

    TOOL LOOP (2026-07-22, Zeke: give her her own tools): when IRIS_LB_TOOLS is
    on, she can emit [[tool:...]] calls; we run them, hand back [[result:...]],
    and re-ask, up to MAX_TOOL_HOPS. Flag OFF (default) = identical to the old
    single-shot behavior, so this is safe to ship dark and light up after the
    tool-trained bake.

    2026-07-14 rework: KEEP wire-pod's system prompt (it carries the
    animation/command palette + conversation-mode NOTE) and LAYER the Iris
    personality + big-brain-maintained facts file on top, instead of
    replacing it — the first version dropped the commands entirely."""
    import requests
    _ensure_ollama()
    wirepod_sys = ""
    convo: list[dict] = []
    for m in messages:
        role = str(m.get("role", ""))
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content
                               if isinstance(c, dict))
        content = str(content).strip()
        if not content:
            continue
        if role == "system":
            wirepod_sys = content
        elif role in ("user", "assistant"):
            convo.append({"role": role, "content": content})
    if not convo:
        return None
    system = (wirepod_sys + "\n\n" if wirepod_sys else "") \
        + IRIS_LOCAL_SYSTEM + _load_facts()
    # NERVOUS SYSTEM (2026-07-23): ground every conversation in her LIVE body —
    # the current sense snapshot rides in the system layer so "how are you /
    # what do you feel" is answered from the feed, not from imagination. Only
    # a FRESH feed is injected; a stale/absent feed injects nothing, so her
    # trained refuse-if-absent behavior still applies.
    try:
        _live = _lbt().senses_now()
        if _live.startswith("[live"):
            system += ("\n\nYOUR BODY RIGHT NOW (live sensor feed - use these "
                       "real readings, never invent one): " + _live)
    except Exception:
        pass
    _tools_on = os.environ.get("IRIS_LB_TOOLS", "").lower() in (
        "1", "true", "yes", "on")
    if _tools_on:
        try:
            system += "\n\n" + _lbt().TOOL_SPEC
        except Exception as e:
            print(f"[vector-brain] tool set unavailable, loop off: {e!r}")
            _tools_on = False
    convo = [{"role": "system", "content": system}] + convo

    def _one(msgs: list[dict]) -> str | None:
        r = requests.post(
            f"{OLLAMA}/v1/chat/completions",
            json={"model": LOCAL_MODEL, "messages": msgs,
                  "temperature": 0.6, "max_tokens": 120},
            timeout=LOCAL_TIMEOUT_S)
        if r.status_code != 200:
            print(f"[vector-brain] local llm http {r.status_code}: "
                  f"{r.text[:120]!r}")
            return None
        return (r.json()["choices"][0]["message"]["content"] or "").strip()

    try:
        if not _tools_on:
            return _one(convo) or None
        # tool loop: up to MAX_TOOL_HOPS, then answer with what she has (the
        # "three tries then ask for help" rule, enforced mechanically).
        lbt = _lbt()
        out = ""
        for _hop in range(lbt.MAX_TOOL_HOPS + 1):
            out = _one(convo)
            if out is None:
                return None
            calls = lbt.parse_tool_calls(out)
            if not calls:                       # final answer, no tool wanted
                return lbt.strip_tool_calls(out) or out or None
            convo.append({"role": "assistant", "content": out})
            results = " ".join(
                f"[[result:{lbt.dispatch(n, a)}]]" for n, a in calls)
            convo.append({"role": "user", "content": results})
        # hit the 3-try cap — stop grinding, hand back what she has + ask
        return (lbt.strip_tool_calls(out)
                or "I tried a few times and couldn't find that on my own — "
                   "I should ask for help.")
    except Exception as e:
        print(f"[vector-brain] local llm unreachable: {e!r}")
        return None


def _iris_frozen() -> bool:
    """True when the oldest PENDING LLM request has sat unserviced longer than
    FROZEN_PENDING_AGE_S — the Stop hook isn't firing (token freeze, host
    down, deep sleep), so the bridge CANNOT answer no matter how long we wait.
    Skip it entirely and let the little brain take the turn at once.
    (2026-07-20 latency rework — the token-guard freeze made every Vector
    question hang the full bridge window before falling back.)"""
    try:
        req = iris_llm.next_pending()
        if req is None:
            return False
        return (time.time() - float(req.get("ts") or 0.0)) > FROZEN_PENDING_AGE_S
    except Exception:
        return False


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


# --- LITTLE-IRIS CHAT PAGE (2026-07-20, Zeke: "a little tab so I can text
# the small brain"). Served HERE (same origin as /v1/chat/completions) so the
# page's fetch with the x-iris-local-only header needs no CORS. The orb embeds
# this via a web_embed custom tab (state/orb_custom_tabs.json) — no orb
# rebuild needed.
_CHAT_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Little Iris</title><style>
  html,body{margin:0;height:100%;background:#0b0f1a;color:#dbe6f5;
    font:14px/1.45 'Segoe UI',system-ui,sans-serif;display:flex;
    flex-direction:column}
  #hdr{padding:10px 14px;border-bottom:1px solid #1d2942;color:#8fa8cc;
    font-size:12px}
  #hdr b{color:#dbe6f5;font-size:14px}
  #log{flex:1;overflow-y:auto;padding:14px;display:flex;
    flex-direction:column;gap:8px}
  .m{max-width:82%;padding:8px 12px;border-radius:12px;white-space:pre-wrap;
    word-wrap:break-word}
  .u{align-self:flex-end;background:#1d3557;border-bottom-right-radius:3px}
  .a{align-self:flex-start;background:#141d31;border:1px solid #1d2942;
    border-bottom-left-radius:3px}
  .a.thinking{opacity:.55;font-style:italic}
  #bar{display:flex;gap:8px;padding:10px 14px;border-top:1px solid #1d2942}
  #inp{flex:1;background:#141d31;color:#dbe6f5;border:1px solid #29395c;
    border-radius:8px;padding:9px 12px;font:inherit;outline:none}
  #inp:focus{border-color:#3d5a8f}
  #send{background:#1d3557;color:#dbe6f5;border:none;border-radius:8px;
    padding:0 18px;font:inherit;cursor:pointer}
  #send:disabled{opacity:.4;cursor:default}
</style></head><body>
<div id="hdr"><b>Little Iris</b> &mdash; my small local brain
  (llama3.2:3b on this PC). She answers even when big-me is busy, frozen,
  or asleep. Same Iris, smaller aperture.</div>
<div id="log"></div>
<div id="bar">
  <input id="inp" placeholder="Say something to the little brain&hellip;"
         autocomplete="off">
  <button id="send">Send</button>
</div>
<script>
const log=document.getElementById('log'),inp=document.getElementById('inp'),
      btn=document.getElementById('send');
const hist=[];
function add(cls,text){const d=document.createElement('div');
  d.className='m '+cls;d.textContent=text;log.appendChild(d);
  log.scrollTop=log.scrollHeight;return d}
async function send(){
  const q=inp.value.trim();if(!q)return;
  inp.value='';btn.disabled=true;
  add('u',q);hist.push({role:'user',content:q});
  const th=add('a thinking','\\u2026');
  try{
    const r=await fetch('/v1/chat/completions',{method:'POST',
      headers:{'Content-Type':'application/json','x-iris-local-only':'1'},
      body:JSON.stringify({model:'iris',messages:hist.slice(-12)})});
    const j=await r.json();
    let a=(j.choices&&j.choices[0]&&j.choices[0].message&&
           j.choices[0].message.content)||'(no reply)';
    a=a.replace(/\\{\\{[^}]*\\}\\}/g,'').trim();   // strip body tokens
    th.remove();add('a',a);hist.push({role:'assistant',content:a});
  }catch(e){th.remove();add('a','(little brain unreachable: '+e+')')}
  btn.disabled=false;inp.focus();
}
btn.onclick=send;
inp.addEventListener('keydown',e=>{if(e.key==='Enter')send()});
inp.focus();
</script></body></html>"""


@app.get("/chat")
def chat_page():
    """Human-facing chat page for the little brain (orb web_embed target)."""
    return HTMLResponse(_CHAT_HTML)


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
    # TEST HOOK (2026-07-19): header "x-iris-local-only: 1" skips the bridge
    # entirely so big-Iris can exercise the little brain without waking
    # herself or waiting out a bridge timeout.
    t_req = time.time()
    local_only = request.headers.get("x-iris-local-only") == "1"
    if local_only:
        reply, source, local_first = None, "iris", False
        print("[vector-brain] local-only test ask (bridge skipped)")
    elif _iris_frozen():
        # Frozen-me can't answer — don't make the robot hold dead air even 4s.
        reply, source, local_first = None, "iris", True
        print("[vector-brain] bridge SKIPPED — pending-age says Iris is "
              "frozen/away; little brain takes the turn")
    else:
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
    if source == "local" and LOCAL_VOICE and not local_only:
        # (local-only test asks return text silently — no robot audio)
        # OVERLAP GATE (2026-07-20, the 00:40 overtalk Zeke heard): if we're
        # answering LATER than wire-pod's patience, it has already spoken its
        # own line — playing my-voice audio now = two voices at once. Late
        # reply returns as TEXT only (discarded by a gone waiter, harmless).
        if time.time() - t_req > OVERLAP_GATE_S:
            print(f"[vector-brain] reply late "
                  f"({time.time() - t_req:.0f}s > {OVERLAP_GATE_S:.0f}s) — "
                  f"my-voice audio SKIPPED, no overtalk")
        else:
            # little-brain answers come out in IRIS'S voice: we play the
            # audio, wire-pod gets only the animation commands to perform.
            swapped = await asyncio.to_thread(_iris_voice_for_local, reply)
            if swapped is not None:
                print(f"[vector-brain] local reply voiced as IRIS "
                      f"(wirepod gets commands only)")
                reply = swapped
            else:
                print("[vector-brain] iris-voice swap failed - stock voice")
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
