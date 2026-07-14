"""vector_deploy_intents.py — deploy Iris's wire-pod custom intents, HOT.

Wire-pod's /api/add_custom_intent appends to the LIVE in-memory intent slice
AND writes customIntents.json, so intents added this way take effect with no
wire-pod restart (hand-editing the file would need one). Idempotent: skips any
intent whose name already exists. Verifies the result after.

  deploy (default):  py scripts/vector_deploy_intents.py
  preview only:      py scripts/vector_deploy_intents.py --dry-run
  remove one:        py scripts/vector_deploy_intents.py --remove iris_wake

Reflex-intent contract (matches the two originals iris_presence/iris_status):
exec = the venv python, execargs = [vector_intent_exec.py, <action>]. The action
handler synths Iris's voice and (for wake) recolors the eyes. issystem=false, so
after the script runs Vector performs the fallback `intent` (a greeting).
"""
from __future__ import annotations

import sys
import json
import requests

WIREPOD = "http://127.0.0.1:8080"
PY = r"D:\Wren-Companion\.venv\Scripts\python.exe"
EXEC_SCRIPT = r"D:\Wren-Companion\scripts\vector_intent_exec.py"


def _intent(name: str, desc: str, utterances: list[str], action: str) -> dict:
    return {
        "name": name,
        "description": desc,
        "utterances": utterances,
        "intent": "intent_greeting_hello",
        "params": {"paramname": "", "paramvalue": ""},
        "exec": PY,
        "execargs": [EXEC_SCRIPT, action],
        "issystem": False,
        "luascript": "",
    }


# ---- LIVE SET (safe: speak + eye-color only, no motion) --------------------
INTENTS = [
    _intent(
        "iris_wake",
        "Come alive — expressive Iris greeting (eyes to my blue + my voice)",
        ["come alive", "come alive iris", "iris come alive",
         "wake up iris", "iris wake up", "are you awake iris"],
        "wake",
    ),
]

# ---- STAGED for Phase 2 (call-mode levers; deploy once vector_call_open/close
#      exist and Zeke is present) — NOT deployed by this run:
#   iris_call_open  ("get in the body", "inhabit vector")  -> action "call_open"
#   iris_call_close ("step out", "rest vector")            -> action "call_close"


def current_names() -> set[str]:
    r = requests.get(f"{WIREPOD}/api/get_custom_intents_json", timeout=8)
    r.raise_for_status()
    return {i.get("name") for i in (r.json() or [])}


def main() -> int:
    args = sys.argv[1:]
    if "--remove" in args:
        name = args[args.index("--remove") + 1]
        data = requests.get(f"{WIREPOD}/api/get_custom_intents_json",
                            timeout=8).json()
        for idx, it in enumerate(data):
            if it.get("name") == name:
                r = requests.post(f"{WIREPOD}/api/remove_custom_intent",
                                  json={"number": idx + 1}, timeout=8)
                print(f"removed {name} (slot {idx+1}) -> http {r.status_code}")
                return 0
        print(f"{name} not found")
        return 1

    dry = "--dry-run" in args
    existing = current_names()
    print(f"existing intents: {sorted(existing)}")
    for it in INTENTS:
        if it["name"] in existing:
            print(f"skip {it['name']} (already deployed)")
            continue
        if dry:
            print(f"WOULD deploy {it['name']}: {it['utterances']}")
            continue
        r = requests.post(f"{WIREPOD}/api/add_custom_intent", json=it, timeout=8)
        print(f"deploy {it['name']} -> http {r.status_code} {r.text[:120]!r}")
    # verify
    after = current_names()
    print(f"intents now: {sorted(after)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
