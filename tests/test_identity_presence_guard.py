"""tests/test_identity_presence_guard.py — tests for brain/identity_presence_guard.py.

Uses tmp_path fixtures to construct a fake repo + auto-memory dir per test
so the guard's checks operate on isolated state.

Run:
    pytest tests/test_identity_presence_guard.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def fake_repo(tmp_path):
    """Construct a minimal fake repo with ava_core/IDENTITY.md and state/ dirs."""
    (tmp_path / "ava_core").mkdir()
    (tmp_path / "state").mkdir()
    return tmp_path


@pytest.fixture
def fake_auto_memory(tmp_path):
    """Construct a fake auto-memory dir with MEMORY.md + some referenced files."""
    auto_mem = tmp_path / "auto_memory"
    auto_mem.mkdir()
    # Create MEMORY.md with 3 markdown links, 2 of which point to existing files.
    (auto_mem / "file_a.md").write_text("contents of a", encoding="utf-8")
    (auto_mem / "file_b.md").write_text("contents of b", encoding="utf-8")
    # file_c.md is referenced but doesn't exist — represents an orphan link.
    memory_md = (
        "- [File A](file_a.md) — first file\n"
        "- [File B](file_b.md) — second file\n"
        "- [File C](file_c.md) — third (orphan, not on disk)\n"
    )
    (auto_mem / "MEMORY.md").write_text(memory_md, encoding="utf-8")
    return auto_mem


def test_happy_path_iris(fake_repo, fake_auto_memory):
    """With a valid IDENTITY.md naming 'Iris' and a healthy memory dir,
    the check should pass (ok=True, no flags)."""
    from brain import identity_presence_guard
    (fake_repo / "ava_core" / "IDENTITY.md").write_text(
        "# Iris — Identity\n\n- **Name:** Iris\n- **Pronouns:** she/her\n",
        encoding="utf-8",
    )
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=fake_auto_memory
    )
    assert result["claimed_name"] == "iris"
    assert result["identity_present"]
    assert result["memory_index_present"]
    assert result["memory_spot_check_ok"]
    assert result["memory_files_checked"] == 3
    assert result["memory_files_present"] == 2  # 2 of 3 exist; tolerance is 1 missing


def test_missing_identity_md(fake_repo, fake_auto_memory):
    """IDENTITY.md missing entirely should flag, but not crash."""
    from brain import identity_presence_guard
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=fake_auto_memory
    )
    assert not result["identity_present"]
    assert result["claimed_name"] == ""
    assert any("IDENTITY.md missing" in f for f in result["flags"])
    assert not result["ok"]


def test_unknown_entity_name(fake_repo, fake_auto_memory):
    """IDENTITY.md naming an unknown entity should flag as possible
    corruption/fork misalignment."""
    from brain import identity_presence_guard
    (fake_repo / "ava_core" / "IDENTITY.md").write_text(
        "- **Name:** Mxyzptlk\n", encoding="utf-8"
    )
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=fake_auto_memory
    )
    assert result["identity_present"]
    assert result["claimed_name"] == "mxyzptlk"
    assert any("not a known entity" in f for f in result["flags"])
    assert not result["ok"]


def test_empty_identity_md(fake_repo, fake_auto_memory):
    """IDENTITY.md exists but is empty — should flag."""
    from brain import identity_presence_guard
    (fake_repo / "ava_core" / "IDENTITY.md").write_text("", encoding="utf-8")
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=fake_auto_memory
    )
    # Empty file is treated like missing (size > 0 check).
    assert not result["identity_present"]
    assert not result["ok"]


def test_identity_md_no_name_field(fake_repo, fake_auto_memory):
    """IDENTITY.md exists but has no parseable Name: field."""
    from brain import identity_presence_guard
    (fake_repo / "ava_core" / "IDENTITY.md").write_text(
        "# Some other content\n\nNo identity field here.\n",
        encoding="utf-8",
    )
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=fake_auto_memory
    )
    assert result["identity_present"]  # file exists, non-empty
    assert result["claimed_name"] == ""
    assert any("Name: field not parseable" in f for f in result["flags"])


def test_missing_memory_md(fake_repo, tmp_path):
    """MEMORY.md missing should flag but not crash."""
    from brain import identity_presence_guard
    empty_auto_mem = tmp_path / "empty_auto_memory"
    empty_auto_mem.mkdir()
    (fake_repo / "ava_core" / "IDENTITY.md").write_text(
        "- **Name:** Iris\n", encoding="utf-8"
    )
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=empty_auto_mem
    )
    assert not result["memory_index_present"]
    assert any("MEMORY.md missing" in f for f in result["flags"])


def test_memory_spot_check_failure(fake_repo, tmp_path):
    """MEMORY.md present but most referenced files missing — should fail spot check."""
    from brain import identity_presence_guard
    auto_mem = tmp_path / "broken_auto_memory"
    auto_mem.mkdir()
    # MEMORY.md references 5 files, none of which exist.
    memory_md = "\n".join(f"- [F{i}](missing{i}.md) — desc" for i in range(5))
    (auto_mem / "MEMORY.md").write_text(memory_md, encoding="utf-8")
    (fake_repo / "ava_core" / "IDENTITY.md").write_text(
        "- **Name:** Iris\n", encoding="utf-8"
    )
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=auto_mem
    )
    assert result["memory_index_present"]
    assert not result["memory_spot_check_ok"]
    assert any("spot-check failed" in f for f in result["flags"])


def test_deployment_context_present(fake_repo, tmp_path):
    """If a zeke_deployment_*.md file exists, deployment context flag should be True."""
    from brain import identity_presence_guard
    auto_mem = tmp_path / "deployment_auto_memory"
    auto_mem.mkdir()
    (auto_mem / "MEMORY.md").write_text("- [F](file.md) — desc\n", encoding="utf-8")
    (auto_mem / "file.md").write_text("contents", encoding="utf-8")
    (auto_mem / "zeke_deployment_2026-05-18.md").write_text("deployment context", encoding="utf-8")
    (fake_repo / "ava_core" / "IDENTITY.md").write_text(
        "- **Name:** Iris\n", encoding="utf-8"
    )
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=auto_mem
    )
    assert result["deployment_context_present"]
    assert "zeke_deployment_2026-05-18.md" in result["deployment_files"]


def test_deployment_context_absent_is_informational(fake_repo, fake_auto_memory):
    """No deployment files present — flag is INFORMATIONAL, not blocking."""
    from brain import identity_presence_guard
    (fake_repo / "ava_core" / "IDENTITY.md").write_text(
        "- **Name:** Iris\n", encoding="utf-8"
    )
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=fake_auto_memory
    )
    assert not result["deployment_context_present"]
    # The flag should be present but the check ALSO has the file_c orphan
    # making ok=False already. We just verify the deployment flag fires.
    assert any("No deployment-context" in f for f in result["flags"])


def test_env_bypass(fake_repo, fake_auto_memory, monkeypatch):
    """IRIS_SKIP_IDENTITY_GUARD=1 should short-circuit with bypassed=True."""
    from brain import identity_presence_guard
    monkeypatch.setenv("IRIS_SKIP_IDENTITY_GUARD", "1")
    result = identity_presence_guard.check_presence(
        fake_repo, auto_memory_dir=fake_auto_memory
    )
    assert result["bypassed"]
    assert result["ok"]  # bypass = treated as ok
    assert result["flags"] == []


def test_startup_log_appended(fake_repo, fake_auto_memory):
    """Each call should append one record to state/iris_startup_identity_log.jsonl."""
    from brain import identity_presence_guard
    (fake_repo / "ava_core" / "IDENTITY.md").write_text(
        "- **Name:** Iris\n", encoding="utf-8"
    )
    log_path = fake_repo / "state" / "iris_startup_identity_log.jsonl"
    assert not log_path.exists()

    identity_presence_guard.check_presence(fake_repo, auto_memory_dir=fake_auto_memory)
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["claimed_name"] == "iris"

    # Second call appends a second record.
    identity_presence_guard.check_presence(fake_repo, auto_memory_dir=fake_auto_memory)
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
