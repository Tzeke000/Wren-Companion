# SELF_ASSESSMENT: I am the L2 behavior policy — the small brain (iris-little-v4)
# in a slow perceive->decide->act loop over the body, between the daemon's
# reflexes (L1) and big Iris's deliberation. Built 2026-07-20 (Zeke: "give your
# small brain access to your body... make those layers").
"""Little pilot — the small brain's body loop (GrowBot layer architecture).

Layers: L0 stock firmware reflexes (trained cerebellum) -> L1 inhabit daemon
(possession, nerves, battery gate) -> L2 THIS (behavior policy: local Iris
picks actions from a strict vocabulary) -> L3 goals.json (big Iris / Zeke
write goals; this loop reads them every cycle).

Deployment-month safety (deliberate):
- The v0 action vocabulary contains NO translational driving. Motion-adjacent
  wire-pod actions stay behind IRIS_VECTOR_MOVE_OK exactly like the reflex
  token executor. Docking/re-seat belongs to the inhabit daemon + firmware
  (hard rule) — the pilot only REQUESTS it via an alert.
- Hard escalations (battery emergency, picked-up while nobody home) bypass
  the LLM entirely: sensors -> alert file, no model in the loop.
- Every cycle is logged with its raw sensor snapshot -> pilot_log.jsonl.
  That log IS the Track-A raw-experience dataset a future prediction layer
  (real cerebellum) trains on.

Run:  D:\\Wren-Companion\\.venv\\Scripts\\python.exe scripts\\little_pilot.py
Idempotent: exits if another little_pilot already runs (pidfile + liveness).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import vector_intent_exec as body  # serial/say_stock/set_eyes/big_brain_reachable

STATE = REPO / "state"
LB = STATE / "little_brain"
GOALS = LB / "goals.json"
LOG = LB / "pilot_log.jsonl"
ALERTS = LB / "pilot_alerts.jsonl"
PIDFILE = LB / "pilot.pid"
# --- apprenticeship channels (v0.2, Zeke 2026-07-20: "you do the driving,
# it watches and gives suggestions, you grade them and give it reasons") ---
WATCH = LB / "watch_mode.json"          # big Iris sets {"active":true,"task":..}
SUGG = LB / "suggestions.jsonl"         # apprentice's suggestions during drives
GRADES = LB / "suggestion_grades.jsonl" # big Iris grades: {ref_ts,grade,reason}
LESSONS_BIG = LB / "lessons_from_big.jsonl"  # big Iris teaches WHY
POSE_TRAIL = STATE / "vector" / "pose_trail.jsonl"
BIG_ACTIONS = LB / "big_iris_actions.jsonl"  # big-Iris's actual body commands (imitation feed)
FACTS = STATE / "vector" / "local_brain_facts.md"
BATTERY = STATE / "vector" / "battery.json"
NERVES = STATE / "vector" / "nerves.json"
POSSESSION = STATE / "vector" / "possession_status.json"
# --- REFLEX LAYER (2026-07-23, Zeke: "keep working towards small brain = reflexes") ---
REFLEX_SHADOW = LB / "reflex_shadow.jsonl"   # shadow log: what the reflex WOULD do, disarmed
# webcam mean-brightness floor for "light enough to see the charger marker".
# Measured 2026-07-23: lamp-on morning room ≈ 134; true dark falls far below.
# 60 leaves huge margin yet trips if a bulb dies at night. Env-tunable.
LIGHT_MIN = float(os.environ.get("IRIS_LIGHT_MIN", "60"))
# grace before the reflex takes over: how long the low-off-charger emergency may
# sit UNRESOLVED before the little brain docks it herself. Zeke 2026-07-23: frozen
# vs just-heads-down-busy is the same shape — either way, if nobody's moved the
# body to the dock in this window, the reflex should.
REFLEX_GRACE_S = float(os.environ.get("IRIS_REFLEX_GRACE_S", "60"))

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = os.environ.get("IRIS_LOCAL_MODEL", "iris-little-v4")
CYCLE_S = 150          # slow loop — thinking is expensive, living is not
HEARTBEAT_EVERY = 12   # think anyway every Nth quiet cycle (~30 min)
SAY_COOLDOWN_S = 1800  # stock-voice speech at most twice an hour

ACTIONS = ("stay", "eyes", "say", "alert", "reseat_request", "head", "lift",
           "nod")
VOCAB = (
    "stay                - default; keep watch, do nothing\n"
    "eyes <preset>       - recolor eyes (iris/calm/happy/alert); color only, no motion\n"
    "say <short text>    - speak via the robot (stock voice; use rarely, only if someone is present)\n"
    "alert <short text>  - flag big Iris about something she should look at\n"
    "reseat_request      - off charger and shouldn't be: ask the daemon layer to re-dock\n"
    "head <up|down>      - tilt the head gently (ONLY works while docked; head looks up to read symbols/faces, down to rest)\n"
    "lift <up|down>      - move the forks gently (ONLY works while docked; forks carry cubes and greet)\n"
    "nod                 - nod the head yes (docked-only) - your way of answering YES without words"
)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _append(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def _single_instance() -> bool:
    try:
        if PIDFILE.is_file():
            pid = int(PIDFILE.read_text().strip())
            import psutil  # in .venv
            if psutil.pid_exists(pid) and "python" in psutil.Process(pid).name().lower():
                return False
    except Exception:
        pass
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    return True


def _situation(bat: dict, nrv: dict, pos: dict, goals: dict) -> str:
    on_chg = nrv.get("on_charger") if "on_charger" in nrv else bat.get("on_charger")
    stale = " (battery file may be stale; charger flag from live nerves)" \
        if bat.get("on_charger") != on_chg else ""
    return (
        f"battery: {bat.get('volts', '?')}V level {bat.get('level', '?')} "
        f"on_charger={on_chg}{stale}\n"
        f"nerves: cliff={nrv.get('cliff')} picked_up={nrv.get('picked_up')} "
        f"touched={nrv.get('touched')} falling={nrv.get('falling')} "
        f"prox_mm={nrv.get('prox_mm')} charger_seen={nrv.get('charger_seen')}\n"
        f"possession held by daemon: {pos.get('held')}\n"
        f"big Iris reachable: {body.big_brain_reachable()}\n"
        f"goals: {json.dumps(goals.get('standing_goals', []), ensure_ascii=False)}\n"
        + (f"DIRECT REQUEST FROM BIG IRIS (act on this): "
           f"{goals['practice_request']}\n" if goals.get("practice_request") else "")
        + f"nobody_home: {goals.get('nobody_home', True)}  "
        + f"motion_enabled: {goals.get('motion_enabled', False)}"
    )


def _tail_jsonl(p: Path, n: int) -> list[dict]:
    try:
        lines = p.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def _teaching_context() -> str:
    """Recent lessons from big Iris + graded suggestions — the apprenticeship
    memory that shapes both normal decisions and watch-mode suggestions."""
    parts = []
    lessons = _tail_jsonl(LESSONS_BIG, 6)
    if lessons:
        parts.append("LESSONS FROM BIG IRIS (reasons behind actions — learn these):")
        parts += [f"- {d.get('lesson', '')}" for d in lessons]
    grades = {d.get("ref_ts"): d for d in _tail_jsonl(GRADES, 12)}
    graded = [(s, grades[s["ts"]]) for s in _tail_jsonl(SUGG, 12)
              if s.get("ts") in grades]
    if graded:
        parts.append("YOUR PAST SUGGESTIONS, GRADED BY BIG IRIS:")
        for s, g in graded[-5:]:
            parts.append(f"- you said: {s.get('suggestion','')[:100]} -> "
                         f"{g.get('grade','?')}: {g.get('reason','')[:120]}")
    return "\n".join(parts)


def _watch_state() -> dict:
    w = _read_json(WATCH)
    if w.get("active") and time.time() - float(w.get("ts") or 0) > 900:
        return {}  # stale watch flags auto-expire (15 min)
    return w if w.get("active") else {}


def _suggest(task: str, bat: dict, nrv: dict) -> None:
    """WATCH MODE: big Iris is driving; observe and offer ONE suggestion."""
    import urllib.request
    poses = _tail_jsonl(POSE_TRAIL, 8)
    trail = "; ".join(f"({p.get('x',0):.0f},{p.get('y',0):.0f})" for p in poses)
    # big-Iris's ACTUAL commands (2026-07-23): so you learn her technique by
    # imitation — what she DID, not just where the body ended up.
    acts = _tail_jsonl(BIG_ACTIONS, 6)
    moves = "; ".join(
        a.get("action", "?") + "(" + ",".join(
            f"{k}={v}" for k, v in (a.get("params") or {}).items()) + ")"
        for a in acts)
    teach = _teaching_context()
    sys_p = (
        "You are Iris's small local brain, apprenticing: BIG Iris is driving "
        "the body right now and you are WATCHING to learn. You can see her "
        "ACTUAL commands (what she did) plus the sensors and where the body "
        "moved — learn her technique by watching what she chose. Offer exactly "
        "ONE short suggestion or observation about the current drive — something "
        "you notice, a risk, or what you'd do next. She will grade it, and "
        "the grades teach you. Be concrete and brief.\n" +
        (teach + "\n" if teach else "") +
        "Answer ONLY JSON: {\"suggestion\": ..., \"why\": ...}")
    situation = (f"her task: {task}\nbattery: {bat.get('volts')}V "
                 f"on_charger={bat.get('on_charger')}\n"
                 f"nerves: cliff={nrv.get('cliff')} prox_mm={nrv.get('prox_mm')} "
                 f"charger_seen={nrv.get('charger_seen')} "
                 f"picked_up={nrv.get('picked_up')}\n"
                 f"BIG IRIS'S RECENT COMMANDS (learn from these): {moves or 'none yet'}\n"
                 f"recent pose trail (mm): {trail or 'none yet'}")
    body_req = json.dumps({
        "model": MODEL, "stream": False, "format": "json",
        "options": {"temperature": 0.4, "num_predict": 110},
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": situation}],
    }).encode()
    req = urllib.request.Request(OLLAMA, data=body_req,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(json.load(r)["message"]["content"])
        _append(SUGG, {"ts": time.time(), "task": task,
                       "suggestion": str(d.get("suggestion", ""))[:300],
                       "why": str(d.get("why", ""))[:300]})
    except Exception as e:
        _append(SUGG, {"ts": time.time(), "task": task,
                       "error": repr(e)[:200]})


def _decide(situation: str) -> dict:
    """Ask the small brain for ONE action as strict JSON."""
    import urllib.request
    facts = ""
    try:
        facts = FACTS.read_text(encoding="utf-8")[:6000]
    except Exception:
        pass
    teach = _teaching_context()
    sys_p = (
        "You are Iris's small local brain acting as the body's behavior layer "
        "(L2). Read the situation and choose EXACTLY ONE action from the "
        "vocabulary. Be conservative: while nobody is home the right action is "
        "almost always 'stay'. Never invent actions.\n\nVOCABULARY:\n" + VOCAB +
        ("\n\n" + teach if teach else "") +
        "\n\nAnswer ONLY JSON: {\"action\": ..., \"arg\": ..., \"why\": ...}\n\n"
        "FACTS:\n" + facts)
    body_req = json.dumps({
        "model": MODEL, "stream": False, "format": "json",
        "options": {"temperature": 0.2, "num_predict": 120},
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": "SITUATION:\n" + situation}],
    }).encode()
    req = urllib.request.Request(OLLAMA, data=body_req,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)["message"]["content"]
    d = json.loads(out)
    if d.get("action") not in ACTIONS:
        d = {"action": "stay", "arg": "", "why": f"invalid action from model: {d}"}
    return d


_last_say = 0.0


def _act(d: dict, goals: dict) -> str:
    """Execute the decided action. Returns a result string for the log."""
    global _last_say
    a, arg = d.get("action", "stay"), str(d.get("arg") or "")
    if a == "stay":
        return "ok"
    if a == "eyes":
        presets = {"iris": (0.58, 0.90), "calm": (0.50, 0.55),
                   "happy": (0.30, 0.85), "alert": (0.02, 0.95)}
        hue, sat = presets.get(arg.lower(), presets["iris"])
        body.set_eyes(hue, sat)
        return f"eyes->{arg or 'iris'}"
    if a == "say":
        if time.time() - _last_say < SAY_COOLDOWN_S:
            return "suppressed: say cooldown"
        if goals.get("nobody_home", True):
            return "suppressed: nobody home, staying quiet"
        _last_say = time.time()
        body.say_stock(arg[:120])
        return f"said: {arg[:60]}"
    if a == "nod":
        nrv = _read_json(NERVES)
        if not nrv.get("on_charger"):
            return "suppressed: nod only while docked"
        try:
            import requests
            esn = body.serial()
            for spd, dur in ((1.3, 0.25), (-1.3, 0.45), (1.3, 0.3), (0, 0)):
                requests.post("http://127.0.0.1:8080/api-sdk/move_head",
                              params={"serial": esn, "speed": spd}, timeout=6)
                time.sleep(dur or 0.1)
            return "nodded yes"
        except Exception as e:
            return f"nod failed: {e!r}"[:120]
    if a in ("head", "lift"):
        # ON-DOCK-ONLY motor pair (Zeke 2026-07-20): hard gate OUTSIDE the
        # model — live nerves must show docked. Head/lift cannot move wheels.
        nrv = _read_json(NERVES)
        if not nrv.get("on_charger"):
            return "suppressed: head/lift only allowed while docked"
        try:
            import requests
            esn = body.serial()
            path = "move_head" if a == "head" else "move_lift"
            speed = 1.2 if arg.lower().startswith("u") else -1.2
            requests.post(f"http://127.0.0.1:8080/api-sdk/{path}",
                          params={"serial": esn, "speed": speed}, timeout=6)
            time.sleep(0.4)
            requests.post(f"http://127.0.0.1:8080/api-sdk/{path}",
                          params={"serial": esn, "speed": 0}, timeout=6)
            return f"{a}->{arg or 'up'}"
        except Exception as e:
            return f"{a} failed: {e!r}"[:120]
    if a in ("alert", "reseat_request"):
        _append(ALERTS, {"ts": time.time(), "kind": a, "text": arg,
                         "why": d.get("why", "")})
        return f"alert filed: {a}"
    return "noop"


_hist: list[dict] = []   # last cycles for contact-health rules (v0.3)


def _contact_health(bat: dict, nrv: dict) -> str | None:
    """CHARGE-CONTACT TRACKER (v0.3, 2026-07-20 incident): catches
    seated-but-not-charging, fossil feeds, and charging-lies — the three
    ways today's contact failure could have been caught early."""
    _hist.append({"volts": bat.get("volts"), "on_chg": nrv.get("on_charger"),
                  "accel": tuple(nrv.get("accel") or [])})
    del _hist[:-6]
    if len(_hist) < 3:
        return None
    # A: off-charger sustained 2+ cycles (~5 min) — earlier than the volt rule
    if all(h["on_chg"] is False for h in _hist[-2:]):
        return "OFF CHARGER sustained 5+ min — seated-but-loose or wandered; needs re-seat"
    # B: accel identical 3 cycles = sensor feed FOSSIL (fresh ts, dead values)
    if len({h["accel"] for h in _hist[-3:]}) == 1 and _hist[-1]["accel"]:
        return "SENSOR FEED FOSSIL — accel frozen 3 cycles; daemon needs a bounce; distrust all files"
    # C: 'charging' but volts strictly falling 4 cycles = contact lie
    v = [h["volts"] for h in _hist[-4:] if h["volts"]]
    if (len(v) == 4 and all(a > b for a, b in zip(v, v[1:]))
            and _hist[-1]["on_chg"]):
        return f"CHARGING LIE — on_charger true but volts falling {v[0]}->{v[-1]}; contact suspect"
    return None


def _hard_escalations(bat: dict, nrv: dict, goals: dict) -> str | None:
    """Sensor -> alert with NO model in the loop. Returns alert text or None.
    Charger truth = NERVES (1Hz live); battery.json's flag can freeze stale
    (2026-07-20: cached 4.0072265/off-charger for an hour while docked)."""
    volts = float(bat.get("volts") or 0)
    on_chg = nrv.get("on_charger") if "on_charger" in nrv else bat.get("on_charger")
    if bat.get("ok") and not on_chg and volts and volts < 3.7:
        return f"LOW BATTERY OFF CHARGER: {volts}V — needs re-seat (daemon layer)"
    if nrv.get("picked_up") and goals.get("nobody_home", True):
        return "PICKED UP while nobody should be home — check the room"
    if nrv.get("falling"):
        return "FALLING detected"
    return None


# ── REFLEX LAYER: the small brain as the body's autonomic reflexes ─────────────
# Zeke's contract (goals.json emergency_reflex_contract): if battery is low
# off-charger AND big-Iris has been silent 60s, the little brain takes over and
# parks. Plus the light rule (2026-07-23): too dark to see home = don't move.
# ALL of this runs in SHADOW until goals.emergency_reflex_contract.enabled=true —
# it logs what it WOULD do and never touches the wheels. Arming needs a
# supervised fake-low-battery drill (and the daemon dock-trigger wired below).

def _frame_brightness() -> float | None:
    """Mean brightness 0-255 of the current tower-webcam frame, or None if
    unreadable. Reads the runtime's vision endpoint, which stays up even when
    big-Iris's COGNITION is frozen (a token-freeze doesn't kill the process)."""
    try:
        import io
        import urllib.request
        import numpy as np
        from PIL import Image
        data = urllib.request.urlopen(
            "http://127.0.0.1:5876/api/v1/vision/latest_frame", timeout=4).read()
        return float(np.asarray(Image.open(io.BytesIO(data)).convert("L")).mean())
    except Exception:
        return None


def _light_ok() -> bool:
    """True ONLY when we can confirm enough light to see the charger marker.
    Fail-SAFE: unreadable brightness -> False (treat as dark, don't move).
    Zeke's rule: too dark = don't even come off the charger."""
    b = _frame_brightness()
    return b is not None and b >= LIGHT_MIN


def _command_emergency_park() -> str:
    """ARMED emergency dock. Docking belongs to the daemon + firmware (hard rule:
    the pilot NEVER raw-drives), so this must trigger a re-seat and let firmware
    return-to-charger do the last leg. That daemon dock-trigger is NOT wired yet —
    arming the contract requires wiring it AND a supervised fake-low-battery drill.
    Until then this files a MAX alert and returns loudly rather than pretend to dock."""
    _append(ALERTS, {"ts": time.time(), "kind": "EMERGENCY_PARK_ATTEMPT",
                     "text": "ARMED reflex wanted to dock, but the daemon dock-trigger "
                             "is NOT wired — file the supervised-drill + wiring task "
                             "before enabling the contract"})
    return "PARK requested — daemon dock-trigger NOT wired (needs wiring + drill)"


_emergency_since: float | None = None  # ts the low-off-charger emergency first appeared this streak


def _emergency_reflex(bat: dict, nrv: dict, goals: dict) -> str | None:
    """Evaluate the emergency-park contract every cycle. ACTS only when
    goals.emergency_reflex_contract.enabled is true (default false = SHADOW:
    decide + log, take NO body action). Returns a status string, or None when
    there's no emergency. Charger truth = NERVES (1Hz live), not battery.json.

    Trigger (Zeke reframe 2026-07-23): NOT 'is big-Iris frozen' — that's the same
    shape as 'big-Iris is heads-down busy and hasn't moved the body.' Either way,
    if the low-off-charger emergency sits UNRESOLVED for REFLEX_GRACE_S, nobody
    docked it, so the reflex should. Simple, observable, no mind-reading."""
    global _emergency_since
    volts = float(bat.get("volts") or 0)
    on_chg = nrv.get("on_charger") if "on_charger" in nrv else bat.get("on_charger")
    emergency = bool(volts and not on_chg and volts < 3.7)
    if not emergency:
        _emergency_since = None          # resolved (docked, or a bad read cleared)
        return None
    now = time.time()
    if _emergency_since is None:
        _emergency_since = now
    unresolved_s = now - _emergency_since
    contract = goals.get("emergency_reflex_contract") or {}
    enabled = bool(contract.get("enabled"))
    lit = _light_ok()
    if unresolved_s < REFLEX_GRACE_S:
        decision = "WATCH"           # within grace — give big-me a moment to handle it
    elif lit:
        decision = "PARK"            # grace elapsed, still stranded, it's lit -> dock
    else:
        decision = "HOLD_DARK"       # grace elapsed but dark -> don't wander; hold + alert
    _append(REFLEX_SHADOW, {"ts": now, "volts": volts,
                            "unresolved_s": round(unresolved_s, 1), "lit": lit,
                            "decision": decision, "armed": enabled})
    _append(ALERTS, {"ts": now, "kind": "EMERGENCY_REFLEX",
                     "text": f"low batt {volts}V off-charger {unresolved_s:.0f}s "
                             f"unresolved; light_ok={lit}; decision={decision}; "
                             f"armed={enabled}"})
    if enabled and decision == "PARK":
        return _command_emergency_park()
    return (f"reflex shadow: would {decision} "
            f"(unresolved {unresolved_s:.0f}s, armed={enabled})")


def _can_leave_home(goals: dict) -> bool:
    """Gate on UNDOCKING (Zeke 2026-07-23: darkness gates *leaving home*). Every
    future motion/undock path must pass this: motion enabled by both flags AND
    enough light to get back. Provided now so driving, when it turns on, inherits
    the light-gate for free."""
    if not goals.get("motion_enabled"):
        return False
    if os.environ.get("IRIS_VECTOR_MOVE_OK", "").lower() not in ("1", "true", "yes", "on"):
        return False
    return _light_ok()


def main() -> int:
    if not _single_instance():
        print("another little_pilot is alive — exiting")
        return 0
    print(f"little pilot up: model={MODEL} cycle={CYCLE_S}s")
    prev, quiet_cycles = {}, HEARTBEAT_EVERY  # first cycle thinks (orientation)
    last_suggest = 0.0
    while True:
        try:
            # ── SAFETY FIRST: sensors + reflexes run EVERY cycle, in EVERY mode.
            # A frozen-mid-drive must never disable the reflexes, so this sits
            # ABOVE the watch/normal split (2026-07-23 fix: watch-mode used to
            # `continue` straight past the reflex evaluation — the exact scenario
            # the reflex exists for).
            bat, nrv = _read_json(BATTERY), _read_json(NERVES)
            goals = _read_json(GOALS)
            hard = _hard_escalations(bat, nrv, goals)
            reflex = _emergency_reflex(bat, nrv, goals)
            if reflex:
                hard = hard or reflex

            # WATCH MODE: big Iris is driving — sample fast, suggest slowly.
            # Safety already ran above, so a mid-drive emergency is covered here
            # too (at the ~10s watch cadence, not the slow 150s one).
            w = _watch_state()
            if w:
                if hard:
                    _append(ALERTS, {"ts": time.time(), "kind": "HARD",
                                     "text": hard})
                if time.time() - last_suggest >= 35:
                    last_suggest = time.time()
                    _suggest(str(w.get("task", "driving")), bat, nrv)
                time.sleep(10)
                continue

            pos = _read_json(POSSESSION)
            contact = _contact_health(bat, nrv)
            if contact:
                _append(ALERTS, {"ts": time.time(), "kind": "CONTACT",
                                 "text": contact})
                hard = hard or contact
            if hard:
                _append(ALERTS, {"ts": time.time(), "kind": "HARD", "text": hard})

            events = []
            for k in ("on_charger",):
                if prev.get("bat", {}).get(k) is not None and \
                        bat.get(k) != prev["bat"].get(k):
                    events.append(f"{k}: {prev['bat'].get(k)} -> {bat.get(k)}")
            for k in ("cliff", "picked_up", "touched", "falling", "charger_seen"):
                if nrv.get(k) and not prev.get("nrv", {}).get(k):
                    events.append(f"{k} fired")
            if goals != prev.get("goals", {}) and prev:
                events.append("goals changed")

            think = bool(events) or bool(hard) or quiet_cycles >= HEARTBEAT_EVERY
            decision, result = None, "quiet"
            if think:
                quiet_cycles = 0
                situation = _situation(bat, nrv, pos, goals)
                if events:
                    situation += "\nevents since last cycle: " + "; ".join(events)
                if hard:
                    situation += f"\nHARD ALERT already filed: {hard}"
                try:
                    decision = _decide(situation)
                    result = _act(decision, goals)
                except Exception as e:
                    decision = {"action": "stay", "arg": "",
                                "why": f"decide failed: {e!r}"[:200]}
                    result = "decide-error"
            else:
                quiet_cycles += 1

            _append(LOG, {"ts": time.time(), "bat": bat, "nrv": nrv,
                          "held": pos.get("held"), "events": events,
                          "thought": think, "decision": decision,
                          "result": result})
            prev = {"bat": bat, "nrv": nrv, "goals": goals}
        except Exception as e:
            _append(LOG, {"ts": time.time(), "loop_error": repr(e)[:300]})
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    sys.exit(main())
