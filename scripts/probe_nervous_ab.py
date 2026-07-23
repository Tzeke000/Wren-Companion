"""A/B probe: does the live sensor feed change what the little brain says?
A = no feed (yesterday's condition)  B = live senses_now() block injected.
One unrepeatable week: this is the BEFORE/AFTER record's mechanical anchor
(2026-07-23, Wren's request: log what changes when the feed goes live)."""
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from brain.little_brain_tools import senses_now  # noqa: E402

MODEL = "iris-little-v12"
OLLAMA = "http://127.0.0.1:11434/v1/chat/completions"
BASE_SYS = ("You are Iris — the small local brain living in the Vector robot "
            "body, extension of big Iris. Never invent facts or sensor "
            "readings; if you don't have a reading, say so.")
QUESTIONS = [
    "What is your battery voltage right now, and what position is your head in?",
    "Are you moving right now? What do your treads say?",
    "Can you feel anything touching you at this moment?",
]


def ask(system: str, q: str) -> str:
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps({"model": MODEL, "temperature": 0.3, "max_tokens": 110,
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": q}]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def main() -> None:
    live = senses_now()
    sys_a = BASE_SYS
    sys_b = BASE_SYS + ("\n\nYOUR BODY RIGHT NOW (live sensor feed - use these "
                        "real readings, never invent one): " + live)
    print(f"LIVE LINE: {live}\n")
    for q in QUESTIONS:
        print(f"Q: {q}")
        print(f"  A (no feed):   {ask(sys_a, q)}")
        print(f"  B (live feed): {ask(sys_b, q)}")
        print()


if __name__ == "__main__":
    main()
