"""
Phase 66 — Ava's own goal system.

Goals Ava sets for herself — not assigned by Zeke.
They emerge from persistent curiosity topics and her interactions.
Bootstrap: Ava's goals reveal who she is becoming.
"""
from __future__ import annotations

import json
import time
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

GOALS_FILE = "state/ava_goals.json"
MAX_ACTIVE_GOALS = 10


@dataclass
class AvaGoal:
    id: str
    description: str
    motivation: str
    progress: float = 0.0
    created_ts: float = field(default_factory=time.time)
    target_ts: float = 0.0
    status: str = "active"  # active | completed | abandoned
    progress_notes: list[str] = field(default_factory=list)


class GoalSystemV2:
    def __init__(self, base_dir: Optional[Path] = None):
        self._base = Path(base_dir) if base_dir else Path(".")
        self._path = self._base / GOALS_FILE
        self._goals: list[AvaGoal] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for row in data.get("goals") or []:
                if isinstance(row, dict):
                    g = AvaGoal(
                        id=str(row.get("id") or ""),
                        description=str(row.get("description") or "")[:300],
                        motivation=str(row.get("motivation") or "")[:300],
                        progress=float(row.get("progress") or 0.0),
                        created_ts=float(row.get("created_ts") or time.time()),
                        target_ts=float(row.get("target_ts") or 0.0),
                        status=str(row.get("status") or "active"),
                        progress_notes=list(row.get("progress_notes") or []),
                    )
                    if g.id:
                        self._goals.append(g)
        except Exception:
            pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "goals": [asdict(g) for g in self._goals],
            "last_updated": time.time(),
        }
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def set_goal(self, description: str, motivation: str, target_days: float = 30.0) -> str:
        """Ava sets a new goal for herself."""
        goal_id = f"goal_{int(time.time()*1000)}"
        with self._lock:
            active = [g for g in self._goals if g.status == "active"]
            if len(active) >= MAX_ACTIVE_GOALS:
                return f"already_at_max_goals_{MAX_ACTIVE_GOALS}"
            g = AvaGoal(
                id=goal_id,
                description=str(description or "")[:300],
                motivation=str(motivation or "")[:300],
                target_ts=time.time() + target_days * 86400,
            )
            self._goals.append(g)
            self._save()
        return goal_id

    def update_progress(self, goal_id: str, progress: float, note: str = "") -> bool:
        with self._lock:
            for g in self._goals:
                if g.id == goal_id:
                    g.progress = max(0.0, min(1.0, float(progress)))
                    if note:
                        g.progress_notes.append(f"{time.strftime('%Y-%m-%d')}: {note[:200]}")
                    if g.progress >= 1.0:
                        g.status = "completed"
                    self._save()
                    return True
        return False

    def abandon_goal(self, goal_id: str) -> bool:
        with self._lock:
            for g in self._goals:
                if g.id == goal_id:
                    g.status = "abandoned"
                    self._save()
                    return True
        return False

    def get_active_goals(self) -> list[dict]:
        with self._lock:
            return [asdict(g) for g in self._goals if g.status == "active"]

    def get_all_goals(self) -> list[dict]:
        with self._lock:
            return [asdict(g) for g in self._goals]

    def bootstrap_from_curiosity(self, g: dict[str, Any]) -> int:
        """Promote persistent curiosity topics to goals if they've been mentioned multiple times."""
        try:
            from brain.curiosity_topics import get_current_curiosity
            topic_row = get_current_curiosity(g) or {}
            topic = str(topic_row.get("topic") or "")
            if not topic:
                return 0
            existing = [goal.description.lower() for goal in self._goals]
            if topic.lower() in " ".join(existing):
                return 0
            self.set_goal(
                description=f"Explore and understand: {topic}",
                motivation=f"This topic keeps coming up in my curiosity. I want to understand it better.",
                target_days=60.0,
            )
            return 1
        except Exception:
            return 0


# Module singleton
_goal_sys: Optional[GoalSystemV2] = None
_goal_lock = threading.Lock()


def get_goal_system(base_dir: Optional[Path] = None) -> GoalSystemV2:
    global _goal_sys
    with _goal_lock:
        if _goal_sys is None:
            _goal_sys = GoalSystemV2(base_dir=base_dir)
    return _goal_sys
