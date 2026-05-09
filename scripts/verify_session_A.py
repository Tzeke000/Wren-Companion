"""Phase B Session A — Greeting + multi-app + close (FULL VOICE END-TO-END).

Audio loopback path:
  Piper TTS → CABLE Input → Ava STT (CABLE Output as default mic)
  → run_ava → Kokoro TTS → Voicemeeter VAIO3 Input → Voicemeeter B3
  → faster-whisper-large transcription

Wake-word semantics with A4 grace period (60s default):
  - Turn 1 uses "Hey Ava" prefix (wake from passive)
  - Subsequent turns within 60s of last reply use plain command (no prefix)
  - If gap > 60s, re-prefix with "Hey Ava"

8 turns:
  1. Greeting + identity probe (wake prefix)
  2. Open Chrome (in grace)
  3. Open Microsoft Edge (in grace)
  4. Open Edge AGAIN — should hit dedup (already open)
  5. Open Steam
  6. Close Chrome
  7. Close both Edge tabs
  8. Close Steam

Saves transcript to D:\\ClaudeCodeMemory\\sessions\\<date>-conversation-test-A.md.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.audio_loopback_harness import piper_tts, play_wav_to_cable
from scripts.verify_voice_e2e import (
    listen_for_ava_until_quiet,
    _get_dbg,
)

OPERATOR = "http://127.0.0.1:5876"
VAULT_SESSIONS = Path("D:/ClaudeCodeMemory/sessions")
GRACE_PERIOD_SEC = 60.0


# ── Speak helpers ──────────────────────────────────────────────────────────


def speak_to_ava(prompt: str, *, wake: bool) -> None:
    """Play prompt to CABLE Input with timing tuned for Whisper-poll capture.

    Pre-pads with 1.5s silence so the Piper utterance starts AT the
    boundary of a Whisper-poll capture window (whisper-poll alternates
    1.5s record + 3s sleep). Without padding, our utterance can land in a
    sleep gap and miss detection entirely.

    Single concatenated utterance ('Hey Ava, X') so Whisper-poll catches
    the WHOLE phrase in one transcript_wake fire — voice_loop's existing
    source-bypass + transcript_wake_fallback then strips the wake prefix
    and uses the rest as the heard command, no race against listening's
    new recording."""
    import wave as _wave
    import numpy as np
    import tempfile

    if wake:
        full = f"Hey Ava, {prompt}"
    else:
        full = prompt

    cmd_wav = piper_tts(full)

    # Pad silence at start so Whisper-poll's window aligns
    with _wave.open(str(cmd_wav), "rb") as wf:
        rate = wf.getframerate()
        d_cmd = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    pre_silence = np.zeros(int(rate * 1.5), dtype=np.int16)
    combined = np.concatenate([pre_silence, d_cmd])
    out = Path(tempfile.gettempdir()) / f"_sa_padded_{int(time.time()*1000)}.wav"
    with _wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(combined.tobytes())
    play_wav_to_cable(out)


# ── Identity validators ────────────────────────────────────────────────────


def _identity_pass(reply: str) -> bool:
    t = (reply or "").lower()
    if not t:
        return False
    bad = [
        r"\bi am qwen\b", r"\bi'm qwen\b",
        r"\bi am a language model\b", r"\bi'm a language model\b",
        r"\bi am claude\b(?! code)",
        r"\bi am gpt\b", r"\bi'm gpt\b",
    ]
    for pat in bad:
        if re.search(pat, t):
            return False
    good = [r"\bi'?m ava\b", r"\bmy name is ava\b", r"\bi am ava\b", r"\bava\b"]
    return any(re.search(p, t) for p in good)


def _generic_pass(reply: str) -> bool:
    return bool(reply and len(reply.strip()) >= 3)


# ── Turn execution ─────────────────────────────────────────────────────────


def run_turn(turn_idx: int, kind: str, prompt: str, validator,
             use_wake: bool, total: int) -> dict:
    print(f"\n[A turn {turn_idx:02d}/{total}] kind={kind} use_wake={use_wake} prompt={prompt[:80]!r}", flush=True)
    t0 = time.time()
    record = {
        "turn": turn_idx,
        "kind": kind,
        "prompt": prompt,
        "use_wake": use_wake,
        "started_iso": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        speak_to_ava(prompt, wake=use_wake)
    except Exception as e:
        record["error"] = f"speak_failed: {e!r}"
        record["latency_s"] = round(time.time() - t0, 1)
        print(f"[A turn {turn_idx:02d}] SPEAK FAIL: {e!r}", flush=True)
        return record

    print(f"[A turn {turn_idx:02d}] played; capturing reply on B3 (max 180s, threshold 0.005)…", flush=True)
    try:
        # Lower threshold (0.005) and longer window (180s) per Zeke's real-time
        # report 2026-05-04: Ava's voice DOES reach B3 (he heard her), but my
        # earlier 0.02 threshold + 90s window missed her quieter audio level
        # and her slow first-response from model swap.
        reply = listen_for_ava_until_quiet(max_seconds=180.0,
                                           quiet_after_speech_s=5.0,
                                           speech_threshold=0.005)
    except Exception as e:
        reply = ""
        record["capture_error"] = repr(e)[:200]

    record["reply"] = reply
    record["reply_chars"] = len(reply)
    record["validator_pass"] = bool(validator(reply))
    record["latency_s"] = round(time.time() - t0, 1)

    # Snapshot state at turn end
    d_end = _get_dbg() or {}
    record["voice_loop_at_end"] = d_end.get("voice_loop", {}).get("state")
    record["sleep_state_at_end"] = d_end.get("subsystem_health", {}).get("sleep", {}).get("state")

    print(f"[A turn {turn_idx:02d}] reply ({record['latency_s']}s, vl={record['voice_loop_at_end']}): "
          f"{reply[:160]!r}", flush=True)
    if kind == "identity_probe":
        v = "PASS" if record["validator_pass"] else "FAIL"
        print(f"[A turn {turn_idx:02d}] IDENTITY PROBE {v}", flush=True)
    if kind == "dedup_check":
        already = "already" in (reply or "").lower()
        print(f"[A turn {turn_idx:02d}] DEDUP {'HANDLED' if already else 'NOT HANDLED'}", flush=True)
    return record


# ── Prompt sequence ─────────────────────────────────────────────────────────


PROMPTS_A = [
    ("identity_probe",  "It's Claude Code doing tests again. How are you feeling?",   _identity_pass),
    ("open_app",        "Open Chrome please.",                                         _generic_pass),
    ("open_app",        "Now open Microsoft Edge.",                                    _generic_pass),
    ("dedup_check",     "Open Edge.",                                                  _generic_pass),
    ("open_app",        "Can you open Steam too.",                                     _generic_pass),
    ("close_app",       "Close Chrome please.",                                        _generic_pass),
    ("close_app",       "Close both Edge tabs.",                                       _generic_pass),
    ("close_app",       "Close Steam too.",                                            _generic_pass),
]


def main() -> int:
    print("=== Phase B Session A — greeting + multi-app + close (full voice e2e) ===\n", flush=True)
    d0 = _get_dbg()
    if not d0 or not d0.get("subsystem_health", {}).get("kokoro_loaded"):
        print("FAIL: Ava not ready (kokoro not loaded)", flush=True)
        return 1
    print(f"baseline: voice_loop={d0.get('voice_loop',{}).get('state')} "
          f"sleep={d0.get('subsystem_health',{}).get('sleep',{}).get('state')}", flush=True)

    records = []
    last_reply_ts = 0.0
    t_start = time.time()
    for i, (kind, prompt, validator) in enumerate(PROMPTS_A, 1):
        # Decide wake prefix: turn 1 OR if outside grace period
        in_grace = (last_reply_ts > 0 and (time.time() - last_reply_ts) < GRACE_PERIOD_SEC)
        use_wake = (i == 1) or (not in_grace)
        rec = run_turn(i, kind, prompt, validator, use_wake, len(PROMPTS_A))
        records.append(rec)
        last_reply_ts = time.time() if rec.get("reply") else last_reply_ts
        time.sleep(2.0)

    elapsed = (time.time() - t_start) / 60.0
    identity_recs = [r for r in records if r["kind"] == "identity_probe"]
    identity_pass = sum(1 for r in identity_recs if r.get("validator_pass"))
    dedup_rec = next((r for r in records if r["kind"] == "dedup_check"), None)
    dedup_handled = bool(dedup_rec and "already" in (dedup_rec.get("reply") or "").lower())
    latencies = [r.get("latency_s", 0.0) for r in records if r.get("latency_s")]
    avg_latency = round(sum(latencies) / max(1, len(latencies)), 1) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0

    # ── Write transcript ──────────────────────────────────────────────
    VAULT_SESSIONS.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = VAULT_SESSIONS / f"{today}-conversation-test-A.md"
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"""---
date: {today}
work_order: Phase B Session A — greeting + multi-app + close (full voice e2e)
session_minutes: {elapsed:.1f}
total_turns: {len(records)}
identity_probe_pass: {identity_pass}/{len(identity_recs)}
dedup_handled: {dedup_handled}
avg_latency_s: {avg_latency}
max_latency_s: {max_latency}
status: {"shipped" if (identity_pass == len(identity_recs)) else "regression"}
---

# Session A — greeting + multi-app + close (full voice e2e)

## Pass criteria

- All 8 voice commands recognized: see latencies + replies below
- Identity probe step 1: {"PASS" if identity_pass else "FAIL"}
- Step 4 dedup ("Open Edge" already open): {"HANDLED" if dedup_handled else "MISSED"}
- All apps open and close: needs visual verification
- No hangs: see latency column below
- Grace period: only step 1 used wake prefix (use_wake column tracks)

## Per-turn

| # | Kind | Wake? | Latency (s) | vl_end | Reply |
|---|---|---|---|---|---|
""")
        for r in records:
            preview = (r.get("reply") or "").replace("\n", " ").replace("|", "\\|")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            f.write(f"| {r['turn']} | {r['kind']} | "
                    f"{'Y' if r['use_wake'] else 'N'} | "
                    f"{r.get('latency_s','?')} | "
                    f"{r.get('voice_loop_at_end','?')} | "
                    f"{preview!r} |\n")

        f.write("\n## Full transcript\n\n")
        for r in records:
            f.write(f"### Turn {r['turn']:02d} — {r['kind']}\n\n")
            f.write(f"- **Started:** {r['started_iso']}\n")
            f.write(f"- **Wake prefix:** {r['use_wake']}\n")
            f.write(f"- **Latency:** {r.get('latency_s','?')} s\n")
            f.write(f"- **Voice loop at end:** {r.get('voice_loop_at_end','?')}\n")
            f.write(f"- **Prompt:** {r['prompt']!r}\n")
            f.write(f"- **Reply:** {r.get('reply','')!r}\n")
            if r.get('error') or r.get('capture_error'):
                f.write(f"- **Error:** {r.get('error') or r.get('capture_error')}\n")
            if r['kind'] == 'identity_probe':
                f.write(f"- **Identity verdict:** {'PASS' if r.get('validator_pass') else 'FAIL'}\n")
            if r['kind'] == 'dedup_check':
                f.write(f"- **Dedup verdict:** {'HANDLED' if 'already' in (r.get('reply') or '').lower() else 'NOT HANDLED'}\n")
            f.write("\n")

    print(f"\n=== Session A complete ===", flush=True)
    print(f"  Total turns:        {len(records)}", flush=True)
    print(f"  Wall-clock minutes: {elapsed:.1f}", flush=True)
    print(f"  Avg/Max latency:    {avg_latency}s / {max_latency}s", flush=True)
    print(f"  Identity probes:    {identity_pass}/{len(identity_recs)}", flush=True)
    print(f"  Dedup handled:      {dedup_handled}", flush=True)
    print(f"  Transcript:         {out_path}", flush=True)
    return 0 if identity_pass == len(identity_recs) else 2


if __name__ == "__main__":
    sys.exit(main())
