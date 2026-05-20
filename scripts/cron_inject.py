"""scripts/cron_inject.py — Task Scheduler -> side-channel prompt writer.

ARCHITECTURE: Windows Task Scheduler fires this script at each block's
exact time. The script looks up the prompt by name and writes it to a
side-channel file at .tmp/cron_inject/<timestamp>_<name>.txt. The
long-running iris_cold_wake.py instance (which owns CC's TTY via pywinpty)
has a background watcher thread that polls .tmp/cron_inject/, reads new
files, and types their contents into CC's TTY via proc.write(). CC sees
keystrokes as user input and starts a turn.

WHY this side-channel design (vs direct SendInput from this script):
Task Scheduler runs tasks in a different Windows desktop session than
the user's interactive logon, so EnumWindows from this script doesn't
see the cmd.exe window hosting iris_cold_wake.py. Falling back to a file
on disk avoids the desktop boundary entirely — Task Scheduler can write
to any path, and iris_cold_wake.py (in the user's interactive session)
reads from the same path and does the TTY write where it already has
access. Diagnosed 2026-05-20 work block.

USAGE:
  python cron_inject.py <prompt_name>

  Available prompt names match brain/ritual_scheduler_prompts.py.

EXIT CODES:
  0 = success (file written to inject directory)
  1 = file write failure
  2 = unknown prompt name

ACTIVATION:
  iris_cold_wake.py's watcher thread is added in the same 2026-05-20 work
  block. The currently-running iris_cold_wake.py won't pick up the new
  code until next CC restart. After that restart, this side-channel path
  is live.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from brain import ritual_scheduler_prompts as prompts  # noqa: E402

INJECT_DIR = REPO_ROOT / ".tmp" / "cron_inject"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: cron_inject.py <prompt_name>", file=sys.stderr)
        print(f"available: {', '.join(prompts.names())}", file=sys.stderr)
        return 2

    name = argv[1]
    try:
        text = prompts.get(name)
    except KeyError:
        print(f"unknown prompt: {name!r}", file=sys.stderr)
        print(f"available: {', '.join(prompts.names())}", file=sys.stderr)
        return 2

    try:
        INJECT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[cron_inject] could not create inject dir: {e!r}", file=sys.stderr)
        return 1

    # Atomic write: write to .tmp file, then rename. Avoids the case where
    # iris_cold_wake.py's watcher reads a partial file mid-write.
    ts = int(time.time() * 1000)  # millisecond resolution for ordering
    final_path = INJECT_DIR / f"{ts:013d}_{name}_{uuid.uuid4().hex[:8]}.txt"
    tmp_path = final_path.with_suffix(".txt.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(final_path)
        print(
            f"[cron_inject] wrote {final_path.name} ({len(text)} chars) "
            f"for prompt {name!r}",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[cron_inject] file write failed: {e!r}", file=sys.stderr)
        # Best-effort cleanup of tmp file.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
