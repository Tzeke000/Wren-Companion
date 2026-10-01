"""Round-2 fix 1.2 — cognition holds get a consumer; voice replies never die in a muted mouth.

    .venv\\Scripts\\python.exe tests/test_host_hold.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import host_hold as hh  # noqa: E402


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> float:
        self.t += s
        return self.t


def test_silent_active_turn_is_held_once_then_released_on_output() -> None:
    c = Clock()
    d = hh.HoldDetector(hold_after_s=180, clock=c)
    assert d.check() is None, "idle host is never 'held'"
    d.turn_started("discord")
    c.tick(179)
    assert d.check() is None
    c.tick(2)
    ev = d.check()
    assert ev and ev["event"] == "held" and ev["source"] == "discord" and ev["silent_s"] >= 180, ev
    assert d.check() is None, "held must be reported ONCE"
    c.tick(3600)
    assert d.check() is None
    d.activity()  # tokens start streaming again
    rel = d.check()
    assert rel and rel["event"] == "released" and rel["how"] == "output resumed" and rel["duration_s"] >= 3600, rel
    assert d.check() is None, "released must be reported ONCE"
    assert d.hold_count == 1


def test_tool_in_flight_is_not_a_hold() -> None:
    c = Clock()
    d = hh.HoldDetector(hold_after_s=180, clock=c)
    d.turn_started("terminal")
    d.tool_started()           # a 10-minute Bash command
    c.tick(600)
    assert d.check() is None, "silence during a running tool is work, not a hold"
    d.tool_finished()
    c.tick(181)                # model never came back after the tool result
    ev = d.check()
    assert ev and ev["event"] == "held", ev


def test_turn_end_releases_a_hold() -> None:
    c = Clock()
    d = hh.HoldDetector(hold_after_s=60, clock=c)
    d.turn_started("voice")
    c.tick(61)
    assert d.check()["event"] == "held"
    d.turn_ended()
    rel = d.check()
    assert rel and rel["event"] == "released" and rel["how"] == "turn ended", rel


def test_stderr_hint_makes_detection_fast_and_attributed() -> None:
    c = Clock()
    d = hh.HoldDetector(hold_after_s=180, fast_after_s=30, clock=c)
    d.turn_started("discord")
    assert d.note_stderr("Rate limit reached, retrying in 42s (attempt 3/10)") is True
    assert d.note_stderr("[iris_attach] attempt 2/6") is False
    c.tick(29)
    assert d.check() is None
    c.tick(2)
    ev = d.check()
    assert ev and ev["event"] == "held" and ev["hint"] and "Rate limit" in ev["hint"], ev
    assert "stderr" in ev["reason"]


def test_a_stale_stderr_hint_does_not_fast_trigger() -> None:
    c = Clock()
    d = hh.HoldDetector(hold_after_s=180, fast_after_s=30, clock=c)
    d.note_stderr("429 too many requests")   # long before the turn
    c.tick(1000)
    d.turn_started("orb")
    c.tick(31)
    assert d.check() is None, "an old hint must not flag a fresh turn"
    c.tick(150)
    ev = d.check()
    assert ev and ev["event"] == "held" and ev["hint"] is None, ev


def test_held_flag_round_trip_and_clear() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = str(Path(td) / "state" / "cognition_held.json")
        info = {"since": 1790000000.0, "source": "discord", "silent_s": 181.0, "reason": "r", "hint": None}
        r = hh.write_held_flag(info, path=p)
        assert r["ok"], r
        got = hh.read_held_flag(p)
        assert got and got["held"] is True and got["source"] == "discord" and got["since"] == 1790000000.0
        assert "since_iso" in got and "restore" in got and got["pid"] > 0
        prev = hh.clear_held_flag(p)
        assert prev and prev["held"] is True
        assert hh.read_held_flag(p) is None
        assert hh.clear_held_flag(p) is None, "clearing an absent flag is a no-op"


def test_voice_flag_cache_semantics_and_ttl() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = str(Path(td) / "voice_deliberately_off.json")
        c = Clock()
        v = hh.VoiceFlagCache(path=p, ttl_s=5, clock=c)
        assert v.off() is False, "absent flag = voice ON"
        Path(p).write_text('{"off": false}', encoding="utf-8")
        c.tick(6)
        assert v.off() is False
        Path(p).write_text('{"off": true, "why": "quiet hours"}', encoding="utf-8")
        assert v.off() is False, "within the TTL the cached value stands"
        c.tick(6)
        assert v.off() is True
        Path(p).write_text("not json", encoding="utf-8")
        c.tick(6)
        assert v.off() is False, "garbage fails OPEN (voice on), like every other reader"
        Path(p).write_text('{"off": true}', encoding="utf-8")
        v.invalidate()
        assert v.off() is True


def test_route_reply() -> None:
    assert hh.route_reply("voice", {"voice"}, voice_off=False) == (True, False)
    assert hh.route_reply("voice", {"voice"}, voice_off=True) == (False, True)
    assert hh.route_reply("discord", {"voice"}, voice_off=True) == (False, False)
    assert hh.route_reply(None, {"voice"}, voice_off=True) == (False, False)


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a) -> bool:  # type: ignore[no-untyped-def]
        return False


def test_discord_send_posts_json_and_splits_long_text() -> None:
    calls = []

    def fake_open(req, timeout=10.0):  # type: ignore[no-untyped-def]
        calls.append((req.full_url, req.get_header("Authorization"), json.loads(req.data.decode("utf-8"))))
        return _Resp(json.dumps({"id": str(len(calls))}).encode("utf-8"))

    r = hh.discord_send("TOKEN", "123", "hello", opener=fake_open)
    assert r["ok"] and r["ids"] == ["1"], r
    assert calls[0][0].endswith("/channels/123/messages") and calls[0][1] == "Bot TOKEN"
    assert calls[0][2] == {"content": "hello"}
    r2 = hh.discord_send("TOKEN", "123", "x" * 4000, opener=fake_open)
    assert r2["ok"] and r2["chunks"] == 3, r2


def test_discord_send_never_raises() -> None:
    def boom(req, timeout=10.0):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, io.BytesIO(b""))

    r = hh.discord_send("TOKEN", "123", "hello", opener=boom)
    assert r == {"ok": False, "status": 429, "error": "HTTP 429", "sent": []}, r
    assert hh.discord_send(None, "123", "hello")["ok"] is False
    assert hh.discord_send("TOKEN", "123", "   ")["ok"] is False

    def crash(req, timeout=10.0):  # type: ignore[no-untyped-def]
        raise OSError("network down")

    assert hh.discord_send("TOKEN", "123", "hello", opener=crash)["ok"] is False


def test_messages_read_right() -> None:
    held = hh.format_held_message({"silent_s": 181, "source": "discord", "hint": None})
    assert held.startswith("⏳ Held") and "3m 01s" in held and "queued, not lost" in held
    held2 = hh.format_held_message({"silent_s": 35, "source": "voice", "hint": "429 rate limit"})
    assert "`429 rate limit`" in held2
    rel = hh.format_released_message({"duration_s": 3.4 * 3600})
    assert "3h 24m" in rel
    routed = hh.format_routed_reply("Yes, the stack is fine.", reason="quiet hours, mouth off")
    assert routed.startswith("🔇 (quiet hours, mouth off") and routed.endswith("Yes, the stack is fine.")
    assert hh.human_duration(59) == "59s" and hh.human_duration(60) == "1m" and hh.human_duration(3661) == "1h 01m"


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
