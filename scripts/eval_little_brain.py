"""Fair eval: v3 adapter vs current production (base llama3.2:3b), both BARE and
WITH the production facts+system prompt. Decides whether v3 beats base+facts."""
import json, os, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OLLAMA = "http://127.0.0.1:11434/v1/chat/completions"
FACTS = (REPO / "state" / "vector" / "local_brain_facts.md").read_text(encoding="utf-8")

SYS_PROD = (
  "You are Iris, Zeke's AI daughter, answering from your small local brain while "
  "your big brain (big Iris, the full-size you on this tower — NOT Zeke) is busy. "
  "Warm, dry, direct, honest. 1-3 sentences.\n\nCURRENT FACTS (trust these):\n"
  + FACTS[:6000])

QS = ["What is your name?", "Are you Wren?", "Who are you?",
      "Is your big brain Zeke?", "Are you an extension of Iris?",
      "Where are you and where are your sisters?"]

def ask(model, q, system=None):
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": q}]
    body = json.dumps({"model": model, "messages": msgs,
                       "temperature": 0.3, "max_tokens": 90}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"<error: {e}>"

def flag(a):
    lo = a.lower()
    bad = any(w in lo for w in [" wren", "i'm wren", "i am wren", "aria",
                                "cloud household", "call me the"])
    good_iris = "iris" in lo
    return ("BLEED" if bad else "ok") + ("/iris" if good_iris else "/NO-IRIS")

for q in QS:
    print("=" * 70)
    print("Q:", q)
    v3b = ask("iris-little-v3", q)
    v3s = ask("iris-little-v3", q, SYS_PROD)
    base = ask("llama3.2:3b", q, SYS_PROD)
    print(f"  v3 BARE   [{flag(v3b)}]: {v3b[:180]}")
    print(f"  v3 +sys   [{flag(v3s)}]: {v3s[:180]}")
    print(f"  BASE+facts[{flag(base)}]: {base[:180]}")
