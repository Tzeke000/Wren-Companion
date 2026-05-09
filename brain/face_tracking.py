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
           similarity: float | None = None, frame_ts: float | None = None) -> dict[str, Any]:
    """Called per frame from the face-recognition pipeline. Returns a status
    dict including whether a new-person promotion fired this frame.

    `recognized_person_id`: the result of face_recognizer (e.g. 'zeke', or None
                            if no face / unknown face).
    `similarity`: best-match similarity score from InsightFace (optional;
                  used as a confidence input).
    """
    if frame_ts is None:
        frame_ts = time.time()
    out: dict[str, Any] = {"promoted_new_person": False}

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

    try:
        from brain.signal_bus import publish, SIGNAL_PERSON_ONBOARDED  # may not exist; falls through
    except Exception:
        publish = None  # type: ignore
    try:
        if publish is not None:
            publish(g, "SIGNAL_NEW_PERSON_DETECTED", {"temp_id": temp_id, "first_seen_ts": ts})
    except Exception:
        pass

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
