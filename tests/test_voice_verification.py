"""tests/test_voice_verification.py — tests for brain/voice_verification.py.

Uses tmp_path fixtures to isolate state files per test.

Run:
    pytest tests/test_voice_verification.py -v
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def isolated_vv(tmp_path):
    """Bind voice_verification to a tmp dir per test."""
    from brain import voice_verification as vv
    vv.configure(tmp_path)
    return vv, tmp_path


def test_seed_challenges_on_first_get(isolated_vv):
    """First get_challenge() call should auto-seed the three default challenges."""
    vv, tmp_path = isolated_vv
    assert not (tmp_path / "state" / "voice_verification_challenges.json").is_file()
    result = vv.get_challenge()
    assert result["ok"]
    assert result["id"] in {"sibling-first", "mother-name", "current-mos"}
    # File should now exist with 3 challenges.
    assert (tmp_path / "state" / "voice_verification_challenges.json").is_file()


def test_get_challenge_by_category(isolated_vv):
    """Filter by category should return only matching challenges."""
    vv, _ = isolated_vv
    result = vv.get_challenge(category="usmc")
    assert result["ok"]
    assert result["id"] == "current-mos"
    assert result["category"] == "usmc"


def test_get_challenge_unknown_category(isolated_vv):
    """Unknown category should return ok=False."""
    vv, _ = isolated_vv
    vv.get_challenge()  # trigger seeding
    result = vv.get_challenge(category="nonexistent")
    assert not result["ok"]


def test_verify_correct_answer(isolated_vv):
    """Correct answer should match and update freshness."""
    vv, _ = isolated_vv
    vv.get_challenge()  # seed
    result = vv.verify_answer("current-mos", "5954")
    assert result["matched"]
    assert result["attempts_remaining"] == 3
    fresh = vv.get_verification_freshness()
    assert fresh["last_verified_ts"] > 0
    assert fresh["last_source"] == "challenge"


def test_verify_correct_answer_normalized(isolated_vv):
    """Answer normalization: case + whitespace should be ignored."""
    vv, _ = isolated_vv
    vv.get_challenge()  # seed
    result = vv.verify_answer("sibling-first", "  AVA  ")
    assert result["matched"]


def test_verify_wrong_answer_increments_failures(isolated_vv):
    """Wrong answer should NOT match, and failure count should increment."""
    vv, _ = isolated_vv
    vv.get_challenge()  # seed
    result = vv.verify_answer("current-mos", "wrong-mos")
    assert not result["matched"]
    assert result["attempts_remaining"] == 2  # 3 - 1


def test_lockout_after_three_failures(isolated_vv):
    """Three consecutive wrong answers should trigger lockout."""
    vv, _ = isolated_vv
    vv.get_challenge()  # seed
    for i in range(3):
        result = vv.verify_answer("current-mos", "wrong")
    # 4th attempt should be locked out.
    final = vv.verify_answer("current-mos", "5954")
    assert final["locked_out"]
    assert not final["matched"]  # locked out, didn't even attempt


def test_correct_answer_resets_failure_count(isolated_vv):
    """A correct answer should reset consecutive_failures to 0."""
    vv, _ = isolated_vv
    vv.get_challenge()  # seed
    vv.verify_answer("current-mos", "wrong")  # fail 1
    vv.verify_answer("current-mos", "wrong")  # fail 2
    # Now succeed.
    result = vv.verify_answer("current-mos", "5954")
    assert result["matched"]
    assert result["attempts_remaining"] == 3  # reset


def test_verify_unknown_challenge_id(isolated_vv):
    """Unknown challenge id should return ok=False, error."""
    vv, _ = isolated_vv
    vv.get_challenge()  # seed
    result = vv.verify_answer("totally-fake-id", "anything")
    assert not result["ok"]


def test_freshness_no_prior_verification(isolated_vv):
    """Before any verify, freshness should report last_verified_ts=0."""
    vv, _ = isolated_vv
    fresh = vv.get_verification_freshness()
    assert fresh["last_verified_ts"] == 0.0
    assert fresh["last_verified_iso"] == ""


def test_freshness_after_verify(isolated_vv):
    """After successful verify, freshness should show recent time + reverify=False."""
    vv, _ = isolated_vv
    vv.get_challenge()  # seed
    vv.verify_answer("current-mos", "5954")
    fresh = vv.get_verification_freshness()
    assert fresh["minutes_since"] < 1.0
    assert not fresh["requires_reverify_for_sensitive"]


def test_record_verification_state_external(isolated_vv):
    """External verify (e.g., Layer 1 confidence) should update freshness."""
    vv, _ = isolated_vv
    vv.record_verification_state(verified=True, source="ecapa-tdnn")
    fresh = vv.get_verification_freshness()
    assert fresh["last_verified_ts"] > 0
    assert fresh["last_source"] == "ecapa-tdnn"


def test_add_challenge_new(isolated_vv):
    """add_challenge should add a new challenge to the registry."""
    vv, _ = isolated_vv
    result = vv.add_challenge(
        question="What is my callsign?",
        answer="charlie",
        category="usmc",
        notes="test challenge",
    )
    assert result["ok"]
    # Should now be retrievable.
    c = vv.get_challenge(category="usmc")
    # Either current-mos or the new one — depends on random pick. Try a few.
    found_new = False
    for _ in range(20):
        c = vv.get_challenge(category="usmc")
        if c["id"] == result["id"]:
            found_new = True
            break
    assert found_new


def test_add_challenge_duplicate_id_rejected(isolated_vv):
    """add_challenge with duplicate id should be rejected."""
    vv, _ = isolated_vv
    vv.add_challenge("Question one", "answer", challenge_id="dup")
    result = vv.add_challenge("Question two", "answer2", challenge_id="dup")
    assert not result["ok"]


def test_answer_not_in_returned_challenge(isolated_vv):
    """get_challenge should NOT return the answer_hash in its result."""
    vv, _ = isolated_vv
    result = vv.get_challenge()
    assert "answer_hash" not in result
    assert "answer" not in result
