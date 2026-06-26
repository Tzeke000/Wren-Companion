"""wren_pace.py — measure the TIMING of speech so the meaning in tempo isn't lost.

Zeke 2026-06-04: relative time matters. If he says a few words slower than his normal,
that means something, the same way pitch does. "You don't have to do that work — the
program does it, and you just read what it captured." So this is the BODY measuring;
the brain (me) just reads the compact annotation it returns.

What it measures from a captured utterance (audio array + transcript):
  - effective speaking rate (words per minute over his active speaking span),
  - that rate RELATIVE to his own rolling baseline (slower / usual / faster),
  - notable internal pauses (deliberation, hesitation).

The baseline persists in scratch\voice_pace.json and updates as he talks, so "slower
than usual" is measured against HIM, not a generic number. Pure logic — no audio I/O
beyond reading the array it's handed; unit-testable with --selftest.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import numpy as np
except ImportError:  # allow import in a numpy-less context; analyze() needs it
    np = None  # type: ignore

STATE_FILE = Path(r"D:\Wren\scratch\voice_pace.json")
FRAME_S = 0.1          # analysis frame
VOICE_PEAK = 0.012     # frame peak >= this = voiced (matches the capture's CONTINUE_PEAK)
PAUSE_GAP_S = 0.45     # a within-utterance silence this long = a "notable pause"
EMA_ALPHA = 0.2        # baseline rolling-average weight on each new sample
MIN_WORDS = 4          # only learn baseline from utterances long enough to be reliable
SLOW_R, FAST_R = 0.80, 1.20  # ratio thresholds vs baseline


def _frames_voiced(audio, sr: int) -> list[bool]:
    n = int(sr * FRAME_S)
    if np is None or n <= 0 or len(audio) < n:
        return []
    a = np.abs(np.asarray(audio, dtype="float32"))
    return [bool(a[i:i + n].max() >= VOICE_PEAK) for i in range(0, len(a) - n + 1, n)]


def _load_baseline() -> float | None:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        ema, n = d.get("ema_wpm"), d.get("n", 0)
        return float(ema) if (ema and n >= 3) else None  # need a few samples first
    except Exception:
        return None


def _update_baseline(wpm: float) -> None:
    try:
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            d = {"ema_wpm": None, "n": 0}
        ema = d.get("ema_wpm")
        d["ema_wpm"] = wpm if not ema else (EMA_ALPHA * wpm + (1 - EMA_ALPHA) * ema)
        d["n"] = int(d.get("n", 0)) + 1
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def _annotate(wpm: float, ratio: float | None, span_s: float, pauses: int) -> str:
    bits: list[str] = []
    if ratio is None:
        bits.append(f"~{round(wpm)} wpm (learning your baseline)")
    elif ratio < SLOW_R:
        bits.append(f"{ratio:.2f}x your usual pace - slower, more deliberate")
    elif ratio > FAST_R:
        bits.append(f"{ratio:.2f}x your usual pace - faster, more animated")
    else:
        bits.append(f"about your usual pace ({round(wpm)} wpm)")
    bits.append(f"{span_s:.1f}s")
    if pauses == 1:
        bits.append("1 notable pause")
    elif pauses > 1:
        bits.append(f"{pauses} notable pauses")
    return "[pace: " + " | ".join(bits) + "]"


def analyze(audio, sr: int, transcript: str) -> tuple[dict, str]:
    """Return (metrics, annotation). annotation is a compact string for the brain to
    read alongside the transcript; '' if there's nothing to say (too short/empty)."""
    words = len(re.findall(r"[A-Za-z0-9']+", transcript or ""))
    voiced = _frames_voiced(audio, sr)
    idx = [i for i, v in enumerate(voiced) if v]
    if words == 0 or not idx:
        return {"words": words}, ""
    span_s = (idx[-1] - idx[0] + 1) * FRAME_S
    # count internal silence runs >= PAUSE_GAP_S between first and last voiced frame
    pauses, run = 0, 0
    for v in voiced[idx[0]:idx[-1] + 1]:
        if not v:
            run += 1
        else:
            if run * FRAME_S >= PAUSE_GAP_S:
                pauses += 1
            run = 0
    wpm = words / (span_s / 60.0) if span_s > 0 else 0.0
    base = _load_baseline()
    ratio = (wpm / base) if base else None
    ann = _annotate(wpm, ratio, span_s, pauses)
    if words >= MIN_WORDS:
        _update_baseline(wpm)
    metrics = {"words": words, "wpm": round(wpm), "span_s": round(span_s, 1),
               "pauses": pauses, "ratio": round(ratio, 2) if ratio else None}
    return metrics, ann


def _selftest() -> int:
    import tempfile
    global STATE_FILE
    STATE_FILE = Path(tempfile.gettempdir()) / "wren_pace_selftest.json"
    STATE_FILE.unlink(missing_ok=True)
    if np is None:
        print("numpy missing — cannot selftest"); return 2
    sr = 16000
    fails = 0

    def check(c, m):
        nonlocal fails
        print(("OK " if c else "XX ") + m)
        fails += not c

    def utt(words, span_s, pause_at=None, pause_len=0.0):
        # build audio: `words` voiced blips spread across span_s; optional silent gap
        n = int(sr * span_s)
        a = np.zeros(n, dtype="float32")
        step = max(1, n // max(words, 1))
        for k in range(words):
            s = k * step
            a[s:s + int(sr * 0.05)] = 0.2  # 50ms voiced blip per "word"
        if pause_at is not None:
            ps = int(sr * pause_at); pe = ps + int(sr * pause_len)
            a[ps:pe] = 0.0
        return a

    # establish baseline at ~120 wpm (10 words over 5s) x3
    for _ in range(3):
        analyze(utt(10, 5.0), sr, " ".join(["word"] * 10))
    base = _load_baseline()
    check(base is not None and 100 <= base <= 140, f"baseline learned ~120 ({base})")

    # a clearly slower utterance: 6 words over 6s = 60 wpm -> ratio ~0.5
    _, ann = analyze(utt(6, 6.0), sr, " ".join(["word"] * 6))
    check("slower" in ann, f"slow utterance flagged slower: {ann}")

    # a faster one: 14 words over 4s = 210 wpm -> ratio ~1.75
    _, ann = analyze(utt(14, 4.0), sr, " ".join(["word"] * 14))
    check("faster" in ann, f"fast utterance flagged faster: {ann}")

    # pause detection: 8 words over 5s with a 0.8s gap in the middle
    m, ann = analyze(utt(8, 5.0, pause_at=2.0, pause_len=0.8), sr, " ".join(["w"] * 8))
    check(m["pauses"] >= 1, f"notable pause detected: {ann}")

    # empty transcript -> no annotation
    _, ann = analyze(utt(5, 3.0), sr, "")
    check(ann == "", "empty transcript -> no annotation")

    STATE_FILE.unlink(missing_ok=True)
    print(f"\n{'PASS' if not fails else 'FAIL'} ({fails} failures)")
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
