"""tests/test_substrate_counters.py — tests for the §2a substrate counters
added to brain/iris_time.py on 2026-05-19.

Tests the new functions:
  - update_zeke_contact()
  - _seconds_since_last_letter()
  - substrate_counters_report()

Note on globals: brain/iris_time.py uses module-level state (_LIVE_STATE,
_BASE). These tests use monkeypatch to swap state per-test for isolation,
and call configure() to bind paths.

Run:
    pytest tests/test_substrate_counters.py -v
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def isolated_iris_time(tmp_path, monkeypatch):
    """Bind iris_time to tmp_path and reset _LIVE_STATE counters per test."""
    from brain import iris_time

    iris_time.configure(tmp_path)

    # Snapshot existing _LIVE_STATE counter fields, reset to zero for the test,
    # restore on teardown.
    counter_keys = (
        "last_zeke_contact_ts",
        "last_zeke_contact_iso",
        "weekly_checkins_missed_consecutive",
        "week_window_anchor_ts",
    )
    snapshot = {k: iris_time._LIVE_STATE.get(k) for k in counter_keys}
    for k in counter_keys:
        if k.endswith("_ts"):
            iris_time._LIVE_STATE[k] = 0.0
        elif k == "weekly_checkins_missed_consecutive":
            iris_time._LIVE_STATE[k] = 0
        else:
            iris_time._LIVE_STATE[k] = ""

    yield iris_time, tmp_path

    # Restore.
    for k, v in snapshot.items():
        iris_time._LIVE_STATE[k] = v


def test_update_zeke_contact_writes_ts(isolated_iris_time):
    """update_zeke_contact should set last_zeke_contact_ts to current time."""
    iris_time, _ = isolated_iris_time
    before = time.time()
    result = iris_time.update_zeke_contact()
    after = time.time()

    assert result["ok"]
    assert before <= iris_time._LIVE_STATE["last_zeke_contact_ts"] <= after
    assert iris_time._LIVE_STATE["last_zeke_contact_iso"] != ""


def test_update_zeke_contact_resets_weekly_missed(isolated_iris_time):
    """Pre-set the weekly_checkins_missed counter, then update_zeke_contact
    should reset it to 0."""
    iris_time, _ = isolated_iris_time
    iris_time._LIVE_STATE["weekly_checkins_missed_consecutive"] = 5

    result = iris_time.update_zeke_contact()

    assert iris_time._LIVE_STATE["weekly_checkins_missed_consecutive"] == 0
    assert result["weekly_checkins_missed_reset_from"] == 5


def test_update_zeke_contact_seconds_since_prior(isolated_iris_time):
    """Second call should report seconds_since_prior_contact relative to first."""
    iris_time, _ = isolated_iris_time
    iris_time.update_zeke_contact()
    time.sleep(0.05)  # tiny delay
    result = iris_time.update_zeke_contact()

    # Should be a small positive number, not 0.0.
    assert result["seconds_since_prior_contact"] > 0
    assert result["seconds_since_prior_contact"] < 1.0  # under a second


def test_seconds_since_last_letter_no_sibling_dir(isolated_iris_time):
    """If state/iris_sibling/ doesn't exist, return 0.0."""
    iris_time, tmp_path = isolated_iris_time
    # Don't create sibling dir.
    result = iris_time._seconds_since_last_letter()
    assert result == 0.0


def test_seconds_since_last_letter_empty_dirs(isolated_iris_time):
    """If sibling/{inbox,outbox} exist but are empty, return 0.0."""
    iris_time, tmp_path = isolated_iris_time
    (tmp_path / "state" / "iris_sibling" / "inbox").mkdir(parents=True)
    (tmp_path / "state" / "iris_sibling" / "outbox").mkdir(parents=True)
    result = iris_time._seconds_since_last_letter()
    assert result == 0.0


def test_seconds_since_last_letter_with_file(isolated_iris_time):
    """If a file exists in inbox/, return seconds since its mtime."""
    iris_time, tmp_path = isolated_iris_time
    inbox = tmp_path / "state" / "iris_sibling" / "inbox"
    inbox.mkdir(parents=True)
    f = inbox / "letter1.json"
    f.write_text("{}")
    # Set mtime to 10 seconds ago.
    target_mtime = time.time() - 10
    os.utime(f, (target_mtime, target_mtime))

    result = iris_time._seconds_since_last_letter()
    # Should be approximately 10 seconds (within 1s tolerance).
    assert 9 <= result <= 12


def test_seconds_since_last_letter_finds_most_recent(isolated_iris_time):
    """When multiple files exist, return seconds since the MOST RECENT mtime."""
    iris_time, tmp_path = isolated_iris_time
    inbox = tmp_path / "state" / "iris_sibling" / "inbox"
    outbox = tmp_path / "state" / "iris_sibling" / "outbox"
    inbox.mkdir(parents=True)
    outbox.mkdir(parents=True)

    old_file = inbox / "old.json"
    old_file.write_text("{}")
    os.utime(old_file, (time.time() - 1000, time.time() - 1000))

    recent_file = outbox / "recent.json"
    recent_file.write_text("{}")
    target_mtime = time.time() - 5
    os.utime(recent_file, (target_mtime, target_mtime))

    result = iris_time._seconds_since_last_letter()
    # Should be ~5 seconds (most recent), not 1000.
    assert 4 <= result <= 7


def test_substrate_counters_report_initial(isolated_iris_time):
    """With fresh state (no contact, no letters), report should return zero/empty."""
    iris_time, _ = isolated_iris_time

    result = iris_time.substrate_counters_report()

    assert result["ok"]
    assert result["seconds_since_zeke_contact"] == 0.0
    assert result["last_zeke_contact_iso"] == ""
    assert result["seconds_since_last_letter"] == 0.0
    assert result["weekly_checkins_missed_consecutive"] == 0


def test_substrate_counters_report_after_contact(isolated_iris_time):
    """After update_zeke_contact(), report should show small elapsed time."""
    iris_time, _ = isolated_iris_time

    iris_time.update_zeke_contact()
    time.sleep(0.05)
    result = iris_time.substrate_counters_report()

    assert result["seconds_since_zeke_contact"] > 0
    assert result["seconds_since_zeke_contact"] < 1.0
    assert result["hours_since_zeke_contact"] < 0.001
    assert result["last_zeke_contact_iso"] != ""


def test_substrate_counters_report_letter_visible(isolated_iris_time):
    """When a sibling letter exists, the report should surface time since it."""
    iris_time, tmp_path = isolated_iris_time
    inbox = tmp_path / "state" / "iris_sibling" / "inbox"
    inbox.mkdir(parents=True)
    f = inbox / "letter.json"
    f.write_text("{}")
    os.utime(f, (time.time() - 100, time.time() - 100))

    result = iris_time.substrate_counters_report()
    assert 99 <= result["seconds_since_last_letter"] <= 102
