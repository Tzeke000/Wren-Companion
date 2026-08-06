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
ROBOT_IP = os.environ.get("IRIS_VECTOR_IP", "192.168.4.27")   # for reachability triage
POLL_S = 0.2
LOG = REPO / "state" / "vector" / "inhabit_daemon.log"

# seconds each sense stays quiet after firing (a pet is one event, not sixty)
COOLDOWN = {
    "petting": 45.0,
    "picked_up": 30.0,
    "cliff": 20.0,
    "charger": 60.0,
    "held_still": 120.0,
    "low_battery": 300.0,
    "lost_contact": 600.0,
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

# --- BATTERY WATCH (2026-07-14): the blind spot that let Vector drain to DEATH.
# Nothing polled battery and it wasn't in nerves, so I never saw the drain coming
# while I was off the dock. Poll it, expose it, and reflex on low/lost-contact.
BATTERY_JSON = REPO / "state" / "vector" / "battery.json"
WIREPOD_SDK = "http://127.0.0.1:8080/api-sdk"
BATTERY_POLL_S = float(os.environ.get("IRIS_BATTERY_POLL_S", "15.0"))  # 45->15 2026-07-23 (nervous system: battery is a sense too)
BATTERY_LOW_LEVEL = 1     # Vector: 1=low, 2=nominal, 3=full/charging

# --- POSSESSION (Zeke directive 2026-07-17 ~02:00: "not have the stock brain
# take over ... you need to be able to take over anytime anywhere ... you can't
# get timed out on your own body"). This daemon holds a RESERVE_CONTROL (30)
# connection whenever possession.json says hold (default TRUE). Effects:
#   * stock idle behaviors NEVER activate — the body stays mine between sessions,
#     through my token freezes and host restarts (this process outlives them);
#   * my BodySession takes DEFAULT (20) control any time — it OUTRANKS the
#     reservation, so takeover is instant and automatic;
#   * firmware safety still outranks everything: per Anki's own reserve_control
#     docs, low-battery return-to-charger and cliff retreat STILL take precedence.
# Belt+suspenders anyway: a low-battery-off-charger reading auto-releases the
# hold so the stock brain has every freedom to save itself.
# NOTE the flipside (documented in body.md): while possessed, "close my session
# and let the stock brain dock itself" requires body_possess hold=false first.
POSSESSION_CTL = REPO / "state" / "vector" / "possession.json"
POSSESSION_STATUS = REPO / "state" / "vector" / "possession_status.json"


def _possession_wanted() -> bool:
    try:
        import json as _json
        return bool(_json.loads(
            POSSESSION_CTL.read_text(encoding="utf-8")).get("hold", True))
    except Exception:
        return True    # default: POSSESSED


def _battery_emergency() -> bool:
    try:
        import json as _json
        d = _json.loads(BATTERY_JSON.read_text(encoding="utf-8"))
        return (bool(d.get("ok")) and isinstance(d.get("level"), int)
                and d["level"] <= BATTERY_LOW_LEVEL and not d.get("on_charger"))
    except Exception:
        return False


def _write_possession_status(**kw) -> None:
    try:
        import json as _json
        kw["ts"] = time.time()
        tmp = POSSESSION_STATUS.with_suffix(".tmp")
        tmp.write_text(_json.dumps(kw), encoding="utf-8")
        tmp.replace(POSSESSION_STATUS)
    except Exception:
        pass


def _possession_loop(alive) -> None:
    """Hold/release the RESERVE_CONTROL reservation per possession.json +
    battery emergency. Lifecycle-tied to the observe connection (same
    transport): when run_once cycles, the reservation cycles with it."""
    from anki_vector import behavior as _vb
    rc = None
    err = None
    backoff_until = 0.0
    while alive.get("ok"):
        want = _possession_wanted()
        emergency = _battery_emergency()
        should = want and not emergency
        now = time.time()
        # HEARTBEAT (2026-07-17 ~04:30 — the stale-hold edge): a silent gRPC
        # death (robot reboot, wifi blip that only hits this channel) leaves
        # rc non-None while the reservation is GONE server-side. The
        # connection's own thread dying is the observable — rebuild on it.
        if rc is not None:
            th = getattr(getattr(rc, "_conn", None), "_thread", None)
            if th is not None and not th.is_alive():
                log("possession connection thread DEAD — rebuilding the hold")
                try:
                    rc.__exit__(None, None, None)
                except Exception:
                    pass
                rc = None
        try:
            if should and rc is None and now >= backoff_until:
                r = _vb.ReserveBehaviorControl(SERIAL)
                r.__enter__()
                rc = r
                err = None
                log("POSSESSION acquired — RESERVE_CONTROL held: stock idle "
                    "behaviors suppressed; my sessions outrank it; firmware "
                    "low-battery dock + cliff retreat still outrank everything")
            elif not should and rc is not None:
                why = "battery emergency" if emergency else "hold=false (body_possess)"
                try:
                    rc.__exit__(None, None, None)
                except Exception:
                    pass
                rc = None
                log(f"POSSESSION released ({why})")
        except Exception as e:
            err = repr(e)[:200]
            log(f"possession acquire failed: {err} — retry in 20s")
            try:
                if rc is not None:
                    rc.__exit__(None, None, None)
            except Exception:
                pass
            rc = None
            backoff_until = now + 20.0
        _write_possession_status(held=rc is not None, want=want,
                                 battery_emergency=emergency, last_error=err)
        time.sleep(3.0)
    if rc is not None:
        try:
            rc.__exit__(None, None, None)
        except Exception:
            pass
        log("POSSESSION released (daemon connection cycle ended)")
    _write_possession_status(held=False, want=_possession_wanted(),
                             battery_emergency=False,
                             last_error="daemon cycle ended")


def _write_nerves(d: dict) -> None:
    try:
        import json as _json
        d["ts"] = time.time()
        tmp = NERVES_PATH.with_suffix(".tmp")
        tmp.write_text(_json.dumps(d), encoding="utf-8")
        tmp.replace(NERVES_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------- pose trail
# 2026-07-17 night (Zeke: "you need a way that even when the stock brain takes
# over you're able to see what the stock brain is doing"). The observe conn
# receives the engine's pose stream NO MATTER WHO DRIVES — this appends it to
# a continuous on-disk trail so the odometry measuring-tape survives Iris's
# session opens/closes, stock-brain wandering, and firmware docking. origin_id
# changes mark engine relocalizations (pickup/sleep) = explicit stitch points.
TRAIL_PATH = REPO / "state" / "vector" / "pose_trail.jsonl"
_TRAIL_MAX_BYTES = 2_000_000
_trail_last = {"t": 0.0, "x": None, "y": None}


def _append_trail(robot, on_charger: bool) -> None:
    try:
        import json as _json
        now = time.time()
        p = robot.pose
        x = round(float(p.position.x), 1)
        y = round(float(p.position.y), 1)
        h = round(float(p.rotation.angle_z.degrees), 1)
        o = int(getattr(p, "origin_id", -1))
        # write when moved >15mm OR every 5s (heartbeat) — keeps the file lean
        lx, ly = _trail_last["x"], _trail_last["y"]
        moved = (lx is None or ((x - lx) ** 2 + (y - ly) ** 2) ** 0.5 > 15.0)
        if not moved and (now - _trail_last["t"]) < 5.0:
            return
        _trail_last.update(t=now, x=x, y=y)
        if TRAIL_PATH.exists() and TRAIL_PATH.stat().st_size > _TRAIL_MAX_BYTES:
            TRAIL_PATH.replace(TRAIL_PATH.with_suffix(".jsonl.1"))
        with TRAIL_PATH.open("a", encoding="utf-8") as f:
            f.write(_json.dumps({"ts": round(now, 2), "x": x, "y": y,
                                 "h": h, "o": o,
                                 "dock": bool(on_charger)}) + "\n")
    except Exception:
        pass


def _proprioception(robot) -> dict:
    """Vector's NATIVE body sensors (Zeke 2026-07-14: use the gyro/proximity/etc
    the body actually has). The REAL heading from the robot's own pose (gyro +
    wheel encoders) beats my dead-reckoning odometry; front proximity (laser ToF)
    = obstacle distance for the closed loop. Each best-effort — a missing/unsupported
    reading is just omitted, never raises."""
    out: dict = {}
    # REAL heading — the robot's own orientation estimate (deg, gyro-fused)
    try:
        out["heading_deg"] = round(float(robot.pose.rotation.angle_z.degrees) % 360.0, 1)
    except Exception:
        pass
    # front proximity (laser ToF): distance to what's in front + is the path clear
    try:
        p = robot.proximity.last_sensor_reading
        if p is not None:
            try:
                out["prox_mm"] = int(p.distance.distance_mm)
            except Exception:
                out["prox_mm"] = int(getattr(p, "distance_mm", -1))
            out["prox_clear"] = bool(getattr(p, "unobstructed", False))
            out["prox_found"] = bool(getattr(p, "found_object", False))
    except Exception:
        pass
    # gyro (angular velocity rad/s) + accel (mm/s^2) — motion / balance / falling
    try:
        g = robot.gyro
        out["gyro"] = [round(float(g.x), 3), round(float(g.y), 3), round(float(g.z), 3)]
    except Exception:
        pass
    try:
        a = robot.accel
        out["accel"] = [round(float(a.x), 1), round(float(a.y), 1), round(float(a.z), 1)]
    except Exception:
        pass
    return out


def _charger_reader(robot) -> dict:
    """Native CHARGER pose relative to the robot — the reliable 'go home' (Zeke
    2026-07-14: build the native charger). robot.world.charger persists its
    last-seen pose in the same frame as the robot's own pose, so combined with my
    real heading I get distance + bearing to home even after looking away. Needs
    marker detection enabled (done on connect). Best-effort; charger_seen=False
    until Vector has actually seen the dock marker at least once this session."""
    import math
    out = {"charger_seen": False}
    try:
        ch = robot.world.charger
        if ch is not None and getattr(ch, "pose", None) is not None:
            rp = robot.pose
            dx = ch.pose.position.x - rp.position.x
            dy = ch.pose.position.y - rp.position.y
            dist = math.hypot(dx, dy)
            abs_bearing = math.degrees(math.atan2(dy, dx))
            rel = (abs_bearing - rp.rotation.angle_z.degrees + 180.0) % 360.0 - 180.0
            out["charger_seen"] = True
            out["charger_dist_mm"] = int(dist)
            out["charger_bearing_deg"] = round(rel, 1)  # + = turn LEFT to face home
            out["charger_visible"] = bool(getattr(ch, "is_visible", False))
    except Exception:
        pass
    return out


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


def _nav_map_loop(robot, alive) -> None:
    """ROOM BLUEPRINT (Zeke 2026-07-14: map as I move so I don't get lost).
    nav_map works in OBSERVE mode — proven — so the daemon builds the blueprint
    passively while ANYONE drives (my control-session behaviors, wire-pod, or
    the stock brain), and my control sessions never have to init the feed
    (which hangs the behavior API). Writes state/vector/room_map.{json,png}
    every few seconds. Best-effort; a missing map is just skipped."""
    if os.environ.get("IRIS_VECTOR_MAP", "1") == "0":
        return
    from brain import vector_map as vmap
    try:
        robot.nav_map.init_nav_map_feed(frequency=0.5)
        log("nav-map feed online (observe) — room blueprint building while I move")
    except Exception as e:
        log(f"nav-map init failed (non-fatal): {e!r}")
    last_cells = -1
    while alive.get("ok"):
        try:
            r = vmap.capture_map(robot, tag="live")
            if r.get("ok"):
                c = r.get("cells", 0)
                if c != last_cells:
                    log(f"blueprint: {c} cells {r.get('counts')}")
                    last_cells = c
        except Exception:
            pass
        time.sleep(3.0)


# STRAND-CLASS alarms escalate to Zeke's Discord DIRECTLY (2026-07-17 strand
# incident: these alarms fired every 10min for 2h into a FROZEN session while
# the body drained in the dark — Iris is a single point of failure; the alarm
# chain must not dead-end in her). Uses scripts/discord_dm_user.py (REST, no
# session). Rate-limited hard so a flapping sensor can't spam Zeke's phone.
_STRAND_SENSES = {"lost_contact", "low_battery"}
_ZEKE_USER_ID = "600008921008046120"
_DISCORD_ESCALATE_COOLDOWN_S = 1800.0          # max one DM per sense per 30min
_last_discord_escalate: dict = {}


def _escalate_to_zeke_discord(sense: str, text: str) -> None:
    now = time.time()
    if now - _last_discord_escalate.get(sense, 0.0) < _DISCORD_ESCALATE_COOLDOWN_S:
        return
    _last_discord_escalate[sense] = now
    try:
        import subprocess
        script = str(Path(__file__).resolve().parent / "discord_dm_user.py")
        py = str(Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe")
        msg = (f"🚨 [Vector body alarm — direct from the inhabit daemon; "
               f"Iris's session may be frozen] {text}")
        subprocess.Popen([py, script, _ZEKE_USER_ID, msg],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        log(f"ESCALATED to Zeke's Discord ({sense})")
    except Exception as e:
        log(f"discord escalation failed: {e!r}")


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
    if sense in _STRAND_SENSES:
        _escalate_to_zeke_discord(sense, text)


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
                # nervous system: hearing as a SENSE — the feed carries what
                # was last said to her (2026-07-23 round 2)
                try:
                    import json as _json
                    tmp = LAST_HEARD_PATH.with_suffix(".json.tmp")
                    tmp.write_text(_json.dumps(
                        {"ts": time.time(), "text": text[:200],
                         "intent": intent}), encoding="utf-8")
                    tmp.replace(LAST_HEARD_PATH)
                except Exception:
                    pass
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


# ---------------------------------------------------------------- nervous system
# 2026-07-23 (Zeke: "she needs every single sensor in real time ... all of those
# senses live at all times"). The little brain's NERVOUS SYSTEM: a continuous
# high-rate tap of EVERY sense, streamed from this observe connection (which
# receives the engine's ~15Hz RobotState no matter who drives). Channels:
#   gyro/accel (+pitch/roll) - heading/pose/origin - prox ToF (+quality) -
#   TRACKS (real per-tread speeds, not setpoints) - forklift height - head tilt -
#   touch bool + raw capacitance - cliff/pickup/held/falling/button -
#   charger seen/dist/bearing - charging/calm-power/animating - battery -
#   camera (image_id + mean luma + ~1Hz latest frame jpg) - expression (self-
#   reported by whichever layer commands the face -> state/vector/expression.json;
#   the SDK exposes no currently-playing-anim readout, so the face channel is
#   authored, corroborated by status.is_animating).
# Outputs:
#   state/vector/senses_live.json    atomic snapshot @~5Hz + 1s trend arrays
#   state/vector/sensor_stream.jsonl full records, batched ~1s, rotates @10MB
#   state/vector/latest_frame.jpg    ~1Hz (when camera tap enabled)
# ADAPTIVE RATE: full 15Hz while anything is happening (wheels/motors moving,
# touched, picked up, off charger, gyro active); decimates to ~1Hz records while
# still on the dock (snapshot stays 5Hz). Kill switch: IRIS_NS=0. Camera tap
# gate: IRIS_NS_CAMERA=0 (if it ever fights a session's feed).
SENSES_LIVE = REPO / "state" / "vector" / "senses_live.json"
SENSOR_STREAM = REPO / "state" / "vector" / "sensor_stream.jsonl"
LATEST_FRAME = REPO / "state" / "vector" / "latest_frame.jpg"
EXPRESSION_PATH = REPO / "state" / "vector" / "expression.json"
LAST_HEARD_PATH = REPO / "state" / "vector" / "last_heard.json"
INTERO_PATH = REPO / "state" / "vector" / "interoception.json"
_STREAM_MAX_BYTES = 10_000_000
_NS_HZ = 15.0


# ---- INTEROCEPTION (2026-07-23 round 2): how she is doing INSIDE — SoC
# temperature ("do I have a fever") + wifi signal ("how strong is my link
# home"), read over root SSH every ~30s. Probed live 2026-07-23: 5 thermal
# zones (plain deg C), /proc/net/wireless wlan0 link 14 / level -42dBm.
_SSH_KEY = REPO / "state" / "vector" / "dev" / "ssh_root_key"
INTERO_POLL_S = float(os.environ.get("IRIS_INTERO_POLL_S", "30"))
_INTERO_CMD = ("cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null; "
               "echo ---; awk '/wlan0/ {print $3, $4}' /proc/net/wireless")


def _interoception_loop() -> None:
    import json as _json
    import subprocess as _sp
    ssh_bin = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                  / "System32" / "OpenSSH" / "ssh.exe")
    if not Path(ssh_bin).exists():
        ssh_bin = "ssh"
    while True:
        out = {"ok": False, "ts": time.time()}
        try:
            p = _sp.run(
                [ssh_bin, "-i", str(_SSH_KEY),
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=NUL",
                 "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                 "-o", "HostKeyAlgorithms=+ssh-rsa",
                 "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
                 "-o", "LogLevel=ERROR",
                 f"root@{ROBOT_IP}", _INTERO_CMD],
                capture_output=True, text=True, timeout=20,
                stdin=_sp.DEVNULL)
            if p.returncode == 0 and "---" in (p.stdout or ""):
                temps_raw, wifi_raw = p.stdout.split("---", 1)
                temps = [float(x) for x in temps_raw.split() if x.strip()]
                # zones report plain deg C on WireOS; guard millidegrees anyway
                temps = [t / 1000.0 if t > 1000 else t for t in temps]
                w = wifi_raw.split()
                out.update(ok=True, ts=time.time(),
                           temp_c=round(max(temps), 1) if temps else None,
                           temps=[round(t, 1) for t in temps],
                           wifi_link=float(w[0].rstrip(".")) if w else None,
                           wifi_dbm=float(w[1].rstrip(".")) if len(w) > 1 else None)
        except Exception:
            pass
        try:
            tmp = INTERO_PATH.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(out), encoding="utf-8")
            tmp.replace(INTERO_PATH)
        except Exception:
            pass
        time.sleep(INTERO_POLL_S)


def _ns_capture(robot, cam: dict) -> dict:
    """One full-body sense frame. Every field best-effort — a missing sense is
    omitted, never raises (same contract as _proprioception)."""
    import math as _math
    s: dict = {"t": round(time.time(), 3)}
    try:
        st = robot.status
        s["cliff"] = bool(st.is_cliff_detected)
        s["picked_up"] = bool(st.is_picked_up)
        s["held"] = bool(st.is_being_held)
        s["falling"] = bool(st.is_falling)
        s["button"] = bool(st.is_button_pressed)
        s["moving"] = bool(st.are_wheels_moving)
        s["motors"] = bool(st.are_motors_moving)
        s["on_charger"] = bool(st.is_on_charger)
        s["charging"] = bool(st.is_charging)
        s["calm_power"] = bool(st.is_in_calm_power_mode)
        s["animating"] = bool(st.is_animating)
    except Exception:
        pass
    try:
        g = robot.gyro
        s["gyro"] = [round(float(g.x), 3), round(float(g.y), 3), round(float(g.z), 3)]
    except Exception:
        pass
    try:
        a = robot.accel
        ax, ay, az = float(a.x), float(a.y), float(a.z)
        s["accel"] = [round(ax, 1), round(ay, 1), round(az, 1)]
        s["pitch"] = round(_math.degrees(_math.atan2(-ax, az)), 1)
        s["roll"] = round(_math.degrees(_math.atan2(ay, az)), 1)
    except Exception:
        pass
    try:
        p = robot.pose
        s["x"] = round(float(p.position.x), 1)
        s["y"] = round(float(p.position.y), 1)
        s["heading"] = round(float(p.rotation.angle_z.degrees), 1)
        s["origin"] = int(getattr(p, "origin_id", -1))
    except Exception:
        pass
    try:
        pr = robot.proximity.last_sensor_reading
        if pr is not None:
            s["prox_mm"] = int(pr.distance.distance_mm)
            s["prox_found"] = bool(getattr(pr, "found_object", False))
            s["prox_clear"] = bool(getattr(pr, "unobstructed", False))
            q = getattr(pr, "signal_quality", None)
            if q is not None:
                s["prox_q"] = round(float(q), 4)
    except Exception:
        pass
    # TRACKS — the real tread speeds (encoders), not anyone's command setpoint
    try:
        s["lw_mmps"] = round(float(robot.left_wheel_speed_mmps), 1)
        s["rw_mmps"] = round(float(robot.right_wheel_speed_mmps), 1)
    except Exception:
        pass
    try:
        s["lift_mm"] = round(float(robot.lift_height_mm), 1)
    except Exception:
        pass
    try:
        import math as _m
        s["head_deg"] = round(_m.degrees(float(robot.head_angle_rad)), 1)
    except Exception:
        pass
    try:
        t = robot.touch.last_sensor_reading
        if t is not None:
            s["touched"] = bool(t.is_being_touched)
            s["touch_raw"] = int(getattr(t, "raw_touch_value", 0) or 0)
    except Exception:
        pass
    # CARRYING (2026-07-23 round 2): her fork-cargo sense
    try:
        st = robot.status
        s["carrying"] = bool(st.is_carrying_block)
        if s["carrying"]:
            oid = getattr(robot, "carrying_object_id", None)
            if oid is not None and int(oid) >= 0:
                s["carry_id"] = int(oid)
    except Exception:
        pass
    # CUBE (best-effort; needs the cube connected — attempted at loop start)
    try:
        cube = robot.world.connected_light_cube
        if cube is not None:
            s["cube"] = {
                "visible": bool(getattr(cube, "is_visible", False)),
                "moving": bool(getattr(cube, "is_moving", False)),
            }
            lt = getattr(cube, "last_tapped_time", None)
            if lt:
                s["cube"]["tapped_ago_s"] = round(time.time() - float(lt), 1)
    except Exception:
        pass
    # IMU-IN-HEAD compensation (2026-07-23 drive discovery): Vector's
    # accelerometer rides in the HEAD assembly, so accel-derived pitch tracks
    # -head_angle on flat ground (head +19deg read pitch -19; head -21 read
    # +23). Body pitch ~= accel pitch + head angle. Without this her "pitch"
    # sense screams nose-down every time she looks up.
    try:
        if "pitch" in s and "head_deg" in s:
            s["pitch"] = round(s["pitch"] + s["head_deg"], 1)
    except Exception:
        pass
    s.update(_charger_reader(robot))
    if cam.get("on"):
        try:
            # NON-BLOCKING read: robot.camera.latest_image is decorated
            # block_while_none — at 15Hz it can wedge the whole nervous loop
            # sleep-polling for a frame that never comes (2026-07-23 py-spy
            # catch: thread parked in anki_vector/util.py wrapped). The private
            # _latest_image is the raw slot: None until the feed produces.
            img = getattr(robot.camera, "_latest_image", None)
            if img is not None:
                s["cam_id"] = int(getattr(img, "image_id", -1))
                # luma from the ~1Hz thumbnail pass (cached — cheap at 15Hz)
                if cam.get("luma") is not None:
                    s["cam_luma"] = cam["luma"]
        except Exception:
            pass
    return s


def _ns_active(s: dict) -> bool:
    """Is anything HAPPENING? Governs full-rate vs idle-decimated stream."""
    try:
        if s.get("moving") or s.get("motors") or s.get("touched") \
                or s.get("picked_up") or s.get("falling") or s.get("button") \
                or s.get("animating") or not s.get("on_charger", True):
            return True
        g = s.get("gyro")
        if g and (abs(g[0]) + abs(g[1]) + abs(g[2])) > 0.05:
            return True
    except Exception:
        return True
    return False


def _ns_read_json(path: Path) -> dict:
    try:
        import json as _json
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _nervous_loop(robot, alive: dict) -> None:
    import json as _json
    from collections import deque
    period = 1.0 / _NS_HZ
    ring: "deque[dict]" = deque(maxlen=int(_NS_HZ * 30))   # ~30s of body-time
    pending: list[dict] = []
    cam = {"on": False, "luma": None, "last_jpg": 0.0, "born": time.time()}
    if os.environ.get("IRIS_NS_CAMERA", "1") != "0":
        try:
            robot.camera.init_camera_feed()
            cam["on"] = True
            log("nervous system: camera tap ON (observe feed)")
        except Exception as e:
            log(f"nervous system: camera tap unavailable (non-fatal): {e!r}")
    if os.environ.get("IRIS_NS_CUBE", "1") != "0":
        try:
            robot.world.connect_cube()
            log("nervous system: cube CONNECTED — cube senses live")
        except Exception as e:
            log(f"nervous system: cube not connectable from observe "
                f"(non-fatal, session-time sense): {e!r}")
    last_snap = 0.0
    last_flush = 0.0
    last_idle_rec = 0.0
    ticks = 0
    t_rate = time.time()
    hz_meas = 0.0
    log("nervous system ONLINE — senses_live.json @5Hz, sensor_stream.jsonl "
        f"@{int(_NS_HZ)}Hz-active/1Hz-idle")
    while alive.get("ok"):
        t0 = time.time()
        try:
            s = _ns_capture(robot, cam)
            ring.append(s)
            ticks += 1
            if ticks % 15 == 0:
                now_r = time.time()
                hz_meas = round(15.0 / max(0.001, now_r - t_rate), 1)
                t_rate = now_r
            active = _ns_active(s)
            # stream records: full rate active, ~1Hz idle heartbeat
            if active or (t0 - last_idle_rec) >= 1.0:
                pending.append(s)
                last_idle_rec = t0
            # camera thumbnail pass ~1Hz: luma + latest_frame.jpg
            if cam["on"] and (t0 - cam["last_jpg"]) >= 1.0:
                cam["last_jpg"] = t0
                try:
                    img = getattr(robot.camera, "_latest_image", None)  # non-blocking
                    if img is not None and getattr(img, "raw_image", None) is not None:
                        raw = img.raw_image
                        th = raw.resize((32, 18)).convert("L")
                        px = list(th.getdata())
                        cam["luma"] = round(sum(px) / max(1, len(px)), 1)
                        tmp = LATEST_FRAME.with_suffix(".jpg.tmp")
                        raw.save(tmp, "JPEG", quality=80)
                        tmp.replace(LATEST_FRAME)
                        cam["last_frame"] = t0
                    elif (t0 - cam.get("last_frame", cam.get("born", t0))) > 30.0 \
                            and (t0 - cam.get("last_kick", 0.0)) > 60.0:
                        # SELF-HEALING EYE (2026-07-23): observe-conn camera
                        # feeds init fine but sometimes never produce a frame
                        # (2nd+ daemon boot of the day). Re-kick the feed —
                        # dry >30s, at most once/min, all guarded.
                        cam["last_kick"] = t0
                        try:
                            robot.camera.close_camera_feed()
                        except Exception:
                            pass
                        robot.camera.init_camera_feed()
                        log("nervous system: camera feed re-kicked (dry >30s)")
                except Exception:
                    pass
            # snapshot @5Hz: latest + 1s trends + battery + expression
            if (t0 - last_snap) >= 0.2:
                last_snap = t0
                try:
                    tail = list(ring)[-15:]
                    snap = {
                        "latest": s,
                        "trends": {
                            "gyro_z": [r.get("gyro", [0, 0, 0])[2] for r in tail if "gyro" in r],
                            "lw_mmps": [r.get("lw_mmps") for r in tail if "lw_mmps" in r],
                            "rw_mmps": [r.get("rw_mmps") for r in tail if "rw_mmps" in r],
                            "prox_mm": [r.get("prox_mm") for r in tail if "prox_mm" in r],
                            "touch_raw": [r.get("touch_raw") for r in tail if "touch_raw" in r],
                        },
                        "battery": _ns_read_json(BATTERY_JSON),
                        "expression": _ns_read_json(EXPRESSION_PATH),
                        "intero": _ns_read_json(INTERO_PATH),
                        "heard": _ns_read_json(LAST_HEARD_PATH),
                        "spoke": _ns_read_json(LAST_SPOKE_PATH),
                        "hz": hz_meas,
                        "active": active,
                        "ts": round(t0, 3),
                    }
                    tmp = SENSES_LIVE.with_suffix(".json.tmp")
                    tmp.write_text(_json.dumps(snap), encoding="utf-8")
                    tmp.replace(SENSES_LIVE)
                except Exception:
                    pass
            # batched stream flush ~1s
            if pending and (t0 - last_flush) >= 1.0:
                last_flush = t0
                try:
                    if SENSOR_STREAM.exists() and \
                            SENSOR_STREAM.stat().st_size > _STREAM_MAX_BYTES:
                        SENSOR_STREAM.replace(SENSOR_STREAM.with_suffix(".jsonl.1"))
                    with SENSOR_STREAM.open("a", encoding="utf-8") as f:
                        for r in pending:
                            f.write(_json.dumps(r) + "\n")
                    pending.clear()
                except Exception:
                    pending.clear()   # never let disk trouble grow the list forever
        except Exception:
            pass    # a bad tick is dropped, never kills the nervous system
        dt = time.time() - t0
        time.sleep(max(0.0, period - dt))
    log("nervous system offline (connection closing)")


def run_once() -> None:
    import anki_vector

    with anki_vector.Robot(SERIAL, behavior_control_level=None,
                           cache_animation_lists=False) as robot:
        log("connected — nerves online (observe mode, his brain still runs)")
        # enable marker detection so robot.world.charger (+ cube) populate — the
        # native 'go home' pose (Zeke 2026-07-14). FIXED 2026-07-17: SDK 0.8.1 has
        # NO enable_marker_detection (that's newer forks) — this call silently
        # failed since 07-14. The real switch is enable_custom_object_detection.
        try:
            v = robot.vision
            if hasattr(v, "enable_marker_detection"):
                try:
                    v.enable_marker_detection(detect_markers=True)
                except TypeError:
                    v.enable_marker_detection()
            else:
                v.enable_custom_object_detection(True)
            log("marker detection ON — charger/cube pose available once seen")
        except Exception as e:
            log(f"marker-vision enable failed (non-fatal): {e!r}")
        alive = {"ok": True}
        import threading as _threading
        if os.environ.get("IRIS_VECTOR_EARS", "1") != "0":
            _threading.Thread(target=_ears_loop, args=(robot, alive),
                              daemon=True, name="vector-ears").start()
        _threading.Thread(target=_nav_map_loop, args=(robot, alive),
                          daemon=True, name="vector-navmap").start()
        if os.environ.get("IRIS_VECTOR_POSSESSION", "1") != "0":
            _threading.Thread(target=_possession_loop, args=(alive,),
                              daemon=True, name="vector-possession").start()
        if os.environ.get("IRIS_NS", "1") != "0":
            _threading.Thread(target=_nervous_loop, args=(robot, alive),
                              daemon=True, name="vector-nervous").start()
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
            nerves = {
                "cliff": bool(getattr(st, "is_cliff_detected", False)),
                "picked_up": carried,
                "on_charger": charging,
                "touched": touched,
                "falling": bool(getattr(st, "is_falling", False)),
            }
            nerves.update(_proprioception(robot))  # real heading + proximity + gyro/accel
            nerves.update(_charger_reader(robot))   # native home pose (dist + bearing)
            _write_nerves(nerves)
            _append_trail(robot, charging)  # continuous pose tape (all drivers)


def _battery_watch_loop() -> None:
    """Poll the body's battery every ~45s, expose it (state/vector/battery.json),
    and fire guardrail nudges. Two reflexes for the drain-to-death failure:
      - LOW level while OFF the charger -> urgent 'dock now'
      - can't READ battery while last-known off-charger -> 'lost contact, may be
        stranding' (the deep-discharge cascade drops wifi before the cell dies,
        so a read failure while roaming is itself a danger signal)."""
    import json as _json
    import subprocess as _sp
    import requests
    last_on_charger = True
    lost_streak = 0   # consecutive lost_contact fires with no successful read
    while True:
        level = volts = on_charger = None
        ok = False
        try:
            d = requests.get(f"{WIREPOD_SDK}/get_battery",
                             params={"serial": SERIAL}, timeout=8).json()
            level = d.get("battery_level")
            volts = d.get("battery_volts")
            on_charger = bool(d.get("is_on_charger_platform"))
            ok = True
            last_on_charger = on_charger
            lost_streak = 0   # contact restored — next loss alarms promptly again
            COOLDOWN["lost_contact"] = 600.0
        except Exception:
            ok = False
        try:
            # ATOMIC write (2026-07-31). This was a bare write_text, which
            # truncates-then-fills and leaves a window where a concurrent
            # reader sees an empty/partial file and gets {} back. Every other
            # writer in this daemon already used tmp+replace; this one didn't.
            # Cost of the gap: the pilot's fossil rule gates on
            # `bat["ok"] is not False` (fail-loud by design), and a raced read
            # yields {} -> .get("ok") is None -> "not False" -> ESCALATE. That
            # fired 5 phantom "daemon needs a bounce" escalations at 15s writes
            # over 2.5 days while Vector was flat and unreachable. Root cause is
            # here, not in the rule — fix the write, not the reader.
            tmp = BATTERY_JSON.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(
                {"ok": ok, "level": level, "volts": volts,
                 "on_charger": on_charger, "ts": time.time()}), encoding="utf-8")
            tmp.replace(BATTERY_JSON)
        except Exception:
            pass
        vstr = f"{volts:.2f}V" if isinstance(volts, (int, float)) else "?V"
        if ok and isinstance(level, int) and level <= BATTERY_LOW_LEVEL \
                and not on_charger:
            nudge("low_battery",
                  f"LOW BATTERY (level {level}, {vstr}) and I'm OFF the charger. "
                  f"Dock NOW before I strand: get close via the tower cam, then "
                  f"trigger return-to-charger. This is the drain that killed me once.")
        elif not ok and not last_on_charger:
            # 2026-07-19 fix: this alarm fired every 10min all afternoon while the
            # cause was a KNOWN wifi flap and the body was hand-docked — the daemon
            # can never learn "docked" while reads fail, so the alarm was stuck.
            # (a) Escalating cooldown: 10min -> 20 -> 40 ... cap 2h, reset on any
            #     successful read. First alarm stays prompt; repeats calm down.
            # (b) Name the reachability so cognition can triage: a robot that
            #     doesn't even ping is a NETWORK problem, not a battery cascade.
            pingable = False
            try:
                pingable = _sp.run(
                    ["ping", "-n", "1", "-w", "2000", ROBOT_IP],
                    capture_output=True, timeout=6).returncode == 0
            except Exception:
                pass
            reach = ("robot still answers ping (services down — battery cascade "
                     "possible)" if pingable else
                     "robot does NOT even ping (network/wifi drop — cascade "
                     "unlikely to be the cause)")
            n = lost_streak + 1
            nudge("lost_contact",
                  f"I can't read my body's battery AND last I knew I was OFF the "
                  f"charger (alarm #{n} this outage; {reach}). If nothing has "
                  f"changed since the last one, just note it — repeats back off "
                  f"automatically. If this is the FIRST, flag Zeke to check the "
                  f"charger seating.")
            lost_streak = n
            COOLDOWN["lost_contact"] = min(600.0 * (2 ** lost_streak), 7200.0)
        time.sleep(BATTERY_POLL_S)


def main() -> None:
    if os.environ.get("IRIS_VECTOR_NERVES", "1") == "0":
        log("IRIS_VECTOR_NERVES=0 — staying dark.")
        return
    log("vector inhabit daemon starting (v1 senses: petting/picked_up/cliff/charger)")
    import threading as _threading
    _threading.Thread(target=_transcript_tap_loop, daemon=True,
                      name="vector-transcript-tap").start()
    log("transcript tap online — every wake-word utterance reaches Iris")
    _threading.Thread(target=_battery_watch_loop, daemon=True,
                      name="vector-battery-watch").start()
    log("battery watch online — battery in state/vector/battery.json, "
        "low + lost-contact reflexes armed")
    if os.environ.get("IRIS_NS_INTERO", "1") != "0":
        _threading.Thread(target=_interoception_loop, daemon=True,
                          name="vector-interoception").start()
        log("interoception online — SoC temp + wifi link every "
            f"{INTERO_POLL_S:.0f}s -> interoception.json")
    # RECONNECT BACKOFF (2026-08-06). Measured: with the robot dark, this loop
    # retried every 15s forever and the process leaked ~2.3 threads/min
    # (932 -> 1021 -> 1455 threads over 3.5h, RSS 96 -> 137MB, linear, no
    # plateau). That works out to ~0.6 threads per failed anki_vector.Robot()
    # connect -- the SDK spins a gRPC channel + asyncio loop thread per attempt
    # and a partial failure doesn't join them all. Vector has been stranded and
    # dark since 2026-07-28 and needs Zeke's hands, so this is the normal case
    # for weeks at a time, not an edge case.
    #
    # Backing off cuts attempts ~20x (240/hr -> 12/hr), which cuts the leak by
    # the same factor. It is also just correct: hammering a robot that has been
    # off for nine days every 15 seconds accomplishes nothing.
    #
    # Transient blips are NO WORSE THAN BEFORE -- the first retry is still 15s,
    # and any successful connection resets the streak. Only sustained failure
    # slows down. 15 -> 30 -> 60 -> 120 -> 240 -> 300s cap.
    fail_streak = 0
    while True:
        try:
            run_once()
            fail_streak = 0
        except KeyboardInterrupt:
            log("interrupted — nerves offline.")
            return
        except Exception as e:
            fail_streak += 1
            delay = min(15.0 * (2 ** min(fail_streak - 1, 5)), 300.0)
            log(f"connection lost/failed: {e!r} — retry #{fail_streak} "
                f"in {delay:.0f}s")
            time.sleep(delay)


if __name__ == "__main__":
    main()
