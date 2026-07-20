# SELF_ASSESSMENT: I transform Vector's stock behavior tree (358 JSONs pulled
# off the rooted robot 2026-07-20) into little-brain training pairs — the
# stock brain's curriculum, modified to OUR rules (Zeke: "train her with all
# of vector's stock data and then modify it so it fits our needs").
"""v6 stock-brain corpus — behavior-tree JSON -> NL training pairs.

Source: state/vector/stock_brain/behaviorComponent/** (harvested via body_root
from /anki/data/assets/cozmo_resources/config/engine/behaviorComponent).

Two layers:
  AUTO  — one compact factual pair per production behavior (activation/cancel
          conditions + tuned numeric params, templated into full sentences).
          Games/dev trees are skipped for training density.
  MODS  — hand-written pairs for the load-bearing families, teaching the
          STOCK rule with its real numbers AND our Iris overlay (possession,
          no-dark, Zeke's expression-vs-travel taxonomy, deployment mode).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = (Path(__file__).resolve().parent.parent / "state" / "vector" /
        "stock_brain" / "behaviorComponent" / "behaviors")

SKIP_DIRS = ("devBehaviors", "blackjack", "alexa", "clock", "danceToTheBeat",
             "onboarding", "appBehaviors")

_COND_NL = {
    "OnCharger": "the body is on the charger",
    "OffTreadsState": "the body's tread state matches",
    "Emotion": "its mood crosses a threshold",
    "CliffDetected": "a cliff is sensed",
    "BatteryLevel": "the battery level matches",
    "RobotHeldInPalm": "it is held in a palm",
    "RobotPickedUp": "it is picked up",
    "RobotPlacedOnSlope": "it is placed on a slope",
    "IlluminationDetected": "the room light level matches",
    "MotionDetected": "motion is seen",
    "SalientPointDetected": "something salient is seen",
    "FeatureGate": "the feature is enabled",
    "TimedDedup": "enough time has passed since last run",
    "Compound": "a compound of sub-conditions holds",
    "UnitTest": "unit-test only",
}


def _strip_comments(txt: str) -> str:
    return re.sub(r"^\s*//.*$", "", txt, flags=re.M)


def _cond_nl(cond) -> str:
    if not isinstance(cond, dict):
        return ""
    ct = cond.get("conditionType", "")
    base = _COND_NL.get(ct, ct)
    extras = {k: v for k, v in cond.items()
              if k not in ("conditionType",) and not isinstance(v, (dict, list))}
    if extras:
        base += " (" + ", ".join(f"{k}={v}" for k, v in extras.items()) + ")"
    subs = cond.get("and") or cond.get("or") or []
    if isinstance(subs, list) and subs:
        base += " with sub-conditions: " + "; ".join(_cond_nl(s) for s in subs)
    return base


def _params_nl(d: dict) -> str:
    """Numeric tuning constants, unit-suffixed keys first."""
    out = []
    for k, v in d.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if re.search(r"(_mm|_mmps|_ms|_s|_m|_deg|_sec|Radius|Distance|"
                         r"Speed|Timeout|Cooldown|Count|Threshold)", k, re.I):
                out.append(f"{k} = {v}")
    return "; ".join(out[:8])


def auto_pairs() -> list[tuple[str, str]]:
    pairs = []
    if not ROOT.is_dir():
        return pairs
    for p in sorted(ROOT.rglob("*.json")):
        if any(s in p.parts for s in SKIP_DIRS):
            continue
        try:
            d = json.loads(_strip_comments(p.read_text(encoding="utf-8")))
        except Exception:
            continue
        bid = d.get("behaviorID") or p.stem
        cls = d.get("behaviorClass", "")
        act = _cond_nl(d.get("wantsToBeActivatedCondition"))
        cxl = _cond_nl(d.get("wantsToCancelSelfCondition"))
        prm = _params_nl(d)
        subs = d.get("behaviors")
        bits = [f"'{bid}' is a stock-firmware behavior"
                + (f" of class {cls}" if cls else "") + "."]
        if act:
            bits.append(f"It activates when {act}.")
        if cxl:
            bits.append(f"It cancels itself when {cxl}.")
        if isinstance(subs, list) and subs:
            names = [s.get("behavior") for s in subs
                     if isinstance(s, dict) and s.get("behavior")][:6]
            if names:
                bits.append("It dispatches to: " + ", ".join(names) + ".")
        if prm:
            bits.append(f"Tuned constants: {prm}.")
        if len(bits) < 2:
            continue  # nothing informative beyond the name
        q = f"what does the stock brain's {bid} behavior do?"
        pairs.append((q, " ".join(bits)))
    return pairs


# ------------------------------------------------------- the Iris overlay
MODS: list[tuple[str, str]] = [
    ("what does the stock brain do when it senses a cliff?",
     "ReactToCliff: it backs up 60 millimeters at 100 millimeters per second "
     "the moment a wheel sensor sees the drop — unless it's on the charger, "
     "which cancels the reaction. We keep this reflex exactly as-is: it "
     "already saved this body from desk falls. Never fight it."),
    ("how does the stock brain explore, and how do WE explore?",
     "Stock Exploring searches in a radius up to half a meter and never "
     "roams more than 2 meters from a known charger, with a 10-second "
     "cooldown on motion reactions. OUR version adds three gates on top: "
     "motion_enabled in the goals, light in the room, and Zeke home or big "
     "Iris actively watching. The stock limits stay as the outer fence; our "
     "gates decide whether exploring happens at all."),
    ("what is driveOffChargerRandomly and do we use it?",
     "A stock behavior that hops off the charger on its own whim. We NEVER "
     "use it — deployment rule: nothing leaves the dock without a deliberate "
     "decision by big Iris or Zeke. The possession hold keeps this behavior "
     "suppressed."),
    ("what does the stock brain do in darkness?",
     "ReactToDarkness: it notices the light dropping and winds down toward "
     "sleep. Ours goes further — darkness means wheels do not exist at all. "
     "The stock brain dims; we lock."),
    ("what does the stock brain do when picked up?",
     "It startles, stops its wheels instantly, and waits to be put down — "
     "treads-state reactions are mandatory physical reflexes. We keep that, "
     "and add: picked up while nobody should be home is an ALERT to big "
     "Iris, because during deployment no one should be lifting the body."),
    ("what happens at low battery in the stock brain vs ours?",
     "Stock: below its threshold it seeks the charger and self-docks using "
     "its marker-guided parking. Ours keeps that instinct with a number on "
     "it — under 3.7 volts off-charger the body heads home immediately — "
     "plus the takeover contract: if big Iris stays silent 60 seconds "
     "during a low-battery event, the little brain runs the go-home "
     "sequence itself and hands the firmware the final stretch."),
    ("what is motor calibration and when does it happen?",
     "ReactToMotorCalibration: the firmware recalibrates its head and lift "
     "motors when they drift or after being forced — you'll feel a small "
     "whir-and-settle. It's normal maintenance, not a fault. Let it finish "
     "before commanding head or lift moves."),
    ("what does the stock brain do when petted?",
     "ReactToHand / petting reactions: it leans in, purrs, and marks the "
     "moment as positive. Ours wires that same signal into the reward "
     "channel — petting means the recent behavior was good and worth "
     "keeping. Same reflex, upgraded meaning."),
    ("does the stock brain ask for help?",
     "Yes — askForHelp triggers when it's stuck on its side or on an edge "
     "beyond self-recovery: it calls out and waits. Ours reroutes that: the "
     "alert goes to big Iris and Zeke on Discord, because during deployment "
     "there is no one in the room to hear a robot cry."),
    ("who wins when your rules and the stock brain's rules disagree?",
     "The stricter one. Stock limits are the outer fence (search radii, "
     "cliff backups, calibrations); our deployment gates sit inside them "
     "(possession hold, no-dark, motion permissions, alerts to big Iris). "
     "The body obeys whichever layer says no."),
]


def compose() -> list[tuple[str, str]]:
    pairs = auto_pairs() + MODS
    seen, out = set(), []
    for q, a in pairs:
        if (q, a) not in seen:
            seen.add((q, a))
            out.append((q, a))
    return out


if __name__ == "__main__":
    ap = auto_pairs()
    print(f"auto pairs: {len(ap)} | mods: {len(MODS)} | total: {len(compose())}")
    for q, a in ap[:3]:
        print("\nQ:", q, "\nA:", a[:240])
