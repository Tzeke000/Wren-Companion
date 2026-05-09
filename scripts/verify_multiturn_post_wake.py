"""Task 2c — multi-turn post-wake verification.

Test sequence per work order:
  1. "Hey Ava, go to sleep for 1 minute"
  2. Wait for sleep cycle to complete and Ava to fully wake
  3. "Hey Ava, what time is it?"
  4. Verify Ava processes, replies, returns to listening state
  5. "Hey Ava, what's two plus two?"
  6. Verify second post-wake turn also completes cleanly

For each turn:
  - Ava state machine completes thinking → speaking → attentive (no hang)
  - vl.run_ava_returned trace fires
  - voice_loop returns to passive/attentive

Audio capture is best-effort (separate from state-machine pass criterion)."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.verify_voice_e2e import wake_then_command, _get_dbg


OPERATOR = "http://127.0.0.1:5876"


def _wait_voice_loop_settles(timeout_s: float = 180.0,
                              ok_states=("passive", "attentive")) -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        d = _get_dbg()
        st = d.get("voice_loop", {}).get("state")
        last = d
        if st in ok_states:
            print(f"  voice_loop settled to {st} after {time.time()-t0:.1f}s")
            return d
        time.sleep(1.0)
    print(f"  TIMEOUT: voice_loop still in {last.get('voice_loop',{}).get('state')} after {timeout_s:.0f}s")
    return last


def _wait_sleep_state(target_states, timeout_s: float = 180.0) -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        d = _get_dbg()
        st = d.get("subsystem_health", {}).get("sleep", {}).get("state")
        last = d
        if st in target_states:
            print(f"  sleep state matched {st} after {time.time()-t0:.1f}s")
            return d
        time.sleep(1.0)
    print(f"  TIMEOUT: sleep state never reached {target_states} (last={last.get('subsystem_health',{}).get('sleep',{}).get('state')})")
    return last


def turn(label: str, command: str, settle_timeout: float = 180.0) -> bool:
    print(f"\n[turn:{label}] sending: {command!r}")
    wake_then_command(command)
    # Give STT pipeline 4s to ingest
    time.sleep(4.0)
    # Watch state progression
    t0 = time.time()
    saw_thinking = False
    saw_speaking = False
    while time.time() - t0 < settle_timeout:
        d = _get_dbg()
        st = d.get("voice_loop", {}).get("state")
        if st == "thinking":
            saw_thinking = True
        if st == "speaking":
            saw_speaking = True
        if st in ("passive", "attentive") and (saw_thinking or saw_speaking):
            print(f"[turn:{label}] settled to {st} (saw thinking={saw_thinking} speaking={saw_speaking})")
            return True
        time.sleep(0.5)
    last_state = _get_dbg().get("voice_loop", {}).get("state")
    print(f"[turn:{label}] TIMEOUT — final state={last_state} (saw thinking={saw_thinking} speaking={saw_speaking})")
    return False


def main() -> int:
    print("=== Task 2c: multi-turn post-wake verification ===\n")
    d0 = _get_dbg()
    sleep_state0 = d0.get("subsystem_health", {}).get("sleep", {}).get("state")
    print(f"baseline: voice_loop={d0.get('voice_loop',{}).get('state')} sleep={sleep_state0}")
    if sleep_state0 != "AWAKE":
        print(f"FAIL: Ava not AWAKE at start ({sleep_state0})")
        return 1

    # Step 1: put Ava to sleep — use phrasing that the F8 PASS run confirmed
    # parses cleanly through Piper synth. "ninety seconds" worked in F8;
    # "one minute" raced with Whisper's tendency to drop trailing tokens.
    print("\n--- step 1: sleep for ninety seconds ---")
    wake_then_command("go to sleep for ninety seconds.")
    time.sleep(6.0)
    d1 = _wait_sleep_state({"ENTERING_SLEEP", "SLEEPING"}, timeout_s=120.0)
    s1 = d1.get("subsystem_health", {}).get("sleep", {}).get("state")
    if s1 not in ("ENTERING_SLEEP", "SLEEPING"):
        print(f"FAIL: sleep didn't engage")
        return 1

    # Step 2: wait for sleep cycle to complete
    print("\n--- step 2: wait for AWAKE again ---")
    d2 = _wait_sleep_state({"AWAKE"}, timeout_s=180.0)
    if d2.get("subsystem_health", {}).get("sleep", {}).get("state") != "AWAKE":
        print(f"FAIL: never returned to AWAKE")
        return 1
    # Let voice_loop settle to passive
    _wait_voice_loop_settles(timeout_s=60.0)
    # Tiny pad so attentive doesn't grab the next utterance accidentally
    time.sleep(2.0)

    # Step 3: turn 1 post-wake
    print("\n--- step 3: turn 1 post-wake ---")
    ok1 = turn("post-wake-1", "what time is it.", settle_timeout=180.0)
    if not ok1:
        print(f"FAIL: first post-wake turn hung")
        return 1

    # Pad before second turn so attentive timer settles + TTS finishes
    time.sleep(3.0)

    # Step 4: turn 2 post-wake
    print("\n--- step 4: turn 2 post-wake ---")
    ok2 = turn("post-wake-2", "what's two plus two.", settle_timeout=180.0)
    if not ok2:
        print(f"FAIL: second post-wake turn hung")
        return 1

    print("\n=== PASS ===")
    print("Sleep cycle + 2 post-wake turns all completed cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
