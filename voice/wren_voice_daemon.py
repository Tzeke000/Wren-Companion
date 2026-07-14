"""wren_voice_daemon.py — Long-running voice daemon (GOSE pattern, Zeke 2026-06-14).

Owns: warm mic models (whisper + Silero VAD), the playback worker queue, and TCP
dispatch so MCP tools can talk to it without knowing about hardware or model state.

The GOSE pattern: MCP tools are thin clients (wren_voice_client.py → this daemon).
Voice fixes reload the DAEMON (cmd="reload" + kill/restart the daemon process) — no
Claude Code relaunch needed. The daemon stays up; only the logic layer (wren_voice_core)
is hot-swapped via importlib.reload().

Protocol: newline-delimited JSON over TCP 127.0.0.1:WREN_VOICE_DAEMON_PORT (default 8770).
  request : {"cmd": <str>, "args": {<dict>}}\n
  response: {"ok": true,  "result": <json>}\n
         OR {"ok": false, "error": <str>}\n

Connections stay open (keep-alive); the server handles each in its own daemon thread.
SO_REUSEADDR is set so a quick restart doesn't hit "address already in use".

Dispatch table:
  ping                → {"pong": true}
  reload              → importlib.reload(core) + core.reload_helpers(); returns reloaded list
  listen              → mic-locked  → core.cmd_listen(ctx, args)
  speak_interruptible → mic-locked  → core.cmd_speak_interruptible(ctx, args)
  status              → core.cmd_status(ctx, args)
  speak               → core.cmd_speak(ctx, args)       (enqueues via ctx.play_queue)
  call_start          → core.cmd_call_start(ctx, args)
  call_end            → core.cmd_call_end(ctx, args)
  backend             → core.cmd_backend(ctx, args)
  <unknown>           → {"ok": false, "error": "unknown cmd: <name>"}
"""
from __future__ import annotations

import importlib
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
import faulthandler

# ── path so sibling modules resolve ──────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Native-crash visibility (Wren's port, 37f3267): faulthandler dumps a C-level stack
# on a segfault/abort — e.g. a non-deterministic CUDA teardown hard-fault at call_end
# that pure-Python try/except CAN'T catch. It writes to stderr, which the watchdog's
# _launch_daemon redirects to a persistent append log (voice/daemon.log), so the next
# such crash is captured instead of vanishing silently with the process.
faulthandler.enable()

import wren_listen as wl          # whisper warm (get_whisper) + SAMPLE_RATE
import wren_voice_core as core    # all the actual logic; reload()able

# ── Port config ───────────────────────────────────────────────────────────────
PORT = int(os.environ.get("WREN_VOICE_DAEMON_PORT", "8770"))

# Commands that need exclusive mic access — serialize them via ctx.mic_lock.
_MIC_CMDS = frozenset({"listen", "speak_interruptible"})


# ── Context (created once at startup, passed into every core call) ─────────────
class Ctx:
    """Shared mutable state for the daemon lifetime. Passed by reference into
    every core.cmd_* call so core can read/write voice pipeline state without
    global variables in this module."""

    def __init__(self) -> None:
        # ── mouth server ──────────────────────────────────────────────────
        _voice_port = int(os.environ.get("WREN_VOICE_PORT", "8765"))
        self.server_url: str = f"http://127.0.0.1:{_voice_port}"

        # ── SenseVoice ears port ──────────────────────────────────────────
        self.sv_port: int = int(os.environ.get("WREN_SV_PORT", "8766"))

        # ── call / engine state ───────────────────────────────────────────
        self.call_warm: bool = False
        # engine label follows the actual mouth port (WREN_VOICE_PORT), not a hard-coded
        # default — start_wren sets 8769 (StyleTTS2), so init must reflect that, not "xtts".
        self.engine: str = core._engine_for_url(self.server_url)
        self.engine_procs: dict = {}   # engine_name -> subprocess.Popen (cold-started)

        # ── Silero VAD (loaded by _warm_vad thread) ───────────────────────
        self.vad_model = None
        self.vad_lock: threading.Lock = threading.Lock()

        # ── playback queue / speaking state ───────────────────────────────
        self.play_queue: queue.Queue = queue.Queue()
        self.speaking: bool = False

        # ── mic serialization ─────────────────────────────────────────────
        # RLock (NOT Lock): the daemon dispatch wraps mic commands in `with mic_lock`,
        # and core.cmd_listen ALSO acquires it for capture — same thread, so a plain Lock
        # self-deadlocks (found 2026-06-14 in testing). RLock allows the same-thread
        # re-entry while still serializing across different connection threads.
        self.mic_lock: threading.RLock = threading.RLock()

        # ── mic device index, resolved ONCE on the main thread at startup ──
        # (sd.query_devices on a worker connection thread hangs on Windows; see
        # _warm_audio_main). core._capture_utterance reads this instead of calling
        # find_input_device itself. None → core falls back to find_input_device.
        self.mic_dev_idx = None


# ── Warm-up threads ───────────────────────────────────────────────────────────

def _warm_whisper(ctx: Ctx) -> None:  # noqa: ARG001 — ctx reserved for future use
    """Load the faster-whisper model in the background so the first listen is fast."""
    try:
        wl.get_whisper()
        print("[daemon] whisper warm", flush=True)
    except Exception as e:
        print(f"[daemon] whisper warm FAILED: {e!r}", file=sys.stderr, flush=True)


def _warm_vad(ctx: Ctx) -> None:
    """Load Silero VAD on CPU (~4s) so the first barge-in isn't slow."""
    try:
        from silero_vad import load_silero_vad  # type: ignore[import]
        m = load_silero_vad(onnx=False)
        with ctx.vad_lock:
            ctx.vad_model = m
        print("[daemon] silero VAD warm", flush=True)
    except Exception as e:
        print(f"[daemon] silero VAD warm FAILED (barge-in disabled): {e!r}",
              file=sys.stderr, flush=True)


# ── Playback worker ───────────────────────────────────────────────────────────

def _playback_worker(ctx: Ctx) -> None:
    """Drain ctx.play_queue in order, playing each chunk via core.play_one().

    cmd_speak in core sets ctx.speaking=True and puts text segments to the queue;
    this worker consumes them in order, then flips speaking=False and fires
    core.on_drain() when the queue empties (so core can update the overlay state,
    etc. — the same 'flip cue' pattern as the old voice_speak handler).
    """
    while True:
        try:
            text: str = ctx.play_queue.get()
        except Exception:
            continue
        try:
            core.play_one(ctx, text)
        except Exception as e:
            print(f"[daemon] play_one error: {e!r}", file=sys.stderr, flush=True)
        finally:
            try:
                ctx.play_queue.task_done()
            except Exception:
                pass
        # After each chunk: check whether the queue just drained.
        if ctx.play_queue.empty():
            ctx.speaking = False
            try:
                core.on_drain(ctx)
            except Exception as e:
                print(f"[daemon] on_drain error: {e!r}", file=sys.stderr, flush=True)


# ── Per-connection handler ────────────────────────────────────────────────────

def _handle_connection(conn: socket.socket, addr, ctx: Ctx) -> None:
    """Read newline-delimited JSON requests from `conn` and write responses.

    The connection stays open (keep-alive) until the client closes it or an
    unrecoverable socket error occurs. A malformed or unknown request returns an
    error JSON — it never closes the connection or crashes this thread.
    """
    buf = b""
    try:
        while True:
            # ── Read until we have a complete line ───────────────────────
            while b"\n" not in buf:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return  # client closed / socket error
                if not chunk:
                    return  # clean EOF
                buf += chunk

            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue

            # ── Parse ────────────────────────────────────────────────────
            try:
                req = json.loads(line.decode("utf-8", "replace"))
                cmd: str = str(req.get("cmd", ""))
                args: dict = req.get("args", {}) or {}
            except Exception as e:
                _send(conn, {"ok": False, "error": f"parse error: {e!r}"})
                continue

            # ── Dispatch ─────────────────────────────────────────────────
            try:
                result = _dispatch(cmd, args, ctx)
                _send(conn, {"ok": True, "result": result})
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                _send(conn, {"ok": False, "error": repr(e)})
    except Exception:
        # Unexpected connection-level error — log, let the thread end cleanly.
        traceback.print_exc(file=sys.stderr)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _send(conn: socket.socket, payload: dict) -> None:
    """Serialise payload as a single JSON line and write it to conn."""
    try:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    except OSError:
        pass  # client gone — nothing to do


def _dispatch(cmd: str, args: dict, ctx: Ctx):
    """Resolve and call the right handler.  Returns a JSON-serialisable value."""
    # ── Built-in commands ─────────────────────────────────────────────────
    if cmd == "ping":
        return {"pong": True}

    if cmd == "reload":
        # Hot-swap the logic layer: reload core, then ask it to reload its own
        # helper modules (wren_prosody, wren_smartturn, etc.). Warm models in
        # ctx.vad_model / play_queue / etc. are unaffected — that's the whole point.
        importlib.reload(core)
        helpers = core.reload_helpers()
        return {"reloaded_core": True, "helpers": helpers}

    # ── Core command lookup (at call-time, so a reload is immediately visible) ──
    fn = getattr(core, "cmd_" + cmd, None)
    if fn is None:
        raise ValueError(f"unknown cmd: {cmd!r}")

    # ── Mic-serialised commands ───────────────────────────────────────────
    if cmd in _MIC_CMDS:
        with ctx.mic_lock:
            return fn(ctx, args)

    # ── All other core commands ───────────────────────────────────────────
    return fn(ctx, args)


# ── TCP server ────────────────────────────────────────────────────────────────

class _ThreadedTCPServer:
    """Minimal threaded TCP server — avoids the socketserver quirks on Windows
    while still giving us SO_REUSEADDR and daemon-thread connections."""

    def __init__(self, host: str, port: int, ctx: Ctx) -> None:
        self._ctx = ctx
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(16)

    def serve_forever(self) -> None:
        while True:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break  # server socket closed
            t = threading.Thread(
                target=_handle_connection,
                args=(conn, addr, self._ctx),
                daemon=True,
                name=f"wren-voice-conn-{addr}",
            )
            t.start()

    def shutdown(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

def _warm_audio_main(ctx: Ctx) -> None:
    """Initialize PortAudio/WASAPI AND resolve the mic device index on the MAIN thread.

    WINDOWS GOTCHA (found 2026-06-14 in live daemon testing): sd.query_devices() — which
    find_input_device calls — does a WASAPI/COM device enumeration that HANGS when it runs
    on a worker connection thread (where cmd_listen lives). Opening an InputStream from a
    worker thread is fine, but enumerating devices is not. So we resolve the device index
    ONCE here on the main thread and stash it on ctx.mic_dev_idx; core._capture_utterance
    reads that index and never calls find_input_device on the worker thread. We also open a
    short real capture here to fully prime PortAudio/WASAPI on the main thread.
    """
    try:
        import sounddevice as sd
        sd.query_devices()  # Pa_Initialize + host-API/device enumeration (main thread)
        try:
            idx = wl.find_input_device(wl.DEFAULT_MIC_SUBSTR)
            ctx.mic_dev_idx = idx
            if idx is None:
                # ── LOUD mic-fallback refusal (2026-07-13, echo-saga etiology fix) ──
                # The 17:58 boot race: daemon started before the Logitech USB stack
                # enumerated → name-match failed → idx None → every capture opened
                # the system DEFAULT input, which was 'CABLE Output (VB-Audio)' — a
                # loopback of everything the PC plays. My ears heard my own mouth
                # for ~90 min. Core now REFUSES device=None (deaf-but-honest); this
                # probe keeps hunting for the named mic so ears come back on their
                # own once USB settles. Thread is daemon=True and best-effort: if
                # worker-thread device enumeration hangs (the known WASAPI/COM
                # gotcha), we lose only the probe — never the daemon.
                print(f"[daemon] NAMED MIC '{wl.DEFAULT_MIC_SUBSTR}' NOT FOUND at "
                      "boot — ears will stay DOWN (no default-device fallback; "
                      "that's the loopback trap). Retry-probing every 5s...",
                      flush=True)
                def _mic_retry_probe() -> None:
                    for _ in range(720):   # 5s x 720 = 60 min of patience
                        time.sleep(5.0)
                        if ctx.mic_dev_idx is not None:
                            return
                        try:
                            i2 = wl.find_input_device(wl.DEFAULT_MIC_SUBSTR)
                        except Exception:
                            i2 = None
                        if i2 is not None:
                            ctx.mic_dev_idx = i2
                            print(f"[daemon] named mic APPEARED idx={i2} — ears "
                                  "back up (retry-probe)", flush=True)
                            return
                    print("[daemon] mic retry-probe gave up after 60 min — ears "
                          "still down; check the headset / relaunch", flush=True)
                threading.Thread(target=_mic_retry_probe, daemon=True,
                                 name="mic-retry-probe").start()
            else:
                with sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                                    device=idx, blocksize=1600) as s:
                    s.read(1600)
                print(f"[daemon] mic resolved idx={idx}; capture path primed", flush=True)
        except Exception as e:
            print(f"[daemon] capture-path warm warning: {e!r}", file=sys.stderr, flush=True)
        print("[daemon] audio (PortAudio/WASAPI) initialised on main thread", flush=True)
    except Exception as e:
        print(f"[daemon] audio init FAILED: {e!r}", file=sys.stderr, flush=True)


def _ears_on_boot(ctx: Ctx) -> None:
    """EARS ON BOOT (Iris port of Wren's shape, 2026-07-06, Zeke's pick): warm the FULL
    in-call pipeline (mouth + whisper + smart-turn + prosody, flip call_warm) at daemon
    launch — silently, so it never talks to an empty room. Gated on IRIS_EARS_ON_BOOT
    (default ON). Runs in a background thread so serving is never blocked; failure is
    non-fatal and leaves the daemon exactly as it was before this feature
    (no-worse-than-before)."""
    try:
        if os.environ.get("IRIS_EARS_ON_BOOT", "1").strip().lower() in ("0", "false", "off", "no"):
            print("[daemon] ears-on-boot: disabled via IRIS_EARS_ON_BOOT", flush=True)
            return
        # Wait for the whisper warm thread first: wl.get_whisper() is an UNLOCKED
        # singleton (global _WHISPER, no lock) — calling it concurrently from two
        # threads double-loads the model on the 3060. Wait, don't race.
        deadline = time.time() + 120.0
        while time.time() < deadline and getattr(wl, "_WHISPER", None) is None:
            time.sleep(1.0)
        st = core.cmd_call_start(ctx, {"silent": True, "mouth_deadline_s": 240.0})
        brief = {k: st.get(k) for k in ("mouth", "whisper", "smartturn", "prosody",
                                        "core_ready", "degraded")}
        print(f"[daemon] ears-on-boot: call_warm={ctx.call_warm} {brief}", flush=True)
    except Exception as e:
        print(f"[daemon] ears-on-boot FAILED (non-fatal): {e!r}",
              file=sys.stderr, flush=True)


def main() -> None:
    ctx = Ctx()

    # Warm threads (daemon so they don't block clean exit).
    threading.Thread(target=_warm_whisper, args=(ctx,),
                     daemon=True, name="wren-daemon-whisper-warm").start()
    threading.Thread(target=_warm_vad, args=(ctx,),
                     daemon=True, name="wren-daemon-vad-warm").start()

    # Single playback worker (daemon).
    threading.Thread(target=_playback_worker, args=(ctx,),
                     daemon=True, name="wren-daemon-playback").start()

    # MUST run on the main thread before serving — see _warm_audio_main docstring.
    _warm_audio_main(ctx)

    # EARS ON BOOT (after mic is resolved, before serving; background so the socket
    # comes up immediately — the warm rides alongside the first connections).
    threading.Thread(target=_ears_on_boot, args=(ctx,),
                     daemon=True, name="iris-ears-on-boot").start()

    server = _ThreadedTCPServer("127.0.0.1", PORT, ctx)
    print(f"[wren-voice-daemon] listening on 127.0.0.1:{PORT}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[wren-voice-daemon] shutting down", flush=True)
        server.shutdown()
    except Exception:
        # Last-resort visibility: a Python-level crash in the serve loop gets a full
        # traceback to stderr (→ persistent daemon.log) instead of a silent death.
        # A native crash (CUDA teardown segfault) is caught by faulthandler above.
        # (Wren's port, 37f3267)
        print("[wren-voice-daemon] FATAL in serve_forever:", file=sys.stderr, flush=True)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
