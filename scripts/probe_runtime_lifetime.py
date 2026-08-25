"""Decisive test for the 2026-08-25 'iris_runtime does not persist' bug.

Spawns iris_runtime.py exactly the way .mcp.json does, holds stdin OPEN, and
does nothing else. Two possible outcomes and they mean opposite things:

  * process is still alive after the watch window  -> it does NOT self-exit;
    something on the client side is closing the pipe / killing it.
  * process exits on its own                       -> the reason is in its
    stderr, which we capture and print.

Read-only w.r.t. the running stack: it starts a SECOND runtime, so anything
singleton-guarded (port 5876, pid lock) may refuse — that refusal IS a result
and gets printed, not swallowed.
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(r"D:\Wren-Companion")
PY = REPO / ".venv" / "Scripts" / "python.exe"
WATCH_S = 45

out_lines: list[str] = []


def drain(stream, tag):
    for raw in iter(stream.readline, b""):
        out_lines.append(f"[{tag}] " + raw.decode("utf-8", "replace").rstrip())


p = subprocess.Popen(
    [str(PY), str(REPO / "iris_runtime.py")],
    cwd=str(REPO),
    stdin=subprocess.PIPE,       # held OPEN for the whole run — never closed
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env={**__import__("os").environ, "AVA_STT_STREAMING": "1"},
)
threading.Thread(target=drain, args=(p.stderr, "err"), daemon=True).start()
threading.Thread(target=drain, args=(p.stdout, "out"), daemon=True).start()

print(f"spawned pid={p.pid}; watching {WATCH_S}s with stdin held open", flush=True)
t0 = time.time()
exit_at = None
while time.time() - t0 < WATCH_S:
    if p.poll() is not None:
        exit_at = time.time() - t0
        break
    time.sleep(0.5)

if exit_at is None:
    print(f"RESULT: STILL ALIVE after {WATCH_S}s (rc=None)")
    print("=> it does NOT self-exit. Something client-side ends it.")
    p.kill()
else:
    print(f"RESULT: SELF-EXITED after {exit_at:.1f}s with returncode={p.returncode}")
    print("=> the reason should be in its own output below.")

time.sleep(1.0)
print(f"--- {len(out_lines)} output lines ---")
for ln in out_lines[-60:]:
    print(ln)
