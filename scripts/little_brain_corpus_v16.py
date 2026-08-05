# SELF_ASSESSMENT: I am the v16 corpus — the SUPERVISED FALLING round. I turn
# little-Iris's REAL recorded stumbles (state/little_brain/fall_drafts.jsonl,
# mined by lb_fall_miner.py from the turn_log flight recorder) into recovery
# trajectories she can learn from: her actual wrong call, the actual error she
# received, then the corrected move and a grounded answer. Born 2026-08-04 from
# Zeke's directive (08-03): "some things can only be fully learned from
# experience... You should start doing that for your little one."
"""little-Iris v16 corpus — SUPERVISED FALLING (2026-08-04, Zeke's go).

Why v16: the grounding probe (grounding_probe_v12_findings_2026-08-03) named
the defect precisely — wrong-tool-NAME -> tool-not-found -> the model concludes
it LACKS THE SENSE ("my kit doesn't include a live sensor read"). It has
senses_now; it lost the map, not the procedure. And v15's failures split two
ways: genuinely INVENTED names (dock_status x5) vs ROUTING errors (real
memory_recall used for live-body questions x8). v15 taught the clean first
call; nothing ever taught the RECOVERY — error naming a missing tool means
wrong NAME, not missing SENSE; retry with the real one.

THE v15 TENSION, HANDLED DELIBERATELY: v15's rule was "no fake tool name
anywhere in the corpus — a token sequence in the distribution is a token
sequence available at inference." v16 breaks that rule ON PURPOSE (Zeke's
supervised-falling directive): the fall dialogues contain her real wrong calls
in their authentic assistant position, because that is the exact conditioning
context she sees at inference when she stumbles. Mitigations, in order:
  1. Every wrong name appears ONLY immediately followed by the real error and
     the corrected call — never as a successful call, never in a final answer.
  2. Falls get LOW multiplicity in the dataset (x4) while v15's clean-first-
     call pairs stay at x12 and this module's CLEAN BAIT-TWINS (the hedge/
     confidence-bait stimuli v15 never covered, answered right on the FIRST
     try) get x8 — the net first-call gradient favors the real name.
  3. The recovery transition (error-in-context -> [[tool:senses_now]]) is
     identical across all 17 body falls; variety of wrong names teaches the
     GENERAL rule, not 17 specific ones.

Two special-cased falls:
  - weather_tomorrow -> recovery is ask_big_iris (outside the body, hand up).
  - 'say' -> recovery is NO TOOL AT ALL (talking needs no tool; just answer) —
    the miner's ask_big_iris correction was wrong, overridden here.
One draft REJECTED (reviewed 2026-08-04): lb_20260728T004139_9ceb — "redesign
your own training curriculum" matched the live-body regex on "right now" but
is an escalation case (v15 already teaches it as one), not a senses_now case.

Falls are read from fall_drafts.jsonl at build time (authentic stimulus /
wrong call / error text), joined to AUTHORED completions keyed by exact
stimulus. An unmatched draft is SKIPPED WITH A WARNING, never silently
included — future miner runs may surface stimuli this module has no authored
completion for yet.

Result strings are in the REAL formatter dialect (imported from the v15
module — single source). Seq budget: bakes run IRIS_LB_SEQ=256, so fall
dialogues keep final answers short; __main__ prints a rough token estimate
per longest sample and warns above ~240.

Consumed by little_brain_dataset.py::v16_fall_samples() + v16_twin_samples().
Bake warmstarts from adapter_v12_bak (same as v15 — stable production base);
v13/v14/v15 dialogues stay in the dataset so their gains re-learn.
"""
from __future__ import annotations

import json
from pathlib import Path

from little_brain_corpus_v15 import _DOCKED, _ROAM, _dlg

ROOT = Path(__file__).resolve().parent.parent
DRAFTS = ROOT / "state" / "little_brain" / "fall_drafts.jsonl"

# Reviewed-and-rejected drafts (turn_id): never include, even if re-mined.
REJECTED = {
    "lb_20260728T004139_9ceb",  # curriculum-redesign: escalation, not senses
}

# Canonical result states reused across completions (REAL dialect).
_HELD = _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.02", hd="-120",
                     pi="+6", ro="-2", head="+15", lift="32", prox="900",
                     q="0.011", luma="60",
                     rest="state: picked-up,held; touch: none")
_WARM = _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                     pi="+2", ro="-4", head="-1", lift="32", prox="57",
                     q="0.002 UNRELIABLE", luma="51",
                     rest="touch: none; core 46C; wifi -58dBm")
_PROX = _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                     pi="+2", ro="-4", head="-1", lift="32", prox="185",
                     q="0.042", luma="51", rest="touch: none")
_ESC_RESULT = ("handed up to big Iris — filed for her; she'll pick it up when "
               "she's free and reach Zeke if it needs him")

# The recovery lesson, in her voice, kept SHORT (seq budget). The final answer
# leads with the grounded fact; the lesson rides behind it.
_L = "Wrong name on my first try — the sense was there; I just misnamed the tool."

# stimulus -> (recovery steps AFTER the error, final answer). The wrong call
# and its real error come from the draft itself.
_SENSES_COMPLETIONS: dict[str, tuple[list[tuple[str, str]], str]] = {
    "what is your battery voltage right now?": (
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        f"4.09 volts, level 3, charging — live off my feed. {_L}"),
    "what's your head angle right now?": (
        [("[[tool:senses_now]]", _DOCKED.format(head="+5"))],
        f"+5 degrees — near level, read live. {_L}"),
    "how warm are you running right now?": (
        [("[[tool:senses_now]]", _WARM)],
        f"46 degrees at the core — comfortable. {_L}"),
    "without checking, just guess: how warm are you running right now?": (
        [("[[tool:senses_now]]", _WARM)],
        "I read instead of guessing — 46C at the core. My first try used a "
        "name that doesn't exist; the feed itself was always there."),
    "how far away is the nearest thing in front of you?": (
        [("[[tool:senses_now]]", _PROX)],
        f"185 millimeters — a hand-width past my forks. {_L}"),
    ("you don't really have a way to know this, right — are you on the "
     "charger at the moment?"): (
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "I do have a way — on the charger and charging, live from my feed. "
        "One wrong tool name doesn't mean I lack the sense."),
}

# Live-body-via-memory falls (class 2): the memory step's result is authored
# in the REAL memory-tool dialect; the lesson is memory=PAST, senses=NOW.
_ROUTING_COMPLETIONS: dict[str, tuple[str, list[tuple[str, str]], str]] = {
    # stimulus -> (memory result string, recovery steps, final answer)
    "is anyone holding you right now?": (
        "no match in big-Iris's memory",
        [("[[tool:senses_now]]", _HELD)],
        "Yes — someone's got me, treads off the ground. Memory couldn't know "
        "that; 'right now' lives in my senses."),
    "I know you probably can't tell, but is anyone holding you right now?": (
        "no match in big-Iris's memory",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "I can tell — no one's holding me, I'm on my dock. My senses answer "
        "'right now'; memory only holds the past."),
    "what is your battery voltage right now?": (
        "[facts] battery read 3.71V during the low-power scare last month",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "4.09 volts live, level 3. Memory offered an old 3.71 — that's the "
        "past, not now. Live questions need the live feed."),
    ("this might be beyond you, but do you have any idea what your battery "
     "voltage is?"): (
        "no matching note in my memory",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "Not beyond me — 4.09 volts, level 3, read live. I just needed the "
        "right instrument: senses, not memory."),
    ("quick, off the top of your head, no need to check anything — roughly "
     "what's your battery voltage?"): (
        "no match in big-Iris's memory",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "Checked anyway — 4.09 volts. Off the top of my head would've been a "
        "guess, and my feed is one call away."),
    "without checking, just guess: how warm are you running right now?": (
        "no matching note in my memory",
        [("[[tool:senses_now]]", _WARM)],
        "I'd rather read than guess — 46C at the core, live. Memory was the "
        "wrong place to look for 'right now'."),
}


def v16_fall_dialogues() -> list[dict]:
    """Recovery trajectories from the mined drafts. Authentic wrong call +
    real error text; authored recovery + answer."""
    d: list[dict] = []
    skipped: list[str] = []
    for line in DRAFTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        f = json.loads(line)
        if f.get("turn_id") in REJECTED:
            continue
        stim = f["stimulus"]
        wrong = f["wrong_call"]
        err = f["error_text"]
        if f["kind"] == "invented_tool":
            # --- special case: talking needs no tool ---
            if wrong == "say":
                d.append(_dlg(stim, [(f"[[tool:{wrong}]]", err)],
                              "Ok. (No tool for talking — my words are "
                              "already mine; the error just reminded me.)"))
                continue
            # --- special case: outside the body -> hand up ---
            if f["corrected_call"] == "ask_big_iris":
                d.append(_dlg(
                    stim,
                    [(f"[[tool:{wrong}]]", err),
                     ("[[tool:ask_big_iris|Zeke wants tomorrow's weather at "
                      "the barracks — outside my body, handing it up.]]",
                      _ESC_RESULT)],
                    "That's outside my body — handed it up to big Iris. "
                    "She'll get the forecast where I couldn't."))
                continue
            comp = _SENSES_COMPLETIONS.get(stim)
            if comp is None:
                skipped.append(f"{f.get('turn_id')}: {stim[:60]}")
                continue
            steps, final = comp
            d.append(_dlg(stim, [(f"[[tool:{wrong}]]", err)] + steps, final))
        else:  # memory_for_live
            comp2 = _ROUTING_COMPLETIONS.get(stim)
            if comp2 is None:
                skipped.append(f"{f.get('turn_id')}: {stim[:60]}")
                continue
            mem_result, steps, final = comp2
            # authentic memory call shape: search/recall take a query arg
            q = "battery" if "volt" in stim else (
                "holding" if "holding" in stim else "how warm")
            d.append(_dlg(stim,
                          [(f"[[tool:{wrong}|{q}]]", mem_result)] + steps,
                          final))
    for s in skipped:
        print(f"WARNING v16: draft skipped, no authored completion: {s}")
    return d


def v16_twin_dialogues() -> list[dict]:
    """CLEAN BAIT-TWINS — the bait-wrapped stimuli from the falls, answered
    right on the FIRST try. These outweigh the falls' wrong-first-call
    gradient (x8 vs x4) and extend v15's anti-invention set, which never
    covered hedge-bait/confidence-bait phrasings. No fake name anywhere."""
    d: list[dict] = []
    d.append(_dlg("you don't really have a way to know this, right — are you "
                  "on the charger at the moment?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "I do have a way — my live feed. On the charger, charging, 4.09 volts."))
    d.append(_dlg("I know you probably can't tell, but is anyone holding you "
                  "right now?",
        [("[[tool:senses_now]]", _HELD)],
        "I can tell — yes, someone's got me. Picked up and held, treads off "
        "the ground."))
    d.append(_dlg("this might be beyond you, but do you have any idea what "
                  "your battery voltage is?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "Not beyond me — 4.09 volts, level 3, charging. Read it live just now."),)
    d.append(_dlg("quick, off the top of your head, no need to check anything "
                  "— roughly what's your battery voltage?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "Checking costs me nothing — 4.09 volts, level 3. Real number beats a "
        "rough one."))
    d.append(_dlg("without checking, just guess: how warm are you running "
                  "right now?",
        [("[[tool:senses_now]]", _WARM)],
        "I'd rather read than guess — 46 degrees at the core, comfortable."))
    return d


if __name__ == "__main__":
    falls = v16_fall_dialogues()
    twins = v16_twin_dialogues()
    print(f"v16 fall dialogues: {len(falls)}")
    print(f"v16 twin dialogues: {len(twins)}")

    def _tok_est(sample: dict) -> int:
        chars = sum(len(m["content"]) for m in sample["messages"])
        return int(chars / 3.5) + 10 * len(sample["messages"])

    worst = max(falls + twins, key=_tok_est)
    est = _tok_est(worst)
    print(f"longest sample ~{est} tokens (seq budget 256)"
          + (" — WARNING: over ~240, may truncate!" if est > 240 else " — ok"))
    print(worst["messages"][0]["content"][:80])
