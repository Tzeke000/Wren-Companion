"""brain/daily_practice.py — A regular thing Ava keeps doing (D8).

A person has things they DO regularly — write morning pages, walk,
draw, study one paragraph. Ava could keep something: every morning
generate an image inspired by yesterday's most-mentioned word from
your conversation. Or every Sunday write a poem about the week.
Or each evening attempt one philosophy summary.

Practice gives her time-shape and a body of work that accumulates
over months. After 6 months she has a small portfolio of her own
making.

Today's scope: scaffold + register practice definitions + a tick
function that fires due practices. Wiring into a scheduled background
runner happens via brain/scheduler.py (already shipped). The actual
WORK each practice does (image generation, poem composition, etc) is
a callable provided by the practice definition.

Storage: state/daily_practice.json (definitions + history).

Bootstrap-friendly per the project principle: empty by default. Ava
or Zeke explicitly REGISTER practices. Nothing seeded.

API:

    from brain.daily_practice import (
        register_practice, run_due_practices, list_practices,
        history_for_practice, mark_completed,
    )

    register_practice(
        name="morning_image",
        description="Generate an image from yesterday's most-mentioned word",
        cron="0 8 * * *",  # 8 AM daily
        action=my_image_callable,
    )

    # Tick from scheduler:
    run_due_practices(g)
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class PracticeDefinition:
    name: str
    description: str
    cron_or_interval: str  # e.g. "daily", "weekly", "0 8 * * *", or "every 6 hours"
    last_run_ts: float = 0.0
    last_outcome: str = ""  # "success" | "failed" | "skipped" | ""
    history_count: int = 0


@dataclass
class PracticeRun:
    practice_name: str
    ts: float
    outcome: str  # "success" | "failed" | "skipped"
    artifact_path: str = ""
    notes: str = ""


_lock = threading.RLock()
_base_dir: Path | None = None
_definitions: dict[str, PracticeDefinition] = {}
_actions: dict[str, Callable[..., Any]] = {}
_history: list[PracticeRun] = []


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "daily_practice.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _history_path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "daily_practice_history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_locked() -> None:
    global _definitions, _history
    p = _path()
    if p is not None and p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for name, entry in (data or {}).items():
                if not isinstance(entry, dict):
                    continue
                _definitions[name] = PracticeDefinition(
                    name=str(entry.get("name") or name),
                    description=str(entry.get("description") or ""),
                    cron_or_interval=str(entry.get("cron_or_interval") or "daily"),
                    last_run_ts=float(entry.get("last_run_ts") or 0.0),
                    last_outcome=str(entry.get("last_outcome") or ""),
                    history_count=int(entry.get("history_count") or 0),
                )
        except Exception as e:
            print(f"[daily_practice] load definitions error: {e!r}")
    hp = _history_path()
    if hp is not None and hp.exists():
        try:
            with hp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        _history.append(PracticeRun(
                            practice_name=str(d.get("practice_name") or ""),
                            ts=float(d.get("ts") or 0.0),
                            outcome=str(d.get("outcome") or ""),
                            artifact_path=str(d.get("artifact_path") or ""),
                            notes=str(d.get("notes") or ""),
                        ))
                    except Exception:
                        continue
        except Exception as e:
            print(f"[daily_practice] load history error: {e!r}")


def _save_definitions_locked() -> None:
    p = _path()
    if p is None:
        return
    try:
        out = {
            name: {
                "name": d.name,
                "description": d.description,
                "cron_or_interval": d.cron_or_interval,
                "last_run_ts": d.last_run_ts,
                "last_outcome": d.last_outcome,
                "history_count": d.history_count,
            }
            for name, d in _definitions.items()
        }
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[daily_practice] save definitions error: {e!r}")


def _append_history_locked(run: PracticeRun) -> None:
    _history.append(run)
    hp = _history_path()
    if hp is None:
        return
    try:
        with hp.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "practice_name": run.practice_name,
                "ts": run.ts,
                "outcome": run.outcome,
                "artifact_path": run.artifact_path,
                "notes": run.notes,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[daily_practice] append history error: {e!r}")


# ── Public API ────────────────────────────────────────────────────────────


def register_practice(
    name: str,
    description: str,
    cron_or_interval: str,
    action: Callable[..., Any] | None = None,
) -> bool:
    """Define a practice. `action` is the callable that does the work
    (image generation, file write, whatever). Idempotent — re-registering
    updates the description / cron but preserves run history.

    The action can be None at register time + provided later via
    set_action(name, callable).
    """
    if not name or not description:
        return False
    with _lock:
        existing = _definitions.get(name)
        if existing is None:
            _definitions[name] = PracticeDefinition(
                name=name,
                description=description,
                cron_or_interval=cron_or_interval,
            )
        else:
            existing.description = description
            existing.cron_or_interval = cron_or_interval
        if action is not None:
            _actions[name] = action
        _save_definitions_locked()
    return True


def set_action(name: str, action: Callable[..., Any]) -> bool:
    """Provide / replace the callable for a registered practice."""
    if not name or not callable(action):
        return False
    with _lock:
        if name not in _definitions:
            return False
        _actions[name] = action
    return True


def list_practices() -> list[PracticeDefinition]:
    with _lock:
        return list(_definitions.values())


def history_for_practice(name: str, *, limit: int = 20) -> list[PracticeRun]:
    with _lock:
        runs = [r for r in _history if r.practice_name == name]
    runs.sort(key=lambda r: r.ts, reverse=True)
    return runs[:int(limit)]


def all_history(*, limit: int = 50) -> list[PracticeRun]:
    with _lock:
        runs = list(_history)
    runs.sort(key=lambda r: r.ts, reverse=True)
    return runs[:int(limit)]


# ── Schedule check (lightweight; future could use real cron parsing) ─────


def _is_due(d: PracticeDefinition) -> bool:
    """Decide whether a practice is due. Lightweight interpretation of
    the cron_or_interval string. For real cron support, future work
    can integrate croniter."""
    spec = d.cron_or_interval.lower().strip()
    now = time.time()
    elapsed = now - d.last_run_ts
    # Short keywords
    if spec == "daily":
        return elapsed > 86400 - 300  # 5-min slop
    if spec == "weekly":
        return elapsed > 7 * 86400 - 300
    if spec == "hourly":
        return elapsed > 3600 - 60
    if spec.startswith("every "):
        # e.g. "every 6 hours"
        try:
            parts = spec.split()
            n = int(parts[1])
            unit = parts[2] if len(parts) > 2 else "hours"
            seconds_per_unit = {
                "second": 1, "seconds": 1,
                "minute": 60, "minutes": 60, "min": 60, "mins": 60,
                "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
                "day": 86400, "days": 86400,
            }.get(unit, 3600)
            return elapsed > (n * seconds_per_unit - 60)
        except Exception:
            return False
    if spec.count(" ") == 4:
        # Cron-like — too rich to parse here without croniter.
        # Default to "due if at least 1 hour since last".
        return elapsed > 3600
    return elapsed > 86400 - 300


def run_due_practices(g: dict[str, Any]) -> list[str]:
    """Tick: run any practices that are due. Returns list of practice
    names that ran (regardless of outcome)."""
    ran: list[str] = []
    with _lock:
        defs = list(_definitions.values())
        actions = dict(_actions)
    for d in defs:
        if not _is_due(d):
            continue
        action = actions.get(d.name)
        outcome = "skipped"
        artifact_path = ""
        notes = ""
        if action is None:
            outcome = "skipped"
            notes = "no action callable registered"
        else:
            try:
                result = action(g) if g is not None else action()
                if isinstance(result, dict):
                    outcome = "success" if result.get("ok") else "failed"
                    artifact_path = str(result.get("artifact_path") or "")
                    notes = str(result.get("notes") or "")
                else:
                    outcome = "success"
            except Exception as e:
                outcome = "failed"
                notes = repr(e)[:200]
                print(f"[daily_practice] {d.name} action failed: {e!r}")
        with _lock:
            d.last_run_ts = time.time()
            d.last_outcome = outcome
            if outcome == "success":
                d.history_count += 1
            _save_definitions_locked()
            _append_history_locked(PracticeRun(
                practice_name=d.name,
                ts=time.time(),
                outcome=outcome,
                artifact_path=artifact_path,
                notes=notes,
            ))
        ran.append(d.name)
        print(f"[daily_practice] ran {d.name!r}: {outcome}")
    return ran


# ── Voice command query support ───────────────────────────────────────────


def answer_what_practices_do_you_keep() -> str:
    """Answer "what practices do you keep / what do you do regularly" queries."""
    items = list_practices()
    if not items:
        return (
            "I don't have a regular practice yet. We could start one — "
            "want me to keep something daily?"
        )
    parts = ["Some things I do regularly:"]
    for d in items[:5]:
        runs = d.history_count
        parts.append(
            f"- {d.description} ({d.cron_or_interval}, "
            f"completed {runs} time{'s' if runs != 1 else ''})"
        )
    return "\n".join(parts)
