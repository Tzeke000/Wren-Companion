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

    @property
    def voice_control_file(self) -> Path:
        """Ear-mute control channel (orb "Input on" button, wired 2026-07-10).
        JSON {mic_muted: bool, ping_ts: float} — the SAME file
        voice/wren_voice_status.py declares as CONTROL_FILE (a Wren-era
        channel the daemon's capture loop already honors; nothing wrote it
        until the orb toggle was wired). mic_muted=True → the daemon's
        cmd_listen / cmd_bargein_watch don't capture: Zeke has explicitly
        muted Iris's ears (e.g. he's on a call with a friend and doesn't
        want side-chatter landing as voice turns). Written by orb_http
        /api/v1/voice_input/toggle; missing/unreadable = ears ON (fail-open:
        a broken read never silences the ears)."""
        return self.root / "scratch" / "voice_control.json"

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

    # ── visual attention (2026-08-06) ────────────────────────────────────────
    # Added when brain/visual_attention.py was built. Note that the vision and
    # vector state paths that predate this are hardcoded module constants in
    # their own files; these are declared here because that is the convention
    # this file exists to enforce, and new code should follow it.
    @property
    def attention_dir(self) -> Path:
        return self.state_dir / "attention"

    @property
    def attention_state(self) -> Path:
        """Live snapshot of what I'm looking at — target, lock status, bearing,
        and what that bearing costs me. Written atomically a few times a second
        at most; a consumer should treat anything older than ~2.5s as stale."""
        return self.attention_dir / "attention_state.json"

    @property
    def attention_log(self) -> Path:
        """Append-only transition log (target set / acquired / lost / home).
        Transitions ONLY, never per-frame — rotates at 5MB."""
        return self.attention_dir / "attention_log.jsonl"


# Module-level singleton — import as `from brain.iris_paths import paths`.
paths = _IrisPaths()
