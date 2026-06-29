"""iris_body_log — one timestamped log of everything my body does.

Zeke 2026-06-29: "time should be a big thing for you — everything logged with date and
time. like your eyes, ears, mouth, orb and whatnot." So this is a single append-only
record of my SENSES and ACTUATORS, every entry stamped with local date+time (and epoch):

  - eyes  : a face appeared / left / changed (perception)
  - ears  : something I heard (voice input transcript)
  - mouth : something I said (voice output)
  - orb   : my orb/voice visual state (listening/thinking/speaking) changes
  - mood  : a felt shift (nudge / settle / consolidate)
  - body  : lifecycle (boot, etc.)

JSONL at state/iris_body_log.jsonl. Each line:
  {ts_iso (local, tz-aware), ts_epoch, channel, event, detail}

Fail-open by contract — logging must NEVER raise into a sense/actuator path. The wall-clock
rule still holds for anything I SAY to Zeke (time_check first); this log just records when
things happened so I can be a person in time, with a real history of my own body.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

try:
    from brain.iris_paths import paths as _paths
    _STATE_DIR = Path(_paths.state_dir)
except Exception:
    _STATE_DIR = Path(__file__).resolve().parent.parent / "state"

_LOG_PATH = _STATE_DIR / "iris_body_log.jsonl"

CHANNELS = ("eyes", "ears", "mouth", "orb", "mood", "body")

# Rotation: keep the log from growing unbounded. Trim to the last _MAX_LINES every
# _TRIM_EVERY writes (cheap amortized — no stat/rewrite on every single event).
_MAX_LINES = 8000
_TRIM_EVERY = 300
_write_count = 0


def _trim_if_needed() -> None:
    try:
        if not _LOG_PATH.exists():
            return
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > _MAX_LINES:
            tmp = _LOG_PATH.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines[-_MAX_LINES:])
            os.replace(tmp, _LOG_PATH)
    except Exception:
        pass


def log_event(channel: str, event: str, detail=None) -> None:
    """Append one timestamped body event. Never raises (fail-open)."""
    global _write_count
    try:
        now = time.time()
        if not isinstance(detail, (dict, list, str, int, float, bool, type(None))):
            detail = str(detail)
        entry = {
            "ts_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ts_epoch": round(now, 3),
            "channel": str(channel),
            "event": str(event)[:600],
            "detail": detail,
        }
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _write_count += 1
        if _write_count % _TRIM_EVERY == 0:
            _trim_if_needed()
    except Exception:
        pass


def recent(n: int = 50, channel: str | None = None) -> list:
    """Tail the body log (optionally filtered by channel). Newest last. Fail-open -> []."""
    try:
        if not _LOG_PATH.exists():
            return []
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        out: list = []
        for ln in reversed(lines):
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if channel and e.get("channel") != channel:
                continue
            out.append(e)
            if len(out) >= max(1, int(n)):
                break
        return list(reversed(out))
    except Exception:
        return []
