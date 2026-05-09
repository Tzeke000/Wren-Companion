from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

# Process-level lock so concurrent threads never race on the same .tmp file
_SAVE_LOCK = threading.Lock()

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

NodeType = Literal["person", "topic", "emotion", "memory", "opinion", "curiosity", "self", "event"]

TYPE_COLORS: dict[str, str] = {
    "person": "#ed64a6",
    "topic": "#4299e1",
    "emotion": "#ff8a65",
    "memory": "#9f7aea",
    "opinion": "#ecc94b",
    "curiosity": "#00d4d4",
    "self": "#f5c518",
    "event": "#68d391",
}


@dataclass
class ConceptNode:
    id: str
    label: str
    type: NodeType
    weight: float
    last_activated: float
    activation_count: int
    color: str
    notes: str
    archived: bool = False
    # Memory-rewrite fields (see docs/MEMORY_REWRITE_PLAN.md). The level
    # is 1-10; 10 = highest priority (recently load-bearing), 1 =
    # nearly forgotten. Decay walks levels down over time per the
    # threshold table; reflection scoring (Phase 2 step 4+) walks
    # them up. archive_streak counts consecutive load-bearing turns
    # — at 3 streak hits, archived flips True and the node becomes
    # immune to gone-forever delete at level 0.
    level: int = 5
    archive_streak: int = 0
    archived_at: float = 0.0


@dataclass
class ConceptEdge:
    source: str
    target: str
    relationship: str
    strength: float
    last_fired: float


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return text or f"concept-{int(time.time() * 1000)}"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


class ConceptGraph:
    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "state" / "concept_graph.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.nodes: dict[str, ConceptNode] = {}
        self.edges: list[ConceptEdge] = []
        self.last_updated: float = time.time()
        self.last_bootstrap: float = 0.0
        self.version: int = 1
        # Save throttle — when concept_graph.json is locked by another
        # process (antivirus / OneDrive sync / file explorer preview) the
        # tmp-replace step fails with WinError 5/32. Without backoff, every
        # subsequent add_node call retries immediately and floods stderr.
        # Exponential backoff: 1, 2, 4, 8, 16, 32, capped at 60 seconds.
        self._save_backoff_until: float = 0.0
        self._save_consecutive_failures: int = 0
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
            self.version = int(payload.get("version", 1) or 1)
            self.last_updated = float(payload.get("last_updated") or time.time())
            self.last_bootstrap = float(payload.get("last_bootstrap") or 0.0)
            for row in list(payload.get("nodes") or []):
                if not isinstance(row, dict):
                    continue
                try:
                    # New memory-rewrite fields default per docs/MEMORY_REWRITE_PLAN.md
                    # so old concept_graph.json files load cleanly without rewrite.
                    _level_raw = row.get("level")
                    if _level_raw is None:
                        _level = 5
                    else:
                        try:
                            _level = max(1, min(10, int(_level_raw)))
                        except (TypeError, ValueError):
                            _level = 5
                    node = ConceptNode(
                        id=str(row.get("id") or ""),
                        label=str(row.get("label") or ""),
                        type=str(row.get("type") or "topic"),  # type: ignore[assignment]
                        weight=_clamp(float(row.get("weight") or 0.35), 0.0, 1.0),
                        last_activated=float(row.get("last_activated") or 0.0),
                        activation_count=int(row.get("activation_count") or 0),
                        color=str(row.get("color") or TYPE_COLORS.get(str(row.get("type") or "topic"), "#4299e1")),
                        notes=str(row.get("notes") or ""),
                        archived=bool(row.get("archived", False)),
                        level=_level,
                        archive_streak=int(row.get("archive_streak") or 0),
                        archived_at=float(row.get("archived_at") or 0.0),
                    )
                    if node.id:
                        self.nodes[node.id] = node
                except Exception:
                    continue
            for row in list(payload.get("edges") or []):
                if not isinstance(row, dict):
                    continue
                try:
                    edge = ConceptEdge(
                        source=str(row.get("source") or ""),
                        target=str(row.get("target") or ""),
                        relationship=str(row.get("relationship") or "related_to"),
                        strength=_clamp(float(row.get("strength") or 0.4), 0.0, 1.0),
                        last_fired=float(row.get("last_fired") or 0.0),
                    )
                    if edge.source and edge.target:
                        self.edges.append(edge)
                except Exception:
                    continue
        except Exception:
            return

    def _save(self) -> None:
        # Honor backoff window — if a recent save hit a lock conflict, skip
        # silently until the backoff_until timestamp passes. This stops tight
        # add_node/_save loops from retrying every 50ms while concept_graph.json
        # is locked by an external process (antivirus / OneDrive / preview).
        now = time.time()
        if self._save_backoff_until > now:
            return
        self.last_updated = now
        payload = {
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [asdict(e) for e in self.edges],
            "last_updated": self.last_updated,
            "last_bootstrap": self.last_bootstrap,
            "version": self.version,
        }
        tmp = self.path.with_suffix(".json.tmp")

        def _bump_backoff(reason: str) -> None:
            # 1, 2, 4, 8, 16, 32, then capped at 60 seconds.
            self._save_consecutive_failures += 1
            delay = min(60.0, 2.0 ** max(0, self._save_consecutive_failures - 1))
            self._save_backoff_until = time.time() + delay
            # Only print on the first failure of a streak so logs stay quiet
            # when the lock is held for minutes.
            if self._save_consecutive_failures == 1:
                print(f"[concept_graph] save throttled ({reason}) — backoff {delay:.0f}s")

        # All file operations must happen under the lock to prevent TOCTOU races
        # between concurrent threads in the same process.
        with _SAVE_LOCK:
            # Ensure state/ directory exists — it may have been missing at startup
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as _mkdir_e:
                print(f"[concept_graph] mkdir failed for {self.path.parent}: {_mkdir_e!r}")
                _bump_backoff("mkdir_failed")
                return
            # Stale .tmp from previous crashed instance — try to clear it
            if tmp.is_file():
                try:
                    tmp.unlink()
                except OSError:
                    # Still locked by another process — skip this save
                    _bump_backoff("tmp_locked")
                    return
            try:
                tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.path)
                # Successful save — reset backoff state.
                self._save_consecutive_failures = 0
                self._save_backoff_until = 0.0
            except OSError as _e:
                _winerr = getattr(_e, "winerror", None)
                if _winerr in (5, 32):
                    # Access denied or file locked — discard tmp silently and skip
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
                    _bump_backoff(f"winerror_{_winerr}")
                else:
                    # Unexpected OSError — log full paths to aid diagnosis
                    print(f"[concept_graph] save failed path={self.path} tmp={tmp}: {_e!r}")
                    _bump_backoff("oserror")
            except Exception as _e2:
                print(f"[concept_graph] save unexpected error path={self.path}: {_e2!r}")
                _bump_backoff("unexpected")

    def add_node(self, label: str, type: NodeType, notes: str = "") -> str:
        node_id = _slugify(label)
        if node_id in self.nodes:
            node = self.nodes[node_id]
            node.notes = notes or node.notes
            self.activate_node(node_id)
            self._save()
            return node_id
        node = ConceptNode(
            id=node_id,
            label=str(label or "").strip()[:120] or node_id,
            type=type,
            weight=0.4,
            last_activated=time.time(),
            activation_count=1,
            color=TYPE_COLORS.get(type, "#4299e1"),
            notes=str(notes or "")[:500],
        )
        self.nodes[node_id] = node
        self._save()
        return node_id

    def find_or_create(self, label: str, type: NodeType) -> str:
        slug = _slugify(label)
        if slug in self.nodes:
            self.nodes[slug].archived = False
            return slug
        return self.add_node(label, type)

    def add_edge(self, source_id: str, target_id: str, relationship: str, strength: float) -> None:
        if not source_id or not target_id or source_id not in self.nodes or target_id not in self.nodes:
            return
        rel = str(relationship or "related_to")[:64]
        strength = _clamp(strength, 0.0, 1.0)
        now = time.time()
        for edge in self.edges:
            if edge.source == source_id and edge.target == target_id and edge.relationship == rel:
                edge.strength = _clamp((edge.strength * 0.7) + (strength * 0.3), 0.0, 1.0)
                edge.last_fired = now
                self._save()
                return
        self.edges.append(
            ConceptEdge(
                source=source_id,
                target=target_id,
                relationship=rel,
                strength=strength,
                last_fired=now,
            )
        )
        self._save()

    def activate_node(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        node.archived = False
        node.last_activated = time.time()
        node.activation_count += 1
        node.weight = _clamp(node.weight + 0.04, 0.0, 1.0)
        self._save()

    def activate_path(self, node_ids: list[str]) -> None:
        now = time.time()
        for idx, node_id in enumerate(node_ids):
            self.activate_node(node_id)
            if idx == 0:
                continue
            prev = node_ids[idx - 1]
            for edge in self.edges:
                if edge.source == prev and edge.target == node_id:
                    edge.last_fired = now
                    edge.strength = _clamp(edge.strength + 0.02, 0.0, 1.0)
                    break
        self._save()

    def get_active_nodes(self, last_n_seconds: int = 30) -> list[dict[str, Any]]:
        cutoff = time.time() - max(1, int(last_n_seconds or 30))
        out = [asdict(node) for node in self.nodes.values() if float(node.last_activated or 0.0) >= cutoff]
        out.sort(key=lambda n: float(n.get("last_activated") or 0.0), reverse=True)
        return out

    def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        seen: set[str] = set()
        for edge in self.edges:
            if edge.source == node_id:
                seen.add(edge.target)
            elif edge.target == node_id:
                seen.add(edge.source)
        return [asdict(self.nodes[nid]) for nid in seen if nid in self.nodes]

    def prune_old_nodes(self, max_nodes: int = 500) -> int:
        max_nodes = max(50, int(max_nodes or 500))
        if len(self.nodes) <= max_nodes:
            return 0
        ordered = sorted(
            self.nodes.values(),
            key=lambda n: (n.activation_count, n.last_activated, n.weight),
        )
        removed_ids = {n.id for n in ordered[: max(0, len(self.nodes) - max_nodes)]}
        for nid in removed_ids:
            self.nodes.pop(nid, None)
        self.edges = [e for e in self.edges if e.source not in removed_ids and e.target not in removed_ids]
        self._save()
        return len(removed_ids)

    def get_graph_data(self) -> dict[str, Any]:
        nodes = [asdict(n) for n in self.nodes.values()]
        edges = [asdict(e) for e in self.edges]
        nodes_by_type = {k: 0 for k in TYPE_COLORS}
        for node in nodes:
            t = str(node.get("type") or "topic")
            nodes_by_type[t] = nodes_by_type.get(t, 0) + 1
        most_activated = ""
        if nodes:
            picked = max(nodes, key=lambda n: (int(n.get("activation_count") or 0), float(n.get("weight") or 0.0)))
            most_activated = str(picked.get("label") or "")
        return {
            "nodes": nodes,
            "edges": edges,
            "last_updated": self.last_updated,
            "last_bootstrap": self.last_bootstrap,
            "version": self.version,
            "stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "active_nodes_30s": len(self.get_active_nodes(30)),
                "nodes_by_type": nodes_by_type,
                "most_activated": most_activated,
                "last_bootstrap": self.last_bootstrap,
            },
        }

    def bootstrap_from_existing_memory(self, host: dict[str, Any] | None = None) -> dict[str, Any]:
        return bootstrap_from_existing_memory(self, host=host)

    def get_related_concepts(self, topic: str, max_hops: int = 2) -> list[dict[str, Any]]:
        start_id = _slugify(topic)
        if start_id not in self.nodes:
            for nid, node in self.nodes.items():
                if node.label.strip().lower() == (topic or "").strip().lower():
                    start_id = nid
                    break
        if start_id not in self.nodes:
            return []
        max_hops = max(1, int(max_hops or 2))
        visited: set[str] = {start_id}
        queue: list[tuple[str, int, float]] = [(start_id, 0, 1.0)]
        scored: dict[str, float] = {}
        # Track best relationship label per target
        best_rel: dict[str, str] = {}
        best_via: dict[str, str] = {}
        while queue:
            current, hop, path_strength = queue.pop(0)
            if hop >= max_hops:
                continue
            for edge in self.edges:
                nxt = ""
                rel = str(edge.relationship or "related_to")
                if edge.source == current:
                    nxt = edge.target
                elif edge.target == current:
                    nxt = edge.source
                if not nxt or nxt not in self.nodes:
                    continue
                weight = path_strength * float(edge.strength or 0.0) * (0.92**hop)
                if weight > scored.get(nxt, 0.0):
                    scored[nxt] = weight
                    best_rel[nxt] = rel
                    via_label = str(self.nodes[current].label if current in self.nodes else current)
                    best_via[nxt] = via_label
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, hop + 1, weight))
        results = []
        for nid, score in sorted(scored.items(), key=lambda kv: kv[1], reverse=True):
            if nid == start_id:
                continue
            node = self.nodes[nid]
            if node.archived:
                continue
            row = asdict(node)
            row["association_strength"] = round(score, 4)
            row["relationship"] = best_rel.get(nid, "related_to")
            row["via"] = best_via.get(nid, "")
            results.append(row)
        return results

    def boost_from_usage(self, used_concept_ids: list[str], ignored_concept_ids: list[str]) -> None:
        """Bootstrap: concepts Ava actually referenced gain weight; ignored ones lose weight."""
        for nid in used_concept_ids:
            node = self.nodes.get(nid)
            if node:
                node.weight = _clamp(node.weight + 0.06, 0.0, 1.0)
                node.activation_count += 1
        for nid in ignored_concept_ids:
            node = self.nodes.get(nid)
            if node:
                node.weight = _clamp(node.weight - 0.02, 0.0, 1.0)
        if used_concept_ids or ignored_concept_ids:
            self._save()

    def strengthen_edge(self, source_id: str, target_id: str) -> None:
        if source_id == target_id or source_id not in self.nodes or target_id not in self.nodes:
            return
        now = time.time()
        for edge in self.edges:
            if (
                (edge.source == source_id and edge.target == target_id)
                or (edge.source == target_id and edge.target == source_id)
            ):
                edge.strength = _clamp(edge.strength + 0.05, 0.0, 1.0)
                edge.last_fired = now
                self._save()
                return
        self.edges.append(
            ConceptEdge(
                source=source_id,
                target=target_id,
                relationship="related_to",
                strength=0.45,
                last_fired=now,
            )
        )
        self._save()

    def decay_unused_nodes(self, days_threshold: int = 30) -> int:
        now = time.time()
        threshold_s = max(1, int(days_threshold or 30)) * 24 * 3600
        decayed = 0
        for node in self.nodes.values():
            age = now - float(node.last_activated or 0.0)
            if age < threshold_s:
                continue
            node.weight = _clamp(node.weight - 0.1, 0.0, 1.0)
            if node.weight < 0.1:
                node.archived = True
            decayed += 1
        if decayed:
            self._save()
        return decayed

    # ── 10-level decay (memory rewrite Phase 2 step 3) ─────────────────────
    # Per-level inactivity thresholds (seconds). Higher levels decay more
    # slowly. See docs/MEMORY_REWRITE_PLAN.md § 2.2 for rationale.
    _LEVEL_DECAY_THRESHOLDS_S: dict[int, float] = {
        10: 7 * 86400,   # 7 days
        9:  5 * 86400,   # 5 days
        8:  3 * 86400,   # 3 days
        7:  2 * 86400,   # 2 days
        6:  1 * 86400,   # 1 day
        5:  12 * 3600,   # 12 hours
        4:  6 * 3600,    # 6 hours
        3:  3 * 3600,    # 3 hours
        2:  1 * 3600,    # 1 hour
        1:  1 * 3600,    # 1 hour (then deletion)
    }

    def decay_levels(self, now: float | None = None) -> dict[str, int]:
        """Walk all nodes; demote level if untouched past threshold.

        At level 0, an unarchived node is deleted permanently and a
        tombstone is appended to state/memory_tombstones.jsonl. Archived
        nodes clamp to level 1 instead of being deleted.

        Returns a counts dict for diagnostics:
          {
            "demoted": <int>,
            "deleted": <int>,
            "archived_floor": <int>,  # archived nodes prevented from delete
            "examined": <int>,
          }

        Honors the AVA_DECAY_DISABLED env kill-switch — when set to "1"
        this method is a no-op so the user can pause decay without a
        code change if the rate proves wrong.
        """
        if os.environ.get("AVA_DECAY_DISABLED", "0").strip() == "1":
            return {"demoted": 0, "deleted": 0, "archived_floor": 0, "examined": 0, "skipped": 1}

        t_now = now if now is not None else time.time()
        demoted = 0
        deleted_ids: list[str] = []
        archived_floor = 0
        tombstones: list[dict[str, Any]] = []

        for node_id, node in list(self.nodes.items()):
            inactive_s = t_now - float(node.last_activated or 0.0)
            threshold = self._LEVEL_DECAY_THRESHOLDS_S.get(int(node.level), 12 * 3600)
            if inactive_s < threshold:
                continue
            # Past threshold for current level — demote.
            new_level = int(node.level) - 1
            if new_level >= 1:
                node.level = new_level
                node.last_activated = t_now  # reset timer for next level
                demoted += 1
                continue
            # Hit level 0 — archived nodes clamp to 1, others get deleted.
            if node.archived:
                node.level = 1
                node.last_activated = t_now
                archived_floor += 1
                continue
            tombstones.append({
                "ts": t_now,
                "node_id": node_id,
                "label": node.label,
                "type": node.type,
                "last_level": int(node.level),
                "deleted_reason": "decay_floor",
            })
            deleted_ids.append(node_id)

        # Remove deleted nodes + their incident edges.
        for nid in deleted_ids:
            self.nodes.pop(nid, None)
        if deleted_ids:
            self.edges = [
                e for e in self.edges
                if e.source not in deleted_ids and e.target not in deleted_ids
            ]

        # Append tombstones to log (best-effort; non-fatal).
        if tombstones:
            try:
                tomb_path = self.base_dir / "state" / "memory_tombstones.jsonl"
                tomb_path.parent.mkdir(parents=True, exist_ok=True)
                with tomb_path.open("a", encoding="utf-8") as f:
                    for t in tombstones:
                        f.write(json.dumps(t, ensure_ascii=False) + "\n")
            except OSError as _e:
                print(f"[concept_graph] tombstone write failed (non-fatal): {_e!r}")

        if demoted or deleted_ids or archived_floor:
            self._save()

        return {
            "demoted": demoted,
            "deleted": len(deleted_ids),
            "archived_floor": archived_floor,
            "examined": len(self.nodes) + len(deleted_ids),
        }

    def reset_node_level(self, node_id: str, level: int = 5) -> bool:
        """Set a node's level explicitly. Used by reflection scoring
        (Phase 2 step 5) to apply promotions/demotions. Clamps to 1-10.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return False
        node.level = max(1, min(10, int(level)))
        node.last_activated = time.time()
        self._save()
        return True

    def adjust_node_level(self, node_id: str, delta: int) -> bool:
        """Adjust a node's level by delta, clamping to 1-10. Resets the
        decay timer (last_activated). Returns True if the node existed.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return False
        node.level = max(1, min(10, int(node.level) + int(delta)))
        node.last_activated = time.time()
        self._save()
        return True


def _safe_read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except Exception:
            continue
    if isinstance(limit, int) and limit > 0:
        return out[-limit:]
    return out


def _extract_keywords(text: str, max_items: int = 4) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-']{2,}", str(text or "").lower())
    stop = {
        "the", "and", "that", "this", "with", "from", "have", "your", "about", "what", "when", "where", "which", "them",
        "they", "were", "been", "into", "there", "their", "would", "could", "should", "just", "like", "more", "very",
        "really", "than", "then", "over", "under", "also", "only", "dont", "cant", "im", "youre", "ava", "zeke", "max",
    }
    counts = Counter([w for w in words if w not in stop and len(w) >= 3])
    return [w for w, _ in counts.most_common(max(1, int(max_items or 4)))]


def _infer_emotions_from_text(text: str) -> list[str]:
    low = str(text or "").lower()
    lex = {
        "concerned": ["concern", "worry", "worried", "issue", "problem", "bug"],
        "frustration": ["frustrat", "annoy", "stuck", "broken"],
        "curious": ["curious", "wonder", "question", "explore"],
        "calmness": ["calm", "steady", "stable", "okay", "ok"],
        "sad": ["sad", "goodnight", "sleep", "bye"],
        "hopeful": ["hope", "improve", "better", "upgrade", "fix"],
    }
    out: list[str] = []
    for emo, markers in lex.items():
        if any(m in low for m in markers):
            out.append(emo)
    if not out:
        out.append("neutral")
    return out[:3]


def _extract_turn_topics_batch_with_mistral(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not turns:
        return []
    prompt_rows = []
    for idx, turn in enumerate(turns, start=1):
        role = str(turn.get("role") or "")
        content = " ".join(str(turn.get("content") or "").split()).strip()[:360]
        prompt_rows.append(f"{idx}. {role}: {content}")
    prompt = "\n".join(prompt_rows)
    try:
        llm = ChatOllama(model="mistral:7b", temperature=0.1)
        result = llm.invoke(
            [
                SystemMessage(
                    content=(
                        "Return ONLY JSON array, one object per turn in order: "
                        "{main_topic, sub_topics:[...], people:[...], emotions:[...]}."
                    )
                ),
                HumanMessage(content=prompt[-7500:]),
            ]
        )
        txt = (getattr(result, "content", None) or str(result)).strip()
        left, right = txt.find("["), txt.rfind("]")
        if left < 0 or right <= left:
            raise ValueError("no json array")
        arr = json.loads(txt[left : right + 1])
        if not isinstance(arr, list):
            raise ValueError("bad array")
        out: list[dict[str, Any]] = []
        for row in arr[: len(turns)]:
            if not isinstance(row, dict):
                out.append({})
                continue
            out.append(
                {
                    "main_topic": str(row.get("main_topic") or "").strip()[:120],
                    "sub_topics": [str(x).strip()[:80] for x in list(row.get("sub_topics") or []) if str(x).strip()],
                    "people": [str(x).strip()[:80] for x in list(row.get("people") or []) if str(x).strip()],
                    "emotions": [str(x).strip()[:48] for x in list(row.get("emotions") or []) if str(x).strip()],
                }
            )
        while len(out) < len(turns):
            out.append({})
        return out
    except Exception:
        out = []
        for turn in turns:
            content = str(turn.get("content") or "")
            kws = _extract_keywords(content, max_items=4)
            out.append(
                {
                    "main_topic": kws[0] if kws else "general",
                    "sub_topics": kws[1:4],
                    "people": [p for p in ["Zeke", "Max", "Ava", "Emil"] if p.lower() in content.lower()],
                    "emotions": _infer_emotions_from_text(content),
                }
            )
        return out


def _extract_concepts_with_mistral(corpus: str) -> list[dict[str, str]]:
    if not corpus.strip():
        return []
    try:
        llm = ChatOllama(model="mistral:7b", temperature=0.2)
        result = llm.invoke(
            [
                SystemMessage(
                    content=(
                        "List the key concepts, people, topics, and themes from this conversation as a JSON array of "
                        "{label, type, relationship_to_previous} objects."
                    )
                ),
                HumanMessage(content=corpus[-6000:]),
            ]
        )
        txt = (getattr(result, "content", None) or str(result)).strip()
        left, right = txt.find("["), txt.rfind("]")
        if left < 0 or right <= left:
            return []
        arr = json.loads(txt[left : right + 1])
        out: list[dict[str, str]] = []
        for row in arr:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            rtp = str(row.get("relationship_to_previous") or "related_to").strip() or "related_to"
            typ = str(row.get("type") or "topic").strip().lower()
            if typ not in TYPE_COLORS:
                typ = "topic"
            if label:
                out.append({"label": label[:120], "type": typ, "relationship_to_previous": rtp[:64]})
        return out
    except Exception:
        return []


def extract_concepts_from_text(corpus: str) -> list[dict[str, str]]:
    concepts = _extract_concepts_with_mistral(corpus)
    if concepts:
        return concepts
    # Fallback: lightweight extraction from title-cased names and nouns.
    words = re.findall(r"[A-Za-z][A-Za-z0-9_\-']{2,}", corpus or "")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in words:
        label = token.strip()
        low = label.lower()
        if low in seen:
            continue
        seen.add(low)
        typ = "person" if label[:1].isupper() else "topic"
        out.append({"label": label[:120], "type": typ, "relationship_to_previous": "related_to"})
        if len(out) >= 12:
            break
    return out


def bootstrap_from_existing_memory(graph: ConceptGraph, host: dict[str, Any] | None = None) -> dict[str, Any]:
    host = host or {}
    base_dir = Path(host.get("BASE_DIR") or graph.base_dir)
    report_path = base_dir / "state" / "bootstrap_report.json"
    now = time.time()
    edges_before = len(graph.edges)
    nodes_before = len(graph.nodes)
    node_type_counter: Counter[str] = Counter()
    person_nodes: dict[str, str] = {}
    topic_nodes: dict[str, str] = {}
    emotion_nodes: dict[str, str] = {}
    memory_nodes: list[str] = []
    self_nodes: list[str] = []
    opinion_nodes: list[str] = []
    curiosity_nodes: list[str] = []

    def register_node(label: str, ntype: NodeType, notes: str = "", weight: float | None = None) -> str:
        node_id = graph.find_or_create(label, ntype)
        node = graph.nodes.get(node_id)
        if node is not None:
            if notes and not node.notes:
                node.notes = notes[:500]
            if isinstance(weight, (int, float)):
                node.weight = _clamp(float(weight), 0.0, 1.0)
            node.archived = False
            node.last_activated = now
        node_type_counter[str(ntype)] += 1
        return node_id

    ava_id = register_node("Ava", "self", notes="Core self node from identity/soul.")
    self_nodes.append(ava_id)

    # A) PEOPLE nodes + profile threads as topics/memories/emotions.
    profiles_dir = base_dir / "profiles"
    if profiles_dir.is_dir():
        for profile_path in sorted(profiles_dir.glob("*.json")):
            data = _safe_read_json(profile_path)
            if not isinstance(data, dict):
                continue
            label = str(data.get("name") or profile_path.stem).strip()
            if not label:
                continue
            trust = float(data.get("relationship_score") or 0.0)
            rel_type = str(data.get("relationship_to_zeke") or "known").strip()
            notes = f"relationship={rel_type}; trust={trust:.2f}; interactions={int(data.get('interaction_count') or 0)}"
            person_id = register_node(label, "person", notes=notes, weight=max(0.2, min(1.0, 0.3 + trust * 0.7)))
            person_nodes[label.lower()] = person_id
            graph.add_edge(ava_id, person_id, "knows", 0.85 if label.lower() == "zeke" else 0.55)
            if label.lower() == "zeke":
                graph.add_edge(ava_id, person_id, "created_by", 0.98)
                graph.add_edge(ava_id, person_id, "trusts", 0.95)
            for thread in list(data.get("threads") or []):
                if not isinstance(thread, dict):
                    continue
                t_topic = str(thread.get("topic") or "").strip()
                if not t_topic:
                    continue
                t_emotion = str(thread.get("emotion") or "").strip().lower() or "neutral"
                topic_id = register_node(t_topic, "topic")
                topic_nodes[t_topic.lower()] = topic_id
                graph.add_edge(person_id, topic_id, "discussed_topic", 0.62)
                mem_id = register_node(f"{label}: {t_topic[:96]}", "memory", notes=str(thread.get("notes") or ""))
                memory_nodes.append(mem_id)
                graph.add_edge(mem_id, person_id, "involves_person", 0.6)
                graph.add_edge(mem_id, topic_id, "about_topic", 0.65)
                emo_id = register_node(t_emotion, "emotion")
                emotion_nodes[t_emotion] = emo_id
                graph.add_edge(person_id, emo_id, "felt_around", 0.58)
                graph.add_edge(mem_id, emo_id, "emotion_association", 0.52)

    # B) SELF nodes from identity/soul/self-model.
    identity_md = (base_dir / "ava_core" / "IDENTITY.md").read_text(encoding="utf-8", errors="replace") if (base_dir / "ava_core" / "IDENTITY.md").is_file() else ""
    soul_md = (base_dir / "ava_core" / "SOUL.md").read_text(encoding="utf-8", errors="replace") if (base_dir / "ava_core" / "SOUL.md").is_file() else ""
    self_model = _safe_read_json(base_dir / "memory" / "self reflection" / "self_model.json")
    values = re.findall(r"-\s+(.+)", soul_md)
    for value in values[:24]:
        v = value.strip()
        if not v:
            continue
        vid = register_node(v, "self")
        self_nodes.append(vid)
        graph.add_edge(ava_id, vid, "has_value", 0.88)
    if isinstance(self_model, dict):
        for drive in list(self_model.get("core_drives") or [])[:16]:
            sid = register_node(str(drive), "self")
            self_nodes.append(sid)
            graph.add_edge(ava_id, sid, "has_trait", 0.86)
        for trait in list(self_model.get("perceived_strengths") or [])[:24]:
            sid = register_node(str(trait), "self")
            self_nodes.append(sid)
            graph.add_edge(ava_id, sid, "has_trait", 0.78)
        chapter = str(self_model.get("active_goal", {}).get("name") if isinstance(self_model.get("active_goal"), dict) else "")
        if chapter:
            cid = register_node(f"chapter: {chapter}", "self")
            self_nodes.append(cid)
            graph.add_edge(ava_id, cid, "current_chapter", 0.9)
        for q in list(self_model.get("curiosity_questions") or [])[:20]:
            qid = register_node(str(q), "self")
            self_nodes.append(qid)
            graph.add_edge(ava_id, qid, "self_question", 0.72)
    if identity_md:
        for fact in _extract_keywords(identity_md, max_items=10):
            fid = register_node(fact, "self")
            self_nodes.append(fid)
            graph.add_edge(ava_id, fid, "identity_anchor", 0.7)

    # C) MEMORY nodes from reflections.
    reflections = _safe_read_jsonl(base_dir / "memory" / "self reflection" / "reflection_log.jsonl")
    emotion_freq: Counter[str] = Counter()
    topic_to_memory: defaultdict[str, list[str]] = defaultdict(list)
    for row in reflections:
        summary = str(row.get("summary") or row.get("user_input") or row.get("ai_reply") or "").strip()
        if not summary:
            continue
        importance = _clamp(float(row.get("importance") or 0.45), 0.05, 1.0)
        tags = [str(t).strip().lower() for t in list(row.get("tags") or []) if str(t).strip()]
        topic = tags[0] if tags else (_extract_keywords(summary, max_items=1)[0] if _extract_keywords(summary, max_items=1) else "reflection")
        mem_id = register_node(f"memory: {summary[:96]}", "memory", notes=summary[:500], weight=importance)
        memory_nodes.append(mem_id)
        topic_id = register_node(topic, "topic")
        topic_nodes[topic.lower()] = topic_id
        topic_to_memory[topic.lower()].append(mem_id)
        graph.add_edge(mem_id, topic_id, "about_topic", 0.62)
        graph.add_edge(ava_id, mem_id, "remembers", 0.66)
        person_id = person_nodes.get(str(row.get("person_id") or "").strip().lower()) or person_nodes.get("zeke")
        if person_id:
            graph.add_edge(mem_id, person_id, "involves_person", 0.63)
            graph.add_edge(person_id, mem_id, "memory_of", 0.58)
        emos = [t for t in tags if t in {"calmness", "curious", "concerned", "frustration", "sad", "hopeful", "neutral"}]
        if not emos:
            emos = _infer_emotions_from_text(summary)
        for emo in emos[:2]:
            emotion_freq[emo] += 1
            emo_id = register_node(emo, "emotion")
            emotion_nodes[emo] = emo_id
            graph.add_edge(mem_id, emo_id, "felt_during", 0.58)
            graph.add_edge(topic_id, emo_id, "emotional_association", 0.45)

    # D) EMOTION nodes from mood + reflections.
    mood_data = _safe_read_json(base_dir / "ava_mood.json")
    if isinstance(mood_data, dict):
        weights = mood_data.get("emotion_weights")
        if isinstance(weights, dict):
            for emo, val in sorted(weights.items(), key=lambda kv: float(kv[1] or 0.0), reverse=True)[:12]:
                emo_name = str(emo).strip().lower()
                emo_id = register_node(emo_name, "emotion", weight=_clamp(float(val or 0.0), 0.05, 1.0))
                emotion_nodes[emo_name] = emo_id
                graph.add_edge(ava_id, emo_id, "feels", 0.68)
                emotion_freq[emo_name] += int(max(1, float(val or 0.0) * 10))
    for emo, cnt in emotion_freq.most_common(14):
        emo_id = register_node(emo, "emotion", weight=_clamp(0.25 + cnt * 0.04, 0.0, 1.0))
        emotion_nodes[emo] = emo_id
        graph.add_edge(ava_id, emo_id, "feels", 0.62)

    # E) TOPIC nodes from chatlog + reflection using mistral:7b in batches of 10 turns.
    chat_rows = _safe_read_jsonl(base_dir / "chatlog.jsonl", limit=100)
    for idx in range(0, len(chat_rows), 10):
        batch = chat_rows[idx : idx + 10]
        extracted = _extract_turn_topics_batch_with_mistral(batch)
        for turn, item in zip(batch, extracted):
            main_topic = str(item.get("main_topic") or "").strip() or "general conversation"
            main_id = register_node(main_topic, "topic")
            topic_nodes[main_topic.lower()] = main_id
            role = str(turn.get("role") or "").lower()
            person_id = person_nodes.get("zeke") if role in {"user", "human"} else ava_id
            graph.add_edge(person_id, main_id, "discussed_topic", 0.64)
            for st in list(item.get("sub_topics") or [])[:5]:
                sid = register_node(str(st), "topic")
                topic_nodes[str(st).lower()] = sid
                graph.add_edge(main_id, sid, "related_topic", 0.5)
            for p in list(item.get("people") or [])[:4]:
                pid = person_nodes.get(str(p).lower())
                if pid:
                    graph.add_edge(pid, main_id, "discussed_topic", 0.6)
            emos = list(item.get("emotions") or [])[:3] or _infer_emotions_from_text(str(turn.get("content") or ""))
            for emo in emos:
                eid = register_node(str(emo).lower(), "emotion")
                emotion_nodes[str(emo).lower()] = eid
                graph.add_edge(main_id, eid, "emotional_association", 0.5)
            mem_links = topic_to_memory.get(main_topic.lower(), [])
            for mid in mem_links[:4]:
                graph.add_edge(main_id, mid, "references_memory", 0.54)
                graph.add_edge(mid, main_id, "memory_of_topic", 0.54)

    topic_ids = [nid for nid, node in graph.nodes.items() if node.type == "topic"]
    for i in range(len(topic_ids)):
        left = graph.nodes.get(topic_ids[i])
        if left is None:
            continue
        lw = set(_extract_keywords(left.label, max_items=4))
        if not lw:
            continue
        for j in range(i + 1, min(i + 18, len(topic_ids))):
            right = graph.nodes.get(topic_ids[j])
            if right is None:
                continue
            rw = set(_extract_keywords(right.label, max_items=4))
            overlap = len(lw & rw)
            if overlap >= 1:
                graph.add_edge(left.id, right.id, "co_occurs", 0.26 if overlap == 1 else 0.48)

    # F) Curiosity nodes.
    cur_state = _safe_read_json(base_dir / "state" / "curiosity_topics.json")
    curiosity_rows: list[dict[str, Any]] = []
    if isinstance(cur_state, dict):
        for row in list(cur_state.get("topics") or []):
            if isinstance(row, dict):
                curiosity_rows.append(row)
    if not curiosity_rows:
        try:
            from brain.curiosity_topics import get_current_curiosity

            cur = get_current_curiosity(host)
            if isinstance(cur, dict):
                curiosity_rows.append(cur)
        except Exception:
            pass
    for row in curiosity_rows:
        topic = str(row.get("topic") or row.get("name") or "").strip()
        if not topic:
            continue
        priority = _clamp(float(row.get("priority") or row.get("score") or 0.5), 0.05, 1.0)
        cid = register_node(topic, "curiosity", weight=priority)
        curiosity_nodes.append(cid)
        graph.add_edge(ava_id, cid, "curious_about", 0.66)
        for kw in _extract_keywords(topic, max_items=3):
            tid = register_node(kw, "topic")
            topic_nodes[kw] = tid
            graph.add_edge(cid, tid, "related_topic", 0.48)

    # G) Opinion nodes.
    opinions = _safe_read_json(base_dir / "state" / "opinions.json")
    if isinstance(opinions, dict):
        for row in list(opinions.get("opinions") or []):
            if not isinstance(row, dict):
                continue
            topic = str(row.get("topic") or "").strip()
            stance = str(row.get("stance") or "").strip()
            confidence = _clamp(float(row.get("confidence") or 0.5), 0.05, 1.0)
            if not topic:
                continue
            opinion_label = f"opinion: {topic} -> {stance or 'unspecified'}"
            oid = register_node(opinion_label, "opinion", notes=str(row.get("reasoning") or ""), weight=confidence)
            opinion_nodes.append(oid)
            tid = register_node(topic, "topic")
            topic_nodes[topic.lower()] = tid
            graph.add_edge(oid, tid, "opinion_about", 0.62)
            graph.add_edge(ava_id, oid, "holds_opinion", 0.66)
            formed = str(row.get("formed_from") or "").strip()
            if formed:
                fid = register_node(f"memory: {formed[:96]}", "memory", notes=formed)
                memory_nodes.append(fid)
                graph.add_edge(oid, fid, "formed_from", 0.57)
                graph.add_edge(fid, tid, "about_topic", 0.54)

    # H) Inner monologue nodes.
    thoughts = _safe_read_json(base_dir / "state" / "inner_monologue.json")
    if isinstance(thoughts, dict):
        thought_rows = [r for r in list(thoughts.get("thoughts") or []) if isinstance(r, dict)]
        thought_rows = thought_rows[-120:]
        newest_ts = max((float(r.get("ts") or 0.0) for r in thought_rows), default=0.0)
        for row in thought_rows:
            if isinstance(row, dict):
                thought = str(row.get("thought") or "").strip()
                if thought:
                    ts = float(row.get("ts") or 0.0)
                    recency = 0.45
                    if newest_ts > 0 and ts > 0:
                        recency = _clamp(0.35 + ((ts / newest_ts) * 0.65), 0.1, 1.0)
                    mid = register_node(f"thought: {thought[:96]}", "memory", notes=thought[:500], weight=recency)
                    memory_nodes.append(mid)
                    graph.add_edge(ava_id, mid, "inner_thought", 0.52)
                    for kw in _extract_keywords(thought, max_items=4):
                        tid = register_node(kw, "topic")
                        topic_nodes[kw] = tid
                        graph.add_edge(mid, tid, "about_topic", 0.46)
                    for emo in _infer_emotions_from_text(thought):
                        eid = register_node(emo, "emotion")
                        emotion_nodes[emo] = eid
                        graph.add_edge(mid, eid, "felt_during", 0.42)

    # I) Relationship edge pass.
    for pid in person_nodes.values():
        graph.add_edge(ava_id, pid, "knows", 0.82 if pid == person_nodes.get("zeke") else 0.55)
    for sid in set(self_nodes):
        graph.add_edge(ava_id, sid, "has_trait", 0.84)
    for mid in memory_nodes[:800]:
        for pid in list(person_nodes.values())[:6]:
            mem_node = graph.nodes.get(mid)
            person_node = graph.nodes.get(pid)
            if mem_node is None or person_node is None:
                continue
            if person_node.label.lower() in mem_node.label.lower() or person_node.label.lower() in mem_node.notes.lower():
                graph.add_edge(mid, pid, "involves_person", 0.58)
    for oid in opinion_nodes:
        for pid in list(person_nodes.values()):
            op = graph.nodes.get(oid)
            person = graph.nodes.get(pid)
            if op is None or person is None:
                continue
            if person.label.lower() in (op.label + " " + op.notes).lower():
                graph.add_edge(oid, pid, "formed_with", 0.4)

    # Weak distant associations.
    topic_ids = [nid for nid, n in graph.nodes.items() if n.type == "topic"]
    for i in range(0, min(len(topic_ids), 220), 3):
        if i + 1 < len(topic_ids):
            graph.add_edge(topic_ids[i], topic_ids[i + 1], "distant_association", 0.18)

    graph.last_bootstrap = now
    graph.prune_old_nodes(max_nodes=500)
    graph._save()
    data = graph.get_graph_data()
    nodes_by_type = dict((data.get("stats") or {}).get("nodes_by_type") or {})
    report = {
        "bootstrap_ts": now,
        "nodes_before": nodes_before,
        "edges_before": edges_before,
        "nodes_after": len(data.get("nodes") or []),
        "edges_after": len(data.get("edges") or []),
        "nodes_created": max(0, len(data.get("nodes") or []) - nodes_before),
        "edges_created": max(0, len(data.get("edges") or []) - edges_before),
        "nodes_by_type": nodes_by_type,
        "most_activated": (data.get("stats") or {}).get("most_activated", ""),
        "last_bootstrap": graph.last_bootstrap,
    }
    try:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    print(
        f"[concept_graph] bootstrap complete nodes={report['nodes_after']} edges={report['edges_after']} "
        f"created_nodes={report['nodes_created']} created_edges={report['edges_created']} by_type={nodes_by_type}"
    )
    return report

