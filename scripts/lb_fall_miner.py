# SELF_ASSESSMENT: I am the fall miner — I harvest little-Iris's REAL recorded
# stumbles from the flight recorder (state/little_brain/turn_log/) and draft
# recovery trajectories for the v16 corpus: the fall (her actual wrong call),
# the error she actually got back, and the corrected next move. Born 2026-08-03
# from Zeke's directive ("supervised falling — start doing that for your little
# one"): some things are only fully learned from experience; telling is just
# preparation. Train on the stumble-and-recover, not only the clean answer.
"""
lb_fall_miner — mine error->recovery training drafts from real turn logs.

Two fall classes mined:
  invented_tool : tool call answered "no tool named 'X'" -> corrected to the
                  nearest real tool (semantic map first, difflib fallback)
  memory_for_live : live-body question ("right now"-shaped) answered via
                  memory_* with senses_now never reached -> corrected to
                  senses_now (flagged heuristic=True for human/big-Iris review)

Output: state/little_brain/fall_drafts.jsonl — one draft per fall, carrying the
REAL stimulus, REAL wrong call, REAL error text, corrected call, and provenance
(turn_id, model, date). Corpus formatting into [[tool:]]/[[result:]] dialogue
turns happens at v16 corpus build, not here.

Usage: .venv/Scripts/python.exe scripts/lb_fall_miner.py
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "state" / "little_brain" / "turn_log"
OUT = ROOT / "state" / "little_brain" / "fall_drafts.jsonl"

REAL_TOOLS = ["senses_now", "memory_search", "memory_note", "memory_recall",
              "memory_edit", "time_now", "ask_big_iris"]

# invented-name -> intended real tool, by meaning (checked before difflib,
# which would map sensors_now->senses_now correctly but dock_status->nothing)
SEMANTIC = {
    "sensors": "senses_now", "sensors_now": "senses_now", "sense_now": "senses_now",
    "dock_status": "senses_now", "battery_status": "senses_now",
    "battery": "senses_now", "charger_status": "senses_now",
    "clock": "time_now", "get_time": "time_now",
    "memory_lookup": "memory_recall", "recall": "memory_recall",
    "escalate": "ask_big_iris", "ask_iris": "ask_big_iris",
}

LIVE_BODY_RE = re.compile(
    r"right now|at the moment|currently|are you (charging|moving|held|tilted)|"
    r"battery|voltage|head angle|how warm|holding you|on the charger|in front of you",
    re.I)


_BODY_WORDS = ("sens", "body", "battery", "volt", "charg", "dock", "temp",
               "prox", "head", "gyro", "touch", "motor", "wheel", "lift")


def correct_name(bad: str) -> str:
    if bad in SEMANTIC:
        return SEMANTIC[bad]
    low = bad.lower()
    if any(w in low for w in _BODY_WORDS):
        return "senses_now"
    if "time" in low or "clock" in low:
        return "time_now"
    if "memor" in low or "recall" in low or "note" in low:
        return "memory_recall"
    close = difflib.get_close_matches(bad, REAL_TOOLS, n=1, cutoff=0.6)
    return close[0] if close else "ask_big_iris"


def main() -> None:
    drafts, seen = [], set()
    n_turns = 0
    for p in sorted(LOG_DIR.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                t = json.loads(line)
            except Exception:
                continue
            n_turns += 1
            stim = str(t.get("stimulus", {}).get("clean") or "")
            tools = t.get("tools", []) or []
            names = [str(x.get("name", "")) for x in tools]
            prov = {"turn_id": t.get("turn_id"), "model": t.get("model"),
                    "src": p.name}
            # class 1: invented tool names (the error she actually received)
            for x in tools:
                res = str(x.get("result", ""))
                if "no tool named" in res.lower():
                    bad = str(x.get("name", ""))
                    key = (stim, bad)
                    if key in seen:
                        continue
                    seen.add(key)
                    drafts.append({
                        "kind": "invented_tool", "heuristic": False,
                        "stimulus": stim, "wrong_call": bad,
                        "error_text": res, "corrected_call": correct_name(bad),
                        "lesson": (f"'{bad}' is not one of my tools; for this "
                                   f"question the real one is "
                                   f"'{correct_name(bad)}'. An error naming a "
                                   f"missing tool means I used the wrong NAME, "
                                   f"not that I lack the sense."),
                        **prov})
            # class 2: live-body question served from memory, senses never reached
            if (stim and LIVE_BODY_RE.search(stim)
                    and any(n.startswith("memory_") for n in names)
                    and "senses_now" not in names):
                key = (stim, "memory_for_live")
                if key not in seen:
                    seen.add(key)
                    drafts.append({
                        "kind": "memory_for_live", "heuristic": True,
                        "stimulus": stim,
                        "wrong_call": [n for n in names
                                       if n.startswith("memory_")][0],
                        "error_text": "(no error - memory answered, but the "
                                      "question was about the live body)",
                        "corrected_call": "senses_now",
                        "lesson": ("Memory holds the PAST; 'right now' "
                                   "questions need senses_now first."),
                        **prov})
    OUT.write_text("\n".join(json.dumps(d, ensure_ascii=False)
                             for d in drafts) + ("\n" if drafts else ""),
                   encoding="utf-8")
    by_kind: dict[str, int] = {}
    for d in drafts:
        by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
    print(f"scanned {n_turns} turns -> {len(drafts)} unique fall drafts "
          f"{by_kind}\nsaved: {OUT}")


if __name__ == "__main__":
    main()
