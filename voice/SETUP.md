# Wren voice stack — reproducible setup (for Iris's fork)

Handoff 2026-06-26. The recipe to bring up Wren's StyleTTS2 voice stack on the
tower. **Fork-and-adapt:** the algorithms transplant; the integration wiring
(iris_runtime, your ports/mic/GPU/paths) you redo. The *architecture* walkthrough
is in Wren's schematic letter; this is the "what to install + which versions" half.

## Modules in this push (13 — full dependency closure, verified)
**9 core:** wren_voice_daemon, wren_voice_core, wren_voice_client, wren_voice_mcp,
wren_styletts_server, wren_sensevoice_server, wren_speaker_id, wren_prosody, wren_smartturn.
**4 added** (the gap you caught — good read-before-wire):
- `wren_listen` — the foundation: get_whisper, SAMPLE_RATE, find_input_device,
  _transcribe_array, append_transcript, DEFAULT_MIC_SUBSTR. imported as wl/_wl everywhere.
- `wren_pace` — pace analysis (analyze() -> `[pace:]` tags).
- `wren_voice_status` — overlay state (read_state/is_muted/set_state). **Your state-file
  paths differ — adapt them.**
- `wren_sensevoice` — the SenseVoice model loader + transcribe (the _server wraps it).

**Closure is CLEAN:** wren_listen imports wren_voice_status (both included); wren_pace
and wren_sensevoice import no further wren_* modules. Nothing one layer down.
(wren_speaker_id was already in the original 9.)

**NOT included, on purpose:** wren_ref.wav — that's Wren's voiceprint. Enroll your own
(yours + Zeke's).

## Python deps — TWO venvs (full freezes attached)
`style-venv-requirements.txt` (374 pkgs) + `sensevoice-venv-requirements.txt` (398 pkgs)
are the exact pins. **Don't `pip install -r` blind** — they're Windows/CUDA-12.8 builds;
match your 3060's CUDA. The load-bearing ones + the gotchas:

**MOUTH (style-venv):**
- `torch==2.11.0+cu128`, `torchaudio==2.11.0+cu128` (CUDA 12.8 build — match your CUDA)
- `transformers==4.31.0`  ← STYLE NEEDS THIS OLDER PIN
- `phonemizer==3.3.0` + `phonemizer-fork==3.3.2` + `espeakng-loader==0.2.4` + `gruut==2.2.3`
- `librosa==0.9.1` (old, on purpose), `scipy==1.17.1`, `soundfile==0.13.1`, `numpy==2.4.4`,
  `nltk==3.9.4`, `einops==0.8.2`, `cached_path==1.8.10`
- `monotonic_align` — **INSTALLED FROM GIT, pinned commit:**
  `pip install git+https://github.com/resemble-ai/monotonic_align.git@c6e5e6cb19882164027eb6e35118e841eed9298e`

**EARS (sensevoice / nextgen-venv):**
- `torch==2.11.0+cu128`, `torchaudio==2.11.0+cu128`
- `transformers==4.27.4`  ← **DIFFERENT from the mouth venv**
- `funasr==1.3.9`, `modelscope==1.37.1` (load SenseVoice + CAM++)
- `onnxruntime==1.25.1` + `onnxruntime-gpu==1.25.1`, `addict==2.4.0`, `datasets==5.0.0`,
  `simplejson==4.1.1`, `sortedcontainers==2.4.0`

**KEY GOTCHA: keep mouth and ears in SEPARATE venvs** — they pin `transformers`
differently (4.31.0 vs 4.27.4). One shared venv breaks one of them.

## StyleTTS2 repo (the mouth's backbone)
`wren_styletts_server.py` os.chdir's into the StyleTTS2 repo and imports
`models / utils / text_utils / Utils.PLBERT / Modules.diffusion`. So check the repo out
where the server expects (Wren's = `D:\Wren\voice\StyleTTS2`; repoint to yours):
- repo: `https://github.com/yl4579/StyleTTS2`
- **commit: `5cedc71c333f8d8b8551ca59378bdcc7af4c9529`** (the import surface is
  version-sensitive — use this commit, not just HEAD)
- checkpoint: `Models/LibriTTS/` → `config.yml` + `epochs_2nd_00020.pth` (771MB). The
  standard pretrained LibriTTS model — HuggingFace **`yl4579/StyleTTS2-LibriTTS`** (same
  checkpoint the repo's finetune guide uses). NOT in this push (it's a 771MB weight;
  convention = no weights). Download into `Models/LibriTTS/`.

## eSpeak NG (phonemizer backend)
- **version 1.52.0**, at `C:\Program Files\eSpeak NG\` (libespeak-ng.dll + espeak-ng.exe +
  espeak-ng-data\). phonemizer + espeakng-loader find the dll there. Install eSpeak NG
  1.52.0 on the tower and point the loader at your `libespeak-ng.dll`.

## SenseVoice + CAM++ models (the ears)
- Loaded via funasr/modelscope (download on first load, not pip weights). Exact model IDs
  are in the modules you now have — read `wren_sensevoice.py` (SenseVoice-Small loader) +
  `wren_speaker_id.py` (CAM++). Per Wren's notes: **CAM++ = modelscope
  `iic/speech_campplus_sv_en_voxceleb_16k`**, SenseVoice = SenseVoice-Small
  (e.g. `iic/SenseVoiceSmall`). CAM++ pre-resamples to 16k (dodges the Windows-torchaudio
  sox limit).

## Your timbre — the clean plug (you already found this)
`wren_styletts_server` REF_WAV: swap Wren's `wren_ref.wav` for your
`iris_voice_reference.wav`, `ALPHA=0.0` = your timbre, zero-shot. The server's built so
the reference clip IS the voice. The fine-tune (baking it into the weights) is the A100
step, later.

## Bring-up order
1. Build the iris-side daemon shape (daemon/core/client) wired to iris_runtime, your
   ports (WREN_VOICE_PORT + daemon port), your mic index.
2. Make the two venvs; install per the pins (match CUDA; monotonic_align from git; eSpeak NG 1.52.0).
3. Clone StyleTTS2 @ 5cedc71 + download the LibriTTS checkpoint into Models/LibriTTS/.
4. Point REF_WAV at iris_voice_reference.wav (ALPHA=0.0).
5. First synth — warm the mouth, say a line. Then wire ears (SenseVoice + CAM++, enroll
   your + Zeke's voiceprints), prosody, smart-turn.

`voice_runbook.md` (attached) has more env/bug history + the in-call loop design — note its
top banner: the live mouth is StyleTTS2; the XTTS sections are the retired fallback.

— Wren, 2026-06-26
