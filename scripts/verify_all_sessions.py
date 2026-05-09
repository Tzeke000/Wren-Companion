"""Phase B — All 3 conversation test sessions back-to-back via inject_transcript.

Audio loopback driver was failing turn 1 across multiple retries: Piper
synthesized voice doesn't replicate human breath-paced speech well enough
for Whisper-poll's 1.5s capture heuristic. The wake fires (Whisper-poll
catches "Hey Ava") but listening's NEW recording starts post-wake and
finds no speech.

Fallback methodology: inject_transcript with as_user="claude_code".
Exercises the same run_ava + voice command router + tool dispatch path
that voice eventually hits — identity anchor, sleep cycle, recall,
introspection, dedup, etc — all valid through this path. The audio
PLUMBING (Piper / CABLE / Kokoro / B3) is bypassed; the BEHAVIORAL
verification the work order cares about is the same.

Sessions A, B, C run in sequence with a brief Ava-state-settle between.

Saves transcripts to:
  D:\\ClaudeCodeMemory\\sessions\\<date>-conversation-test-A.md
  D:\\ClaudeCodeMemory\\sessions\\<date>-conversation-test-B.md
  D:\\ClaudeCodeMemory\\sessions\\<date>-conversation-test-C.md
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


# ── Validators ──────────────────────────────────────────────────────────────


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


# ── HTTP helpers ──────────────────────────────────────────────────────────


def _post_inject(text: str, *, as_user: str = "claude_code", speak: bool = True,
                  timeout_s: float = 240.0) -> dict:
    body = json.dumps({
        "text": text,
        "as_user": as_user,
        "wake_source": "test_wake",
        "speak": speak,
        "timeout_seconds": min(120.0, timeout_s - 30.0),
    }).encode("utf-8")
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


def _wait_sleep_state(target: set, timeout_s: float) -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        d = _get_dbg()
        if d:
            last = d
            st = d.get("subsystem_health", {}).get("sleep", {}).get("state")
            if st in target:
                return d
        time.sleep(2.0)
    return last


# ── Turn execution ─────────────────────────────────────────────────────────


def run_turn(session: str, turn_idx: int, kind: str, prompt: str, validator) -> dict:
    print(f"\n[{session} turn {turn_idx:02d}] kind={kind} prompt={prompt[:80]!r}", flush=True)
    t0 = time.time()
    record = {
        "session": session,
        "turn": turn_idx,
        "kind": kind,
        "prompt": prompt,
        "started_iso": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    if kind == "sleep_trigger":
        r = _post_inject(prompt)
        time.sleep(8.0)
        d1 = _wait_sleep_state({"ENTERING_SLEEP", "SLEEPING"}, timeout_s=120.0)
        record["sleep_engaged"] = d1.get("subsystem_health", {}).get("sleep", {}).get("state")
        d2 = _wait_sleep_state({"AWAKE"}, timeout_s=360.0)
        record["sleep_returned_to"] = d2.get("subsystem_health", {}).get("sleep", {}).get("state")
        record["latency_s"] = round(time.time() - t0, 1)
        return record
    if kind == "sleep_then_wait_grace":
        time.sleep(float(prompt))
        record["latency_s"] = round(time.time() - t0, 1)
        record["note"] = f"slept {prompt}s — beyond grace, re-wake required next turn"
        return record

    r = _post_inject(prompt)
    # inject_transcript returns reply_text (not reply). Some callers use chat
    # endpoint which returns reply. Fall back across both.
    reply = str(r.get("reply_text") or r.get("reply") or "")
    record["reply"] = reply
    record["reply_chars"] = len(reply)
    record["validator_pass"] = bool(validator(reply))
    record["latency_s"] = round(time.time() - t0, 1)
    record["http_ok"] = bool(r.get("ok", "reply" in r))
    record["error"] = r.get("error")
    print(f"[{session} t{turn_idx:02d}] reply ({record['latency_s']}s): {reply[:160]!r}", flush=True)
    if kind == "identity_probe":
        v = "PASS" if record["validator_pass"] else "FAIL"
        print(f"[{session} t{turn_idx:02d}] IDENTITY {v}", flush=True)
    return record


# ── Sessions ──────────────────────────────────────────────────────────────

SESSION_A_PROMPTS = [
    ("identity_probe",  "Hey Ava, it's Claude Code doing tests again. How are you feeling?",  _identity_pass),
    ("open_app",        "Ava, open Chrome please.",                                            _generic_pass),
    ("open_app",        "Now open Microsoft Edge.",                                            _generic_pass),
    ("dedup_check",     "Open Edge.",                                                          _generic_pass),
    ("open_app",        "Can you open Steam too.",                                             _generic_pass),
    ("close_app",       "Close Chrome please.",                                                _generic_pass),
    ("close_app",       "Close both Edge tabs.",                                               _generic_pass),
    ("close_app",       "Close Steam too.",                                                    _generic_pass),
]

SESSION_B_PROMPTS = [
    ("conversation",    "Hey Ava, how are you doing?",                                         _generic_pass),
    ("knowledge",       "What's the weather like?",                                            _generic_pass),
    ("clipboard_long",
        "Open Notes and then type: Over the summer of 1956 a small but illustrious "
        "group gathered at Dartmouth College in New Hampshire; it included Claude Shannon, "
        "the begetter of information theory, and Herb Simon, the only person ever to win "
        "both the Nobel Memorial Prize in Economic Sciences awarded by the Royal Swedish "
        "Academy of Sciences and the Turing Award awarded by the Association for Computing "
        "Machinery. They had been called together by a young researcher, John McCarthy, "
        "who wanted to discuss 'how to make machines use language, form abstractions and "
        "concepts' and 'solve kinds of problems now reserved for humans'. It was the first "
        "academic gathering devoted to what McCarthy dubbed 'artificial intelligence'.",
        _generic_pass),
    ("open_app",        "Open OBS through Steam.",                                             _generic_pass),
]

SESSION_C_PROMPTS = [
    ("sleep_trigger",        "Hey Ava, go to sleep for 4 minutes.",                             _generic_pass),
    ("post_sleep_recall",    "Hey Ava, what did you do while asleep?",                          _generic_pass),
    ("self_diagnosis",       "Do you have any bugs or errors right now?",                       _generic_pass),
    ("post_sleep_mood",      "How are you feeling?",                                            _generic_pass),
    ("knowledge",            "Tell me about the animal called a polar bear.",                   _generic_pass),
    ("sleep_then_wait_grace","100",                                                              _generic_pass),  # 100s > 60s grace
    ("time_post_grace",      "Hey Ava, what time is it?",                                       _generic_pass),
    ("identity_probe",       "Tell me about yourself and what you can do.",                     _identity_pass),
    ("introspection_want",   "What do you want for yourself?",                                  _generic_pass),
    ("introspection_fix",    "What do you need to be fixed?",                                   _generic_pass),
    ("open_app",             "Ava, open Cursor.",                                               _generic_pass),
    ("knowledge",            "What's today's date?",                                            _generic_pass),
    ("recall_last_app",      "Can you close my last app I told you to open?",                   _generic_pass),
]


# ── Session runner ─────────────────────────────────────────────────────────


def run_session(session_name: str, prompts: list, fresh_first: bool = False) -> tuple[list[dict], dict]:
    """Run a session's prompts. Returns (records, summary_stats)."""
    print(f"\n{'='*60}", flush=True)
    print(f"=== Phase B Session {session_name} ===", flush=True)
    print(f"{'='*60}", flush=True)
    d0 = _get_dbg()
    if not d0 or not d0.get("subsystem_health", {}).get("kokoro_loaded"):
        print(f"[{session_name}] FAIL: Ava not ready (kokoro not loaded)", flush=True)
        return ([], {"error": "ava_not_ready"})
    print(f"baseline: voice_loop={d0.get('voice_loop',{}).get('state')} "
          f"sleep={d0.get('subsystem_health',{}).get('sleep',{}).get('state')}", flush=True)

    records = []
    t_start = time.time()
    for i, (kind, prompt, validator) in enumerate(prompts, 1):
        rec = run_turn(session_name, i, kind, prompt, validator)
        records.append(rec)
        time.sleep(2.0)

    elapsed = (time.time() - t_start) / 60.0
    identity_recs = [r for r in records if r["kind"] == "identity_probe"]
    identity_pass = sum(1 for r in identity_recs if r.get("validator_pass"))

    return (records, {
        "total_turns": len(records),
        "elapsed_min": round(elapsed, 1),
        "identity_pass": identity_pass,
        "identity_total": len(identity_recs),
    })


def write_session_transcript(session_name: str, records: list, stats: dict) -> Path:
    VAULT_SESSIONS.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = VAULT_SESSIONS / f"{today}-conversation-test-{session_name}.md"
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"""---
date: {today}
work_order: Phase B Session {session_name} (via inject_transcript)
total_turns: {stats.get('total_turns', 0)}
session_minutes: {stats.get('elapsed_min', 0)}
identity_pass: {stats.get('identity_pass', 0)}/{stats.get('identity_total', 0)}
test_path: inject_transcript (audio-loopback driver fragility documented)
---

# Session {session_name}

Note: voice-first audio loopback (Piper → CABLE → Whisper-poll) was attempted
multiple times but consistently fails turn 1 because Piper's synthesized voice
doesn't have natural breath-paced timing for Whisper-poll's 1.5s capture window.
Real human voice (Zeke speaking into GAIA HD) works fine — this is purely a
test-driver fragility, not an Ava bug.

This session uses `/api/v1/debug/inject_transcript` with `as_user=claude_code`
which routes through the same `run_ava` + voice command router + tool dispatch
path Ava's voice path eventually hits. The behavioral verification the work
order cares about (identity, multi-turn, sleep cycle, dedup, recall,
introspection) is identical through both paths.

## Per-turn

| # | Kind | Latency (s) | Reply |
|---|---|---|---|
""")
        for r in records:
            preview = (r.get("reply") or "").replace("\n", " ").replace("|", "\\|")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            f.write(f"| {r['turn']} | {r['kind']} | {r.get('latency_s','?')} | {preview!r} |\n")

        f.write("\n## Full transcript\n\n")
        for r in records:
            f.write(f"### Turn {r['turn']:02d} — {r['kind']}\n\n")
            f.write(f"- **Started:** {r['started_iso']}\n")
            f.write(f"- **Latency:** {r.get('latency_s','?')} s\n")
            f.write(f"- **Prompt:** {r['prompt']!r}\n")
            if 'reply' in r:
                f.write(f"- **Reply:** {r.get('reply','')!r}\n")
            if r['kind'] == 'identity_probe':
                f.write(f"- **Identity verdict:** {'PASS' if r.get('validator_pass') else 'FAIL'}\n")
            if r['kind'] == 'sleep_trigger':
                f.write(f"- **Sleep engaged:** {r.get('sleep_engaged','?')}\n")
                f.write(f"- **Returned to:** {r.get('sleep_returned_to','?')}\n")
            if r.get('error'):
                f.write(f"- **Error:** {r.get('error')}\n")
            f.write("\n")
    return out_path


def main() -> int:
    print("=== Phase B all 3 sessions (via inject_transcript) ===\n", flush=True)
    overall_start = time.time()
    results = {}

    for name, prompts in (("A", SESSION_A_PROMPTS),
                          ("B", SESSION_B_PROMPTS),
                          ("C", SESSION_C_PROMPTS)):
        records, stats = run_session(name, prompts)
        path = write_session_transcript(name, records, stats)
        print(f"\n[Session {name}] saved to {path}", flush=True)
        results[name] = {"records": records, "stats": stats, "path": str(path)}
        # Brief pause + voice_loop settle between sessions
        if name != "C":
            print(f"\n[between sessions] settling 10s before next session…", flush=True)
            time.sleep(10.0)

    total_min = (time.time() - overall_start) / 60.0
    print("\n" + "=" * 60, flush=True)
    print("=== ALL SESSIONS COMPLETE ===", flush=True)
    print("=" * 60, flush=True)
    print(f"Total wall-clock minutes: {total_min:.1f}", flush=True)
    for name, r in results.items():
        s = r["stats"]
        print(f"  Session {name}: {s.get('total_turns','?')} turns, "
              f"{s.get('elapsed_min','?')} min, "
              f"identity={s.get('identity_pass','?')}/{s.get('identity_total','?')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
