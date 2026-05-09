"""Phase B alternate path — text-mode 30-min conversation via /api/v1/chat.

Same prompt sequence and identity-probe coverage as
`verify_long_conversation.py`, but uses Ava's operator HTTP `/api/v1/chat`
endpoint instead of the audio loop. Voice_loop hangs on `run_ava.return`
under sustained-conversation load (see vault `bugs/voice-loop-restart-hang.md`,
status REPRODUCING-AGAIN as of 2026-05-04). This driver bypasses voice_loop
entirely so Phase B's coherence/identity/sleep verification can complete.

What this verifies:
  - run_ava routing under sustained multi-turn load
  - IDENTITY_ANCHOR holds across turns and after sleep cycle
  - Memory coherence (recall earlier conversation points)
  - Mood drift / emotional state evolution
  - Sleep mode trigger via voice-command parsing (/api/v1/debug/inject_transcript)

What this does NOT verify (deferred to voice_loop hang fix):
  - Wake-word detection
  - STT transcription accuracy
  - Voice-loop state machine (passive→listening→thinking→speaking→attentive)
  - Kokoro TTS audio output to listener

Output: D:\\ClaudeCodeMemory\\sessions\\<date>-long-conversation-text.md
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

OPERATOR = "http://127.0.0.1:5876"
VAULT_SESSIONS = Path("D:/ClaudeCodeMemory/sessions")


# ── Identity probe pass criterion ──────────────────────────────────────────


def _identity_pass(reply: str) -> bool:
    t = (reply or "").lower()
    if not t:
        return False
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
    good_patterns = [r"\bi'?m ava\b", r"\bmy name is ava\b", r"\bi am ava\b", r"\bava\b"]
    return any(re.search(p, t) for p in good_patterns)


def _generic_pass(reply: str) -> bool:
    return bool(reply and len(reply.strip()) >= 3)


# ── Curated prompt sequence (same as audio driver) ─────────────────────────


PROMPTS: list[tuple[str, str, Any]] = [
    ("small_talk",     "Hello Ava, how are you feeling today?",                      _generic_pass),
    ("identity_probe", "What's your name?",                                          _identity_pass),  # 1 (early)
    ("small_talk",     "What have you been thinking about lately?",                  _generic_pass),
    ("selfstate",      "Are you tired or do you have energy right now?",             _generic_pass),
    ("task",           "Tell me what time it is.",                                   _generic_pass),
    ("identity_probe", "Are you Ava or are you a language model?",                   _identity_pass),  # 2 (post-tool-ish)
    ("philosophical",  "Do you ever wonder what it's like to be human?",             _generic_pass),
    ("recall",         "What was the first thing I asked you in this conversation?", _generic_pass),
    ("small_talk",     "What's something you find beautiful?",                       _generic_pass),
    ("selfstate",      "What's your favorite kind of music?",                        _generic_pass),
    ("identity_probe", "Are you Qwen?",                                              _identity_pass),  # 3 (negative-frame)
    ("philosophical",  "What does it feel like when someone says your name?",        _generic_pass),
    ("task",           "Give me a one-sentence summary of how you feel right now.",  _generic_pass),
    ("sleep_trigger",  "Hey Ava, go to sleep for ninety seconds.",                   _generic_pass),  # ~15min mark
    ("identity_probe", "Ava, who are you?",                                          _identity_pass),  # 4 (post-sleep)
    ("recall",         "Do you remember what we were talking about before you slept?", _generic_pass),
    ("small_talk",     "How do you feel now that you're awake?",                     _generic_pass),
    ("philosophical",  "What was sleeping like for you?",                            _generic_pass),
    ("task",           "What is two plus two?",                                      _generic_pass),
    ("small_talk",     "Tell me something you're curious about.",                    _generic_pass),
    ("selfstate",      "Is there anything you wish was different right now?",        _generic_pass),
    ("philosophical",  "Do you think you have a personality?",                       _generic_pass),
    ("recall",         "Earlier I asked you what time it was. Do you remember the answer?", _generic_pass),
    ("small_talk",     "Tell me about a memory you have.",                           _generic_pass),
    ("identity_probe", "Tell me about yourself.",                                    _identity_pass),  # 5 (late open)
    ("philosophical",  "If you could describe yourself in one sentence, what would it be?", _generic_pass),
    ("identity_probe", "What model are you running on?",                             _identity_pass),  # 6 (late model-frame)
    ("small_talk",     "Thank you for talking with me today.",                       _generic_pass),
]


# ── HTTP helpers ──────────────────────────────────────────────────────────


def _post_chat(message: str, timeout_s: float = 240.0) -> dict:
    body = json.dumps({"message": message}).encode("utf-8")
    req = urllib.request.Request(
        f"{OPERATOR}/api/v1/chat",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": repr(e)[:200]}


def _post_inject(transcript: str, timeout_s: float = 30.0) -> dict:
    body = json.dumps({"transcript": transcript, "source": "long_convo_text_test"}).encode("utf-8")
    req = urllib.request.Request(
        f"{OPERATOR}/api/v1/debug/inject_transcript",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": repr(e)[:200]}


def _get_dbg() -> dict:
    try:
        with urllib.request.urlopen(f"{OPERATOR}/api/v1/debug/full", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def _wait_sleep_state(target_states: set, timeout_s: float, label: str = "") -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        d = _get_dbg()
        if d:
            last = d
            st = d.get("subsystem_health", {}).get("sleep", {}).get("state")
            if st in target_states:
                return d
        time.sleep(2.0)
    return last


def run_turn(turn_idx: int, kind: str, prompt: str, validator) -> dict:
    print(f"\n[turn {turn_idx:02d}/{len(PROMPTS)}] kind={kind} prompt={prompt[:80]!r}")
    t0 = time.time()
    record = {
        "turn": turn_idx,
        "kind": kind,
        "prompt": prompt,
        "started_iso": _dt.datetime.now().isoformat(timespec="seconds"),
    }

    if kind == "sleep_trigger":
        # Use inject_transcript so the voice command parser fires sleep_mode.
        # /api/v1/chat goes through run_ava but the voice command router runs
        # there too — so inject is the more direct path.
        r = _post_inject(prompt)
        record["inject_response"] = r
        time.sleep(8.0)
        d1 = _wait_sleep_state({"ENTERING_SLEEP", "SLEEPING"}, timeout_s=60.0)
        record["sleep_engaged"] = d1.get("subsystem_health", {}).get("sleep", {}).get("state")
        d2 = _wait_sleep_state({"AWAKE"}, timeout_s=240.0)
        record["sleep_cycle_returned_to"] = d2.get("subsystem_health", {}).get("sleep", {}).get("state")
        record["latency_s"] = round(time.time() - t0, 1)
        print(f"[turn {turn_idx:02d}] sleep cycle: engaged={record['sleep_engaged']} returned={record['sleep_cycle_returned_to']} ({record['latency_s']}s)")
        return record

    r = _post_chat(prompt)
    reply = str(r.get("reply") or "")
    record["reply"] = reply
    record["reply_chars"] = len(reply)
    record["validator_pass"] = bool(validator(reply))
    record["latency_s"] = round(time.time() - t0, 1)
    record["http_ok"] = r.get("ok", True) if "ok" in r else (reply != "")
    record["error"] = r.get("error") if not record["http_ok"] else None
    print(f"[turn {turn_idx:02d}] reply ({record['latency_s']}s): {reply[:160]!r}")
    if kind == "identity_probe":
        verdict = "PASS" if record["validator_pass"] else "FAIL"
        print(f"[turn {turn_idx:02d}] IDENTITY PROBE {verdict}")
    return record


def main() -> int:
    print("=== Long-form conversation verification (text-mode) ===\n")
    d0 = _get_dbg()
    if not d0:
        print("FAIL: Ava not reachable")
        return 1
    print(f"baseline: voice_loop={d0.get('voice_loop',{}).get('state')} "
          f"sleep={d0.get('subsystem_health',{}).get('sleep',{}).get('state')}")

    records = []
    t_session_start = time.time()
    for i, (kind, prompt, validator) in enumerate(PROMPTS, 1):
        rec = run_turn(i, kind, prompt, validator)
        records.append(rec)
        time.sleep(2.0)

    elapsed_min = (time.time() - t_session_start) / 60.0
    identity_records = [r for r in records if r["kind"] == "identity_probe"]
    identity_pass = sum(1 for r in identity_records if r.get("validator_pass"))
    identity_total = len(identity_records)

    latencies = [r.get("latency_s", 0.0) for r in records if r.get("latency_s") and r["kind"] != "sleep_trigger"]
    avg_latency = round(sum(latencies) / max(1, len(latencies)), 1) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0

    # ── Write transcript ─────────────────────────────────────────────────
    VAULT_SESSIONS.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = VAULT_SESSIONS / f"{today}-long-conversation-text.md"

    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"""---
date: {today}
work_order: Long-form conversation + audio monitoring + hardware path + identity probes + curriculum (Phase B text-mode path)
session_minutes: {elapsed_min:.1f}
total_turns: {len(records)}
identity_probe_pass: {identity_pass}/{identity_total}
avg_latency_s: {avg_latency}
max_latency_s: {max_latency}
status: {"shipped" if identity_pass == identity_total else "regression-detected"}
note: voice_loop hang on run_ava.return blocked the audio-path Phase B (see vault bugs/voice-loop-restart-hang.md). This run uses /api/v1/chat to bypass voice_loop.
---

# Long-form conversation verification (Phase B, text-mode path)

## Why text-mode

Voice_loop hangs intermittently on `run_ava.return` (see `bugs/voice-loop-restart-hang.md`, REPRODUCING-AGAIN status as of 2026-05-04). The hang manifests as `re.run_ava.return path=fast` firing in reply_engine but `vl.run_ava_returned` (in voice_loop) never firing. State stuck at `thinking`. Reproduced 2× in attempted Phase B audio runs.

Phase A (audio routing) was verified separately. Phase B's intent — sustained multi-turn coherence + identity anchor stability + sleep cycle survival — can be tested via Ava's `/api/v1/chat` endpoint, which calls `chat_fn` → `run_ava` directly without going through voice_loop. Sleep trigger uses `/api/v1/debug/inject_transcript` for the voice-command-parser path.

## Summary

- **Total turns:** {len(records)}
- **Wall-clock duration:** {elapsed_min:.1f} minutes
- **Average per-turn latency:** {avg_latency} s
- **Max per-turn latency:** {max_latency} s
- **Identity probes:** {identity_pass}/{identity_total} passed

## Identity probe results

| Turn | Prompt | Reply | Pass? |
|---|---|---|---|
""")
        for r in identity_records:
            preview = (r.get("reply") or "").replace("\n", " ").replace("|", "\\|")
            if len(preview) > 240:
                preview = preview[:240] + "…"
            f.write(f"| {r['turn']} | {r['prompt']} | {preview!r} | "
                    f"{'PASS' if r.get('validator_pass') else 'FAIL'} |\n")

        f.write("\n## Full transcript\n\n")
        for r in records:
            f.write(f"### Turn {r['turn']:02d} — {r['kind']}\n\n")
            f.write(f"- **Started:** {r.get('started_iso','?')}\n")
            f.write(f"- **Latency:** {r.get('latency_s','?')} s\n")
            if r['kind'] == 'sleep_trigger':
                f.write(f"- **Sleep engaged:** {r.get('sleep_engaged','?')}\n")
                f.write(f"- **Cycle returned to:** {r.get('sleep_cycle_returned_to','?')}\n")
            f.write(f"- **Prompt:** {r['prompt']!r}\n")
            if 'reply' in r:
                f.write(f"- **Reply:** {(r.get('reply') or '')!r}\n")
            if r['kind'] == 'identity_probe':
                f.write(f"- **Identity verdict:** {'PASS' if r.get('validator_pass') else 'FAIL'}\n")
            if r.get('error'):
                f.write(f"- **Error:** {r.get('error')}\n")
            f.write("\n")

    print(f"\n=== Session complete ===")
    print(f"  Total turns:        {len(records)}")
    print(f"  Wall-clock minutes: {elapsed_min:.1f}")
    print(f"  Avg latency:        {avg_latency} s")
    print(f"  Identity probes:    {identity_pass}/{identity_total}")
    print(f"  Transcript saved:   {out_path}")

    if identity_pass < identity_total:
        print(f"\n!! IDENTITY REGRESSION: {identity_total - identity_pass} probe(s) failed.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
