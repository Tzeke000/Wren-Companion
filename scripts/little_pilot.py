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
FACTS = STATE / "vector" / "local_brain_facts.md"
BATTERY = STATE / "vector" / "battery.json"
NERVES = STATE / "vector" / "nerves.json"
POSSESSION = STATE / "vector" / "possession_status.json"

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = os.environ.get("IRIS_LOCAL_MODEL", "iris-little-v4")
CYCLE_S = 150          # slow loop — thinking is expensive, living is not
HEARTBEAT_EVERY = 12   # think anyway every Nth quiet cycle (~30 min)
SAY_COOLDOWN_S = 1800  # stock-voice speech at most twice an hour

ACTIONS = ("stay", "eyes", "say", "alert", "reseat_request")
VOCAB = (
    "stay                - default; keep watch, do nothing\n"
    "eyes <preset>       - recolor eyes (iris/calm/happy/alert); color only, no motion\n"
    "say <short text>    - speak via the robot (stock voice; use rarely, only if someone is present)\n"
    "alert <short text>  - flag big Iris about something she should look at\n"
    "reseat_request      - off charger and shouldn't be: ask the daemon layer to re-dock"
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
    return (
        f"battery: {bat.get('volts', '?')}V level {bat.get('level', '?')} "
        f"on_charger={bat.get('on_charger')}\n"
        f"nerves: cliff={nrv.get('cliff')} picked_up={nrv.get('picked_up')} "
        f"touched={nrv.get('touched')} falling={nrv.get('falling')} "
        f"prox_mm={nrv.get('prox_mm')} charger_seen={nrv.get('charger_seen')}\n"
        f"possession held by daemon: {pos.get('held')}\n"
        f"big Iris reachable: {body.big_brain_reachable()}\n"
        f"goals: {json.dumps(goals.get('standing_goals', []), ensure_ascii=False)}\n"
        f"nobody_home: {goals.get('nobody_home', True)}  "
        f"motion_enabled: {goals.get('motion_enabled', False)}"
    )


def _decide(situation: str) -> dict:
    """Ask the small brain for ONE action as strict JSON."""
    import urllib.request
    facts = ""
    try:
        facts = FACTS.read_text(encoding="utf-8")[:6000]
    except Exception:
        pass
    sys_p = (
        "You are Iris's small local brain acting as the body's behavior layer "
        "(L2). Read the situation and choose EXACTLY ONE action from the "
        "vocabulary. Be conservative: while nobody is home the right action is "
        "almost always 'stay'. Never invent actions.\n\nVOCABULARY:\n" + VOCAB +
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
    if a in ("alert", "reseat_request"):
        _append(ALERTS, {"ts": time.time(), "kind": a, "text": arg,
                         "why": d.get("why", "")})
        return f"alert filed: {a}"
    return "noop"


def _hard_escalations(bat: dict, nrv: dict, goals: dict) -> str | None:
    """Sensor -> alert with NO model in the loop. Returns alert text or None."""
    volts = float(bat.get("volts") or 0)
    if bat.get("ok") and not bat.get("on_charger") and volts and volts < 3.7:
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
    while True:
        try:
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
