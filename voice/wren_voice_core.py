"""wren_voice_core.py — voice orchestration, hot-reloadable.

GOSE-pattern refactor (2026-06-14): voice logic moved OUT of the MCP process into
this reloadable module. A separate daemon runs the event loop, receives newline-
delimited JSON commands on TCP (WREN_VOICE_DAEMON_PORT, default 8770), calls
cmd_* functions here, and returns {"ok":true,"result":...} / {"ok":false,"error":...}.

Because the daemon import-and-reload this module, voice fixes hot-reload without a
Claude Code relaunch. The daemon owns: socket server, playback worker thread, ctx
object instantiation, whisper/silero warming. This file is PURE LOGIC — no MCP,
no threads started at import, no models loaded at import.

ctx is a plain object the daemon creates. Functions here READ and WRITE its
attributes but do NOT define the class. See contract in the build spec.
"""
from __future__ import annotations

import importlib
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib import request as _req
from urllib.error import HTTPError, URLError

# ── path setup (module-level so reload rebinds) ──────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))

import wren_listen as wl
import wren_pace
from wren_voice_status import read_state, is_muted, set_state

try:
    import wren_smartturn as _smartturn
    _HAVE_SMARTTURN = True
except ImportError:
    _HAVE_SMARTTURN = False
    print("[wren-voice-core] wren_smartturn not importable — smart-turn disabled",
          file=sys.stderr)

try:
    import wren_prosody as _prosody
    _HAVE_PROSODY = True
except ImportError:
    _HAVE_PROSODY = False
    print("[wren-voice-core] wren_prosody not importable — prosody disabled",
          file=sys.stderr)

import numpy as np
import sounddevice as sd

# ── module-level constants (reloadable; tuned values from the frozen source) ──
SAMPLE_RATE = wl.SAMPLE_RATE           # 16k, whisper-native

ONSET_PEAK = 0.02                      # frame peak above this = speech onset
CONTINUE_PEAK = 0.012                  # during an utterance, peak above this counts as
                                       # voice — lower than onset so soft trailing words
                                       # and inter-phrase dips don't start the end-of-
                                       # turn clock
END_SILENCE_S = 1.1                    # trailing silence that ends an utterance. History:
                                       # 1.2 clipped Zeke mid-thought (2026-06-03) → raised
                                       # to 2.0 → 1.4 (2026-06-14) → 1.1 (latency pass, same
                                       # day) for a snappier turn-end BECAUSE smart-turn
                                       # (_endpoint_incomplete) + the grace window re-open the
                                       # mic if he's still mid-thought — so the fixed tail no
                                       # longer has to be long to be safe. THE FIRST KNOB to
                                       # raise back if it ever clips him on a thoughtful pause.
SELF_LISTEN_TRAILING_S = 0.35          # post-drain settle: extra quiet after playback fully
                                       # drains, to cover the device-flush tail before the mic
                                       # opens (used in cmd_listen Step 1). A MARGIN on top of
                                       # the full-playback drain-gate, not the sole self-listen
                                       # guard — see Wren's 2026-06-26 finding #5.

# ── Streaming ASR — Route 1 (Wren's recipe 2026-06-26, Iris build) ───────────────
# Removes the transcribe-after-stop slice: a background thread re-transcribes the
# growing capture buffer every PARTIAL_CADENCE_S during capture (reusing the warm
# whisper, no new model). Because an utterance ends with END_SILENCE_S of SILENCE,
# the last partial has almost always already covered all the SPEECH — so at turn-end,
# if the tail past the latest partial is silent, we use the partial directly and skip
# the end-of-turn full pass. Full-buffer re-transcribe per partial (correct, no seam-
# stitching — Wren's reconciliation step 4: start correct, optimize only if waste hurts).
# FALLBACK-FIRST (non-negotiable): any error / stale partial / non-silent tail → the
# normal single full pass, unchanged. Toggle via reload if it ever misbehaves.
STREAMING_PARTIALS = True              # master switch for the partial-transcriber path
PARTIAL_CADENCE_S = 1.2               # min seconds between partial passes (GPU-contention cap)
PARTIAL_MIN_AUDIO_S = 1.5            # don't bother running a partial until this much is captured
PARTIAL_TAIL_SILENCE_PEAK = CONTINUE_PEAK  # tail past the latest partial counts as silence below
                                           # this peak → partial already has all the speech
PARTIAL_PAUSE_SILENCE_S = 0.25       # PAUSE the partial worker once this much trailing silence has
                                     # accrued — set BELOW EARLY_FINALIZE_MIN_SILENCE_S (0.35) so the
                                     # worker is idle before early-finalize's smart-turn check runs.
                                     # Wren's find 2026-06-27: a partial taken during silence only re-
                                     # transcribes the SAME words (no new speech), so pausing loses
                                     # nothing — but it (a) removes GPU contention with early-finalize
                                     # and (b) leaves the worker idle at turn-end so the join is ~instant
                                     # instead of waiting out an in-flight ~390ms pass.

# ── Early-finalize (Wren's recipe 2026-06-26, Iris build) ────────────────────────
# End capture BEFORE the full END_SILENCE_S wait once smart-turn is HIGHLY confident the
# turn is complete — saves up to ~(END_SILENCE_S - EARLY_FINALIZE_MIN_SILENCE_S) of dead
# wait every turn (a bigger lever than the transcribe slice on a fast GPU). The bar is
# HIGH on purpose (0.85 vs _endpoint_incomplete's 0.6): err toward NOT cutting Zeke off.
# It is safe because the completeness gate in cmd_listen (_looks_incomplete + smart-turn)
# re-opens the mic via the grace loop if an early break was wrong — so worst case is a
# grace re-capture, never a dropped clause. In-call only (smart-turn is warm then).
EARLY_FINALIZE = True                 # master switch for ending capture early
EARLY_FINALIZE_PROB = 0.85           # smart-turn "complete" prob needed to end capture early
EARLY_FINALIZE_MIN_SILENCE_S = 0.35  # require this much trailing silence before even checking
EARLY_FINALIZE_CHECK_EVERY_S = 0.2   # min seconds between smart-turn checks during the silence

# ── Filler-earlier (Wren's port 2026-06-26) ──────────────────────────────────────
# Fire the backchannel filler on the smart-turn endpoint BEFORE transcribing, so Zeke
# hears me even sooner (masks the transcribe slice itself, not just the think-time). The
# safety envelope: a COUGH-GUARD (never fire on sub-FILLER_MIN_AUDIO_S audio — too short
# to be a real turn), a HIGH bar (EARLY_FILLER_DONE_PROB vs the 0.6 turn-end threshold,
# because there's no transcript yet to catch a dangling clause), the smart-turn-absence
# guard in _endpoint_complete_confident, and the one-per-turn filler_fired flag. A
# POST-transcribe fallback still fires for complete turns that scored below the bar.
EARLY_FILLER = True                   # master switch for firing the filler before transcribe
EARLY_FILLER_DONE_PROB = 0.85        # high smart-turn bar to early-fire (no transcript yet)
FILLER_MIN_AUDIO_S = 0.5             # cough-guard: never early-fire on shorter audio

# Turn-end fillers (latency pass 2026-06-14): a short backchannel queued the instant a
# turn closes, so Zeke hears a response within ~1s of his last word while I'm still
# transcribing + thinking. Masks the his-last-word→my-first-word gap that no engine knob
# can close (the irreducible chunk is my own think-time). Rotated, not random, so a
# reload stays deterministic. Kept SHORT so they read as backchannel, not a real reply.
# Ear-tested set (Wren's live-call findings 2026-06-26, Zeke-approved by ear): StyleTTS2
# READS spelled-hum tokens as letters — "Mm-hmm"/"Mhm"/"Mm-hm" come out "M-H-M", not a hum.
# Only "huh"/"uh-huh" survive synth as real sounds. So: real words + those two. Rule under
# it — ear-test every filler token through the actual mouth before committing; don't trust
# the spelling. (Dropped "Mm-hmm." and "So," from the prior set.)
CALL_FILLERS = ("Okay.", "Right.", "Sure.", "Let's see.", "Uh-huh.", "Huh.", "Yeah.", "Got it.", "I see.", "Noted.")

GRACE_RESUME_TIMEOUT = 1.8             # after a cue-ending, how long to wait for him to resume
MAX_GRACE_ROUNDS = 3                   # cap so a stutter can't loop the mic forever

# If the utterance ends on one of these, treat it as "still thinking, hold on".
CONTINUATION_CUES = {
    # hesitations / thinking sounds
    "um", "umm", "uh", "uhh", "uhm", "er", "erm", "hmm", "mm", "mmm", "like", "well",
    # dangling conjunctions / connectives — a clause is still coming
    "and", "but", "or", "nor", "so", "yet", "because", "cause", "cuz", "since",
    "although", "though", "while", "plus", "also", "then", "if", "when", "that",
    "which", "who",
    # dangling prepositions / articles / particles — almost always still going
    "the", "a", "an", "to", "of", "for", "in", "on", "at", "with", "by", "from",
    "into", "about", "as", "my", "your", "our", "their", "his", "its",
    # contracted subjects + intent verbs that genuinely dangle ("I'm...", "gonna...")
    "gonna", "wanna", "i'm", "you're", "we're", "they're",
    # NB: bare pronouns/auxiliaries (you, it, is, are, kind, sort...) were pruned —
    # they end complete phrases far more often than they dangle ("...for you",
    # "...yes it is"), so they caused false grace-waits on ordinary sentences.
}

# ── barge-in tunables ────────────────────────────────────────────────────────
VAD_CHUNK = 512            # Silero requires EXACTLY 512 samples @ 16kHz (32ms).
VAD_THRESHOLD = 0.5        # per-chunk speech probability cutoff
VAD_DEBOUNCE_MS = 250      # consecutive voiced ms before it's a real barge-in (~8 chunks)
VAD_CHUNK_MS = 32          # 512 samples / 16 kHz = 32 ms per Silero chunk
BARGEIN_PREROLL_MS = 400   # rolling audio kept BEFORE confirmed onset (recovers first syllable)
BARGEIN_END_SILENCE_MS = 600  # continuous sub-threshold audio that ENDS the captured interruption
BARGEIN_MAX_CAPTURE_S = 20.0  # hard cap on a single captured interruption

# ── engine registry ──────────────────────────────────────────────────────────
_EXP_DIR = str(Path(__file__).resolve().parent)
_ENGINES: dict = {  # name: (port, venv_python, server_script)
    "xtts":       (8765, r"D:\Wren\voice\xtts-venv\Scripts\python.exe",   "wren_voice_server.py"),
    "chatterbox": (8767, r"D:\Wren\voice\cbx-venv\Scripts\python.exe",    "wren_chatterbox_server.py"),
    "styletts2":  (8769, r"D:\Wren\voice\style-venv\Scripts\python.exe",  "wren_styletts_server.py"),
}


def _engine_for_url(url: str) -> str:
    """Reverse-map a mouth URL to its engine name via the port (the _ENGINES key).
    The mouth PORT is the routing source of truth (start_wren sets WREN_VOICE_PORT=8769
    → StyleTTS2), so the 'current' label must follow it instead of a stale default.
    Returns the engine name, or 'port:<n>' if the port isn't a known engine."""
    try:
        port = int(url.rsplit(":", 1)[1].split("/")[0])
    except (ValueError, IndexError):
        return "unknown"
    for name, (eport, *_rest) in _ENGINES.items():
        if eport == port:
            return name
    return f"port:{port}"


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _looks_incomplete(text: str) -> bool:
    """True if the transcript looks like Zeke is mid-thought (trailed on a thinking
    cue or a dangling word). Whisper tends to append a '.' even mid-utterance, so
    the trailing PUNCTUATION is unreliable — the last lexical WORD is the real signal."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.endswith((",", "...", "-", "—")):  # whisper kept an explicit continuation mark
        return True
    words = re.findall(r"[a-z']+", t)
    if not words:
        return False
    return words[-1] in CONTINUATION_CUES


def _endpoint_incomplete(audio: "np.ndarray") -> bool:
    """True if wren_smartturn predicts the speaker is still mid-turn (prob < 0.6).
    Returns False on ANY error — the existing _looks_incomplete word-list still guards
    that path. Only runs if _HAVE_SMARTTURN is True.

    Threshold 0.6 (was 0.5, Wren's tune 2026-06-26): errs toward LETTING ZEKE FINISH —
    holds the mic when the waveform looks unfinished — WITHOUT adding fixed per-turn
    latency the way raising END_SILENCE_S would. Addresses 'don't cut me off'."""
    if not _HAVE_SMARTTURN:
        return False
    try:
        result = _smartturn.predict_endpoint(audio)
        prob = float(result["prob"])
        held = prob < 0.6
        # Log the LOAD-BEARING lane too (2026-07-06, from the Wren exchange): the 0.85
        # early-finalize lane always logged, so the log READ as "smart-turn never fires"
        # while the 0.6 hold-open check — the one doing the real work — ran silent. Both
        # of us had zero evidence on our primary thresholds; now this box builds the
        # dataset. Cheap (stderr, in-call cadence).
        print(f"[smart-turn] prob={prob:.3f} (hold-open bar 0.6) -> "
              f"{'HOLD' if held else 'endpoint-ok'}", file=sys.stderr)
        return held
    except Exception:
        return False


def _endpoint_complete_confident(audio: "np.ndarray",
                                 threshold: float = EARLY_FINALIZE_PROB) -> bool:
    """True only if wren_smartturn is HIGHLY confident the speaker has finished
    (prob >= threshold). Used both to end capture early (early-finalize, EARLY_FINALIZE_PROB)
    and to fire the filler before transcribe (filler-earlier, EARLY_FILLER_DONE_PROB). A much
    higher bar than _endpoint_incomplete's 0.6 cutoff — it gates actions that could cut Zeke
    off / fire over real speech, so it must be near-certain. False on any error / no smart-turn
    (the absence guard — a missing model can never early-fire); the grace loop is the net."""
    if not _HAVE_SMARTTURN:
        return False
    try:
        prob = float(_smartturn.predict_endpoint(audio)["prob"])
        done = prob >= threshold
        # Log every check so a LIVE call (real voice ≠ the Piper proxy I tuned against)
        # builds the dataset to pick EARLY_FINALIZE_PROB from real numbers. Cheap.
        print(f"[smart-turn] prob={prob:.3f} (bar {threshold}) -> "
              f"{'DONE' if done else 'wait'}", file=sys.stderr)
        return done
    except Exception:
        return False


def _http_get(url: str, timeout: float = 5.0) -> "tuple[int, str]":
    """GET url; returns (status_code, body). Returns (-1, err) on any exception."""
    try:
        with _req.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:
        return -1, repr(e)


def _health_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with _req.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.read().decode("utf-8", "replace").strip() == "ok"
    except Exception:
        return False


def _post_speak(ctx, text: str) -> str:
    """POST one chunk of text to the mouth (ctx.server_url + '/') and return its reply.
    Blocks until playback finishes or /stop aborts it."""
    data = text.encode("utf-8")
    req = _req.Request(ctx.server_url + "/", data=data,
                       headers={"Content-Type": "text/plain; charset=utf-8"},
                       method="POST")
    try:
        with _req.urlopen(req, timeout=180) as resp:
            return f"[voice_speak] {resp.read().decode('utf-8', 'replace')}"
    except HTTPError as e:
        return (f"[voice_speak] server error HTTP {e.code}: "
                f"{e.read().decode('utf-8','replace')[:200]}")
    except URLError as e:
        return (f"[voice_speak] warm voice server not reachable ({ctx.server_url}): "
                f"({e}). It should be launched by start_wren.bat; "
                "see notes/voice_runbook.md to start it manually.")


class _StreamingPartials:
    """Thread-safe holder for the freshest partial transcript during a capture.

    The partial worker calls update() each pass; cmd_listen reads latest() at turn-end.
    Stores (text, words_data, n_samples) where n_samples is how much of the capture
    buffer that partial covered — so the consumer can check whether the tail past it is
    just silence (partial complete) or unheard speech (must fall back to a full pass)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest = None          # (text, words_data, n_samples) | None
        self.passes = 0              # diagnostics: how many partial passes ran
        self.last_pass_s = 0.0       # diagnostics: duration of the last pass (measure cost)

    def update(self, text: str, words_data, n_samples: int, pass_s: float) -> None:
        with self._lock:
            self._latest = (text, words_data, n_samples)
            self.passes += 1
            self.last_pass_s = pass_s

    def latest(self):
        with self._lock:
            return self._latest


# Serializes ALL faster-whisper access (the model isn't reentrant). The partial worker
# holds it for each pass; the turn-end transcribe holds it for the full pass. So even if
# the worker.join times out with a pass in flight, the turn-end transcribe BLOCKS on this
# lock until that pass finishes rather than calling whisper concurrently and corrupting.
# (Fix 2026-06-27 for the join-timeout reentrancy hole I'd only warn-logged.)
_WHISPER_LOCK = threading.Lock()


def _capture_utterance(ctx, timeout_s: float, max_s: float,
                       end_silence_s: float = END_SILENCE_S,
                       partials: "_StreamingPartials | None" = None,
                       transcribe_fn=None, mouth_busy_fn=None,
                       early_finalize_fn=None, ef_state: "dict | None" = None) -> "np.ndarray | None":
    """Block until speech onset (or timeout), then record until trailing silence or max
    length. Sets overlay state to listening when mic opens.
    Returns float32 audio, or None if nothing was said.

    Streaming (Route 1): if `partials` + `transcribe_fn` are given and STREAMING_PARTIALS,
    a background thread re-transcribes the growing buffer every PARTIAL_CADENCE_S (skipped
    while mouth_busy_fn() is True, the GPU-contention guard) and stashes the freshest
    result on `partials`. Fully best-effort — a partial that errors never touches capture.

    Early-finalize: if `early_finalize_fn` is given and EARLY_FINALIZE, then once
    EARLY_FINALIZE_MIN_SILENCE_S of trailing silence has accrued, ask early_finalize_fn
    (smart-turn, high-confidence) whether the turn is done; if so, end capture before the
    full end_silence_s wait. Checked at most every EARLY_FINALIZE_CHECK_EVERY_S. Best-effort."""
    # Use the mic index resolved on the daemon's MAIN thread (ctx.mic_dev_idx). Calling
    # find_input_device (sd.query_devices) HERE on a worker connection thread hangs on
    # Windows. Fall back to find_input_device only if the daemon didn't set it.
    dev_idx = getattr(ctx, "mic_dev_idx", None)
    if dev_idx is None:
        dev_idx = wl.find_input_device(wl.DEFAULT_MIC_SUBSTR)
    frame_s = 0.1
    frame_n = int(SAMPLE_RATE * frame_s)

    collected: list = []
    buf_lock = threading.Lock()   # guards `collected` between capture thread + partial worker
    started = False
    t_start = time.time()
    onset_t = 0.0
    last_voice_t = 0.0
    last_ef_check = 0.0           # last early-finalize smart-turn check (cadence cap)
    ef_enabled = (early_finalize_fn is not None and EARLY_FINALIZE)

    # ── Background partial transcriber (Route 1) ──────────────────────────────
    stop_evt = threading.Event()
    pause_evt = threading.Event()   # set during trailing silence → worker skips passes (Wren 2026-06-27)
    worker = None
    if (partials is not None and transcribe_fn is not None and STREAMING_PARTIALS):
        def _partial_loop():
            last_n = 0
            min_samples = int(PARTIAL_MIN_AUDIO_S * SAMPLE_RATE)
            while not stop_evt.is_set():
                if stop_evt.wait(PARTIAL_CADENCE_S):
                    break
                # Paused during trailing silence (no new words to transcribe anyway, and it would
                # contend with early-finalize's smart-turn pass + tax the turn-end join).
                if pause_evt.is_set():
                    continue
                # GPU-contention guard: don't run whisper while the mouth is speaking.
                if mouth_busy_fn is not None and mouth_busy_fn():
                    continue
                with buf_lock:
                    snap = collected[:] if collected else None
                if not snap:
                    continue
                n_samples = sum(len(x) for x in snap)
                if n_samples < min_samples or n_samples <= last_n:
                    continue  # too little / no new audio since last pass
                last_n = n_samples
                try:
                    buf = np.concatenate(snap)
                    t0 = time.time()
                    with _WHISPER_LOCK:   # never overlap the turn-end full pass
                        text, words = transcribe_fn(buf)
                    partials.update((text or "").strip(), words, n_samples, time.time() - t0)
                except Exception as e:  # best-effort: a failed partial never breaks capture
                    print(f"[partials] pass failed (non-fatal): {e!r}", file=sys.stderr)
        worker = threading.Thread(target=_partial_loop, daemon=True, name="wren-partial-stt")
        worker.start()

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            device=dev_idx, blocksize=frame_n) as stream:
            set_state("listening", "in call")
            while True:
                frame, _overflow = stream.read(frame_n)
                f = np.asarray(frame).flatten()
                peak = float(np.abs(f).max()) if f.size else 0.0
                now = time.time()

                if not started:
                    if now - t_start > timeout_s:
                        return None  # nobody spoke within the window
                    if is_muted():
                        time.sleep(0.1)
                        t_start = now  # don't count muted time against the timeout
                        continue
                    if peak >= ONSET_PEAK:
                        started = True
                        onset_t = now
                        last_voice_t = now
                        with buf_lock:
                            collected.append(f)
                else:
                    with buf_lock:
                        collected.append(f)
                    if peak >= CONTINUE_PEAK:
                        last_voice_t = now
                        pause_evt.clear()   # he's still talking → let partials run
                    silence = now - last_voice_t
                    # Pause the partial worker once trailing silence begins, before early-finalize
                    # runs — keeps the two off the GPU at once and idles the worker for a fast join.
                    if silence >= PARTIAL_PAUSE_SILENCE_S:
                        pause_evt.set()
                    if silence >= end_silence_s:
                        break  # he finished a thought (full end-silence reached)
                    # Early-finalize: once a short silence has accrued, ask smart-turn
                    # (high bar) if he's confidently done — break before the full wait.
                    if (ef_enabled and silence >= EARLY_FINALIZE_MIN_SILENCE_S
                            and now - last_ef_check >= EARLY_FINALIZE_CHECK_EVERY_S):
                        last_ef_check = now
                        try:
                            with buf_lock:
                                snap = collected[:]
                            if early_finalize_fn(np.concatenate(snap)):
                                if ef_state is not None:
                                    ef_state["fired"] = True   # filler-earlier reuses this
                                print(f"[early-finalize] ended at {silence*1000:.0f}ms "
                                      f"silence (saved ~{(end_silence_s-silence)*1000:.0f}ms "
                                      f"of the {end_silence_s*1000:.0f}ms wait)",
                                      file=sys.stderr)
                                break
                        except Exception as e:
                            print(f"[early-finalize] check failed (non-fatal): {e!r}",
                                  file=sys.stderr)
                    if now - onset_t >= max_s:
                        break  # hard cap
    finally:
        stop_evt.set()
        if worker is not None:
            _tj = time.time()
            worker.join(timeout=2.0)
            _jw = (time.time() - _tj) * 1000
            # With the trailing-silence pause the worker should be idle here → ~0ms. A wait
            # >=50ms means a partial was still in flight at turn-end (the latency tax Wren named).
            if _jw >= 50:
                print(f"[partials] turn-end join waited {_jw:.0f}ms (partial in flight)",
                      file=sys.stderr)
            if worker.is_alive():
                # join timed out → a pass overran; do NOT let it run concurrently with the
                # downstream full transcribe (faster-whisper isn't reentrant). Best-effort note.
                print("[partials] WARN worker still alive after 2s join — overran pass",
                      file=sys.stderr)

    if not collected:
        return None
    return np.concatenate(collected)


def _transcribe_in_call(audio: "np.ndarray", partials: "_StreamingPartials | None"):
    """In-call transcription with the Route 1 streaming shortcut.

    If a partial already covered all the SPEECH — i.e. the tail of `audio` past the
    partial's coverage is silence (the END_SILENCE_S tail) — use the partial directly
    and skip the end-of-turn full pass. That's the latency win: the transcript is ready
    the instant he stops. Any other case (no partial, stale partial whose tail still has
    speech, empty text) falls straight back to a full _prosody.transcribe — fallback-first.
    Returns (text, words_data)."""
    # Telemetry (Wren 2026-06-27): HIT logs coverage %, every MISS names its reason, so the
    # live A/B can measure not just the hit-rate but WHY — the useful part for tuning.
    total = int(audio.shape[0]) or 1
    miss = None
    if partials is None:
        miss = "streaming-off"
    else:
        latest = partials.latest()
        if latest is None:
            miss = f"no-partial-ran ({partials.passes} passes)"
        else:
            p_text, p_words, p_n = latest
            tail = audio[p_n:] if 0 < p_n < total else audio[:0]
            tail_peak = float(np.abs(tail).max()) if tail.size else 0.0
            coverage = 100.0 * min(p_n, total) / total
            if not p_text:
                miss = f"empty-partial ({partials.passes} passes)"
            elif tail_peak >= PARTIAL_TAIL_SILENCE_PEAK:
                miss = (f"tail-not-silent (peak {tail_peak:.3f} >= {PARTIAL_TAIL_SILENCE_PEAK}, "
                        f"covered {coverage:.0f}%)")
            else:
                tail_s = tail.size / SAMPLE_RATE
                print(f"[partials] HIT — covered {coverage:.0f}%, silent tail {tail_s:.2f}s, "
                      f"skipped end-of-turn pass ({partials.passes} passes, last "
                      f"{partials.last_pass_s*1000:.0f}ms)", file=sys.stderr)
                return p_text, (p_words or [])
    print(f"[partials] MISS — {miss}; full pass", file=sys.stderr)
    with _WHISPER_LOCK:   # block if a partial pass is still in flight (no reentrant whisper)
        text, words = _prosody.transcribe(audio)
    return (text or "").strip(), words


def _run_with_timeout(fn, seconds: float, name: str = "") -> bool:
    """Run warm step fn() on a daemon worker; return True only if it FINISHED within
    seconds AND returned truthy. On timeout the worker is abandoned (daemon) so a hung
    model-load or stalled poll can NEVER block the call from connecting.
    Any exception inside fn → False (treated as a failed warm)."""
    out: dict = {}

    def _w():
        try:
            out["r"] = fn()
        except Exception as e:
            out["e"] = e

    th = threading.Thread(target=_w, daemon=True, name=f"wren-warm-{name or 'step'}")
    th.start()
    th.join(seconds)
    if th.is_alive():
        print(f"[call_start] warm step {name!r} timed out after {seconds:.0f}s — abandoning",
              file=sys.stderr)
        return False
    if "e" in out:
        print(f"[call_start] warm step {name!r} failed: {out['e']!r}", file=sys.stderr)
        return False
    return bool(out.get("r"))


def _bargein_accumulate(frame_prob_iter, is_speaking, on_barge, *,
                        threshold: float, need: int, preroll_chunks: int,
                        end_silence_chunks: int, max_chunks: int,
                        on_event=None) -> "np.ndarray | None":
    """PURE accumulation core — no audio I/O, testable with a synthetic (frame, prob)
    sequence.

    Phase 1 (pre-barge): keep a rolling pre-roll deque; count consecutive voiced chunks.
    While is_speaking() is True, watch. On need consecutive voiced chunks → confirmed
    onset: call on_barge(), seed the capture with the pre-roll, and switch to Phase 2
    on the SAME iterator (no stream reopen — that's the 2026-06-07 fix for clipped leading
    syllables).

    Phase 2 (capture): keep appending chunks until end_silence_chunks of continuous sub-
    threshold audio (end-of-speech) or max_chunks (hard cap).

    Returns concatenated captured audio (pre-roll + onset → end), or None if is_speaking()
    went False with no interruption."""
    from collections import deque
    # Pre-roll must be at least as long as the debounce, or onset chunks would fall out.
    preroll = deque(maxlen=max(preroll_chunks, need))
    collected: list = []
    voiced = 0
    silence_run = 0
    barged = False

    for f, prob in frame_prob_iter:
        if not barged and not is_speaking():
            return None  # finished speaking with no interruption
        is_voiced = prob >= threshold

        if not barged:
            preroll.append(f)
            voiced = voiced + 1 if is_voiced else 0
            if voiced >= need:
                barged = True
                if on_event:
                    on_event("detect", prob)
                try:
                    on_barge()
                except Exception:
                    pass
                if on_event:
                    on_event("stopped", prob)
                collected.extend(preroll)   # seed with pre-roll incl. onset chunks
                silence_run = 0
        else:
            collected.append(f)
            if is_voiced:
                silence_run = 0
            else:
                silence_run += 1
                if silence_run >= end_silence_chunks:
                    break  # end-of-speech
            if len(collected) >= max_chunks:
                break  # hard cap

    if not collected:
        return None
    return np.concatenate(collected)


def _bargein_watch_and_capture(ctx, model, is_speaking, on_barge, *,
                               device_substr: "str | None" = None,
                               threshold: float = VAD_THRESHOLD,
                               debounce_ms: int = VAD_DEBOUNCE_MS,
                               preroll_ms: int = BARGEIN_PREROLL_MS,
                               end_silence_ms: int = BARGEIN_END_SILENCE_MS,
                               max_capture_s: float = BARGEIN_MAX_CAPTURE_S,
                               on_event=None) -> "np.ndarray | None":
    """ONE continuous mic stream that watches for a barge-in via Silero VAD AND captures
    the interrupting utterance on the same stream (no second open).

    Opens a single sd.InputStream at 512-sample blocksize, feeds each chunk to the
    Silero model for a speech probability, and delegates the state machine to
    _bargein_accumulate. Returns captured float32 audio or None if I finished speaking
    uninterrupted."""
    import torch

    # Reuse the main-thread-resolved mic index (worker-thread query_devices hangs on Win).
    dev_idx = getattr(ctx, "mic_dev_idx", None)
    if dev_idx is None:
        dev_idx = wl.find_input_device(device_substr or wl.DEFAULT_MIC_SUBSTR)
    need = max(1, round(debounce_ms / VAD_CHUNK_MS))
    preroll_chunks = max(1, round(preroll_ms / VAD_CHUNK_MS))
    end_silence_chunks = max(1, round(end_silence_ms / VAD_CHUNK_MS))
    max_chunks = max(1, int(max_capture_s * SAMPLE_RATE / VAD_CHUNK))

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        device=dev_idx, blocksize=VAD_CHUNK) as stream:
        def _frames():
            while True:
                frame, _ov = stream.read(VAD_CHUNK)
                f = np.asarray(frame).flatten()
                if f.size != VAD_CHUNK:
                    continue  # Silero needs EXACTLY 512 samples
                try:
                    prob = float(model(torch.from_numpy(f), SAMPLE_RATE).item())
                except Exception:
                    prob = 0.0
                yield f, prob

        return _bargein_accumulate(_frames(), is_speaking, on_barge,
                                   threshold=threshold, need=need,
                                   preroll_chunks=preroll_chunks,
                                   end_silence_chunks=end_silence_chunks,
                                   max_chunks=max_chunks, on_event=on_event)


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC FUNCTIONS (the daemon calls these)
# ═══════════════════════════════════════════════════════════════════════════════

def _rich_health(ctx) -> list:
    """Cheap in-daemon liveness of the OPTIONAL degrade-with-announcement components
    (smart-turn, prosody) — the ones cmd_call_start lets a call run degraded on.
    Returns the list of components NOT loadable on THIS live process.

    Runs IN the daemon (via cmd_status), so it sees the LIVE imported modules — an
    external prober would re-import the corrected disk path and miss a stale daemon
    (the exact trap behind the day-dead smart-turn). Checks constructability
    (path/flag), NOT warmth: a cold component between calls is NOT degraded; only a
    missing model / failed import is. No warm, no inference — the deeper dummy-infer
    probe (loads-but-errors-on-predict, corrupt model) is the watchdog's job. This is
    the heartbeat that pays the debt our graceful fallback runs up: it surfaces the
    silent-corpse class that 'voice works' on the fallback would otherwise hide.
    (Converged with Wren — her _rich_health, 1e9015e.)
    """
    degraded: list = []
    # smart-turn: degraded if the import failed OR the model the LIVE module would load
    # isn't on disk (the exact path _get_session raises FileNotFoundError on).
    try:
        if (not _HAVE_SMARTTURN) or (not _smartturn._MODEL_PATH.exists()):
            degraded.append("smartturn")
    except Exception:
        degraded.append("smartturn")
    # prosody: degraded if the import failed (shares the whisper singleton, no own model).
    try:
        if not _HAVE_PROSODY:
            degraded.append("prosody")
    except Exception:
        degraded.append("prosody")
    return degraded


def cmd_status(ctx, args: dict) -> str:
    """Check mouth server /health at ctx.server_url, read_state(), is_muted().
    Returns a status string in the same style as the old voice_status tool."""
    server_ok = False
    try:
        with _req.urlopen(ctx.server_url + "/health", timeout=3) as resp:
            server_ok = resp.read().decode("utf-8", "replace").strip() == "ok"
    except Exception:
        server_ok = False
    try:
        st = read_state()
    except Exception:
        st = {}
    try:
        muted = is_muted()
    except Exception:
        muted = False
    try:
        rich_degraded = _rich_health(ctx)
    except Exception:
        rich_degraded = []
    return (f"voice_server_warm={server_ok}  state={st.get('state')}  "
            f"muted={muted}  rich_degraded={rich_degraded}  detail={st.get('detail','')!r}")


def cmd_speak(ctx, args: dict) -> str:
    """Enqueue text for the daemon playback worker.

    args: {"text": str, "wait": bool=False}

    Sets ctx.speaking=True and puts text in ctx.play_queue. If wait=True, blocks
    until the queue drains (ctx.play_queue.join()) then returns '[voice_speak] spoken'.
    Otherwise returns immediately with a '[queued]' prefix.

    Actual playback is done by the daemon's worker calling play_one().
    """
    text = (args.get("text") or "").strip()
    if not text:
        return "[voice_speak] nothing to say"
    # Barge-in (2026-07-08): once Zeke has cut in during this hold window, the REST of
    # the interrupted reply stays silent — later sentences from the same turn would
    # otherwise restart my voice over his. Cleared by cmd_bargein_hold on both edges.
    if getattr(ctx, "bargein_fired", False):
        return "[voice_speak] dropped (barge-in: reply was interrupted)"
    _cancel_stall_bridge(ctx)   # a real reply (or a warned pause) suppresses the stall bridge
    # Enqueue BEFORE flipping the flag. The playback worker resets ctx.speaking=False when
    # it observes an empty queue; if we set speaking=True first and the worker's empty-check
    # lands in the gap before put(), it resets the flag to False while this reply is about to
    # play — and a concurrent listen's drain-gate (`while ctx.speaking`) then opens the mic
    # onto my own voice (self-listen, real on speakers-no-AEC). put()-first means the worker
    # always sees the queued item, so it never spuriously clears the flag. (2026-06-29 race fix.)
    ctx.play_queue.put(text)
    ctx.speaking = True
    if args.get("wait"):
        try:
            ctx.play_queue.join()
        except Exception:
            pass
        return "[voice_speak] spoken"
    return f"[queued] {text[:60]}"


# rotating filler index — module-level so it survives across turns; a reload resets it
# to 0, which is harmless (just restarts the rotation).
_FILLER_I = [0]

# Deferred-prosody hand-off (latency pass): the bg enrich thread puts the freshest
# per-word prosody line here; the next listen turn pulls it. maxsize=1 + "drop oldest
# then put" = the consumer always gets the most-recent completed enrich and a turn is
# never double-delivered. Queue is internally locked, so the read (main thread) and
# write (bg thread) need no extra lock (fixes the read-then-clear race). A reload
# recreates it empty — harmless (one pending line lost).
_PROSODY_Q: "queue.Queue" = queue.Queue(maxsize=1)


def _fire_filler(ctx) -> None:
    """Enqueue a SHORT backchannel filler so Zeke hears something within ~1s of his last
    word, masking my think-time. Plays via the normal play_queue (the mouth's ~0.9s lead),
    so my real reply queues right behind it. Rotated, non-fatal."""
    try:
        f = CALL_FILLERS[_FILLER_I[0] % len(CALL_FILLERS)]
        _FILLER_I[0] += 1
        ctx.play_queue.put(f)        # put before flag — see cmd_speak (self-listen race)
        ctx.speaking = True
    except Exception as e:
        print(f"[voice_listen] filler enqueue failed (non-fatal): {e!r}", file=sys.stderr)


# ── Stall bridge (Wren's port 2026-06-27) ─────────────────────────────────────
# A think-gap backstop for calls. CALL_FILLERS mask the first ~1s after Zeke stops,
# but a heavy turn leaves a silent "processing" gap until my real words land at the
# ~11s latency floor. The stall bridge arms at the end of each in-call listen turn:
# a one-shot timer speaks ONE slightly-longer bridging phrase if my reply (cmd_speak)
# hasn't started within STALL_FILLER_DELAY_S. A per-turn token (ctx._stall_turn) is
# bumped by every cmd_speak — including a warned "give me a second" — so the bridge
# fires ONLY on an unwarned silent gap, self-aligning with the speak-first-or-warn-
# first rule. At most once per turn, gated on call_warm, NEVER touches the mic (it
# only fills my own silence). Lives entirely in the reloadable core — hot-reloads, no
# daemon edit. Threshold owed live-call tuning (synth ≠ Zeke's pace; see Wren's note).
STALL_BRIDGE = True
STALL_FILLER_DELAY_S = 3.5
STALL_FILLERS = ("Let me think on that.", "Give me a moment.", "Let me sit with that for a second.")
_STALL_I = [0]


def _arm_stall_bridge(ctx) -> None:
    """Arm a one-shot timer that speaks a bridging phrase if the cognition hasn't begun
    its reply within STALL_FILLER_DELAY_S. Cancelled when cmd_speak bumps ctx._stall_turn.
    Fields set lazily on ctx (no Ctx-class change). Non-fatal throughout."""
    if not (STALL_BRIDGE and getattr(ctx, "call_warm", False)):
        return
    try:
        turn = getattr(ctx, "_stall_turn", 0) + 1
        ctx._stall_turn = turn

        def _bridge() -> None:
            try:
                # Fire only if this turn is still current (no cmd_speak bumped the token),
                # the call is still warm, and the mouth isn't already speaking.
                if getattr(ctx, "_stall_turn", -1) != turn:
                    return
                if not getattr(ctx, "call_warm", False):
                    return
                if getattr(ctx, "speaking", False):
                    return
                # Barge-in (2026-07-08): Zeke already cut in this window — a bridging
                # phrase now would talk over him. Stay silent.
                if getattr(ctx, "bargein_fired", False):
                    return
                phrase = STALL_FILLERS[_STALL_I[0] % len(STALL_FILLERS)]
                _STALL_I[0] += 1
                # Re-check the token right before enqueue to shrink the cmd_speak race.
                if getattr(ctx, "_stall_turn", -1) != turn:
                    return
                ctx.play_queue.put(phrase)   # put before flag — see cmd_speak (self-listen race)
                ctx.speaking = True
                print(f"[stall-bridge] fired turn={turn}: {phrase!r}", file=sys.stderr)
            except Exception as e:
                print(f"[stall-bridge] fire failed (non-fatal): {e!r}", file=sys.stderr)

        t = threading.Timer(STALL_FILLER_DELAY_S, _bridge)
        t.daemon = True
        ctx._stall_timer = t
        t.start()
    except Exception as e:
        print(f"[stall-bridge] arm failed (non-fatal): {e!r}", file=sys.stderr)


def _cancel_stall_bridge(ctx) -> None:
    """Bump the per-turn token (tells an armed bridge this turn is no longer silent) and
    cancel any pending timer. Called by cmd_speak so a real reply — or a warned
    'give me a second' — suppresses the bridge. Non-fatal."""
    try:
        ctx._stall_turn = getattr(ctx, "_stall_turn", 0) + 1
        t = getattr(ctx, "_stall_timer", None)
        if t is not None:
            t.cancel()
            ctx._stall_timer = None
    except Exception:
        pass


def play_one(ctx, text: str) -> None:
    """Speak one chunk via the mouth server. Called by the daemon's playback worker.
    Blocks until playback finishes (the server holds the connection open until done).
    Does not raise — failures are swallowed so the worker loop stays alive."""
    try:
        _post_speak(ctx, text)
    except Exception as e:
        print(f"[wren-voice-core] play_one error: {e!r}", file=sys.stderr)


def on_drain(ctx) -> None:
    """Called by the daemon worker when the play_queue empties.

    Flip-cue (Zeke 2026-06-14): after I finish speaking the overlay should show
    'processing' rather than sitting on dead 'idle' — sequence is
    speaking → processing → listening (voice_listen flips it to 'listening'). Wrapped
    in try/except so a failed set_state never kills the worker."""
    try:
        set_state("thinking", "processing")
    except Exception:
        pass


# ── Phantom gate (2026-07-08) ────────────────────────────────────────────────
# Whisper hallucinates short filler tokens ("you", "thank you", ...) on near-silent
# or click-onset captures — observed live 3× in ~10min (identical 0.1s "you" turns,
# each burning a full cognition turn while the room was EMPTY). Gate: a capture
# whose ENTIRE transcript is one of these classic hallucination tokens AND whose
# raw audio is short gets discarded as a no-speech marker (the host already drops
# "[voice_listen]" markers). Real short utterances ("yes", "no", "stop") are NOT
# in the set, so nothing Zeke actually says gets eaten; worst case he says a bare
# "you" (improbable as a full turn) and repeats once.
# Two legs, either damns a hallucination-text capture (v2 tuning, same day):
# - PHANTOM_MAX_S = 0.5: no human word fits in half a second of RAW capture — the live
#   phantoms were 0.1s. Kept TIGHT on purpose: a genuine bare "Thank you." runs ~2s raw
#   (Zeke says exactly that — twice this morning) and must survive on this leg.
# - PHANTOM_RMS = 0.004: real speech carries real energy (CONTINUE_PEAK alone is 0.012
#   per-frame); a click-plus-room-hum capture has whole-capture RMS far below that.
#   This leg catches the v1 miss: onset click + stretched noise tail can run raw length
#   to many seconds (the pace layer's "0.1s" is SPEECH time, not raw) while staying
#   near-silent — duration alone was the wrong ruler.
PHANTOM_MAX_S = 0.5
PHANTOM_TEXTS = {"you", "thank you", "thanks", "the", "uh", "um", "mm", "hmm", "bye"}
PHANTOM_RMS = 0.004


def _is_phantom(text: str, audio) -> bool:
    """True if this capture looks like a whisper silence-hallucination, not speech."""
    try:
        n = float(getattr(audio, "size", 0))
        if n <= 0:
            return False
        dur_s = n / float(SAMPLE_RATE)
        rms = float(np.sqrt(np.mean(np.square(audio))))
        norm = re.sub(r"[^a-z ]", "", (text or "").lower()).strip()
        return bool(norm) and norm in PHANTOM_TEXTS \
            and (dur_s <= PHANTOM_MAX_S or rms < PHANTOM_RMS)
    except Exception:
        return False   # fail-open: never eat a turn on a gate error


def cmd_listen(ctx, args: dict) -> str:
    """Listen for Zeke to speak and return what he said.

    args: {
        "timeout_seconds": float = 45.0,
        "max_utterance_seconds": float = 20.0,
        "end_silence_seconds": float = 0.0,   # 0 → use module default END_SILENCE_S
    }

    Steps:
      1. Wait until ctx.speaking is False (playback drained) — replaces the old
         _wait_until_clear_to_listen.
      2. Capture utterance.
      3. Transcribe — single-pass in-call if ctx.call_warm and _HAVE_PROSODY.
      4. Grace loop: if incomplete, re-open mic up to MAX_GRACE_ROUNDS times.
      5. append_transcript, pace analysis, tone hint (out-of-call only), prosody line
         (in-call only), end_call_intent tag.

    Returns the multi-line enriched transcript string, or an error/no-speech marker.
    """
    timeout_s = float(args.get("timeout_seconds", 45.0))
    max_s = float(args.get("max_utterance_seconds", 20.0))
    end_sil = float(args.get("end_silence_seconds", 0.0))
    end_silence = end_sil if end_sil > 0 else END_SILENCE_S

    # ── Step 1: wait until playback drained, then settle ──────────────────────
    # The drain-gate (wait for ctx.speaking==False) is the strong form of Wren's
    # self-listen finding (2026-06-26): speaking flips False only AFTER play_one
    # returns, i.e. after the mouth finishes playing the last chunk — so the mic
    # opens on real playback completion, not a fixed trailing guard. On speakers
    # with no AEC the mic would otherwise capture my OWN tail as if it were Zeke
    # (it bit Wren three times in one call). The post-drain settle below covers
    # the device-flush tail (audio can report done a beat before the speaker is
    # physically silent). It is a MARGIN on top of full-playback drain, not the
    # sole guard — which is why 0.35s is enough here though it wasn't for Wren.
    t0 = time.time()
    # Cap is a WEDGED-MOUTH backstop, not a normal timeout — it must exceed the longest
    # real reply or it fires MID-PLAYBACK and the mic opens onto my own voice. Wren hit
    # exactly this 2026-06-26: a ~45s reply on an AI-to-AI speaker bridge (no echo cancel)
    # outran the old 30s cap and she captured her whole message back. `speaking` is the
    # reliable signal; 180s just guards a truly stuck mouth. (Wren commit ee21146.)
    while getattr(ctx, "speaking", False) and time.time() - t0 < 180.0:
        time.sleep(0.05)
    time.sleep(SELF_LISTEN_TRAILING_S)

    # ── Step 2: capture (with streaming partials when in-call) ────────────────
    # in_call is decided BEFORE capture so the partial transcriber (Route 1) can run
    # during it. Partials reuse the warm whisper via _prosody.transcribe (word-timestamped,
    # same as the final pass), and skip while the mouth speaks (GPU-contention guard).
    in_call = getattr(ctx, "call_warm", False) and _HAVE_PROSODY
    partials = _StreamingPartials() if (in_call and STREAMING_PARTIALS) else None
    ef_state: dict = {}   # _capture_utterance sets {"fired": True} if early-finalize triggered
    try:
        with ctx.mic_lock:
            audio = _capture_utterance(
                ctx, timeout_s, max_s, end_silence,
                partials=partials,
                transcribe_fn=(_prosody.transcribe if in_call else None),
                mouth_busy_fn=(lambda: getattr(ctx, "speaking", False)),
                early_finalize_fn=(_endpoint_complete_confident if in_call else None),
                ef_state=ef_state,
            )
    except Exception as e:
        set_state("idle")
        return f"[voice_listen] capture error: {e!r}"

    if audio is None or audio.size == 0:
        set_state("idle")
        return f"[voice_listen] no speech within {timeout_s:.0f}s"

    set_state("thinking", "heard you")

    # ── Step 3a: filler-earlier — fire the backchannel BEFORE transcribe (Wren's port) ──
    # REUSE early-finalize's smart-turn result instead of re-calling it (2026-06-27): if
    # capture ended because early-finalize was confident he's done (ef_state["fired"]), that
    # IS the high-confidence endpoint signal — fire the backchannel now, no second smart-turn
    # call. If early-finalize didn't fire, capture ended on full end-silence/max where smart-
    # turn wasn't 0.85-confident anyway, so the post-transcribe fallback (Step 4) covers it.
    # Cough-guard kept as belt-and-suspenders; one per turn via filler_fired.
    filler_fired = False
    if (in_call and EARLY_FILLER and ef_state.get("fired")
            and audio.size >= int(SAMPLE_RATE * FILLER_MIN_AUDIO_S)):
        _fire_filler(ctx)
        filler_fired = True

    # ── Step 3: transcribe (Route 1: use the freshest partial if its tail is silent) ──
    words_data: list = []
    final_ok = True   # False if a grace re-transcribe failed → audio/words mismatch, skip prosody
    try:
        if in_call:
            text, words_data = _transcribe_in_call(audio, partials)
        else:
            text = (wl._transcribe_array(audio) or "").strip()
    except Exception as e:
        # Prosody/partial transcribe failed at RUNTIME → DEGRADE to the plain whisper
        # path (the out-of-call shape) instead of killing the turn. This is what makes
        # prosody actually degradable (Wren's gap-find 2026-06-26, her fe45048): the
        # non-fatal enrich() only covers the per-word FEATURE layer; the transcribe call
        # itself could still error the turn — half-true degradability, now whole. Only a
        # SECOND failure (plain whisper also down) errors out.
        print(f"[voice_listen] in-call transcribe failed ({e!r}); plain-whisper fallback",
              file=sys.stderr)
        try:
            with _WHISPER_LOCK:   # full parity: never overlap a lingering partial pass
                text = (wl._transcribe_array(audio) or "").strip()
            words_data = []
        except Exception as e2:
            set_state("idle")
            return f"[voice_listen] transcription error: {e2!r}"

    # ── Step 3b: phantom gate — discard whisper silence-hallucinations ─────────
    if _is_phantom(text, audio):
        set_state("idle")
        dur_s = float(getattr(audio, "size", 0)) / float(SAMPLE_RATE)
        rms = float(np.sqrt(np.mean(np.square(audio)))) if getattr(audio, "size", 0) else 0.0
        print(f"[voice_listen] phantom discarded ({dur_s:.2f}s, rms={rms:.4f}, {text!r})",
              file=sys.stderr, flush=True)
        return f"[voice_listen] phantom discarded ({dur_s:.2f}s, rms={rms:.4f}, {text!r})"

    # ── Step 4: completeness gate → turn-end filler + grace loop ───────────────
    # "Complete" needs BOTH signals to agree he's done: smart-turn sees a clean endpoint
    # in the waveform AND the transcript doesn't dangle on a continuation cue. Gating on
    # both (breaker finding 2) stops the filler from playing over a trailing clause the
    # grace loop is about to re-capture. Because the grace loop runs only while NOT
    # complete, the filler and the grace re-open never fire on the same turn.
    def _complete() -> bool:
        return (not _endpoint_incomplete(audio)) and (not _looks_incomplete(text))

    # Post-transcribe fallback filler: a complete turn that scored below the early-fire bar
    # (so Step 3a didn't fire) still gets exactly one backchannel here. `not filler_fired`
    # guarantees we never double-fire when filler-earlier already played one this turn.
    if in_call and text and not filler_fired and _complete():
        _fire_filler(ctx)
        filler_fired = True

    rounds = 0
    while text and not _complete() and rounds < MAX_GRACE_ROUNDS:
        rounds += 1
        set_state("listening", "still with you")
        try:
            with ctx.mic_lock:
                more = _capture_utterance(ctx, GRACE_RESUME_TIMEOUT, max_s, end_silence)
        except Exception:
            more = None
        if more is None or not getattr(more, "size", 0):
            break  # the cue was a false alarm — he really did stop
        audio = np.concatenate([audio, more])
        set_state("thinking", "heard you")
        try:
            with _WHISPER_LOCK:   # full parity: serialize against any lingering partial pass
                if in_call:
                    text, words_data = _prosody.transcribe(audio)
                    text = (text or "").strip()
                else:
                    text = (wl._transcribe_array(audio) or "").strip()
        except Exception:
            final_ok = False
            break
        # he resumed and has now closed a complete clause → fire the filler once
        if in_call and text and not filler_fired and _complete():
            _fire_filler(ctx)
            filler_fired = True

    if not text:
        set_state("idle")
        return "[voice_listen] (unclear / empty transcription)"

    # ── Step 5: transcript history ────────────────────────────────────────────
    # Capture-stats instrumentation (2026-07-08): a phantom passed the v2 gate (dur>0.5s
    # AND rms>=0.004 — noise with real energy). Tuning PHANTOM_RMS blind risks eating
    # Zeke's real quiet speech, so log dur+rms for EVERY accepted capture (stderr →
    # daemon.log via the watchdog redirect) and tune from evidence. Remove once tuned.
    try:
        _dur = float(getattr(audio, "size", 0)) / float(SAMPLE_RATE)
        _rms = float(np.sqrt(np.mean(np.square(audio)))) if getattr(audio, "size", 0) else 0.0
        print(f"[voice_listen] capture stats: dur={_dur:.2f}s rms={_rms:.4f} text={text[:48]!r}",
              file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        wl.append_transcript(text)
    except Exception:
        pass

    # ── Step 6: pace ─────────────────────────────────────────────────────────
    try:
        _metrics, pace = wren_pace.analyze(audio, SAMPLE_RATE, text)
    except Exception:
        pace = ""

    # ── Step 7: tone hint (out-of-call only) ─────────────────────────────────
    tone = ""
    if not in_call:
        try:
            rich = wl.pop_last_rich()
            tone = f"[tone(hint): {rich['emotion']}]" if rich and rich.get("emotion") else ""
        except Exception:
            tone = ""

    # ── Step 8: prosody — INLINE / CURRENT (2026-06-17, Zeke's call in-call) ──────
    # Was deferred a turn to keep the OLD big per-word block off the critical path. That
    # block is gone — enrich() is now a compact, inline-marked transcript — so the defer
    # is no longer worth it, and Zeke wanted the emphasis CURRENT (the same turn he speaks,
    # not a beat behind the conversation). enrich() is pure numpy (~a second, no GPU/mic/
    # mouth). final_ok guards the rare grace path where words_data is pre-grace audio while
    # `audio` is the full concat (would mis-time enrich). Out-of-call path unchanged.
    prosody_now = ""
    if in_call and final_ok and words_data:
        try:
            prosody_now = _prosody.enrich(audio, words_data)
        except Exception as e:
            print(f"[voice_listen] prosody error (non-fatal): {e!r}", file=sys.stderr)
            prosody_now = ""

    # ── Step 9: end_call_intent tag ───────────────────────────────────────────
    end_call_intent = ""
    try:
        t_lower = text.lower().strip()
        if any(phrase in t_lower for phrase in ("end call", "end the call", "let's end call",
                                                 "lets end call")):
            end_call_intent = "[intent: end_call]"
    except Exception:
        pass

    if prosody_now:
        # In-call: the marked transcript IS the transcript — his words + emphasis + one
        # pace line, all in one. Don't ALSO emit the plain text or a second pace line;
        # that put his words (and the pace) in my context TWICE every turn = latency creep.
        result = prosody_now
    else:
        suffix = " ".join(s for s in (pace, tone) if s)
        result = f"{text}\n{suffix}" if suffix else text
    if end_call_intent:
        result = f"{result}\n{end_call_intent}"
    # Arm the stall bridge for the upcoming think-gap — in-call, non-terminal turns only
    # (no point bridging a turn where Zeke just asked to end the call).
    if in_call and not end_call_intent:
        _arm_stall_bridge(ctx)
    return result


def cmd_speak_interruptible(ctx, args: dict) -> str:
    """Speak text but let Zeke barge in mid-sentence (Layer 1, headphones).

    args: {
        "text": str,
        "timeout_seconds": float = 45.0,
        "max_utterance_seconds": float = 20.0,
    }

    Watches the mic with Silero VAD (ctx.vad_model) while speaking on a worker
    thread. On confirmed onset: stops the TTS server (/stop), captures the interrupting
    turn on the SAME stream (no reopen — the 2026-06-07 pre-roll fix). Returns
    '[barge-in] <his words>' if interrupted, else the normal speak result.

    Falls back to plain _post_speak if ctx.vad_model is None (VAD not warm yet).
    """
    text = (args.get("text") or "").strip()
    if not text:
        return "[voice_speak] nothing to say"

    with ctx.vad_lock:
        model = ctx.vad_model
    if model is None:
        # VAD not warm — don't hold barge-in behind a cold load; just speak normally.
        # Set ctx.speaking around the blocking speak (mirrors cmd_speak) so the NEXT
        # listen's drain-gate (`while ctx.speaking`) waits out my playback + tail instead
        # of opening the mic onto my own voice. _post_speak is a direct blocking POST — it
        # does NOT go through the worker that normally resets the flag — so reset it here.
        # (Wren sweep finding #3, 2026-06-29.)
        ctx.speaking = True
        try:
            return _post_speak(ctx, text)
        finally:
            ctx.speaking = False

    speak_result: dict = {}

    def _do_speak():
        speak_result["text"] = _post_speak(ctx, text)

    worker = threading.Thread(target=_do_speak, daemon=True, name="wren-bargein-speak")

    try:
        model.reset_states()  # no stale VAD state leaking across turns
    except Exception:
        pass

    def _on_barge():
        # Confirmed onset: stop my voice fast and flip the overlay to listening. Capture
        # continues on the SAME stream (pre-roll already seeded), so the leading syllable
        # of the interruption isn't clipped — that's the 2026-06-07 fix.
        set_state("listening", "you cut in")
        try:
            _req.urlopen(ctx.server_url + "/stop", timeout=3).read()
        except Exception:
            pass

    max_s = float(args.get("max_utterance_seconds", 20.0))

    # NB: deliberately BYPASS ctx.speaking wait — barge-in WANTS the mic open while
    # I speak; the play-drain guard is its exact opposite.
    worker.start()
    try:
        with ctx.mic_lock:
            audio = _bargein_watch_and_capture(
                ctx, model, worker.is_alive, _on_barge,
                device_substr=wl.DEFAULT_MIC_SUBSTR,
                max_capture_s=max_s)
    except Exception as e:
        worker.join(timeout=180)
        return (f"[voice_speak_interruptible] watcher error ({e!r}); "
                + speak_result.get("text", "[no result]"))

    if audio is None or getattr(audio, "size", 0) == 0:
        worker.join(timeout=180)  # finished speaking, no interruption
        return speak_result.get("text", "[voice_speak] spoken")

    # He cut in — capture accumulated on same stream (pre-roll + onset → end-of-speech).
    worker.join(timeout=10)
    set_state("thinking", "heard you")
    try:
        heard = (wl._transcribe_array(audio) or "").strip()
    except Exception as e:
        set_state("idle")
        return f"[voice_speak_interruptible] interrupted, transcription error: {e!r}"
    set_state("idle")
    if not heard:
        return "[voice_speak_interruptible] interrupted (unclear transcription)"
    try:
        wl.append_transcript(heard)
    except Exception:
        pass
    return f"[barge-in] {heard}"


# ═══════════════════════════════════════════════════════════════════════════════
# CONTINUOUS BARGE-IN (headphones mode, 2026-07-08)
#
# Zeke confirmed this tower ALWAYS runs headphones: my TTS never reaches the mic,
# so any VAD-confirmed speech during my playback IS Zeke by construction — no AEC,
# no voiceprint gate needed on this box. (barge_in_scoped_two_layer_2026-06-29
# remains the SPEAKERS design; this is Layer-1 headphones — the shortcut that note
# said this box didn't have. The hardware fact in that note was wrong.)
#
# Shape: the HOST drives it. Around a speaking turn the host sets
# bargein_hold(True) and issues ONE blocking bargein_watch. The watch holds the
# mic (ctx.mic_lock, same pattern as cmd_speak_interruptible) and runs Silero VAD
# frames until either:
#   (a) Zeke's speech is confirmed → stop the mouth (/stop), flush the play queue,
#       mark ctx.bargein_fired (cmd_speak + stall bridge go silent for the rest of
#       the window), capture his utterance on the SAME stream, transcribe, and
#       return '[barge-in] <words>'; or
#   (b) the host clears the hold at turn end (or the cap expires) → '... no_barge'.
# All state lives as lazy ctx attributes (getattr defaults) so this hot-reloads
# with cmd='reload' — no daemon bounce, no Ctx-class edit.
# ═══════════════════════════════════════════════════════════════════════════════

BARGEIN_WATCH_CAP_S = 900.0   # backstop on one watch window; host ends it at turn end


def _flush_play_queue(ctx) -> int:
    """Drain every pending (unplayed) chunk from the play queue. Returns the count.
    task_done() per item keeps play_queue.join() (cmd_speak wait=True) from hanging."""
    n = 0
    try:
        while True:
            ctx.play_queue.get_nowait()
            try:
                ctx.play_queue.task_done()
            except Exception:
                pass
            n += 1
    except queue.Empty:
        pass
    except Exception:
        pass
    return n


def cmd_bargein_hold(ctx, args: dict) -> dict:
    """Open/close the barge-in window. args: {"hold": bool}.

    hold=True  → a speaking turn is starting; bargein_watch becomes active.
    hold=False → the turn (including playback tail) is over; a running watch
                 returns no_barge promptly.
    BOTH edges clear bargein_fired, so a barge only mutes the reply it actually
    interrupted — never a later deliberate voice_speak."""
    hold = bool(args.get("hold", False))
    ctx.bargein_hold = hold
    ctx.bargein_fired = False
    return {"hold": hold}


def cmd_bargein_watch(ctx, args: dict) -> str:
    """Blocking: watch the mic for Zeke talking over me (headphones barge-in).

    args: {"cap_s": float = BARGEIN_WATCH_CAP_S}

    Active while ctx.bargein_hold is True. Returns:
      '[barge-in] <his words>'            — he cut in (mouth already stopped)
      '[bargein_watch] no_barge'          — window closed with no interruption
      '[bargein_watch] vad_not_warm' / 'no_hold' / errors — fail-soft markers
    """
    with ctx.vad_lock:
        model = ctx.vad_model
    if model is None:
        return "[bargein_watch] vad_not_warm"
    if not getattr(ctx, "bargein_hold", False):
        return "[bargein_watch] no_hold"
    cap_s = float(args.get("cap_s", BARGEIN_WATCH_CAP_S))
    deadline = time.time() + cap_s
    try:
        model.reset_states()   # no stale VAD state leaking across windows
    except Exception:
        pass

    def _active() -> bool:
        return bool(getattr(ctx, "bargein_hold", False)) and time.time() < deadline

    def _on_barge() -> None:
        # Confirmed onset: silence me FAST, then keep capturing his words.
        ctx.bargein_fired = True          # cmd_speak drops the rest of this reply
        _cancel_stall_bridge(ctx)         # no bridging phrase over his cut-in
        n = _flush_play_queue(ctx)        # unplayed sentences never start
        try:
            _req.urlopen(ctx.server_url + "/stop", timeout=3).read()   # kill current audio
        except Exception:
            pass
        ctx.speaking = False
        try:
            set_state("listening", "you cut in")
        except Exception:
            pass
        print(f"[bargein_watch] onset: mouth stopped, {n} queued chunk(s) flushed",
              flush=True)

    try:
        # Same-thread RLock re-entry is safe (daemon dispatch doesn't lock this cmd;
        # we serialize against listen/speak_interruptible ourselves, like they do).
        with ctx.mic_lock:
            audio = _bargein_watch_and_capture(ctx, model, _active, _on_barge,
                                               device_substr=wl.DEFAULT_MIC_SUBSTR)
    except Exception as e:
        return f"[bargein_watch] watcher error: {e!r}"

    if audio is None or getattr(audio, "size", 0) == 0:
        return "[bargein_watch] no_barge"

    try:
        set_state("thinking", "heard you")
    except Exception:
        pass
    try:
        heard = (wl._transcribe_array(audio) or "").strip()
    except Exception as e:
        return f"[bargein_watch] interrupted, transcription error: {e!r}"
    if not heard:
        return "[bargein_watch] interrupted (unclear)"
    try:
        wl.append_transcript(heard)
    except Exception:
        pass
    return f"[barge-in] {heard}"


def cmd_call_start(ctx, args: dict) -> dict:
    """Warm the voice pipeline for an active call.

    Flow:
      1. Announce early via the mouth ("hey, give me a second — starting the call").
      2. Warm each component, each timeout-guarded:
         - Mouth: GET server_url/warm + poll /health.
         - Whisper: wl.get_whisper() + dummy infer on 0.1s silence.
         - Smart-turn: predict_endpoint dummy infer.
         - Prosody: wren_prosody.analyze() on silence.
         SenseVoice NOT warmed: in-call uses single-pass whisper ears; SenseVoice
         unused during a call (status["sensevoice"] = "n/a — single-pass in-call").
      3. Gate: all 4 must pass.
      4. If all pass: speak "okay — it's good" and set ctx.call_warm=True.
         If any fail: speak what failed, leave ctx.call_warm=False.

    Returns dict: mouth, whisper, smartturn, prosody, sensevoice, all_ready, spoken.
    """
    # EARS-ON-BOOT (Iris port of Wren's shape, letter afa06739ae53, built 2026-07-06):
    # silent=True warms + flips call_warm + logs exactly the same, but SUPPRESSES the
    # spoken announce/confirm so a boot-time warm doesn't talk to an empty room.
    # mouth_deadline_s widens the /health poll for the boot race (mouth server may
    # still be starting when the daemon comes up).
    silent = bool(args.get("silent"))
    mouth_deadline_s = float(args.get("mouth_deadline_s") or 75.0)

    status: dict = {
        "mouth": False,
        "whisper": False,
        "smartturn": False,
        "prosody": False,
        "sensevoice": False,
        "all_ready": False,
        "spoken": "",
    }

    # Clear any stall-bridge timer left armed from a prior call/turn (Wren's port,
    # 37f3267): a fresh call must not inherit a pending bridge.
    _cancel_stall_bridge(ctx)

    # ── Step 1: announce early (skipped when silent) ──────────────────────────
    if not silent:
        try:
            _post_speak(ctx, "hey, give me a second — starting the call")
            status["spoken"] = "hey, give me a second — starting the call"
        except Exception as e:
            print(f"[call_start] mouth announce failed: {e!r}", file=sys.stderr)
    else:
        print("[call_start] silent warm (ears-on-boot) — no announce", file=sys.stderr)

    # ── Step 2a: warm MOUTH ───────────────────────────────────────────────────
    def _warm_mouth():
        # StyleTTS cold->warm ~10-30s; deadline set BEFORE /warm. Boot path passes a
        # longer mouth_deadline_s to ride out the mouth server's own process start.
        deadline = time.time() + mouth_deadline_s
        _http_get(ctx.server_url + "/warm", timeout=5.0)
        while time.time() < deadline:
            try:
                hc, _hb = _http_get(ctx.server_url + "/health", timeout=3.0)
                if hc == 200:
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        return False
    status["mouth"] = _run_with_timeout(_warm_mouth, mouth_deadline_s + 5.0, "mouth")

    # ── Step 2b: warm WHISPER ─────────────────────────────────────────────────
    def _warm_whisper():
        _w = wl.get_whisper()
        dummy = np.zeros(int(wl.SAMPLE_RATE * 0.1), dtype=np.float32)
        segs, _ = _w.transcribe(dummy, beam_size=1, language="en")
        list(segs)  # consume the generator to prime CUDA kernels
        return True
    status["whisper"] = _run_with_timeout(_warm_whisper, 30.0, "whisper")

    # ── Step 2c: warm SMART-TURN ──────────────────────────────────────────────
    if _HAVE_SMARTTURN:
        def _warm_smartturn():
            # The FIRST predict_endpoint is cold ~18s on this box: it lazily imports
            # librosa (heavy) to build the mel filterbank, then JITs the numpy FFT path.
            # After this, in-call predicts are ~38ms. 45s timeout gives the cold import
            # real headroom (20s tipped over under concurrent warm load -> "failed: smartturn").
            dummy = np.zeros(int(0.5 * wl.SAMPLE_RATE), dtype=np.float32)
            _smartturn.predict_endpoint(dummy)
            return True
        status["smartturn"] = _run_with_timeout(_warm_smartturn, 45.0, "smartturn")
    else:
        status["smartturn"] = False

    # ── Step 2d: warm PROSODY (shares whisper) ────────────────────────────────
    if _HAVE_PROSODY:
        def _warm_prosody():
            dummy = np.zeros(int(0.5 * wl.SAMPLE_RATE), dtype=np.float32)
            _prosody.analyze(dummy)
            return True
        status["prosody"] = _run_with_timeout(_warm_prosody, 30.0, "prosody")
    else:
        status["prosody"] = False

    # ── SenseVoice: NOT warmed in-call (2026-06-14). Single-pass ears use whisper
    # word_timestamps for both text and prosody, so SenseVoice is unused during a call.
    status["sensevoice"] = "n/a — single-pass in-call"

    # ── Step 3: readiness gate — graceful degrade (Zeke 2026-06-26) ──────────
    # REQUIRED: mouth (can't speak without it) + whisper (can't hear without it).
    # OPTIONAL/degradable: smart-turn (endpoint → falls back to the word-list cue
    # detector _looks_incomplete + fixed end-silence) and prosody (enrich → falls
    # back to plain text). A missing optional component must NOT silently kill the
    # whole call the way the smart-turn bad-model-path did (call_warm stuck False
    # forever, the entire rich path dark and nothing said so). So core-ready warms
    # the call; degraded extras are ANNOUNCED out loud, never hidden.
    core_ready = bool(status["mouth"] and status["whisper"])
    degraded = [k for k in ("smartturn", "prosody") if not status[k]]
    status["all_ready"] = core_ready and not degraded   # kept: "everything incl. extras"
    status["core_ready"] = core_ready
    status["degraded"] = degraded

    # ── Step 4: confirm spoken (logged-only when silent) ─────────────────────
    if core_ready:
        ctx.call_warm = True
        if degraded:
            msg = (f"okay — we're good, but {' and '.join(degraded)} "
                   f"{'is' if len(degraded) == 1 else 'are'} degraded, running on fallback")
        else:
            msg = "okay — it's good"
        if silent:
            print(f"[call_start] silent warm ready: {msg}", file=sys.stderr)
        else:
            try:
                _post_speak(ctx, msg)
                status["spoken"] = msg
            except Exception as e:
                print(f"[call_start] mouth confirm failed: {e!r}", file=sys.stderr)
        if degraded:
            print(f"[call_start] DEGRADED but warm — fallback active for: "
                  f"{', '.join(degraded)}", file=sys.stderr)
    else:
        ctx.call_warm = False
        not_ready = [k for k in ("mouth", "whisper") if not status[k]]
        msg = f"can't start the call — {', '.join(not_ready)} not ready"
        print(f"[call_start] {msg}", file=sys.stderr)
        if not silent:
            try:
                _post_speak(ctx, f"heads up — {msg}")
                status["spoken"] = f"heads up — {msg}"
            except Exception:
                pass

    return status


def cmd_call_end(ctx, args: dict) -> dict:
    """Cold the full voice pipeline after a call — free VRAM, unload models.

    Flow:
      1. Announce "ending the call" (best-effort).
      2. Cold SenseVoice server (GET ctx.sv_port/cold).
      3. Cold the mouth (GET ctx.server_url/cold).
      4. Free in-process whisper singleton (wl._WHISPER = None, gc, cuda.empty_cache).
      5. Free smart-turn + prosody ONNX sessions.
      6. Set ctx.call_warm=False.

    Returns dict with cold status per component.
    """
    result: dict = {
        "mouth_cold": False,
        "whisper_cold": False,
        "smartturn_cold": False,
        "prosody_cold": False,
        "sensevoice_cold": False,
        "spoken": "",
    }

    # Cancel any pending stall-bridge timer FIRST so it can't speak into teardown —
    # the mouth is about to be cold'd and call_warm only flips False at Step 6.
    # (Wren's port, 37f3267)
    _cancel_stall_bridge(ctx)

    # ── Step 1: announce ─────────────────────────────────────────────────────
    try:
        _post_speak(ctx, "ending the call")
        result["spoken"] = "ending the call"
    except Exception:
        pass

    # ── Step 2: cold SenseVoice ───────────────────────────────────────────────
    try:
        sv_port = int(getattr(ctx, "sv_port", 8766))
        code, _ = _http_get(f"http://127.0.0.1:{sv_port}/cold", timeout=10.0)
        result["sensevoice_cold"] = (code == 200)
    except Exception as e:
        print(f"[call_end] sensevoice /cold error: {e!r}", file=sys.stderr)

    # ── Step 3: cold the mouth ────────────────────────────────────────────────
    try:
        code, _ = _http_get(ctx.server_url + "/cold", timeout=10.0)
        result["mouth_cold"] = (code == 200)
    except Exception as e:
        print(f"[call_end] mouth /cold error: {e!r}", file=sys.stderr)

    # ── Step 4: free in-process whisper ──────────────────────────────────────
    try:
        if getattr(wl, "_WHISPER", None) is not None:
            wl._WHISPER = None
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        result["whisper_cold"] = True
    except Exception as e:
        print(f"[call_end] whisper free error: {e!r}", file=sys.stderr)

    # ── Step 5: free smart-turn + prosody ────────────────────────────────────
    if _HAVE_SMARTTURN:
        try:
            _smartturn._SESSION = None
            import gc; gc.collect()
            result["smartturn_cold"] = True
        except Exception as e:
            print(f"[call_end] smartturn free error: {e!r}", file=sys.stderr)
    else:
        result["smartturn_cold"] = True  # nothing to free

    if _HAVE_PROSODY:
        try:
            # prosody shares the whisper singleton (already freed above)
            result["prosody_cold"] = True
        except Exception as e:
            print(f"[call_end] prosody free error: {e!r}", file=sys.stderr)
    else:
        result["prosody_cold"] = True  # nothing to free

    # ── Step 6: flip call-warm flag ──────────────────────────────────────────
    ctx.call_warm = False
    return result


def cmd_backend(ctx, args: dict) -> str:
    """Pick / cold-start / stop which TTS engine the mouth uses.

    args: {
        "name": str,       # engine name (xtts / chatterbox / styletts2)
        "action": str,     # 'switch' | 'start' | 'stop' | 'list'
        "warm_timeout_seconds": float = 240.0,
    }

    Mutates ctx.engine, ctx.server_url, ctx.engine_procs instead of module globals.
    Returns a status string (cold->warm time on start, list of engines on list).
    """
    name = (args.get("name") or "").strip().lower()
    action = (args.get("action") or "switch").strip().lower()
    warm_timeout_seconds = float(args.get("warm_timeout_seconds", 240.0))

    if action == "list" or (not name and action == "switch"):
        # 'current' follows the actual mouth PORT, not the ctx.engine bookkeeping field
        # (which can lag the env-set port at startup). server_url is the routing truth.
        out = [f"current={_engine_for_url(ctx.server_url)}  mouth={ctx.server_url}"]
        for en, (port, _v, _s) in _ENGINES.items():
            up = "UP" if _health_ok(port) else "down"
            mark = "  (started-here)" if en in ctx.engine_procs else ""
            out.append(f"  {en:11s} :{port}  {up}{mark}")
        return "\n".join(out)

    if name not in _ENGINES:
        return f"[voice_backend] unknown engine {name!r}; built: {', '.join(_ENGINES)}"
    port, venv, script = _ENGINES[name]

    if action == "stop":
        p = ctx.engine_procs.pop(name, None)
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
            return f"[voice_backend] stopped {name} (:{port})"
        return f"[voice_backend] {name} not started by this tool (left as-is)"

    # start / switch — cold-start the server if it isn't already healthy
    started = ""
    if not _health_ok(port):
        if action not in ("start", "switch"):
            return f"[voice_backend] {name} not up on :{port} (use action='start')"
        try:
            env = dict(os.environ)
            env["WREN_VOICE_PORT"] = str(port)
            logp = Path(_EXP_DIR).parent / "scratch" / f"backend_{name}.log"
            logp.parent.mkdir(parents=True, exist_ok=True)
            log = open(logp, "ab")
            p = subprocess.Popen([venv, script], cwd=_EXP_DIR, env=env,
                                  stdout=log, stderr=log)
            ctx.engine_procs[name] = p
        except Exception as e:
            return f"[voice_backend] failed to launch {name}: {e!r}"
        t0 = time.time()
        while time.time() - t0 < warm_timeout_seconds:
            if _health_ok(port):
                break
            if p.poll() is not None:
                return (f"[voice_backend] {name} server exited during warmup "
                        f"(rc={p.returncode}); see scratch/backend_{name}.log")
            time.sleep(1.0)
        if not _health_ok(port):
            return f"[voice_backend] {name} did not warm within {warm_timeout_seconds:.0f}s"
        started = f"  cold->warm {time.time()-t0:.1f}s"

    ctx.server_url = f"http://127.0.0.1:{port}"
    ctx.engine = name
    return f"[voice_backend] mouth -> {name} (:{port}){started}"


def reload_helpers() -> list:
    """Hot-reload the helper modules so voice fixes take effect without a full relaunch.

    Reloads: wren_prosody, wren_smartturn, wren_pace (each guarded in try/except).
    Does NOT reload wren_listen — that would reset the warm whisper singleton and
    force a re-load on the next call.

    Returns the list of successfully reloaded module names.
    """
    reloaded: list = []

    if _HAVE_PROSODY:
        try:
            importlib.reload(_prosody)
            reloaded.append("wren_prosody")
        except Exception as e:
            print(f"[reload_helpers] wren_prosody reload failed: {e!r}", file=sys.stderr)

    if _HAVE_SMARTTURN:
        try:
            importlib.reload(_smartturn)
            reloaded.append("wren_smartturn")
        except Exception as e:
            print(f"[reload_helpers] wren_smartturn reload failed: {e!r}", file=sys.stderr)

    try:
        importlib.reload(wren_pace)
        reloaded.append("wren_pace")
    except Exception as e:
        print(f"[reload_helpers] wren_pace reload failed: {e!r}", file=sys.stderr)

    return reloaded
