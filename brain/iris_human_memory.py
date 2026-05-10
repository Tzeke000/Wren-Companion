"""
brain/iris_human_memory.py — human-shaped memory dynamics on top of
iris_memory.

Researched 2026-05-11 against the cognitive-science literature:
  - Tulving's tripartite long-term memory (episodic / semantic / procedural)
  - Atkinson-Shiffrin multi-store model (sensory → working → long-term)
  - Ebbinghaus forgetting curve (exponential decay without rehearsal)
  - Bjork's New Theory of Disuse (retrieval strength vs storage strength,
    each successful recall bumps both)
  - Reconsolidation theory (retrieval makes memory briefly labile;
    re-stabilization can integrate updates)
  - Amygdala/flashbulb pathway (emotional arousal at encoding boosts
    importance and durability)
  - Active systems consolidation (hippocampal replay → cortical integration
    during sleep AND quiet wake)

What this module adds (on top of iris_memory + iris_semantic_memory):

  1. Working memory ring buffer — the ~7 items "currently in mind" (Miller
     1956). Updated when attention shifts; cleared on long quiet.

  2. Episodic vs semantic split — iris_memory.jsonl keeps semantic facts;
     state/iris_episodes.jsonl gets time-indexed events with mood +
     context. Anchor moments are the most-vivid subset.

  3. Forgetting curve on importance — each memory has last_accessed_ts +
     access_count. effective_importance() = base * exp(-elapsed_days/τ) *
     (1 + retrieval_boost). Reads from semantic_search.

  4. Retrieval strengthening — every search hit bumps the entry's
     access_count and updates last_accessed_ts. Highly-retrieved memories
     stay durable; never-retrieved ones fade.

  5. Encoding salience — when a memory is created during high mood
     arousal (frustration/joy/awe), importance gets multiplied. Matches
     the amygdala's flashbulb pathway.

  6. Reconsolidation hook — record_revisit(id, new_text) writes a
     reconsolidated version that supersedes the old. Both stay in the
     log (audit trail) but search returns the latest.

  7. Idle replay — drain_replay_batch() picks N high-importance memories
     and "replays" them by bumping access_count + last_accessed_ts,
     mimicking awake hippocampal replay during quiet moments. Called
     from inner_monologue tick.

State files:
  state/iris_memory.jsonl — semantic facts (existing)
  state/iris_episodes.jsonl — episodic events (NEW)
  state/iris_working_memory.json — current ~7-item buffer (NEW)
  state/iris_memory_meta.json — per-id last_accessed + access_count (NEW)
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


_BASE: Path | None = None
_LOCK = threading.Lock()

# Ebbinghaus-style decay constant: τ days = half-life of base importance
# without rehearsal. 30 days means a memory's importance drops to
# 0.5x after 30 days, 0.25x after 60, etc. Retrieval bumps reset this.
_DECAY_TAU_DAYS = 30.0

# Retrieval strengthens both retrieval-strength (immediate access) and
# storage-strength (long-term durability). Per Bjork: more spacing between
# retrievals = stronger encoding. We approximate: each retrieval bumps
# importance by +0.05 (capped at 1.0).
_RETRIEVAL_BOOST_PER_HIT = 0.05

# Working memory capacity — Miller's 7±2. Use 7.
_WORKING_MEMORY_CAPACITY = 7

# How long an item persists in working memory without re-attention
# before it gets evicted.
_WORKING_MEMORY_TTL_S = 300.0  # 5 min


# ── Configuration ───────────────────────────────────────────────────────────

def configure(base_dir: Path | str) -> None:
    global _BASE
    _BASE = Path(base_dir)
    (_BASE / "state").mkdir(parents=True, exist_ok=True)


def _episodes_path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "iris_episodes.jsonl"


def _working_memory_path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "iris_working_memory.json"


def _meta_path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "iris_memory_meta.json"


# ── Episodic memory ─────────────────────────────────────────────────────────

def record_episode(
    summary: str,
    person_id: str = "zeke",
    mood_label: str = "",
    mood_arousal: float = 0.0,
    mood_valence: float = 0.0,
    context: Optional[dict] = None,
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Record an episodic memory — a specific event with time, place, mood.

    These are autobiographical: "Zeke went to sleep at 1am 2026-05-10
    after directing me to personalize the harness." Different from
    semantic facts ("Zeke prefers React") which iris_memory holds.

    The mood at encoding boosts importance — flashbulb pathway. High
    arousal events get importance up to 1.0; calm baseline events get
    around 0.4-0.5.
    """
    summary = str(summary or "").strip()
    if not summary:
        raise ValueError("episode summary is empty")
    # Encoding-time importance — base 0.5, boosted by mood arousal.
    # Per amygdala/flashbulb theory: arousal (not just valence) drives
    # encoding strength. A frustrated moment encodes as deeply as a
    # joyful one — the substrate cares about *intensity*, not goodness.
    base_importance = 0.5
    arousal_boost = max(0.0, min(0.5, mood_arousal * 0.5))
    importance = min(1.0, base_importance + arousal_boost)

    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "iso": datetime.now().isoformat(timespec="seconds"),
        "type": "episode",
        "summary": summary[:500],
        "person_id": str(person_id or "zeke"),
        "mood_label_at_encoding": str(mood_label or ""),
        "mood_arousal_at_encoding": round(float(mood_arousal), 3),
        "mood_valence_at_encoding": round(float(mood_valence), 3),
        "importance": round(importance, 3),
        "context": dict(context or {}),
        "tags": list(tags or []),
        # Reconsolidation-tracking fields (start at zeros).
        "access_count": 0,
        "last_accessed_ts": 0.0,
        "version": 1,
        "supersedes": None,
    }
    with _LOCK:
        p = _episodes_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def recent_episodes(limit: int = 20, person_id: Optional[str] = None) -> list[dict[str, Any]]:
    p = _episodes_path()
    if not p.is_file():
        return []
    with _LOCK:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        if person_id and e.get("person_id") != person_id:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


# ── Working memory (the ~7-item attention buffer) ───────────────────────────

def working_memory_state() -> dict[str, Any]:
    """Read the current working-memory buffer. Items past TTL are filtered."""
    p = _working_memory_path()
    if not p.is_file():
        return {"items": [], "updated_ts": 0.0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"items": [], "updated_ts": 0.0}
        items = data.get("items") or []
        now = time.time()
        # Filter expired.
        fresh = [
            i for i in items
            if isinstance(i, dict)
            and (now - float(i.get("ts") or 0)) < _WORKING_MEMORY_TTL_S
        ]
        return {"items": fresh[:_WORKING_MEMORY_CAPACITY],
                "updated_ts": float(data.get("updated_ts") or 0)}
    except Exception:
        return {"items": [], "updated_ts": 0.0}


def working_memory_push(item: dict[str, Any]) -> None:
    """Push a new attention-target into working memory. Most-recent-first.
    Capacity limit ejects the oldest. TTL on read filters stale ones.

    Item shape: {"kind": "user_msg|inner_thought|signal|fact|...",
                 "content": str, "meta": dict}
    """
    if not isinstance(item, dict):
        return
    item = dict(item)
    item.setdefault("ts", time.time())
    item.setdefault("id", uuid.uuid4().hex[:8])
    with _LOCK:
        state = working_memory_state()
        items = state.get("items") or []
        items.insert(0, item)
        items = items[:_WORKING_MEMORY_CAPACITY]
        state = {"items": items, "updated_ts": time.time()}
        p = _working_memory_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)


def working_memory_clear() -> None:
    """Empty the buffer — explicit attention shift / sleep transition."""
    with _LOCK:
        p = _working_memory_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"items": [], "updated_ts": time.time()},
                                indent=2), encoding="utf-8")


# ── Memory metadata (access_count + last_accessed_ts) ───────────────────────

def _load_meta() -> dict[str, dict[str, Any]]:
    p = _meta_path()
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_meta(meta: dict[str, dict[str, Any]]) -> None:
    p = _meta_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)


def record_access(memory_id: str) -> dict[str, Any]:
    """Increment access_count + update last_accessed_ts for a memory.

    Per Bjork: each retrieval bumps both retrieval-strength and storage-
    strength. We model this by:
      - access_count goes up
      - last_accessed_ts updates (reset the decay clock)
      - importance gets nudged up (+_RETRIEVAL_BOOST_PER_HIT, capped 1.0)
    """
    if not memory_id:
        return {"ok": False, "error": "empty memory_id"}
    with _LOCK:
        meta = _load_meta()
        m = meta.get(memory_id) or {}
        access_count = int(m.get("access_count") or 0) + 1
        new_importance = min(1.0,
                             float(m.get("importance_boost") or 0.0)
                             + _RETRIEVAL_BOOST_PER_HIT)
        m["access_count"] = access_count
        m["last_accessed_ts"] = time.time()
        m["importance_boost"] = new_importance
        meta[memory_id] = m
        _save_meta(meta)
    return {"ok": True, "memory_id": memory_id,
            "access_count": access_count,
            "importance_boost": new_importance}


def effective_importance(memory_id: str, base_importance: float,
                         created_ts: float) -> float:
    """Compute the current effective importance of a memory accounting for:
      - Ebbinghaus decay since last access (or creation if never accessed)
      - Retrieval boost from access_count

    Returns a float in [0.0, 1.0].
    """
    meta = _load_meta()
    m = meta.get(memory_id) or {}
    last_accessed = float(m.get("last_accessed_ts") or created_ts)
    boost = float(m.get("importance_boost") or 0.0)
    elapsed_days = max(0.0, (time.time() - last_accessed) / 86400.0)
    # Exponential decay with τ.
    decay_factor = math.exp(-elapsed_days / _DECAY_TAU_DAYS)
    effective = base_importance * decay_factor + boost
    return max(0.0, min(1.0, effective))


# ── Reconsolidation ─────────────────────────────────────────────────────────

def record_revisit(memory_id: str, new_text: str,
                    reason: str = "") -> dict[str, Any]:
    """Reconsolidate a memory — write a new version that supersedes the
    old one. Both stay in the log for audit but search returns the latest.

    Per reconsolidation theory: retrieval makes a memory briefly labile;
    re-stabilization can integrate updates without losing the original.
    """
    if _BASE is None:
        return {"ok": False, "error": "not configured"}
    base = _BASE
    # Search iris_memory.jsonl for the original.
    mem_path = base / "state" / "iris_memory.jsonl"
    if not mem_path.is_file():
        return {"ok": False, "error": "iris_memory.jsonl not found"}
    original = None
    try:
        for line in mem_path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                if isinstance(e, dict) and e.get("id") == memory_id:
                    original = e
                    break
            except Exception:
                pass
    except Exception:
        pass
    if original is None:
        return {"ok": False, "error": "memory not found"}
    # Append the reconsolidated version.
    new_entry = dict(original)
    new_entry["id"] = uuid.uuid4().hex[:12]
    new_entry["ts"] = time.time()
    new_entry["iso"] = datetime.now().isoformat(timespec="seconds")
    new_entry["text"] = str(new_text or "").strip()[:2000]
    new_entry["supersedes"] = memory_id
    new_entry["reconsolidation_reason"] = str(reason or "")[:300]
    with _LOCK:
        with mem_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(new_entry, ensure_ascii=False) + "\n")
    return {"ok": True, "new_id": new_entry["id"], "superseded": memory_id}


# ── Idle replay (sharp-wave-ripple analog) ──────────────────────────────────

def drain_replay_batch(g: dict[str, Any], n: int = 3) -> dict[str, Any]:
    """Pick a few important memories and "replay" them — bump access_count
    + last_accessed_ts.

    Per active systems consolidation theory: hippocampal sharp-wave
    ripples during quiet wake (and sleep) replay recent memories,
    strengthening their cortical integration. We don't have a hippocampus,
    but we can simulate the dynamic by periodically "touching" a small
    sample of high-importance memories so their decay clock resets and
    they stay durable.

    Selection strategy: highest base_importance × decay_factor that
    haven't been replayed in the last hour.

    Called from iris_inner_monologue tick (free CC turn for the cost).
    """
    mem = g.get("_iris_memory")
    if mem is None:
        return {"ok": False, "error": "iris_memory not bootstrapped"}
    try:
        all_entries = mem.list(limit=10000)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not all_entries:
        return {"ok": True, "replayed": 0, "note": "no memories to replay"}

    now = time.time()
    meta = _load_meta()

    # Score each: base_importance with decay, minus penalty if replayed
    # very recently (within 1h).
    scored: list[tuple[float, dict[str, Any]]] = []
    for e in all_entries:
        eid = e.get("id")
        if not eid:
            continue
        base = float(e.get("importance") or 0.5)
        m = meta.get(eid) or {}
        last_acc = float(m.get("last_accessed_ts") or e.get("ts") or now)
        if (now - last_acc) < 3600.0:
            continue  # replayed recently, skip
        elapsed_days = max(0.0, (now - last_acc) / 86400.0)
        decayed = base * math.exp(-elapsed_days / _DECAY_TAU_DAYS)
        scored.append((decayed, e))

    scored.sort(key=lambda kv: kv[0], reverse=True)
    replayed = 0
    replayed_ids: list[str] = []
    for _, e in scored[:max(1, int(n))]:
        eid = e.get("id")
        if eid:
            record_access(eid)
            replayed_ids.append(eid)
            replayed += 1
    return {"ok": True, "replayed": replayed, "ids": replayed_ids}


# ── Bootstrap ───────────────────────────────────────────────────────────────

def bootstrap_iris_human_memory(g: dict[str, Any]) -> None:
    """Wire human-shaped memory dynamics into _g."""
    base = Path(g.get("BASE_DIR") or ".")
    configure(base)
    g["_iris_human_memory_ready"] = True
    # Expose getters on g for other modules.
    g["working_memory_push"] = working_memory_push
    g["working_memory_state"] = working_memory_state
    g["record_access"] = record_access
    g["record_episode"] = record_episode
    g["record_revisit"] = record_revisit
    print(f"[iris_human_memory] ready (decay τ={_DECAY_TAU_DAYS}d, "
          f"retrieval boost={_RETRIEVAL_BOOST_PER_HIT}, "
          f"working memory cap={_WORKING_MEMORY_CAPACITY})")
