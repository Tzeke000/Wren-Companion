"""
Question engine — Ava decides when to ask Zeke a question.

Bootstrap-friendly: this module does NOT prescribe what Ava should be curious
about. It only:
  - tracks recent ask/answer history
  - enforces hard cooldowns (10 min between questions, no two in a row)
  - skips when the screen context says Zeke is busy (gaming/coding)
  - scores a small set of *categories* she could ask about and returns the
    top one when conditions are met. The categories themselves represent
    things she should genuinely understand to be a better companion.

State: state/question_history.jsonl — append-only log of {ts, category, text,
       asked_to, answered_at, answer_text}.

Categories (ordered roughly by importance, but Ava chooses freely):
  expression_calibration  — once early, when generic-threshold expressions
                             keep firing for a person who isn't calibrated.
  wake_word_clarification — handled by WakeLearner; we just record asks.
  voice_mood_check        — when current voice_mood notably differs from
                             baseline.
  emotional_check_in      — after emotionally heavy exchanges.
  general_curiosity       — open-ended ask about a topic Ava is curious about
                             but lacks context for.

The actual delivery (TTS speak + listen for answer) is handled by
proactive_triggers; this engine just decides *what* and *whether*.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional


_HISTORY_PATH = "state/question_history.jsonl"
_COOLDOWN_BETWEEN_QUESTIONS_SEC = 600.0  # 10 minutes
_BUSY_KEYWORDS = (
    "game", "minecraft", "vscode", "visual studio", "code editor", "ide",
    "compiler", "build", "debugger", "intellij", "pycharm", "jetbrains",
    "android studio",
)


class QuestionEngine:
    def __init__(self, base_dir: Path):
        self._base_dir = Path(base_dir)
        self._lock = threading.Lock()
        self._last_question_ts: float = 0.0
        self._last_category: str = ""
        self._answered_categories: set[str] = set()  # categories Ava has resolved
        self._load_history()

    def _path(self) -> Path:
        return self._base_dir / _HISTORY_PATH

    # ── history ────────────────────────────────────────────────────────────────

    def _load_history(self) -> None:
        p = self._path()
        if not p.is_file():
            return
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    ts = float(entry.get("ts") or 0)
                    cat = str(entry.get("category") or "")
                    if ts > self._last_question_ts:
                        self._last_question_ts = ts
                        self._last_category = cat
                    if entry.get("answered_at"):
                        self._answered_categories.add(cat)
        except Exception as e:
            print(f"[question_engine] history load error: {e}")

    def _record(self, entry: dict[str, Any]) -> None:
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[question_engine] history append error: {e}")

    # ── gating ─────────────────────────────────────────────────────────────────

    def _is_zeke_busy(self, g: dict[str, Any]) -> bool:
        screen_ctx = str(g.get("_screen_context") or "").lower()
        if any(kw in screen_ctx for kw in _BUSY_KEYWORDS):
            return True
        win = str(g.get("_active_window_title") or "").lower()
        return any(kw in win for kw in _BUSY_KEYWORDS)

    def cooldown_remaining(self) -> float:
        return max(0.0, _COOLDOWN_BETWEEN_QUESTIONS_SEC - (time.time() - self._last_question_ts))

    # ── candidate selection ────────────────────────────────────────────────────

    def _candidate_expression_calibration(self, g: dict[str, Any]) -> Optional[dict[str, Any]]:
        if "expression_calibration" in self._answered_categories:
            return None
        cal = g.get("_expression_calibrator")
        pid = str(g.get("_recognized_person_id") or "")
        if not cal or not pid or pid == "unknown":
            return None
        try:
            calibrated = cal.is_calibrated(pid)
        except Exception:
            return None
        if calibrated:
            return None
        # Only ask once we've actually seen them often enough to know something
        # is off (e.g. constant "surprised" reads).
        try:
            base = cal.get_baseline(pid)
            samples = int(base.get("sample_count") or 0)
        except Exception:
            samples = 0
        if samples < 60:
            return None
        return {
            "category": "expression_calibration",
            "priority": 0.9,
            "text": (
                "I keep reading your expression as surprised but I'm not sure that's right for you. "
                "What does your face actually look like when you're genuinely surprised?"
            ),
        }

    def _candidate_voice_mood_check(self, g: dict[str, Any]) -> Optional[dict[str, Any]]:
        vm = g.get("_voice_mood")
        if not isinstance(vm, dict):
            return None
        label = str(vm.get("label") or "neutral")
        if label not in ("quiet_or_tired", "excited"):
            return None
        # Throttle to once per topic per day.
        if self._last_category == "voice_mood_check" and (time.time() - self._last_question_ts) < 6 * 3600:
            return None
        descriptor = "quiet" if label == "quiet_or_tired" else "energetic"
        return {
            "category": "voice_mood_check",
            "priority": 0.55,
            "text": f"You sound a bit {descriptor} today — everything alright?",
        }

    def _candidate_emotional_checkin(self, g: dict[str, Any]) -> Optional[dict[str, Any]]:
        # Heavy-topic flag is set by deep_self / repair systems when they detect
        # emotionally weighty content. We just check the flag.
        if not bool(g.get("_recent_heavy_topic")):
            return None
        if self._last_category == "emotional_check_in" and (time.time() - self._last_question_ts) < 4 * 3600:
            return None
        return {
            "category": "emotional_check_in",
            "priority": 0.7,
            "text": "How does that make you feel, Ezekiel?",
        }

    def _candidate_general_curiosity(self, g: dict[str, Any]) -> Optional[dict[str, Any]]:
        # Surface a single curiosity topic Ava lacks context for.
        try:
            from brain.curiosity_topics import get_current_curiosity
            cur = get_current_curiosity(g)
        except Exception:
            cur = None
        if not isinstance(cur, dict):
            return None
        topic = str(cur.get("topic") or "").strip()
        if not topic:
            return None
        sig = f"general_curiosity:{topic}"
        if sig in self._answered_categories:
            return None
        return {
            "category": "general_curiosity",
            "priority": 0.4,
            "text": f"I realised I don't know much about {topic} — can I ask you about it?",
            "topic": topic,
        }

    # ── public API ─────────────────────────────────────────────────────────────

    def consider_asking(self, g: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Decide whether Ava should ask a question right now. Returns a dict
        {category, text, priority} the caller should deliver, or None."""
        if not bool(g.get("tts_enabled", False)):
            return None
        if self.cooldown_remaining() > 0:
            return None
        if self._is_zeke_busy(g):
            return None
        # Don't fire a new question while Zeke and Ava are mid-conversation.
        # This is independent of cooldown — even if 10 minutes have passed,
        # we should not interrupt an ongoing exchange.
        if bool(g.get("_conversation_active")) or bool(g.get("_turn_in_progress")):
            return None

        candidates: list[dict[str, Any]] = []
        for picker in (
            self._candidate_expression_calibration,
            self._candidate_emotional_checkin,
            self._candidate_voice_mood_check,
            self._candidate_general_curiosity,
        ):
            try:
                c = picker(g)
                if c:
                    candidates.append(c)
            except Exception as e:
                print(f"[question_engine] candidate error: {e}")

        if not candidates:
            return None

        # Pick highest priority — but never repeat last category twice in a row.
        candidates.sort(key=lambda c: float(c.get("priority") or 0), reverse=True)
        for c in candidates:
            if c["category"] != self._last_category:
                return c
        return None

    def mark_asked(self, category: str, text: str, asked_to: str = "zeke", meta: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            self._last_question_ts = time.time()
            self._last_category = category
        self._record({
            "ts": time.time(),
            "category": category,
            "text": text,
            "asked_to": asked_to,
            "meta": meta or {},
        })
        print(f"[question_engine] asked category={category}: {text[:80]!r}")

    def mark_answered(self, category: str, answer_text: str) -> None:
        with self._lock:
            self._answered_categories.add(category)
        self._record({
            "ts": time.time(),
            "category": category,
            "answered_at": time.time(),
            "answer_text": str(answer_text or "")[:500],
        })
        print(f"[question_engine] answered category={category}: {answer_text[:60]!r}")


# ── singleton ─────────────────────────────────────────────────────────────────

_SINGLETON: Optional[QuestionEngine] = None
_LOCK = threading.Lock()


def get_question_engine(base_dir: Optional[Path] = None) -> Optional[QuestionEngine]:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    if base_dir is None:
        return None
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = QuestionEngine(Path(base_dir))
    return _SINGLETON


def bootstrap_question_engine(g: dict[str, Any]) -> Optional[QuestionEngine]:
    base = Path(g.get("BASE_DIR") or ".")
    qe = get_question_engine(base)
    g["_question_engine"] = qe
    return qe
