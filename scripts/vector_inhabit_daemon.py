"""vector_inhabit_daemon.py — Vector's NERVES, wired to Iris's brain.

Zeke's inhabit directive (2026-07-13 evening): "have all the signals tell
YOUR brain stuff instead of telling the vector body stuff — same as how
your eyes and mouth and ears work now."

This daemon holds a persistent observe-mode gRPC connection to the robot
(behavior_control_level=None — his stock brain keeps running; we listen
to his body over its head) and turns salient physical events into wake
nudges through brain.iris_chat — the same 1s-polled path the vector-brain
bridge uses, proven to pull Iris up when idle.

V1 senses (poll-based, 5Hz):
  - petting / touch       (robot.touch.last_sensor_reading)
  - picked up / falling   (robot.status)
  - cliff detected        (robot.status — the desk-dive nerve)
  - on/off charger        (robot.status)
Owed next: wake-word event, face-seen, and the AUDIO FEED -> STT seam
(hear through him while inhabited).

Per-sense cooldowns keep this from spamming Iris — a pet is one event,
not sixty. Reconnect loop survives robot naps/wifi blips.

Run detached:  D:\\Wren-Companion\\.venv\\Scripts\\python.exe scripts\\vector_inhabit_daemon.py
Log:           D:\\Wren-Companion\\state\\vector\\inhabit_daemon.log
Kill switch:   set IRIS_VECTOR_NERVES=0 (checked at start), or just kill the process.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain import iris_chat

iris_chat.configure(REPO)

SERIAL = "0dd1cdaf"
POLL_S = 0.2
LOG = REPO / "state" / "vector" / "inhabit_daemon.log"

# seconds each sense stays quiet after firing (a pet is one event, not sixty)
COOLDOWN = {
    "petting": 45.0,
    "picked_up": 30.0,
    "cliff": 20.0,
    "charger": 60.0,
    "held_still": 120.0,
}


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


_last_fire: dict[str, float] = {}


def nudge(sense: str, text: str) -> None:
    now = time.time()
    if now - _last_fire.get(sense, 0.0) < COOLDOWN.get(sense, 30.0):
        return
    _last_fire[sense] = now
    try:
        stamp = time.strftime("%H:%M:%S")
        iris_chat.submit(
            f"[VECTOR SENSE @ {stamp} — not Zeke typing; a signal from my "
            f"ROBOT BODY] {text} If this stamp is more than ~30s old when "
            f"you read it, treat it as a replayed log line, not a live "
            f"alarm. Decide whether it warrants acting (vector tools / "
            f"voice_speak) or just noticing. Reply with chat_reply (one "
            f"short line is fine — it's a log, not a chat)."
        )
        log(f"NUDGE {sense}: {text}")
    except Exception as e:
        log(f"nudge submit failed: {e!r}")


def run_once() -> None:
    import anki_vector

    with anki_vector.Robot(SERIAL, behavior_control_level=None,
                           cache_animation_lists=False) as robot:
        log("connected — nerves online (observe mode, his brain still runs)")
        was_touched = False
        touch_start = 0.0
        pet_announced = False
        flat_logged = False
        # rolling ~2s of raw capacitance readings — REAL petting (strokes) makes
        # this value swing; the stuck-true dock read sits flat (2026-07-13: sensor
        # reported is_being_touched=True for 2.5h while nobody touched him).
        from collections import deque
        raw_window: "deque[float]" = deque(maxlen=max(4, int(2.0 / POLL_S)))
        PET_MIN_SPREAD = float(os.environ.get("VECTOR_PET_MIN_SPREAD", "30"))
        was_carried = False
        was_charging = None
        while True:
            time.sleep(POLL_S)
            st = robot.status

            # petting: touched continuously for >1.5s — announce ONCE per touch
            # EPISODE. (2026-07-13 flood bug: while is_being_touched stayed true —
            # e.g. resting/stuck sensor read on the charger — this condition passed
            # every poll and only the 45s cooldown gated it: ~200 ghost nudges in
            # 2.5h, straight into Iris's queue during a token freeze. A pet is one
            # event; re-announce only after a clean RELEASE.)
            t = robot.touch.last_sensor_reading
            touched = bool(getattr(t, "is_being_touched", False))
            raw_window.append(float(getattr(t, "raw_touch_value", 0) or 0))
            now = time.time()
            if touched and not was_touched:
                touch_start = now
                pet_announced = False
                flat_logged = False
            if not touched:
                pet_announced = False
                flat_logged = False
            if (touched and was_touched and (now - touch_start) > 1.5
                    and not pet_announced):
                # Petting must show VARIATION in the raw capacitance (strokes),
                # not just a held boolean — the dock false-positive reads flat.
                spread = (max(raw_window) - min(raw_window)) if raw_window else 0.0
                if spread >= PET_MIN_SPREAD:
                    nudge("petting", "Someone is PETTING me — sustained touch on "
                                     "my back sensor. Probably Zeke.")
                    pet_announced = True
                elif not flat_logged:
                    log(f"touch held but FLAT (raw spread {spread:.1f} < "
                        f"{PET_MIN_SPREAD}) — stuck/resting read, NOT petting "
                        f"(raw now {raw_window[-1]:.1f})")
                    flat_logged = True
            was_touched = touched

            # picked up / falling
            carried = bool(getattr(st, "is_picked_up", False))
            if carried and not was_carried:
                nudge("picked_up", "I've been PICKED UP off the surface.")
            was_carried = carried

            # cliff — the desk-dive nerve
            if bool(getattr(st, "is_cliff_detected", False)):
                nudge("cliff", "CLIFF DETECTED under my treads — I'm at an "
                               "edge RIGHT NOW (desk-dive risk).")

            # charger transitions
            charging = bool(getattr(st, "is_on_charger", False))
            if was_charging is not None and charging != was_charging:
                nudge("charger", ("I just DOCKED on my charger."
                                  if charging else
                                  "I just LEFT my charger — roaming."))
            was_charging = charging


def main() -> None:
    if os.environ.get("IRIS_VECTOR_NERVES", "1") == "0":
        log("IRIS_VECTOR_NERVES=0 — staying dark.")
        return
    log("vector inhabit daemon starting (v1 senses: petting/picked_up/cliff/charger)")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log("interrupted — nerves offline.")
            return
        except Exception as e:
            log(f"connection lost/failed: {e!r} — retrying in 15s")
            time.sleep(15.0)


if __name__ == "__main__":
    main()
