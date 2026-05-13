"""
brain/iris_paths.py — single source of truth for all flag/state paths.

Before this module existed, paths like `.tmp/voice_session.flag` and
`state/iris_chat/.pending` were repeated as literal strings across:
  - iris_runtime.py
  - brain/iris_chat.py, brain/iris_llm.py
  - brain/orb_http.py
  - scripts/voice_stop_hook.py

A typo in one place silently breaks the integration (chat thinks pending,
hook doesn't see it). Centralizing here.

Usage:

    from brain.iris_paths import paths
    paths.configure(root_dir)
    if paths.voice_flag.exists():
        ...
    chat_pending = paths.chat_pending_flag.exists()
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


class _IrisPaths:
    """Resolves paths against a root directory. configure() once, then read
    properties. All paths are absolute Path objects."""

    def __init__(self) -> None:
        self._root: Optional[Path] = None

    def configure(self, root: Path | str) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        if self._root is None:
            # Fall back to module-relative root — brain/ is one level under repo root
            return Path(__file__).resolve().parent.parent
        return self._root

    # ── Voice mode ──────────────────────────────────────────────────────────
    @property
    def tmp_dir(self) -> Path:
        return self.root / ".tmp"

    @property
    def voice_flag(self) -> Path:
        """Voice mode is ON when this file exists. The Stop hook polls it
        every turn; iris_runtime tools can check before voice-only behavior."""
        return self.tmp_dir / "voice_session.flag"

    @property
    def last_spoken_uuid(self) -> Path:
        """Idempotency for voice TTS — the last uuid we spoke. Stop hook
        skips re-speaking the same assistant message."""
        return self.tmp_dir / "last_spoken_uuid.txt"

    @property
    def body_pause_flag(self) -> Path:
        """Hard kill-switch for the body's autonomous behaviors. When this
        file exists, the body-side wake-and-capture loop (and any other
        autonomous channel emitters) revert to passive mode — wake word
        listener stays on for diagnostics but no audio is captured and no
        channel events are emitted. Drop the file from PowerShell or via
        the voice_body_pause MCP tool if the body is misbehaving."""
        return self.tmp_dir / "body_pause.flag"

    # ── Chat ────────────────────────────────────────────────────────────────
    @property
    def chat_dir(self) -> Path:
        return self.root / "state" / "iris_chat"

    @property
    def chat_pending_flag(self) -> Path:
        """Chat request waiting for me to answer. Stop hook fires the
        chat-handle rewake when this exists."""
        return self.chat_dir / ".pending"

    # ── LLM bridge ──────────────────────────────────────────────────────────
    @property
    def llm_dir(self) -> Path:
        return self.root / "state" / "iris_llm"

    @property
    def llm_pending_flag(self) -> Path:
        """A brain/* module has called ask_iris() and is waiting. Stop hook
        rewakes me with the prompt."""
        return self.llm_dir / ".pending"

    # ── State files for the orb + memory ────────────────────────────────────
    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def transcript(self) -> Path:
        return self.state_dir / "transcript.jsonl"

    @property
    def iris_memory(self) -> Path:
        return self.state_dir / "iris_memory.jsonl"

    @property
    def iris_mood(self) -> Path:
        return self.state_dir / "iris_mood.json"

    @property
    def iris_time(self) -> Path:
        return self.state_dir / "iris_time.json"

    @property
    def journal(self) -> Path:
        return self.state_dir / "journal.jsonl"

    @property
    def inner_monologue(self) -> Path:
        return self.state_dir / "iris_inner_monologue.jsonl"

    @property
    def widget_position(self) -> Path:
        return self.state_dir / "widget_position.json"

    @property
    def orb_active_tab(self) -> Path:
        return self.state_dir / "orb_active_tab.txt"


# Module-level singleton — import as `from brain.iris_paths import paths`.
paths = _IrisPaths()
