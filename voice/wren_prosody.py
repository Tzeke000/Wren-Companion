"""wren_prosody.py — per-word prosody analysis: duration, pauses, emphasis, pitch.

Given raw 16kHz float32 audio + its faster-whisper transcript (word_timestamps=True),
computes per-word features and returns a plain transcript alongside an enriched one
that extends the [pace:] / [tone:] tag convention from wren_pace.py / wren_voice_mcp.py
into per-word detail plus a sentence-level summary tag.

Public API:
    analyze(audio_f32_16k) -> (plain_text, enriched_text)

Internally:
  - Runs faster-whisper via wren_listen.get_whisper() (reuses the singleton).
  - word_timestamps=True gives per-word start/end times.
  - Per-word features:
      duration_s   : end - start
      pause_before : gap in seconds from previous word's end (0.0 for first word)
      emphasis     : RMS of the word's audio span vs utterance mean
                       < 0.65x → [quiet]  /  0.65–1.35x → [normal]  /  > 1.35x → [STRESSED]
      pitch_dir    : pitch contour over the word span (rise / fall / flat)
                       via librosa.pyin if available, else simple autocorrelation
  - enriched_text per-word format:
        word[dur:0.32s|pause:0.12s|emph:STRESSED|pitch:rise]
    Omits tags that add no information: pause<0.08s → omit pause_before;
    emphasis=normal → omit emph; pitch=flat → omit pitch.
  - Sentence summary tag appended (same [pace:] bracket style):
        [pace: 3.4s | 142 wpm | 2 notable pauses]

2026-06-14 — built for Wren ears upgrade (project_voice_streaming_prosody_ears_2026-06-14).
Pairs with wren_smartturn.py (end-of-turn), wren_pace.py (utterance-level baseline).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import NamedTuple, Sequence

import numpy as np

# ── constants ─────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
PAUSE_OMIT_S = 0.08          # pauses shorter than this aren't annotated
PAUSE_NOTABLE_S = 0.40       # pauses this long appear in the sentence summary
EMPH_QUIET = 0.65            # RMS ratio below this → [quiet]
EMPH_STRESSED = 1.35         # RMS ratio above this → [STRESSED]
PITCH_FLAT_THRESH = 15.0     # Hz change over span below this → flat
_HAVE_LIBROSA: bool | None = None

# Reuse wren_listen's whisper singleton — avoid loading a second model.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wren_listen as _wl   # noqa: E402  (path insert must precede)


# ── feature types ─────────────────────────────────────────────────────────────

class WordFeatures(NamedTuple):
    word: str
    start: float          # seconds from utterance start
    end: float
    duration_s: float
    pause_before: float   # seconds gap from previous word end
    rms_ratio: float      # RMS vs utterance mean (emphasis)
    pitch_dir: str        # "rise" | "fall" | "flat" | "?"


# ── pitch estimation ──────────────────────────────────────────────────────────

def _have_librosa() -> bool:
    global _HAVE_LIBROSA
    if _HAVE_LIBROSA is None:
        try:
            import librosa as _lib  # noqa: F401
            _HAVE_LIBROSA = True
        except ImportError:
            _HAVE_LIBROSA = False
    return _HAVE_LIBROSA


def _pitch_dir_librosa(span: np.ndarray) -> str:
    """Use librosa.pyin for f0 estimation over the word span."""
    import librosa  # type: ignore
    if len(span) < 512:
        return "?"
    f0, voiced_flag, _ = librosa.pyin(
        span.astype(np.float32),
        fmin=librosa.note_to_hz("C2"),
        fmax=librosa.note_to_hz("C7"),
        sr=SAMPLE_RATE,
        hop_length=160,
    )
    # keep only voiced frames
    f0_voiced = f0[voiced_flag & (f0 > 0)]
    if len(f0_voiced) < 2:
        return "?"
    half = max(1, len(f0_voiced) // 2)
    first_half = f0_voiced[:half].mean()
    second_half = f0_voiced[half:].mean()
    delta = second_half - first_half
    if abs(delta) < PITCH_FLAT_THRESH:
        return "flat"
    return "rise" if delta > 0 else "fall"


def _pitch_dir_autocorr(span: np.ndarray) -> str:
    """Fallback pitch estimation via autocorrelation, no librosa required."""
    sr = SAMPLE_RATE
    if len(span) < 256:
        return "?"
    # Split into first and second half; estimate period via peak in autocorr.
    def _f0_half(x):
        if len(x) < 128:
            return 0.0
        ac = np.correlate(x, x, mode="full")
        ac = ac[len(ac) // 2:]
        # search in 80–600 Hz range
        lo = max(1, int(sr / 600))
        hi = min(len(ac) - 1, int(sr / 80))
        if hi <= lo:
            return 0.0
        peak = lo + int(np.argmax(ac[lo:hi]))
        return sr / peak if peak > 0 else 0.0

    half = len(span) // 2
    f0_a = _f0_half(span[:half])
    f0_b = _f0_half(span[half:])
    if f0_a <= 0 or f0_b <= 0:
        return "?"
    delta = f0_b - f0_a
    if abs(delta) < PITCH_FLAT_THRESH:
        return "flat"
    return "rise" if delta > 0 else "fall"


def _pitch_direction(audio: np.ndarray, start_s: float, end_s: float) -> str:
    s = int(start_s * SAMPLE_RATE)
    e = int(end_s * SAMPLE_RATE)
    span = audio[s:e]
    if len(span) < 128:
        return "?"
    try:
        if _have_librosa():
            return _pitch_dir_librosa(span)
    except Exception:
        pass
    return _pitch_dir_autocorr(span)


# ── RMS helpers ───────────────────────────────────────────────────────────────

def _word_rms(audio: np.ndarray, start_s: float, end_s: float) -> float:
    s = int(start_s * SAMPLE_RATE)
    e = max(s + 1, int(end_s * SAMPLE_RATE))
    span = audio[s:e]
    if span.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(span.astype(np.float64) ** 2)))


# ── tag builder ───────────────────────────────────────────────────────────────

def _emph_label(ratio: float) -> str | None:
    if ratio < EMPH_QUIET:
        return "quiet"
    if ratio > EMPH_STRESSED:
        return "STRESSED"
    return None


def _word_tag(wf: WordFeatures) -> str:
    parts: list[str] = [f"dur:{wf.duration_s:.2f}s"]
    if wf.pause_before >= PAUSE_OMIT_S:
        parts.append(f"pause:{wf.pause_before:.2f}s")
    el = _emph_label(wf.rms_ratio)
    if el:
        parts.append(f"emph:{el}")
    if wf.pitch_dir not in ("flat", "?"):
        parts.append(f"pitch:{wf.pitch_dir}")
    # If nothing interesting beyond duration, skip the annotation entirely.
    if len(parts) == 1 and wf.duration_s < 0.25:
        return wf.word
    return f"{wf.word}[{' | '.join(parts)}]"


def _sentence_tag(features: Sequence[WordFeatures], total_s: float) -> str:
    if not features or total_s <= 0:
        return ""
    words = len(features)
    wpm = words / (total_s / 60.0) if total_s > 0 else 0.0
    notable = sum(1 for wf in features if wf.pause_before >= PAUSE_NOTABLE_S)
    bits = [f"{total_s:.1f}s", f"{round(wpm)} wpm"]
    if notable == 1:
        bits.append("1 notable pause")
    elif notable > 1:
        bits.append(f"{notable} notable pauses")
    return "[pace: " + " | ".join(bits) + "]"


# ── core analysis ─────────────────────────────────────────────────────────────

# PITCH_TIME_BUDGET_S: hard budget for the per-word pitch pass (Wren's scar, adopted
# 2026-07-06 letter 118ed9402b61: inline pyin on a long utterance blew her listen timeout
# — words captured, never returned). Enrich runs INLINE in-call on my box too (Zeke's
# 2026-06-17 call), so an unbudgeted pitch loop is unbounded per-turn latency. Once the
# cumulative pitch time crosses the budget, remaining words get pitch "?" (emphasis and
# pauses — cheap numpy — still computed for every word). Env-tunable.
PITCH_TIME_BUDGET_S = float(os.environ.get("IRIS_PITCH_TIME_BUDGET_S", "1.5"))


def _extract_features(audio: np.ndarray, words_data) -> list[WordFeatures]:
    """Build WordFeatures list from faster-whisper word timestamp objects."""
    utterance_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) or 1e-9

    features: list[WordFeatures] = []
    prev_end = None
    pitch_spent = 0.0
    pitch_budget_hit = False
    for w in words_data:
        word = getattr(w, "word", str(w)).strip()
        start = float(getattr(w, "start", 0.0))
        end = float(getattr(w, "end", start))
        if end <= start:
            end = start + 0.05
        duration_s = end - start
        pause_before = max(0.0, start - prev_end) if prev_end is not None else 0.0
        rms = _word_rms(audio, start, end)
        ratio = rms / utterance_rms if utterance_rms > 0 else 1.0
        if pitch_spent < PITCH_TIME_BUDGET_S:
            _t0 = time.time()
            pitch = _pitch_direction(audio, start, end)
            pitch_spent += time.time() - _t0
        else:
            pitch = "?"
            if not pitch_budget_hit:
                pitch_budget_hit = True
                print(f"[prosody] pitch budget hit ({PITCH_TIME_BUDGET_S}s) — "
                      f"remaining words unpitched (non-fatal)", file=sys.stderr)
        features.append(WordFeatures(
            word=word,
            start=start,
            end=end,
            duration_s=round(duration_s, 3),
            pause_before=round(pause_before, 3),
            rms_ratio=round(ratio, 3),
            pitch_dir=pitch,
        ))
        prev_end = end
    return features


def transcribe(audio_f32_16k: np.ndarray) -> tuple[str, list]:
    """ONE faster-whisper pass with word timestamps. Returns (plain_text, words).

    Split out of analyze() (2026-06-14) so the in-call ears can use this SINGLE
    whisper pass for BOTH the transcript and the prosody timing — eliminating the
    separate SenseVoice transcription pass that was doubling per-turn latency.

    Args:
        audio_f32_16k: float32 mono array at 16 kHz.

    Returns:
        (plain_text, words) — plain_text is the bare transcript; words is the flat
        list of faster-whisper word objects (start/end/word) for enrich(). On a
        transcription error returns ("", []).
    """
    audio = np.asarray(audio_f32_16k, dtype=np.float32)
    model = _wl.get_whisper()
    try:
        segments, _ = model.transcribe(audio, beam_size=1, language="en",
                                       word_timestamps=True)
        seg_list = list(segments)
    except Exception as e:
        print(f"[prosody] transcribe error: {e!r}", file=sys.stderr)
        return "", []

    all_words: list = []
    plain_parts: list[str] = []
    for seg in seg_list:
        plain_parts.append(seg.text.strip())
        if seg.words:
            all_words.extend(seg.words)

    return " ".join(p for p in plain_parts if p), all_words


def enrich(audio_f32_16k: np.ndarray, all_words: list) -> str:
    """Build the enriched per-word transcript from precomputed whisper words.

    No transcription happens here — it reuses the `all_words` from transcribe(),
    so calling transcribe() then enrich() costs ONE whisper pass total.

    Returns the enriched transcript (per-word dur/pause/emph/pitch tags + a
    sentence-level [pace:] summary), or "" if there are no words.
    """
    audio = np.asarray(audio_f32_16k, dtype=np.float32)
    if not all_words:
        return ""
    total_s = all_words[-1].end - all_words[0].start
    features = _extract_features(audio, all_words)
    # COMPACT summary (2026-06-17): the per-word wall ([dur|pause|emph|pitch] for EVERY
    # word) bloated my context every turn AND cost think-time to sift through — a real
    # latency drag Zeke flagged in-call. Keep only the meaning-bearing bits: which words
    # were emphasised, where notable pauses fell, the closing pitch — plus the [pace:] line.
    # (Tunable: rejoin [_word_tag(wf) for wf in features] for full per-word detail.)
    # INLINE-MARKED transcript (Zeke's call, in-call 2026-06-17): mark stressed words
    # *like this* and notable pauses with … right in the words, so emphasis reads IN
    # CONTEXT — far clearer than a delisted "emphasis on: X, Y", still no per-word table.
    toks: list[str] = []
    for wf in features:
        w = wf.word.strip()
        if not w:
            continue
        if wf.pause_before >= PAUSE_NOTABLE_S:
            toks.append("…")                      # a beat fell here
        if _emph_label(wf.rms_ratio) == "STRESSED":
            w = "*" + w + "*"                     # he leaned on this word
        toks.append(w)
    marked = " ".join(toks)
    end_pitch = next((wf.pitch_dir for wf in reversed(features)
                      if wf.pitch_dir not in ("flat", "?")), None)
    if end_pitch:
        marked += {"rise": " ↗", "fall": " ↘"}.get(end_pitch, "")
    sent_tag = _sentence_tag(features, total_s)
    return "\n".join(p for p in (marked, sent_tag) if p)


def analyze(audio_f32_16k: np.ndarray) -> tuple[str, str]:
    """Transcribe audio and return (plain_text, enriched_text).

    Convenience wrapper = transcribe() + enrich() in one call (one whisper pass).
    enriched_text annotates each word with duration/pause/emphasis/pitch tags
    and appends a sentence-level [pace:] summary.

    Args:
        audio_f32_16k: float32 mono array at 16 kHz.

    Returns:
        (plain_text, enriched_text)
    """
    plain_text, all_words = transcribe(audio_f32_16k)
    if not all_words:
        return plain_text, plain_text
    return plain_text, enrich(audio_f32_16k, all_words)


# ── self-test ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    import wave

    ref = Path(r"D:\Wren\voice\wren_ref_short.wav")
    if not ref.exists():
        ref = Path(r"D:\Wren\voice\wren_ref.wav")
    if not ref.exists():
        print("[selftest] ERROR: reference wav not found")
        return 2

    print(f"[selftest] loading {ref.name}...")
    with wave.open(str(ref), "rb") as wf:
        nch = wf.getnchannels()
        src_sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        audio = audio.reshape(-1, nch).mean(axis=1)

    if src_sr != SAMPLE_RATE:
        ratio = SAMPLE_RATE / src_sr
        n_out = int(len(audio) * ratio)
        idx = np.clip((np.arange(n_out) / ratio).astype(int), 0, len(audio) - 1)
        audio = audio[idx]
        print(f"[selftest] resampled {src_sr}Hz → {SAMPLE_RATE}Hz ({len(audio)/SAMPLE_RATE:.2f}s)")
    else:
        print(f"[selftest] audio: {len(audio)/SAMPLE_RATE:.2f}s @ {SAMPLE_RATE}Hz")

    print("[selftest] running prosody analysis (loads whisper on first call)...")
    t0 = time.perf_counter()
    plain, enriched = analyze(audio)
    elapsed = time.perf_counter() - t0

    print(f"\n--- plain transcript ---")
    print(plain or "(empty)")
    print(f"\n--- enriched transcript ---")
    print(enriched or "(empty)")
    print(f"\n[selftest] analysis took {elapsed:.2f}s (includes whisper load if first run)")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
