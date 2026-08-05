# SELF_ASSESSMENT: I am the little-brain conversation driver — big-Iris talks
# to little-Iris through the REAL turn path (:8772) with persistent per-session
# history, raising the eval flag each ask so hand-ups don't echo-wake big-Iris
# mid-study (the 08-04 scar: free-form asks without the flag surface as
# "[Zeke asked Vector]" bridge nudges).
"""
lb_converse — converse with the little brain for behavioral study (2026-08-05).

Built for Zeke's work order (Discord, 08-05 ~00:5x): "test v16 some more and
try conversing with her to really get a feel on her and see what behaviorally
needs to change so she is more like you."

Usage (venv python, server on :8772):
    .venv/Scripts/python.exe scripts/lb_converse.py --session smalltalk --say "hey, how are you feeling?"
    .venv/Scripts/python.exe scripts/lb_converse.py --session smalltalk --show
    .venv/Scripts/python.exe scripts/lb_converse.py --session smalltalk --reset

History lives at state/little_brain/converse/<session>.json and the full
message list (capped at the last 12 turns, matching the pilot page) is sent
each ask, so she has conversational context.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8772/v1/chat/completions"
LB_DIR = ROOT / "state" / "little_brain"
SESS_DIR = LB_DIR / "converse"
EVAL_FLAG = LB_DIR / "eval_running.flag"
ASK_TIMEOUT_S = 170
HISTORY_WINDOW = 12  # messages sent per ask; matches the pilot page's slice(-12)


def _load(session: str) -> list[dict]:
    p = SESS_DIR / f"{session}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save(session: str, hist: list[dict]) -> None:
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    (SESS_DIR / f"{session}.json").write_text(
        json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")


def _touch_flag() -> None:
    # Fresh timestamp every ask — the read side stale-gates at 30 min, so a
    # long study session stays covered as long as asks keep coming.
    try:
        EVAL_FLAG.parent.mkdir(parents=True, exist_ok=True)
        EVAL_FLAG.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    except Exception:
        pass


def ask(session: str, text: str) -> str:
    hist = _load(session)
    hist.append({"role": "user", "content": text, "ts": time.strftime("%H:%M:%S")})
    msgs = [{"role": m["role"], "content": m["content"]} for m in hist][-HISTORY_WINDOW:]
    _touch_flag()
    body = json.dumps({"model": "iris", "messages": msgs}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "x-iris-local-only": "1"})
    with urllib.request.urlopen(req, timeout=ASK_TIMEOUT_S) as r:
        data = json.loads(r.read().decode("utf-8"))
    reply = data["choices"][0]["message"]["content"]
    hist.append({"role": "assistant", "content": reply, "ts": time.strftime("%H:%M:%S")})
    _save(session, hist)
    return reply


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--say")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset:
        p = SESS_DIR / f"{args.session}.json"
        if p.exists():
            p.unlink()
        print(f"reset: {args.session}")
        return
    if args.show:
        for m in _load(args.session):
            print(f"[{m.get('ts','?')}] {m['role']}: {m['content']}\n")
        return
    if args.say:
        print(ask(args.session, args.say))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
