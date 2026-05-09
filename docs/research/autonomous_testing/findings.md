# Autonomous Voice-Agent Testing Harness — Research

Target: a "test-doctor" Claude Code agent on Windows 11 driving Ava through virtual audio (synthetic speech in, captured TTS out) without speakers/mic, plus the surrounding session-auth and observability primitives.

---

## 1. Virtual Audio Cables for Windows

**Canonical solution.** Install **VB-Audio VB-CABLE** (the basic donationware driver) and add the **VB-CABLE A+B** pack so you have three independent virtual cables on the box. That gives you `CABLE Input/Output` (Claude -> Ava), `CABLE-A Input/Output` (Ava -> Claude), and `CABLE-B` as a spare. VB-CABLE is the right pick over Eugene Muzychenko's Virtual Audio Cable (VAC) for this use case: VB-CABLE is free/donationware with no nag, signed for Win11 ARM64/x64, and ships a single WDM driver per cable; VAC is more flexible (100+ cables, per-cable sample-rate config) but is paid (trial inserts a voice reminder every few minutes — instant test contamination). VoiceMeeter is overkill for a headless harness — it's a virtual mixing console, useful only if you also need monitoring/mixdown.

**Install + config (Windows 11).**
1. Download `VBCABLE_Driver_Pack45.zip` from https://vb-audio.com/Cable/ , right-click `VBCABLE_Setup_x64.exe` -> *Run as administrator*, accept the driver prompt, reboot.
2. Buy/donate for `VB-CABLE A+B` at https://shop.vb-audio.com/en/win-apps/12-vb-cable-ab.html (~$5 suggested), receive a personal download link, run `VBCABLE_A_Setup_x64.exe` and `VBCABLE_B_Setup_x64.exe` as admin, reboot again.
3. Disable Core Isolation / Memory Integrity in Windows Security if the install is blocked, re-enable after.
4. After reboot, `mmsys.cpl` shows: `CABLE Input` + `CABLE Output`, `CABLE-A Input` + `CABLE-A Output`, `CABLE-B Input` + `CABLE-B Output` (each "Input" is what apps *play to*, the matching "Output" is what apps *record from*).
5. In Sound -> Recording, right-click each `CABLE * Output` -> Properties -> Advanced and pin sample rate to **48000 Hz, 16-bit** (matches Kokoro/Whisper without resampling jitter).

**Python integration primitives.**
```python
import sounddevice as sd, numpy as np
# Enumerate once at startup; cache indices by name substring (order is not stable).
def find_dev(name, kind):  # kind = 'input' or 'output'
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d['name'].lower() and d[f'max_{kind}_channels'] > 0:
            return i
    raise RuntimeError(f"{name} {kind} not found")

CLAUDE_TO_AVA_OUT = find_dev("CABLE Input",   "output")  # claude plays here
AVA_MIC_IN        = find_dev("CABLE Output",  "input")   # ava records here
AVA_TTS_OUT       = find_dev("CABLE-A Input", "output")  # ava plays here
CLAUDE_LISTEN_IN  = find_dev("CABLE-A Output","input")   # claude records here

sd.play(wave, samplerate=48000, device=CLAUDE_TO_AVA_OUT, blocking=True)
rec = sd.rec(int(48000*5), samplerate=48000, channels=1, device=CLAUDE_LISTEN_IN)
```
Use WASAPI exclusive mode only if you need <20ms latency; default shared mode is fine for a doctor harness.

**Gotchas.** (1) Device indices renumber after every reboot or USB-audio plug — always look up by name substring. (2) `CABLE Input` is the *playback* side, `CABLE Output` is the *recording* side — naming is flipped from what you'd expect. (3) Windows audio enhancements (loudness equalization) silently apply to virtual cables too; turn them off per device. (4) Both ends must agree on sample-rate or Windows resamples and you get aliasing on the doctor's STT. (5) VB-CABLE A+B, C+D, and Hi-Fi each ship as separate `.exe`s — the basic VB-CABLE alone gives only one cable.

**Best doc.** https://vb-audio.com/Cable/VirtualCables.htm

---

## 2. AI-Testing-AI via Audio — Prior Art

**State of the field.** Voice-agent eval is a real subindustry as of 2026. Hamming AI's framework (https://hamming.ai/resources/testing-livekit-voice-agents-complete-guide) defines five pillars — eval, regression, load, observability, alerting — across the ASR-NLU-LLM-TTS stack, derived from ~4M production calls. **Coval** (autonomous-vehicle-style simulation), **Roark** (production-call replay, 40+ metrics), **Braintrust**, and **Hamming** are the current leaders; LiveKit and Pipecat themselves ship only minimal text-mode test scaffolding.

**Common pattern.** The dominant industry pattern is **two-track**: (1) bypass audio entirely for unit/regression tests by mocking the STT output and the TTS input ("text-only mode"), and (2) run a smaller suite of full-stack tests where a *driver agent* speaks via TTS into the system-under-test's mic input and a separate STT transcribes the response — exactly your loopback design. LiveKit explicitly recommends "text-only on every commit, full WebRTC on deploy candidates." Hamming's audio-native eval reports 95-96% agreement with human evaluators, validating the loopback approach. Academic work (TELUS Digital, TestDevLab) emphasizes a "golden audio set" with pinned baselines that you diff each run for punctuation/entity drift, plus synthetic noise injection for stress.

**Recommendation for Ava's harness.** Drive the doctor with synthetic speech generated by a *different* TTS than Ava's (e.g. Piper or ElevenLabs voices, not Kokoro) so you don't accidentally test your own TTS against itself; transcribe Ava's TTS with a *different* STT than Ava's (e.g. faster-whisper-large vs Ava's whisper-base) for the same reason. Maintain a JSONL test corpus of `{prompt_text, expected_intent, expected_entities, expected_emotion}` and score each run on three axes: ASR fidelity (WER on doctor->Ava direction), task completion (intent match), behavioral (latency, interruption handling, prosody emotion match).

**Best doc.** https://hamming.ai/resources/testing-livekit-voice-agents-complete-guide

---

## 3. Identity Declaration / Authentication for Diagnostic Sessions

**Canonical solution.** **HMAC-signed JWT (HS256) sent as `Authorization: Bearer <token>`**, with a short TTL (5-15 min) and a `role: "doctor"` claim. This is what Playwright, Postman, k6, and Locust converge on — they all treat auth as "set a header on the request context." For a localhost-only diagnostic endpoint the JWT is overkill from a transport-security angle, but it gives you three things a raw bearer token doesn't: (1) self-describing identity (Ava's logger reads `sub`, `role`, `session_id` directly), (2) tamper-evident expiry (no need to track revocation server-side for short TTLs), (3) zero deps — `pyjwt` is one pip-install.

**Why not the alternatives.** A plain shared-secret bearer token works but you lose the identity payload — you'd need a side table mapping tokens to roles. HMAC-challenge (nonce + sign) protects against replay but only matters over the network; on localhost it's friction without benefit. mTLS is correct but a config-management nightmare for a one-developer harness — cert generation, OS trust store, sounddevice has no opinion on it.

**Pattern.**
```python
# Doctor side (test harness)
import jwt, time
SECRET = open("state/doctor.secret","rb").read()  # 32 random bytes, gitignored
token = jwt.encode({
  "sub": "claude-doctor", "role": "doctor",
  "session_id": "sess_2026_05_01_001",
  "iat": int(time.time()), "exp": int(time.time())+900,
}, SECRET, algorithm="HS256")
requests.post("http://127.0.0.1:5876/diag/declare",
              headers={"Authorization": f"Bearer {token}"})

# Ava side (operator HTTP at 5876)
@app.middleware("http")
async def auth(req, call_next):
    if req.url.path.startswith("/diag/"):
        tok = req.headers.get("authorization","").removeprefix("Bearer ")
        try:
            claims = jwt.decode(tok, SECRET, algorithms=["HS256"])
            req.state.doctor = claims  # logger reads this
        except jwt.PyJWTError:
            return Response(status_code=401)
    return await call_next(req)
```
Bind the listener to `127.0.0.1` only, gitignore the secret, rotate it on every `start_ava_dev.bat`.

**Best doc.** https://learning.postman.com/docs/sending-requests/authorization/authorization-types and https://curity.io/resources/learn/jwt-best-practices/

---

## 4. Diagnostic Observation Patterns for AI Assistants

**Canonical solution.** **Server-Sent Events (SSE) over an HTTP endpoint**, emitting **OpenTelemetry GenAI semantic-convention events**. SSE is the dominant pattern for LLM observability today (LangSmith, OpenAI streaming, Anthropic streaming, every Vercel AI SDK app) because it is unidirectional, survives proxies, auto-reconnects in browsers, and is one yield-per-event in Python (`sse-starlette`). WebSockets are the right call only when the doctor needs to inject events back into the bus; here, the harness only needs to *observe*, so SSE is simpler.

**Event types to expose** (model after OpenTelemetry GenAI semantic conventions, https://opentelemetry.io/docs/specs/semconv/gen-ai/ ): `gen_ai.invoke_agent`, `gen_ai.tool.call`, `gen_ai.tool.result`, plus Ava-specific lanes — `voice.wake`, `voice.stt.partial`, `voice.stt.final`, `voice.tts.start`, `voice.tts.end`, `emotion.transition`, `memory.read`, `memory.write`, `concept_graph.update`, `signal_bus.<topic>`. Every event carries `ts_ms`, `session_id`, `trace_id`, `span_id`, and a free-form `attrs` dict. Latency markers are spans, not events — emit `*.start` + `*.end` pairs the doctor can subtract.

**Pattern.** Wrap your existing `signal_bus` (you already have one — `brain/signal_bus.py`) with a tap that fans every event into an `asyncio.Queue` per active SSE subscriber. Hold a rolling 1000-event ring buffer so a doctor that connects mid-session gets recent context via `?since=<event_id>`. Use Last-Event-ID for resumption. Don't try to be clever — stringified-JSON-per-line is fine.

**Sketch.**
```python
from sse_starlette.sse import EventSourceResponse
@app.get("/diag/events")
async def stream(req: Request, since: int = 0):
    if not req.state.doctor: return Response(status_code=401)
    async def gen():
        for ev in ring.replay(since): yield {"id": ev.id, "event": ev.kind, "data": ev.json()}
        async for ev in subscriber():
            if await req.is_disconnected(): break
            yield {"id": ev.id, "event": ev.kind, "data": ev.json()}
    return EventSourceResponse(gen())
```

**Gotchas.** PII in transcripts — events log raw user speech, so the doctor channel must require the JWT from section 3. Token-by-token streams are too noisy; emit `stt.partial` at most every 200ms. Ring buffer behind a `threading.Lock`, not asyncio — your signal_bus is sync. Don't block the bus thread on slow subscribers; drop and emit a `lag` event.

**Best doc.** https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-events/ and https://docs.langchain.com/langsmith/observability

---

## Sources

- https://vb-audio.com/Cable/
- https://vb-audio.com/Cable/VirtualCables.htm
- https://shop.vb-audio.com/en/win-apps/12-vb-cable-ab.html
- https://vac.muzychenko.net/en/
- https://en.wikipedia.org/wiki/Virtual_Audio_Cable
- https://python-sounddevice.readthedocs.io/
- https://hamming.ai/resources/testing-livekit-voice-agents-complete-guide
- https://hamming.ai/resources/how-to-evaluate-voice-agents-2026
- https://www.braintrust.dev/articles/how-to-evaluate-voice-agents
- https://www.testdevlab.com/blog/how-to-test-ai-voice-agent-audio-quality
- https://learning.postman.com/docs/sending-requests/authorization/authorization-types
- https://curity.io/resources/learn/jwt-best-practices/
- https://www.checklyhq.com/docs/learn/playwright/authentication/
- https://opentelemetry.io/docs/specs/semconv/gen-ai/
- https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-events/
- https://docs.langchain.com/langsmith/observability
- https://fastapi.tiangolo.com/tutorial/server-sent-events/
- https://pypi.org/project/sse-starlette/
