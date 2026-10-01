"""Round-2 fix 2.7 — a scene caption that times out is retried when cognition is back; people in
frame tighten the caption gap.

    .venv\\Scripts\\python.exe tests/test_scene_caption_retry.py
"""
from __future__ import annotations

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

from brain import iris_llm, iris_time, scene_memory as sm  # noqa: E402


def _make(tmp: Path) -> "sm.SceneMemory":
    sm.DIR = tmp / "state" / "scene_memory"
    sm.INDEX = sm.DIR / "keyframes.jsonl"
    s = sm.SceneMemory({"BASE_DIR": str(tmp)})
    kid = "kf_test_1"
    s.records[kid] = {"id": kid, "reason": "change", "diff": 9.1, "iso": "2026-10-01T09:00:00",
                      "path": str(tmp / "kf.jpg"), "sensors": {"faces": ["zeke"]}}
    s.order.append(kid)
    return s


def _attach(recent: bool, now: float):
    """Pretend cognition attached 10 s before `now` (recent) or an hour before (stale)."""
    iris_time.get_state = (lambda: {"last_session_attached_ts": now - (10.0 if recent else 3600.0)})  # type: ignore[assignment]


def test_timeout_queues_a_retry_then_succeeds_when_cognition_is_back() -> None:
    with tempfile.TemporaryDirectory() as td:
        s = _make(Path(td))
        answers = iter([None, "He sat down at the desk and the lamp came on."])
        iris_llm.describe_image = lambda *a, **k: next(answers)  # type: ignore[assignment]
        t0 = sm.time.time()
        s._caption("kf_test_1")
        r = s.records["kf_test_1"]
        assert r["caption_status"] == "timeout" and r["caption_attempts"] == 1
        assert "kf_test_1" in s._retry and s._retry["kf_test_1"] >= t0 + sm.CAPTION_RETRY_GAP_S - 1
        due = s._retry["kf_test_1"] + 1
        # too early → nothing
        _attach(True, due - 60)
        assert s._retry_timeouts(due - 60, sync=True) is None
        # due, but cognition has NOT attached recently → wait
        _attach(False, due)
        assert s._retry_timeouts(due, sync=True) is None
        assert "kf_test_1" in s._retry
        # due + cognition awake → retried inline, succeeds
        _attach(True, due)
        kid = s._retry_timeouts(due, sync=True)
        assert kid == "kf_test_1", kid
        assert r["caption_status"] == "done" and r["words"].startswith("He sat down"), r
        assert "kf_test_1" not in s._retry and s.stats["caption_retries"] == 1


def test_retries_are_bounded_and_backfilled_frames_are_dropped() -> None:
    with tempfile.TemporaryDirectory() as td:
        s = _make(Path(td))
        iris_llm.describe_image = lambda *a, **k: None  # type: ignore[assignment]
        s._caption("kf_test_1")                                    # attempt 1 → queued
        t = s._retry["kf_test_1"] + 1
        _attach(True, t)
        assert s._retry_timeouts(t, sync=True) == "kf_test_1"       # attempt 2 → queued again
        assert s.records["kf_test_1"]["caption_attempts"] == 2 and "kf_test_1" in s._retry
        # the not-before is stamped from the REAL clock while this test drives a synthetic `now`,
        # so also clear the live rate gate (CAPTION_MIN_GAP_S since the last caption at `t`)
        t2 = max(s._retry["kf_test_1"] + 1, t + sm.CAPTION_MIN_GAP_S + 1)
        _attach(True, t2)
        assert s._retry_timeouts(t2, sync=True) == "kf_test_1"      # attempt 3 → gives up
        assert s.records["kf_test_1"]["caption_attempts"] == 3 and "kf_test_1" not in s._retry
        # a frame someone backfilled by hand is dropped from the retry list, not re-asked
        s.records["kf_test_1"]["caption_attempts"] = 0
        s._retry["kf_test_1"] = t2
        s.set_words("kf_test_1", "backfilled by hand")
        _attach(True, t2 + 5000)
        assert s._retry_timeouts(t2 + 5000, sync=True) is None and "kf_test_1" not in s._retry


def test_person_in_frame_halves_the_caption_gap() -> None:
    with tempfile.TemporaryDirectory() as td:
        s = _make(Path(td))
        started = []
        s._caption = lambda kid: started.append(kid)  # type: ignore[assignment]
        import threading
        real_thread = threading.Thread

        class _Inline(real_thread):
            def start(self):  # type: ignore[override]
                self._target(*self._args)  # type: ignore[attr-defined]
        sm.threading.Thread = _Inline  # type: ignore[assignment]
        try:
            s._last_caption_ts = sm.time.time() - 90.0          # 90 s since the last caption
            s._maybe_caption("kf_test_1")                       # faces present → 60 s gap → goes
            assert started == ["kf_test_1"], started
            s.records["kf_test_1"]["sensors"] = {"faces": []}
            s._last_caption_ts = sm.time.time() - 90.0
            s._maybe_caption("kf_test_1")                       # nobody there → 120 s gap → skipped
            assert started == ["kf_test_1"] and s.records["kf_test_1"]["caption_status"] == "skipped_rate"
        finally:
            sm.threading.Thread = real_thread  # type: ignore[assignment]


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
