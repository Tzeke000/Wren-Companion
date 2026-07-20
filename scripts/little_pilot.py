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
FACTS = STATE / "vector" / "local_brain_facts.md"
BATTERY = STATE / "vector" / "battery.json"
NERVES = STATE / "vector" / "nerves.json"
POSSESSION = STATE / "vector" / "possession_status.json"

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
    teach = _teaching_context()
    sys_p = (
        "You are Iris's small local brain, apprenticing: BIG Iris is driving "
        "the body right now and you are WATCHING to learn. Offer exactly ONE "
        "short suggestion or observation about the current drive — something "
        "you notice, a risk, or what you'd do next. She will grade it, and "
        "the grades teach you. Be concrete and brief.\n" +
        (teach + "\n" if teach else "") +
        "Answer ONLY JSON: {\"suggestion\": ..., \"why\": ...}")
    situation = (f"her task: {task}\nbattery: {bat.get('volts')}V "
                 f"on_charger={bat.get('on_charger')}\n"
                 f"nerves: cliff={nrv.get('cliff')} prox_mm={nrv.get('prox_mm')} "
                 f"charger_seen={nrv.get('charger_seen')} "
                 f"picked_up={nrv.get('picked_up')}\n"
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


def main() -> int:
    if not _single_instance():
        print("another little_pilot is alive — exiting")
        return 0
    print(f"little pilot up: model={MODEL} cycle={CYCLE_S}s")
    prev, quiet_cycles = {}, HEARTBEAT_EVERY  # first cycle thinks (orientation)
    last_suggest = 0.0
    while True:
        try:
            # WATCH MODE: big Iris is driving — sample fast, suggest slowly.
            w = _watch_state()
            if w:
                bat, nrv = _read_json(BATTERY), _read_json(NERVES)
                if time.time() - last_suggest >= 35:
                    last_suggest = time.time()
                    _suggest(str(w.get("task", "driving")), bat, nrv)
                time.sleep(10)
                continue

            bat, nrv = _read_json(BATTERY), _read_json(NERVES)
            pos, goals = _read_json(POSSESSION), _read_json(GOALS)

            hard = _hard_escalations(bat, nrv, goals)
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
