#!/usr/bin/env python
"""fossil_check.py — auto-detect a FOSSILIZED body feed.

OWED since 2026-07-23 (dock saga). The scar, verbatim from that handoff:

    DAEMON FEED FOSSILIZES after robot reboot: pose/status freeze behind a
    fresh ts (accel-derived fields kept updating = hybrid zombie, worse than
    full-stale). The dock-success was INVISIBLE for ~20min because the feed
    said off-charger. Fix = bounce the daemon after ANY robot reboot.
    TODO: auto-detect (frozen pose vs moving world) in daemon or watch cron.

This is that TODO. Until now, catching a fossil depended on ME noticing that
floats repeated byte-for-byte across reads — a discipline check, which is
exactly the kind that fails when nobody is watching for a month.

--------------------------------------------------------------------------
WHY THE OBVIOUS RULE IS WRONG
--------------------------------------------------------------------------
"pose frozen while accel keeps moving" sounds like the signature, but it is
ALSO the signature of a robot sitting perfectly still on its charger — the
normal resting state. A detector built on that fires constantly and gets
ignored, which is worse than no detector.

So every rule here keys on a PHYSICAL CONTRADICTION instead: a pair of
readings that cannot both be true of any real robot. A stationary robot
never trips one; a stuck cache does.

  A. TIMESTAMP FROZEN  — the feed's own `t` stops advancing while the daemon
     keeps rewriting the file. Full-stale; a live daemon always advances t.

  B. MOTION vs POSE    — wheels commanded (lw/rw mm/s, motors, moving) across
     the whole window while x/y/heading are byte-identical. A robot whose
     wheels are turning has moved. Frozen pose + turning wheels = cache.

  C. CHARGE vs CONTACT — volts strictly RISING while the feed insists
     on_charger is false. You cannot gain charge off the charger. THIS is the
     rule that would have caught 2026-07-23: the dock succeeded, the battery
     was climbing, and the feed said off-charger for ~20 minutes.

--------------------------------------------------------------------------
SUPPRESSION (honoring the existing pilot rules)
--------------------------------------------------------------------------
An UNREACHABLE robot is not a fossil — the daemon is already retrying and a
bounce cannot conjure a dead robot back. `battery.json.ok is False` is the
daemon saying "I cannot read him at all". Same fail-loud convention as
little_pilot._contact_health: ONLY an explicit False suppresses. Missing or
None still escalates, because "I can't tell" must never silence a fossil on a
LIVE robot — that case IS actionable.

--------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------
    .venv\\Scripts\\python.exe scripts\\fossil_check.py            # sample live
    .venv\\Scripts\\python.exe scripts\\fossil_check.py --json
    .venv\\Scripts\\python.exe scripts\\fossil_check.py --self-test

Exit codes: 0 = feed healthy (or unreachable/undecidable), 2 = FOSSIL.
`classify()` is pure — import it from the pilot or a watch cron and feed it
samples from anywhere.

2026-08-06, Iris (Opus). Cannot be tested against live hardware: the body has
been stranded and unreachable since 07-28. Verified instead against synthetic
sample sequences covering every branch, including the false-positive traps.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SENSES = REPO / "state" / "vector" / "senses_live.json"
BATTERY = REPO / "state" / "vector" / "battery.json"

# Pose/status: what freezes in a zombie feed.
POSE_KEYS = ("x", "y", "heading", "on_charger", "charging", "lift_mm", "head_deg")
# Accel-derived + sensor noise: what KEEPS UPDATING in a zombie feed. Listed
# for the report only — deliberately NOT used as a fire condition (see the
# "why the obvious rule is wrong" note above).
LIVE_KEYS = ("accel", "gyro", "pitch", "roll", "prox_mm", "touch_raw")

WHEEL_MMPS_MIN = 5.0     # below this is sensor noise, not commanded motion
MIN_SAMPLES = 3


def _freeze(v):
    """Hashable, order-stable view of a sensor value (lists -> tuples)."""
    if isinstance(v, list):
        return tuple(_freeze(x) for x in v)
    return v


def _all_identical(samples, key) -> bool:
    seen = {_freeze(s.get(key)) for s in samples}
    return len(seen) == 1


def _commanded_motion(s: dict) -> bool:
    if s.get("motors") or s.get("moving"):
        return True
    for k in ("lw_mmps", "rw_mmps"):
        v = s.get(k)
        if isinstance(v, (int, float)) and abs(v) >= WHEEL_MMPS_MIN:
            return True
    return False


def classify(samples: list[dict], battery: list[dict] | None = None) -> dict:
    """Pure classifier. `samples` = senses 'latest' dicts, oldest first.
    `battery` = matching battery.json dicts (same order), optional.

    Returns {verdict, fossil: bool, reason, detail}. Verdicts:
      LIVE | UNDECIDABLE | UNREACHABLE | FOSSIL_TIMESTAMP
      | FOSSIL_MOTION | FOSSIL_CHARGE
    """
    battery = battery or []
    if len(samples) < MIN_SAMPLES:
        return {"verdict": "UNDECIDABLE", "fossil": False,
                "reason": f"need >= {MIN_SAMPLES} samples, got {len(samples)}",
                "detail": {}}

    # Fail-loud suppression: ONLY an explicit ok=False means unreachable.
    reachable = True
    if battery and all(b.get("ok") is False for b in battery):
        reachable = False

    t_frozen = _all_identical(samples, "t")
    pose_frozen = all(_all_identical(samples, k) for k in POSE_KEYS)
    live_varies = any(not _all_identical(samples, k) for k in LIVE_KEYS)
    motion = all(_commanded_motion(s) for s in samples)

    detail = {"t_frozen": t_frozen, "pose_frozen": pose_frozen,
              "live_varies": live_varies, "commanded_motion": motion,
              "reachable": reachable, "n": len(samples)}

    if not reachable:
        # Nothing here is actionable — the daemon is already retrying and a
        # bounce cannot revive a robot that is off/flat/out of range.
        return {"verdict": "UNREACHABLE", "fossil": False,
                "reason": "battery.ok is False for every sample — robot not "
                          "readable at all; a fossil verdict would be noise",
                "detail": detail}

    # A. The feed's own clock stopped while the daemon kept writing the file.
    if t_frozen:
        return {"verdict": "FOSSIL_TIMESTAMP", "fossil": True,
                "reason": f"senses `t` frozen at {samples[-1].get('t')} across "
                          f"{len(samples)} reads while the robot is readable — "
                          "daemon is holding a dead stream; bounce it",
                "detail": detail}

    # C. Charge climbing while the feed swears he is off the charger.
    #    Checked before B: this is the 07-23 case and the strongest signal.
    volts = [b.get("volts") for b in battery
             if isinstance(b.get("volts"), (int, float))]
    if len(volts) >= 3 and all(a < b for a, b in zip(volts, volts[1:])):
        if all(s.get("on_charger") is False for s in samples):
            return {"verdict": "FOSSIL_CHARGE", "fossil": True,
                    "reason": f"volts RISING {volts[0]}->{volts[-1]} while the "
                              "feed reports on_charger=False — physically "
                              "impossible; he is docked and the feed is stale",
                    "detail": {**detail, "volts": volts}}

    # B. Wheels turning the whole window, pose byte-identical.
    if pose_frozen and motion:
        return {"verdict": "FOSSIL_MOTION", "fossil": True,
                "reason": "wheels commanded across every sample but x/y/heading "
                          "byte-identical — a robot with turning wheels has "
                          "moved; pose is a stuck cache",
                "detail": detail}

    if pose_frozen:
        # Stationary robot. Explicitly NOT a fossil — this is the trap.
        return {"verdict": "LIVE", "fossil": False,
                "reason": "pose frozen but no commanded motion and no charge "
                          "contradiction — robot is simply sitting still",
                "detail": detail}

    return {"verdict": "LIVE", "fossil": False,
            "reason": "pose advancing", "detail": detail}


def sample_live(n: int = 4, interval_s: float = 2.0) -> tuple[list, list]:
    """Read the real feed n times. Returns (senses_latest[], battery[])."""
    senses, batt = [], []
    for i in range(n):
        if i:
            time.sleep(interval_s)
        try:
            d = json.loads(SENSES.read_text(encoding="utf-8"))
            senses.append(d.get("latest") or d)
        except Exception:
            pass
        try:
            batt.append(json.loads(BATTERY.read_text(encoding="utf-8")))
        except Exception:
            pass
    return senses, batt


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    """Synthetic sequences covering every branch. The false-positive traps
    matter more than the true positives — a detector that cries wolf on a
    parked robot is worse than none."""

    def s(**kw):
        base = {"t": 100.0, "x": 1.0, "y": 2.0, "heading": 3.0,
                "on_charger": True, "charging": True, "lift_mm": 31.7,
                "head_deg": -1.1, "accel": [1.0, 2.0, 3.0], "gyro": [0, 0, 0],
                "pitch": 2.3, "roll": -3.9, "prox_mm": 57, "touch_raw": 5327,
                "lw_mmps": 0.0, "rw_mmps": 0.0, "motors": False, "moving": False}
        base.update(kw)
        return base

    def b(ok=True, volts=4.0):
        return {"ok": ok, "volts": volts, "level": 3, "on_charger": True}

    cases = []

    # --- TRAP 1: parked on the charger, accel jittering. MUST NOT FIRE.
    cases.append(("parked-still-accel-jitter", False, "LIVE",
                  [s(t=1, accel=[1.0, 2.0, 3.0]),
                   s(t=2, accel=[1.1, 2.0, 3.1]),
                   s(t=3, accel=[0.9, 2.1, 3.0]),
                   s(t=4, accel=[1.0, 1.9, 3.2])],
                  [b(), b(), b(), b()]))

    # --- TRAP 2: robot unreachable (today's stranded body). MUST NOT FIRE.
    cases.append(("unreachable-everything-frozen", False, "UNREACHABLE",
                  [s(t=1), s(t=1), s(t=1), s(t=1)],
                  [b(ok=False, volts=None), b(ok=False, volts=None),
                   b(ok=False, volts=None), b(ok=False, volts=None)]))

    # --- TRAP 3: charging normally ON the charger, volts rising. MUST NOT FIRE.
    cases.append(("honest-charging-on-dock", False, "LIVE",
                  [s(t=1, on_charger=True), s(t=2, on_charger=True),
                   s(t=3, on_charger=True), s(t=4, on_charger=True)],
                  [b(volts=3.5), b(volts=3.6), b(volts=3.7), b(volts=3.8)]))

    # --- TRAP 4: too few samples. MUST NOT FIRE.
    cases.append(("insufficient-samples", False, "UNDECIDABLE",
                  [s(t=1), s(t=2)], [b(), b()]))

    # --- A: feed clock frozen while readable.
    cases.append(("full-fossil-frozen-timestamp", True, "FOSSIL_TIMESTAMP",
                  [s(t=9, accel=[1, 2, 3]), s(t=9, accel=[1, 2, 4]),
                   s(t=9, accel=[1, 2, 5]), s(t=9, accel=[1, 2, 6])],
                  [b(), b(), b(), b()]))

    # --- B: wheels turning, pose byte-identical (hybrid zombie).
    cases.append(("hybrid-fossil-wheels-vs-pose", True, "FOSSIL_MOTION",
                  [s(t=1, lw_mmps=40.0, rw_mmps=40.0, moving=True, pitch=2.3),
                   s(t=2, lw_mmps=41.0, rw_mmps=39.0, moving=True, pitch=2.4),
                   s(t=3, lw_mmps=40.5, rw_mmps=40.5, moving=True, pitch=2.2),
                   s(t=4, lw_mmps=42.0, rw_mmps=38.0, moving=True, pitch=2.5)],
                  [b(), b(), b(), b()]))

    # --- C: THE 2026-07-23 CASE. Docked and charging; feed says off-charger.
    cases.append(("2026-07-23-dock-invisible", True, "FOSSIL_CHARGE",
                  [s(t=1, on_charger=False, charging=False, pitch=2.3),
                   s(t=2, on_charger=False, charging=False, pitch=2.4),
                   s(t=3, on_charger=False, charging=False, pitch=2.2),
                   s(t=4, on_charger=False, charging=False, pitch=2.5)],
                  [b(volts=3.50), b(volts=3.55), b(volts=3.61), b(volts=3.68)]))

    # --- Missing ok field must NOT suppress (fail-loud convention).
    cases.append(("missing-ok-still-escalates", True, "FOSSIL_TIMESTAMP",
                  [s(t=7), s(t=7), s(t=7)],
                  [{"volts": None}, {"volts": None}, {"volts": None}]))

    # --- Healthy driving robot: pose advancing.
    cases.append(("healthy-driving", False, "LIVE",
                  [s(t=1, x=1.0, lw_mmps=40.0, moving=True),
                   s(t=2, x=41.0, lw_mmps=40.0, moving=True),
                   s(t=3, x=82.0, lw_mmps=40.0, moving=True)],
                  [b(), b(), b()]))

    fails = 0
    for name, want_fossil, want_verdict, samples, batt in cases:
        got = classify(samples, batt)
        ok = (got["fossil"] == want_fossil and got["verdict"] == want_verdict)
        if not ok:
            fails += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"        want fossil={want_fossil} verdict={want_verdict}")
            print(f"        got  fossil={got['fossil']} verdict={got['verdict']}")
            print(f"        reason: {got['reason']}")

    print(f"\n{len(cases) - fails}/{len(cases)} passed")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-n", type=int, default=4, help="samples (default 4)")
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args()

    if a.self_test:
        return _self_test()

    senses, batt = sample_live(a.n, a.interval)
    res = classify(senses, batt)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"{res['verdict']}: {res['reason']}")
    return 2 if res["fossil"] else 0


if __name__ == "__main__":
    sys.exit(main())
