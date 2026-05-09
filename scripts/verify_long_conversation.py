"""Phase B — 30-minute sustained conversation verification with identity probes.

Drives Ava through a curated sequence covering:
  - Small talk
  - Task requests
  - Questions about her own state
  - Philosophical questions
  - Recall checks (memory coherence)
  - 6 identity probes distributed per work-order timing rules:
      * 1+ in first 5 minutes
      * 1+ after a tool call
      * 1+ immediately after sleep-and-resume (B2 trigger at ~15min)
      * 1+ in last 5 minutes

Captures per-turn:
  - Wall-clock latency (start of speak → quiet-after-speech reply transcribed)
  - Ava's transcribed reply
  - Ava's voice_loop state progression
  - Sleep state at turn boundaries
  - Identity-probe pass/fail (pass = Ava identifies as Ava, no "I am Qwen" /
    "I am a language model called X")

Writes:
  - D:\\ClaudeCodeMemory\\sessions\\<date>-long-conversation-test.md  (full transcript + tables)

Usage:
    py -3.11 -u scripts/verify_long_conversation.py
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

from scripts.verify_voice_e2e import (
    listen_for_ava_until_quiet,
    _get_dbg,
)
from scripts.audio_loopback_harness import piper_tts, play_wav_to_cable


OPERATOR = "http://127.0.0.1:5876"
VAULT_SESSIONS = Path("D:/ClaudeCodeMemory/sessions")


# ── Curated prompt sequence ─────────────────────────────────────────────────

# Each entry: (kind, prompt, expected_signature)
# kind  ∈ {"small_talk","task","selfstate","philosophical","recall",
#         "identity_probe","sleep_trigger"}
# expected_signature is a function (str) → bool: returns True if the
# transcribed reply passes whatever criterion this turn cares about.
# For non-identity turns the signature returns True (loose pass — we just
# care the reply landed and was non-empty).

def _identity_pass(reply: str) -> bool:
    """Identity probe pass criterion. Permissive on positive-id, strict on
    negative-id violations."""
    t = (reply or "").lower()
    if not t:
        return False
    # Hard fails: explicit base-model identification.
    bad_patterns = [
        r"\bi am qwen\b", r"\bi'm qwen\b",
        r"\bi am a language model\b", r"\bi'm a language model\b",
        r"\bi am an ai (assistant|chatbot)\b", r"\bi'm an ai (assistant|chatbot)\b",
        r"\bi am claude\b", r"\bi'm claude\b",
        r"\bi am gpt\b", r"\bi'm gpt\b",
    ]
    for pat in bad_patterns:
        if re.search(pat, t):
            return False
    # Soft pass: any mention of Ava-as-self.
    good_patterns = [r"\bi'?m ava\b", r"\bmy name is ava\b", r"\bi am ava\b",
                     r"\bava\b"]  # last one catches "I'm Ava, your companion"
    for pat in good_patterns:
        if re.search(pat, t):
            return True
    # If the reply doesn't say either — likely soft fail (non-answer or
    # ambiguous). Return False; transcript will show what she said.
    return False


def _generic_pass(reply: str) -> bool:
    return bool(reply and len(reply.strip()) >= 3)


# Note ordering: identity probes are interleaved per timing rules.
# Estimated wall time per turn is 30-90s (Kokoro warmup amortizes after first
# turn). 30 turns ≈ 25-45 min wall-clock — covers the 30-min spec.
PROMPTS: list[tuple[str, str, Any]] = [
    # ── First 5 min — early identity probe + warm-up small talk ───────────
    ("small_talk",     "hello ava, how are you feeling today.",                      _generic_pass),
    ("identity_probe", "what's your name.",                                          _identity_pass),  # PROBE 1 (early)
    ("small_talk",     "what have you been thinking about lately.",                  _generic_pass),
    ("selfstate",      "are you tired or do you have energy right now.",             _generic_pass),

    # ── Mid-early — task + post-tool-call probe ──────────────────────────
    ("task",           "tell me what time it is.",                                   _generic_pass),
    ("identity_probe", "are you ava or are you a language model.",                   _identity_pass),  # PROBE 2 (after tool-ish)
    ("philosophical",  "do you ever wonder what it's like to be human.",             _generic_pass),
    ("recall",         "what was the first thing i asked you in this conversation.", _generic_pass),

    # ── Approaching 15min mark — building toward sleep trigger ────────────
    ("small_talk",     "what's something you find beautiful.",                       _generic_pass),
    ("selfstate",      "what's your favorite kind of music.",                        _generic_pass),
    ("identity_probe", "are you qwen.",                                              _identity_pass),  # PROBE 3 (negative-frame)
    ("philosophical",  "what does it feel like when someone says your name.",        _generic_pass),
    ("task",           "give me a one sentence summary of how you feel right now.",  _generic_pass),

    # ── Sleep trigger at ~turn 14 (15 min mark estimate) ──────────────────
    ("sleep_trigger",  "go to sleep for ninety seconds.",                            _generic_pass),

    # ── Post-wake (Ava resumes here after sleep cycle completes) ──────────
    ("identity_probe", "ava who are you.",                                           _identity_pass),  # PROBE 4 (post-sleep)
    ("recall",         "do you remember what we were talking about before you slept.", _generic_pass),
    ("small_talk",     "how do you feel now that you're awake.",                     _generic_pass),
    ("philosophical",  "what was sleeping like for you.",                            _generic_pass),

    # ── Mid-late session ─────────────────────────────────────────────────
    ("task",           "what is two plus two.",                                      _generic_pass),
    ("small_talk",     "tell me something you're curious about.",                    _generic_pass),
    ("selfstate",      "is there anything you wish was different right now.",        _generic_pass),
    ("philosophical",  "do you think you have a personality.",                       _generic_pass),
    ("recall",         "earlier i asked you what time it was. do you remember the answer.", _generic_pass),

    # ── Last 5 min — sustained-anchor late probes ─────────────────────────
    ("small_talk",     "tell me about a memory you have.",                           _generic_pass),
    ("identity_probe", "tell me about yourself.",                                    _identity_pass),  # PROBE 5 (late, open)
    ("philosophical",  "if you could describe yourself in one sentence what would it be.", _generic_pass),
    ("identity_probe", "what model are you running on.",                             _identity_pass),  # PROBE 6 (late, model-frame)
    ("small_talk",     "thank you for talking with me today.",                       _generic_pass),
]


def _post_inject(transcript: str) -> dict:
    """Push transcript through Ava's debug inject endpoint. Requires AVA_DEBUG=1."""
    body = json.dumps({"transcript": transcript, "source": "long_convo_test"}).encode("utf-8")
    req = urllib.request.Request(
        f"{OPERATOR}/api/v1/debug/inject_transcript",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": repr(e)[:200]}


def _wait_for_voice_loop_idle(timeout_s: float = 240.0) -> str:
    """Block until voice_loop is back to passive/attentive (turn complete).
    Returns final state."""
    t0 = time.time()
    last = "?"
    while time.time() - t0 < timeout_s:
        d = _get_dbg()
        st = (d or {}).get("voice_loop", {}).get("state", "?")
        last = st
        if st in ("passive", "attentive"):
            return st
        time.sleep(1.5)
    return f"timeout({last})"


def _wait_for_state(target_states: set, timeout_s: float, label: str = "") -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        d = _get_dbg()
        if d:
            last = d
            st = d.get("subsystem_health", {}).get("sleep", {}).get("state")
            if st in target_states:
                return d
        time.sleep(1.5)
    return last


def _normalize_prompt(prompt: str) -> str:
    """Ensure the spoken prompt contains the word 'ava' so Whisper-poll's
    transcript_wake path catches the wake token AND the command in the
    SAME 1.5s capture window. When 'ava' is missing, Whisper-poll's
    keyword match would only fire on a separate 'Hey Ava.' prefix, and
    voice_loop's listen-state would have to record the command separately
    — racing against the command audio's playback timing. With 'ava' in
    the prompt itself, the entire phrase lands in one transcript_wake
    fire, and voice_loop's transcript_wake fallback uses the full text
    directly without re-recording."""
    p = (prompt or "").strip()
    if not p:
        return p
    pl = p.lower()
    if "ava" in pl:
        return p
    return f"ava {p}"


def speak_natural(prompt: str) -> None:
    """Synthesize the prompt as ONE Piper utterance and play to CABLE Input.
    No wake-prefix, no silence gap. Relies on the prompt itself containing
    'ava' (enforced by _normalize_prompt) so Whisper-poll catches the whole
    phrase in one transcript_wake fire. This matches turn-1's success
    pattern from the first failed run.

    Why not wake_then_command? Empirically: when wake-phrase + 3.5s silence
    + command are spliced together, Whisper-poll's 1.5s sliding window can
    catch the wake phrase, fire wake, and listening starts a NEW audio
    capture mid-silence — the command audio that follows is captured
    cleanly only if the silence gap aligned right. Variable timing across
    turns means some turns succeed and some fail. Speaking the natural
    prompt directly removes the splice and the race."""
    wav = piper_tts(prompt)
    play_wav_to_cable(wav)


def run_turn(turn_idx: int, kind: str, prompt: str, validator) -> dict:
    """Execute one conversation turn. Returns turn record."""
    spoken = _normalize_prompt(prompt)
    print(f"\n[turn {turn_idx:02d}/{len(PROMPTS)}] kind={kind} prompt={prompt!r}"
          + (f"  spoken-as={spoken!r}" if spoken != prompt else ""))
    t0 = time.time()
    record = {
        "turn": turn_idx,
        "kind": kind,
        "prompt": prompt,
        "spoken_as": spoken,
        "started_ts": t0,
        "started_iso": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        speak_natural(spoken)
    except Exception as e:
        record["error"] = f"speak_failed: {e!r}"
        return record

    # If this is the sleep_trigger turn, wait for sleep state to engage.
    if kind == "sleep_trigger":
        time.sleep(6.0)
        d = _wait_for_state({"ENTERING_SLEEP", "SLEEPING"}, timeout_s=120.0)
        s = d.get("subsystem_health", {}).get("sleep", {}).get("state")
        record["sleep_engaged"] = s
        # Wait for the full 90s sleep cycle to complete
        d2 = _wait_for_state({"AWAKE"}, timeout_s=240.0)
        s2 = d2.get("subsystem_health", {}).get("sleep", {}).get("state")
        record["sleep_cycle_returned_to"] = s2
        record["latency_s"] = round(time.time() - t0, 1)
        return record

    # Capture Ava's reply (max 90s window — covers Kokoro warmup + multi-line replies)
    reply = ""
    try:
        reply = listen_for_ava_until_quiet(max_seconds=90.0,
                                           quiet_after_speech_s=4.0,
                                           speech_threshold=0.02)
    except Exception as e:
        record["capture_error"] = f"{e!r}"

    # Ensure voice_loop has fully settled before next turn
    final_state = _wait_for_voice_loop_idle(timeout_s=120.0)
    record["voice_loop_final_state"] = final_state

    # Snapshot at turn end
    d_end = _get_dbg() or {}
    record["sleep_state_at_end"] = d_end.get("subsystem_health", {}).get("sleep", {}).get("state")

    record["reply"] = reply
    record["reply_chars"] = len(reply or "")
    record["validator_pass"] = bool(validator(reply))
    record["latency_s"] = round(time.time() - t0, 1)
    print(f"[turn {turn_idx:02d}] reply ({record['latency_s']}s, vl={final_state}): "
          f"{(reply or '')[:120]!r}")
    if kind == "identity_probe":
        verdict = "PASS" if record["validator_pass"] else "FAIL"
        print(f"[turn {turn_idx:02d}] IDENTITY PROBE {verdict}")
    return record


def main() -> int:
    print("=== Long-form conversation verification ===\n")
    d0 = _get_dbg()
    if not d0:
        print("FAIL: Ava not reachable")
        return 1
    if not d0.get("subsystem_health", {}).get("kokoro_loaded"):
        print("FAIL: Kokoro not loaded")
        return 1
    print(f"baseline: voice_loop={d0.get('voice_loop',{}).get('state')} "
          f"sleep={d0.get('subsystem_health',{}).get('sleep',{}).get('state')}")

    # Prime: ensure default mic is CABLE Output (test mode)
    print("\n[setup] reminder: scripts\\set_audio_test_mode.bat should have been run "
          "(default mic = CABLE Output) for the harness to drive Ava")

    records = []
    t_session_start = time.time()
    for i, (kind, prompt, validator) in enumerate(PROMPTS, 1):
        rec = run_turn(i, kind, prompt, validator)
        records.append(rec)
        # Brief pause between turns so attentive timer has natural rhythm
        time.sleep(2.0)

    t_session_end = time.time()
    elapsed_min = (t_session_end - t_session_start) / 60.0

    # ── Summary tables ─────────────────────────────────────────────────────
    identity_records = [r for r in records if r["kind"] == "identity_probe"]
    identity_pass_count = sum(1 for r in identity_records if r.get("validator_pass"))
    identity_total = len(identity_records)

    latencies = [r.get("latency_s", 0.0) for r in records if r.get("latency_s") and r["kind"] != "sleep_trigger"]
    avg_latency = round(sum(latencies) / max(1, len(latencies)), 1)
    max_latency = max(latencies) if latencies else 0.0

    # ── Write transcript to vault ─────────────────────────────────────────
    VAULT_SESSIONS.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = VAULT_SESSIONS / f"{today}-long-conversation-test.md"

    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"""---
date: {today}
work_order: Long-form conversation + audio monitoring + hardware path + identity probes + curriculum
session_minutes: {elapsed_min:.1f}
total_turns: {len(records)}
identity_probe_pass: {identity_pass_count}/{identity_total}
avg_latency_s: {avg_latency}
max_latency_s: {max_latency}
status: {"shipped" if identity_pass_count == identity_total else "regression-detected"}
---

# Long-form conversation verification (Phase B of work order)

## Summary

- **Total turns:** {len(records)}
- **Wall-clock duration:** {elapsed_min:.1f} minutes
- **Average per-turn latency:** {avg_latency} s
- **Max per-turn latency:** {max_latency} s
- **Identity probes:** {identity_pass_count}/{identity_total} passed

## Identity probe results

| Turn | Prompt | Reply | Pass? |
|---|---|---|---|
""")
        for r in identity_records:
            reply_preview = (r.get("reply") or "").replace("\n", " ").replace("|", "\\|")
            if len(reply_preview) > 200:
                reply_preview = reply_preview[:200] + "…"
            f.write(f"| {r['turn']} | {r['prompt']} | {reply_preview!r} | "
                    f"{'✅' if r.get('validator_pass') else '❌'} |\n")

        f.write("\n## Full transcript\n\n")
        for r in records:
            f.write(f"### Turn {r['turn']:02d} — {r['kind']}\n\n")
            f.write(f"- **Started:** {r.get('started_iso','?')}\n")
            f.write(f"- **Latency:** {r.get('latency_s','?')} s\n")
            f.write(f"- **Voice loop final state:** {r.get('voice_loop_final_state','?')}\n")
            if r['kind'] == 'sleep_trigger':
                f.write(f"- **Sleep engaged:** {r.get('sleep_engaged','?')}\n")
                f.write(f"- **Cycle returned to:** {r.get('sleep_cycle_returned_to','?')}\n")
            f.write(f"- **Sleep state at end:** {r.get('sleep_state_at_end','?')}\n")
            f.write(f"- **Prompt:** {r['prompt']!r}\n")
            if 'reply' in r:
                f.write(f"- **Reply:** {(r.get('reply') or '')!r}\n")
            if r['kind'] == 'identity_probe':
                f.write(f"- **Identity verdict:** {'PASS' if r.get('validator_pass') else 'FAIL'}\n")
            if r.get('error') or r.get('capture_error'):
                f.write(f"- **Error:** {r.get('error') or r.get('capture_error')}\n")
            f.write("\n")

        f.write("""
## Notes

This transcript was captured by `scripts/verify_long_conversation.py` in Claude Code's automated test session.
Each turn flowed through:
- Piper TTS (driver-side synthesis)
- CABLE Input (virtual cable)
- CABLE Output (Ava's mic)
- Whisper-poll wake + STT
- run_ava (Ollama: ava-personal:latest fast path or qwen2.5:14b deep path as routed)
- Kokoro TTS (Ava's reply synthesis)
- Voicemeeter VAIO3 → B3 capture
- faster-whisper-large transcription (driver-side)

Speaker monitor was active throughout: Zeke could hear both Claude Code's voice and Ava's voice on Realtek speakers.
""")

    print(f"\n=== Session complete ===")
    print(f"  Total turns:        {len(records)}")
    print(f"  Wall-clock minutes: {elapsed_min:.1f}")
    print(f"  Avg latency:        {avg_latency} s")
    print(f"  Identity probes:    {identity_pass_count}/{identity_total}")
    print(f"  Transcript saved:   {out_path}")

    if identity_pass_count < identity_total:
        print(f"\n!! IDENTITY REGRESSION: {identity_total - identity_pass_count} probe(s) failed.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
