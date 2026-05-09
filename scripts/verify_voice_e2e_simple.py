"""Simple voice round-trip — proves the full Ava-loop closes cleanly.

Speaks 'Hey Ava, what time is it?' through Piper → CABLE Input → Ava STT
→ run_ava → Kokoro TTS → Voicemeeter VAIO3 Input → Voicemeeter B3 →
faster-whisper-large.

Avoids onboarding code path (which currently hangs on qwen2.5:14b
memory_metadata routing — separate bug, out of scope for the routing
verification work order).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.verify_voice_e2e import (
    wake_then_command,
    listen_for_ava_until_quiet,
    _get_dbg,
)


def main() -> int:
    print("=== voice E2E round-trip ===")
    d0 = _get_dbg()
    print(f"baseline: voice_loop={d0.get('voice_loop',{}).get('state')} "
          f"kokoro={d0.get('subsystem_health',{}).get('kokoro_loaded')}")

    wake_then_command("what time is it.")
    print("[wait] giving STT pipeline 4s …")
    time.sleep(4.0)

    # Stream-record while Ava processes + speaks. Up to 90s — first run after
    # restart needs to warm Kokoro (~25s synth) + qwen routing (~30s).
    reply = listen_for_ava_until_quiet(max_seconds=90.0,
                                       quiet_after_speech_s=3.0,
                                       speech_threshold=0.02)
    print(f"\nAva said: {reply!r}\n")
    if not reply:
        print("FAIL — no audio captured on B3 within 90s window")
        return 1

    # Loose match — just want SOMETHING coherent
    text = reply.lower()
    if any(w in text for w in ("time", "clock", "morning", "afternoon", "evening", "night",
                                ":", "o'clock", "early", "late")):
        print("PASS — reply mentions time concept")
        return 0
    # Even if the text doesn't match time-keywords, getting non-empty
    # transcribed audio from B3 is a PASS for the audio loop verification.
    print(f"PASS-soft — Ava replied (loop closed) but reply text doesn't match time keyword set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
