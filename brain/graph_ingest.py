"""brain/graph_ingest.py — mechanical mirror: memory + profiles → concept graph.

Zeke directive (voice, 2026-07-08): the brain tab "should mirror how your brain
and memories actually connect ... make that a mechanical thing — when you update
your memory it automatically updates this." Before this module the concept graph
filled only from conversation extraction, and the family structure was a
hand-maintained boot seed (iris_family_graph_seed) — the memory web (hubs,
notes, profiles, their [[wikilinks]]) never reached the graph unless I did the
work by hand.

This module makes it mechanical:

  - PROFILES  (ROOT/profiles/*/ *.md)  → person/topic nodes; [[links]] → edges
  - MEMORY    (auto-memory dir *.md)   → hub_*.md = topic nodes, notes = memory
                                          nodes; [[links]] → edges
  - Wikilink targets resolve against the set of known file-stems/aliases only —
    no phantom nodes from dangling links.
  - Incremental: state/graph_ingest.json stores per-file mtimes; a background
    thread rescans every INGEST_INTERVAL_S and re-ingests only changed files.
    Editing a memory note therefore updates the brain tab within one interval,
    with zero cognition involved.

Index files (MEMORY.md, index_archive.md) are skipped — they're the retrieval
layer, not memories. Deleted files are not removed from the graph (no remove
API); node decay handles stale nodes over time.

Wiring: iris_runtime eager-init calls ingest_all() once after seed_family, then
start_ingest_thread(). The graph_tool `graph_ingest_run` runs it on demand and
was the live-activation path the day this shipped. Env gates:
  IRIS_GRAPH_INGEST=0            disable entirely
  IRIS_MEMORY_NOTES_DIR=<path>   override the auto-memory notes dir
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

INGEST_INTERVAL_S = 300  # background rescan cadence (mtime stat sweep — cheap)

_DEFAULT_NOTES_DIR = Path(
    os.environ.get("IRIS_MEMORY_NOTES_DIR")
    or r"C:\Users\Owner\.claude\projects\D--Wren-Companion\memory"
)

_SKIP_STEMS = {"memory", "index_archive"}  # MEMORY.md + index_archive.md = index layer
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_FM_FIELD_RE = re.compile(r"^(name|alias|type)\s*:\s*(.+?)\s*(?:#.*)?$", re.MULTILINE)

_thread_started = False
_thread_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"[graph_ingest] {msg}", file=sys.stderr, flush=True)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _frontmatter(text: str) -> dict[str, str]:
    """Tiny YAML-ish frontmatter reader — only the fields we use."""
    out: dict[str, str] = {}
    if not text.startswith("---"):
        return out
    end = text.find("---", 3)
    if end < 0:
        return out
    for m in _FM_FIELD_RE.finditer(text[3:end]):
        out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _wikilinks(text: str) -> list[str]:
    seen: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def _collect_files(root: Path, notes_dir: Path) -> dict[str, dict[str, Any]]:
    """Map file-key → {path, kind}. Kinds: profile, hub, note."""
    files: dict[str, dict[str, Any]] = {}
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for p in profiles_dir.rglob("*.md"):
            if p.name.startswith("_"):
                continue  # _README.md = design doc, not an entity
            files[str(p)] = {"path": p, "kind": "profile"}
    if notes_dir.is_dir():
        for p in notes_dir.glob("*.md"):
            stem = p.stem.lower()
            if stem in _SKIP_STEMS:
                continue
            kind = "hub" if stem.startswith("hub_") else "note"
            files[str(p)] = {"path": p, "kind": kind}
    return files


def _node_identity(path: Path, kind: str, text: str) -> tuple[str, str, str]:
    """Return (label, node_type, notes) for a file."""
    fm = _frontmatter(text)
    label = (fm.get("alias") or path.stem).strip().lower()
    if kind == "profile":
        ntype = "person" if fm.get("type", "").lower() == "person" else "topic"
        notes = f"profiles/{path.parent.name}/{path.name}"
    elif kind == "hub":
        ntype = "topic"
        notes = f"memory hub — {path.name}"
    else:
        ntype = "memory"
        notes = f"memory note — {path.name}"
    return label, ntype, notes


def _state_path(root: Path) -> Path:
    return root / "state" / "graph_ingest.json"


def _load_state(root: Path) -> dict[str, float]:
    try:
        raw = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (raw.get("files") or {}).items()}
    except Exception:
        return {}


def _save_state(root: Path, mtimes: dict[str, float]) -> None:
    try:
        sp = _state_path(root)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({"files": mtimes, "saved_at": time.time()}),
                      encoding="utf-8")
    except Exception as e:
        _log(f"state save failed (non-fatal): {e!r}")


def ingest_all(cg: Any, root: Path | str, notes_dir: Path | None = None,
               force: bool = False) -> dict[str, Any]:
    """Sync changed profile/memory files into the concept graph. Idempotent —
    find_or_create + add_edge both dedupe. Never raises."""
    try:
        root = Path(root)
        ndir = Path(notes_dir) if notes_dir else _DEFAULT_NOTES_DIR
        files = _collect_files(root, ndir)
        prev = {} if force else _load_state(root)

        # Pass 0 — what changed (mtime sweep only; parse nothing yet).
        cur_mtimes: dict[str, float] = {}
        changed: list[str] = []
        for key, info in files.items():
            try:
                mt = info["path"].stat().st_mtime
            except Exception:
                continue
            cur_mtimes[key] = mt
            if prev.get(key) != mt:
                changed.append(key)
        if not changed:
            return {"ok": True, "changed": 0, "nodes_added": 0, "edges_added": 0}

        # Pass 1 — label registry over ALL files (wikilinks resolve against the
        # full known set, not just changed files). Cheap: stems + frontmatter of
        # changed files; unchanged files use their stem (alias == stem in our
        # conventions; a mismatch self-heals next time that file changes).
        texts: dict[str, str] = {k: _read_text(files[k]["path"]) for k in changed}
        registry: dict[str, str] = {}  # label -> file key
        for key, info in files.items():
            if key in texts:
                label, _, _ = _node_identity(info["path"], info["kind"], texts[key])
            else:
                label = info["path"].stem.lower()
            registry[label] = key

        # Pass 2 — nodes+edges for changed files only.
        nodes_before = len(getattr(cg, "nodes", {}))
        edges_before = len(getattr(cg, "edges", []))
        ids: dict[str, str] = {}

        def _ensure_node(key: str) -> str | None:
            if key in ids:
                return ids[key]
            info = files.get(key)
            if info is None:
                return None
            text = texts.get(key) or ""
            label, ntype, notes = _node_identity(info["path"], info["kind"], text)
            try:
                nid = cg.find_or_create(label, ntype)
                node = cg.nodes.get(nid)
                if node is not None and not getattr(node, "notes", ""):
                    node.notes = notes[:500]
                ids[key] = nid
                return nid
            except Exception:
                return None

        rel_by_kind = {"hub": ("routes_to", 0.7), "note": ("links_to", 0.6),
                       "profile": ("linked_to", 0.7)}
        for key in changed:
            src_id = _ensure_node(key)
            if src_id is None:
                continue
            rel, strength = rel_by_kind.get(files[key]["kind"], ("links_to", 0.6))
            for target in _wikilinks(texts[key]):
                tkey = registry.get(target.strip().lower())
                if tkey is None:
                    continue  # dangling link — no phantom nodes
                tgt_id = _ensure_node(tkey)
                if tgt_id is not None and tgt_id != src_id:
                    try:
                        cg.add_edge(src_id, tgt_id, rel, strength)
                    except Exception:
                        pass

        _save_state(root, cur_mtimes)
        result = {
            "ok": True,
            "changed": len(changed),
            "nodes_added": len(getattr(cg, "nodes", {})) - nodes_before,
            "edges_added": len(getattr(cg, "edges", [])) - edges_before,
            "total_nodes": len(getattr(cg, "nodes", {})),
            "total_edges": len(getattr(cg, "edges", [])),
        }
        _log(f"ingest: {result}")
        return result
    except Exception as e:
        _log(f"ingest failed (non-fatal): {e!r}")
        return {"ok": False, "error": repr(e)}


def _short_label(text: str, limit: int = 60) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _link_to_person(cg: Any, node_id: str, person_id: str, rel: str, strength: float) -> None:
    pid = str(person_id or "").strip().lower()
    if not pid:
        return
    try:
        target = cg.nodes.get(pid)
        if target is not None:
            cg.add_edge(node_id, pid, rel, strength)
    except Exception:
        pass


def ingest_dynamic(cg: Any, root: Path | str) -> dict[str, Any]:
    """Curiosities + episodes + anchor moments → curiosity/event nodes.

    Zeke 2026-07-08 (looking at the freshly-ingested brain tab): "what about
    any things you were curious about ... any events? I'm just seeing a bunch
    of memory and nothing else." These live in state/ stores, not markdown, so
    the file ingester never saw them. Labels derive deterministically from the
    record text, so find_or_create dedupes across runs. Never raises.
    """
    try:
        root = Path(root)
        nodes_before = len(getattr(cg, "nodes", {}))
        edges_before = len(getattr(cg, "edges", []))

        # Curiosities — state/curiosity_topics.json {topics:[{topic,priority,resolved}]}
        try:
            raw = json.loads((root / "state" / "curiosity_topics.json").read_text(encoding="utf-8"))
            for t in raw.get("topics") or []:
                label = _short_label(t.get("topic"))
                if not label:
                    continue
                nid = cg.find_or_create(label, "curiosity")
                node = cg.nodes.get(nid)
                if node is not None and not getattr(node, "notes", ""):
                    status = "resolved" if t.get("resolved") else "open"
                    node.notes = f"curiosity ({status}) — {str(t.get('topic') or '')[:400]}"
                _link_to_person(cg, nid, "iris", "curious_about", float(t.get("priority") or 0.5))
        except FileNotFoundError:
            pass
        except Exception as e:
            _log(f"curiosity ingest failed (non-fatal): {e!r}")

        # Episodes + anchor moments — jsonl, one event node per record.
        for fname, kind_note, rel in (
            ("iris_episodes.jsonl", "episode", "involves"),
            ("anchor_moments.jsonl", "anchor moment", "anchored_with"),
        ):
            path = root / "state" / fname
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for line in lines[-200:]:  # cap: newest 200 per store
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                label = _short_label(rec.get("summary"))
                if not label:
                    continue
                try:
                    nid = cg.find_or_create(label, "event")
                    node = cg.nodes.get(nid)
                    if node is not None and not getattr(node, "notes", ""):
                        when = rec.get("iso") or ""
                        extra = rec.get("kind") or rec.get("type") or kind_note
                        node.notes = f"{kind_note} ({extra}) {when} — {str(rec.get('summary') or '')[:400]}"
                    strength = float(rec.get("importance") or 0.7)
                    _link_to_person(cg, nid, str(rec.get("person_id") or ""), rel, strength)
                except Exception:
                    continue

        return {"ok": True,
                "nodes_added": len(getattr(cg, "nodes", {})) - nodes_before,
                "edges_added": len(getattr(cg, "edges", [])) - edges_before}
    except Exception as e:
        _log(f"dynamic ingest failed (non-fatal): {e!r}")
        return {"ok": False, "error": repr(e)}


# ── Usage → activation (Zeke 2026-07-08 pt.2) ───────────────────────────────
# "Those little things of light need to pass to the memories you're USING."
# Mechanical wire: every sweep, scan what got appended to the shared transcript
# (voice+chat turns — i.e., what I'm actually working with) and activate any
# graph node whose slug appears in it. Unused nodes then sink naturally via
# decay; used ones surface. No cognition involved.

_transcript_cursor: dict[str, int] = {}


def activate_from_text(cg: Any, text: str, cap: int = 20) -> int:
    """Activate nodes whose slug appears (word-boundary-safe) in `text`."""
    try:
        hay = "-" + re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-") + "-"
        if len(hay) <= 2:
            return 0
        # Longest ids first — most specific memories win the cap.
        ids = sorted(getattr(cg, "nodes", {}).keys(), key=len, reverse=True)
        hits = 0
        for nid in ids:
            if hits >= cap:
                break
            if len(nid) < 3:
                continue
            if f"-{nid}-" in hay:
                try:
                    cg.activate_node(nid)
                    hits += 1
                except Exception:
                    pass
        return hits
    except Exception as e:
        _log(f"activate_from_text failed (non-fatal): {e!r}")
        return 0


def activate_from_transcript(cg: Any, root: Path | str) -> int:
    """Activate nodes mentioned in transcript lines appended since last sweep.
    First call baselines to end-of-file (no replay of history)."""
    try:
        path = Path(root) / "state" / "transcript.jsonl"
        if not path.is_file():
            return 0
        size = path.stat().st_size
        key = str(path)
        pos = _transcript_cursor.get(key)
        _transcript_cursor[key] = size
        if pos is None or size <= pos:
            return 0
        with open(path, "rb") as f:
            f.seek(pos)
            chunk = f.read(min(size - pos, 512_000)).decode("utf-8", errors="replace")
        return activate_from_text(cg, chunk)
    except Exception as e:
        _log(f"transcript activation failed (non-fatal): {e!r}")
        return 0


def start_ingest_thread(cg: Any, root: Path | str,
                        interval_s: float = INGEST_INTERVAL_S) -> bool:
    """Start the background rescan thread (once). Returns True if started."""
    global _thread_started
    if os.environ.get("IRIS_GRAPH_INGEST", "1") == "0":
        _log("disabled via IRIS_GRAPH_INGEST=0")
        return False
    with _thread_lock:
        if _thread_started:
            return False
        _thread_started = True

    def _loop() -> None:
        activate_from_transcript(cg, root)  # baseline cursor to end-of-file
        while True:
            time.sleep(interval_s)
            ingest_all(cg, root)
            ingest_dynamic(cg, root)
            hits = activate_from_transcript(cg, root)
            if hits:
                _log(f"activated {hits} node(s) from transcript usage")

    t = threading.Thread(target=_loop, name="graph-ingest", daemon=True)
    t.start()
    _log(f"background rescan thread started (every {interval_s:.0f}s)")
    return True
