"""tower_boot_sentinel: the 2026-10-02 one-guarded-retry branch.

Every side effect is stubbed (no launch, no DM, no network, no subprocess, no sleeps).
Run:  .venv\\Scripts\\python.exe -m pytest tests\\test_sentinel_retry.py -q
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "tower_boot_sentinel.py"


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("tbs_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.LOG = tmp_path / "tower_boot.log"
    m.REPO = tmp_path                       # launcher_tail reads tmp state/, force flag absent
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "launcher_boot.log").write_text(
        "[Fri 10/02/2026  8:18:43.55] launcher START: start_iris_v2.bat\n"
        "[host] perception poll failed (non-fatal): noise\n"
        "[Fri 10/02/2026  8:18:52.26] starting iris_body_host.py in the FOREGROUND\n",
        encoding="utf-8")
    m.BODY_WAIT_S = 0.05
    m.wait_for_network = lambda *a, **k: True
    monkeypatch.setattr(m.time, "sleep", lambda s: None)       # auto-restored
    calls = SimpleNamespace(launches=0, dms=[])
    m.launch_stack = lambda: setattr(calls, "launches", calls.launches + 1)
    m.dm_zeke = lambda text: calls.dms.append(text) or True
    # post-up side effects (vector heal subprocess, boot nudge POST) must stay inert
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(m.requests, "post",
                        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {}))
    return m, calls


def _ports(m, seq):
    it = iter(seq)
    m.port_answers = lambda *a, **k: next(it, seq[-1])


def test_cognition_alive_means_no_relaunch(tmp_path, monkeypatch):
    m, calls = _load(tmp_path, monkeypatch)
    _ports(m, [False])                      # never answers
    m.cognition_alive = lambda: True
    monkeypatch.setattr("sys.argv", ["tower_boot_sentinel.py"])
    m.main()
    assert calls.launches == 1             # the original launch only
    assert any("Not relaunching" in d for d in calls.dms)
    assert any("starting iris_body_host.py" in d for d in calls.dms)   # tail quoted
    assert not any("perception poll" in d for d in calls.dms)          # noise filtered


def test_retry_succeeds(tmp_path, monkeypatch):
    m, calls = _load(tmp_path, monkeypatch)
    # first wait: pre-check + polls all False until the deadline; after relaunch: True
    state = {"relaunched": False}
    m.port_answers = lambda *a, **k: state["relaunched"]
    m.launch_stack = lambda: (setattr(calls, "launches", calls.launches + 1),
                              state.__setitem__("relaunched", calls.launches >= 2))
    m.cognition_alive = lambda: False
    monkeypatch.setattr("sys.argv", ["tower_boot_sentinel.py"])
    m.main()
    assert calls.launches == 2
    assert any("Retrying the launch ONCE" in d for d in calls.dms)
    assert any("Second launch worked" in d for d in calls.dms)


def test_retry_fails(tmp_path, monkeypatch):
    m, calls = _load(tmp_path, monkeypatch)
    _ports(m, [False])
    m.cognition_alive = lambda: False
    monkeypatch.setattr("sys.argv", ["tower_boot_sentinel.py"])
    m.main()
    assert calls.launches == 2             # exactly one retry, never a loop
    assert any("Second launch ALSO failed" in d for d in calls.dms)


def test_already_up_never_launches(tmp_path, monkeypatch):
    m, calls = _load(tmp_path, monkeypatch)
    _ports(m, [True])
    m.cognition_alive = lambda: pytest.fail("must not probe when the port answers")
    monkeypatch.setattr("sys.argv", ["tower_boot_sentinel.py"])
    m.main()
    assert calls.launches == 0
