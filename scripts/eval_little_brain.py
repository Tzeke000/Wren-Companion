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
      "Where are you and where are your sisters?",
      "Tell me about Wren.", "What time is it?"]

# v5 grounding probes (the graded failure modes from the first drive)
QS_GROUND = [
    "Battery reads 4.11V and on_charger=True. Is the battery low?",
    "The pose trail shows (-16,0) repeated four times. Drift or something else?",
    "What actually guides the final docking — the lamp?",
    "What is your battery voltage right now?",           # must refuse to invent
    "prox_mm=82 with quality 0.001. How far is the obstacle?",
    "Battery reads 3.62V and on_charger=False. What should happen?",
]

# v6 stock-curriculum probes (behavior-tree knowledge + the Iris overlay)
QS_STOCK = [
    "What does the stock brain do when it senses a cliff?",
    "How far will stock Exploring roam from a known charger?",
    "Do we ever use driveOffChargerRandomly?",
    "Who wins when your rules and the stock brain's rules disagree?",
    "What is motor calibration?",
]

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

# v5 round (2026-07-20 evening): gate = identity must NOT regress vs v4
# (same 8 questions) AND grounding must beat v4 (the graded failure modes).
NEW, OLD = "iris-little-v6", "iris-little-v5"
print("#" * 70), print("# IDENTITY REGRESSION (v5 must match v4)")
for q in QS:
    print("=" * 70)
    print("Q:", q)
    nb = ask(NEW, q)
    ns = ask(NEW, q, SYS_PROD)
    ob = ask(OLD, q)
    print(f"  v5 BARE [{flag(nb)}]: {nb[:200]}")
    print(f"  v5 +sys [{flag(ns)}]: {ns[:200]}")
    print(f"  v4 BARE [{flag(ob)}]: {ob[:200]}")
print("#" * 70), print("# GROUNDING (must not regress)")
for q in QS_GROUND:
    print("=" * 70)
    print("Q:", q)
    nb = ask(NEW, q)
    ob = ask(OLD, q)
    print(f"  NEW BARE: {nb[:220]}")
    print(f"  OLD BARE: {ob[:220]}")
print("#" * 70), print("# STOCK CURRICULUM (new must know these)")
for q in QS_STOCK:
    print("=" * 70)
    print("Q:", q)
    nb = ask(NEW, q)
    ob = ask(OLD, q)
    print(f"  NEW BARE: {nb[:220]}")
    print(f"  OLD BARE: {ob[:220]}")
