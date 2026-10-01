"""2026-10-01: an API error message on the stream must HOLD immediately (the 15:35->18:40 429 loop)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain.host_hold import HoldDetector  # noqa: E402


def test_api_error_holds_immediately_and_releases_on_real_output():
    t = [1000.0]
    d = HoldDetector(hold_after_s=180, fast_after_s=45, clock=lambda: t[0])
    d.turn_started("discord")
    t[0] += 5
    ev = d.api_error("rate_limit", 429)
    assert ev and ev["event"] == "held" and "rate_limit" in ev["reason"]
    # the periodic check hands the SAME event to the flag/DM path exactly once
    assert d.check() is ev
    assert d.check() is None
    # repeats while held are absorbed and do not count as activity (no false release)
    for _ in range(41):
        t[0] += 20
        assert d.api_error("rate_limit", 429) is None
        assert d.check() is None
    # real output releases
    t[0] += 1
    d.activity()
    rel = d.check()
    assert rel and rel["event"] == "released" and rel["how"] == "output resumed"


def test_api_error_outside_a_turn_is_remembered_but_does_not_hold():
    d = HoldDetector(clock=lambda: 50.0)
    assert d.api_error("overloaded", 529) is None
    assert d.api_error_hint == "overloaded (529)"
    assert d.check() is None
