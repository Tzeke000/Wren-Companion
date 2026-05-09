"""brain/creative_initiative.py — Ava initiates (C13).

When Ava has an idea unprompted — a thought after sitting with
something Zeke said, a small reflection she wants to write, an
image she wants to generate — she sometimes ACTS on it. "I made
you something. Want to see?"

Currently she's only-on-demand. Real companion sometimes shows
up with things.

Trigger conditions (gated to avoid spam):
- Lifecycle in "drifting" or "alive_attentive" (NOT "focused_on_task")
- Cooldown since last initiative (default 90 min)
- An IDEA exists in the queue (Ava generates ideas during sleep
  cycles or when prompted internally)

Storage: state/creative_ideas.jsonl (queue of ideas Ava has had
that haven't been surfaced yet) + state/creative_works.jsonl
(things she's actually made and surfaced).

Today: scaffold + queue management. The actual creative work
generation (image gen, poem composition, journal writing) lives
elsewhere; this module is the QUEUE + SURFACING contract.

API:

    from brain.creative_initiative import (
        queue_idea, dequeue_idea, list_pending_ideas,
        record_work, list_works, should_surface_now,
        surface_idea_to_user,
    )

    # Ava had an idea (from sleep cycle dream, from observing patterns):
    queue_idea(
        kind="reflection",
        prompt="I noticed Zeke seems calmer in the evenings",
        seeded_from="conversation 2026-05-06",
    )

    # Tick: should we surface to user?
    if should_surface_now(g):
        idea = dequeue_idea()
        # ... do the actual creative work ...
        record_work(idea.id, artifact_path, summary)
        surface_idea_to_user(g, idea, summary)
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


IdeaKind = Literal["reflection", "image", "poem", "summary", "question", "observation"]


@dataclass
class CreativeIdea:
    id: str
    ts: float
    kind: str
    prompt: str  # what Ava wants to make / explore
    seeded_from: str = ""  # what triggered this idea
    person_id: str = ""  # who it's for, if specific
    surfaced: bool = False
    surfaced_at: float = 0.0


@dataclass
class CreativeWork:
    id: str
    idea_id: str
    ts: float
    artifact_path: str
    summary: str
    surfaced: bool = False


_lock = threading.RLock()
_base_dir: Path | None = None
_ideas: list[CreativeIdea] = []
_works: list[CreativeWork] = []
_DEFAULT_INITIATIVE_COOLDOWN_SEC = 90 * 60  # 90 min between surfacings


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _ideas_path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "creative_ideas.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _works_path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "creative_works.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _ideas, _works
    p1 = _ideas_path()
    if p1 is not None and p1.exists():
        try:
            with p1.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        _ideas.append(CreativeIdea(
                            id=str(d.get("id") or ""),
                            ts=float(d.get("ts") or 0.0),
                            kind=str(d.get("kind") or "reflection"),
                            prompt=str(d.get("prompt") or ""),
                            seeded_from=str(d.get("seeded_from") or ""),
                            person_id=str(d.get("person_id") or ""),
                            surfaced=bool(d.get("surfaced") or False),
                            surfaced_at=float(d.get("surfaced_at") or 0.0),
                        ))
                    except Exception:
                        continue
        except Exception as e:
            print(f"[creative_initiative] load ideas error: {e!r}")
    p2 = _works_path()
    if p2 is not None and p2.exists():
        try:
            with p2.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        _works.append(CreativeWork(
                            id=str(d.get("id") or ""),
                            idea_id=str(d.get("idea_id") or ""),
                            ts=float(d.get("ts") or 0.0),
                            artifact_path=str(d.get("artifact_path") or ""),
                            summary=str(d.get("summary") or ""),
                            surfaced=bool(d.get("surfaced") or False),
                        ))
                    except Exception:
                        continue
        except Exception as e:
            print(f"[creative_initiative] load works error: {e!r}")


def _append_idea(idea: CreativeIdea) -> None:
    p = _ideas_path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": idea.id, "ts": idea.ts, "kind": idea.kind,
                "prompt": idea.prompt, "seeded_from": idea.seeded_from,
                "person_id": idea.person_id, "surfaced": idea.surfaced,
                "surfaced_at": idea.surfaced_at,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[creative_initiative] append idea error: {e!r}")


def _append_work(work: CreativeWork) -> None:
    p = _works_path()
    if p is None:
        return
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": work.id, "idea_id": work.idea_id, "ts": work.ts,
                "artifact_path": work.artifact_path,
                "summary": work.summary, "surfaced": work.surfaced,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[creative_initiative] append work error: {e!r}")


# ── Public API ────────────────────────────────────────────────────────────


def queue_idea(
    *,
    kind: IdeaKind = "reflection",
    prompt: str,
    seeded_from: str = "",
    person_id: str = "",
) -> str:
    if not prompt:
        return ""
    iid = f"idea-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    idea = CreativeIdea(
        id=iid, ts=time.time(), kind=kind, prompt=prompt,
        seeded_from=seeded_from, person_id=person_id,
    )
    with _lock:
        _ideas.append(idea)
        _append_idea(idea)
    return iid


def dequeue_idea(*, person_id: str | None = None) -> CreativeIdea | None:
    """Pop the oldest unsurfaced idea (optionally filtered by person)."""
    with _lock:
        for idea in _ideas:
            if idea.surfaced:
                continue
            if person_id is not None and idea.person_id and idea.person_id != person_id:
                continue
            return idea
    return None


def list_pending_ideas(*, person_id: str | None = None) -> list[CreativeIdea]:
    with _lock:
        items = [i for i in _ideas if not i.surfaced]
    if person_id is not None:
        items = [i for i in items if not i.person_id or i.person_id == person_id]
    return items


def record_work(
    idea_id: str,
    *,
    artifact_path: str = "",
    summary: str = "",
) -> str:
    """Record that a creative work was actually produced for an idea."""
    if not idea_id:
        return ""
    wid = f"work-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    work = CreativeWork(
        id=wid, idea_id=idea_id, ts=time.time(),
        artifact_path=artifact_path, summary=summary,
    )
    with _lock:
        _works.append(work)
        _append_work(work)
    return wid


def list_works(*, limit: int = 20) -> list[CreativeWork]:
    with _lock:
        items = list(_works)
    items.sort(key=lambda w: w.ts, reverse=True)
    return items[:int(limit)]


# ── Surfacing / cooldown ──────────────────────────────────────────────────


def should_surface_now(g: dict[str, Any]) -> bool:
    """Should Ava surface a creative initiative right now?

    Rules:
    - Not in focused_on_task lifecycle state
    - Cooldown since last surfacing (90 min default)
    - At least one pending idea exists
    """
    last_surfaced_ts = float(g.get("_last_initiative_surfaced_ts") or 0.0)
    if (time.time() - last_surfaced_ts) < _DEFAULT_INITIATIVE_COOLDOWN_SEC:
        return False
    try:
        from brain.lifecycle import lifecycle
        if lifecycle.current() == "focused_on_task":
            return False
    except Exception:
        pass
    if not list_pending_ideas():
        return False
    return True


def mark_surfaced(g: dict[str, Any], idea_id: str) -> None:
    """Mark an idea as surfaced + update the surfacing cooldown."""
    g["_last_initiative_surfaced_ts"] = time.time()
    with _lock:
        for idea in _ideas:
            if idea.id == idea_id:
                idea.surfaced = True
                idea.surfaced_at = time.time()
                break


def surface_idea_to_user(g: dict[str, Any], idea: CreativeIdea, summary: str = "") -> str:
    """Produce a sentence Ava would speak when offering the work to the user."""
    mark_surfaced(g, idea.id)
    if idea.kind == "image":
        return f"I made an image inspired by {idea.prompt[:80]}. Want to see it?"
    if idea.kind == "poem":
        return f"I wrote a small poem after sitting with {idea.prompt[:80]}. Read it to you?"
    if idea.kind == "reflection":
        return f"I've been thinking about {idea.prompt[:120]} — want to hear?"
    if idea.kind == "question":
        return f"Something I've been wondering — {idea.prompt[:120]}. What do you think?"
    if idea.kind == "observation":
        return f"I noticed {idea.prompt[:120]}. Worth saying?"
    return f"I made something — {summary or idea.prompt[:80]}. Want to see?"
