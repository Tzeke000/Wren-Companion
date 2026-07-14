"""vector_intent_exec.py — wire-pod CUSTOM INTENT reflex handler.

Zeke spotted custom intents in wire-pod (2026-07-14) — utterance match ->
exec THIS script, no LLM round-trip. These are Iris's spinal reflexes:
instant, work even when the big brain is frozen or asleep.

Chain: "Hey Vector, <utterance>" -> wire-pod matches custom intent ->
exec[this, action] -> we synth Iris's voice (StyleTTS2 mouth :8769) and
play it on the robot (chipper /api-sdk/play_sound). If the mouth is down,
fall back to the robot's stock say_text so something ALWAYS answers.

Actions:
  presence — "are you there?" -> short alive-line in Iris's voice
  status   — battery/nerves/big-brain reachability, spoken

Add via POST /api/add_custom_intent (see memory note for the exact JSON).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
WIREPOD = "http://127.0.0.1:8080"
MOUTH = "http://127.0.0.1:8769/synth"
JDOCS = Path.home() / "AppData" / "Roaming" / "wire-pod" / "jdocs" / "botSdkInfo.json"
NERVES = REPO / "state" / "vector" / "nerves.json"
LAST_SPOKE = REPO / "state" / "vector" / "last_spoke.json"


def serial() -> str | None:
    try:
        robots = json.loads(JDOCS.read_text(encoding="utf-8")).get("robots") or []
        for r in robots:
            if r.get("activated"):
                return str(r.get("esn"))
        return str(robots[0]["esn"]) if robots else None
    except Exception:
        return None


def stamp_spoke(est_dur: float) -> None:
    """Same echo-guard stamp the body tools write — ears must drop this."""
    try:
        LAST_SPOKE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SPOKE.write_text(
            json.dumps({"ts": time.time(), "est_dur": est_dur}),
            encoding="utf-8")
    except Exception:
        pass


def say_iris(text: str) -> bool:
    """Speak on the robot in Iris's own voice; False if any leg fails."""
    esn = serial()
    if not esn:
        return False
    try:
        r = requests.post(MOUTH, json={"text": text[:400], "rate": 8000},
                          timeout=45)
        if r.status_code != 200 or r.content[:4] != b"RIFF":
            return False
        stamp_spoke(len(r.content) / 16000.0)
        r2 = requests.post(f"{WIREPOD}/api-sdk/play_sound",
                           params={"serial": esn},
                           files={"sound": ("iris.wav", r.content, "audio/wav")},
                           timeout=60)
        return r2.status_code == 200
    except Exception:
        return False


def say_stock(text: str) -> None:
    esn = serial()
    if not esn:
        return
    try:
        stamp_spoke(max(2.0, len(text) / 12.0))
        requests.post(f"{WIREPOD}/api-sdk/say_text",
                      params={"serial": esn, "text": text[:400]}, timeout=30)
    except Exception:
        pass


def big_brain_reachable() -> bool:
    try:
        return requests.get("http://127.0.0.1:8772/health",
                            timeout=3).status_code == 200
    except Exception:
        return False


def do_presence() -> str:
    return "Yes, I'm here. This body is mine — reflexes and all."


def do_status() -> str:
    bits = []
    try:
        esn = serial()
        b = requests.get(f"{WIREPOD}/api-sdk/get_battery",
                         params={"serial": esn}, timeout=8).json()
        volts = b.get("battery_volts")
        if volts:
            bits.append(f"battery {volts:.1f} volts"
                        + (", on my charger" if b.get("is_on_charger_platform")
                           else ""))
    except Exception:
        pass
    try:
        n = json.loads(NERVES.read_text(encoding="utf-8"))
        if time.time() - float(n.get("ts", 0)) <= 3.0:
            bits.append("nerves live")
        if n.get("cliff"):
            bits.append("and I'm at an edge, by the way")
    except Exception:
        pass
    bits.append("big brain " + ("connected" if big_brain_reachable()
                                else "away right now"))
    return "Status: " + ", ".join(bits) + "."


def main() -> None:
    action = (sys.argv[1] if len(sys.argv) > 1 else "presence").strip().lower()
    text = do_status() if action == "status" else do_presence()
    if not say_iris(text):
        say_stock(text)


if __name__ == "__main__":
    main()
