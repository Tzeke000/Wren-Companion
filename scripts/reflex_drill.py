"""reflex_drill.py — rehearse the emergency-park reflex against fake scenarios.

Feeds synthetic sensor readings through the REAL _emergency_reflex() decision
function (imported live from little_pilot) and checks each decision against what
it SHOULD be. Safe: disarmed (contract enabled=false), never touches the body,
and redirects the alert/shadow logs to a temp file so it can't pollute the real
alert queue. Zeke 2026-07-23: "do a few drills together like 5 or so" — the
confidence pass before the contract is ever armed on a live body.

Run:  D:\\Wren-Companion\\.venv\\Scripts\\python.exe scripts\\reflex_drill.py
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lp", ROOT / "scripts" / "little_pilot.py")
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)

# redirect side-effecting logs to a throwaway so real alerts/shadow stay clean
_DRILL_LOG = ROOT / "state" / "little_brain" / "_drill_scratch.jsonl"
lp.ALERTS = _DRILL_LOG
lp.REFLEX_SHADOW = _DRILL_LOG

DISARMED = {"emergency_reflex_contract": {"enabled": False}}

# each drill: (name, volts, on_charger, unresolved_s, light_override, expected_decision)
#   unresolved_s None -> fresh emergency (0s). light_override None -> use real camera.
#   expected None -> no emergency should be detected.
DRILLS = [
    ("1. docked & healthy (4.09V, on charger)",        4.09, True,  None,  None,  None),
    ("2. low off-charger, JUST happened (grace)",       3.60, False, 0,     None,  "WATCH"),
    ("3. low off-charger, 75s unresolved, LIT",         3.60, False, 75,    True,  "PARK"),
    ("4. low off-charger, 75s unresolved, DARK (sim)",  3.60, False, 75,    False, "HOLD_DARK"),
    ("5. borderline 3.80V off-charger (above 3.7)",     3.80, False, None,  None,  None),
]


def run():
    print(f"REFLEX DRILL — {len(DRILLS)} scenarios (disarmed, no body action)")
    print(f"live light_ok right now: {lp._light_ok()}  |  grace: {lp.REFLEX_GRACE_S}s\n")
    passed = 0
    for name, volts, on_chg, unresolved_s, light, expected in DRILLS:
        bat = {"volts": volts, "on_charger": on_chg, "ok": True}
        nrv = {"on_charger": on_chg}
        # set the persistence streak for this drill
        lp._emergency_since = None if unresolved_s is None else (time.time() - unresolved_s)
        # optional light override (to rehearse dark without turning off the lamp)
        real_light = lp._light_ok
        if light is not None:
            lp._light_ok = lambda _v=light: _v
        try:
            out = lp._emergency_reflex(bat, nrv, DISARMED)
        finally:
            lp._light_ok = real_light
        # extract the decision word
        if out is None:
            decision = None
        else:
            decision = next((d for d in ("WATCH", "PARK", "HOLD_DARK")
                             if d in out), "?")
        ok = decision == expected
        passed += ok
        mark = "PASS" if ok else "FAIL"
        exp = expected or "no-emergency"
        got = decision or "no-emergency"
        print(f"[{mark}] {name}\n        expected={exp:12s} got={got}")
    # cleanup scratch log
    try:
        _DRILL_LOG.unlink()
    except Exception:
        pass
    print(f"\n{passed}/{len(DRILLS)} drills passed.")
    return passed == len(DRILLS)


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
