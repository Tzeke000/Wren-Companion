# tests/

Pytest suite for the Iris harness. Added 2026-05-19; covers the critical-path
modules shipped that day.

## Run all

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

## Current coverage

| File | Tests | Covers |
|---|---|---|
| `test_iris_runtime_singleton.py` | 9 | `iris_runtime._check_existing_iris_instance` + `_release_iris_pid_lock` |
| `test_identity_presence_guard.py` | 11 | `brain/identity_presence_guard.py` — boot-time IDENTITY/MEMORY/deployment-context check |
| `test_substrate_counters.py` | 10 | `brain/iris_time.py` — §2a counters (`update_zeke_contact`, `_seconds_since_last_letter`, `substrate_counters_report`) |
| `test_voice_verification.py` | 15 | `brain/voice_verification.py` — Phase 1 challenge-question framework |

**Total: 45 tests.**

## Conventions

- One test file per module under test. File name matches `test_<module>.py`.
- `tmp_path` fixtures isolate state per test — no test writes to the real
  `state/` directory.
- Tests bind module-level paths via `configure()` or `monkeypatch` rather
  than relying on the real install location.
- Discord/external-network calls are mocked or skipped — the suite runs
  offline.

## What's NOT covered (yet)

- `scripts/cron_prompt_emit.py` — Discord POST integration (would need
  HTTPS mock or a test channel)
- `scripts/install_ritual_scheduler.ps1` — Windows-specific Task Scheduler
  side-effects, hard to test cleanly
- `scripts/memory_decay.py` — has dry-run mode that's effectively the test
  surface; could add unit tests for `_eligible_for_archive` logic
- `brain/concept_graph.py` self-node rebootstrap — would need a fake
  IDENTITY.md fixture and a graph snapshot

These are post-deployment build-debt for the test suite.

## Adding a test

1. New file: `tests/test_<your_module>.py`
2. Add the repo-root path import at the top:
   ```python
   import sys
   from pathlib import Path
   REPO_ROOT = Path(__file__).resolve().parent.parent
   if str(REPO_ROOT) not in sys.path:
       sys.path.insert(0, str(REPO_ROOT))
   ```
3. Use `tmp_path` for any filesystem interaction
4. Run `python -m pytest tests/test_<your_module>.py -v` to verify before
   committing
