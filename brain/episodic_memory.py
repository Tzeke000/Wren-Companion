"""
Phase 64 — Persistent episodic memory with emotional context.

Each episode stores what happened AND what Ava felt.
Bootstrap: Ava scores her own memorability (emotional intensity × novelty × interest).
She controls the fidelity of her own memory.
"""
from __future__ import annotations

import json
import time
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

MAX_EPISODES = 1000
EPISODES_FILE = "state/episodes.jsonl"


@dataclass
class Episode:
    id: str
    timestamp: float
    topic: str
    summary: str
    emotional_context: str
    importance: float
    people_present: list[str] = field(default_factory=list)
    novelty: float = 0.5
    interest: float = 0.5
    memorability: float = 0.5


class EpisodicMemory:
    def __init__(self, base_dir: Optional[Path] = None):
        self._base = Path(base_dir) if base_dir else Path(".")
        self._path = self._base / EPISODES_FILE
        self._episodes: list[Episode] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if isinstance(d, dict):
                        ep = Episode(
                            id=str(d.get("id") or f"ep_{int(time.time()*1000)}"),
                            timestamp=float(d.get("timestamp") or time.time()),
                            topic=str(d.get("topic") or "")[:120],
                            summary=str(d.get("summary") or "")[:600],
                            emotional_context=str(d.get("emotional_context") or "")[:200],
                            importance=float(d.get("importance") or 0.5),
                            people_present=list(d.get("people_present") or []),
                            novelty=float(d.get("novelty") or 0.5),
                            interest=float(d.get("interest") or 0.5),
                            memorability=float(d.get("memorability") or 0.5),
                        )
                        self._episodes.append(ep)
                except Exception:
                    continue
        except Exception:
            pass

    def _save_append(self, ep: Episode) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(ep), ensure_ascii=False) + "\n")

    def _score_memorability(self, importance: float, novelty: float, emotional_context: str) -> float:
        emotional_intensity = 0.5
        if any(w in emotional_context.lower() for w in ("joy", "anger", "fear", "love", "surprise", "grief", "pride")):
            emotional_intensity = 0.85
        elif any(w in emotional_context.lower() for w in ("curious", "interested", "amused", "satisfied")):
            emotional_intensity = 0.65
        return min(1.0, importance * 0.4 + novelty * 0.3 + emotional_intensity * 0.3)

    def store_episode(
        self,
        topic: str,
        summary: str,
        emotional_context: str,
        importance: float = 0.5,
        people_present: Optional[list[str]] = None,
        novelty: float = 0.5,
    ) -> str:
        memorability = self._score_memorability(importance, novelty, emotional_context)
        if memorability < 0.25:
            return ""  # Ava decides this isn't worth remembering

        ep_id = f"ep_{int(time.time()*1000)}"
        ep = Episode(
            id=ep_id,
            timestamp=time.time(),
            topic=str(topic or "")[:120],
            summary=str(summary or "")[:600],
            emotional_context=str(emotional_context or "")[:200],
            importance=float(importance),
            people_present=list(people_present or []),
            novelty=float(novelty),
            interest=0.6,
            memorability=memorability,
        )

        with self._lock:
            self._episodes.append(ep)
            if len(self._episodes) > MAX_EPISODES:
                # Remove lowest memorability when over limit
                self._episodes.sort(key=lambda e: e.memorability)
                self._episodes = self._episodes[-MAX_EPISODES:]
            self._save_append(ep)

        return ep_id

    def search_episodes(self, query: str, limit: int = 3) -> list[dict]:
        query_low = query.lower()
        results = []
        with self._lock:
            for ep in sorted(self._episodes, key=lambda e: e.timestamp, reverse=True):
                score = 0.0
                for word in query_low.split()[:8]:
                    if word in ep.topic.lower() or word in ep.summary.lower():
                        score += 1.0
                if score > 0:
                    row = asdict(ep)
                    row["search_score"] = score
                    results.append(row)
        results.sort(key=lambda r: r["search_score"], reverse=True)
        return results[:limit]

    def get_emotional_context(self, topic: str) -> str:
        topic_low = topic.lower()
        contexts = []
        with self._lock:
            for ep in self._episodes:
                if topic_low in ep.topic.lower() and ep.emotional_context:
                    contexts.append(ep.emotional_context)
        if not contexts:
            return ""
        return contexts[-1]  # Most recent emotional context for topic

    def get_episodes_with_person(self, person_id: str, limit: int = 10) -> list[dict]:
        pid_low = person_id.lower()
        with self._lock:
            matches = [
                asdict(ep) for ep in self._episodes
                if any(pid_low in str(p).lower() for p in ep.people_present)
            ]
        return sorted(matches, key=lambda e: e["timestamp"], reverse=True)[:limit]

    def get_recent(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in sorted(self._episodes, key=lambda e: e.timestamp, reverse=True)[:limit]]


# Module singleton
_episodic_mem: Optional[EpisodicMemory] = None
_em_lock = threading.Lock()


def get_episodic_memory(base_dir: Optional[Path] = None) -> EpisodicMemory:
    global _episodic_mem
    with _em_lock:
        if _episodic_mem is None:
            _episodic_mem = EpisodicMemory(base_dir=base_dir)
    return _episodic_mem
