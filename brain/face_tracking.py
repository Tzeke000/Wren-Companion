"""brain/face_tracking.py — temporal filter on face recognition.

Wraps the existing brain/insight_face_engine + brain/face_recognizer per-frame
match results with a persistence window. Promotes "transient unknown face"
to "new person detected" only after `unknown_persistence_seconds` of
continuous unknown-face visibility.

The goal: filter out brief look-aways, recognition jitter, lighting changes,
and no-person states (shadows / reflections) — so Ava only flags a "new
person" when one is actually there.

When a new person is detected:
- Inner-monologue note is written ("there's an unknown person here").
- Person is implicitly tracked at Trust Level 1 (stranger band).
- No auto-introduction; Ava stays reserved unless engaged.
- Onboarding flow is triggered ONLY by an explicit Zeke command.

See docs/AVA_FEATURE_ADDITIONS_2026-05.md §4 for the framework.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


_TRACK_LOCK = threading.Lock()

# Config (loaded once)
_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "onboarding.json"
_CFG: dict[str, Any] | None = None


def _cfg(*keys: str, default: Any = None) -> Any:
    global _CFG
    if _CFG is None:
        try:
            _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            _CFG = {}
    cur: Any = _CFG
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _state(g: dict[str, Any]) -> dict[str, Any]:
    """Per-process state stored on g. Lazy init."""
    s = g.get("_face_tracking_state")
    if not isinstance(s, dict):
        s = {
            "current_person_id": None,        # who's been continuously visible
            "first_seen_ts": 0.0,
            "last_seen_ts": 0.0,
            "consecutive_frames": 0,
            "candidate_unknown": False,
            "candidate_unknown_since_ts": 0.0,
            "last_known_seen_ts": 0.0,
            "last_promotion_ts": 0.0,
            "promoted_unknown_id": None,      # the temp id assigned on promotion
        }
        g["_face_tracking_state"] = s
    return s


def update(g: dict[str, Any], *, recognized_person_id: str | None,
           similarity: float | None = None, frame_ts: float | None = None,
           faces_present: bool | None = None) -> dict[str, Any]:
    """Called per frame from the face-recognition pipeline. Returns a status
    dict including whether a new-person promotion fired this frame.

    `recognized_person_id`: the result of face_recognizer (e.g. 'zeke', or None
                            if no face / unknown face).
    `similarity`: best-match similarity score from InsightFace (optional;
                  used as a confidence input).
    `faces_present`: whether the detector actually saw a face this frame.

    WHY faces_present EXISTS (added 2026-08-07, before this was ever wired live):
    the original code treated "unknown face" and "no face at all" as the same
    input — see the old comment "Unknown face (or no face at all)". The live
    capture loop reports person_id="unknown" for an EMPTY FRAME, so wiring this
    up verbatim meant 12 seconds of an empty room would fire a new-person
    promotion, write "There's an unknown person here" into my inner monologue,
    and log an audit row for a person who does not exist. Pass faces_present
    explicitly and nobody gets invented. None = unknown, preserves old behaviour
    for the legacy callers.
    """
    if frame_ts is None:
        frame_ts = time.time()
    out: dict[str, Any] = {"promoted_new_person": False}

    if faces_present is False:
        # Nobody in frame. Not an unknown person — an empty room. Clear any
        # in-flight candidacy so a person who leaves mid-jitter and a different
        # person who arrives later never merge into one 12-second candidate.
        with _TRACK_LOCK:
            st = _state(g)
            st["candidate_unknown"] = False
            st["candidate_unknown_since_ts"] = 0.0
        out["status"] = "no_face"
        return out

    persistence = float(_cfg("temporal_filter", "unknown_persistence_seconds", default=12.0))
    cooldown = float(_cfg("temporal_filter", "promotion_cooldown_seconds", default=300.0))

    with _TRACK_LOCK:
        st = _state(g)

        if recognized_person_id and recognized_person_id != "unknown":
            # Known face. Reset unknown candidacy.
            st["candidate_unknown"] = False
            st["candidate_unknown_since_ts"] = 0.0
            st["last_known_seen_ts"] = frame_ts
            if st["current_person_id"] == recognized_person_id:
                st["consecutive_frames"] = int(st.get("consecutive_frames") or 0) + 1
            else:
                st["current_person_id"] = recognized_person_id
                st["first_seen_ts"] = frame_ts
                st["consecutive_frames"] = 1
            st["last_seen_ts"] = frame_ts
            out["status"] = "known"
            out["person_id"] = recognized_person_id
            return out

        # Unknown face (or no face at all).
        if not recognized_person_id or recognized_person_id == "unknown":
            if not st.get("candidate_unknown"):
                # First frame of unknown candidacy. Don't promote yet.
                st["candidate_unknown"] = True
                st["candidate_unknown_since_ts"] = frame_ts
                out["status"] = "unknown_jitter_start"
                return out

            elapsed = frame_ts - float(st.get("candidate_unknown_since_ts") or frame_ts)
            if elapsed >= persistence:
                # Promote — but respect cooldown so we don't spam new-person events.
                last_promo = float(st.get("last_promotion_ts") or 0.0)
                if (frame_ts - last_promo) < cooldown:
                    out["status"] = "unknown_persisting_cooldown"
                    return out
                # Generate a temp id.
                temp_id = f"unknown_{int(frame_ts)}"
                st["last_promotion_ts"] = frame_ts
                st["promoted_unknown_id"] = temp_id
                st["current_person_id"] = temp_id
                st["first_seen_ts"] = float(st.get("candidate_unknown_since_ts") or frame_ts)
                st["candidate_unknown"] = False
                st["candidate_unknown_since_ts"] = 0.0
                out["promoted_new_person"] = True
                out["temp_id"] = temp_id
                out["status"] = "promoted"
                # Side-effect: inner-monologue note + signal bus.
                _on_promotion(g, temp_id, frame_ts)
                # Trust default — register at stranger trust if not already.
                _set_default_trust(g, temp_id)
                return out

            out["status"] = "unknown_jitter"
            out["elapsed_seconds"] = round(elapsed, 2)
            out["needs_more_seconds"] = round(persistence - elapsed, 2)
            return out

    return out


def tick_from_capture(g: dict[str, Any], face_results: Any = None,
                      recognized_person_id: str | None = None,
                      similarity: float | None = None,
                      frame_ts: float | None = None) -> dict[str, Any]:
    """THE hook the live video-capture loop calls. Takes raw loop state, does the
    gating, calls update(). Never raises.

    Wired into iris_runtime._iris_video_capture_loop 2026-08-07 — before that,
    `update()` had never once run in the Iris runtime. Its only live-looking call
    site was in brain/background_ticks.py, which is only reachable from
    avaagent.py, which has not run since this became Iris's harness. So the whole
    unknown-person promotion path was dead code that read as a feature.

    The loop passes `g["_face_results"]` straight in; presence is derived HERE
    rather than in the loop so the empty-room gate can be tuned by hot-swapping
    this function instead of restarting the runtime. Keep it module-level and
    closure-free — brain_hot_swap refuses closures.
    """
    try:
        n = len(face_results) if face_results is not None else 0
        pid = recognized_person_id
        if pid == "unknown":
            pid = None
        return update(g, recognized_person_id=pid, similarity=similarity,
                      frame_ts=frame_ts, faces_present=n > 0)
    except Exception as e:
        return {"promoted_new_person": False, "status": "error",
                "error": repr(e)[:200]}


def _on_promotion(g: dict[str, Any], temp_id: str, ts: float) -> None:
    """Side-effects when a new-person promotion fires:
    - Append to inner monologue.
    - Publish SIGNAL_NEW_PERSON_DETECTED if signal_bus is wired.
    - Append audit-trail row.
    """
    try:
        from brain import inner_monologue
        base = Path(g.get("BASE_DIR") or ".")
        inner_monologue._append_thought(
            base,
            "There's an unknown person here. I'm not initiating — staying reserved.",
            "face_tracking",
            "calmness",
        )
    except Exception as e:
        print(f"[face_tracking] inner_monologue note skipped: {e!r}")

    # The signal bus. This block used to import `publish, SIGNAL_PERSON_ONBOARDED`
    # from brain.signal_bus — NEITHER NAME HAS EVER EXISTED there (verified
    # 2026-08-07: the module exposes SignalBus.fire / get_signal_bus /
    # bootstrap_signal_bus and no module-level publish). So the import always
    # raised, publish was always None, and the fire was silently skipped inside a
    # bare `except: pass`. A dead code path that looked live for months, which is
    # the more dangerous kind. Using the real API now.
    try:
        bus = g.get("_signal_bus")
        if bus is None:
            from brain.signal_bus import get_signal_bus
            bus = get_signal_bus()
        if bus is not None:
            bus.fire("new_person_detected",
                     data={"temp_id": temp_id, "first_seen_ts": ts},
                     priority="medium")
    except Exception as e:
        print(f"[face_tracking] signal fire skipped: {e!r}")

    # Audit trail
    try:
        log_path = Path(g.get("BASE_DIR") or ".") / "state" / "face_tracking_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": ts,
                "event": "new_person_promoted",
                "temp_id": temp_id,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _set_default_trust(g: dict[str, Any], person_id: str) -> None:
    """Register the temp person at Trust Level 1 (stranger band)."""
    try:
        from brain import trust_system
        # get_trust_level initializes if missing — we want stranger init.
        trust_system.get_trust_level(person_id, g)
    except Exception:
        pass


def get_current_person(g: dict[str, Any]) -> dict[str, Any]:
    """Snapshot of the current temporal-filter state (for snapshot endpoint)."""
    with _TRACK_LOCK:
        st = _state(g)
        return {
            "person_id": st.get("current_person_id"),
            "first_seen_ts": st.get("first_seen_ts"),
            "consecutive_frames": st.get("consecutive_frames"),
            "candidate_unknown": st.get("candidate_unknown"),
            "candidate_unknown_seconds": (
                time.time() - float(st["candidate_unknown_since_ts"])
                if st.get("candidate_unknown") else 0.0
            ),
            "last_promotion_ts": st.get("last_promotion_ts"),
        }


# ── Onboarding command parser ────────────────────────────────────────


_TRUST_LEVEL_TO_SCORE = {1: 0.20, 2: 0.40, 3: 0.50, 4: 0.65, 5: 0.80}


_RELATIONSHIPS = ("friend", "family", "colleague", "partner", "girlfriend", "boyfriend",
                  "sister", "brother", "mother", "father", "mom", "dad", "spouse",
                  "wife", "husband", "child", "kid", "son", "daughter")


import re as _re

_INTRO_PHRASE_RE = _re.compile(
    r"\b(?:this is|meet)\s+(?:my\s+)?(?P<rel>\w+)\b",
    _re.IGNORECASE,
)
_TRUST_PHRASE_RE = _re.compile(
    r"\b(?:give (?:them|him|her)|set (?:their|his|her)|trust (?:level)?)\s*(?:to\s+)?(?P<lvl>[12345])\b",
    _re.IGNORECASE,
)
_INTRODUCE_YOURSELF_RE = _re.compile(r"\bintroduce yourself\b|\bsay hi\b|\bsay hello\b", _re.IGNORECASE)


def parse_onboarding_command(text: str) -> dict[str, Any]:
    """Parse a Zeke-side voice command for onboarding triggers.

    Returns:
    - {onboarding_intent: True, relationship: str|None, trust_score: float|None}
    - {onboarding_intent: False} if not an onboarding command.

    Examples that match:
    - "Hey ava, this is my friend, give them trust 3"
    - "Meet my colleague Sarah"
    - "Introduce yourself"
    - "Set their trust to 4"
    """
    if not text:
        return {"onboarding_intent": False}
    s = text.lower()
    intro = _INTRO_PHRASE_RE.search(s)
    trust = _TRUST_PHRASE_RE.search(s)
    introduce = _INTRODUCE_YOURSELF_RE.search(s)
    if not (intro or trust or introduce):
        return {"onboarding_intent": False}
    rel: str | None = None
    if intro:
        candidate = intro.group("rel").lower()
        if candidate in _RELATIONSHIPS:
            rel = candidate
    score: float | None = None
    if trust:
        try:
            lvl = int(trust.group("lvl"))
            score = _TRUST_LEVEL_TO_SCORE.get(lvl)
        except Exception:
            pass
    # Reject the intent if the regex matched but produced nothing meaningful.
    # Without this, common phrases like "meet his expectations" or "this is
    # great" trigger onboarding because `meet|this is\s+\w+` is too greedy.
    # Real onboarding requires either a known relationship word, a trust
    # level, or an explicit "introduce yourself" / "say hi" phrase.
    has_signal = bool(rel) or (score is not None) or bool(introduce)
    if not has_signal:
        return {"onboarding_intent": False}
    return {
        "onboarding_intent": True,
        "relationship": rel,
        "trust_score": score,
        "raw_text": text,
    }
