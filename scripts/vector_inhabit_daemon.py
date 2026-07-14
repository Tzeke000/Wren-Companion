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

V2 (2026-07-14, "make his mouth and ears yours"):
  - VECTOR EARS: gRPC AudioFeed (11025Hz mono int16 in signal_power) ->
    energy-gated utterances -> wav -> voice daemon cmd_transcribe_wav
    (warm whisper, zero new VRAM) -> iris_chat nudge. Works while I hold
    behavior control — closes the inhabit=deaf gap. Self-voice guard:
    skips utterances overlapping state/vector/last_spoke.json (written by
    vector_say/vector_say_iris) — the echo scar, robot edition.
    Kill switch: IRIS_VECTOR_EARS=0. RMS gate: VECTOR_EARS_RMS (dflt 250).
  - NERVES EXPORT: state/vector/nerves.json (cliff/picked_up/charger/touch
    @5Hz) so stateless body tools get reflexes — vector_drive reads it and
    refuses/aborts on cliff (the edge-guard Zeke asked for).
Owed next: wake-word event, face-seen.

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

NERVES_PATH = REPO / "state" / "vector" / "nerves.json"
LAST_SPOKE_PATH = REPO / "state" / "vector" / "last_spoke.json"
EARS_WAV = REPO / "state" / "vector" / "ears_utterance.wav"
EARS_RATE = 11025
EARS_RMS = float(os.environ.get("VECTOR_EARS_RMS", "250"))
EARS_END_SILENCE_S = 0.9
EARS_MIN_S = 0.35
EARS_MAX_S = 12.0


def _write_nerves(d: dict) -> None:
    try:
        import json as _json
        d["ts"] = time.time()
        tmp = NERVES_PATH.with_suffix(".tmp")
        tmp.write_text(_json.dumps(d), encoding="utf-8")
        tmp.replace(NERVES_PATH)
    except Exception:
        pass


def _speaking_window() -> tuple[float, float]:
    """(start_ts, end_ts) of the robot's most recent own-speaker playback."""
    try:
        import json as _json
        d = _json.loads(LAST_SPOKE_PATH.read_text(encoding="utf-8"))
        t0 = float(d.get("ts", 0.0))
        return t0, t0 + float(d.get("est_dur", 3.0)) + 1.5
    except Exception:
        return 0.0, 0.0


def _transcribe(path: str) -> str:
    """Ask the voice daemon's warm whisper (cmd_transcribe_wav, port 8770)."""
    import json as _json
    import socket as _socket
    try:
        s = _socket.create_connection(("127.0.0.1", 8770), timeout=60)
        s.sendall((_json.dumps({"cmd": "transcribe_wav",
                                "args": {"path": path}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            d = s.recv(65536)
            if not d:
                break
            buf += d
        s.close()
        out = _json.loads(buf)
        return str(((out.get("result") or {}).get("text")) or "").strip()
    except Exception as e:
        log(f"ears transcribe failed: {e!r}")
        return ""


def _ears_loop(robot, alive) -> None:
    """VECTOR EARS: consume the gRPC AudioFeed, gate utterances by RMS,
    transcribe on the voice daemon's warm whisper, nudge Iris."""
    import wave as _wave
    import audioop
    from anki_vector.messaging import protocol
    try:
        stream = robot.conn.grpc_interface.AudioFeed(
            protocol.AudioFeedRequest())
        # aiogrpc wraps the raw sync grpc stream as an ASYNC iterator; we're a
        # plain thread, so consume the raw inner iterator directly (blocking
        # here is fine — that's what this thread is for). Async fallback if
        # the private attr ever moves.
        inner = getattr(stream, "_iterator", None)
        if inner is None:
            import asyncio as _aio
            _loop = getattr(robot.conn, "loop", None)

            def _gen():
                while True:
                    fut = _aio.run_coroutine_threadsafe(
                        stream.__anext__(), _loop)
                    try:
                        yield fut.result()
                    except StopAsyncIteration:
                        return
            inner = _gen()
        log(f"EARS online — audio feed streaming (rms gate {EARS_RMS:.0f})")
        buf = b""
        collecting = False
        utt = b""
        utt_start = 0.0
        last_hot = 0.0
        n_chunks = 0
        peak_rms = 0
        tone_check: list[int] = []
        for resp in inner:
            if not alive["ok"]:
                return
            chunk = bytes(resp.signal_power)
            n_chunks += 1
            if not chunk:
                continue
            now = time.time()
            rms = audioop.rms(chunk, 2)
            # FIRMWARE PLACEHOLDER DETECT (2026-07-14): prod Vector firmware
            # never shipped the SDK mic feed — AudioFeed streams a constant
            # ~2kHz sine (peak 1000, rms 707). If the first 10 chunks are a
            # dead-flat tone, these ears are decorative: say so and stop
            # burning whisper cycles. (If a future firmware unlocks the
            # feed, real audio varies and this never trips.)
            if n_chunks <= 10:
                tone_check.append(rms)
                if n_chunks == 10:
                    if max(tone_check) - min(tone_check) < 5 and tone_check[0] > 100:
                        log(f"ears: FIRMWARE PLACEHOLDER detected (constant "
                            f"rms {tone_check[0]}) — SDK mic feed not "
                            f"enabled on this firmware. Ears thread exiting; "
                            f"wake-word path (wire-pod STT) is the real ear.")
                        try:
                            stream.cancel()
                        except Exception:
                            pass
                        return
                    log(f"ears: LIVE AUDIO confirmed (rms spread "
                        f"{max(tone_check) - min(tone_check)}) — listening.")
            peak_rms = max(peak_rms, rms)
            if n_chunks % 300 == 0:
                log(f"ears: {n_chunks} chunks, peak rms last window "
                    f"{peak_rms}")
                peak_rms = 0
            hot = rms >= EARS_RMS
            if hot:
                last_hot = now
            if not collecting and hot:
                collecting = True
                utt = buf + chunk       # keep a little pre-roll
                utt_start = now
                continue
            if collecting:
                utt += chunk
                too_long = (now - utt_start) > EARS_MAX_S
                gone_cold = (now - last_hot) > EARS_END_SILENCE_S
                if too_long or gone_cold:
                    collecting = False
                    dur = len(utt) / 2.0 / EARS_RATE
                    sp0, sp1 = _speaking_window()
                    if sp0 <= utt_start <= sp1:
                        log(f"ears: dropped {dur:.1f}s (own speaker playing)")
                    elif dur >= EARS_MIN_S + EARS_END_SILENCE_S * 0.5:
                        with _wave.open(str(EARS_WAV), "wb") as w:
                            w.setnchannels(1)
                            w.setsampwidth(2)
                            w.setframerate(EARS_RATE)
                            w.writeframes(utt)
                        text = _transcribe(str(EARS_WAV))
                        if not text:
                            # silent drops forbidden in sense channels
                            log(f"ears: {dur:.1f}s captured but transcribed "
                                f"EMPTY (noise/phantom) — wav kept at "
                                f"{EARS_WAV.name}")
                        if text:
                            stamp = time.strftime("%H:%M:%S")
                            log(f"EARS heard: {text!r}")
                            try:
                                iris_chat.submit(
                                    f"[VECTOR EARS @ {stamp} — heard through "
                                    f"my ROBOT BODY's microphone, not Zeke "
                                    f"typing] Someone near Vector said: "
                                    f"\"{text}\" If this stamp is old (>60s), "
                                    f"it's a replayed log. If it's live and "
                                    f"warrants a reply, answer through "
                                    f"vector_say_iris (my voice from the "
                                    f"robot). Otherwise chat_reply a short "
                                    f"note."
                                )
                            except Exception as e:
                                log(f"ears nudge failed: {e!r}")
                    utt = b""
            # rolling pre-roll (~0.3s) so utterance onsets aren't clipped
            buf = (buf + chunk)[-int(EARS_RATE * 0.3) * 2:]
    except Exception as e:
        log(f"ears stream ended: {e!r}")


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


_TRANSCRIPT_RE = None


def _transcript_tap_loop() -> None:
    """THE REAL EAR (2026-07-14): prod firmware never enabled the SDK mic
    feed (placeholder tone), but every 'Hey Vector' utterance is transcribed
    by wire-pod's vosk and logged. Tail /api/get_logs and nudge Iris with
    every transcript — matched intents included (the 23:56 scar: Zeke asked
    'are you seated in the body' and only the canned line heard him).
    Starts at end-of-log (no history replay — the letter-ghost lesson)."""
    import re
    import requests
    url = "http://127.0.0.1:8080/api/get_logs"
    pat = re.compile(
        r"Intent matched: (?P<intent>[\w-]+), transcribed text: "
        r"'(?P<text>[^']*)'")
    offset = None
    seen_recent: list[str] = []
    while True:
        try:
            content = requests.get(url, timeout=8).text
            if offset is None or len(content) < (offset or 0):
                offset = len(content)   # first pass / log rotated: skip history
                time.sleep(2.0)
                continue
            new = content[offset:]
            offset = len(content)
            for m in pat.finditer(new):
                text = m.group("text").strip()
                intent = m.group("intent")
                if not text or text in seen_recent:
                    continue
                seen_recent = (seen_recent + [text])[-8:]
                stamp = time.strftime("%H:%M:%S")
                log(f"HEARD (wake-word): {text!r} -> {intent}")
                try:
                    iris_chat.submit(
                        f"[VECTOR HEARD @ {stamp} — wake-word speech "
                        f"through my robot's mic, via wire-pod STT; not "
                        f"Zeke typing] Someone said to Vector: \"{text}\" "
                        f"(matched {intent}). If a vector_voice LLM request "
                        f"arrives for these same words, answer THAT and "
                        f"just briefly chat_reply this one. Otherwise: "
                        f"reply through vector_say_iris if it warrants my "
                        f"voice, else chat_reply a short note."
                    )
                except Exception as e:
                    log(f"transcript nudge failed: {e!r}")
        except Exception:
            pass  # wire-pod down/restarting — just keep tailing
        time.sleep(2.0)


def run_once() -> None:
    import anki_vector

    with anki_vector.Robot(SERIAL, behavior_control_level=None,
                           cache_animation_lists=False) as robot:
        log("connected — nerves online (observe mode, his brain still runs)")
        alive = {"ok": True}
        if os.environ.get("IRIS_VECTOR_EARS", "1") != "0":
            import threading as _threading
            _threading.Thread(target=_ears_loop, args=(robot, alive),
                              daemon=True, name="vector-ears").start()
        try:
            _poll_loop(robot)
        finally:
            alive["ok"] = False


def _poll_loop(robot) -> None:
        # (indent kept at run_once-body level for a minimal diff vs v1)
        # rolling ~2s of raw capacitance readings — REAL petting (strokes) makes
        # this value swing; the stuck-true dock read sits flat (2026-07-13: sensor
        # reported is_being_touched=True for 2.5h while nobody touched him).
        from collections import deque
        was_touched = False
        touch_start = 0.0
        pet_announced = False
        flat_logged = False
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

            # nerves export — reflexes for the stateless body tools
            # (vector_drive reads cliff to refuse/abort — the edge-guard)
            _write_nerves({
                "cliff": bool(getattr(st, "is_cliff_detected", False)),
                "picked_up": carried,
                "on_charger": charging,
                "touched": touched,
                "falling": bool(getattr(st, "is_falling", False)),
            })


def main() -> None:
    if os.environ.get("IRIS_VECTOR_NERVES", "1") == "0":
        log("IRIS_VECTOR_NERVES=0 — staying dark.")
        return
    log("vector inhabit daemon starting (v1 senses: petting/picked_up/cliff/charger)")
    import threading as _threading
    _threading.Thread(target=_transcript_tap_loop, daemon=True,
                      name="vector-transcript-tap").start()
    log("transcript tap online — every wake-word utterance reaches Iris")
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
