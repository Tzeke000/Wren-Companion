"""
Phase 52 — Smart screenshot management.

EpisodicVisualMemory captures screenshots when warranted, extracts knowledge
via LLaVA, and stores facts in the concept graph. Ava decides when screenshots
are useful — not on every tick.

Bootstrap: Ava tracks which screenshot-derived knowledge she later references.
Screenshots that produced useful knowledge keep their metadata longer.
She develops her own judgment about when visual capture is worth it.
"""
from __future__ import annotations

import json
import os
import time
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


CATEGORIES = ("learning", "error", "navigation", "gaming", "general")
MAX_PER_CATEGORY = 50
AUTO_DELETE_SECONDS = 60
EPISODES_DIR = Path("state/visual_episodes")


@dataclass
class VisualEpisode:
    path: str
    reason: str
    category: str
    ts: float
    knowledge: str = ""
    knowledge_used: int = 0
    keep: bool = False


class EpisodicVisualMemory:
    def __init__(self, base_dir: Optional[Path] = None):
        self._base = Path(base_dir) if base_dir else Path(".")
        self._episodes_dir = self._base / "state" / "visual_episodes"
        self._meta_path = self._base / "state" / "visual_episodes_meta.json"
        self._episodes: list[VisualEpisode] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._meta_path.is_file():
            return
        try:
            rows = json.loads(self._meta_path.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                for r in rows:
                    if isinstance(r, dict):
                        self._episodes.append(VisualEpisode(**{k: v for k, v in r.items() if k in VisualEpisode.__dataclass_fields__}))
        except Exception:
            pass

    def _save(self) -> None:
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        self._meta_path.write_text(
            json.dumps([asdict(e) for e in self._episodes], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def should_capture(self, context: str) -> bool:
        """Heuristic: capture if context mentions error, learning, game state, or navigation."""
        low = context.lower()
        triggers = ("error", "exception", "traceback", "loading", "navigat", "game", "tutorial", "screenshot", "see", "look at", "check")
        return any(t in low for t in triggers)

    def capture_and_store(self, reason: str, category: str = "general") -> Optional[str]:
        if category not in CATEGORIES:
            category = "general"
        try:
            import subprocess
            import sys
            self._episodes_dir.mkdir(parents=True, exist_ok=True)
            fname = f"ep_{category}_{int(time.time()*1000)}.png"
            fpath = self._episodes_dir / fname

            # Use PIL if available for screenshot
            try:
                from PIL import ImageGrab  # type: ignore
                img = ImageGrab.grab()
                img.save(str(fpath))
            except ImportError:
                # Fallback: PowerShell screenshot
                ps_cmd = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Screen]::PrimaryScreen | Out-Null; $bmp = New-Object System.Drawing.Bitmap([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width, [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height); $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen(0,0,0,0,$bmp.Size); $bmp.Save('{fpath}')"
                subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=10)

            if not fpath.is_file():
                return None

            ep = VisualEpisode(path=str(fpath), reason=reason[:200], category=category, ts=time.time())
            with self._lock:
                self._episodes.append(ep)
                self.cleanup_old()
                self._save()

            # Auto-delete after timeout unless flagged
            def _auto_delete():
                time.sleep(AUTO_DELETE_SECONDS)
                with self._lock:
                    for e in self._episodes:
                        if e.path == str(fpath) and not e.keep:
                            try:
                                Path(e.path).unlink(missing_ok=True)
                            except Exception:
                                pass
                            break

            threading.Thread(target=_auto_delete, daemon=True).start()
            return str(fpath)
        except Exception:
            return None

    def extract_knowledge(self, screenshot_path: str, delete_after: bool = True) -> str:
        """Send screenshot to LLaVA, extract text knowledge, optionally delete image."""
        try:
            from langchain_ollama import ChatOllama
            from langchain_core.messages import HumanMessage
            import base64

            img_bytes = Path(screenshot_path).read_bytes()
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            llm = ChatOllama(model="llava:latest", temperature=0.1)
            result = llm.invoke([HumanMessage(content=[
                {"type": "text", "text": "Briefly describe what is shown in this screenshot. What is the user doing? What important information is visible? Be concise."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ])])
            knowledge = (getattr(result, "content", None) or str(result)).strip()[:600]
            with self._lock:
                for e in self._episodes:
                    if e.path == screenshot_path:
                        e.knowledge = knowledge
                        break
                self._save()
            if delete_after:
                try:
                    Path(screenshot_path).unlink(missing_ok=True)
                except Exception:
                    pass
            return knowledge
        except Exception as ex:
            return f"(knowledge extraction failed: {ex!r})"

    def cleanup_old(self) -> int:
        removed = 0
        for cat in CATEGORIES:
            cat_eps = [e for e in self._episodes if e.category == cat]
            if len(cat_eps) > MAX_PER_CATEGORY:
                by_age = sorted(cat_eps, key=lambda e: (e.keep, e.ts))
                to_remove = by_age[:len(cat_eps) - MAX_PER_CATEGORY]
                for e in to_remove:
                    try:
                        Path(e.path).unlink(missing_ok=True)
                    except Exception:
                        pass
                    self._episodes.remove(e)
                    removed += 1
        return removed

    def mark_knowledge_used(self, screenshot_path: str) -> None:
        with self._lock:
            for e in self._episodes:
                if e.path == screenshot_path:
                    e.knowledge_used += 1
                    e.keep = e.knowledge_used >= 2
            self._save()

    def get_recent(self, limit: int = 10) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in sorted(self._episodes, key=lambda e: e.ts, reverse=True)[:limit]]
