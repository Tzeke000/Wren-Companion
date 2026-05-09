"""
Correction handler.

Detects when Zeke is correcting something Ava just did. Uses the recent
action context (g["_last_action"]) to figure out what to redo and learn the
mapping.

Storage:
  state/correction_log.jsonl  — append-only history of corrections + fixes
  state/learned_commands.json — mapping from corrected phrases to actions
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional


_LOG_PATH = "state/correction_log.jsonl"
_LEARNED_PATH = "state/learned_commands.json"


# Phrase patterns. Order matters — more specific patterns first.
_CORRECTION_PATTERNS = [
    # "I meant X" / "I said X not Y" — specific
    re.compile(r"\bi (?:said|meant) ['\"]?([^'\"]+?)['\"]?\b(?:[,\.\?!]|$)", re.IGNORECASE),
    re.compile(r"\bnot (?:that|this|what)? *(?:one|thing)? *,? ?(?:i meant|i said) ['\"]?([^'\"]+?)['\"]?\b", re.IGNORECASE),
    # Pure rejection phrases — no replacement supplied
    re.compile(r"^\s*(?:no,? )?(?:that's|thats) (?:not (?:right|what i meant|it)|wrong)", re.IGNORECASE),
    re.compile(r"^\s*(?:no,? )?wrong (?:one|thing|app|tab)", re.IGNORECASE),
    re.compile(r"^\s*not (?:that|this|what i (?:said|meant|wanted))", re.IGNORECASE),
    re.compile(r"^\s*try again\b", re.IGNORECASE),
    re.compile(r"^\s*no\.?\s+the (?:other|different) (?:one|app|tab|thing)\b", re.IGNORECASE),
]


def detect_correction(text: str) -> tuple[bool, Optional[str]]:
    """Return (is_correction, replacement_phrase_if_any)."""
    if not text:
        return False, None
    for pat in _CORRECTION_PATTERNS:
        m = pat.search(text)
        if m:
            replacement = m.group(1).strip() if m.groups() else None
            return True, replacement
    return False, None


class CorrectionHandler:
    def __init__(self, base_dir: Path):
        self._base = Path(base_dir)
        self._lock = threading.Lock()

    # ── persistence ────────────────────────────────────────────────────────────

    def _log_path(self) -> Path:
        return self._base / _LOG_PATH

    def _learned_path(self) -> Path:
        return self._base / _LEARNED_PATH

    def _append_log(self, entry: dict[str, Any]) -> None:
        p = self._log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[correction_handler] log error: {e}")

    def _load_learned(self) -> dict[str, str]:
        p = self._learned_path()
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_learned(self, learned: dict[str, str]) -> None:
        p = self._learned_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(json.dumps(learned, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[correction_handler] learned save error: {e}")

    # ── public API ────────────────────────────────────────────────────────────

    def get_learned_action(self, phrase: str) -> Optional[str]:
        learned = self._load_learned()
        return learned.get((phrase or "").lower().strip())

    def record_correction(
        self,
        original_phrase: str,
        wrong_action: str,
        correct_action: str,
    ) -> None:
        original_phrase = (original_phrase or "").lower().strip()
        if not original_phrase or not correct_action:
            return
        with self._lock:
            learned = self._load_learned()
            learned[original_phrase] = correct_action
            self._save_learned(learned)
            self._append_log({
                "ts": time.time(),
                "phrase": original_phrase,
                "wrong_action": wrong_action,
                "correct_action": correct_action,
                "learned": True,
            })
        print(f"[correction] learned: {original_phrase!r} → {correct_action}")

    def handle(self, text: str, g: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Called from reply_engine BEFORE the voice command router. If the
        utterance looks like a correction, returns a dict the caller should
        treat as the response. None otherwise."""
        is_corr, replacement = detect_correction(text)
        if not is_corr:
            return None

        last_action = g.get("_last_action")
        last_phrase = str(g.get("_last_user_input_pre_router") or "")

        # If Zeke said "I meant X", treat X as a fresh utterance — re-route it.
        if replacement:
            # Record the correction for the previous phrase if we have one.
            if last_action and last_phrase:
                self.record_correction(last_phrase, str(last_action.get("trigger") or ""), replacement)
            # Re-route: hand back to the voice command router with X as input.
            try:
                from brain.voice_commands import get_voice_command_router
                router = get_voice_command_router()
                if router is not None:
                    handled, resp = router.route(replacement, g, allow_correction=False)
                    if handled:
                        return {"ok": True, "response": resp, "rerun": replacement}
            except Exception:
                pass
            return {"ok": True, "response": f"Got it — let me try {replacement}."}

        # No replacement — just an "undo" / "wrong one" rejection.
        return {
            "ok": True,
            "response": "Got it — let me try that again. What did you mean?",
            "needs_clarification": True,
        }


# ── singleton ─────────────────────────────────────────────────────────────────

_SINGLETON: Optional[CorrectionHandler] = None
_LOCK = threading.Lock()


def get_correction_handler(base_dir: Optional[Path] = None) -> Optional[CorrectionHandler]:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    if base_dir is None:
        return None
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = CorrectionHandler(Path(base_dir))
    return _SINGLETON


def bootstrap_correction_handler(g: dict[str, Any]) -> Optional[CorrectionHandler]:
    base = Path(g.get("BASE_DIR") or ".")
    ch = get_correction_handler(base)
    g["_correction_handler"] = ch
    return ch
