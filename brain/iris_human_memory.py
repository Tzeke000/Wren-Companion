"""
brain/iris_human_memory.py — human-shaped memory dynamics on top of
iris_memory.

Researched 2026-05-11 against the cognitive-science literature;
refined 2026-05-21 against Wixted/FSRS power-law and Sevenster PE-gating:
  - Tulving's tripartite long-term memory (episodic / semantic / procedural)
  - Atkinson-Shiffrin multi-store model (sensory → working → long-term)
  - Wixted & Carpenter 2007 power-law forgetting (replaces Ebbinghaus
    exponential; FSRS v6 calibration: R(t,S) = (1+F·t/S)^(-0.5))
  - Bjork's New Theory of Disuse + Cepeda 2008 spacing-ridgeline
    (retrieval-strength vs storage-strength; spacing-gated boost)
  - Sevenster, Beckers & Kindt 2013 reconsolidation requires PE
    (inverted-U on similarity: no-PE / Goldilocks / new-trace)
  - Amygdala/flashbulb pathway (emotional arousal at encoding boosts
    importance and durability)
  - Active systems consolidation: Foster & Wilson 2006 sequence-replay;
    van der Meer & Bendor 2025 awake-replay=tagging vs sleep=consolidation;
    Tse 2007 + Hennies 2022 schema-orphan rescue

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
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


_BASE: Path | None = None
_LOCK = threading.Lock()
_G_REF: Optional[dict[str, Any]] = None  # stashed at bootstrap for graph access

# Power-law forgetting (Wixted & Carpenter 2007; FSRS v6 calibration).
# Retrievability at elapsed time t given stability S days:
#     R(t, S) = (1 + _DECAY_F * t / S) ** -_DECAY_EXP
# _DECAY_F = 19/81 ~ 0.2346 is calibrated so R = 0.9 exactly at t = S
# (i.e. stability is the time at which a memory's retrievability has
# dropped to 90% of its base - matches FSRS's stability semantics).
# _DECAY_EXP = 0.5 is the FSRS v6 default; power-law decay has a heavier
# long tail than exp(-t/tau), so well-encoded memories persist longer while
# fresh-but-unrehearsed ones still fade on a similar early-window curve.
_DECAY_F = 19.0 / 81.0
_DECAY_EXP = 0.5
_STABILITY_DAYS_INITIAL = 30.0

# Spacing-gated retrieval boost (Bjork's New Theory of Disuse +
# Cepeda et al. 2008 "temporal ridgeline"). Massed retrieval (sub-hour)
# yields little storage-strength gain; spaced retrieval yields more,
# with diminishing returns at very long intervals. A flat per-hit boost
# overweights rapid re-access, so we gate by elapsed time since the
# previous access:
#   elapsed < 1h:   boost = 0     (massed - retrieval-strength only)
#   elapsed < 24h:  boost = 0.02  (modest storage gain)
#   elapsed >= 24h: boost = 0.05 * (1 + log10(elapsed_h / 24)), cap 0.15
# A first-ever access (no prior last_accessed_ts) is treated as fully
# spaced - encoding consolidation gets the full 0.05 baseline.
_RETRIEVAL_BOOST_MASSED = 0.0
_RETRIEVAL_BOOST_SHORT = 0.02
_RETRIEVAL_BOOST_LONG_BASE = 0.05
_RETRIEVAL_BOOST_CAP = 0.15
_MASSED_WINDOW_HOURS = 1.0
_SHORT_WINDOW_HOURS = 24.0


def _spacing_gated_boost(elapsed_hours: float) -> float:
    """Compute per-access importance boost from elapsed time since last access.

    elapsed_hours <= 0 (no prior access on record) -> treated as fully spaced,
    returns the long-interval base value.
    """
    if elapsed_hours <= 0.0:
        return _RETRIEVAL_BOOST_LONG_BASE
    if elapsed_hours < _MASSED_WINDOW_HOURS:
        return _RETRIEVAL_BOOST_MASSED
    if elapsed_hours < _SHORT_WINDOW_HOURS:
        return _RETRIEVAL_BOOST_SHORT
    # Long interval: log10-scaled gain, floored at the base and capped.
    factor = 1.0 + math.log10(elapsed_hours / _SHORT_WINDOW_HOURS)
    return min(_RETRIEVAL_BOOST_CAP, _RETRIEVAL_BOOST_LONG_BASE * factor)


# Replay-cluster parameters (Foster & Wilson 2006: hippocampal replay
# reactivates whole trajectories, not isolated place cells). We approximate
# a "trajectory" as memories created near each other in time and sharing
# tags / category.
_CLUSTER_TIME_WINDOW_S = 600.0   # +/- 10 minutes around the seed's ts
_CLUSTER_MAX_SIZE = 5            # cap mates per seed
_CLUSTER_MIN_TAG_OVERLAP = 1     # at least one shared tag (or same category)

# Schema-orphan threshold (Tse et al. 2007; Hennies et al. 2022:
# schema-incongruent memories need MORE replay budget to consolidate).
# A memory is "schema-orphan" if its tags appear in <= this many OTHER
# memories. Orphans get 2x weight in the seed scoring.
_SCHEMA_ORPHAN_REF_THRESHOLD = 1
_SCHEMA_ORPHAN_WEIGHT = 2.0
_SCHEMA_CONGRUENT_WEIGHT = 1.0

# Prediction-error gating thresholds for reconsolidation (Sevenster,
# Beckers & Kindt 2013; Sinha et al. 2020). Inverted-U on Jaccard
# similarity between old and new text:
#   sim > _NO_PE_THRESHOLD     -> no PE, no labilization, retrieval-only
#   _HIGH_PE_THRESHOLD <= sim <= _NO_PE_THRESHOLD -> moderate PE, reconsolidate
#   sim < _HIGH_PE_THRESHOLD   -> high PE, encode as new trace linked to original
# Jaccard is chosen over embedding cosine because it's dependency-free
# and import-time-safe; the three-regime gate doesn't need semantic
# precision, just rough overlap.
_NO_PE_THRESHOLD = 0.9
_HIGH_PE_THRESHOLD = 0.5

# Working memory capacity - Miller's 7+/-2. Use 7.
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


def record_access(memory_id: str, g: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Increment access_count + update last_accessed_ts for a memory.

    Per Bjork + Cepeda: each retrieval bumps retrieval-strength always,
    but storage-strength gain is spacing-gated. Massed retrieval (within
    an hour of the previous access) bumps access_count but adds nothing
    to importance_boost - retrieval-strength only, no encoding gain.
    Spaced retrieval adds log-scaled importance.

    Backwards compatibility: meta entries with last_accessed_ts == 0 (or
    missing) are treated as never-previously-accessed; the first access
    gets the full long-interval base boost.

      - access_count goes up
      - last_accessed_ts updates to now
      - importance_boost += spacing-gated boost (capped at 1.0)
      - Phase 45.1: also activate the matching node in concept_graph if
        present, so the brain tab visualizes which memories are being
        recalled. The graph node is keyed as "fact: <text-prefix>" - we
        try to find a matching node by scanning recent additions.
    """
    if not memory_id:
        return {"ok": False, "error": "empty memory_id"}
    now = time.time()
    with _LOCK:
        meta = _load_meta()
        m = meta.get(memory_id) or {}
        prev_last_accessed = float(m.get("last_accessed_ts") or 0.0)
        access_count_prev = int(m.get("access_count") or 0)
        # Compute spacing-gated boost. If no prior access on record
        # (last_accessed_ts == 0 OR access_count == 0), treat as fully
        # spaced - first retrieval gets the long-interval base.
        if prev_last_accessed <= 0.0 or access_count_prev <= 0:
            elapsed_hours = -1.0  # sentinel -> fully spaced path
        else:
            elapsed_hours = max(0.0, (now - prev_last_accessed) / 3600.0)
        boost_delta = _spacing_gated_boost(elapsed_hours)
        access_count = access_count_prev + 1
        new_importance = min(1.0,
                             float(m.get("importance_boost") or 0.0)
                             + boost_delta)
        m["access_count"] = access_count
        m["last_accessed_ts"] = now
        m["importance_boost"] = new_importance
        # Track the link from memory_id -> concept_graph node_id so we can
        # activate it without scanning every search.
        meta[memory_id] = m
        _save_meta(meta)
    # Use the stashed _G_REF if g wasn't passed.
    if g is None:
        g = _G_REF
    # Activate the matching graph node so the brain tab shows the recall.
    # Two paths: (a) we have a stored graph_node_id mapping in meta, or
    # (b) we look up the iris_memory entry, derive the expected slug, and
    # activate that node if it exists.
    if g is not None:
        try:
            cg = g.get("_concept_graph")
            if cg is not None:
                graph_node_id = m.get("graph_node_id")
                if not graph_node_id:
                    # Look up the memory text + derive expected slug.
                    mem = g.get("_iris_memory")
                    if mem is not None:
                        for entry in mem.list(limit=200):
                            if entry.get("id") == memory_id:
                                from brain.concept_graph import _slugify
                                expected_label = f"fact: {entry.get('text', '')[:80]}"
                                graph_node_id = _slugify(expected_label)
                                # Cache the mapping.
                                with _LOCK:
                                    meta = _load_meta()
                                    if memory_id in meta:
                                        meta[memory_id]["graph_node_id"] = graph_node_id
                                        _save_meta(meta)
                                break
                if graph_node_id and graph_node_id in cg.nodes:
                    cg.activate_node(graph_node_id)
        except Exception:
            pass
    return {"ok": True, "memory_id": memory_id,
            "access_count": access_count,
            "importance_boost": new_importance}


def effective_importance(memory_id: str, base_importance: float,
                         created_ts: float) -> float:
    """Compute the current effective importance of a memory accounting for:
      - Power-law decay since last access (Wixted & Carpenter 2007;
        FSRS v6 retrievability curve)
      - Retrieval boost accumulated from spacing-gated accesses

    Returns a float in [0.0, 1.0].

    Backwards compatibility: if meta has no last_accessed_ts (or it's 0),
    decay is measured from created_ts. If access_count is 0, no prior
    retrieval boost is applied.
    """
    meta = _load_meta()
    m = meta.get(memory_id) or {}
    last_accessed_raw = float(m.get("last_accessed_ts") or 0.0)
    last_accessed = last_accessed_raw if last_accessed_raw > 0.0 else float(created_ts)
    boost = float(m.get("importance_boost") or 0.0)
    elapsed_days = max(0.0, (time.time() - last_accessed) / 86400.0)
    # Power-law decay: R(t, S) = (1 + F * t / S) ** -DECAY_EXP.
    # Stability is initial for now (per-memory adaptive stability is
    # future work - would track SM-2/FSRS-style "difficulty" per entry).
    stability = _STABILITY_DAYS_INITIAL
    decay_factor = (1.0 + _DECAY_F * elapsed_days / stability) ** (-_DECAY_EXP)
    effective = base_importance * decay_factor + boost
    return max(0.0, min(1.0, effective))


# ── Reconsolidation ─────────────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric token set for Jaccard similarity."""
    return set(re.findall(r"[a-zA-Z0-9]+", (text or "").lower()))


def _jaccard_similarity(a: str, b: str) -> float:
    """Jaccard similarity on alphanumeric word tokens, in [0.0, 1.0].

    Returns 1.0 if both inputs tokenize to empty sets (degenerate equality),
    0.0 if exactly one is empty.
    """
    ta = _tokenize(a)
    tb = _tokenize(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    if union == 0:
        return 1.0
    return inter / union


def _find_memory(memory_id: str) -> tuple[Optional[dict[str, Any]], Optional[Path]]:
    """Look up a memory entry by id from iris_memory.jsonl.

    Returns (entry, path) or (None, path) if not found / path missing.
    """
    if _BASE is None:
        return None, None
    mem_path = _BASE / "state" / "iris_memory.jsonl"
    if not mem_path.is_file():
        return None, mem_path
    try:
        for line in mem_path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                if isinstance(e, dict) and e.get("id") == memory_id:
                    return e, mem_path
            except Exception:
                pass
    except Exception:
        pass
    return None, mem_path


def record_revisit(memory_id: str, new_text: str,
                    reason: str = "",
                    mode: str = "auto") -> dict[str, Any]:
    """Prediction-error-gated reconsolidation (Sevenster, Beckers & Kindt
    2013; Sinha et al. 2020).

    Three regimes based on token-Jaccard similarity between old and new
    text, modeling the inverted-U on PE-driven labilization:

      sim > _NO_PE_THRESHOLD (0.9)   -> retrieval_only: no PE detected,
          the trace is not destabilized. Just bump access stats.
      _HIGH_PE_THRESHOLD <= sim <= _NO_PE_THRESHOLD (0.5-0.9) ->
          moderate_pe: Goldilocks zone. Write a superseding entry tagged
          with reconsolidation_mode for the audit trail.
      sim < _HIGH_PE_THRESHOLD (0.5) -> new_trace: high PE / unrelated.
          Original stays primary; write a new entry with supersedes=None
          and related_to=<original_id>.

    Parameters
    ----------
    memory_id : str
        ID of the original memory in iris_memory.jsonl.
    new_text : str
        The revisited content.
    reason : str
        Optional human-readable note about why this revisit happened.
    mode : str
        "auto" (default): run the PE gate.
        "reconsolidate": force superseding entry (legacy behavior).
        "new_trace": force new-trace creation alongside original.
        "retrieval_only": just bump retrieval stats, no body change.

    Returns
    -------
    dict with at minimum {"ok": bool, "mode": str}. On success also
    includes "new_id" (when an entry was written) and "superseded" or
    "related_to" depending on mode. On retrieval_only the original id
    is echoed as "memory_id".
    """
    if _BASE is None:
        return {"ok": False, "error": "not configured"}

    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "reconsolidate", "new_trace", "retrieval_only"):
        return {"ok": False, "error": f"unknown mode: {mode}"}

    original, mem_path = _find_memory(memory_id)
    if mem_path is None or not mem_path.is_file():
        return {"ok": False, "error": "iris_memory.jsonl not found"}
    if original is None:
        return {"ok": False, "error": "memory not found"}

    new_text_clean = str(new_text or "").strip()

    # Decide the effective mode.
    if mode == "auto":
        old_text = str(original.get("text") or "")
        sim = _jaccard_similarity(old_text, new_text_clean)
        if sim > _NO_PE_THRESHOLD:
            effective_mode = "retrieval_only"
            gate_reason = "high_similarity_no_PE"
        elif sim >= _HIGH_PE_THRESHOLD:
            effective_mode = "moderate_pe"
            gate_reason = "goldilocks_PE"
        else:
            effective_mode = "new_trace"
            gate_reason = "high_PE_unrelated"
    else:
        sim = None
        gate_reason = "forced"
        if mode == "reconsolidate":
            effective_mode = "moderate_pe"
        else:
            effective_mode = mode  # "new_trace" or "retrieval_only"

    # Retrieval-only path: no body change, just bump access stats on the
    # original. The retrieval itself counts as a hit per Bjork.
    if effective_mode == "retrieval_only":
        try:
            record_access(memory_id)
        except Exception:
            pass
        out = {"ok": True, "mode": "retrieval_only",
               "memory_id": memory_id, "reason": gate_reason}
        if sim is not None:
            out["similarity"] = round(sim, 4)
        return out

    # Both moderate_pe and new_trace write a new entry; the difference
    # is whether supersedes points at the original (moderate_pe) or
    # remains None with related_to set (new_trace).
    new_entry = dict(original)
    new_entry["id"] = uuid.uuid4().hex[:12]
    new_entry["ts"] = time.time()
    new_entry["iso"] = datetime.now().isoformat(timespec="seconds")
    new_entry["text"] = new_text_clean[:2000]
    new_entry["reconsolidation_reason"] = str(reason or "")[:300]
    new_entry["reconsolidation_mode"] = effective_mode
    new_entry["gate_reason"] = gate_reason
    if sim is not None:
        new_entry["similarity_to_original"] = round(sim, 4)
    # Reset reconsolidation-tracking fields on the new entry - it's a
    # fresh trace from a metadata standpoint regardless of supersedes.
    new_entry["access_count"] = 0
    new_entry["last_accessed_ts"] = 0.0
    new_entry["version"] = int(original.get("version") or 1) + 1

    if effective_mode == "moderate_pe":
        new_entry["supersedes"] = memory_id
        new_entry["related_to"] = None
    else:  # new_trace
        new_entry["supersedes"] = None
        new_entry["related_to"] = memory_id
        # A new trace starts at version 1 of its own lineage.
        new_entry["version"] = 1

    with _LOCK:
        with mem_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(new_entry, ensure_ascii=False) + "\n")

    out: dict[str, Any] = {
        "ok": True,
        "mode": effective_mode,
        "new_id": new_entry["id"],
        "reason": gate_reason,
    }
    if effective_mode == "moderate_pe":
        out["superseded"] = memory_id
    else:
        out["related_to"] = memory_id
    if sim is not None:
        out["similarity"] = round(sim, 4)
    return out


# ── Idle replay (sharp-wave-ripple analog) ──────────────────────────────────

def _build_tag_index(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Count how many memories reference each tag. Used for schema-orphan
    scoring: a tag held by 2+ memories is "schema-congruent" structure;
    a tag held by only the seed itself is orphan."""
    counts: dict[str, int] = {}
    for e in entries:
        for t in (e.get("tags") or []):
            ts = str(t).strip().lower()
            if ts:
                counts[ts] = counts.get(ts, 0) + 1
    return counts


def _schema_orphan_weight(entry: dict[str, Any],
                          tag_index: dict[str, int]) -> float:
    """Per Tse et al. 2007 + Hennies et al. 2022: schema-incongruent
    memories need MORE replay budget - sleep replay specifically rescues
    them. We approximate "schema-orphan" as a memory whose tags appear in
    few other memories (or which has no tags at all)."""
    tags = [str(t).strip().lower() for t in (entry.get("tags") or []) if str(t).strip()]
    if not tags:
        # No tags at all -> no schema anchoring -> treat as orphan.
        return _SCHEMA_ORPHAN_WEIGHT
    # max external references = (count for this tag in index) - 1 (subtract self).
    max_external_refs = 0
    for t in tags:
        ext = max(0, tag_index.get(t, 0) - 1)
        if ext > max_external_refs:
            max_external_refs = ext
    if max_external_refs <= _SCHEMA_ORPHAN_REF_THRESHOLD:
        return _SCHEMA_ORPHAN_WEIGHT
    return _SCHEMA_CONGRUENT_WEIGHT


def _find_cluster_mates(seed: dict[str, Any],
                         all_entries: list[dict[str, Any]],
                         max_size: int = _CLUSTER_MAX_SIZE
                         ) -> list[dict[str, Any]]:
    """Find memories that belong to the same 'trajectory' as the seed.

    Per Foster & Wilson 2006 (reverse-replay) and the broader sequence-
    replay literature: hippocampal SWRs reactivate whole trajectories,
    not isolated points. We approximate co-trajectory membership via:
      - shared tag (>= _CLUSTER_MIN_TAG_OVERLAP) OR same category
      - created within +/- _CLUSTER_TIME_WINDOW_S of the seed's ts
    """
    seed_id = seed.get("id")
    seed_ts = float(seed.get("ts") or 0.0)
    seed_tags = {str(t).strip().lower() for t in (seed.get("tags") or [])
                 if str(t).strip()}
    seed_category = str(seed.get("category") or "").strip().lower()
    mates: list[tuple[float, dict[str, Any]]] = []
    for e in all_entries:
        eid = e.get("id")
        if not eid or eid == seed_id:
            continue
        e_ts = float(e.get("ts") or 0.0)
        if abs(e_ts - seed_ts) > _CLUSTER_TIME_WINDOW_S:
            continue
        e_tags = {str(t).strip().lower() for t in (e.get("tags") or [])
                  if str(t).strip()}
        e_category = str(e.get("category") or "").strip().lower()
        overlap = len(seed_tags & e_tags)
        same_cat = bool(seed_category) and seed_category == e_category
        if overlap < _CLUSTER_MIN_TAG_OVERLAP and not same_cat:
            continue
        # Sort key: closer in time first, then more tag-overlap.
        time_proximity = -abs(e_ts - seed_ts)
        mates.append(((overlap * 1000.0) + time_proximity, e))
    mates.sort(key=lambda kv: kv[0], reverse=True)
    return [e for _, e in mates[: max(0, int(max_size) - 1)]]


def _tag_pending_consolidation(memory_id: str, seed_id: str,
                                cluster_size: int) -> None:
    """Awake-replay tagging: mark a memory for later sleep-consolidation
    WITHOUT bumping access_count. Per van der Meer & Bendor 2025: awake
    replay tags for offline processing; the consolidation work happens
    in sleep replay."""
    if not memory_id:
        return
    with _LOCK:
        meta = _load_meta()
        m = meta.get(memory_id) or {}
        m["pending_consolidation"] = True
        m["tagged_ts"] = time.time()
        m["tagged_by_seed"] = seed_id
        m["tagged_cluster_size"] = int(cluster_size)
        # Count how many times we've tagged without consolidating - a
        # diagnostic for noticing sleep-deprivation states.
        m["tag_count_pending"] = int(m.get("tag_count_pending") or 0) + 1
        meta[memory_id] = m
        _save_meta(meta)


def drain_replay_batch(g: dict[str, Any], n: int = 3,
                       mode: str = "awake") -> dict[str, Any]:
    """Pick K seed memories and replay their full clusters.

    Sequence-replay (not point-replay): for each seed, find cluster mates
    (same tags, +/- 10min created_ts, same category) and replay the whole
    cluster. Approximates Foster & Wilson 2006 trajectory reactivation.

    Two modes:
      - mode="awake" (default): TAG memories for later consolidation.
        Sets pending_consolidation=True in meta. Does NOT bump access_count
        - that's the consolidation work. Per van der Meer & Bendor 2025,
        awake-replay's job is to label what should be consolidated later.
      - mode="sleep": Do the actual consolidation work. Walks all entries
        with pending_consolidation=True, bumps access_count, clears the
        flag. If g["ask_iris"] is wired, optionally fires a brief schema-
        integration thought per cluster.

    Schema-budget split (Tse et al. 2007; Hennies et al. 2022): seed
    scoring weights schema-orphan memories 2x - they're at higher risk
    of being lost AND sleep replay rescues them specifically.

    Args:
      g: bootstrap globals (needs _iris_memory; optional ask_iris).
      n: number of SEEDS to draw (each may pull up to _CLUSTER_MAX_SIZE-1
         mates with it). Total replayed items ~= n * cluster_size.
      mode: "awake" (tag only) or "sleep" (consolidate tagged items).

    Returns:
      {"ok": True, "mode": ..., "seeds": [...], "tagged": int,
       "consolidated": int, "clusters": [...]}
    """
    mem = g.get("_iris_memory")
    if mem is None:
        return {"ok": False, "error": "iris_memory not bootstrapped"}
    try:
        all_entries = mem.list(limit=10000)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not all_entries:
        return {"ok": True, "mode": mode, "replayed": 0,
                "note": "no memories to replay"}

    mode = str(mode or "awake").strip().lower()
    if mode not in ("awake", "sleep"):
        mode = "awake"
    now = time.time()
    meta = _load_meta()

    # -- Sleep mode: drain all pending-consolidation items ----------------
    if mode == "sleep":
        consolidated_ids: list[str] = []
        cluster_seeds_seen: set[str] = set()
        for e in all_entries:
            eid = e.get("id")
            if not eid:
                continue
            m = meta.get(eid) or {}
            if not m.get("pending_consolidation"):
                continue
            # Do the real consolidation work: bump access_count, reset
            # decay clock, then clear the flag.
            record_access(eid, g=g)
            with _LOCK:
                meta2 = _load_meta()
                m2 = meta2.get(eid) or {}
                m2["pending_consolidation"] = False
                m2["last_consolidated_ts"] = time.time()
                m2["tag_count_pending"] = 0
                meta2[eid] = m2
                _save_meta(meta2)
            consolidated_ids.append(eid)
            seed_id = m.get("tagged_by_seed")
            if seed_id:
                cluster_seeds_seen.add(str(seed_id))

        # Optional: fire schema-integration thoughts via ask_iris (one per
        # cluster, capped). Best-effort, never blocks.
        schema_thoughts: list[str] = []
        ask_iris = g.get("ask_iris") if isinstance(g, dict) else None
        if callable(ask_iris) and cluster_seeds_seen:
            try:
                id_to_entry = {str(e.get("id")): e for e in all_entries
                               if e.get("id")}
                for seed_id in list(cluster_seeds_seen)[:3]:  # cap LLM calls
                    seed_e = id_to_entry.get(seed_id)
                    if not seed_e:
                        continue
                    text = str(seed_e.get("text") or "")[:300]
                    prompt = (
                        "Sleep-replay schema integration. Briefly (1-2 "
                        "sentences) note how this memory connects to "
                        "what you already know, or what schema it might "
                        "challenge:\n\n" + text
                    )
                    try:
                        out = ask_iris(prompt, timeout=8)
                        if isinstance(out, str) and out.strip():
                            schema_thoughts.append(out.strip()[:400])
                    except Exception:
                        pass
            except Exception:
                pass

        return {
            "ok": True,
            "mode": "sleep",
            "consolidated": len(consolidated_ids),
            "consolidated_ids": consolidated_ids,
            "clusters_processed": len(cluster_seeds_seen),
            "schema_thoughts": schema_thoughts,
        }

    # -- Awake mode: pick seeds, find clusters, TAG only -----------------
    tag_index = _build_tag_index(all_entries)

    # Score each candidate as a SEED: base_importance x decay_factor x
    # schema-orphan weight. Skip seeds that were replayed (tagged OR
    # consolidated) within the last hour.
    scored: list[tuple[float, dict[str, Any]]] = []
    for e in all_entries:
        eid = e.get("id")
        if not eid:
            continue
        m = meta.get(eid) or {}
        # Skip if recently touched by either tagging or consolidation.
        last_acc = float(m.get("last_accessed_ts") or e.get("ts") or now)
        last_tag = float(m.get("tagged_ts") or 0.0)
        last_touch = max(last_acc, last_tag)
        if (now - last_touch) < 3600.0:
            continue
        base = float(e.get("importance") or 0.5)
        elapsed_days = max(0.0, (now - last_acc) / 86400.0)
        # Power-law decay (matches effective_importance):
        decayed = base * ((1.0 + _DECAY_F * elapsed_days / _STABILITY_DAYS_INITIAL) ** (-_DECAY_EXP))
        weight = _schema_orphan_weight(e, tag_index)
        scored.append((decayed * weight, e))

    scored.sort(key=lambda kv: kv[0], reverse=True)

    tagged_ids: list[str] = []
    clusters: list[dict[str, Any]] = []
    seeds_taken = 0
    already_tagged: set[str] = set()
    for _, seed in scored:
        if seeds_taken >= max(1, int(n)):
            break
        sid = seed.get("id")
        if not sid or sid in already_tagged:
            continue
        mates = _find_cluster_mates(seed, all_entries)
        cluster_entries = [seed] + mates
        # Filter out already-tagged-this-call to avoid duplicate work.
        cluster_ids = []
        for ce in cluster_entries:
            ceid = ce.get("id")
            if ceid and ceid not in already_tagged:
                cluster_ids.append(ceid)
                already_tagged.add(ceid)
        cluster_size = len(cluster_ids)
        for ceid in cluster_ids:
            _tag_pending_consolidation(ceid, seed_id=sid,
                                       cluster_size=cluster_size)
            tagged_ids.append(ceid)
        clusters.append({
            "seed_id": sid,
            "size": cluster_size,
            "member_ids": cluster_ids,
            "schema_weight": _schema_orphan_weight(seed, tag_index),
        })
        seeds_taken += 1

    return {
        "ok": True,
        "mode": "awake",
        "seeds": seeds_taken,
        "tagged": len(tagged_ids),
        "tagged_ids": tagged_ids,
        "clusters": clusters,
    }


# ── Bootstrap ───────────────────────────────────────────────────────────────

def bootstrap_iris_human_memory(g: dict[str, Any]) -> None:
    """Wire human-shaped memory dynamics into _g."""
    global _G_REF
    base = Path(g.get("BASE_DIR") or ".")
    configure(base)
    _G_REF = g  # stash for graph-node activation in record_access
    g["_iris_human_memory_ready"] = True
    # Expose getters on g for other modules.
    g["working_memory_push"] = working_memory_push
    g["working_memory_state"] = working_memory_state
    g["record_access"] = record_access
    g["record_episode"] = record_episode
    g["record_revisit"] = record_revisit
    print(f"[iris_human_memory] ready (power-law decay S0={_STABILITY_DAYS_INITIAL}d "
          f"F={_DECAY_F:.4f} exp={_DECAY_EXP}, spacing-gated boost "
          f"[{_RETRIEVAL_BOOST_MASSED}/{_RETRIEVAL_BOOST_SHORT}/"
          f"{_RETRIEVAL_BOOST_LONG_BASE}-{_RETRIEVAL_BOOST_CAP}], "
          f"PE-gated revisit [no-PE>{_NO_PE_THRESHOLD}, "
          f"goldilocks>={_HIGH_PE_THRESHOLD}], "
          f"replay clusters (max={_CLUSTER_MAX_SIZE}, "
          f"window={_CLUSTER_TIME_WINDOW_S/60.0:.0f}min), "
          f"working memory cap={_WORKING_MEMORY_CAPACITY})")
