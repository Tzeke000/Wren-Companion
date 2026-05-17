"""scripts/iris_cold_wake.py — pywinpty-based cold-wake launcher for Claude Code.

Spawned by start_iris.bat. Owns the pseudo-terminal that hosts a fresh CC
session, so we can inject keystrokes into CC's stdin without the focus-race
or window-manager issues of SendKeys/SendInput against Windows Terminal.

The sequence after spawn:
  1. Wait for CC to display the experimental-channels confirmation prompt
  2. Send Enter to accept
  3. Wait briefly, send Enter again to accept the custom-channel prompt
  4. Wait for CC's interactive REPL to be ready
  5. Type a memory-read directive and submit it as the first message

Per agent research 2026-05-17:
  - Anthropic does NOT expose config to suppress the dangerously-load
    confirmation or the channel-allowlist prompts. Pty injection is the
    only workaround.
  - CC accepts a trailing positional [prompt] argument that becomes the
    first interactive message after channel prompts resolve. We try that
    path first (Path A); if CC doesn't consume the positional arg the way
    we expect, we fall through and type the message ourselves (Path B).

This script BLOCKS until CC exits. The .bat invokes it and gets the same
foreground lifecycle as if it had launched claude directly.
"""
from __future__ import annotations
import os
import sys
import time

try:
    import winpty  # type: ignore
except ImportError:
    print("[iris_cold_wake] ERROR: winpty not installed. "
          "Run: .venv/Scripts/python.exe -m pip install pywinpty",
          file=sys.stderr, flush=True)
    sys.exit(2)

# ── tunables ────────────────────────────────────────────────────────────────
PROMPT_1_WAIT_S = 2.5     # time from spawn to the first (experimental) prompt
PROMPT_2_WAIT_S = 1.5     # time from accept-1 to the second (channel) prompt
READY_WAIT_S    = 2.5     # time from accept-2 to chat-ready
PTY_COLS        = 200
PTY_ROWS        = 50

# Use Path A (positional [prompt] arg). If CC swallows it before the channel
# prompts resolve, set USE_PATH_A = False and the script will type the
# message after the prompts instead.
USE_PATH_A   = True
FIRST_MSG    = "Please read all of your memories before responding to me. Not just the auto-memory index — open the most recent 4-6 memory files in C:\\Users\\Owner\\.claude\\projects\\D--Wren-Companion\\memory\\ by mtime, and check MEMORY.md, then bring that context to the conversation."

# ── launcher ────────────────────────────────────────────────────────────────
def _build_cmd() -> str:
    """Compose the claude command string. Anchored to D:\\Wren-Companion so
    CC reads CLAUDE.md and .claude/settings.local.json from this project.
    """
    # The .bat already cd'd here, but we set cwd via PtyProcess for safety.
    base = (
        'claude '
        '--dangerously-skip-permissions '
        '--dangerously-load-development-channels server:iris '
        '--channels plugin:discord@claude-plugins-official server:iris'
    )
    if USE_PATH_A:
        # Path A: positional prompt that CC consumes as first interactive
        # message after prompts resolve. Shell-quote the message; pywinpty
        # passes it through cmd.exe-equivalent parsing.
        escaped = FIRST_MSG.replace('"', '\\"')
        return f'{base} "{escaped}"'
    return base


def main() -> int:
    cmd = _build_cmd()
    cwd = r'D:\Wren-Companion'
    print(f"[iris_cold_wake] spawning: {cmd[:80]}...", file=sys.stderr, flush=True)
    print(f"[iris_cold_wake] cwd={cwd}", file=sys.stderr, flush=True)

    # Env: inherit + force AVA_TTS_ENGINE so the spawned iris_runtime
    # picks xtts (the .bat used to set this; now the python launcher does).
    env = dict(os.environ)
    env["AVA_TTS_ENGINE"] = "xtts"

    try:
        proc = winpty.PtyProcess.spawn(
            cmd,
            dimensions=(PTY_ROWS, PTY_COLS),
            cwd=cwd,
            env=env,
        )
    except Exception as e:
        print(f"[iris_cold_wake] spawn failed: {e!r}", file=sys.stderr, flush=True)
        return 3

    print(f"[iris_cold_wake] proc spawned (pid={proc.pid})", file=sys.stderr, flush=True)

    # ── pump the prompts ────────────────────────────────────────────────
    # We're not parsing CC's output to detect prompts — that's fragile and
    # version-dependent. We sleep enough for each prompt to appear, then
    # blind-press Enter. If CC's prompt cadence changes, tune the *_WAIT_S
    # constants above.
    try:
        time.sleep(PROMPT_1_WAIT_S)
        print("[iris_cold_wake] sending Enter for experimental-channels prompt",
              file=sys.stderr, flush=True)
        proc.write("\r\n")

        time.sleep(PROMPT_2_WAIT_S)
        print("[iris_cold_wake] sending Enter for channel-allowlist prompt",
              file=sys.stderr, flush=True)
        proc.write("\r\n")

        time.sleep(READY_WAIT_S)

        if not USE_PATH_A:
            # Path B fallback: type the message manually after the prompts.
            print("[iris_cold_wake] Path B: typing first message",
                  file=sys.stderr, flush=True)
            proc.write(FIRST_MSG + "\r\n")
    except Exception as e:
        print(f"[iris_cold_wake] prompt-injection error: {e!r}",
              file=sys.stderr, flush=True)
        # Don't return — let the user interact manually if the injection
        # failed. CC is still alive in the pty.

    # ── relay loops: stay alive until CC exits ───────────────────────────
    # winpty's read() is blocking (no non-blocking flag), so we run two
    # background threads: one reads pty output and writes to our stdout
    # (visible to whatever terminal launched the .bat); one reads our
    # stdin and writes to the pty (so manual user input still works after
    # the injected message).
    import threading

    def _pty_to_stdout() -> None:
        """Forward CC's pty output to launcher stdout."""
        try:
            while True:
                try:
                    chunk = proc.read(1024)
                except EOFError:
                    break
                except Exception:
                    break
                if not chunk:
                    # winpty returns "" briefly at startup occasionally;
                    # short sleep, then check isalive.
                    if not proc.isalive():
                        break
                    time.sleep(0.05)
                    continue
                try:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                except Exception:
                    break
        except Exception:
            pass

    def _stdin_to_pty() -> None:
        """Forward user keystrokes from launcher-stdin into CC's pty.

        Uses 1-char reads so each keystroke lands immediately (no need
        to wait for newline). Exits when stdin closes or pty dies.
        """
        try:
            while proc.isalive():
                ch = sys.stdin.read(1)
                if not ch:
                    break
                try:
                    proc.write(ch)
                except Exception:
                    break
        except Exception:
            pass

    t_out = threading.Thread(target=_pty_to_stdout, daemon=True, name="pty-out")
    t_in = threading.Thread(target=_stdin_to_pty, daemon=True, name="pty-in")
    t_out.start()
    t_in.start()

    # Main thread waits for the pty to exit (or Ctrl-C).
    try:
        while proc.isalive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[iris_cold_wake] Ctrl-C received, closing pty",
              file=sys.stderr, flush=True)
    finally:
        try:
            proc.close(force=True)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
