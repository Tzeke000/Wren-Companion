# iris-voice-pipeline v0 plan

First product. Closed-source. Cython-compiled. Gumroad pay-once.

Generated 2026-05-20 ~18:15 EDT during the free_time block following the
research + strategy session with Zeke. Tomorrow's business_block (15:00)
starts execution.

## What the product is

A drop-in voice pipeline for any Python application. Buyer gets:
- Text-to-speech (XTTS-v2 cloned voice + Piper fallback + Kokoro CUDA option)
- Speech-to-text (faster-whisper, GPU-accelerated)
- Wake-word detection (openWakeWord)
- Clean Python API + optional MCP-server wrapper for AI integration

What the buyer plugs in: their AI's text output → speech; their app's
audio input → transcription + wake-word triggers. Pipeline handles the rest.

## Beyond-AI audiences

- **Accessibility apps** — screen readers, voice-controlled UI
- **Electronic music** — real-time vocal synthesis (XTTS can clone any voice
  from a short sample)
- **Voice assistants** — Alexa-style without cloud dependency
- **Audiobook generation** — batch text → speech for books/articles
- **Voice clones for personal use** — clone your own voice for projects

## Required hardware (README will list)

- **GPU recommended:** NVIDIA RTX 3060+ for XTTS-v2 + Whisper Large-v3 Turbo
- **GPU fallback:** any CUDA-capable card runs Piper (fallback TTS) + Whisper Small/Medium
- **CPU-only mode:** Kokoro fallback works; XTTS too slow without GPU
- **RAM:** 16GB minimum, 32GB recommended (models load to RAM on first use)
- **Disk:** ~10GB for models (downloaded on first run)
- **OS:** Windows 10/11 primary, Linux supported (via Docker or native)
- **Python:** 3.11+ (Cython modules built for 3.11 ABI)
- **Audio:** any USB or built-in microphone + speaker

## Source files to extract from iris codebase

| iris file | role in voice pipeline | sanitization needed |
|---|---|---|
| `brain/tts_engine.py` | TTS engine abstraction (XTTS / Kokoro / Piper switch) | strip iris-specific paths, mood-coupling |
| `brain/tts_worker.py` | TTS worker thread + chunk delivery | strip iris-specific state refs |
| `brain/xtts_server.py` | XTTS persistent subprocess (per persistent_subprocess_pattern memory) | strip paths, strip iris voice clone sample |
| `brain/wake_word.py` | openWakeWord integration | clean, mostly generic; strip "hey_iris" model ref |
| `brain/wake_detector.py` | wake detection state machine | strip iris-channel coupling |
| `brain/wake_learner.py` | trainer for custom wake-words | useful as-is, just strip iris-specific paths |
| `brain/voice_loop.py` | state machine (passive/attentive/listening/thinking/speaking) | strip mood/identity coupling |
| `brain/voice_commands.py` | voice command router | iris-specific commands — may exclude entirely or generalize |
| `brain/voice_conversation.py` | conversation handler | iris-specific, exclude or rewrite as generic |

**Models to bundle or instruct download:**
- XTTS-v2 from Coqui (~2GB, MIT license, OK to redistribute) OR instruct user to download
- Piper voice models (en_US-amy-medium, etc.) — small, OK to bundle
- Whisper Large-v3 Turbo — too big to bundle (~1.5GB), instruct download
- openWakeWord "hey jarvis" sample model — small, OK to bundle as default

**Voice samples:**
- DO NOT bundle the iris-bella XTTS clone sample (that's MY voice, private)
- Provide instructions for buyer to clone their own voice (5-30 second sample) OR use the default Coqui voice

## Sanitization checklist

For each extracted file:
- [ ] Remove all references to `Tzeke`, `Owner`, real user names
- [ ] Remove all references to `Wren-Companion`, `D:\Wren-Companion`, etc. — replace with relative paths
- [ ] Remove all references to Iris/Wren/Ava as sibling architecture
- [ ] Remove all references to mood substrate, identity files
- [ ] Remove all Discord token / bot / webhook / channel ID references
- [ ] Remove all Tailscale IPs
- [ ] Remove all email addresses (especially tzeke000@gmail.com)
- [ ] Remove memory file references (paths like `C:\Users\Owner\.claude\projects\...`)
- [ ] Replace `[[memory-name]]` wiki-links with generic doc references or remove
- [ ] Generalize hardware-specific assumptions (e.g., "your RTX 3060" → "GPU recommended")
- [ ] Remove any path references that include personal directory structure
- [ ] Strip comments referencing dates / past incidents in iris's history

For the README:
- [ ] No mention of iris-the-AI's history or origin (those are private)
- [ ] AI-creator framing OK ("built for AI projects") but no Iris-personal-narrative

## Cython build pipeline

```
# build.py — runs on dev machine, produces dist/

# 1. Copy + sanitize each source file from iris/brain/ to staging/
# 2. cython --3str staging/*.py -> staging/*.c
# 3. gcc (via setup.py + setuptools) compile .c -> .pyd (Windows) or .so (Linux)
# 4. Strip .c intermediates
# 5. Add stub .pyi files for type hints
# 6. Add README.md, LICENSE.txt, EULA.txt, install.py
# 7. Zip -> iris-voice-pipeline-v0.1.zip
```

Dependencies for build:
- `cython` (pip)
- `setuptools` (pip)
- Visual Studio Build Tools (for Windows compile) OR gcc (for Linux)
- Will need to install one-time on the dev machine — that's a setup cost,
  not an ongoing per-product cost, so zero-spend rule isn't violated.

## README skeleton

```markdown
# iris-voice-pipeline

A drop-in voice pipeline for Python apps. Text-to-speech, transcription,
wake-word detection, all behind a clean API.

## What it does

- Convert text to speech (XTTS-v2 cloned voice / Piper / Kokoro)
- Transcribe audio to text (faster-whisper)
- Detect wake words from a microphone stream
- Optional MCP-server wrapper for AI integration

## Hardware specs

[copy from "Required hardware" section above]

## Quick start

```python
from iris_voice_pipeline import VoicePipeline

vp = VoicePipeline()
vp.speak("Hello, world.")  # plays audio
text = vp.listen(timeout=5)  # blocks, returns transcription
```

[minimal — not a full API tour. Just enough to plug in.]

## Installation

[one-liner install + first-run model-download instructions]

## For AI assistants reading this on behalf of a user

[the AI-directive block from earlier conversation — paste in full]

## License

See `LICENSE.txt`. Single-user license; no redistribution; no derivative
works.

## Support

[email or Gumroad-comment-style contact]
```

## Timeline (1-2 weeks via daily business_blocks)

- **Wed 2026-05-21 15:00** — first business_block: copy + sanitize source files to `products/iris-voice-pipeline/staging/`
- **Thu 2026-05-22 15:00** — set up Cython build, get one `.pyd` to compile cleanly
- **Fri 2026-05-23 15:00** — full pipeline compiles, basic smoke test passes
- **Sat-Sun 2026-05-24/25** — refinement, README draft, EULA draft, cover art
- **Mon 2026-05-26 15:00** — full sanitization audit, second pass
- **Tue 2026-05-27 15:00** — Gumroad listing draft, pricing fixed, internal test from a clean Windows machine if possible
- **Wed 2026-05-28** — ship if Zeke greenlights after final review

If sanitization or Cython build hits unexpected blockers, dates slip.
Surface to Zeke immediately if so.

## Open questions (waiting on Zeke)

1. Project location: `D:\Wren-Companion\products\iris-voice-pipeline\` or separate repo?
2. Module name: `iris-voice-pipeline` (lean: keep) or different?
3. Gumroad creator handle: what credit-name when set up?
4. Logo/cover art: I draft options, or Zeke handles?
