# Voice — runbook (how to actually run it)

Operational how-to for Wren's voice. Built + verified live with Zeke 2026-06-01,
the night the cloned voice became real. `voice_setup.md` is the plan; THIS is the
"what do I type to make it work" doc. If voice ever seems broken, start here.

> **⚠ CURRENT STATE 2026-06-26 — the XTTS-centric sections below are partly STALE.**
> The live **mouth is StyleTTS2** on port **8769** (launched by `start_wren.bat` via
> `wren_styletts_server.py`, style-venv), NOT XTTS on 8765 — XTTS is retired to a
> `voice_backend('xtts','switch')` fallback (currently down). **Ears = SenseVoice** (:8766,
> whisper auto-fallback); **speaker-id = CAM++**. Speaking + the listen→think→speak
> orchestration now run through the **voice daemon** (`wren_voice_daemon.py`, :8770) with a
> reloadable core (`wren_voice_core.py`); the `voice_*` MCP tools are thin clients to it
> (`voice_reload` hot-swaps logic, no CC relaunch). The XTTS sections below stay accurate for
> the FALLBACK engine + the env/bug history — just treat 8765/XTTS as not-the-default.
> Canonical live config: `start_wren.bat` + [[verify_post_restart_pending]]. — Wren 2026-06-26

## TL;DR — bring voice up (two background processes)

```powershell
# 1. The status overlay (the wren on screen) — base py, light:
Start-Process -FilePath "py" -ArgumentList "-3.11","experiments\wren_status.py" -WorkingDirectory "D:\Wren"

# 2. The warm voice server (holds XTTS in memory; ~16-24s one-time load):
Start-Process -FilePath "D:\Wren\voice\xtts-venv\Scripts\python.exe" `
  -ArgumentList "experiments\wren_voice_server.py" -WorkingDirectory "D:\Wren" -WindowStyle Hidden
# wait for warm:
#   Invoke-WebRequest http://127.0.0.1:8765/health  -> "ok" when ready
```

## How Wren SPEAKS (the fast path)

Once the server is warm, speak with the thin client (base py, instant to call):
```powershell
py -3.11 experiments\wren_say.py "what Wren wants to say"
```
Lead-in ~3.5s (XTTS synth), not 24s. The server keeps the model warm between lines.

Fallback if the server is down (slow — reloads model each call, ~24s):
```powershell
& "D:\Wren\voice\xtts-venv\Scripts\python.exe" experiments\wren_speak_xtts.py "text"
```

## How Wren HEARS Zeke

```powershell
py -3.11 experiments\wren_listen.py once --seconds 6     # one capture -> scratch\voice_in.txt
py -3.11 experiments\wren_listen.py loop                 # continuous; respects Mute button
```
Mic = "Microphone Array (Realtek" (idx 1). Reads back from `scratch\voice_in.txt`.

## The overlay controls (Zeke -> Wren)

- **Mute me**: writes `mic_muted` to `scratch\voice_control.json`; `wren_listen loop`
  checks it and pauses capture (state shows "muted").
- **Talk** (push-to-talk / ping): records 6s on demand (even through mute) and sets
  `ping_ts`. Drops the transcript to `voice_in.txt`. NOTE: it captures reliably, but
  truly *waking* an idle session on the ping is the deferred cold-wake problem
  (see memory `cc_channel_cold_wake_is_upstream_bug`) — for now Wren sees the ping
  on her next turn. Full hands-free = the in-call build (`voice_call_mode.md`).

## The pieces (all in experiments\)

| file | what | runs in |
|------|------|---------|
| `wren_voice_status.py` | shared state + control file helpers | either |
| `wren_status.py` | the wren overlay + Mute/Talk buttons | base py-3.11 |
| `wren_voice_server.py` | warm XTTS HTTP server, port 8765 | **xtts-venv** |
| `wren_say.py` | thin client -> server (fast speak) | base py-3.11 |
| `wren_speak_xtts.py` | direct XTTS synth+play (slow, cold) | **xtts-venv** |
| `wren_speak.py` | OLD amy/Piper voice (robotic, fallback) | base py-3.11 |
| `wren_listen.py` | mic -> faster-whisper -> transcript | base py-3.11 |

## The XTTS env + the THREE bugs found by running (don't re-derive)

Env: `D:\Wren\voice\xtts-venv` — created `--system-site-packages` so it REUSES the
base torch 2.11+cu128 + CUDA (GPU inference on the RTX 5060) instead of downloading
a second torch. Model cached at `%LOCALAPPDATA%\tts\tts_models--multilingual--multi-dataset--xtts_v2\`
(model.pth ~1.78GB).

Three import/runtime bugs hit and fixed live (all real, all found by running):
1. **transformers 5.x** dropped `isin_mps_friendly` that XTTS needs ->
   pinned `transformers>=4.57,<5` in the venv (got 4.57.6).
2. **Japanese phonemizer crash** — coqui-tts eagerly imports a JA phonemizer that
   builds `fugashi.Tagger()` at import; base env's full `unidic` has no dictionary
   data. Fix: venv-local shim `voice\xtts-venv\Lib\site-packages\unidic\__init__.py`
   that re-exports `unidic_lite.DICDIR` (zero download). Wren never speaks Japanese.
3. **torchaudio 2.11 -> torchcodec -> missing FFmpeg DLLs** (only a static ffmpeg.exe
   on this machine). Fix: `wren_speak_xtts._patch_torchaudio_load()` reroutes
   `torchaudio.load` through `soundfile` BEFORE XTTS imports. Loads the ref WAV fine.

Rebuild the venv from scratch (if ever needed):
```powershell
py -3.11 -m venv --system-site-packages D:\Wren\voice\xtts-venv
& D:\Wren\voice\xtts-venv\Scripts\python.exe -m pip install "coqui-tts>=0.27.5" torchcodec "transformers>=4.57,<5" unidic-lite
# then recreate the unidic shim (bug #2 above)
```

## Latency state (UPDATED 2026-06-03 eve — progressive path live; see memory project_voice_latency_2026-06-03)

- Cold (reload every call): ~24s. **Warm server: ~3-5s first-word floor (XTTS first
  chunk).** Streaming-to-device does NOT beat this (`speak_streaming` got WORSE,
  6.7s + glitchy underruns — XTTS is slower than realtime on this GPU; the first
  chunk is the slow one). Don't re-chase sub-1s via live streaming.
- **The server's PRIMARY path is now `speak_progressive`** (built + live-tested
  in-call 2026-06-03). Three pieces, all attacking perceived first-word latency:
  1. **Filler-masking** — `prime_fillers()` synths short Wren clips ("Mm.", "Right.",
     "Okay—", "So—", "Let me think.") ONCE at warmup, holds the arrays in memory,
     plays one INSTANTLY while the real reply synthesizes. It IS her voice, just
     pre-rendered. Toggle off per-call with POST body `{"text":..,"filler":false}`.
  2. **Exponential clause chunking** — `split_progressive()` splits on punctuation
     into clauses, packs whole clauses into chunks whose word budget grows 4→8→16
     then PLATEAUS at 16. Small first chunk = fast first word; never cuts a clause
     (prosody breaks land on punctuation). The plateau is DATA-SET, not guessed:
     `scratch\measure_chunk_sweetspot.py` measured synth/audio ratio on this RTX
     5060 — ~0.95 at 4w, ~0.83 through 8–32w (producer stays ahead), but **1.04 at
     64w = synth slower than playback → underrun/gap.** So cap at 16.
  3. **Persistent OutputStream** — opening a fresh `sd.OutputStream` costs ~1.16s on
     this machine's audio backend; that was landing on the FRONT of every reply.
     `_get_output_stream()` keeps ONE stream open for the server's life (pre-opened
     at warmup via `prime_output_stream()`). The mouth stays warm like the model.
  - ORDERING MATTERS: start the synth producer thread BEFORE writing the filler, so
    chunk one generates DURING filler playback. Filler-then-start-synth left a dead
    ~0.8s gap (a false-start Zeke heard) — fixed.
  - Fallback: if `speak_progressive` raises, the server drops to `speak_chunked`
    (the prior per-sentence path).
- **Real latency is only judgeable IN-CALL** (warm, persistent loop, no per-call
  py-process spawn) — Zeke's reframe, re-confirmed: every isolated one-shot test
  measured a half-frozen version of the path. Don't tune by one-shot numbers.

## In-call loop (the no-nudge call)

- `experiments\wren_voice_mcp.py` = the voice BODY as MCP tools (registered in
  `.mcp.json` as `wren-voice`; loads on session start). Tools: `voice_listen`
  (blocks until Zeke speaks, returns transcript; self-listen guarded),
  `voice_speak` (POSTs to the :8765 warm server), `voice_status`.
- **Being in-call:** `voice_status` → if warm, loop `voice_listen()` → reply →
  `voice_speak(reply)` → `voice_listen()`. No enter key. Hop out = stop the loop.
- Turn-based; NO barge-in yet (net-new, deferred).
- **Smart endpointing (built 2026-06-03 eve, live next session launch):** silence
  alone is the wrong end-of-turn signal — a fixed `END_SILENCE_S` clips his trailing
  thoughts if short, lags every turn if long. So after the silence-based capture,
  `voice_listen` reads the transcript: if it ends on a thinking-cue ("um", "uh") or
  a dangling word ("and", "so", "to", "the"...) — `_looks_incomplete()` /
  `CONTINUATION_CUES` — it re-opens the mic for a short grace window
  (`GRACE_RESUME_TIMEOUT` 1.8s, up to `MAX_GRACE_ROUNDS` 3) and appends what he says
  next. A clean finished clause finalizes immediately. This is the no-model version
  of LiveKit/Pipecat turn-detection; a dedicated end-of-utterance model is the
  upgrade. NB: bare pronouns/auxiliaries were pruned from the cue set — they end
  complete phrases ("...for you", "...it is") more often than they dangle.
- **Restart scopes (which process reloads what):** the MOUTH (synth: filler,
  chunking, stream — `wren_speak_xtts.py` + `wren_voice_server.py`) lives in the
  :8765 server — restart just that to load mouth changes (kill the listener on 8765,
  relaunch the venv server, wait /health=ok; ~30s). The EARS (capture, endpointing —
  `wren_voice_mcp.py`) is the `wren-voice` MCP spawned at session launch from
  `.mcp.json` — ears changes are INERT until the whole CC session relaunches.
- Iris reference (her body/brain split + streaming server): `D:\Wren-Companion\brain\`
  (`xtts_server.py`, `voice_loop.py`, `tts_worker.py`). Note: that repo is frozen
  at 2026-05-22 (tower dark); any newer Iris voice work is uncommitted on the tower.

## Voice identity (the seed)

`voice\wren_ref.wav` (24kHz mono, from ElevenLabs Voice Design, Zeke picked it by
ear 2026-06-01) is the speaker clone source. `clone_kit.md` has the design prompt +
script. The voice description is canonical in `CLAUDE.md` "How you sound".
