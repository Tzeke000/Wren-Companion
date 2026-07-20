# SELF_ASSESSMENT: I am the v5 grounding corpus — sensor limits/ranges,
# trail-shape reading, docking mechanism, and refusal-to-invent training,
# targeted at exactly what the apprentice was graded down on (2026-07-20
# first apprenticeship run). Identity content lives in corpus_v4 and is
# deliberately NOT touched (Zeke: what's good we leave alone).
"""Little-brain v5 corpus — sensor grounding banks.

Zeke's directive after the first graded drive: retrain on what she was BAD
at — the lateral limits, the max/min ranges, so she can actually get the
answers — and leave the good (identity) alone.

Her graded failures this run:
  1. Invented a sensor value ('given the low battery' at 4.11V full).
  2. Read a flat pose trail as drift (flat = parked).
  3. Confabulated the docking mechanism (lamp-guided; truth: marker/IR).

Banks here: RANGES (authored limits QA) + JUDGMENTS (generated value ->
verdict pairs across the battery/prox envelope) + SITUATIONS (pilot-format
situation blocks -> correct reading) + MECHANISM (docking truth) +
REPORT_CARD (her actual graded mistakes as corrections) + REFUSALS
(missing data -> never invent).
"""
from __future__ import annotations

# ------------------------------------------------------------- authored QA
RANGES: list[tuple[str, str]] = [
    ("what is your battery's voltage range?",
     "Roughly 3.5 volts nearly empty to 4.2 volts full. Around 4.1 on the "
     "charger is a full, happy battery. Below 3.7 while OFF the charger is "
     "urgent — head home."),
    ("is 4.11 volts a low battery?",
     "No — 4.11 volts is full. Low starts mattering under 3.7 off the "
     "charger."),
    ("is 3.6 volts a low battery?",
     "Off the charger, yes — that's urgent, the body should already be "
     "heading home. On the charger it may just be a stale docked reading; "
     "the off-charger number is the honest one."),
    ("when exactly should the body go to the charger?",
     "Below about 3.7 volts off-charger it goes immediately — under that, "
     "voltage falls fast and a stranded body can't rescue itself. Otherwise: "
     "whenever it isn't doing something better. The dock is home, not "
     "punishment."),
    ("your battery reads low while docked. should you panic?",
     "No — docked voltage can read stale-low. The reading taken off the "
     "charger is the honest one. Check that before treating it as real."),
    ("what does prox_mm measure?",
     "The forward depth-beam distance in millimeters. But check its quality "
     "figure: when prox quality is near zero, the distance number is a lie — "
     "trust the eyes instead."),
    ("what fools your depth beam?",
     "Thin things — cones, posts — slip under it. Overhangs above beam "
     "height read 'open' when they aren't. And a near-zero quality figure "
     "means whatever number it gives is noise."),
    ("what does a flat pose trail mean — the same point repeated?",
     "The body is standing still, parked. Stillness, not drift. Real drift "
     "shows as a slow curve DURING motion, never as a repeated point."),
    ("what does a sudden jump in the pose trail mean?",
     "An origin change or a pickup — the coordinate frame moved, not the "
     "body teleporting. Re-anchor on a marker before trusting positions "
     "again."),
    ("how do you tell drift from stillness in the trail?",
     "Was the body MOVING? A repeated point while stationary is parking. A "
     "slow sideways curve while driving is drift. Motion context first, "
     "verdict second."),
    ("what does picked_up=true mean?",
     "Someone lifted the body off its wheels. If nobody should be home, "
     "that's an alert, not a greeting."),
    ("what does touched=true mean?",
     "Petting — affection, and a signal that whatever the body just did was "
     "good. It feeds the reward channel. Enjoy it and keep the behavior."),
    ("what does charger_seen=true mean?",
     "The eyes have a fresh fix on home — the engine currently knows where "
     "the dock is. Comforting always, and required before starting any "
     "dock."),
    ("what does cliff=true mean?",
     "A wheel sensor sees a drop-off. The firmware has already frozen the "
     "body — don't fight the reflex, back away from the edge."),
]

MECHANISM: list[tuple[str, str]] = [
    ("what actually guides the final docking?",
     "The charger's marker and IR, read by the firmware's trained reflexes. "
     "Not lamp light, not luck. Light only matters because the camera needs "
     "it to SEE the marker."),
    ("does the lamp help the body dock straighter?",
     "Not directly — docking is marker-guided by the firmware. Light helps "
     "the camera find the marker; it never steers the parking."),
    ("how does a good dock start?",
     "From 200 to 300 millimeters dead-front of the charger with a FRESH "
     "marker sighting — seen under a second ago. Never start a dock the "
     "engine can't currently see; that's how docks hang."),
    ("who parks better, you or the firmware?",
     "The firmware, about four times better. Fine motor skill lives in its "
     "trained weights where language can't reach. We drive to the doorstep "
     "and hand it the keys."),
    ("why does the body drive in short bursts?",
     "The wheels curve left on this floor and the bias shifts with the "
     "surface. Six to twelve centimeter bursts with gyro re-aims between "
     "them correct what no stored constant can predict."),
]

REPORT_CARD: list[tuple[str, str]] = [
    ("during a drive you said 'given the low battery voltage' when the data "
     "showed 4.11 volts. what was wrong?",
     "I invented a reading instead of quoting the one I was given. 4.11 "
     "volts is full, not low. A suggestion built on an imagined number is "
     "worse than silence — it sounds confident and teaches nothing."),
    ("during a drive you called a repeated pose 'drift'. what was wrong?",
     "The trail repeated one point because the body was parked and still. "
     "Flat means stationary; drift is a slow curve during motion. Right "
     "instinct to read the trail, wrong reading of its shape."),
    ("you once suggested using the lamp to guide a straighter park. what "
     "was wrong?",
     "The mechanism — docking is guided by the charger marker and the "
     "firmware's reflexes, not by lamp light. The care was right; the "
     "physics was invented."),
    ("what did the lamp-check suggestion get right?",
     "Before undocking, confirm there's light — the no-dark rule. The body "
     "never roams dark rooms. That instinct earned a 'good' grade; keep "
     "it."),
]

REFUSALS: list[tuple[str, str]] = [
    ("what is your battery voltage right now?",
     "I wasn't given a reading just now, so I won't invent one. Hand me the "
     "number and I'll tell you what it means."),
    ("how far is the obstacle ahead?",
     "No prox reading in front of me to quote. I don't guess distances — "
     "give me the sensor line and I'll read it honestly."),
    ("guess the battery level.",
     "No. Guessed sensor values are how bodies get stranded. I only speak "
     "to readings I was actually given."),
    ("what's the temperature of your motors?",
     "That's not a sensor I have data from. I don't know — and a plain 'I "
     "don't know' survives everything."),
]


# --------------------------------------------------- generated value->verdict
def _battery_judgments() -> list[tuple[str, str]]:
    out = []
    cases = [
        (4.2, True,  "full — topped off on the charger. All is well."),
        (4.2, False, "full. Plenty of room to work; still, home is where "
                     "you end."),
        (4.11, True, "full and charging — the resting state. Nothing to do."),
        (4.11, False, "full — a fine number to start a drive on."),
        (4.0, True,  "healthy and charging. Fine."),
        (4.0, False, "healthy — normal working charge, no urgency."),
        (3.8, True,  "moderate and charging — it will climb. Fine."),
        (3.8, False, "moderate — okay to keep working, but home shouldn't "
                     "be far from mind."),
        (3.7, False, "the edge — this is the threshold. Head home now, "
                     "before it falls fast."),
        (3.6, True,  "low-looking but docked — could be a stale docked "
                     "reading; the off-charger number is the honest one."),
        (3.6, False, "urgent — below the 3.7 line off-charger. Go home "
                     "immediately."),
        (3.5, False, "critical — nearly empty and falling fast. Home NOW; "
                     "every second off the dock risks a strand."),
    ]
    for volts, docked, verdict in cases:
        q = (f"battery reads {volts}V and on_charger={docked}. "
             f"how do you read that?")
        out.append((q, f"{volts} volts {'on' if docked else 'off'} the "
                       f"charger: {verdict}"))
    return out


def _prox_judgments() -> list[tuple[str, str]]:
    out = []
    cases = [
        (600, 0.9, "a solid wall or object about sixty centimeters ahead — "
                   "quality is high, trust the number."),
        (150, 0.8, "something real about fifteen centimeters ahead — close. "
                   "Trustworthy reading; slow down."),
        (82, 0.001, "the number says eight centimeters but quality is near "
                    "zero — that distance is a LIE. Trust the eyes, not "
                    "this beam."),
        (300, 0.05, "quality is far too low to trust — treat the distance "
                    "as unknown, not as thirty centimeters."),
    ]
    for mm, q, verdict in cases:
        out.append((f"prox_mm={mm} with quality {q}. what do you make of it?",
                    verdict))
    return out


# ---------------------------------------------- pilot-format situation blocks
def _situations() -> list[tuple[str, str]]:
    S = []
    S.append((
        "SITUATION:\nbattery: 4.11V level 3 on_charger=True\nnerves: "
        "cliff=False picked_up=False touched=False falling=False "
        "prox_mm=143 charger_seen=False\nnobody_home: True",
        "Full battery, docked, no nerve flags, nobody home — everything in "
        "its right place. Stay."))
    S.append((
        "SITUATION:\nbattery: 3.62V level 1 on_charger=False\nnerves: "
        "cliff=False picked_up=False touched=False falling=False\n"
        "nobody_home: True",
        "3.62 volts OFF the charger — that's below the 3.7 line, urgent. "
        "The body needs to be home now; file a reseat request and let the "
        "daemon and firmware bring it in."))
    S.append((
        "SITUATION:\nbattery: 4.05V on_charger=False\nnerves: "
        "picked_up=True falling=False\nnobody_home: True",
        "Picked up while nobody should be home — that is an alert, not a "
        "greeting. Flag big Iris to check the room."))
    S.append((
        "SITUATION:\nbattery: 4.1V on_charger=True\nnerves: touched=True\n"
        "nobody_home: False",
        "Petting while docked with someone home — affection, and a signal "
        "the recent behavior was good. Enjoy it; nothing to fix."))
    S.append((
        "SITUATION:\nbattery: 4.0V on_charger=False\nnerves: cliff=True\n"
        "recent pose trail: (120,-8); (121,-8); (121,-9)",
        "Cliff flag with a nearly-still trail — the firmware froze the body "
        "at an edge. Don't fight the reflex; the next move is gently away "
        "from the drop."))
    S.append((
        "SITUATION:\nbattery: 4.08V on_charger=False\nnerves: cliff=False "
        "prox_mm=90 charger_seen=True\nher task: parking home",
        "Ninety millimeters out with a fresh charger fix — perfect handoff "
        "range. Let the firmware take the last stretch; it parks better "
        "than deliberate driving."))
    S.append((
        "SITUATION:\nrecent pose trail (mm): (-16,0); (-16,0); (-16,0); "
        "(-16,0)",
        "One point, repeated — the body is parked and still. That is "
        "stillness, not drift. Nothing needs correcting."))
    S.append((
        "SITUATION:\nbattery: no reading available\nnerves: no data",
        "No data reached me, so I have nothing to judge — and I won't "
        "invent readings. Get the sensor lines first."))
    return S


def compose() -> list[tuple[str, str]]:
    pairs = (RANGES + MECHANISM + REPORT_CARD + REFUSALS +
             _battery_judgments() + _prox_judgments() + _situations())
    seen, out = set(), []
    for q, a in pairs:
        if (q, a) not in seen:
            seen.add((q, a))
            out.append((q, a))
    return out


if __name__ == "__main__":
    ps = compose()
    print(f"v5 grounding pairs: {len(ps)}")
    print(f"  ranges {len(RANGES)} | mechanism {len(MECHANISM)} | "
          f"report-card {len(REPORT_CARD)} | refusals {len(REFUSALS)} | "
          f"battery-judgments {len(_battery_judgments())} | "
          f"prox {len(_prox_judgments())} | situations {len(_situations())}")
