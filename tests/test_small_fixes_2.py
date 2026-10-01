"""Round-2 2.4 + 3.5 — the adaptive learner stops re-applying unchanged evidence; curriculum lessons
reach the file the sleep handoff reads.

    .venv\\Scripts\\python.exe tests/test_small_fixes_2.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_fake_av = types.ModuleType("avaagent")
_fake_av.load_profile_by_id = lambda person_id: {}  # type: ignore[attr-defined]
sys.modules.setdefault("avaagent", _fake_av)

from brain import adaptive_iris as ai, adaptive_learning as al, curriculum as cur  # noqa: E402


def _fixed(target, n):
    return lambda *a, **k: {"target": target, "n": n, "detail": "fixed-for-test"}


def _redirect_prefs(tmp: Path) -> None:
    al.LEARNING_DIR = tmp / "state" / "learning"
    al.PREFERENCES_PATH = al.LEARNING_DIR / "adaptive_preferences.json"


def test_same_evidence_twice_moves_weights_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _redirect_prefs(tmp)
        ai.evidence_proactive = _fixed(0.8, 12)
        ai.evidence_pacing = _fixed(0.9, 30)
        ai.evidence_memory = _fixed(0.3, 20)
        ai.evidence_curiosity = _fixed(None, 0)
        r1 = ai.learn(str(tmp))
        assert r1["ok"] and set(r1["updated"]) >= {"proactive_trigger_usefulness", "conversation_pacing", "memory_usefulness"}, r1["updated"]
        w1 = dict(al.load_preferences()["weights"])
        r2 = ai.learn(str(tmp))
        assert r2["updated"] == {}, f"second run on identical evidence must change nothing, got {r2['updated']}"
        assert all("no new evidence" in r2["skipped"][f] for f in ("proactive_trigger_usefulness", "conversation_pacing", "memory_usefulness")), r2["skipped"]
        assert dict(al.load_preferences()["weights"]) == w1, "weights on disk must be untouched by a no-evidence run"
        assert Path(r2["evidence_sig_path"]).is_file()
        # one more observation → that focus moves again, the others stay put
        ai.evidence_memory = _fixed(0.3, 21)
        r3 = ai.learn(str(tmp))
        assert set(r3["updated"]) == {"memory_usefulness"}, r3["updated"]
        assert "no new evidence" in r3["skipped"]["conversation_pacing"]


def test_dry_run_never_writes_signatures() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _redirect_prefs(tmp)
        ai.evidence_proactive = _fixed(0.6, 10)
        ai.evidence_pacing = _fixed(None, 0)
        ai.evidence_memory = _fixed(None, 0)
        ai.evidence_curiosity = _fixed(None, 0)
        r = ai.learn(str(tmp), dry_run=True)
        assert r["updated"] and not Path(r["evidence_sig_path"]).exists()
        r2 = ai.learn(str(tmp))
        assert r2["updated"], "a dry run must not consume the evidence"


def _write_entry(dirp: Path, slug: str, title: str) -> None:
    (dirp / f"{slug}.txt").write_text(f"---\ntitle: {title}\nthemes: patience, pride\nreading_status: unread\n---\nOnce upon a time.\n",
                                       encoding="utf-8")


def test_mark_read_lands_in_the_file_the_sleep_handoff_reads() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        found = tmp / "curriculum" / "foundation"
        found.mkdir(parents=True)
        cur.FOUNDATION_DIR = found
        cur.INDEX_PATH = found / "_index.json"
        cur.LESSONS_LOG = tmp / "state" / "learning" / "lessons.jsonl"
        _write_entry(found, "the_fox_and_the_grapes", "The Fox and the Grapes")
        cur.INDEX_PATH.write_text(json.dumps([{"slug": "the_fox_and_the_grapes", "title": "The Fox and the Grapes",
                                               "filename": "the_fox_and_the_grapes.txt", "reading_status": "unread"}]),
                                  encoding="utf-8")
        entry = cur.mark_read(slug="the_fox_and_the_grapes", lessons_extracted=["contempt is the cheapest consolation"])
        assert entry["reading_status"] == "read" and entry["lessons_extracted"] == ["contempt is the cheapest consolation"]
        assert cur.LESSONS_LOG.is_file(), "the lessons log the sleep handoff reads must now exist"
        rows = [json.loads(l) for l in cur.LESSONS_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert rows and rows[0]["lesson"] == "contempt is the cheapest consolation" and rows[0]["source"] == "curriculum:the_fox_and_the_grapes"
        # the reader: sleep_mode._recent_lessons(g) looks at BASE_DIR/state/learning/lessons.jsonl
        from brain import sleep_mode
        got = sleep_mode._recent_lessons({"BASE_DIR": str(tmp)}, limit=5)
        assert got and "contempt is the cheapest consolation" in got[0], got


def test_mark_read_unknown_slug_raises_value_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        found = tmp / "curriculum" / "foundation"
        found.mkdir(parents=True)
        cur.FOUNDATION_DIR = found
        cur.INDEX_PATH = found / "_index.json"
        cur.LESSONS_LOG = tmp / "state" / "learning" / "lessons.jsonl"
        try:
            cur.mark_read(slug="nope", lessons_extracted=["x"])
        except ValueError as e:
            assert "entry not found" in str(e)
        else:
            raise AssertionError("expected ValueError for an unknown slug")


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
