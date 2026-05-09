"""
Wake-word learner.

Augments WakeDetector with patterns Ava learns at runtime by either:
  1) explicit correction — Zeke confirms "yes I was talking to you" /
     "no I wasn't" after Ava asks
  2) implicit correction — Zeke immediately sends a follow-up indicating
     it was indirect (e.g. responds to a totally unrelated topic)

Persistence: state/wake_patterns_learned.json
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional


_LEARNED_PATH = "state/wake_patterns_learned.json"
_CLARIFY_COOLDOWN_SEC = 300.0  # 5 minutes between clarification asks


class WakeLearner:
    def __init__(self, base_dir: Path):
        self._base_dir = Path(base_dir)
        self._lock = threading.Lock()
        self._direct: list[str] = []
        self._indirect: list[str] = []
        self._last_clarify_ts: float = 0.0
        self._load()

    # ── persistence ────────────────────────────────────────────────────────────

    def _path(self) -> Path:
        return self._base_dir / _LEARNED_PATH

    def _load(self) -> None:
        p = self._path()
        if not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._direct = [str(x) for x in data.get("direct", []) if isinstance(x, str)]
                self._indirect = [str(x) for x in data.get("indirect", []) if isinstance(x, str)]
        except Exception as e:
            print(f"[wake_learner] load error: {e}")

    def _save(self) -> None:
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(json.dumps({
                "direct": self._direct,
                "indirect": self._indirect,
                "last_updated": time.time(),
            }, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[wake_learner] save error: {e}")

    # ── pattern extraction ────────────────────────────────────────────────────

    @staticmethod
    def _extract_pattern(text: str) -> Optional[str]:
        """Pick a 2-4 word window around 'ava' as the learned pattern.
        Escapes regex metacharacters and wraps in word boundaries."""
        t = (text or "").lower().strip()
        if "ava" not in t:
            return None
        words = t.split()
        try:
            idx = next(i for i, w in enumerate(words) if "ava" in w)
        except StopIteration:
            return None
        start = max(0, idx - 1)
        end = min(len(words), idx + 2)
        chunk = " ".join(words[start:end])
        # Escape and add boundaries.
        return r"\b" + re.escape(chunk) + r"\b"

    # ── public API ─────────────────────────────────────────────────────────────

    def learn_from_correction(self, text: str, was_direct: bool, g: dict[str, Any]) -> None:
        pattern = self._extract_pattern(text)
        if not pattern:
            return
        with self._lock:
            target = self._direct if was_direct else self._indirect
            if pattern in target:
                return
            target.append(pattern)
            self._save()
        # Push into the live detector singleton too.
        try:
            from brain.wake_detector import get_wake_detector
            get_wake_detector().add_learned(pattern, was_direct)
        except Exception as e:
            print(f"[wake_learner] live update failed: {e}")
        print(f"[wake_learner] learned pattern direct={was_direct}: {pattern!r}")

    def hydrate_detector(self) -> None:
        """Push existing learned patterns into the WakeDetector singleton."""
        try:
            from brain.wake_detector import get_wake_detector
            wd = get_wake_detector()
            with self._lock:
                for p in self._direct:
                    wd.add_learned(p, True)
                for p in self._indirect:
                    wd.add_learned(p, False)
        except Exception as e:
            print(f"[wake_learner] hydrate error: {e}")

    def can_clarify(self) -> bool:
        return (time.time() - self._last_clarify_ts) >= _CLARIFY_COOLDOWN_SEC

    def ask_clarification(self, text: str, g: dict[str, Any]) -> bool:
        """Speak a clarification question via the TTS worker. Records cooldown.
        Does NOT block waiting for the answer — voice_loop will associate the
        next utterance with this prompt via _wake_clarify_pending."""
        if not self.can_clarify():
            return False
        worker = g.get("_tts_worker")
        if worker is None or not getattr(worker, "available", False):
            return False
        if not bool(g.get("tts_enabled", False)):
            return False
        snippet = (text or "").strip()
        if len(snippet) > 35:
            snippet = snippet[:35].rsplit(" ", 1)[0] + "…"
        question = f"Hey — were you talking to me just now when you said '{snippet}'?"
        try:
            worker.speak_with_emotion(question, emotion="curiosity", intensity=0.5, blocking=False)
        except Exception as e:
            print(f"[wake_learner] speak failed: {e}")
            return False
        self._last_clarify_ts = time.time()
        g["_wake_clarify_pending"] = {
            "asked_at": time.time(),
            "ambiguous_text": text,
        }
        return True


# ── singleton ─────────────────────────────────────────────────────────────────

_SINGLETON: Optional[WakeLearner] = None
_LOCK = threading.Lock()


def get_wake_learner(base_dir: Optional[Path] = None) -> Optional[WakeLearner]:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    if base_dir is None:
        return None
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = WakeLearner(Path(base_dir))
            _SINGLETON.hydrate_detector()
    return _SINGLETON


def bootstrap_wake_learner(g: dict[str, Any]) -> Optional[WakeLearner]:
    base = Path(g.get("BASE_DIR") or ".")
    wl = get_wake_learner(base)
    g["_wake_learner"] = wl
    return wl
