"""
Custom command + custom tab builder.

Lets Ava (and Zeke via voice) create:
  - Voice commands: trigger phrase → action → response template
  - UI tabs: web_embed / journal_view / data_display / image_gallery / etc

Storage:
  state/custom_commands.json — list of dicts {trigger, action, description, params}
  state/custom_tabs.json     — list of dicts {id, name, content_type, data_source, ...}

Bootstrap-friendly: we store what Ava and Zeke build at runtime. We do NOT
seed defaults — the file is empty until something is created.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional


_COMMANDS_PATH = "state/custom_commands.json"
_TABS_PATH = "state/custom_tabs.json"

_VALID_TAB_TYPES = (
    "web_embed",       # iframe to a URL
    "journal_view",    # rendered journal entries
    "data_display",    # JSON data from any operator endpoint
    "image_gallery",   # images from a directory
    "custom_stats",    # specific snapshot fields
    "chat_log",        # filtered conversation history
)


class CommandBuilder:
    def __init__(self, base_dir: Path):
        self._base = Path(base_dir)
        self._lock = threading.Lock()

    # ── persistence ────────────────────────────────────────────────────────────

    def _commands_path(self) -> Path:
        return self._base / _COMMANDS_PATH

    def _tabs_path(self) -> Path:
        return self._base / _TABS_PATH

    def load_commands(self) -> list[dict[str, Any]]:
        p = self._commands_path()
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            if isinstance(data, dict):
                # Accept either {"commands": [...]} or single dict
                items = data.get("commands")
                if isinstance(items, list):
                    return [d for d in items if isinstance(d, dict)]
        except Exception as e:
            print(f"[command_builder] commands load error: {e}")
        return []

    def save_commands(self, commands: list[dict[str, Any]]) -> None:
        p = self._commands_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(json.dumps(commands, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[command_builder] commands save error: {e}")

    def load_tabs(self) -> list[dict[str, Any]]:
        p = self._tabs_path()
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception as e:
            print(f"[command_builder] tabs load error: {e}")
        return []

    def save_tabs(self, tabs: list[dict[str, Any]]) -> None:
        p = self._tabs_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(json.dumps(tabs, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[command_builder] tabs save error: {e}")

    # ── command CRUD ──────────────────────────────────────────────────────────

    def create_command(
        self,
        trigger: str,
        action: str,
        description: str = "",
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        trigger = (trigger or "").strip().lower()
        action = (action or "").strip()
        if not trigger or not action:
            return {"ok": False, "error": "trigger and action required"}
        with self._lock:
            commands = self.load_commands()
            # Replace existing command with same trigger
            commands = [c for c in commands if c.get("trigger", "").lower() != trigger]
            entry = {
                "trigger": trigger,
                "action": action,
                "description": description.strip(),
                "params": dict(params or {}),
                "created_ts": time.time(),
            }
            commands.append(entry)
            self.save_commands(commands)
        # Hot-reload into the live router so the trigger works immediately.
        try:
            from brain.voice_commands import get_voice_command_router
            router = get_voice_command_router()
            if router is not None:
                router.reload_custom_commands()
        except Exception:
            pass
        print(f"[command_builder] created: {trigger!r} → {action}")
        return {"ok": True, "command": entry}

    def delete_command(self, trigger: str) -> dict[str, Any]:
        trigger = (trigger or "").strip().lower()
        if not trigger:
            return {"ok": False, "error": "trigger required"}
        with self._lock:
            commands = self.load_commands()
            before = len(commands)
            commands = [c for c in commands if c.get("trigger", "").lower() != trigger]
            removed = before - len(commands)
            self.save_commands(commands)
        try:
            from brain.voice_commands import get_voice_command_router
            router = get_voice_command_router()
            if router is not None:
                router.reload_custom_commands()
        except Exception:
            pass
        print(f"[command_builder] deleted {removed} command(s) for {trigger!r}")
        return {"ok": True, "removed": removed}

    # ── tab CRUD ──────────────────────────────────────────────────────────────

    def create_tab(
        self,
        name: str,
        content_type: str,
        data_source: str = "",
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        content_type = (content_type or "").strip().lower()
        if not name or content_type not in _VALID_TAB_TYPES:
            return {
                "ok": False,
                "error": f"name + valid content_type required (one of {_VALID_TAB_TYPES})",
            }
        # Stable id from name
        tab_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or f"tab_{int(time.time())}"
        with self._lock:
            tabs = self.load_tabs()
            tabs = [t for t in tabs if t.get("id") != tab_id]
            entry = {
                "id": tab_id,
                "name": name,
                "content_type": content_type,
                "data_source": data_source,
                "config": dict(config or {}),
                "created_ts": time.time(),
            }
            tabs.append(entry)
            self.save_tabs(tabs)
        print(f"[command_builder] created tab: {name!r} ({content_type})")
        return {"ok": True, "tab": entry}

    def delete_tab(self, tab_id: str) -> dict[str, Any]:
        tab_id = (tab_id or "").strip().lower()
        if not tab_id:
            return {"ok": False, "error": "tab id required"}
        with self._lock:
            tabs = self.load_tabs()
            before = len(tabs)
            tabs = [t for t in tabs if t.get("id") != tab_id]
            removed = before - len(tabs)
            self.save_tabs(tabs)
        return {"ok": True, "removed": removed}

    # ── conversational helpers (used by VoiceCommandRouter on a 2-step flow) ──

    def begin_command_creation(self, phrase: str, g: dict[str, Any]) -> str:
        """Start an interactive command-creation flow. Returns the question
        Ava should ask. The router stashes pending state in g and resumes
        when Zeke answers."""
        pending = {
            "kind": "command",
            "phrase": phrase,
            "stage": "ask_action",
            "started_ts": time.time(),
        }
        g["_command_builder_pending"] = pending
        return f"What should '{phrase}' do?"

    def begin_tab_creation(self, name: str, g: dict[str, Any]) -> str:
        pending = {
            "kind": "tab",
            "name": name,
            "stage": "ask_content_type",
            "started_ts": time.time(),
        }
        g["_command_builder_pending"] = pending
        return (
            f"What should the {name} tab display? "
            "A website, your journal, stats, or images?"
        )

    def resume_pending(self, answer: str, g: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Continue a pending creation flow with Zeke's answer. Returns
        {ok, response} on completion, or None if there was nothing pending."""
        pending = g.get("_command_builder_pending")
        if not isinstance(pending, dict):
            return None
        kind = pending.get("kind")
        if kind == "command":
            phrase = str(pending.get("phrase") or "")
            action = answer.strip()
            if not action:
                return {"ok": False, "response": "I need an action to attach to it."}
            res = self.create_command(phrase, action, description="learned from voice")
            g.pop("_command_builder_pending", None)
            return {"ok": res.get("ok", False), "response": f"Got it — I'll remember '{phrase}'."}
        if kind == "tab":
            name = str(pending.get("name") or "")
            content_type, data_source, config = self._classify_tab_answer(answer)
            res = self.create_tab(name, content_type, data_source=data_source, config=config)
            g.pop("_command_builder_pending", None)
            ok = res.get("ok", False)
            if ok:
                return {"ok": True, "response": f"Done — check your {name} tab."}
            return {"ok": False, "response": "I couldn't make that tab — let me know more details."}
        return None

    @staticmethod
    def _classify_tab_answer(answer: str) -> tuple[str, str, dict[str, Any]]:
        a = answer.lower().strip()
        # Default to a stats tab if Zeke is vague.
        if not a:
            return "custom_stats", "snapshot", {"stats_fields": ["mood", "ribbon", "voice_loop"]}
        if "journal" in a:
            return "journal_view", "/api/v1/journal/shared", {}
        if "image" in a or "picture" in a or "gallery" in a:
            return "image_gallery", "/api/v1/images/list", {}
        # URL hint
        m = re.search(r"https?://\S+", answer)
        if m:
            return "web_embed", m.group(0), {}
        if "website" in a or "url" in a or "site" in a:
            return "web_embed", "https://", {}
        if "stats" in a or "data" in a or "live" in a:
            return "custom_stats", "snapshot", {"stats_fields": ["mood", "ribbon", "voice_loop"]}
        return "custom_stats", "snapshot", {"stats_fields": ["mood", "ribbon"]}


# ── singleton ─────────────────────────────────────────────────────────────────

_SINGLETON: Optional[CommandBuilder] = None
_LOCK = threading.Lock()


def get_command_builder(base_dir: Optional[Path] = None) -> Optional[CommandBuilder]:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    if base_dir is None:
        return None
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = CommandBuilder(Path(base_dir))
    return _SINGLETON


def bootstrap_command_builder(g: dict[str, Any]) -> Optional[CommandBuilder]:
    base = Path(g.get("BASE_DIR") or ".")
    cb = get_command_builder(base)
    g["_command_builder"] = cb
    return cb
