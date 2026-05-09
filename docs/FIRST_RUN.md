# Ava — First Run

A practical walkthrough. From zero to "Ava said something back" in under 10 minutes on a clean install. If you've been away for weeks and need to remember how to start this thing, this is the doc.

> **Path summary:** install Python + Ollama → `pip install -r requirements.txt` plus a handful of extras → confirm Ollama models → `start_ava_desktop.bat` → see "operator HTTP on :5876" → talk to Ava.

---

## 1. What you need installed (system level)

| Tool | Purpose | Where to get |
| --- | --- | --- |
| **Python 3.11** | Agent runtime. Must be 3.11 specifically — `py -3.11` is hardcoded in start scripts. | python.org installer (check "Add to PATH") |
| **Ollama** | Local LLM host. Listens on `127.0.0.1:11434`. | ollama.com |
| **Node 18+** | Tauri UI dev/build. Only needed if you'll edit the UI; the prebuilt exe runs without it. | nodejs.org |
| **Rust toolchain** | Tauri build. Only for `npm run tauri:build`. | rustup.rs |
| **NVIDIA drivers + CUDA 12** | InsightFace GPU mode. Without these Ava runs but face recognition stays on CPU and is slower. | nvidia.com |

Versions known good as of 2026-04-30: Python 3.11.x, Ollama 0.10.x, Node 18.20+, Rust 1.79+.

---

## 2. Required Ollama models

```bash
ollama list
```

Should show at least:

- `ava-personal:latest` — primary fast-path social chat (5 GB).
- `gemma4:latest` — deep-path reasoning (10 GB).
- `nomic-embed-text:latest` — mem0 + vector embeddings (300 MB).

Optional but referenced:
- `ava-gemma4:latest` — identity-baked variant (10 GB). Used as a deep-path fallback.
- `mistral:7b` — concept-graph bootstrap (4 GB).
- `qwen2.5:14b` — self-model weekly update (9 GB).
- `llava:13b` — vision/scene understanding (8 GB).

If a model is missing:

```bash
ollama pull ava-personal:latest
ollama pull gemma4:latest
ollama pull nomic-embed-text:latest
```

`ava-personal` and `ava-gemma4` are user-fine-tuned models. If you don't have the weights cached, you'll need to recreate them via the finetune pipeline (out of scope for first-run). For a fresh install without those, Ava's `_pick_fast_model_fallback()` falls back through `mistral:7b → llama3.1:8b → llama3:8b` automatically — replies will be less personalized but the voice path still works.

---

## 3. Python environment

From the repo root:

```bash
py -3.11 -m pip install -r requirements.txt
```

`requirements.txt` is intentionally minimal. Subsystems pull what they need at import time. **Additional packages you'll need on a clean install** (the imports fail loudly if missing):

```bash
py -3.11 -m pip install "protobuf>=3.20,<4" \
  mediapipe sounddevice silero-vad openwakeword \
  kokoro mem0ai onnxruntime-gpu insightface \
  langchain-core langchain-ollama \
  fastapi uvicorn

py -3.11 -m pip install \
  nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12 \
  nvidia-cuda-nvrtc-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 \
  nvidia-cusolver-cu12 nvidia-cusparse-cu12 nvidia-nvjitlink-cu12
```

> **`protobuf` is pinned to 3.20.x** for MediaPipe compatibility. If something else upgrades it (mem0 sometimes does), restore with `py -3.11 -m pip install "protobuf>=3.20,<4" --force-reinstall`. This is a known hazard noted in `CLAUDE.md`.

---

## 4. Start commands

### Production (packaged exe + Python backend)

```bash
start_ava_desktop.bat
```

What this does:
1. Launches `py -3.11 avaagent.py` in a separate minimized window.
2. Waits for `127.0.0.1:5876` to respond (the operator HTTP API).
3. Launches `apps\ava-control\src-tauri\target\release\ava-control.exe` if present, otherwise falls back to `npm run tauri:dev`.

### Dev mode (hot-reload UI changes)

```bash
start_ava_dev.bat
```

Same backend, but the frontend runs via Vite at `http://localhost:5173` with HMR. Any `.tsx/.ts/.css` change reflects instantly — only Rust / `tauri.conf.json` changes need a full rebuild.

### Backend only (no UI)

```bash
py -3.11 avaagent.py
```

Useful for headless diagnostic runs (the regression test suite uses this pattern). `127.0.0.1:5876` will be live; you can hit it with `curl` or `tools/dev/dump_debug.py`.

If you set `AVA_DEBUG=1` before launching, the `/api/v1/debug/inject_transcript` endpoint becomes available — lets you drive synthetic turns through Ava without a microphone.

---

## 5. What "good startup" looks like

Boot takes about 3 minutes cold (InsightFace cudnn warmup + concept-graph bootstrap + app discovery + Kokoro pipeline load). You should see a sequence like:

```
[startup] step: signal bus
[startup] step: identity / soul / user
[startup] step: tool registry
[startup] step: visual memory
[startup] step: connectivity monitor
...
[concept_graph] stale .tmp removed, bootstrap will run
[startup] step: insight_face GPU engine (background — first-run cudnn warmup ~60-90s, cached after)
[startup] insight_face: ready=True provider=CUDAExecutionProvider
[startup] step: TTS worker (COM-isolated thread)
[tts_worker] loading Kokoro pipeline (this takes ~5s on first run)...
[tts_worker] Kokoro ready (default voice=af_heart)
✅ Vector memory ready using nomic-embed-text
[voice_loop] started passive listening
[voice_loop] started=True — passive listening active
[ava] operator HTTP on :5876 — Ctrl+C to exit.
[prewarm] warming fast path: ava-personal:latest
[prewarm] fast path warmed in 1234ms
```

When you see `[ava] operator HTTP on :5876`, the API is live. Ten seconds after, the prewarm should report success and Ava is ready for her first real turn at the optimized fast-path latency.

---

## 6. What "broken startup" looks like

| Symptom | Most likely cause | Fix |
| --- | --- | --- |
| `ImportError: numpy.core.multiarray failed to import` or any `protobuf` complaint | protobuf got upgraded past 3.20 | `py -3.11 -m pip install "protobuf>=3.20,<4" --force-reinstall` |
| `mediapipe` errors with `MessageFactory` | Same — protobuf | Same fix as above |
| `cufft64_11.dll missing` (or any CUDA DLL) | Missing `nvidia-*-cu12` packages | Reinstall the `nvidia-*-cu12` block in § 3 |
| InsightFace runs on CPU not GPU | CUDA DLLs not on path | Confirm `_add_cuda_paths` log line at startup; reinstall `nvidia-*-cu12` |
| `[ava_memory] disabled (init failed: …)` | mem0 / Chroma init failed | Non-fatal; voice still works. Check that `nomic-embed-text:latest` is pulled and that `memory/mem0_chroma/` is writable. |
| `Kokoro fails to init` | First-run downloads ~360 MB; offline machines fail here | Connect to the internet on first run; subsequent runs use the local cache at `~/.cache/huggingface/hub/`. Falls back to `pyttsx3` automatically. |
| Tauri build fails with `os error 5` | A stale `ava-control.exe` is locked | Kill the running exe before rebuild |
| Wake word fires too often / never fires | Threshold mismatch | `brain/wake_word.py:_DEFAULT_THRESHOLD` (default 0.5; raise to 0.6 for stricter); clap floor is `brain/clap_detector.py:_MIN_THRESHOLD_FLOOR=0.35` |
| `concept_graph.json.tmp` stuck after a crash | Locked tmp from previous instance | Auto-cleared at startup; if it persists, manually `del state\concept_graph.json.tmp` |
| Boot stalls indefinitely on `re.run_ava.entered chars=N` line for the first turn | The `__main__` ↔ `avaagent` alias didn't apply | Confirm avaagent.py line ~3-4 has `sys.modules["avaagent"] = sys.modules["__main__"]`. If missing, reapply commit `f99804e`. |
| `port 5876 already in use` from regression_test.py or any tool | A stale Ava process | `Get-Process | Where-Object { $_.ProcessName -eq "py" -or $_.ProcessName -eq "python" }` then kill it. Or close any minimized "ava" terminal window. |

---

## 7. The first voice test

Once `[ava] operator HTTP on :5876` appears:

### Real microphone

Make sure the Tauri UI is showing "Live" (top-left). Then:

1. **Clap twice** OR **say "hey jarvis"** (the openWakeWord proxy for `hey ava`).
2. The orb should pulse / shift colour when Ava enters listening state.
3. **Say** "what time is it".
4. Ava should reply with the time within 1-3 seconds.

If you don't hear Ava but the UI shows her speaking text streaming, the speakers aren't selected correctly — check `sounddevice.query_devices()` output in the Python window.

### Synthetic (no mic / headless)

```bash
set AVA_DEBUG=1
py -3.11 avaagent.py
```

In a second terminal:

```bash
py -3.11 tools\dev\inject_test_turn.py --text "what time is it"
```

You should get a JSON response with `ok: true` and a non-empty `reply_text`. Add `--wait-audio` to block until Kokoro finishes speaking the reply through your speakers.

### The four-shot regression battery

```bash
py -3.11 tools\dev\regression_test.py
```

This boots Ava as a child process, runs the four-test battery (`time`, `date`, `joke`, `thanks`) plus seven extended tests (conversation_active gating, self-listen guard observability, attentive window decay, wake source variety, weird inputs, sequential fast-path latency, concept_graph save under load), captures `/api/v1/debug/full` before and after, then shuts everything down cleanly. Report at `state/regression/last.json`.

---

## 8. Reading the live state

Two HTTP endpoints make the system self-describing:

```bash
curl http://127.0.0.1:5876/api/v1/snapshot | py -3.11 -m json.tool | head -50
```

The big one — what the UI polls every 5 s. Includes ribbon, heartbeat, models, mood, vision, voice_loop, speech, inner_state_line.

```bash
py -3.11 tools\dev\dump_debug.py
```

Pretty-prints the unified diagnostic payload (`/api/v1/debug/full`): server time, voice_loop state, last 200 stdout lines, last 100 [trace] lines, last 50 errors, dual_brain state, subsystem health, concept_graph counts, app discovery state.

```bash
py -3.11 tools\dev\watch_log.py                # live trace tail
py -3.11 tools\dev\watch_log.py --kind errors  # live error tail
py -3.11 tools\dev\watch_log.py --grep app_disc
```

Tails the rings in real time without scrolling through the boot log.

---

## 9. Where the docs live

| Doc | Purpose |
| --- | --- |
| `docs/ARCHITECTURE.md` | The 10-minute system map. Read this before changing anything substantial. |
| `docs/FIRST_RUN.md` | This file. |
| `docs/HISTORY.md` | Project history — phases 1-100, post-100 hardening, stabilization arcs, cross-phase bug fixes. (Replaces the older `AVA_HANDOFF.md` / `AVA_ROADMAP.md` Section 1 / `AVA_HISTORY.md`, all consolidated 2026-05-01.) |
| `docs/ROADMAP.md` | Forward-looking work — Ready to ship / Designed-awaiting / In design / Awaiting user / Long-term. |
| `docs/BRAIN_ARCHITECTURE.md` | Neuro-symbolic mapping of Ava's modules onto brain regions. |
| `docs/MEMORY_REWRITE_PLAN.md` | The 10-level memory rewrite design (Phases 1-4 shipped, 5-7 designed). |
| `docs/CONVERSATIONAL_DESIGN.md` | Voice naturalness architecture — streaming chunks, tier system, interrupt model. |
| `docs/TRAIN_WAKE_WORD.md` | How to train a custom `hey_ava.onnx` wake word (WSL2 required). |
| `docs/DISCORD_SETUP_NOTES.md` | Discord channel plugin setup, permission relay, .md uploads. |
| `docs/research/voice_naturalness/findings.md` | Research pass for the conversational naturalness work order. |
| `CLAUDE.md` | Project rules + key commands + standing operating rules for Claude Code sessions. |

---

## 10. When something goes wrong mid-session

If voice stops responding:

1. `py -3.11 tools\dev\dump_debug.py | findstr voice_loop`
   What state is the loop in? Should be `passive` (waiting) or `attentive` (open conversation) — never stuck in `thinking` for more than 30 s.

2. `py -3.11 tools\dev\watch_log.py --kind errors`
   Recent errors_recent ring — exception messages with module + traceback. If empty, the failure isn't reaching the error capture, which is itself a clue.

3. `py -3.11 tools\dev\watch_log.py --grep re.run_ava`
   Recent run_ava trace lines. Look for `re.run_ava.entered` followed by silence — that's the cold-import hang signature, fixed by the `__main__` alias.

If TTS fails (Ava replies via UI but doesn't speak):

```bash
curl http://127.0.0.1:5876/api/v1/tts/state
```

Look at `available`, `engine_type` (should be `kokoro`), `muted`. If `muted: true`, the user toggled mute (deliberate or accidental) — toggle it back via the UI's mute button or `POST /api/v1/tts/toggle`.

If the orb drifts off-screen or the UI looks wrong, see `docs/HISTORY.md` § Section 3.1 for the orb-drift fix and `apps/ava-control/src/App.tsx`'s `PRESENCE_V2_ENABLED` / `PRESENCE_V2_CUBE_MORPH_ENABLED` flags — toggling these gates the streaming text and cube morph independently.

---

## 11. Sanity checklist after a long break

Before doing anything substantial, run through this:

- [ ] `ollama list` shows at least `ava-personal`, `gemma4`, `nomic-embed-text`.
- [ ] `py -3.11 -c "import mediapipe, kokoro, openwakeword, sounddevice; print('ok')"` succeeds.
- [ ] `py -3.11 tools\dev\regression_test.py` reports `all_pass=True`.
- [ ] The Tauri UI shows the orb breathing and the EMOTION/HEARTBEAT rows updating every few seconds.
- [ ] Real voice test: clap + "hey ava what time is it" — you get a spoken reply within a few seconds.

If all five pass, Ava is in a healthy state. If any fail, the dump_debug + watch_log tools above will tell you which subsystem is degraded.
