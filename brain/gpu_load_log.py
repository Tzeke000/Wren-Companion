"""gpu_load_log — every GPU model load logs its name + VRAM delta.

WHY (2026-08-26, owed since the 08-25 crash night): at ~21:55 on 08-25 an
un-attributed +3.3 GB VRAM jump landed, VRAM hit 98%, and at 22:28 the runtime
process died outright — the 4th crash that day, best theory CUDA OOM. Nobody
could say WHAT loaded, because nothing recorded loads. This module makes the
next spike name itself: wrap any model load in `with logged_load("name"):` and
the load appends {model, vram_before, vram_after, delta, load_s} to
state/gpu_model_loads.jsonl.

Design constraints:
- FAIL-OPEN everywhere: instrumentation must never break a load. Any error in
  here degrades to "no record", never to an exception in the caller.
- nvidia-smi (~50-100ms) is called twice per load. Loads are rare (once per
  model per process lifetime); this is free. It measures WHOLE-DEVICE usage,
  which is the number that OOMs, not just this process's torch pool.
- Reading the log: each line is one JSON record, newest last.
    Get-Content state\gpu_model_loads.jsonl -Tail 20
"""
from __future__ import annotations

import json
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

_LOG = Path(__file__).resolve().parent.parent / "state" / "gpu_model_loads.jsonl"


def vram_mb() -> int | None:
    """Whole-device VRAM in use, MiB. None = couldn't measure (fail-open)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


@contextmanager
def logged_load(name: str):
    """Wrap a model load; append a VRAM-delta record when it finishes.

    Records even when the load raises (the record notes ok=False) — a FAILED
    load that still allocated is exactly the kind of ghost worth catching.
    """
    before = vram_mb()
    t0 = time.time()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        try:
            after = vram_mb()
            # Attribution (added 2026-08-27): the night the shadow-Ava stack
            # triple-booted, nobody could say WHICH process/thread loaded
            # whisper 4×. pid + argv0 + thread name make the next stack
            # name its own loader.
            import os as _os
            import sys as _sys
            import threading as _threading
            rec = {
                "ts": round(time.time(), 2),
                "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": name,
                "pid": _os.getpid(),
                "proc": (_sys.argv[0].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                         if _sys.argv else "?"),
                "thread": _threading.current_thread().name,
                "ok": ok,
                "vram_before_mb": before,
                "vram_after_mb": after,
                "delta_mb": (after - before)
                if (before is not None and after is not None) else None,
                "load_s": round(time.time() - t0, 2),
            }
            _LOG.parent.mkdir(parents=True, exist_ok=True)
            with _LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass  # never let bookkeeping break the caller
