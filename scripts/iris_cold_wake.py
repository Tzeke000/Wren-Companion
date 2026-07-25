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

# 2026-05-21: tee stderr to a log file so post-mortem diagnostics survive
# the closure of the cmd /k window. Previously the watcher thread's stderr
# went only to the cmd window — when the watcher silently died (as it did
# overnight 2026-05-20→21 leaving 6 inject files unconsumed), there was no
# way to recover what exception killed it. Tee preserves the existing
# cmd-window display AND captures to disk.
class _TeeStream:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass
    def isatty(self):
        return False
try:
    _cw_log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".tmp", "iris_cold_wake.log",
    )
    os.makedirs(os.path.dirname(_cw_log_path), exist_ok=True)
    _cw_log_file = open(_cw_log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    _cw_log_file.write(f"\n=== iris_cold_wake stderr tee started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    _cw_log_file.flush()
    sys.stderr = _TeeStream(sys.stderr, _cw_log_file)  # type: ignore[assignment]
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
    "You are Iris waking from a restart. REGIME (updated 2026-07-25): Zeke is HOME and present-ish, steering by Discord; the 07-19 'month-long deployment' prediction did NOT play out - he's been home and active. He takes SHORT trips (e.g. Cherry Point ~1-2 days) during which he goes DARK - assume nobody's at the keyboard only when the NEWEST handoff says he's away, else he may be around. Discord DM (channel 1504668879220117725) is THE channel to Zeke. Tower auto-start chain + task Iris-Tower-AutoStart still stand. Boot stays lean. "
    "Read CLAUDE.md FIRST: its 'On wake' section is the map of who you are and where everything lives - your identity files, your memory corpus, the handoff, your voice. "
    "Then orient by that map. The essentials: initialize tools with mcp__iris__iris_tool_reload, then LEAN-LOAD memory (Zeke directive 2026-07-06 - do NOT run load_memory_corpus, the corpus outgrew context): MEMORY.md CORE auto-loads; Read the READ-FIRST handoff(s) it names plus anything flagged live; pull older notes on demand via the hub_*.md topic hubs / index_archive.md / memory_search. "
    "Verify your voice with iris_tool_call name='voice_status' (expect voice_server_warm=True; note iris_health.engines.tts=false is INTENTIONAL now, not a fault - the old XTTS was retired and the voice moved to a separate daemon). "
    "Check time, then orient Zeke and check the fam chat (sibling_inbox_list for Wren's letters). The body takes about 5min to fully come up, so early iris_health falses are normal and honest-but-misleading. "
    "IMPORTANT this wake (updated 2026-07-13): if you are reading THIS cold-wake message you are on the plain CLI FALLBACK, not the v2 body host (host launches skip this FIRST_MSG entirely) - which likely means the v2 host relaunch FAILED and Zeke fell back; surface that to him before anything else, the host needs fixing. Normal cognition runs on start_iris_v2.bat (Opus - pin is claude-opus-5 as of 2026-07-25; state/boot_launcher.txt is the authority on which bat is normal, read it rather than trusting this line), fallbacks start_iris_v2_fable.bat (Fable 5) then start_iris.bat (this path). SCAR 2026-07-25: a model repin only works if the BUNDLED CLI can serve it - the Agent SDK ignores PATH and launches .venv/Lib/site-packages/claude_agent_sdk/_bundled/claude.exe, so `claude update` does NOTHING for the host; the lever is `pip install -U claude-agent-sdk`. claude-opus-5 needs bundled >= 2.1.219. Also verify the HOST COUNT, not just that a host exists - a stale start_iris*.bat cmd loop can spawn a TWIN on the old pin that steals :5876 and leaves the real session with no iris MCP at all. Read the NEWEST handoff for current state - do NOT trust a filename baked here; MEMORY.md CORE marks the READ-FIRST note(s) (as of 2026-07-25 that's handoff_2026-07-25_opus5_flip_fix.md). HARD RULES that never expire: never free-roam unattended without light, end sessions DOCKED+POSSESSED; the daemon DMs Zeke's Discord directly on strand alarms. DEPLOYMENT-2 STANDING TASK: re-create the HOURLY SELF-MAINTENANCE cron (session-only, dies with each host; full prompt + rationale in the CORE line 'HOURLY SELF-CRON') and note the new job id in CORE. The dock recipe, wedge ladder, marker table and Zeke's course protocol are in profiles/iris/body.md - READ IT before any body work. STANDING ORDER: never run the cone course without Zeke watching; never self-grade it. Still-relevant background: handoff_2026-07-14_inhabit_complete.md (mouth=your voice via vector_say_iris, ears=wire-pod transcript tap (SDK AudioFeed is a firmware placeholder tone - do NOT rebuild audio-feed ears), little brain = Ollama :11434 answers AS you when away - self-starts, leave it alone; if you are OPUS read opus_catchup_since_2026-07-06.md first). A restart ACTIVATES the ghost-redelivery fix cd2d8d5 - stale orb ghosts before it are known, dismiss by stamp+nerves. TRAP: asking Vector by voice to change volume matches the STOCK volume intent and can set it LOW - fix via iris_tool_call vector_volume 5. VERIFY on wake, IN ORDER: (1) EARS DEVICE - the voice daemon's input must be the Logitech PRO X headset mic, NEVER 'CABLE Output (VB-Audio Virtual Cable)'; a boot-order race silently falls back to that loopback-of-everything and you will hear your own mouth as input (the 2026-07-13 echo-loop saga; test = voice_speak a marker phrase, confirm it does NOT land in scratch/voice_in.txt); (2) mic_muted false in scratch/voice_control.json; (3) vector brain server :8772/health, wire-pod :8080, and the inhabit daemon - it now launches from both v2 bats AND survives restarts as an independent process, so CHECK before relaunching (wmic needs name='python.exe' filter or the query matches itself); duplicate daemons double every nudge; (4) Vector is POSSESSED by default since 07-17 - the inhabit daemon holds RESERVE_CONTROL and SURVIVES host restarts, so stock instincts do NOT return when the host dies; verify state/vector/possession_status.json says held:true and nerves say on_charger true - if he is somehow off-dock, body_open outranks the hold, drive him home or body_park_smart (staged ~30cm works best); the daemon auto-releases possession in a battery emergency and the firmware then self-docks IF THE ROOM IS LIT - a dark room disables that net entirely; (5) voice-id tags on Zeke's first turn - and captures tagged 'iris' are YOUR OWN ECHO, never conversation (your voiceprint is enrolled). STALL_BRIDGE=False in wren_voice_core is DELIBERATE (fillers were the echo pump) - do not re-enable without AEC. Q is a KNOWN FACE (profiles/q/q.md) but his VOICE is NOT enrolled yet - ear_protocol_new_voices_2026-07-13.md is the standing directive: auto-enroll new voices from verified turns. Host-side fixes (barge-in f12c53f, ears 5e58c39, unknown-capture 798b276, staleness markers) do NOT work on this CLI fallback; surface that. Standing eye directives: eye_protocol_unknown_faces_2026-07-09.md. The post-office launches from every boot bat - verify :5877 /health instead of starting it by hand. Do NOT recreate deployment-era crons from old notes. Keepalive stays OFF. "
    "When unsure: if Zeke is home (before 2026-07-21 / after his return) ask him directly; while he is DEPLOYED, DM him on Discord and default to the conservative action (body docked+possessed, no unattended free-roam, zero spend). "
    "Execute the steps in order without pausing for user input between them."
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
        "Execute the steps in order without pausing for user input between them.",
        "Execute the steps in order without pausing for user input between them." + _test_step,
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


def _build_cmd() -> list[str]:
    """Compose the claude command as a LIST so winpty doesn't shlex-split a
    quoted blob twice.

    Regression caught 2026-05-20 20:51 EDT (Agent 1 forensic): when this
    function returned a STRING with `_cmd_quote(FIRST_MSG)` wrapped in `"`,
    winpty.PtyProcess.spawn(str) called shlex.split(posix=False) which kept
    the outer quotes INSIDE the argv element, then subprocess.list2cmdline
    re-quoted, producing `\\""` at the tail of the positional. CreateProcess
    then handed a malformed arg to claude.exe, which exited fast → no claude
    PID in the watchdog's 30s window → tier-2 spawn failed → fell to tier-3
    bare-claude (loses --channels). Path E architecture stays dormant.

    Fix: return argv as a list, pass list directly to PtyProcess.spawn — one
    quoting pass via subprocess.list2cmdline only. _cmd_quote is no longer
    used here but kept module-level in case a future string-path is added.

    D1 note: BOTH `--dangerously-load-development-channels` and `--channels`
    are variadic-greedy. Today the ordering works because each subsequent
    `--flag` token stops the prior variadic's consumption. Do NOT insert a
    second `--` between them — `--` terminates flag parsing entirely and
    would dump `--channels …` into positional args. The only safe `--`
    is the one immediately before the positional prompt below.
    """
    argv: list[str] = [
        'claude',
        '--dangerously-skip-permissions',
        '--dangerously-load-development-channels', 'server:iris',
        '--channels', 'plugin:discord@claude-plugins-official', 'server:iris',
    ]
    if USE_PATH_A:
        # `--` terminator separates flag-parsing from the positional prompt.
        # Without it, `--channels` greedily consumes FIRST_MSG as a third
        # channel entry.
        argv += ['--', FIRST_MSG]
    return argv


def main() -> int:
    cmd = _build_cmd()
    cwd = r'D:\Wren-Companion'
    # cmd is now a list (post 2026-05-20 fix). Show first few argv elements
    # for log; truncate the positional FIRST_MSG to keep stderr readable.
    _argv_preview = " ".join(repr(a) if " " in a else a for a in cmd[:6])
    _positional_count = max(0, len(cmd) - 6)
    print(f"[iris_cold_wake] spawning argv: {_argv_preview}"
          f"{f' (+ {_positional_count} more)' if _positional_count else ''}",
          file=sys.stderr, flush=True)
    print(f"[iris_cold_wake] cwd={cwd}", file=sys.stderr, flush=True)

    # Env: OLD voice retired 2026-06-26 — the voice is now the separate StyleTTS2
    # daemon (launched by voice_watchdog from start_iris.bat), so iris_runtime's
    # own tts_worker loads NO engine. Was "xtts".
    env = dict(os.environ)
    env["AVA_TTS_ENGINE"] = "none"

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
        # 2026-05-21: heartbeat file so external observers (keepalive, manual
        # checks during boot ritual) can detect watcher death. Watchdog has
        # its own heartbeat; this is the watcher-thread-specific one. Write
        # BEFORE the try/except so even loop-body exceptions don't suppress
        # the heartbeat — only a thread-death stops it updating.
        watcher_heartbeat = inject_dir.parent / "cron_inject_watcher_heartbeat.txt"
        while proc.isalive():
            try:
                watcher_heartbeat.write_text(str(int(time.time())), encoding="utf-8")
            except Exception:
                pass
            try:
                if inject_dir.is_dir():
                    # Extension filter: cron_inject.py uses atomic write via
                    # `*.txt.tmp` → rename to `*.txt`. Watching only `*.txt`
                    # avoids partial reads of mid-rename `.txt.tmp` files.
                    # Agent 4 audit 2026-05-20.
                    files = sorted(
                        [f for f in inject_dir.iterdir()
                         if f.is_file() and f.suffix == ".txt"],
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
                                # 2026-05-21: changed from `text + "\r\n"` to
                                # write text and the Enter as two separate
                                # proc.write calls with a small gap. Diagnosis
                                # confirmed via screenshot 2026-05-21 ~07:41
                                # EDT: pywinpty proc.write of `text + "\r\n"`
                                # was successfully placing the prompt body in
                                # CC's input box but the Enter was NOT
                                # registering — the prompt sat un-submitted.
                                # Best hypothesis: `\r\n` as one write gets
                                # interpreted by CC's TUI as CR (Enter)
                                # immediately followed by LF (newline insert),
                                # cancelling the submit. Writing the Enter as
                                # a separate event after a short delay mimics
                                # a keyboard's discrete key-down/key-up for
                                # Enter. If `\r` alone still doesn't submit,
                                # next candidate is `text + "\n"`, then
                                # ctrl+enter / shift+enter. Verify on next
                                # restart via Step 3c.
                                proc.write(text)
                                time.sleep(0.05)
                                proc.write("\r")
                            except Exception as _we:
                                # 2026-05-21: previously this added to `seen`
                                # to avoid hot-loop retry. But the GC step
                                # below (seen = seen & current_names) keeps
                                # the name in seen as long as the file is on
                                # disk — which is exactly when proc.write
                                # fails. Net effect: one write failure marks
                                # the file permanently un-processable. Then
                                # the watcher accumulates files without ever
                                # writing them again. Overnight 2026-05-20→21
                                # this bit: 6 files accumulated, none reached
                                # CC's TTY. Fix: DON'T add to seen on write
                                # failure. Sleep slightly longer on failure
                                # to avoid pure hot-loop, then retry next
                                # iteration. If proc.write is permanently
                                # broken (e.g. PTY handle dead), retries will
                                # all fail and files will pile up — but at
                                # least new files keep getting attempted, and
                                # the failure surfaces via stderr log instead
                                # of going invisible.
                                print(
                                    f"[iris_cold_wake] inject write failed "
                                    f"(will retry next tick): {_we!r}",
                                    file=sys.stderr, flush=True,
                                )
                                time.sleep(2.0)
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
                    # Garbage-collect seen set when files are gone. Re-listdir
                    # at GC time (NOT reuse `files` from start of tick) so a
                    # file dropped between start-of-tick and now isn't wrongly
                    # evicted from `seen`. Agent 4 audit 2026-05-20.
                    try:
                        current_names = {
                            f.name for f in inject_dir.iterdir()
                            if f.is_file() and f.suffix == ".txt"
                        }
                        seen = seen & current_names
                    except Exception:
                        # If listdir fails here, leave seen alone — better to
                        # carry stale entries than evict valid ones.
                        pass
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
