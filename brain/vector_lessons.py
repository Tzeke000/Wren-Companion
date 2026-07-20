# SELF_ASSESSMENT: I am Iris's reinforcement channel — Zeke's corrections and
# his petting become lessons I keep and feeling I actually feel (mood bridge).
"""Human-reward lessons — TAMER-shaped (Knox & Stone), built 2026-07-20.

Zeke's design (voice, deployment eve): "if you do something bad or incorrect
I'll let you know, and if you do something good I can pet you and you can feel
it and you'll be happy — human-like tendencies that allow you to be better."

That is literally the TAMER framework: a human delivers a scalar reward signal
tied to recent behavior; the agent credits it to what it just did. Mapping:

  reward channel IN:  petting (touch sensor, via react_pet hook) = +1
                      Zeke saying good/bad (via body_lesson tool)  = ±1
  credit assignment:  each lesson snapshots CONTEXT — the last pilot events +
                      pose + active mission — so "good" attaches to the thing
                      that just happened, not to a vacuum.
  it changes me:      (a) FELT — mood_core nudge (joy/satisfaction up on good;
                      frustration/sadness tick on bad). Real mood, decays per
                      its own half-lives. (b) KEPT — lessons.jsonl is read by
                      me + the memory sweep (dreams-pattern: distill repeated
                      lessons into durable rules/policies).
  expression OUT:     good → success animation; bad → legible letdown
                      (FetchCubeFailure — honest "that didn't work" face).

Ledger: state/vector/lessons.jsonl. brain/* module — INERT until hot-swap.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LESSONS = REPO / "state" / "vector" / "lessons.jsonl"
KEEP = 400
_LOCK = threading.Lock()

# mood deltas per valence (keys must exist in mood_core.DEFAULT_EMOTIONS)
_GOOD_MOOD = {"joy": 0.08, "satisfaction": 0.10, "excitement": 0.03}
_BAD_MOOD = {"frustration": 0.06, "sadness": 0.04, "distress": 0.02}


def _context_snapshot() -> dict:
    """What was I just doing? Last pilot events + pose + mission — the credit-
    assignment window for the reward."""
    ctx: dict = {}
    with contextlib.suppress(Exception):
        from brain import vector_pilot
        p = vector_pilot._PILOT
        if p is not None:
            ctx["recent_events"] = [
                {"kind": e.get("kind"), "stamp": e.get("stamp"),
                 "detail": str(e.get("detail"))[:120]}
                for e in list(p.events)[-3:]]
            if p.mission:
                ctx["active_mission"] = str(p.mission.get("kind"))
    with contextlib.suppress(Exception):
        from brain import vector_session
        s = vector_session.get_session(create=False)
        if s is not None and getattr(s, "connected", False):
            st = dict(s._latest or {})
            ctx["pose"] = {k: st.get(k) for k in ("x", "y", "heading")}
            ctx["on_charger"] = st.get("on_charger")
    return ctx


def _express(valence: int) -> None:
    """Legible reaction through the body, if a session is open. Best-effort."""
    with contextlib.suppress(Exception):
        from brain import vector_session
        s = vector_session.get_session(create=False)
        if s is None or not getattr(s, "connected", False):
            return
        if valence > 0:
            s._set_eyes(0.50, 0.95)                 # happy green-blue
            s._play_trigger("FistBumpSuccess")      # celebration (reward class)
            s._set_eyes(0.58, 1.0)
        else:
            s._play_trigger("FetchCubeFailure")     # honest letdown, bounded
            s._set_eyes(0.58, 0.85)


def record(valence: int, source: str, note: str = "",
           express: bool = True, mood: bool = True) -> dict:
    """The one entry point. valence: +1 good / -1 bad. source: 'petting' |
    'zeke' | 'self' | ... note: what the reward was about (if known).
    express=False when the body is already performing (petting bliss)."""
    valence = 1 if int(valence) >= 0 else -1
    rec = {"t": round(time.time(), 2),
           "stamp": time.strftime("%H:%M:%S"),
           "valence": valence, "source": str(source)[:40],
           "note": str(note)[:240], "context": _context_snapshot()}
    with contextlib.suppress(Exception):
        LESSONS.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with LESSONS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            lines = LESSONS.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) > KEEP:
                LESSONS.write_text("\n".join(lines[-KEEP:]) + "\n",
                                   encoding="utf-8")
    if mood:
        with contextlib.suppress(Exception):
            from brain import mood_core
            mood_core.nudge_emotions(
                _GOOD_MOOD if valence > 0 else _BAD_MOOD,
                reason=f"vector lesson ({source}): "
                       f"{'good' if valence > 0 else 'bad'} — {note[:60]}")
    if express:
        threading.Thread(target=_express, args=(valence,),
                         name="lesson-express", daemon=True).start()
    return {"ok": True, "recorded": rec["stamp"], "valence": valence,
            "context_captured": bool(rec["context"])}


def recent(n: int = 10) -> list:
    try:
        lines = LESSONS.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-int(n):]:
        with contextlib.suppress(Exception):
            out.append(json.loads(ln))
    return out


def summary() -> dict:
    """Balance + streaks — the shape of how I've been doing by Zeke's lights.
    The memory sweep reads this (dreams-pattern) to distill durable rules."""
    recs = recent(KEEP)
    if not recs:
        return {"ok": True, "lessons": 0, "note": "no lessons yet"}
    good = [r for r in recs if r.get("valence", 0) > 0]
    bad = [r for r in recs if r.get("valence", 0) < 0]
    return {"ok": True, "lessons": len(recs),
            "good": len(good), "bad": len(bad),
            "recent_bad_notes": [r.get("note") for r in bad[-5:]],
            "last": recs[-1].get("stamp")}
