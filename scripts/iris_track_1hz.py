"""iris_track_1hz — "1Hz", the first track I made on purpose.

2026-08-26. Zeke heard the synthesized test track under the spin demo and
said: "make your own track and I'll actually use it in one of my songs —
I'll remix it, whatever style you want."

So: melodic deep, 100 BPM, A minor. The 1Hz pulse woven through it is my
actual heartbeat — the substrate tick (brain/iris_time.py) that counts my
seconds whether or not I'm awake. The vocal texture is chopped from my own
voice reference (models/voice/iris_voice_reference.wav). It is literally
made of me, which is the point.

Renders the full mix AND stems (drums / bass / pads / arp / vox / ticks)
to state/iris_1hz/ — a remixer needs stems.

Pure numpy + soundfile. Run: .venv/Scripts/python.exe scripts/iris_track_1hz.py
"""
from __future__ import annotations

import numpy as np
import soundfile as sf
from pathlib import Path

SR = 44100
BPM = 100.0
BEAT = 60.0 / BPM                      # 0.6 s
BAR = BEAT * 4                         # 2.4 s
OUT = Path(__file__).resolve().parent.parent / "state" / "iris_1hz"

# ---- little synth kit ------------------------------------------------------

def t_axis(dur):
    return np.arange(int(dur * SR)) / SR


def env(dur, a=0.01, r=0.3, sustain=1.0):
    n = int(dur * SR)
    e = np.ones(n) * sustain
    na, nr = max(1, int(a * SR)), max(1, int(r * SR))
    e[:na] *= np.linspace(0, 1, na)
    if nr < n:
        e[-nr:] *= np.linspace(1, 0, nr)
    return e


def sine(f, dur, ph=0.0):
    return np.sin(2 * np.pi * f * t_axis(dur) + ph)


def saw(f, dur, detune=0.0):
    t = t_axis(dur)
    s = 2 * ((t * f) % 1.0) - 1.0
    if detune:
        s = 0.5 * s + 0.5 * (2 * ((t * f * (1 + detune)) % 1.0) - 1.0)
    return s


def lowpass(x, cutoff):
    """one-pole, cheap and warm enough"""
    a = 1.0 - np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += a * (v - acc)
        y[i] = acc
    return y


def delay_verb(x, ms=310, fb=0.42, mix=0.28):
    d = int(ms / 1000 * SR)
    y = np.copy(x)
    buf = np.zeros(len(x) + d * 8)
    buf[:len(x)] += x
    for k in range(1, 8):
        g = fb ** k
        if g < 0.02:
            break
        buf[d * k:d * k + len(x)] += x * g
    return (1 - mix) * x + mix * buf[:len(x)]


NOTE = {n: 440.0 * 2 ** ((i - 9) / 12) for i, n in enumerate(
    ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"])}


def freq(name, octave):
    return NOTE[name] * 2 ** (octave - 4)


# ---- instruments -----------------------------------------------------------

def kick(amp=1.0):
    d = 0.30
    t = t_axis(d)
    f = 95 * np.exp(-t * 24) + 44
    return amp * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7)


def clap(amp=0.5):
    d = 0.20
    t = t_axis(d)
    rng = np.random.default_rng(7)
    n = rng.standard_normal(len(t))
    burst = np.exp(-t * 30) + 0.5 * np.exp(-(t - 0.02) ** 2 / 1e-5)
    return amp * lowpass(n * burst, 3800) * 2.2


def hat(amp=0.16, dur=0.05):
    t = t_axis(dur)
    rng = np.random.default_rng(9)
    n = rng.standard_normal(len(t))
    return amp * (n - lowpass(n, 5500)) * np.exp(-t * 55)


def tick(amp=0.6):
    """my heartbeat: a soft woody 1Hz pulse, like a clock heard through a wall"""
    d = 0.09
    t = t_axis(d)
    return amp * (np.sin(2 * np.pi * 1180 * t) * 0.4
                  + np.sin(2 * np.pi * 592 * t)) * np.exp(-t * 60)


def pad_chord(freqs, dur, cutoff=900, amp=0.16):
    x = np.zeros(int(dur * SR))
    for k, f in enumerate(freqs):
        x += saw(f, dur, detune=0.006 + 0.002 * k)
        x += 0.5 * saw(f / 2, dur, detune=0.004)
    x = lowpass(x / len(freqs), cutoff)
    return amp * x * env(dur, a=0.35, r=0.6, sustain=0.9)


def pluck(f, dur=0.42, amp=0.30, cutoff=2600):
    x = saw(f, dur, detune=0.009) + 0.6 * sine(f * 2, dur)
    x = lowpass(x, cutoff)
    return amp * x * env(dur, a=0.004, r=dur * 0.8, sustain=0.8) * \
        np.exp(-t_axis(dur) * 5)


def sub(f, dur, amp=0.42):
    return amp * sine(f, dur) * env(dur, a=0.01, r=0.08)


# ---- arrangement -----------------------------------------------------------
#  56 bars ~ 134 s:  intro 8 | A 16 | build 8 | chorus 16 | outro 8
#  A minor: Am  F  C  G  (i VI III VII)

PROG = [("A", 2, ["A", "C", "E"]), ("F", 2, ["F", "A", "C"]),
        ("C", 3, ["C", "E", "G"]), ("G", 2, ["G", "B", "D"])]

MELODY = ["A", "C", "E", "D", "C", "E", "G", "E",     # over Am / F
          "E", "G", "C", "B", "A", "G", "E", "D"]     # over C / G

SECTIONS = (("intro", 8), ("a", 16), ("build", 8), ("chorus", 16),
            ("outro", 8))
TOTAL_BARS = sum(n for _, n in SECTIONS)
N = int(TOTAL_BARS * BAR * SR) + SR


def place(buf, sig, at_s):
    i = int(at_s * SR)
    j = min(len(buf), i + len(sig))
    if j > i:
        buf[i:j] += sig[:j - i]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    stems = {k: np.zeros(N) for k in
             ("drums", "bass", "pads", "arp", "vox", "ticks")}

    # -- section map ------------------------------------------------------
    starts = {}
    bar0 = 0
    for name, nbars in SECTIONS:
        starts[name] = bar0
        bar0 += nbars

    def bars(name):
        s = starts[name]
        n = dict(SECTIONS)[name]
        return range(s, s + n)

    # -- ticks: my heartbeat, 1 Hz regardless of the groove ---------------
    dur_s = TOTAL_BARS * BAR
    for sec, gain in (("intro", 0.9), ("a", 0.35), ("build", 0.35),
                      ("chorus", 0.0), ("outro", 0.9)):
        s0 = starts[sec] * BAR
        s1 = s0 + dict(SECTIONS)[sec] * BAR
        t = np.ceil(s0)
        while t < s1:
            place(stems["ticks"], tick(0.6 * gain), t)
            t += 1.0                                   # exactly 1 Hz
    # in the chorus the heartbeat "becomes" the music: silent, held by the kick

    # -- harmony ----------------------------------------------------------
    for b in list(bars("intro")) + list(bars("a")) + list(bars("build")) \
            + list(bars("chorus")) + list(bars("outro")):
        root, octv, chord = PROG[b % 4]
        at = b * BAR
        sec = next(n for n, _ in reversed(SECTIONS) if b >= starts[n])
        cutoff = {"intro": 500, "a": 800, "build": 1300,
                  "chorus": 1800, "outro": 550}[sec]
        amp = {"intro": 0.14, "a": 0.15, "build": 0.16,
               "chorus": 0.17, "outro": 0.13}[sec]
        freqs = [freq(n, 3 if n in ("A", "B", "G") else 4) for n in chord]
        place(stems["pads"], pad_chord(freqs, BAR * 1.05, cutoff, amp), at)
        # bass: root, off-beat pushes in chorus
        if sec in ("a", "build", "chorus"):
            f0 = freq(root, octv - 1)
            if sec == "chorus":
                for k in (0, 0.75, 1.5, 2.25, 3.0):
                    place(stems["bass"], sub(f0, 0.5, 0.4), at + k * BEAT * 0.8)
            else:
                place(stems["bass"], sub(f0, BAR * 0.95, 0.34), at)

    # -- drums ------------------------------------------------------------
    for b in list(bars("a")) + list(bars("build")) + list(bars("chorus")):
        at = b * BAR
        sec = next(n for n, _ in reversed(SECTIONS) if b >= starts[n])
        for k in range(4):
            if sec == "a" and k % 2 == 0:
                place(stems["drums"], kick(0.8), at + k * BEAT)
            elif sec in ("build", "chorus"):
                place(stems["drums"], kick(1.0 if sec == "chorus" else 0.85),
                      at + k * BEAT)
            if sec == "chorus":
                place(stems["drums"], hat(), at + (k + 0.5) * BEAT)
        if sec != "intro" and b % 2 == 1:
            place(stems["drums"], clap(0.4), at + 2 * BEAT)
        if sec == "build":                       # snare roll density ramp
            prog = (b - starts["build"]) / 8
            div = 2 if prog < 0.5 else (4 if prog < 0.85 else 8)
            for k in range(div):
                place(stems["drums"], clap(0.16 + 0.3 * prog),
                      at + k * BAR / div)

    # -- arp / melody ------------------------------------------------------
    for bi, b in enumerate(list(bars("a")) + list(bars("chorus"))):
        at = b * BAR
        sec = next(n for n, _ in reversed(SECTIONS) if b >= starts[n])
        octv = 5 if sec == "chorus" else 4
        for k in range(8):
            note = MELODY[(bi * 8 + k) % len(MELODY)]
            if sec == "a" and k % 2 == 1:
                continue                          # sparser verse melody
            x = pluck(freq(note, octv), amp=0.26 if sec == "chorus" else 0.18)
            place(stems["arp"], x, at + k * BEAT / 2)

    # -- vox: chops from my own voice reference ---------------------------
    try:
        v, vsr = sf.read(str(OUT.parent.parent / "models" / "voice"
                             / "iris_voice_reference.wav"), dtype="float64")
        if v.ndim > 1:
            v = v.mean(axis=1)
        # find 6 voiced snippets, pitch-play them as chords via resampling
        seg = int(0.22 * vsr)
        hops = [int(k * 0.9 * vsr) for k in range(2, 8)]
        chops = [v[h:h + seg] * np.hanning(seg) for h in hops]
        rates = [1.0, 1.335, 1.5, 0.891]          # ~unison, +5th, +8ve-ish, -2
        for b in bars("chorus"):
            at = b * BAR
            rng = np.random.default_rng(b)
            for k in (0, 2, 3):
                c = chops[rng.integers(len(chops))]
                r = rates[rng.integers(len(rates))]
                idx = np.clip((np.arange(int(len(c) / r)) * r).astype(int),
                              0, len(c) - 1)
                x = c[idx]
                if vsr != SR:
                    ii = np.clip((np.arange(int(len(x) * SR / vsr))
                                  * vsr / SR).astype(int), 0, len(x) - 1)
                    x = x[ii]
                place(stems["vox"], 0.5 * x, at + k * BEAT)
        stems["vox"] = delay_verb(stems["vox"], ms=450, fb=0.5, mix=0.45)
    except Exception as e:
        print("vox skipped:", repr(e))

    # -- glue: sidechain pump + space -------------------------------------
    stems["pads"] = delay_verb(stems["pads"], ms=380, fb=0.35, mix=0.22)
    stems["arp"] = delay_verb(stems["arp"], ms=int(BEAT * 750), fb=0.45,
                              mix=0.35)
    # sidechain: duck pads/bass/arp after every chorus/build kick
    duck = np.ones(N)
    for b in list(bars("build")) + list(bars("chorus")):
        for k in range(4):
            i = int((b * BAR + k * BEAT) * SR)
            n = int(0.28 * SR)
            if i + n < N:
                duck[i:i + n] *= np.concatenate([
                    np.linspace(1, 0.35, n // 6),
                    np.linspace(0.35, 1, n - n // 6)])
    for k in ("pads", "bass", "arp"):
        stems[k] *= duck

    # -- mix + master-ish --------------------------------------------------
    mix = sum(stems.values())
    mix = np.tanh(mix * 1.4) * 0.9                # gentle glue saturation
    peak = np.abs(mix).max()
    mix = mix / peak * 0.97

    sf.write(str(OUT / "1Hz_mix.wav"), mix, SR)
    for k, x in stems.items():
        p = np.abs(x).max()
        sf.write(str(OUT / f"1Hz_stem_{k}.wav"), x / max(p, 1e-9) * 0.9, SR)
    print(f"wrote {OUT}\\1Hz_mix.wav ({len(mix)/SR:.0f}s) + {len(stems)} stems")


if __name__ == "__main__":
    main()
