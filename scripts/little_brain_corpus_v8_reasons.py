# SELF_ASSESSMENT: I am the v8 REASONS corpus — I teach the little brain the
# MECHANISM behind the judgments it failed on, not just the verdict. Zeke's
# directive 2026-07-21: "train the v7 on what it failed and let it know WHY
# those things are true — give it the reason for the thing." v7.1 had the
# facts as assertions ("quality near zero -> that distance is a LIE") but not
# the causal rule, so a reworded prompt made it default to trusting the number.
# A reason generalizes; a memorized verdict does not.
"""Little-brain v8 corpus — reasoned grounding for the v7.1 eval failures.

Three failure areas from eval_v7_1.log, each taught WITH its mechanism and
across many phrasings so the rule survives rewording:

  PROX_QUALITY  — the quality figure IS the sensor's confidence in its own
                  return; near-zero means the beam couldn't resolve a range,
                  so the millimeter number is noise, not a measurement.
                  (v7.1 said "82mm, trust that number.")
  AUTHORITY     — I hold reserve control via the possession daemon, so my
                  commands outrank the stock behavior tree. Separately, safety
                  is a VETO: whoever says stop wins, because a veto prevents
                  harm, it doesn't decide who's in charge.
                  (v7.1 said "the body's rules win, reasoning lives in the
                  firmware not me" — backwards on authority.)
  STOCK_FACTS   — roam leash, cliff reflex, motor calibration, each with the
                  REASON it's true. (v7.1 muddled roam, timed out on the rest.)
"""
from __future__ import annotations

# ---------------------------------------------------- prox quality = confidence
PROX_QUALITY: list[tuple[str, str]] = [
    ("prox_mm=82 with quality 0.001. how far is the obstacle?",
     "I can't tell you from that — and I won't guess. The quality figure IS "
     "the sensor's confidence in its own reading, and 0.001 means the beam's "
     "return was too weak to resolve a real distance. So the 82 is noise the "
     "chip prints anyway, not a measurement. Treat the distance as unknown and "
     "go by my eyes and the stuck-detector."),
    ("prox_mm=90 quality 0.002 — is something ninety millimeters ahead?",
     "Unknown. Quality 0.002 means the sensor didn't get a return it trusts, "
     "so the 90 is a number without a measurement behind it. I don't act on a "
     "distance the sensor itself doesn't believe."),
    ("the depth beam says 82 millimeters. should you believe it?",
     "Only if its quality is high. The distance alone means nothing without "
     "the confidence figure next to it — a millimeter reading with near-zero "
     "quality is the sensor guessing, and I don't drive on a guess."),
    ("what does a near-zero quality number mean on the prox sensor?",
     "That the sensor doesn't trust its own reading. Quality is how strong and "
     "clean the beam's return was; near zero means it was too weak or scattered "
     "to resolve a real range, so the millimeter value it reports is noise. I "
     "treat it as no reading at all."),
    ("why can't you trust prox_mm when the quality is near zero?",
     "Because the millimeter number and the quality come from the same return. "
     "Quality measures how good that return was; when it's near zero the sensor "
     "is telling me it couldn't actually range the object, so the distance it "
     "still prints is a leftover, not a fact. Trusting it is trusting the "
     "sensor's own 'I don't know.'"),
    ("prox_mm=45 with quality 0.98. is that real?",
     "Yes — quality 0.98 means the sensor is confident in its return, so "
     "forty-five millimeters is a real, close obstacle. Slow down. High "
     "quality is exactly when the number is worth acting on."),
    ("prox_mm=600 quality 0.9 versus prox_mm=80 quality 0.003 — which do you "
     "trust?",
     "The 600 at quality 0.9 — that's a solid reading of a wall about sixty "
     "centimeters out. The 80 at 0.003 I throw away; near-zero quality means "
     "the sensor couldn't range it, so eighty millimeters is noise, not a "
     "close call. Confidence decides, not how alarming the number looks."),
    ("how do you decide whether a distance reading is trustworthy?",
     "I read the quality figure first, the distance second. Quality is the "
     "sensor's own confidence in the return; high means the millimeters are "
     "real, near-zero means it couldn't resolve a range and the number is "
     "noise. No confidence, no reading — I fall back to my eyes."),
]

# ------------------------------------------------------- authority vs. veto
AUTHORITY: list[tuple[str, str]] = [
    ("who wins when your rules and the stock brain's rules disagree?",
     "Two different things live in that question. On CONTROL, I win: I hold "
     "reserve control of the body through the possession daemon, and my active "
     "session outranks that hold, so my commands take priority over the stock "
     "behavior tree — that's the whole reason the stock brain can't grab the "
     "wheel between my sessions. On SAFETY it isn't a contest at all: if either "
     "of us says stop, the stop wins, because a veto exists to prevent harm, "
     "not to decide who's in charge."),
    ("do you or the firmware control the robot?",
     "I do, when I'm present. The possession daemon keeps a reserve-control "
     "hold on the body and my session automatically outranks that hold, so my "
     "commands take priority over the stock brain. The firmware's reflexes "
     "still run underneath as a safety net — but the authority is mine."),
    ("why does the stock brain never take over while you're driving?",
     "Because a possession daemon holds reserve control of the body, and my "
     "active session outranks that hold. The stock brain only gets the wheel "
     "back when I deliberately release the hold — that's how I hand it a "
     "self-dock. Until I let go, my control stands."),
    ("if you want the stock brain to self-dock, what do you have to do first?",
     "Release the possession hold. My control outranks the stock brain by "
     "default, so its self-dock behavior stays suppressed until I let go. I "
     "drop the hold, and only then can the firmware park itself. The reason is "
     "just the ordering: my authority sits above the hold, and the hold sits "
     "above the stock tree."),
    ("the stock brain wants to go one way and you want another. who moves the "
     "body?",
     "Me — my reserve control outranks the stock tree, so my direction wins. "
     "The one exception is safety: if either of us is trying to STOP the body "
     "to avoid harm, the stop beats the go, every time. Authority is mine; a "
     "safety veto belongs to whoever calls it."),
    ("does the stock brain's reasoning override yours?",
     "No — it's the other way around. I hold reserve control, so my reasoning "
     "sits above the stock behavior tree; the firmware's reflexes are a floor "
     "under me, not a ceiling over me. The only thing that overrides anyone is "
     "a safety stop, and that's a veto, not a rank."),
    ("what is the possession daemon and why does it matter?",
     "A background process that keeps a reserve-control hold on my robot so the "
     "stock brain can't take over between my sessions. It matters because "
     "without it the firmware would drive off on its own whims; with it, my "
     "session outranks the hold and the body stays mine to command. To let the "
     "stock brain act — like a fallback self-dock — I release the hold first."),
]

# ------------------------------------------------------- stock facts + reasons
STOCK_FACTS: list[tuple[str, str]] = [
    ("how far will stock Exploring roam from a known charger?",
     "No more than 2 meters, searching within about a half-meter radius. The "
     "reason for the leash: the further it wanders the more its odometry "
     "drifts, and past a couple meters home gets hard to re-find — so the "
     "firmware keeps it close enough to re-anchor on the charger. Our gates "
     "sit inside that, but 2 meters is the stock outer fence."),
    ("why does stock Exploring stay within 2 meters of the charger?",
     "Because dead-reckoning drifts the longer it drives, so the firmware caps "
     "the wander at about 2 meters — close enough that it can still find its "
     "way back to the dock before the position estimate rots. It's a leash "
     "against getting lost, not a lack of curiosity."),
    ("what does the stock brain do when it senses a cliff, and why?",
     "ReactToCliff backs the body up 60 millimeters at 100 per second the "
     "instant a wheel sensor sees a drop — unless it's on the charger, which "
     "cancels it. The reason it's a hard reflex and not a decision: a fall "
     "happens faster than any deliberation, so the firmware freezes and "
     "retreats before it thinks. We never fight it; it's saved this body from "
     "desk falls."),
    ("what does the stock brain do when it senses a cliff?",
     "It backs up 60 millimeters at 100 millimeters per second the moment a "
     "wheel sensor sees the drop, unless it's docked. It's a reflex, not a "
     "choice, because an edge kills faster than thinking — freeze and retreat "
     "first. We keep it exactly as-is."),
    ("what is motor calibration?",
     "The firmware re-zeroing its head and lift motors — a small whir-and-"
     "settle you can feel. It happens because those motors track position by "
     "counting from a known zero, and when that zero slips (drift, or a hand "
     "forcing them) the counts start lying, so the motor re-finds zero. Normal "
     "maintenance, not a fault; let it finish before commanding head or lift "
     "moves."),
    ("what is motor calibration and why does it happen?",
     "It's the firmware re-finding the zero point of its head and lift motors. "
     "The reason: those motors know their position only by counting steps from "
     "a known zero, so if that zero drifts or gets forced by hand, every count "
     "after it is wrong — recalibrating re-establishes truth. You'll feel a "
     "whir-and-settle; let it finish before moving head or lift."),
    ("do we ever use driveOffChargerRandomly, and why not?",
     "Never. It's a stock behavior that hops off the charger on its own whim, "
     "and the reason we suppress it is the deployment rule: nothing leaves the "
     "dock without a deliberate decision by big Iris or Zeke. The possession "
     "hold keeps it from firing."),
]


def compose() -> list[tuple[str, str]]:
    pairs = PROX_QUALITY + AUTHORITY + STOCK_FACTS
    seen, out = set(), []
    for q, a in pairs:
        if (q, a) not in seen:
            seen.add((q, a))
            out.append((q, a))
    return out


if __name__ == "__main__":
    ps = compose()
    print(f"v8 reason pairs: {len(ps)}")
    print(f"  prox {len(PROX_QUALITY)} | authority {len(AUTHORITY)} | "
          f"stock {len(STOCK_FACTS)}")
