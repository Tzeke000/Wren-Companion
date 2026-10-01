"""The host's long-lived tasks must not die silently (2026-10-01: the eye poller died at 11:32 on an
UnboundLocalError and nothing noticed for 2+ h). `_supervised` logs, ledgers, DMs once, and respawns.

    .venv\\Scripts\\python.exe tests/test_host_supervised.py
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_fake_av = types.ModuleType("avaagent")
_fake_av.load_profile_by_id = lambda person_id: {}  # type: ignore[attr-defined]
sys.modules.setdefault("avaagent", _fake_av)

import iris_body_host as h  # noqa: E402


def test_supervised_respawns_a_dying_task_and_dms_once() -> None:
    calls = {"runs": 0, "blog": [], "dm": []}
    h._blog = lambda ch, ev, detail=None: calls["blog"].append((ch, ev, detail))  # type: ignore[assignment]
    assert h._hh is not None
    h._hh.discord_send = lambda token, chan, text, **k: calls["dm"].append(text) or {"ok": True}  # type: ignore[assignment]
    h.load_bot_token = lambda: "T"  # type: ignore[assignment]
    done = asyncio.Event()

    async def flaky():
        calls["runs"] += 1
        if calls["runs"] < 3:
            raise UnboundLocalError("cannot access local variable 'sal'")
        done.set()
        await asyncio.sleep(3600)  # a healthy long-lived task never returns

    async def main():
        t = asyncio.create_task(h._supervised("perception_reader", flaky, backoff_s=0.01))
        await asyncio.wait_for(done.wait(), timeout=5)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert calls["runs"] == 3, calls["runs"]
    died = [b for b in calls["blog"] if b[1] == "task died"]
    assert len(died) == 2 and died[0][2]["task"] == "perception_reader" and "sal" in died[0][2]["error"], died
    assert len(calls["dm"]) == 1 and "perception_reader" in calls["dm"][0], calls["dm"]


def test_supervised_passes_cancellation_through() -> None:
    async def forever():
        await asyncio.sleep(3600)

    async def main():
        t = asyncio.create_task(h._supervised("x", forever, backoff_s=0.01))
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(main()) is True


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
