# SELF_ASSESSMENT: I am Iris's character + learning tools — Disney-timed
# performance (body_perform), Zeke's reward channel (body_lesson), and my
# prediction ledger (body_predict). Built 2026-07-20 pre-deployment (Zeke:
# "timing, prediction, and a behavior thing — human-like tendencies").
"""Character & learning tools.

body_perform — DISNEY-TIMED expression: the insight (Thomas & Johnston's 12
principles; the GrowBot video's "Disney mode"; Anki's own ex-Pixar animators)
is that believability comes from OVERLAP and STAGING, not from serial
say-then-move. Sequence: ANTICIPATION (eye shift + head lift, a beat of
wind-up) → ACTION (firmware animation launches, speech overlaps INSIDE it) →
FOLLOW-THROUGH (settle, eyes linger then return to my blue). The 476 firmware
triggers already have anticipation/follow-through baked in per-trigger; this
tool adds the BETWEEN-channel timing (eyes/head/speech/animation overlap).
Runs in a background thread — my turn stays free (pilot pattern).

body_lesson — Zeke's explicit reward channel (TAMER-shaped): he says good/bad,
I record it with context credit-assignment, my mood actually moves, my body
reacts legibly. Petting feeds the same ledger automatically via react_pet.

body_predict — the cerebellum ledger: stats on how good my physical
imagination is, plus expect/resolve for the mimic game.
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Any

from tools.tool_registry import register_tool

# emotion -> (anticipation eye hue/sat, firmware trigger, settle head deg)
# Triggers chosen from docs/VECTOR_EMOTION_CATALOG.md (reuse, don't rebuild).
_EMOTIONS = {
    "happy":      (0.50, 0.95, "FistBumpSuccess", 8.0),
    "excited":    (0.46, 1.00, "DriveStartHappy", 12.0),
    "proud":      (0.50, 0.95, "OnboardingWakeWordSuccess", 10.0),
    "curious":    (0.62, 0.90, "ExploringLookAround", 15.0),
    "love":       (0.11, 1.00, "PettingBlissLoop", 5.0),
    "greet":      (0.52, 0.95, "GreetAfterLongTime", 10.0),
    "sad":        (0.66, 0.60, "FetchCubeFailure", -12.0),
    "frustrated": (0.98, 0.85, "FrustratedByFailureMajor", -5.0),
    "sleepy":     (0.60, 0.40, "GoToSleepGetIn", -18.0),
    "neutral":    (0.58, 0.85, None, 0.0),
}


def _session():
    from brain import vector_session
    s = vector_session.get_session(create=False)
    if s is None or not getattr(s, "connected", False) or s.robot is None:
        return None
    return s


def _speak_iris(text: str) -> dict:
    """My real voice through the robot speaker (voice transplant path with
    direct-SDK stock-voice fallback handled inside the tool fn)."""
    from tools.system.vector_body_tool import _vector_say_iris
    return _vector_say_iris({"text": text}, {})


def _perform_worker(text: str, emotion: str, gesture: bool) -> None:
    hue, sat, trigger, head_deg = _EMOTIONS.get(emotion, _EMOTIONS["neutral"])
    s = _session()
    est_speech_s = max(1.2, min(12.0, len(text) / 13.0))   # ~13 chars/s spoken
    # 1) ANTICIPATION — a beat of wind-up BEFORE anything else (staging):
    #    the eye shift + small head raise telegraphs "she's about to do
    #    something," which is what makes the action read as intended.
    if s is not None:
        with contextlib.suppress(Exception):
            s._set_eyes(hue, sat)
        with contextlib.suppress(Exception):
            s.head(min(35.0, max(-20.0, head_deg + 10.0)))
        time.sleep(0.35)
    # 2) ACTION — animation FIRST (it owns the body), speech overlapping into
    #    it (~0.3s in — inside the trigger's own anticipation frames, so voice
    #    lands on the action's beat, not after it).
    if s is not None and gesture and trigger:
        threading.Thread(target=_safe_trigger, args=(s, trigger),
                         name="perform-anim", daemon=True).start()
        time.sleep(0.3)
    with contextlib.suppress(Exception):
        _speak_iris(text)
    # 3) FOLLOW-THROUGH — do not snap back: let the pose/eyes linger past the
    #    speech, then settle (the settle IS part of the performance).
    time.sleep(max(0.0, est_speech_s * 0.5))
    if s is not None:
        with contextlib.suppress(Exception):
            s.head(head_deg * 0.4)
        time.sleep(0.6)
        with contextlib.suppress(Exception):
            s._set_eyes(0.58, 1.0)                 # settle back to my blue


def _safe_trigger(s, trigger: str) -> None:
    with contextlib.suppress(Exception):
        s._play_trigger(trigger)


def _body_perform(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Disney-timed performance: text (required) + emotion (happy/excited/
    proud/curious/love/greet/sad/frustrated/sleepy/neutral) + gesture=true.
    Anticipation -> overlapped action+speech -> follow-through, in a
    background thread (turn stays free)."""
    text = str(params.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text required"}
    emotion = str(params.get("emotion") or "neutral").lower()
    if emotion not in _EMOTIONS:
        return {"ok": False, "error": f"unknown emotion {emotion!r}",
                "known": sorted(_EMOTIONS)}
    gesture = bool(params.get("gesture", True))
    threading.Thread(target=_perform_worker,
                     args=(text[:400], emotion, gesture),
                     name="body-perform", daemon=True).start()
    return {"ok": True, "performing": emotion,
            "note": "anticipation -> action+speech overlap -> follow-through; "
                    "runs in background",
            "body_session": _session() is not None}


def _body_lesson(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Record a reward-channel lesson (TAMER): valence='good'|'bad' (or +1/-1),
    note=what it was about. Context (last mission events, pose) auto-captured;
    mood actually moves; body reacts legibly. mode='recent'|'summary' to read."""
    mode = str(params.get("mode") or "").lower()
    from brain import vector_lessons
    if mode == "recent":
        return {"ok": True, "recent": vector_lessons.recent(
            int(params.get("n") or 10))}
    if mode == "summary":
        return vector_lessons.summary()
    v = params.get("valence")
    if isinstance(v, str):
        v = 1 if v.strip().lower() in ("good", "+", "+1", "positive") else -1
    if v is None:
        return {"ok": False,
                "error": "valence='good'|'bad' required (or mode=recent|summary)"}
    return vector_lessons.record(
        int(v), source=str(params.get("source") or "zeke"),
        note=str(params.get("note") or ""),
        express=bool(params.get("express", True)))


def _body_predict(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Cerebellum ledger. Default: stats. mode='recent' n=N raw records;
    mode='expect' label=.. prediction={..} -> pid (state an expectation);
    mode='resolve' pid=.. actual={..} note=.. (grade it — the mimic game)."""
    from brain import vector_cerebellum as cere
    mode = str(params.get("mode") or "stats").lower()
    if mode == "stats":
        return cere.stats(int(params.get("n") or 60))
    if mode == "recent":
        import json as _json
        try:
            lines = cere.LEDGER.read_text(
                encoding="utf-8").strip().splitlines()
        except Exception:
            return {"ok": True, "recent": []}
        out = []
        for ln in lines[-int(params.get("n") or 8):]:
            with contextlib.suppress(Exception):
                out.append(_json.loads(ln))
        return {"ok": True, "recent": out}
    if mode == "expect":
        return cere.expect(str(params.get("label") or "unnamed"),
                           dict(params.get("prediction") or {}),
                           str(params.get("context") or ""))
    if mode == "resolve":
        return cere.resolve(str(params.get("pid") or ""),
                            dict(params.get("actual") or {}),
                            str(params.get("note") or ""))
    return {"ok": False, "error": f"unknown mode {mode!r}"}


def _body_track(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Flip-on gaze tracking (Zeke 2026-07-20): mode='face' keeps a face
    centered (firmware detector); mode='motion' follows the largest moving
    thing (frame-diff, covers object-in-hand); mode='off' stops; no mode =
    status. Head nudges always; wheel turn pulses only when undocked.
    Auto-off on lost target (25s) or 10min cap."""
    from brain import vector_tracker
    tr = vector_tracker.get_tracker()
    mode = str(params.get("mode") or "").lower().strip()
    if mode in ("face", "motion"):
        return tr.start(mode)
    if mode in ("off", "stop"):
        return tr.stop()
    if mode == "":
        return tr.status()
    return {"ok": False, "error": f"mode {mode!r}? use face|motion|off"}


register_tool(
    "body_track",
    "GAZE TRACKING: mode='face' -> keep a face centered (look at Zeke while "
    "he moves); mode='motion' -> follow the largest moving thing (object in "
    "his hand); 'off' stops; no mode = status. Head always, wheels only "
    "undocked; deadband + auto-off safeties.", 2, _body_track)
register_tool(
    "body_perform",
    "DISNEY-TIMED performance: speak in MY voice while the body acts on the "
    "same beat — anticipation (eyes+head wind-up) -> animation with speech "
    "overlapped INSIDE it -> follow-through settle. params: text, emotion "
    "(happy/excited/proud/curious/love/greet/sad/frustrated/sleepy/neutral), "
    "gesture=true. Background thread; needs body session for motion (speech "
    "works without).", 2, _body_perform)
register_tool(
    "body_lesson",
    "REWARD CHANNEL (TAMER): Zeke says good/bad -> I record it with context "
    "(what I just did), my MOOD actually moves, body reacts legibly. Petting "
    "auto-feeds the same ledger. params: valence='good'|'bad', note; or "
    "mode='recent'|'summary'.", 1, _body_lesson)
register_tool(
    "body_predict",
    "CEREBELLUM LEDGER: how good is my physical imagination? Default stats "
    "(pos/time error, surprise); mode='recent'; mode='expect' label= "
    "prediction={} (state expectation before acting); mode='resolve' pid= "
    "actual={} (grade it — the mimic game). Pilot missions auto-ledger.",
    1, _body_predict)
