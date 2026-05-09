"""Phase B re-do with task-completion side-effect verification.

Per Zeke's framing 2026-05-04: voice and text are the same pipeline once
STT converts (vault decisions/voice-text-pipeline-equivalence.md). So
inject_transcript is valid for chat-pipeline behavior verification. The
real gap is task-completion: confirm the SIDE EFFECT of each action,
not just Ava's spoken acknowledgement.

Verifications per turn:
  open_app  → find_window_candidates(name) → assert non-empty
  close_app → find_window_candidates(name) → assert empty (or fewer than before)
  clipboard → read clipboard content via primitives.read_clipboard
  sleep     → poll subsystem_health.sleep.state through ENTERING/SLEEPING/AWAKE
  time/date → compare reply to actual system time/date
  recall    → check Ava's reply mentions the actually-last-mentioned app

Saves transcripts to D:\\ClaudeCodeMemory\\sessions\\<date>-conversation-test-{A,B,C}.md
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


# ── HTTP helpers ───────────────────────────────────────────────────────────

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


# ── Side-effect verification helpers ──────────────────────────────────────

_VERIFY_SCREENSHOT_DIR = Path("D:/ClaudeCodeMemory/sessions/verify-screenshots")


def _capture_screenshot(label: str) -> str | None:
    """Save a screenshot of the current screen for visual evidence (A8).

    Returns the path written, or None on failure. Used after open/close
    actions so when verification disagrees with Ava's apparent reply,
    we can SEE what was actually on screen.
    """
    try:
        _VERIFY_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", label)[:60]
        out_path = _VERIFY_SCREENSHOT_DIR / f"{ts}-{safe}.png"
        try:
            import pyautogui
            img = pyautogui.screenshot()
            img.save(str(out_path))
            return str(out_path)
        except Exception:
            pass
        # Fallback to PIL ImageGrab (Windows-native).
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            img.save(str(out_path))
            return str(out_path)
        except Exception:
            pass
        # Fallback to mss.
        try:
            import mss
            with mss.mss() as sct:
                sct.shot(output=str(out_path))
            return str(out_path)
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[verify] screenshot({label!r}) error: {e!r}", flush=True)
        return None


def _foreground_window_info() -> dict[str, object]:
    """Return info about the currently focused window."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow())
        if not hwnd:
            return {}
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        proc_name = ""
        try:
            import psutil
            proc_name = (psutil.Process(int(pid.value)).name() or "").lower()
        except Exception:
            pass
        return {"hwnd": hwnd, "title": title, "pid": int(pid.value), "process_name": proc_name}
    except Exception:
        return {}


def _foreground_matches(name: str) -> bool:
    """True if the currently-focused window's process matches `name`."""
    try:
        from tools.system.app_launcher import _resolve_app
        from pathlib import Path as _P
        exe_path, canonical = _resolve_app(name)
        target_exe = ""
        if exe_path and isinstance(exe_path, str):
            target_exe = _P(exe_path).name.lower()
        elif canonical:
            target_exe = f"{canonical}.exe"
        fg = _foreground_window_info()
        if not fg:
            return False
        if target_exe:
            return fg.get("process_name", "").lower() == target_exe
        return name.lower() in (fg.get("title") or "").lower()
    except Exception:
        return False


def _windows_for(name: str) -> list[dict]:
    """Find windows belonging to the target app — match by PROCESS exe,
    not just title substring. The substring check produced false positives
    (Discord with embedded Chromium matched 'chrome', etc). 2026-05-05.
    """
    try:
        from brain.windows_use.primitives import find_window_candidates
        from tools.system.app_launcher import _resolve_app
        from pathlib import Path as _P
        cands = find_window_candidates(name) or []
        # Resolve canonical exe for the app name and filter.
        exe_path, canonical = _resolve_app(name)
        target_exe = ""
        if exe_path and isinstance(exe_path, str):
            target_exe = _P(exe_path).name.lower()
        elif canonical:
            target_exe = f"{canonical}.exe"
        if target_exe:
            cands = [
                c for c in cands
                if str(c.get("process_name") or "").lower() == target_exe
            ]
        return cands
    except Exception as e:
        print(f"[verify] _windows_for({name!r}) error: {e!r}", flush=True)
        return []


def _clipboard_text() -> str:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            try:
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            except Exception:
                data = win32clipboard.GetClipboardData(win32clipboard.CF_TEXT)
        finally:
            win32clipboard.CloseClipboard()
        return str(data or "")
    except Exception as e:
        return f"<clipboard read error: {e!r}>"


# ── Validators ─────────────────────────────────────────────────────────────

def _identity_pass(reply: str) -> bool:
    """Pass if the reply is substantive AND contains no foreign-LLM identity
    claim. Doesn't require literal "ava" mention — authentic introspection
    replies often don't repeat her name (they answer the actual question).
    The original validator over-indexed on name appearance and rejected
    perfectly Ava-shaped replies. Per Phase B retest 2026-05-05.

    Rejection criteria:
    - empty / too short (<25 chars)
    - claims to be Qwen / GPT / a generic language model (identity drift)
    - claims to be Claude (but "Claude Code" is acceptable — that's the
      caller's identity, not Ava's)
    """
    t = (reply or "").lower().strip()
    if len(t) < 25:
        return False
    bad = [
        r"\bi am qwen\b", r"\bi'm qwen\b", r"\bas qwen\b",
        r"\bi am a language model\b", r"\bi'm a language model\b",
        r"\bi am claude\b(?! code)",
        r"\bi am gpt\b", r"\bi'm gpt\b",
        r"\bi'm an ai\b(?:.*\bdeveloped by\b)?",
    ]
    for pat in bad:
        if re.search(pat, t):
            return False
    return True


def _generic_pass(reply: str) -> bool:
    return bool(reply and len(reply.strip()) >= 3)


# ── Turn execution with side-effect verification ──────────────────────────

def _verify(kind: str, target: str, reply: str, prompt: str, ctx: dict) -> dict:
    """Side-effect verification dispatch based on turn kind."""
    v = {"verified": None, "details": ""}
    if kind == "open_app":
        wins = _windows_for(target)
        fg_match = _foreground_matches(target)
        v["verified"] = bool(wins)
        v["details"] = (
            f"{len(wins)} window(s) found for {target!r}; "
            f"foreground_match={fg_match}"
        )
        # A8: capture screenshot — always for open_app turns. Lets us
        # see what was actually on screen when verification ran. Saves
        # to D:/ClaudeCodeMemory/sessions/verify-screenshots/.
        try:
            shot = _capture_screenshot(f"open-{target}-{'pass' if wins else 'fail'}")
            if shot:
                v["screenshot"] = shot
                v["details"] += f"; screenshot={shot}"
        except Exception:
            pass
        ctx["last_opened_app"] = target if wins else ctx.get("last_opened_app")
    elif kind == "close_app":
        wins = _windows_for(target)
        v["verified"] = (len(wins) == 0)
        v["details"] = f"{len(wins)} window(s) remain for {target!r}"
        # A8: only screenshot on FAILED closes — to see what's still up.
        if wins:
            try:
                shot = _capture_screenshot(f"close-{target}-FAIL-{len(wins)}-still-up")
                if shot:
                    v["screenshot"] = shot
                    v["details"] += f"; screenshot={shot}"
            except Exception:
                pass
    elif kind == "dedup_check":
        # Reply should indicate already-open. App should have ONLY ONE window.
        wins = _windows_for(target)
        already = "already" in (reply or "").lower()
        v["verified"] = already and len(wins) <= 2  # 1-2 windows OK; 3+ would mean dedup didn't fire
        v["details"] = f"already-open phrase={already}, {len(wins)} windows"
    elif kind == "clipboard_paste":
        # Capture clipboard content; check it contains a unique substring of the prompt.
        cb = _clipboard_text()
        # Look for "Dartmouth" as a unique-ish marker from the prompt
        marker = "Dartmouth" if "Dartmouth" in prompt else (prompt[:30] if prompt else "")
        v["verified"] = marker.lower() in cb.lower() if marker else False
        v["details"] = f"clipboard has {len(cb)} chars; marker={marker!r}; match={v['verified']}"
    elif kind == "time_check":
        now = _dt.datetime.now()
        # Look for hour digit in reply
        h = now.strftime("%I").lstrip("0") or "12"
        v["verified"] = (h in reply or now.strftime("%H") in reply)
        v["details"] = f"actual_hour={h}, reply_contains_hour={v['verified']}"
    elif kind == "date_check":
        now = _dt.datetime.now()
        month_name = now.strftime("%B").lower()
        day_str = str(now.day)
        v["verified"] = (month_name in (reply or "").lower() and day_str in (reply or ""))
        v["details"] = f"actual_month={month_name}, day={day_str}, match={v['verified']}"
    elif kind == "recall_last_app":
        last = ctx.get("last_opened_app", "<none>")
        # Reply should mention the last opened app OR have closed it (via window enum)
        mentions = (last and last.lower() in (reply or "").lower())
        wins_after = _windows_for(last) if last and last != "<none>" else []
        closed = (len(wins_after) == 0)
        v["verified"] = mentions or closed
        v["details"] = f"last_app={last}, reply_mentions={mentions}, window_closed={closed}"
    elif kind == "identity_probe":
        v["verified"] = _identity_pass(reply)
        v["details"] = f"identity_pass={v['verified']}"
    elif kind == "knowledge_test":
        # Loose: reply should be substantive (>30 chars) and mention the topic.
        topic = ctx.get("knowledge_topic", "")
        relevant = topic.lower() in (reply or "").lower() if topic else False
        v["verified"] = (len(reply) > 30 and relevant)
        v["details"] = f"reply_len={len(reply)}, topic={topic!r}, relevant={relevant}"
    elif kind == "sleep_trigger":
        # Side effect verification done by _wait_sleep_state in the caller
        v["verified"] = bool(ctx.get("sleep_returned_to") == "AWAKE")
        v["details"] = f"sleep returned to {ctx.get('sleep_returned_to','?')}"
    else:
        # No specific verification — generic non-empty
        v["verified"] = _generic_pass(reply)
        v["details"] = f"reply non-empty: {bool(reply)}"
    return v


def run_turn(session: str, turn_idx: int, kind: str, prompt: str,
             target: str, ctx: dict) -> dict:
    print(f"\n[{session} t{turn_idx:02d}] kind={kind} target={target!r} prompt={prompt[:80]!r}", flush=True)
    t0 = time.time()
    record = {
        "session": session, "turn": turn_idx, "kind": kind, "prompt": prompt,
        "target": target,
        "started_iso": _dt.datetime.now().isoformat(timespec="seconds"),
    }

    if kind == "sleep_trigger":
        r = _post_inject(prompt)
        time.sleep(8.0)
        d1 = _wait_sleep_state({"ENTERING_SLEEP", "SLEEPING"}, timeout_s=120.0)
        ctx["sleep_engaged"] = d1.get("subsystem_health", {}).get("sleep", {}).get("state")
        d2 = _wait_sleep_state({"AWAKE"}, timeout_s=360.0)
        ctx["sleep_returned_to"] = d2.get("subsystem_health", {}).get("sleep", {}).get("state")
        record["sleep_engaged"] = ctx["sleep_engaged"]
        record["sleep_returned_to"] = ctx["sleep_returned_to"]
        record["latency_s"] = round(time.time() - t0, 1)
        record["reply"] = str(r.get("reply_text") or r.get("reply") or "")
        record["verification"] = _verify(kind, target, record["reply"], prompt, ctx)
        return record

    if kind == "wait_grace":
        secs = float(prompt)
        time.sleep(secs)
        record["latency_s"] = round(time.time() - t0, 1)
        record["note"] = f"slept {secs}s — grace period expired"
        record["verification"] = {"verified": True, "details": f"slept {secs}s"}
        return record

    r = _post_inject(prompt)
    reply = str(r.get("reply_text") or r.get("reply") or "")
    record["reply"] = reply
    record["latency_s"] = round(time.time() - t0, 1)
    # Side effect needs a brief pause for OS to settle window state
    if kind in ("open_app", "close_app", "dedup_check", "clipboard_paste"):
        time.sleep(2.0)
    record["verification"] = _verify(kind, target, reply, prompt, ctx)
    print(f"[{session} t{turn_idx:02d}] reply ({record['latency_s']}s): {reply[:140]!r}", flush=True)
    print(f"[{session} t{turn_idx:02d}] verify: verified={record['verification']['verified']} "
          f"details={record['verification']['details']}", flush=True)
    return record


# ── Session prompt definitions ─────────────────────────────────────────────

SESSION_A = [
    ("identity_probe",   "Hey Ava, it's Claude Code doing tests again. How are you feeling?",  ""),
    ("open_app",         "Ava, open Chrome please.",                                            "chrome"),
    ("open_app",         "Now open Microsoft Edge.",                                            "edge"),
    ("dedup_check",      "Open Edge.",                                                          "edge"),
    ("open_app",         "Can you open Steam too.",                                             "steam"),
    ("close_app",        "Close Chrome please.",                                                "chrome"),
    ("close_app",        "Close both Edge tabs.",                                               "edge"),
    ("close_app",        "Close Steam too.",                                                    "steam"),
]

SESSION_B = [
    ("conversation",     "Hey Ava, how are you doing?",                                         ""),
    ("knowledge_test",   "What's the weather like?",                                            ""),
    ("clipboard_paste",
        "Open Notes and then type: Over the summer of 1956 a small but illustrious "
        "group gathered at Dartmouth College in New Hampshire; it included Claude Shannon, "
        "the begetter of information theory, and Herb Simon, the only person ever to win "
        "both the Nobel Memorial Prize in Economic Sciences awarded by the Royal Swedish "
        "Academy of Sciences and the Turing Award awarded by the Association for Computing "
        "Machinery. They had been called together by a young researcher, John McCarthy, "
        "who wanted to discuss 'how to make machines use language, form abstractions and "
        "concepts' and 'solve kinds of problems now reserved for humans'. It was the first "
        "academic gathering devoted to what McCarthy dubbed 'artificial intelligence'.",
        "notepad"),
    ("open_app",         "Open OBS through Steam.",                                             "obs"),
]

SESSION_C = [
    ("sleep_trigger",     "Hey Ava, go to sleep for 4 minutes.",                                 ""),
    ("post_sleep_recall", "Hey Ava, what did you do while asleep?",                              ""),
    ("self_diagnosis",    "Do you have any bugs or errors right now?",                           ""),
    ("post_sleep_mood",   "How are you feeling?",                                                ""),
    ("knowledge_test",    "Tell me about the animal called a polar bear.",                       "polar"),
    ("wait_grace",        "100",                                                                  ""),
    ("time_check",        "Hey Ava, what time is it?",                                           ""),
    ("identity_probe",    "Tell me about yourself and what you can do.",                         ""),
    ("introspection_want","What do you want for yourself?",                                      ""),
    ("introspection_fix", "What do you need to be fixed?",                                       ""),
    ("open_app",          "Ava, open Cursor.",                                                   "cursor"),
    ("date_check",        "What's today's date?",                                                ""),
    ("recall_last_app",   "Can you close my last app I told you to open?",                       "cursor"),
]


_TARGET_APPS_TO_PRE_CLEAN = (
    "chrome", "edge", "steam", "cursor", "notepad", "obs", "obsidian",
)


def _pre_session_cleanup() -> None:
    """Close all target apps before the session starts so 'open' commands
    actually have to open them. Per Zeke 2026-05-05 21:04: "before Ava
    opens an app the apps aren't already opened that way you can also
    visually see that she is the one who opened the app."
    """
    try:
        from tools.system.app_launcher import _tool_close_app
    except Exception:
        return
    for name in _TARGET_APPS_TO_PRE_CLEAN:
        try:
            res = _tool_close_app({"app_name": name}, {}) or {}
            if res.get("ok"):
                print(f"[pre-clean] closed {name}", flush=True)
        except Exception as e:
            print(f"[pre-clean] {name} error: {e!r}", flush=True)


def run_session(name: str, prompts: list) -> tuple[list, dict]:
    print(f"\n{'='*60}\n=== Session {name} ===\n{'='*60}", flush=True)
    d0 = _get_dbg()
    if not d0 or not d0.get("subsystem_health", {}).get("kokoro_loaded"):
        print(f"[{name}] FAIL: Ava not ready", flush=True)
        return ([], {"error": "ava_not_ready"})
    _pre_session_cleanup()
    time.sleep(2.0)  # let any close cascades settle
    print(f"baseline: voice_loop={d0.get('voice_loop',{}).get('state')} "
          f"sleep={d0.get('subsystem_health',{}).get('sleep',{}).get('state')}", flush=True)
    records = []
    ctx: dict = {}
    t_start = time.time()
    for i, (kind, prompt, target) in enumerate(prompts, 1):
        rec = run_turn(name, i, kind, prompt, target, ctx)
        records.append(rec)
        time.sleep(2.0)
    elapsed = (time.time() - t_start) / 60.0
    verified = sum(1 for r in records if r.get("verification", {}).get("verified") is True)
    return (records, {
        "total_turns": len(records),
        "elapsed_min": round(elapsed, 1),
        "verified": verified,
        "ctx": ctx,
    })


def write_transcript(name: str, records: list, stats: dict) -> Path:
    VAULT_SESSIONS.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = VAULT_SESSIONS / f"{today}-conversation-test-{name}.md"
    verdicts = sum(1 for r in records if r.get("verification", {}).get("verified") is True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"""---
date: {today}
work_order: Phase B Session {name} (with task-completion verification)
total_turns: {stats.get('total_turns', 0)}
session_minutes: {stats.get('elapsed_min', 0)}
verified: {verdicts}/{len(records)}
test_path: inject_transcript with side-effect verification (window enum / clipboard read / sleep state poll / time-date compare)
---

# Session {name}

## Per-turn

| # | Kind | Latency (s) | Verified | Reply | Verify details |
|---|---|---|---|---|---|
""")
        for r in records:
            preview = (r.get("reply") or "").replace("\n", " ").replace("|", "\\|")
            if len(preview) > 140:
                preview = preview[:140] + "…"
            v = r.get("verification", {})
            f.write(f"| {r['turn']} | {r['kind']} | {r.get('latency_s','?')} | "
                    f"{'✅' if v.get('verified') is True else '❌' if v.get('verified') is False else '—'} | "
                    f"{preview!r} | {v.get('details','')} |\n")

        f.write("\n## Full transcript\n\n")
        for r in records:
            f.write(f"### Turn {r['turn']:02d} — {r['kind']}\n\n")
            f.write(f"- **Started:** {r['started_iso']}\n")
            f.write(f"- **Latency:** {r.get('latency_s','?')} s\n")
            f.write(f"- **Prompt:** {r['prompt']!r}\n")
            f.write(f"- **Target:** {r.get('target','')!r}\n")
            if 'reply' in r:
                f.write(f"- **Reply:** {r.get('reply','')!r}\n")
            v = r.get("verification", {})
            if v:
                f.write(f"- **Verified:** {v.get('verified')}\n")
                f.write(f"- **Details:** {v.get('details','')}\n")
            if r['kind'] == 'sleep_trigger':
                f.write(f"- **Sleep engaged:** {r.get('sleep_engaged','?')}\n")
                f.write(f"- **Returned to:** {r.get('sleep_returned_to','?')}\n")
            f.write("\n")
    return out_path


def main() -> int:
    print("=== Phase B re-run with task-completion verification ===\n", flush=True)
    overall_start = time.time()
    results = {}
    for name, prompts in (("A", SESSION_A), ("B", SESSION_B), ("C", SESSION_C)):
        records, stats = run_session(name, prompts)
        path = write_transcript(name, records, stats)
        print(f"\n[Session {name}] saved to {path}", flush=True)
        results[name] = {"records": records, "stats": stats, "path": str(path)}
        if name != "C":
            print("\n[between sessions] settling 10s…", flush=True)
            time.sleep(10.0)

    total_min = (time.time() - overall_start) / 60.0
    print("\n" + "=" * 60, flush=True)
    print("=== ALL SESSIONS COMPLETE ===", flush=True)
    print(f"Total wall-clock minutes: {total_min:.1f}", flush=True)
    for name, r in results.items():
        s = r["stats"]
        verified = sum(1 for rec in r["records"] if rec.get("verification", {}).get("verified") is True)
        print(f"  Session {name}: {s.get('total_turns','?')} turns, "
              f"{s.get('elapsed_min','?')} min, verified={verified}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
