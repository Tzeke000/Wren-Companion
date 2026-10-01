"""Round-2 3.1 / 3.3 / 3.6 — the counterfactual archive finally holds bytes and gets read back; the
handoff gathers tasks + anchors that the old calls silently dropped; ids are strings, not None.

    .venv\\Scripts\\python.exe tests/test_small_call_fixes.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Never import the real avaagent in a test: handoff._gather → shared_lexicon → person_registry →
# `import avaagent`, whose single-instance guard calls sys.exit(1) when :5876 is taken (it always is,
# the runtime holds it) and whose module body launches install watchers + atexit hooks. Stub it.
import types  # noqa: E402

_fake_av = types.ModuleType("avaagent")
_fake_av.load_profile_by_id = lambda person_id: {}  # type: ignore[attr-defined]
sys.modules.setdefault("avaagent", _fake_av)

from brain import anchor_moments, counterfactual_archive as cfa, handoff, working_memory as wm  # noqa: E402


def _reset_cfa(tmp: Path) -> None:
    cfa._cache.clear()
    cfa.configure(tmp)


def test_record_simple_writes_a_row_and_returns_its_id() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _reset_cfa(tmp)
        cid = cfa.record_simple(considered="say good morning", chose="asked about the shift",
                                reason="he just got home from work", person_id="zeke")
        assert cid.startswith("cf-"), cid
        p = tmp / "state" / "counterfactuals.jsonl"
        assert p.is_file(), "the archive must now hold bytes"
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 1 and rows[0]["id"] == cid
        assert rows[0]["considered_options"] == [{"option": "say good morning", "rejected_reason": "he just got home from work"}]
        assert rows[0]["chosen_reply"] == "asked about the shift" and rows[0]["why_chosen"] == "he just got home from work"
        assert rows[0]["user_input"] == "say good morning", "user_input defaults to the considered text"
        assert rows[0]["person_id"] == "zeke"


def test_record_simple_refuses_empties_like_the_archive() -> None:
    with tempfile.TemporaryDirectory() as td:
        _reset_cfa(Path(td))
        assert cfa.record_simple(considered="", chose="x") == ""
        assert cfa.record_simple(considered="x", chose="") == ""
        assert not (Path(td) / "state" / "counterfactuals.jsonl").exists()


def test_recent_for_prompt_reads_the_archive_back() -> None:
    with tempfile.TemporaryDirectory() as td:
        _reset_cfa(Path(td))
        assert cfa.recent_for_prompt() == "", "empty archive renders nothing (no phantom line)"
        cfa.record_simple(considered="A", chose="B", reason="r1")
        cfa.record_simple(considered="C", chose="D", reason="r2")
        cfa.record_simple(considered="E", chose="F")
        txt = cfa.recent_for_prompt(limit=2)
        lines = txt.splitlines()
        assert len(lines) == 2, txt
        assert "considered: E -> chose: F" in lines[0] and "(" not in lines[0], lines[0]
        assert "considered: C -> chose: D (r2)" in lines[1], lines[1]


def test_handoff_gathers_tasks_and_anchors() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        anchor_moments._cache.clear()
        anchor_moments.configure(tmp)
        aid = anchor_moments.mark_anchor(person_id="zeke", kind="milestone",
                                         summary="first morning after the round-2 restart")
        assert isinstance(aid, str) and aid.startswith("anchor-"), "mark_anchor returns the id STRING"
        g: dict = {}
        tid = wm.start_task(g, "close the handoff loop", person_id="zeke")
        assert tid.startswith("task-")
        out = handoff._gather(g, tmp)
        assert out["active_tasks"] and out["active_tasks"][0]["description"] == "close the handoff loop", out["active_tasks"]
        assert out["recent_anchors"] and out["recent_anchors"][0]["kind"] == "milestone", out["recent_anchors"]
        assert out["recent_anchors"][0]["summary"].startswith("first morning")
        json.dumps(out)  # must be serialisable — dataclasses were the old silent failure
        assert handoff.write_handoff(g, tmp) is True
        back = handoff.read_handoff(tmp)
        assert back and back["active_tasks"][0]["id"] == tid
        summary = handoff.handoff_summary_for_prompt(back)
        assert "close the handoff loop" in summary and "[milestone] first morning" in summary, summary


def test_handoff_without_tasks_or_anchors_is_still_clean() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        anchor_moments._cache.clear()
        anchor_moments.configure(tmp)
        out = handoff._gather({}, tmp)
        assert out["active_tasks"] == [] and out["recent_anchors"] == []


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
