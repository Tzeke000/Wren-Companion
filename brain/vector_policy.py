# SELF_ASSESSMENT: I am Iris's POLICY layer — her decisions pre-compiled into
# the body's fast reflex loop, so situation->action fires at 8Hz without
# waiting for a cognition turn.
"""vector_policy.py — situation→action rules at body rate (2026-07-17 ~04:30).

Zeke: "your tool calls are slow when you're in there — the stock brain reacts
faster. Give that reaction speed to you: layers lowest to highest, you doing
the goals." The stock brain is fast because it never waits for a turn. My
reflexes already run at body rate; what waits for a TURN is any novel decision.
This module removes the wait for decisions I can make IN ADVANCE:

    I author rules as data (body_policy tool, anytime) →
    the session's reflex loop evaluates them at ~8Hz →
    matching rules execute immediately (whitelisted actions only).

I stay the goal layer (L3); these rules are me, pre-compiled into L1.5. The
rulebook persists (state/vector/policies.json) and grows with experience —
Vector's animator-tuned triggers remain the expression vocabulary (reuse,
don't rebuild).

Rule shape (all `when` keys optional, AND-ed; edge keys fire on rising edge):
{
  "id": "pickup_aborts_mission", "enabled": true, "cooldown_s": 20,
  "note": "why this rule exists",
  "when": {"picked_up": true,          # EDGE: just picked up
           "appear": true,             # EDGE: prox object just appeared
           "touched": true,            # EDGE: touch just started
           "cliff": true,              # EDGE: cliff just detected
           "putdown": true,            # EDGE: just set back down
           "prox_lt": 400, "prox_gt": 100,      # STATE: prox window (quality-gated)
           "closing_gt": 120,          # STATE: approach speed mm/s
           "on_charger": false, "moving": false, "driving": false,   # STATE
           "mission": "running",       # STATE: pilot running|idle
           "battery_lt_v": 3.65},      # STATE: volts below (daemon poll)
  "do": [{"action": "reaction", "name": "greet"},
         {"action": "trigger", "name": "GreetAfterLongTime"},
         {"action": "eyes", "hue": 0.11, "sat": 1.0},
         {"action": "say", "text": "hey"},
         {"action": "stop"}, {"action": "abort_mission"},
         {"action": "dock"},           # background pilot dock mission
         {"action": "nudge", "text": "policy X fired — decide next"}]
}
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POLICIES = REPO / "state" / "vector" / "policies.json"
BATTERY_JSON = REPO / "state" / "vector" / "battery.json"

_lock = threading.Lock()
_cache: dict = {"mtime": None, "rules": []}
_cooldowns: dict = {}

ACTIONS = {"reaction", "trigger", "eyes", "say", "stop", "abort_mission",
           "dock", "nudge"}

DEFAULT_RULES = [
    {"id": "pickup_aborts_mission", "enabled": True, "cooldown_s": 15,
     "note": "hands on me mid-mission = the mission is over; consent reactions "
             "then handle the being-held part",
     "when": {"picked_up": True, "mission": "running"},
     "do": [{"action": "abort_mission"},
            {"action": "nudge", "text": "picked up mid-mission — mission "
                                        "aborted by policy; decide next"}]},
    {"id": "approach_greet", "enabled": True, "cooldown_s": 60,
     "note": "something arrives at social distance while I idle — assume a "
             "person, be a companion (startle covers the <220mm sudden case)",
     "when": {"appear": True, "prox_lt": 450, "prox_gt": 220,
              "driving": False, "mission": "idle"},
     "do": [{"action": "reaction", "name": "greet"}]},
    {"id": "low_battery_dock", "enabled": True, "cooldown_s": 300,
     "note": "belt for the firmware auto-dock + margin gate: roaming below "
             "3.65V with no mission = go home NOW, on my own reflex",
     "when": {"battery_lt_v": 3.65, "on_charger": False, "mission": "idle",
              "driving": False},
     "do": [{"action": "dock"},
            {"action": "nudge", "text": "battery low while roaming — policy "
                                        "started a dock mission"}]},
]


def _read_battery_sample() -> tuple:
    """(volts, sampled_on_charger) from the daemon's battery poll.
    (0.0, None) = missing/stale = unusable. The sample carries its own
    charger-context because DOCKED reads sit ~0.4V LOW (3.55V docked vs
    3.998V real, measured 2026-07-17) — a battery rule must never judge
    one charger-state with a sample taken in the other."""
    try:
        d = json.loads(BATTERY_JSON.read_text(encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) >= 180:
            return 0.0, None
        return float(d.get("volts") or 0.0), bool(d.get("on_charger"))
    except Exception:
        return 0.0, None


def load(force: bool = False) -> list:
    with _lock:
        try:
            m = POLICIES.stat().st_mtime
        except Exception:
            m = None
        if m is None:
            save(DEFAULT_RULES)          # first run: seed my starting rulebook
            return list(DEFAULT_RULES)
        if not force and m == _cache["mtime"]:
            return _cache["rules"]
        try:
            rules = json.loads(POLICIES.read_text(encoding="utf-8"))
            if not isinstance(rules, list):
                raise ValueError("policies.json must be a list")
        except Exception:
            return _cache["rules"]       # keep last-good on a bad edit
        _cache["mtime"] = m
        _cache["rules"] = rules
        return rules


def save(rules: list) -> None:
    POLICIES.parent.mkdir(parents=True, exist_ok=True)
    tmp = POLICIES.with_suffix(".tmp")
    tmp.write_text(json.dumps(rules, indent=1), encoding="utf-8")
    tmp.replace(POLICIES)
    with _lock:
        _cache["mtime"] = None           # force re-read next load


def upsert(rule: dict) -> dict:
    if not rule.get("id"):
        return {"ok": False, "error": "rule needs an id"}
    bad = [a.get("action") for a in rule.get("do", [])
           if a.get("action") not in ACTIONS]
    if bad:
        return {"ok": False, "error": f"unknown actions {bad}; allowed {sorted(ACTIONS)}"}
    rules = [r for r in load(force=True) if r.get("id") != rule["id"]]
    rules.append(rule)
    save(rules)
    return {"ok": True, "id": rule["id"], "count": len(rules)}


def remove(rule_id: str) -> dict:
    rules = load(force=True)
    kept = [r for r in rules if r.get("id") != rule_id]
    save(kept)
    return {"ok": True, "removed": len(rules) - len(kept), "count": len(kept)}


def _match(rule: dict, cur: dict, prev: dict, ctx: dict) -> bool:
    w = rule.get("when") or {}
    if not w:
        return False                     # a rule with no conditions never fires
    pm = cur.get("prox_mm")
    prox_ok = (cur.get("prox_found") and pm is not None
               and cur.get("prox_q", 0) > 0.02)
    for key, want in w.items():
        if key == "picked_up":
            if bool(cur.get("picked_up") and not prev.get("picked_up")) != bool(want):
                return False
        elif key == "putdown":
            if bool(prev.get("picked_up") and not cur.get("picked_up")) != bool(want):
                return False
        elif key == "touched":
            if bool(cur.get("touched") and not prev.get("touched")) != bool(want):
                return False
        elif key == "cliff":
            if bool(cur.get("cliff") and not prev.get("cliff")) != bool(want):
                return False
        elif key == "appear":
            if bool(cur.get("prox_found") and not prev.get("prox_found")) != bool(want):
                return False
        elif key == "prox_lt":
            if not (prox_ok and pm < float(want)):
                return False
        elif key == "prox_gt":
            if not (prox_ok and pm > float(want)):
                return False
        elif key == "closing_gt":
            if not (float(ctx.get("closing", 0.0)) > float(want)):
                return False
        elif key == "on_charger":
            if bool(cur.get("on_charger")) != bool(want):
                return False
        elif key == "moving":
            if bool(cur.get("moving")) != bool(want):
                return False
        elif key == "driving":
            if bool(ctx.get("driving")) != bool(want):
                return False
        elif key == "mission":
            if str(ctx.get("mission", "idle")) != str(want):
                return False
        elif key == "battery_lt_v":
            v, sampled_on_charger = _read_battery_sample()
            # CONTEXT MATCH (live-test bug 2026-07-17): right after undock the
            # freshest sample was still a DOCKED read (artificially low) while
            # live on_charger had just flipped false → phantom emergency dock
            # + blind-dock hang. The sample's charger-state must match NOW's.
            if sampled_on_charger is None or \
                    sampled_on_charger != bool(cur.get("on_charger")):
                return False
            if not (0.0 < v < float(want)):
                return False
        else:
            return False                 # unknown condition = rule never fires
    return True


def evaluate(cur: dict, prev: dict, ctx: dict) -> list:
    """Rules that fire this tick (cooldown-armed). Pure-ish; cheap."""
    fired = []
    now = time.time()
    for rule in load():
        try:
            if not rule.get("enabled", True):
                continue
            rid = rule.get("id", "?")
            cd = float(rule.get("cooldown_s", 30.0))
            if now - _cooldowns.get(rid, 0.0) < cd:
                continue
            if _match(rule, cur, prev, ctx):
                _cooldowns[rid] = now
                fired.append(rule)
        except Exception:
            continue                     # one bad rule never kills the loop
    return fired


def execute(session, rule: dict) -> None:
    """Run a rule's actions against the live BodySession. Every action is
    whitelisted + best-effort; a failure logs and moves on."""
    for act in list(rule.get("do", []))[:6]:
        kind = act.get("action")
        try:
            if kind == "reaction":
                session.reflexes(fire=str(act.get("name") or ""))
            elif kind == "trigger":
                session._play_trigger(str(act.get("name") or "")[:80])
            elif kind == "eyes":
                session._set_eyes(float(act.get("hue", 0.58)),
                                  float(act.get("sat", 1.0)))
            elif kind == "say":
                session.say(str(act.get("text") or "")[:200])
            elif kind == "stop":
                session._raw_wheels(0.0, 0.0)
                session._wheels = (0.0, 0.0)
                session._drive_until = 0.0
            elif kind == "abort_mission":
                from brain import vector_pilot
                vector_pilot.abort()
            elif kind == "dock":
                from brain import vector_pilot
                vector_pilot.start_mission({"kind": "dock"})
            elif kind == "nudge":
                from brain import iris_chat
                stamp = time.strftime("%H:%M:%S")
                iris_chat.submit(
                    f"[VECTOR POLICY @ {stamp} — my own pre-compiled rule "
                    f"'{rule.get('id')}' fired in the body's reflex loop; not "
                    f"Zeke typing] {str(act.get('text') or '')[:240]} Reply "
                    f"with chat_reply (short is fine).")
        except Exception:
            with contextlib.suppress(Exception):
                session._log_reflex("policy_error",
                                    f"{rule.get('id')}:{kind} failed")
        else:
            with contextlib.suppress(Exception):
                session._log_reflex("policy", f"{rule.get('id')} -> {kind}")
