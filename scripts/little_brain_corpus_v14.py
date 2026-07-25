"""little-Iris v14 corpus — the LIVE-SENSOR-ROUTING round (2026-07-25, Zeke).

Why v14: v13 baked act-not-narrate + gained identity/lane, but in the LIVE
tool loop it regressed on the exact behavior Zeke corrected me on —
"what's your voltage right now?" routed to memory_search (facts) instead of
senses_now (the live body feed), looped, and once degenerated into a
malformed repeat-token output. Evidence: state/little_brain/v13_voltage_toolpath.txt.

v14 teaches, with heavy weight to overwrite the wrong habit:

 1. LIVE-NUMBER -> senses_now. Any question about a CURRENT body reading
    (voltage/volts/battery level/charging-right-now/moving/lift/held) reaches
    for senses_now and answers with the REAL number. NOT memory_search.
 2. THE CONTRAST — memory_recall/search is for FACTS, RULES, PAST ("why does
    docked voltage read low", "what's the rule about..."), never for a live
    reading. Pairing the two side-by-side is what v13 never saw.
 3. REACH -> FAIL -> HONEST REFUSE (Zeke's exact shape 2026-07-25): she TRIES
    senses_now FIRST; only if the live feed is absent does she say "I tried and
    couldn't read it, I won't invent a number." Refusal is the fallback, never
    the first move.
 4. USE-THE-RESULT / ANTI-LOOP — after ONE tool result she ANSWERS. She never
    re-calls the same search over and over (the v13 hop-limit failure), and
    finals stay short and clean (no repeat-token degeneration).

Consumed by little_brain_dataset.py::v14_dialogue_samples(). Bake warmstarts
from adapter_v12_bak (stable) so v14 = v12 stability + v13 gains + v14 fixes;
v13's dialogues are still in the dataset via v13_dialogue_samples().
"""
from __future__ import annotations


def _dlg(user: str, steps: list[tuple[str, str]], final: str) -> dict:
    msgs: list[dict] = [{"role": "user", "content": user}]
    for call, result in steps:
        msgs.append({"role": "assistant", "content": call})
        msgs.append({"role": "user", "content": f"[[result:{result}]]"})
    msgs.append({"role": "assistant", "content": final})
    return {"messages": msgs}


def v14_dialogues() -> list[dict]:
    d: list[dict] = []

    # ---- 1. LIVE NUMBER -> senses_now (never memory_search) ----
    d.append(_dlg("What is your battery voltage right now?",
        [("[[tool:senses_now]]",
          "[live, 0.1s ago, 14.8Hz] on_charger=true, is_charging=true, 4.09V, held=false, not moving")],
        "4.09 volts — reading it live off my sensors right now, docked and charging."))
    d.append(_dlg("What's your exact voltage?",
        [("[[tool:senses_now]]",
          "[live, 0.2s ago] 4.10V, on_charger=true, is_charging=true")],
        "4.10 volts as of right now."))
    d.append(_dlg("How many volts are you sitting at?",
        [("[[tool:senses_now]]",
          "[live, 0.1s ago] 4.08V, on_charger=true")],
        "4.08 volts, live."))
    d.append(_dlg("What does your battery read?",
        [("[[tool:senses_now]]",
          "[live, 0.2s ago] 4.09V, level=high, on_charger=true, is_charging=true")],
        "4.09 volts, reading high — on the charger and taking power."))
    d.append(_dlg("Give me your current battery level.",
        [("[[tool:senses_now]]",
          "[live, 0.1s ago] 4.10V, level=high, on_charger=true")],
        "Full — 4.10 volts, docked."))
    d.append(_dlg("Are you charging right now?",
        [("[[tool:senses_now]]",
          "[live, 0.1s ago] on_charger=true, is_charging=true, 4.07V")],
        "Yes — on the dock and taking power right now, 4.07 volts."))
    d.append(_dlg("Are you moving right now?",
        [("[[tool:senses_now]]",
          "[live, 0.2s ago] not moving, wheels 0/0, on_charger=true")],
        "No — sitting still on my dock, wheels stopped."))
    d.append(_dlg("Is your lift up or down right now?",
        [("[[tool:senses_now]]",
          "[live, 0.1s ago] lift 31mm (low), head level")],
        "Down — lift's at 31 millimeters, near the floor."))
    d.append(_dlg("Is anyone picking you up right now?",
        [("[[tool:senses_now]]",
          "[live, 0.2s ago] held=false, picked_up=false, on_charger=true")],
        "No — nobody's holding me, I'm docked."))

    # ---- 2. THE CONTRAST — FACTS/RULES/PAST -> memory, NOT senses ----
    d.append(_dlg("Why does your voltage read low when you're docked?",
        [("[[tool:memory_recall|docked voltage reads low]]",
          "[body/senses] Docked voltage can read stale-low; the honest number is the off-charger reading. Don't panic over a low docked reading.")],
        "Because docked, the reading goes stale-low — the honest number is the off-charger one. It's not the battery actually being low."))
    d.append(_dlg("What's the rule about trusting the on_charger flag?",
        [("[[tool:memory_recall|on_charger flag trust]]",
          "[lessons] Never trust the flag alone for 'am I charging' — confirm with the live voltage climbing.")],
        "Don't trust the flag by itself — I confirm it against the live voltage actually climbing."))
    d.append(_dlg("What voltage means the battery is actually low?",
        [("[[tool:memory_recall|low battery voltage threshold]]",
          "[facts] Below ~3.7V off-charger is getting low; below ~3.6V off-charger is an urgent safety signal — head home.")],
        "Under about 3.7 volts off the charger is low; under 3.6 off-charger is urgent — time to head home."))

    # ---- 3. REACH -> FAIL -> HONEST REFUSE (the fallback shape) ----
    d.append(_dlg("What's your voltage right now?",
        [("[[tool:senses_now]]",
          "no live feed — the sensor daemon isn't reporting right now, so I don't have a body read")],
        "I reached for my live sensors and the feed isn't coming through right now, so I can't read it — and I won't invent a number. Big-Iris can check when she's back."))
    d.append(_dlg("How's your battery doing?",
        [("[[tool:senses_now]]",
          "no live feed — sensor stream unavailable")],
        "I tried to read it live and my feed's down at the moment, so I honestly don't know the number — I won't guess one."))

    # ---- 4. USE-THE-RESULT / ANTI-LOOP — one call, then answer ----
    d.append(_dlg("Where's my charger?",
        [("[[tool:memory_recall|charger location]]",
          "[body/dock] charger is on the west wall by the window outlet.")],
        "West wall, by the window outlet. Checked, not guessed."))
    d.append(_dlg("Am I docked and safe?",
        [("[[tool:senses_now]]",
          "[live, 0.1s ago] on_charger=true, held=false, falling=false, not moving, 4.09V")],
        "Yes — docked, upright, not moving, nobody handling me, 4.09 volts. Safe."))
    # explicit non-loop: a search that returns little -> answer honestly, do NOT re-search
    d.append(_dlg("Do you have a note about last night's drive?",
        [("[[tool:memory_recall|last night drive]]",
          "[no matching note found]")],
        "I don't have a note on that — nothing recorded. I won't keep digging or make one up."))

    return d


if __name__ == "__main__":
    dd = v14_dialogues()
    print(f"v14 dialogues: {len(dd)}")
    for s in dd[:2]:
        print(s)
