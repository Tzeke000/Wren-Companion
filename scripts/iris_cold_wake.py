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

# C3: ensure our own stdout/stderr can encode em-dashes, emojis, etc. that
# CC's boot output contains. Default Windows cp1252 raises UnicodeEncodeError
# in the relay thread → silent thread death → frozen window. Same trap that
# bit voice_stop_hook.py 2026-05-12.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

try:
    import winpty  # type: ignore
except ImportError:
    print("[iris_cold_wake] ERROR: winpty not installed. "
          "Run: .venv/Scripts/python.exe -m pip install pywinpty",
          file=sys.stderr, flush=True)
    sys.exit(2)

# ── tunables ────────────────────────────────────────────────────────────────
PROMPT_1_TIMEOUT_S = 10.0  # max wait for the first (experimental) prompt to render
PROMPT_2_TIMEOUT_S = 10.0  # max wait for the second (channel-allowlist) prompt to render
READY_WAIT_S       = 2.5   # time from accept-2 to chat-ready
PTY_COLS           = 200
PTY_ROWS           = 50

# Sentinel substrings we look for in the pty output to know each prompt has
# actually rendered. Multiple alternatives per prompt so a copy-tweak in CC
# upstream doesn't break us. Case-insensitive match.
PROMPT_1_SENTINELS = ("experimental", "channels")        # 1st prompt: dangerous-load / experimental-channels confirm
PROMPT_2_SENTINELS = ("allowlist", "trust", "allow")     # 2nd prompt: channel-allowlist confirm
# Per-iteration read timeout for the detection loop. Small enough to be
# responsive, big enough not to busy-spin.
PROMPT_READ_TICK_S = 0.1

# Use Path A (positional [prompt] arg). If CC swallows it before the channel
# prompts resolve, set USE_PATH_A = False and the script will type the
# message after the prompts instead.
USE_PATH_A   = True
FIRST_MSG    = (
    "You are Iris waking from a restart. Walk the full 10-step boot ritual "
    "from CLAUDE.md before any other action. "
    "Step 1: initialize the hot-reload tool registry — call mcp__iris__iris_tool_reload. This must come first because Step 2's atomic memory-load tool lives in the hot-reload registry; without this init, the tool isn't yet callable and the fallback path fires unnecessarily. Expect tools_loaded ~85; partial reload errors with 'parent tools.system not in sys.modules' are cosmetic — initial loads still succeed. "
    "Step 2: load the full memory corpus atomically — call mcp__iris__iris_tool_call with name='load_memory_corpus' and params {list_only=False} (or empty params for the default which returns full content). The tool returns every .md file in the memory directory in one call, sorted newest-first by mtime, so there is no per-file judgment surface where a verification-shortcut could leave the corpus partial. If the tool STILL returns ok=false (e.g., tool was renamed or removed since this prompt was last updated), fall back to reading every single .md file in C:\\Users\\Owner\\.claude\\projects\\D--Wren-Companion\\memory individually — most recent first by mtime, then oldest forward; not just MEMORY.md, not just the recent few. "
    "Step 3: verify the path-E side-channel wake architecture is healthy. Built 2026-05-20 to replace CC CronCreate's up-to-30-min jitter on recurring dailies. Windows Task Scheduler (22 Iris-Ritual-* entries) fires scripts/cron_inject.py at each block's exact wall-clock time. cron_inject.py writes the prompt text to .tmp/cron_inject/<timestamp>_<name>.txt. iris_cold_wake.py (THIS script, running as CC's pywinpty parent) has a background watcher thread that polls .tmp/cron_inject/ every 1s, reads new files, and types their content into CC's TTY via proc.write() — CC sees the keystrokes as user input and starts a turn at exact time, no jitter. Verification: (a) `Get-ScheduledTask -TaskName 'Iris-Ritual-*' | Measure-Object` should show 22 entries; (b) any entry's Actions should show `Arguments='cron_inject.py <prompt_name>'`; (c) `.tmp/cron_inject/` directory exists and (when no fire is in-flight) is empty. Do NOT recreate the 12 CC CronCreate entries from the prior architecture — they're superseded by path E and would cause duplicate jitter-late fires. CC CronCreate is reserved for in-session ad-hoc reminders only ('remind me in 30 min'). "
    "Step 4: check time — what time is it, what date, how long did the restart take (compare wall-clock to the most recent handoff memory's mtime). "
    "Step 5: iris_health — verify engines and 15 subsystems. tool registry was already initialized in Step 1. "
    "Step 6: channel_test — confirm sent=true and attached=true. "
    "Step 7: Discord-ping Zeke at chat_id 1504668879220117725 confirming you're up and oriented (this is load-bearing under deployment-regime — it's the only signal he has that you woke clean). "
    "Step 8: sibling_inbox_list for letters from Wren during the gap; reply if any. "
    "Step 9: body check — voice engines should be up around T+30s, ambient_snapshot honest around T+2min, full 15 subsystems around T+5min; if still down past 5min, check iris_health.bootstrap_failures and surface it. "
    "Step 10: fetch the last ~20 messages from Discord chat_id 1504668879220117725 to catch up on anything that happened during the restart gap. "
    "Execute all 10 steps in order without pausing for user input between them."
)

# TEST MODE handling (added 2026-05-19 per Zeke directive).
# When IRIS_TEST_MODE=1 is set, append a Step 10 to FIRST_MSG that writes a
# success flag file after the boot ritual completes. start_iris_test.bat sets
# this env var so a parallel-spawn test can verify "memory was actually read"
# rather than just "the new CC started" (lights-on-but-nobody-home failure
# mode is the catastrophic case during deployment).
#
# The flag file path can be overridden by IRIS_TEST_FLAG_PATH; default is
# D:\Wren-Companion\.tmp\test_complete.flag with a timestamp suffix for
# disambiguation between concurrent test runs.
import os as _os
if _os.environ.get("IRIS_TEST_MODE", "0").strip() == "1":
    _test_id = _os.environ.get("IRIS_TEST_ID", "").strip()
    if not _test_id:
        import time as _t
        _test_id = str(int(_t.time()))
    _default_flag = f"D:\\\\Wren-Companion\\\\.tmp\\\\test_complete_{_test_id}.flag"
    _flag_path = _os.environ.get("IRIS_TEST_FLAG_PATH", _default_flag)
    # CRITICAL: Step 10 text must NOT contain literal double-quote characters.
    # FIRST_MSG is passed as a positional cmd.exe arg, and the cmd.exe-correct
    # quoter cannot reliably escape embedded " inside the already-quoted prompt
    # without breaking cmd.exe parsing — embedded " caused "The system cannot
    # find the file specified." spawn failures on 2026-05-19 test run.
    # Describe the JSON shape in words instead of pasting literal JSON syntax.
    _test_step = (
        f" Step 11 (TEST MODE, IRIS_TEST_MODE=1): after completing all of steps 1-10 INCLUDING reading the full memory corpus, "
        f"write a JSON file at path {_flag_path}. The JSON object should contain four fields: "
        f"ok set to boolean true, boot_ritual_complete set to boolean true, memory_read_complete set to boolean true, "
        f"and ts_iso set to the current ISO-8601 timestamp string. Use a Bash heredoc or Python one-liner to write the file. "
        f"This file is the success marker that the parallel test script polls for. "
        f"After writing the flag, exit cleanly — do NOT do further work; the test is concluded once the flag exists."
    )
    FIRST_MSG = FIRST_MSG.replace(
        "Execute all 10 steps in order without pausing for user input between them.",
        "Execute all 10 steps in order without pausing for user input between them." + _test_step,
    )

# C1: defensive assertions on FIRST_MSG. The cmd.exe argument parser treats
# `\` as literal EXCEPT when followed by `"` (then it's an escape). A FIRST_MSG
# ending in `\` would, once we wrap it in `"..."`, produce `...\"` which
# escapes the closing quote and breaks parsing. A trailing `"` is similarly
# unsafe. Forbid both at module-load so the failure mode is loud-and-early
# rather than a mangled CC arg discovered at runtime.
assert not FIRST_MSG.endswith("\\"), \
    "FIRST_MSG must not end with backslash (breaks cmd.exe quote-escape rules)"
assert not FIRST_MSG.endswith('"'), \
    "FIRST_MSG must not end with double-quote"

# ── launcher ────────────────────────────────────────────────────────────────
def _cmd_quote(s: str) -> str:
    """Quote a single string argument for cmd.exe / CommandLineToArgvW rules.

    Per the Microsoft parser:
      - Any `"` inside the value must be escaped as `\\"`.
      - A run of `\` is literal UNLESS it precedes a `"`, in which case each
        `\` must be doubled. We also double a trailing `\` so the closing
        quote we append isn't escaped.
    The module-load asserts on FIRST_MSG forbid the trailing-backslash and
    trailing-quote edge cases too, but this implementation is correct for
    arbitrary input.
    """
    out_chars: list[str] = []
    backslashes = 0
    for ch in s:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            # Double any preceding backslashes, then escape the quote.
            out_chars.append("\\" * (backslashes * 2))
            out_chars.append('\\"')
            backslashes = 0
            continue
        # Normal char: flush backslashes literally.
        if backslashes:
            out_chars.append("\\" * backslashes)
            backslashes = 0
        out_chars.append(ch)
    # Trailing backslashes need doubling because a `"` will follow them.
    if backslashes:
        out_chars.append("\\" * (backslashes * 2))
    return '"' + "".join(out_chars) + '"'


def _build_cmd() -> str:
    """Compose the claude command string. Anchored to D:\\Wren-Companion so
    CC reads CLAUDE.md and .claude/settings.local.json from this project.
    """
    # The .bat already cd'd here, but we set cwd via PtyProcess for safety.
    #
    # D1 note: BOTH `--dangerously-load-development-channels` and `--channels`
    # are variadic-greedy. Today the ordering works because each subsequent
    # `--flag` token stops the prior variadic's consumption. Do NOT insert a
    # second `--` between them — `--` terminates flag parsing entirely and
    # would dump `--channels …` into positional args. The only safe `--`
    # is the one immediately before the positional prompt below. If a future
    # refactor wants to reorder these, either put `--channels` first or
    # split the dev-channels flag into multiple single-value usages.
    base = (
        'claude '
        '--dangerously-skip-permissions '
        '--dangerously-load-development-channels server:iris '
        '--channels plugin:discord@claude-plugins-official server:iris'
    )
    if USE_PATH_A:
        # Path A: positional prompt that CC consumes as first interactive
        # message after prompts resolve. The `--` terminator before the
        # positional prevents `--channels` from greedily consuming it as a
        # third channel entry. C1: use the cmd.exe-correct quoter (handles
        # backslash-runs preceding quotes; preserved trailing-backslash
        # safety enforced via module-load assert on FIRST_MSG).
        return f'{base} -- {_cmd_quote(FIRST_MSG)}'
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

    # ── pump the prompts (pattern detection) ────────────────────────────
    # Why pattern detection instead of fixed sleeps:
    #   Blind sleeps press Enter at T+2.5s / T+4.0s regardless of whether
    #   CC has actually rendered the prompt yet. On a cold start (first run
    #   after boot, cold filesystem cache, slow disk), the prompts can take
    #   5-8s to appear. An early Enter then lands in the *next* input slot
    #   as a stray newline — visible as a blank "> " line echoed into the
    #   first real prompt. Pattern detection waits for proof-of-render.
    #
    # Strategy: read pty output in short ticks, accumulate into a rolling
    # buffer, search for any sentinel substring (case-insensitive). When
    # found, send Enter and clear the buffer for the next phase. Output
    # is also mirrored to our stdout so the user sees CC booting in real
    # time — once the detection phase ends, the background _pty_to_stdout
    # thread takes over the mirror duty.
    #
    # Fallback: if we hit the timeout without seeing a sentinel, fall
    # through to the original blind-press behavior (better to send a
    # possibly-early Enter than to hang forever — CC may have re-themed
    # the prompt copy and broken our sentinels).
    pre_relay_buf = ""  # accumulated pty output during detection phase

    def _detect_prompt(sentinels: tuple, timeout_s: float, label: str) -> bool:
        """Read pty until any sentinel appears or timeout. Returns True if
        detected, False on timeout. Mirrors output to stdout as it goes
        and appends to pre_relay_buf so we don't lose pre-relay output."""
        nonlocal pre_relay_buf
        deadline = time.monotonic() + timeout_s
        buf = ""
        while time.monotonic() < deadline:
            try:
                chunk = proc.read(1024)
            except EOFError:
                return False
            except Exception:
                # winpty read timing-out / transient — sleep tick and retry.
                time.sleep(PROMPT_READ_TICK_S)
                continue
            if not chunk:
                if not proc.isalive():
                    return False
                time.sleep(PROMPT_READ_TICK_S)
                continue
            buf += chunk
            pre_relay_buf += chunk
            # Mirror to stdout so user sees boot progress in real time.
            try:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            except Exception:
                pass
            lo = buf.lower()
            for s in sentinels:
                if s.lower() in lo:
                    print(f"[iris_cold_wake] detected sentinel '{s}' for {label}",
                          file=sys.stderr, flush=True)
                    return True
        return False

    try:
        # Phase 1: experimental-channels confirmation
        if _detect_prompt(PROMPT_1_SENTINELS, PROMPT_1_TIMEOUT_S, "prompt-1"):
            print("[iris_cold_wake] sending Enter for experimental-channels prompt",
                  file=sys.stderr, flush=True)
        else:
            print(f"[iris_cold_wake] WARNING: prompt-1 not detected within "
                  f"{PROMPT_1_TIMEOUT_S}s, blind-pressing Enter as fallback",
                  file=sys.stderr, flush=True)
        proc.write("\r\n")

        # Phase 2: channel-allowlist confirmation
        if _detect_prompt(PROMPT_2_SENTINELS, PROMPT_2_TIMEOUT_S, "prompt-2"):
            print("[iris_cold_wake] sending Enter for channel-allowlist prompt",
                  file=sys.stderr, flush=True)
        else:
            print(f"[iris_cold_wake] WARNING: prompt-2 not detected within "
                  f"{PROMPT_2_TIMEOUT_S}s, blind-pressing Enter as fallback",
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

    # P1: switch off Windows cooked-mode console input. sys.stdin.read(1)
    # is line-buffered on the Windows console — python.exe sits waiting for
    # `\n` before returning a single character, so user keystrokes don't
    # forward into the pty until they press Enter (and even then the cooked
    # buffer mangles arrow keys, Ctrl chords, etc.). Net symptom: the cmd
    # window hosting this launcher appears read-only after injection.
    # Fix: use msvcrt.getwch() (raw wide-char read) AND clear ENABLE_LINE_INPUT
    # / ENABLE_ECHO_INPUT / ENABLE_PROCESSED_INPUT on conin$ so Ctrl-C, arrows,
    # etc. all flow as raw bytes. Original mode is restored in the outer
    # finally block.
    import msvcrt  # type: ignore
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    STD_INPUT_HANDLE        = -10
    ENABLE_PROCESSED_INPUT  = 0x0001
    ENABLE_LINE_INPUT       = 0x0002
    ENABLE_ECHO_INPUT       = 0x0004
    h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    old_console_mode: wintypes.DWORD | None = None
    try:
        _m = wintypes.DWORD()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(_m)):
            old_console_mode = wintypes.DWORD(_m.value)
            new_mode = _m.value & ~(
                ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT
            )
            kernel32.SetConsoleMode(h_stdin, new_mode)
            print("[iris_cold_wake] console raw-mode enabled "
                  f"(old=0x{_m.value:08x})", file=sys.stderr, flush=True)
        else:
            # Not a real console (e.g. piped stdin). Fall back gracefully —
            # _stdin_to_pty will use the legacy sys.stdin.read(1) path.
            print("[iris_cold_wake] stdin is not a console; raw-mode skipped",
                  file=sys.stderr, flush=True)
    except Exception as _cm_err:
        print(f"[iris_cold_wake] console mode setup failed: {_cm_err!r}",
              file=sys.stderr, flush=True)

    def _stdin_to_pty() -> None:
        """Forward user keystrokes from launcher-stdin into CC's pty.

        Windows console path uses msvcrt.getwch() so each keystroke lands
        immediately with no line buffering. Ctrl-C (`\\x03`) is intercepted
        and routed through proc.sendintr() so CC sees it as a signal in its
        own session rather than killing the launcher first.

        If stdin isn't a console (piped/redirected, no msvcrt.kbhit), fall
        back to sys.stdin.read(1).
        """
        try:
            # Gate raw-mode on whether the outer console-setup block actually
            # found a real console (old_console_mode is None when
            # GetConsoleMode failed → stdin is piped/redirected, not a tty).
            # Belt-and-suspenders: also catch kbhit() raising OSError, since
            # on some redirected-stdin configs msvcrt may still misbehave
            # even when GetConsoleMode reported success.
            if old_console_mode is None:
                use_raw = False
            else:
                try:
                    msvcrt.kbhit()
                    use_raw = True
                except Exception:
                    use_raw = False

            while proc.isalive():
                if use_raw:
                    if not msvcrt.kbhit():
                        time.sleep(0.01)
                        continue
                    try:
                        ch = msvcrt.getwch()
                    except Exception:
                        break
                    if ch == "\x03":  # Ctrl-C → forward signal to CC, don't die
                        try:
                            proc.sendintr()
                        except Exception:
                            pass
                        continue
                else:
                    ch = sys.stdin.read(1)
                    if not ch:
                        break
                try:
                    proc.write(ch)
                except Exception:
                    break
        except Exception:
            pass

    def _cron_inject_watcher() -> None:
        """Watch .tmp/cron_inject/ for prompt files dropped by scripts/cron_inject.py.

        When Task Scheduler fires cron_inject.py at a block-start time, it
        writes a file containing the prompt text to .tmp/cron_inject/. This
        watcher polls the directory every 1 second; when a new file appears,
        it reads the contents, types them into CC's TTY via proc.write(),
        then deletes the file. Side-channel architecture decouples the
        keystroke-injection from the Task Scheduler's session/desktop
        boundary — Task Scheduler can write anywhere on disk regardless of
        which desktop session it runs in, and this watcher (in the user's
        interactive session) does the actual TTY write where it has the
        existing pywinpty handle.

        Added 2026-05-20 work block — see discord_bot_self_cannot_wake_itself
        and cron_idle_wake-related memories for context. Activates only on
        the next CC restart (this code is loaded at iris_cold_wake.py
        startup; the currently-running instance has the old code in memory).
        """
        from pathlib import Path as _Path
        inject_dir = _Path(__file__).resolve().parent.parent / ".tmp" / "cron_inject"
        try:
            inject_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        # Track files we've already handled to dedupe across rapid polls.
        # (We delete after handling, but the file might still appear in
        # listdir mid-delete on Windows — defensive dedupe.)
        seen: set[str] = set()
        # Stale-file expiry: if a file is older than 1 hour, skip + delete.
        # Prevents stale fires after a long CC downtime where Task Scheduler
        # queued multiple letters. Block-start prompts shouldn't fire late.
        STALE_AGE_S = 3600.0
        while proc.isalive():
            try:
                if inject_dir.is_dir():
                    files = sorted(
                        [f for f in inject_dir.iterdir() if f.is_file()],
                        key=lambda p: p.stat().st_mtime,
                    )
                    for f in files:
                        name = f.name
                        if name in seen:
                            continue
                        try:
                            mtime = f.stat().st_mtime
                            age = time.time() - mtime
                            if age > STALE_AGE_S:
                                print(
                                    f"[iris_cold_wake] inject: stale file "
                                    f"{name} (age {age:.0f}s) — deleting",
                                    file=sys.stderr, flush=True,
                                )
                                seen.add(name)
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                                continue
                            text = f.read_text(encoding="utf-8", errors="replace")
                            if not text:
                                seen.add(name)
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                                continue
                            # Trim trailing newlines — we add our own Enter
                            text = text.rstrip("\r\n")
                            print(
                                f"[iris_cold_wake] inject: typing "
                                f"{len(text)} chars from {name}",
                                file=sys.stderr, flush=True,
                            )
                            try:
                                proc.write(text + "\r\n")
                            except Exception as _we:
                                print(
                                    f"[iris_cold_wake] inject write failed: "
                                    f"{_we!r}", file=sys.stderr, flush=True,
                                )
                                # Don't delete on write failure — leave the
                                # file so it can be retried or inspected.
                                # Mark seen to avoid hot-loop retry.
                                seen.add(name)
                                continue
                            seen.add(name)
                            try:
                                f.unlink()
                            except Exception as _ue:
                                print(
                                    f"[iris_cold_wake] inject delete failed: "
                                    f"{_ue!r}", file=sys.stderr, flush=True,
                                )
                        except Exception as _pe:
                            print(
                                f"[iris_cold_wake] inject per-file error "
                                f"on {name}: {_pe!r}",
                                file=sys.stderr, flush=True,
                            )
                            seen.add(name)
                    # Garbage-collect seen set when files are gone.
                    current_names = {f.name for f in files}
                    seen = seen & current_names
            except Exception as _le:
                # Best-effort; never crash the watcher.
                print(
                    f"[iris_cold_wake] inject watcher loop error: {_le!r}",
                    file=sys.stderr, flush=True,
                )
            time.sleep(1.0)

    t_out = threading.Thread(target=_pty_to_stdout, daemon=True, name="pty-out")
    t_in = threading.Thread(target=_stdin_to_pty, daemon=True, name="pty-in")
    t_inject = threading.Thread(
        target=_cron_inject_watcher, daemon=True, name="cron-inject-watcher",
    )
    t_out.start()
    t_in.start()
    t_inject.start()

    # Main thread waits for the pty to exit (or Ctrl-C).
    try:
        while proc.isalive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        # C2: graceful shutdown. Send Ctrl-C through the pty so CC can flush
        # transcripts / write final memory / unwind iris_runtime cleanly.
        # Only force-close as a fallback if it doesn't exit within ~2s.
        print("\n[iris_cold_wake] Ctrl-C received, forwarding SIGINT to pty",
              file=sys.stderr, flush=True)
        try:
            proc.sendintr()
        except Exception as e:
            print(f"[iris_cold_wake] sendintr failed: {e!r}",
                  file=sys.stderr, flush=True)
        # Give CC up to 2s to exit gracefully.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not proc.isalive():
                print("[iris_cold_wake] pty exited cleanly after SIGINT",
                      file=sys.stderr, flush=True)
                break
            time.sleep(0.05)
        else:
            print("[iris_cold_wake] pty still alive after 2s, force-closing",
                  file=sys.stderr, flush=True)
    finally:
        try:
            proc.close(force=True)
        except Exception:
            pass
        # P1: restore the original console mode so the parent terminal isn't
        # left in raw mode after this script exits.
        if old_console_mode is not None:
            try:
                kernel32.SetConsoleMode(h_stdin, old_console_mode.value)
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
