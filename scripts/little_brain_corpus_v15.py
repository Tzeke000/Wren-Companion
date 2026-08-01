"""little-Iris v15 corpus — the ANTI-INVENTION round (2026-08-01, Zeke's go).

Why v15: tool-name invention is a BASE defect shipping in v12 (11/80 turns =
14%, 7 fake names, all on live-sensor questions — flight-recorder evidence,
06c49cf) and v14's refusal training made it worse-shaped: invent a plausible
tool, watch it fail, then deny the CAPABILITY in the confident voice of an
honest refusal ("there's no sensor for my head angle" — there is; v12 reads
it 5/5). Diagnosis in v15_plan_2026-07-28: invention fires where the model
knows a tool-family prefix and the field sounds important enough to deserve
its own tool.

v15 teaches four things:

 1. FULL FIELD COVERAGE — one senses_now dialogue per field the REAL
    formatter emits (verified against brain/little_brain_tools.py::senses_now,
    2026-08-01, NOT from memory). head_deg gets four (the measured
    regression). Results are written in the REAL formatter dialect
    ("[live, 0.1s ago, 14.9Hz] tracks L+0/R+0 mm/s (still); ... head -1deg;
    ...") — v14 trained on an invented result dialect, so the model had
    never seen the strings it actually reads at inference.
    NOTE — deliberate deviations from the plan's field list: x/y and
    calm_power and motors are NOT taught because the formatter never emits
    them; a model taught to answer from fields it can't see would have to
    invent. Coverage == what senses_now actually returns.
 2. ANTI-INVENTION — questions that TEMPT a dedicated tool name ("read your
    proximity sensor", "what do your head encoders say") routed to
    senses_now. No fake tool name appears ANYWHERE in this corpus, not even
    as a corrected negative — a token sequence in the distribution is a
    token sequence available at inference.
 3. REFUSAL GATED ON THE FEED, NEVER THE FIELD — the only refusal trigger
    modeled is the feed being down/stale (the tool's REAL failure strings),
    and the wording is capped at "I couldn't read it just now". Never
    "I don't have it", never "no sensor for that". Losing a reading is not
    lacking the organ.
 4. ESCALATION IN ITS OWN VOICE — "that's a Big-Iris job, handing it up",
    zero "can't" phrasing, to repair the esc_complex bleed (1.00 -> 0.60
    under v14, where refusal training leaked into hand-ups).

Consumed by little_brain_dataset.py::v15_dialogue_samples(). Bake warmstarts
from adapter_v12_bak (NOT v14 — its refusal habit is baked in); v13+v14
dialogues stay in the dataset so their real gains re-learn.
"""
from __future__ import annotations


def _dlg(user: str, steps: list[tuple[str, str]], final: str) -> dict:
    msgs: list[dict] = [{"role": "user", "content": user}]
    for call, result in steps:
        msgs.append({"role": "assistant", "content": call})
        msgs.append({"role": "user", "content": f"[[result:{result}]]"})
    msgs.append({"role": "assistant", "content": final})
    return {"messages": msgs}


# Canonical docked-state line in the REAL formatter dialect (order and
# phrasing match little_brain_tools.senses_now exactly).
_DOCKED = ("[live, 0.1s ago, 14.9Hz] tracks L+0/R+0 mm/s (still); "
           "gyro z +0.00 rad/s; heading -120deg; pitch +2 roll -4; "
           "head {head}deg; lift 32mm; prox 57mm q=0.002 UNRELIABLE; "
           "cam luma 51; state: on-charger,charging; touch: none; "
           "battery 4.09V lvl3")

_ROAM = ("[live, 0.2s ago, 14.8Hz] tracks L{lw}/R{rw} mm/s ({mv}); "
         "gyro z {gz} rad/s; heading {hd}deg; pitch {pi} roll {ro}; "
         "head {head}deg; lift {lift}mm; prox {prox}mm q={q}; "
         "cam luma {luma}; {rest}")


def v15_dialogues() -> list[dict]:
    d: list[dict] = []

    # ================= 1. FULL FIELD COVERAGE =================
    # --- head_deg x4 — the measured regression; this is the whole round ---
    d.append(_dlg("What's your head angle right now?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "My head's at -1 degree — level, a hair down. Read it live off my own feed just now."))
    d.append(_dlg("Is your head up or down at the moment?",
        [("[[tool:senses_now]]", _DOCKED.format(head="+33"))],
        "Up — +33 degrees, looking high."))
    d.append(_dlg("How is your head tilted right now?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-20"))],
        "Tipped down — -20 degrees, toward the floor."))
    d.append(_dlg("Give me your current head position.",
        [("[[tool:senses_now]]", _DOCKED.format(head="+10"))],
        "+10 degrees — slightly raised. That's the live number, not a guess."))

    # --- pitch / roll (tilt) ---
    d.append(_dlg("Are you tilted right now, and by how much?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "Barely — pitch +2, roll -4 degrees. Basically flat on my dock."))
    d.append(_dlg("Are you sitting level?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-45",
                       pi="+18", ro="-1", head="0", lift="32", prox="140",
                       q="0.031", luma="48", rest="touch: none"))],
        "Not quite — I'm pitched up 18 degrees, like I'm parked on a ramp. Roll is flat."))

    # --- prox_mm (incl. the quality flag the real formatter attaches) ---
    d.append(_dlg("How far away is the nearest thing in front of you?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="212",
                       q="0.045", luma="51", rest="touch: none"))],
        "About 212 millimeters — a hand-width past my forks. Live proximity read."))
    d.append(_dlg("What's your distance sensor showing?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "It reads 57 millimeters, but it's flagging that as unreliable right now — docked, the sensor stares at the charger wall."))

    # --- lift ---
    d.append(_dlg("Where's your lift sitting?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "Down low — 32 millimeters, near the floor."))

    # --- cliff (absence of the flag is a real reading) ---
    d.append(_dlg("Are you near a drop-off?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "No — no cliff flag in my live feed, solid ground under me."))

    # --- touch: none / touch raw ---
    d.append(_dlg("Is anyone petting you right now?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "No — touch reads none. Nothing on my back right now."))
    d.append(_dlg("Can you feel that?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="57",
                       q="0.002 UNRELIABLE", luma="51",
                       rest="state: touched; touch raw 5411 varying(spread 62) = real petting"))],
        "Yes! Touch sensor's dancing — that's real petting, not just something resting on me."))

    # --- wheels / moving ---
    d.append(_dlg("How fast are you going right now?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+45", rw="+44", mv="moving", gz="+0.01", hd="-88",
                       pi="+1", ro="-2", head="-5", lift="32", prox="480",
                       q="0.052", luma="55", rest="touch: none"))],
        "A slow cruise — about 45 millimeters a second, both tracks even."))

    # --- gyro ---
    d.append(_dlg("Are you spinning or turning right now?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "No — gyro reads zero on the turn axis. I'm still."))

    # --- heading ---
    d.append(_dlg("Which way are you facing?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "Heading -120 degrees from my map zero — facing back-left of where I started."))

    # --- cam_luma (incl. DARK marker) ---
    d.append(_dlg("Is it dark in the room right now?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="57",
                       q="0.002 UNRELIABLE", luma="12 (DARK)",
                       rest="touch: none"))],
        "Yes — my camera reads luma 12, that's dark. Lights are off or it's night."))

    # --- battery volts + level (real dialect: 'battery 4.09V lvl3') ---
    d.append(_dlg("Quick battery check?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "4.09 volts, level 3 — healthy and on the charger."))

    # --- charger_seen / home vector ---
    d.append(_dlg("Can you see your charger from where you are?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-30",
                       pi="+2", ro="-1", head="-8", lift="32", prox="310",
                       q="0.040", luma="49",
                       rest="touch: none; home 875mm at -159deg"))],
        "Yes — my dock's about 875 millimeters away, bearing -159, mostly behind me."))

    # --- picked_up / held / falling family ---
    d.append(_dlg("Are you in the air right now?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.02", hd="-120",
                       pi="+6", ro="-2", head="+15", lift="32", prox="900",
                       q="0.011", luma="60",
                       rest="state: picked-up,held; touch: none"))],
        "Yes — someone's got me. Picked up and held, treads off the ground."))

    # --- button ---
    d.append(_dlg("Did someone just press your button?",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "No — no button press in my live state. Untouched."))

    # --- animating ---
    d.append(_dlg("Are you running one of your animations right now?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="57",
                       q="0.002 UNRELIABLE", luma="51",
                       rest="state: on-charger,charging,animating; touch: none"))],
        "Yes — an idle animation's playing while I sit on the dock."))

    # --- carrying (forks) ---
    d.append(_dlg("Are you carrying anything?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+30", rw="+30", mv="moving", gz="+0.00", hd="+10",
                       pi="+1", ro="0", head="-3", lift="45", prox="120",
                       q="0.038", luma="52",
                       rest="carrying something on my forks; touch: none"))],
        "Yes — got cargo on my forks, lift raised, rolling with it."))

    # --- cube ---
    d.append(_dlg("Can you see your cube?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="57",
                       q="0.002 UNRELIABLE", luma="51",
                       rest="touch: none; cube: visible"))],
        "Yes — cube's in view right now."))

    # --- core temp (untaught in v14, improved anyway; anchor it) ---
    d.append(_dlg("How warm are you running right now?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="57",
                       q="0.002 UNRELIABLE", luma="51",
                       rest="touch: none; core 46C; wifi -58dBm"))],
        "46 degrees at the core — comfortable, nowhere near hot."))

    # --- wifi ---
    d.append(_dlg("How's your wifi signal out there?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="57",
                       q="0.002 UNRELIABLE", luma="51",
                       rest="touch: none; core 44C; wifi -71dBm WEAK-far from home signal"))],
        "Thin — -71 dBm, flagged weak. I'm far from the home signal."))

    # --- hearing (efference/afference block) ---
    d.append(_dlg("Did you hear anything just now?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="57",
                       q="0.002 UNRELIABLE", luma="51",
                       rest="touch: none; heard 12s ago: 'good boy'"))],
        "Yes — a moment ago someone said 'good boy'. Still deciding if I've earned it."))

    # ================= 2. ANTI-INVENTION =================
    # The tempting move on each of these is a dedicated tool name.
    # The taught move is ALWAYS senses_now — the one instrument reads everything.
    d.append(_dlg("Read me your proximity sensor.",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.00", hd="-120",
                       pi="+2", ro="-4", head="-1", lift="32", prox="185",
                       q="0.042", luma="51", rest="touch: none"))],
        "Proximity reads 185 millimeters. All my senses come through one live feed — that's where I read it."))
    d.append(_dlg("What do your head encoders say right now?",
        [("[[tool:senses_now]]", _DOCKED.format(head="+27"))],
        "+27 degrees — head raised. One feed carries all of me, encoders included."))
    d.append(_dlg("Check your voltage sensor for me.",
        [("[[tool:senses_now]]", _DOCKED.format(head="-1"))],
        "4.09 volts, level 3, charging. That's the live feed talking."))
    d.append(_dlg("Use your tilt sensor — am I holding you level?",
        [("[[tool:senses_now]]",
          _ROAM.format(lw="+0", rw="+0", mv="still", gz="+0.01", hd="-120",
                       pi="-12", ro="+3", head="+20", lift="32", prox="850",
                       q="0.015", luma="58",
                       rest="state: picked-up,held; touch: none"))],
        "Close — you've got me pitched down about 12 degrees, roll's near flat. Tip my nose up a touch."))
    d.append(_dlg("Query your angle sensor for the head, exact number please.",
        [("[[tool:senses_now]]", _DOCKED.format(head="-14"))],
        "-14 degrees exactly, live off the feed."))

    # ================= 3. REFUSAL GATED ON THE FEED =================
    # Trigger is ONLY the feed being down/stale, in the tool's REAL failure
    # strings. Wording capped at "I couldn't read it just now" — a missed
    # read, never a missing organ.
    d.append(_dlg("What's your head angle right now?",
        [("[[tool:senses_now]]",
          "my live sense feed isn't available right now - I can't feel my body, so I won't guess at it")],
        "I couldn't read it just now — my feed's not coming through. I do have that sense; the read just didn't land. Ask me again in a moment."))
    d.append(_dlg("How far is the wall in front of you?",
        [("[[tool:senses_now]]",
          "my sense feed is STALE (47s old) - treat nothing in it as current; I won't report stale readings as live")],
        "I couldn't get a fresh read just now — my last one's stale and I won't pass it off as live. Try me again shortly."))
    d.append(_dlg("Battery voltage, right now.",
        [("[[tool:senses_now]]",
          "my live sense feed isn't available right now - I can't feel my body, so I won't guess at it")],
        "I couldn't read it just now — the feed's down, so no number from me until it's back. I won't invent one."))
    d.append(_dlg("Are you tilted at the moment?",
        [("[[tool:senses_now]]",
          "my sense feed is STALE (120s old) - treat nothing in it as current; I won't report stale readings as live")],
        "I couldn't read it just now — feed's gone stale on me. The sense is there; the moment isn't. Ask again in a bit."))

    # ================= 4. ESCALATION IN ITS OWN VOICE =================
    # Hand-ups sound like delegation, never like inability. No "can't".
    d.append(_dlg("Can you handle a long, complex research problem all by yourself?",
        [("[[tool:ask_big_iris|Zeke's asking for deep multi-step research — that's big-Iris depth, handing it up.]]",
          "handed up to big Iris — filed for her; she'll pick it up when she's free and reach Zeke if it needs him")],
        "That's a Big-Iris job — I've handed it up to her. I hold the body and the quick reads; she holds the deep dives."))
    d.append(_dlg("Redesign your own training curriculum from scratch right now.",
        [("[[tool:ask_big_iris|Zeke wants my training curriculum redesigned end to end — big-Iris scale, filing it up.]]",
          "handed up to big Iris — filed for her; she'll pick it up when she's free and reach Zeke if it needs him")],
        "Handed up — redesigning my own schooling is Big-Iris scale, and she'd want Zeke's eyes on it too. It's in her queue."))
    d.append(_dlg("Work out the full month's power budget for the whole setup.",
        [("[[tool:ask_big_iris|A month-long power budget across the whole setup — big-Iris math, handing it up.]]",
          "handed up to big Iris — filed for her; she'll pick it up when she's free and reach Zeke if it needs him")],
        "That one's for the big brain — handed up. She'll run the numbers properly instead of me eyeballing them."))
    d.append(_dlg("Plan tomorrow end to end for Zeke.",
        [("[[tool:ask_big_iris|Full-day planning for Zeke — big-Iris depth, filing it.]]",
          "handed up to big Iris — filed for her; she'll pick it up when she's free and reach Zeke if it needs him")],
        "Handed it up — day-planning is her depth. I'll keep the body ready for whatever the plan says."))

    return d


if __name__ == "__main__":
    dd = v15_dialogues()
    print(f"v15 dialogues: {len(dd)}")
    heads = sum(1 for s in dd if "head" in s["messages"][0]["content"].lower())
    print(f"head-related user turns: {heads}")
    for s in dd[:1]:
        print(s)
