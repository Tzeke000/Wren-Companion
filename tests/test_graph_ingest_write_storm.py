"""Round-2 fix 1.1 — graph-ingest write storm. Proves the LOOP, not the note:

  * one disk write per ingest pass (was one per add_edge re-fire, ~104/cycle);
  * a pass whose sources are unchanged writes NOTHING;
  * the fallback path (live instance of the pre-fix class) batches too;
  * an exception inside a batched block still flushes once;
  * add_node on an existing node writes once (was twice).

Runs against a temp dir — never touches the live state/. Writes are counted by
wrapping pathlib.Path.replace (the tmp→final step of ConceptGraph._save).

    .venv\\Scripts\\python.exe -m pytest tests/test_graph_ingest_write_storm.py -q
    .venv\\Scripts\\python.exe tests/test_graph_ingest_write_storm.py      (no pytest)
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import graph_ingest as gi  # noqa: E402
from brain.concept_graph import ConceptGraph  # noqa: E402


class WriteCounter:
    """Counts real tmp→final replaces of a given target file."""

    def __init__(self, target: Path) -> None:
        self.target = target.resolve()
        self.n = 0
        self._orig = pathlib.Path.replace

    def __enter__(self) -> "WriteCounter":
        counter = self

        def _replace(p: pathlib.Path, dst):  # type: ignore[no-untyped-def]
            try:
                if Path(dst).resolve() == counter.target:
                    counter.n += 1
            except Exception:
                pass
            return counter._orig(p, dst)

        pathlib.Path.replace = _replace  # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> bool:  # type: ignore[no-untyped-def]
        pathlib.Path.replace = self._orig  # type: ignore[assignment]
        return False


def _seed_sources(root: Path, n_topics: int = 100, n_events: int = 150) -> None:
    st = root / "state"
    st.mkdir(parents=True, exist_ok=True)
    (st / "curiosity_topics.json").write_text(json.dumps({
        "topics": [{"topic": f"curiosity number {i} about something", "priority": 0.5, "resolved": False}
                   for i in range(n_topics)]}), encoding="utf-8")
    with open(st / "iris_episodes.jsonl", "w", encoding="utf-8") as f:
        for i in range(n_events):
            f.write(json.dumps({"summary": f"episode {i} happened in the room", "person_id": "zeke",
                                "importance": 0.7, "iso": "2026-10-01T00:00:00"}) + "\n")
    with open(st / "anchor_moments.jsonl", "w", encoding="utf-8") as f:
        for i in range(20):
            f.write(json.dumps({"summary": f"anchor moment {i}", "person_id": "zeke", "importance": 0.8}) + "\n")
    (st / "iris_mood.json").write_text(json.dumps({"emotion_weights": {
        "interest": 0.61, "calmness": 0.30, "joy": 0.05, "sadness": 0.01}}), encoding="utf-8")


def _fresh(root: Path) -> ConceptGraph:
    gi._dyn_cache.clear()
    cg = ConceptGraph(root)
    cg.add_node("iris", "self")
    cg.add_node("zeke", "person")
    return cg


class OldStyleGraph(ConceptGraph):
    """Simulates a LIVE instance of the pre-fix class: no deferred_save(), and a
    _save that ignores the _flush kwarg, exactly like the 09-30 code."""
    deferred_save = None  # type: ignore[assignment]

    def _save(self, *_a, **_k) -> None:  # type: ignore[override]
        self._defer_depth = 0  # the old class never deferred
        ConceptGraph._save(self, _flush=True)


def test_dynamic_pass_writes_once_then_nothing() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_sources(root)
        cg = _fresh(root)
        with WriteCounter(cg.path) as wc:
            r = gi.ingest_dynamic(cg, root)
        assert r["ok"] and r["nodes_added"] >= 100, r
        assert wc.n == 1, f"first dynamic pass should write once, wrote {wc.n}"
        with WriteCounter(cg.path) as wc:
            r2 = gi.ingest_dynamic(cg, root)
        assert r2["ok"] and r2["nodes_added"] == 0, r2
        assert wc.n == 0, f"unchanged sources must write nothing, wrote {wc.n}"


def test_changed_source_reingests_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_sources(root)
        cg = _fresh(root)
        gi.ingest_dynamic(cg, root)
        p = root / "state" / "curiosity_topics.json"
        time.sleep(0.01)
        p.write_text(json.dumps({"topics": [{"topic": "a brand new curiosity", "priority": 0.9}]}), encoding="utf-8")
        with WriteCounter(cg.path) as wc:
            r = gi.ingest_dynamic(cg, root)
        assert r["nodes_added"] == 1, r
        assert wc.n == 1, wc.n
        assert "a-brand-new-curiosity" in cg.nodes


def test_mood_signature_gates_the_mood_section() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_sources(root)
        cg = _fresh(root)
        gi.ingest_dynamic(cg, root)
        mp = root / "state" / "iris_mood.json"
        # the mood file rewrites every 5 s with sub-0.01 drift — must NOT write
        mp.write_text(json.dumps({"emotion_weights": {"interest": 0.612, "calmness": 0.298, "joy": 0.051}}), encoding="utf-8")
        with WriteCounter(cg.path) as wc:
            gi.ingest_dynamic(cg, root)
        assert wc.n == 0, f"sub-0.01 mood drift wrote {wc.n}"
        # a real shift (new top emotion) → exactly one write
        mp.write_text(json.dumps({"emotion_weights": {"frustration": 0.5, "interest": 0.3}}), encoding="utf-8")
        with WriteCounter(cg.path) as wc:
            gi.ingest_dynamic(cg, root)
        assert wc.n == 1, wc.n
        assert "frustration" in cg.nodes


def test_old_class_fallback_batches_and_cleans_up() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_sources(root)
        gi._dyn_cache.clear()
        cg = OldStyleGraph(root)
        cg.add_node("iris", "self")
        cg.add_node("zeke", "person")
        assert not callable(getattr(cg, "deferred_save", None))
        with WriteCounter(cg.path) as wc:
            r = gi.ingest_dynamic(cg, root)
        assert r["ok"] and r["nodes_added"] >= 100, r
        assert wc.n == 1, f"fallback path should batch to one write, wrote {wc.n}"
        assert "_save" not in vars(cg), "instance-level _save interception must be removed after the block"
        on_disk = json.loads(cg.path.read_text(encoding="utf-8"))
        assert len(on_disk["nodes"]) == len(cg.nodes), "flush must have written the new nodes"


def test_nested_blocks_flush_once_at_outermost_exit() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cg = _fresh(root)
        with WriteCounter(cg.path) as wc:
            with gi._deferred(cg):
                cg.add_node("alpha", "topic")
                with gi._deferred(cg):
                    cg.add_node("beta", "topic")
                    cg.add_edge("alpha", "beta", "links_to", 0.5)
                assert wc.n == 0, "inner exit must not flush"
            assert wc.n == 1, wc.n
        cg2 = OldStyleGraph(root)
        with WriteCounter(cg2.path) as wc:
            with gi._deferred(cg2):
                cg2.add_node("gamma", "topic")
                with gi._deferred(cg2):  # nested on the fallback path → no-op wrapper
                    cg2.add_node("delta", "topic")
            assert wc.n == 1, wc.n
        assert "_save" not in vars(cg2)


def test_exception_inside_block_still_flushes_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for cls in (ConceptGraph, OldStyleGraph):
            cg = cls(root)
            with WriteCounter(cg.path) as wc:
                try:
                    with gi._deferred(cg):
                        cg.add_node("boom", "topic")
                        raise RuntimeError("mid-block failure")
                except RuntimeError:
                    pass
            assert wc.n == 1, f"{cls.__name__}: wrote {wc.n}"
            assert "_save" not in vars(cg)
            assert "boom" in json.dumps(json.loads(cg.path.read_text(encoding="utf-8")))


def test_add_node_existing_writes_once_not_twice() -> None:
    with tempfile.TemporaryDirectory() as td:
        cg = _fresh(Path(td))
        with WriteCounter(cg.path) as wc:
            cg.add_node("zeke", "person")
        assert wc.n == 1, f"add_node(existing) wrote {wc.n} (was 2 before the fix)"


def test_ingest_all_writes_once_then_nothing() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        notes = root / "notes"
        notes.mkdir()
        (notes / "hub_voice.md").write_text("# voice\nsee [[mouth_note]] and [[ears_note]]", encoding="utf-8")
        (notes / "mouth_note.md").write_text("mouth [[ears_note]]", encoding="utf-8")
        (notes / "ears_note.md").write_text("ears", encoding="utf-8")
        (root / "profiles").mkdir()
        cg = _fresh(root)
        with WriteCounter(cg.path) as wc:
            r = gi.ingest_all(cg, root, notes_dir=notes, force=True)
        assert r["ok"] and r["edges_added"] >= 2, r
        assert wc.n == 1, f"ingest_all should write once, wrote {wc.n}"
        with WriteCounter(cg.path) as wc:
            r2 = gi.ingest_all(cg, root, notes_dir=notes)
        assert r2["changed"] == 0 and wc.n == 0, (r2, wc.n)


def test_activate_from_text_writes_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cg = _fresh(root)
        for i in range(10):
            cg.add_node(f"topic-{i}-thing", "topic")
        text = " ".join(f"topic {i} thing" for i in range(10))
        with WriteCounter(cg.path) as wc:
            hits = gi.activate_from_text(cg, text)
        assert hits == 10, hits
        assert wc.n == 1, wc.n


def test_reload_keeps_thread_flag_and_cursor() -> None:
    import importlib
    gi._thread_started = True  # pretend the live thread exists
    gi._transcript_cursor["x"] = 42
    gi._dyn_cache["probe"] = 1
    importlib.reload(gi)
    assert gi._thread_started is True, "reload must not forget the live thread (would spawn a twin)"
    assert gi._transcript_cursor.get("x") == 42
    assert gi._dyn_cache.get("probe") == 1
    gi._thread_started = False
    gi._transcript_cursor.clear()
    gi._dyn_cache.clear()


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)
