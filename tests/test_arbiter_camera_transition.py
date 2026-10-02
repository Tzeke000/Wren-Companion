"""attention_arbiter: camera_transition ownership (added 2026-10-02 from wake_missed evidence).

Run: .venv\\Scripts\\python.exe -m pytest tests\\test_arbiter_camera_transition.py -q
"""
import importlib


def _arb(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTENTION_ARBITER_LEDGER", str(tmp_path / "situations.jsonl"))
    from brain import attention_arbiter as a
    return importlib.reload(a)


def test_owned_now(tmp_path, monkeypatch):
    a = _arb(tmp_path, monkeypatch)
    assert "camera_transition" in a.OWNERSHIP
    assert a.OWNERSHIP["camera_transition"]["primary"] == "host-eyes"


def test_runtime_camera_stands_by_while_host_eyes_alive(tmp_path, monkeypatch):
    a = _arb(tmp_path, monkeypatch)
    d = a.decide("camera_transition", "runtime-camera", primary_alive=True, record=False)
    assert d["wake"] is False and d["role"] == "standing_by"


def test_runtime_camera_is_fallback_when_host_eyes_dead(tmp_path, monkeypatch):
    a = _arb(tmp_path, monkeypatch)
    for alive in (False, None):           # dead, or unknown -> fail open
        d = a.decide("camera_transition", "runtime-camera", primary_alive=alive, record=False)
        assert d["wake"] is True and d["role"] == "fallback"
