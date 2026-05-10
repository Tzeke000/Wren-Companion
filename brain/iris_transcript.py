"""
brain/iris_transcript.py — shared conversation log.

One durable JSONL at state/transcript.jsonl. Both voice and chat write here
so the orb's history view is unified across modalities.

Schema per line:
  {"id": "<12-hex>", "ts": <unix>, "iso": "<rfc3339>",
   "role": "user"|"assistant",
   "source": "zeke"|"iris",
   "modality": "voice"|"chat",
   "content": "<text>"}

Append-only. Bounded by trim_to_last(N) on read paths — we keep all turns on
disk forever (cheap), the orb just gets the most recent slice.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


_DEFAULT_PATH = "state/transcript.jsonl"
_LOCK = threading.Lock()
_BASE: Path | None = None


def configure(base_dir: Path | str) -> None:
    global _BASE
    _BASE = Path(base_dir)
    (_BASE / "state").mkdir(parents=True, exist_ok=True)


def _path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / _DEFAULT_PATH


def append(
    role: str,
    content: str,
    source: str,
    modality: str,
) -> dict[str, Any]:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "iso": datetime.now().isoformat(timespec="seconds"),
        "role": str(role),
        "source": str(source),
        "modality": str(modality),
        "content": str(content or "")[:8000],
    }
    p = _path()
    with _LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def recent(n: int = 100) -> list[dict[str, Any]]:
    p = _path()
    if not p.is_file():
        return []
    with _LOCK:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, int(n)) :]:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if isinstance(e, dict):
                out.append(e)
        except Exception:
            pass
    return out
