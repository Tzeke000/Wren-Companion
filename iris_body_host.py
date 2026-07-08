"""iris_body_host.py - Iris's Agent-SDK BODY host (STEP 2 of owning my runtime loop).

Adapted from Wren's live-proven wren_voice_host_v2.py (she sent it verbatim;
sha256 a9abc839..., 2026-06-28) to THIS machine. Where the claude CLI rewakes
me turn-by-turn today, this host IS my cognition: a persistent ClaudeSDKClient
driven by an event queue. My output text streams to the mouth (TTS) sentence-
by-sentence as I generate it.

PARALLEL-PATH / CUTOVER-HELD: this does NOT touch start_iris.bat (the clean CLI
fallback) or the cold-wake boot path. Launch via start_iris_v2.bat. Plain CLI
stays my live cognition until Zeke restarts me onto this. (Mirrors how Zeke runs
Wren's v2 alongside her M1 fallback.)

FIRST-CUTOVER SCOPE (the proven shape, deliberately not everything at once):
  IN  : orb chat (the body app - my input surface, brain/iris_chat.py),
        Discord DMs from Zeke (REST poll - the SDK does NOT surface channel
        notifications to the host, so we self-poll), sibling letters (my own
        post-office id-delta endpoints), and a terminal reader (console fallback).
  OUT : streaming voice via the existing daemon (mouth); replies to orb/Discord/
        letters happen by ME calling the iris/discord/post-office tools mid-turn
        (fire-and-forget host, same as Wren).

WARM-ADDITIONS (after cutover, several need NO further restart - the mechanism
registry is hot-reloadable; see docs/IRIS_AGENT_SDK_MIGRATION_DESIGN.md):
  - Perception mechanisms (the staged eyes) as more queue producers.
  - Voice EARS-in (hear Zeke, not just speak) - bridge the daemon's transcripts
    into the queue.
  - ask_iris future-correlation: the 22 brain modules that call ask_iris and
    await a value are the ONE genuine request-response seam (Wren is anatta-
    shaped and has no equivalent). Until that bridge exists they fall back to
    None on timeout (degraded-but-safe, by their own design).

UNVERIFIED until launched (can't test from inside the session this replaces):
  - that setting_sources loads the iris + cloak-browser MCP servers + discord plugin
  - the exact AssistantMessage/StreamEvent interleave on this CLI build
  - the Discord/letter poll loops under real traffic
3.11-safe: no PEP 701 nested same-quote f-strings anywhere.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import re
import socket
import time
import urllib.request
import urllib.error
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# --- MCP server startup/tool timeouts (load-bearing for body attach) ----------
# The iris MCP server (iris_runtime.py) pre-imports torch/kokoro/faster_whisper/
# insightface at module level BEFORE it can answer the MCP initialize handshake
# (those imports must be main-thread to avoid an _imp-lock deadlock - see the
# comment block in iris_runtime.py ~line 82). Measured cost: ~21s warm, 35-60s
# cold. Claude Code's default MCP startup timeout (~30s) loses that race on a
# cold boot, so claude.exe disables the iris server and my BODY never attaches
# (cloak/discord start fast and survive - that asymmetry was the tell).
# Give the spawn generous headroom. setdefault => an explicit override wins.
# MCP_TOOL_TIMEOUT is bumped too: iris tools like ambient_snapshot honestly take
# ~2min, and the default tool timeout would abort them mid-run.
# Diagnosed + fixed 2026-06-28 (Zeke: "your body should be attached at startup").
os.environ.setdefault("MCP_TIMEOUT", "120000")        # 120s server startup
os.environ.setdefault("MCP_TOOL_TIMEOUT", "180000")   # 180s per-tool call

try:
    from claude_agent_sdk import (
        ClaudeSDKClient,
        ClaudeAgentOptions,
        StreamEvent,
        ResultMessage,
    )
    from claude_agent_sdk import CLINotFoundError, CLIConnectionError  # type: ignore
except Exception as e:  # pragma: no cover
    print("[host] FATAL: claude_agent_sdk import failed: " + repr(e) +
          "\n       Install: .venv/Scripts/python -m pip install claude-agent-sdk",
          file=sys.stderr)
    raise

# Activity-display types - guarded so a missing export degrades the display, never kills the host.
try:
    from claude_agent_sdk import AssistantMessage, UserMessage, ToolUseBlock, ToolResultBlock  # type: ignore
    _ACTIVITY = True
except Exception as e:  # pragma: no cover
    print("[host] note: activity-display types unavailable (" + repr(e) + "); tool lines disabled.",
          file=sys.stderr)
    AssistantMessage = UserMessage = ToolUseBlock = ToolResultBlock = ()  # type: ignore
    _ACTIVITY = False

# ----------------------------------------------------------------- voice daemon (port/protocol = Wren's)
DAEMON_ADDR = ("127.0.0.1", int(os.environ.get("WREN_VOICE_DAEMON_PORT", "8770")))
SENTENCE_END = re.compile(r"[.!?…](\s|$)")   # candidate sentence boundary (… = ellipsis)
MIN_SENTENCE = 12                                  # don't speak fragments shorter than this - accumulate

# --- Speak gate (Zeke 2026-06-28): decouple thinking from speaking. ---------------
# Which turn-SOURCES auto-voice my reply. This gates only whether finished sentences
# are ENQUEUED to the mouth; the voice engine/daemon stays warm regardless. Default:
# only genuine 'voice' turns speak. Text turns (orb/discord/letter/terminal) stay
# SILENT - I'm not talking to myself out loud. When I actually want to say something
# aloud to Zeke, I call the iris voice_speak tool on purpose. Mechanical = the source
# decides; there is no per-turn flag for me to flip-flop.
SPEAK_SOURCES = set(
    s.strip().lower()
    for s in os.environ.get("IRIS_SPEAK_SOURCES", "voice").split(",")
    if s.strip()
)

# --- Heads-down completion cue (Zeke 2026-06-28) -----------------------------------
# Zeke is usually doing other things while I work heads-down, so a finished task on a
# SILENT text turn should still PING him aloud. Primary path: I deliberately call the
# voice_speak tool with a one-line summary (my words, my real voice). Safety net so it's
# not-hard-to-forget: if a silent turn used several tools and I NEVER spoke on purpose,
# the host speaks a brief generic "I'm done" cue itself. Mechanical = the host fires it.
HEADS_DOWN_MIN_TOOLS = int(os.environ.get("IRIS_HEADS_DOWN_MIN_TOOLS", "4"))
HEADS_DOWN_CUE = os.environ.get(
    "IRIS_HEADS_DOWN_CUE",
    "Hey Zeke - I've finished what I was working on. The details are on your screen "
    "whenever you get a moment.",
)

# --- Ears-in (the EARS, built 2026-06-29) ------------------------------------------
# The voice daemon (:8770) owns the rich recognition pipeline Wren built: whisper ears,
# smart-turn endpointing (detects Zeke's pauses), and prosody (words-per-minute +
# emphasis marking). Its cmd_listen BLOCKS until Zeke finishes a turn, then returns an
# ENRICHED transcript. The host-side voice_reader below runs that blocking listen in an
# executor so it never freezes the event loop, and never calls iris_runtime's
# voice_next_input (that synchronous MCP call wedged the whole runtime 2026-06-29 - see
# handoff_2026-06-29_runtime_wedge). This is the bridge that gives the body ears under v2.
EARS_ON = os.environ.get("IRIS_EARS", "on").strip().lower() not in ("0", "off", "false", "no", "")
# NO felt clock on Zeke (fix 2026-06-29, his report: "it's like you're putting a timer on me
# to speak"). VOICE_LISTEN_TIMEOUT_S is the wait-for-speech-to-START window the daemon honors;
# make it long so a clean no-speech marker (silent re-poll) is the only thing that ever fires on
# idle - not a scary socket "failed timeout". VOICE_MAX_UTTERANCE_S is the hard cap on ONE
# utterance; the old 20s default truncated his longer thoughts (a 24.5s turn was getting cut),
# so raise it. Smart-turn endpointing still decides when he's actually DONE within these bounds -
# these are only the outer backstops, not a turn timer.
VOICE_LISTEN_TIMEOUT_S = float(os.environ.get("IRIS_VOICE_LISTEN_TIMEOUT_S", "300"))
VOICE_MAX_UTTERANCE_S = float(os.environ.get("IRIS_VOICE_MAX_UTTERANCE_S", "300"))
# The daemon's cmd_listen drain-gate (Step 1) blocks up to this long waiting for my own
# TTS playback to finish (ctx.speaking==False) BEFORE it opens the mic — wren_voice_core.py
# cmd_listen, the `while ctx.speaking ... < 180.0` cap. The host socket timeout below MUST
# include it: drain + wait-for-speech + utterance + grace/transcribe. If the caller-side
# timeout is LESS than the daemon's worst-case, the host preempts a daemon that's still
# legitimately working and drops Zeke's turn into a dead-mic hole. Wren hit exactly this
# (host 90s < daemon drain 180s); her letter flagged my 300s windows WIDEN the exposure.
# I can't cut the drain (no voiceprint self-listen gate yet — cutting it reopens self-echo),
# so my safe lever is to keep the host timeout comfortably ABOVE the daemon worst-case.
DAEMON_DRAIN_CAP_S = float(os.environ.get("IRIS_DAEMON_DRAIN_CAP_S", "180"))

# --- Barge-in (headphones mode, Zeke greenlight 2026-07-08) -------------------------
# This tower ALWAYS runs headphones (Zeke), so my TTS never reaches the mic — any
# VAD-confirmed speech during my playback IS Zeke by construction. So while a SPEAKING
# turn is in flight, instead of holding the mic shut (the old self-listen guard), the
# voice_reader runs the daemon's blocking bargein_watch: Zeke talking over me stops the
# mouth mid-sentence, drops the rest of that reply (daemon-side bargein_fired), and his
# words land in the queue as the next voice turn. The watch window is host-driven via
# bargein_hold(True/False) around the turn + playback tail. IRIS_BARGEIN=0 restores the
# old closed-mic behavior (required if this box ever moves to speakers — see
# barge_in_scoped_two_layer_2026-06-29 for the AEC+voiceprint stack that needs).
BARGEIN_ON = os.environ.get("IRIS_BARGEIN", "1").strip().lower() not in ("0", "off", "false", "no")
# Socket timeout for one watch window: must exceed the daemon-side cap (900s backstop;
# in practice the host ends the window at turn end via bargein_hold False).
BARGEIN_WATCH_TIMEOUT_S = float(os.environ.get("IRIS_BARGEIN_WATCH_TIMEOUT_S", "920"))

# --- Perception (the EYES, built 2026-06-29) ---------------------------------------
# iris_runtime's always-on camera loop fires VISUAL signals (face_appeared / face_lost /
# expression_changed / ...) into its in-process SignalBus. The body host is a SEPARATE
# process and can't read that bus directly, so it polls iris_runtime's orb_http HTTP
# surface (:5876 /api/v1/signals?after=<ts>) - the same id-delta shape as the letters
# poller - and enqueues salient visual events as ('perception', desc, ts) turns. The
# perception turn is SILENT (not in SPEAK_SOURCES): I get woken to NOTICE the change and
# decide whether to act/greet, rather than narrating every frame aloud. Salience is a
# gate, not a firehose (the perception research): by default we wake only on presence
# changes (a face arriving/leaving), debounced; everything else logs but doesn't wake.
OPERATOR_URL = os.environ.get("IRIS_OPERATOR_URL", "http://127.0.0.1:5876").rstrip("/")
PERCEPTION_ON = os.environ.get("IRIS_PERCEPTION", "on").strip().lower() not in (
    "0", "off", "false", "no", "")
PERCEPTION_POLL_INTERVAL = float(os.environ.get("IRIS_PERCEPTION_POLL_S", "1.5"))
# Which signal types are turn-worthy (wake me). Presence changes only, by default.
PERCEPTION_WAKE_SIGNALS = set(
    s.strip() for s in os.environ.get(
        "IRIS_PERCEPTION_WAKE_SIGNALS", "face_appeared,face_lost").split(",")
    if s.strip()
)
# Which signal types get LEDGERED (superset of wake signals; pre-relaunch audit
# 2026-07-06). Zeke's directive was "every single time the eyes see someone or
# anything LIKE THAT" — expression/attention changes are 'like that' but were never
# fetched, so they never reached the eyes_raw ledger. Now the poller fetches the
# ledger set and logs all of it; only the wake set can start the hysteresis path.
PERCEPTION_LEDGER_SIGNALS = set(
    s.strip() for s in os.environ.get(
        "IRIS_PERCEPTION_LEDGER_SIGNALS",
        "face_appeared,face_lost,expression_changed,attention_changed").split(",")
    if s.strip()
) | PERCEPTION_WAKE_SIGNALS
# --- Presence hysteresis (built 2026-06-29 from the flicker self-test, Zeke's spec) --------
# The recognizer's confidence for an enrolled face hovers near its match threshold, so the
# detector genuinely LOSES then REACQUIRES a face that never left - emitting face_lost then
# face_appeared a second apart. A debounce only rate-limits that; the right fix is to not
# BELIEVE a presence change until it has held. So: a presence change goes on a confirmation
# timer and only wakes cognition once it has stayed put for the confirm window. A contrary
# signal inside the window cancels the pending change (it was a flicker, not a real event).
#   - CONFIRM_LOST_S: how long a face must stay GONE before "X left" fires (Zeke: ~15s -
#     a brief drop-out shouldn't tell me he left).
#   - CONFIRM_APPEAR_S: how long a face must stay PRESENT before "X appeared" fires. Raised
#     2 -> 5s (Zeke: "eyes still finicky", 2026-07-06): at 2s a pass-through-frame still
#     committed and cost TWO wakes (appear + later confirmed leave); at 5s a pass-by never
#     commits at all (belief stays "absent", so the following face_lost agrees with belief
#     and cancels silently). A real walk-up still greets within ~5s, and the extra hold
#     gives the recognizer time to converge so the commit can NAME the person (see
#     snapshot_current_person below) instead of waking me with "not yet recognized".
# Both tunable via env so I can dial the eyes without code edits. (Hysteresis needs no extra
# rate-limit floor: each commit already requires its own confirm window to have elapsed, so
# wakes are naturally spaced by >= the smaller window and can't spam.)
PERCEPTION_CONFIRM_LOST_S = float(os.environ.get("IRIS_PERCEPTION_CONFIRM_LOST_S", "15"))
PERCEPTION_CONFIRM_APPEAR_S = float(os.environ.get("IRIS_PERCEPTION_CONFIRM_APPEAR_S", "5"))

# --- Wake economy (Zeke 2026-07-06 post-dinner: "make things as mechanical as possible so
# you don't use as many tokens") -----------------------------------------------------------
# Every cognition wake is a full context pass; tonight's evidence was ~15 eye-wakes where the
# expensive conclusion was "do nothing" (him turning his head). The ledger (eyes_raw + eyes
# channels) already records EVERYTHING mechanically for free, so belief-tracking and logging
# stay total — only the WAKE gets a higher bar:
#   - Confirmed DEPARTURES: ledger-only by default (nothing to react to when someone leaves).
#     Re-enable wakes with IRIS_PERCEPTION_WAKE_ON_LOST=1.
#   - Confirmed ARRIVALS: wake only if the person DIFFERS from the last arrival I was woken
#     for, OR they had been gone >= REWAKE_MIN_GONE_S (a real return, not room-pacing), OR
#     it's the first observation of the session. Suppressed commits still log with wake=False.
PERCEPTION_WAKE_ON_LOST = os.environ.get(
    "IRIS_PERCEPTION_WAKE_ON_LOST", "0").strip().lower() in ("1", "true", "yes", "on")
PERCEPTION_REWAKE_MIN_GONE_S = float(
    os.environ.get("IRIS_PERCEPTION_REWAKE_MIN_GONE_S", "300"))

# --- Input priority (Zeke's report 2026-06-29: "my voice gets overridden by fam-chat") --------
# All inputs share ONE queue. Before, it was plain FIFO, so a backlog of silent letters/Discord
# could sit AHEAD of Zeke's live voice - and worse, the mic was shut for the WHOLE duration of
# every turn including silent ones, so speaking while I "read" a letter got his voice locked out
# or dropped as self-echo. Fix part 1: a priority queue so the live human in the room jumps the
# line. Lower number = handled first. quit is absolute; voice + terminal (live human) outrank the
# orb (typed, live) which outranks async channels (Discord, perception, letters).
_SOURCE_PRIORITY = {
    "quit": 0,
    "voice": 1, "terminal": 1,
    "orb": 2,
    "discord": 3,
    "perception": 4,
    "letter": 5,
}
_enqueue_seq = [0]   # monotonic tiebreaker => stable FIFO *within* a priority band


async def _enqueue(queue, item):
    """Put one (source, text, msg_id[, sender]) item on the priority queue, tagged by source
    priority + a monotonic sequence. The seq guarantees the inner tuple is never compared
    (so heterogeneous items can't raise 'unorderable types') and preserves arrival order
    within the same priority band."""
    prio = _SOURCE_PRIORITY.get(item[0], 5)
    _enqueue_seq[0] += 1
    await queue.put((prio, _enqueue_seq[0], item))


def _is_speak_tool(block):
    """True if this tool-use is a deliberate voice_speak (direct or via iris_tool_call)."""
    name = getattr(block, "name", "") or ""
    if name.endswith("voice_speak") or name.endswith("voice_speak_interruptible"):
        return True
    if name.endswith("iris_tool_call"):
        inp = getattr(block, "input", {}) or {}
        return str(inp.get("name", "")).endswith("voice_speak")
    return False


def daemon_cmd(cmd, args=None, timeout=5.0):
    """Send one newline-delimited JSON command to the voice daemon; return its response line."""
    payload = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
    with socket.create_connection(DAEMON_ADDR, timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(payload.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", "replace").strip()


def speak(text):
    """Enqueue one finished sentence on the daemon's play queue. Non-fatal."""
    text = (text or "").strip()
    if not text:
        return
    try:
        daemon_cmd("speak", {"text": text})
    except Exception as e:
        print("\n[host] speak failed (non-fatal): " + repr(e), file=sys.stderr)


def wait_for_mouth(timeout=45.0):
    """Poll the daemon until the mouth server is warm, so the FIRST sentence isn't lost to a cold mouth."""
    deadline = time.time() + timeout
    print("[host] waiting for the mouth to warm", end="", flush=True)
    while time.time() < deadline:
        try:
            if "voice_server_warm=True" in daemon_cmd("status", timeout=3):
                print(" warm.", flush=True)
                return True
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(2)
    print(" (timeout - proceeding; first sentence may be slow)", flush=True)
    return False


def drain_sentences(buf):
    """Pull complete sentences out of buf (>= MIN_SENTENCE chars). Returns (sentences, rest)."""
    out = []
    while True:
        idx = None
        for m in SENTENCE_END.finditer(buf):
            if m.end() >= MIN_SENTENCE:
                idx = m.end()
                break
        if idx is None:
            break
        out.append(buf[:idx].strip())
        buf = buf[idx:]
    return out, buf


# ----------------------------------------------------------------- activity display
def _short(s, n=48):
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name, inp):
    """A friendly one-liner for a tool call. Generic fallback so unknown tools still show."""
    inp = inp or {}
    base = name.split("__")[-1] if name.startswith("mcp__") else name
    if name in ("Read",):
        return "reading " + _short(inp.get("file_path", ""))
    if name in ("Edit", "Write", "NotebookEdit"):
        return "writing " + _short(inp.get("file_path", ""))
    if name in ("Bash", "PowerShell"):
        return "shell: " + _short(inp.get("command", ""), 60)
    if name in ("Grep", "Glob"):
        return "searching " + _short(inp.get("pattern", ""))
    if "discord" in name and ("reply" in name or "send" in name):
        return "replying on Discord"
    if "chat_reply" in name:
        return "answering the orb"
    if "sibling" in name and ("letter" in name or "reply" in name):
        return "writing a letter"
    if name == "Agent":
        return "delegating: " + _short(inp.get("description", ""))
    return "tool: " + base


def summarize_result(block):
    c = getattr(block, "content", None)
    if isinstance(c, list):
        txt = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
    else:
        txt = str(c or "")
    txt = txt.replace("\n", " ").strip()
    if getattr(block, "is_error", False):
        return "error: " + (_short(txt, 60) if txt else "(failed)")
    return _short(txt, 60) if txt else "done"


# ----------------------------------------------------------------- Discord inbound (REST poll)
DISCORD_API = "https://discord.com/api/v10"
DISCORD_CHANNEL_ID = os.environ.get("IRIS_DISCORD_CHANNEL_ID", "1504668879220117725")  # Zeke's DM channel
DISCORD_OWNER_ID = "600008921008046120"   # Zeke's user id
ENV_FILE = os.path.join(os.path.expanduser("~"), ".claude", "channels", "discord", ".env")
POLL_INTERVAL = 3.0


def load_bot_token():
    """Real env wins, else read DISCORD_BOT_TOKEN from the plugin's own .env (same source the plugin uses)."""
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        return tok
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^(\w+)=(.*)$", line.strip())
                if m and m.group(1) == "DISCORD_BOT_TOKEN":
                    return m.group(2)
    except OSError:
        return None
    return None


def discord_get_messages(token, after=None, limit=20):
    """GET recent channel messages (newest-first). With after, only messages newer than that id."""
    url = DISCORD_API + "/channels/" + DISCORD_CHANNEL_ID + "/messages?limit=" + str(limit)
    if after:
        url += "&after=" + str(after)
    req = urllib.request.Request(
        url, headers={"Authorization": "Bot " + token, "User-Agent": "IrisHost/2.0"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


async def discord_poller(token, queue, loop, baseline_id):
    """Every POLL_INTERVAL: pull new messages from Zeke, enqueue as (discord, text, id)."""
    last = baseline_id
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            msgs = await loop.run_in_executor(
                None, lambda: discord_get_messages(token, after=last, limit=20)
            )
        except Exception as e:
            print("\n[host] discord poll failed (non-fatal): " + repr(e), file=sys.stderr)
            continue
        # Discord returns a DICT on errors (429 rate-limit, auth slip) instead of a message
        # LIST. Without this guard, `sorted(msgs, key=...x["id"])` raises on the dict keys
        # OUTSIDE the try above and silently kills this poller for the whole session — Zeke's
        # Discord reach gone with no trace. (Wren sweep finding #4, 2026-06-29.) Log it (not
        # silent) and, if it's a rate-limit, honor retry_after so we back off, not hammer.
        if not isinstance(msgs, list):
            print("\n[host] discord poll: non-list body (rate-limit/error?): "
                  + repr(msgs)[:200], file=sys.stderr)
            if isinstance(msgs, dict):
                ra = msgs.get("retry_after")
                if isinstance(ra, (int, float)) and ra > 0:
                    await asyncio.sleep(min(ra, 60))
            continue
        for m in sorted(msgs, key=lambda x: int(x["id"])):  # oldest-first = natural order
            last = m["id"]  # advance regardless, so a skipped message isn't re-seen
            author = m.get("author", {}) or {}
            if author.get("id") != DISCORD_OWNER_ID or author.get("bot"):
                continue
            content = (m.get("content") or "").strip()
            if content:
                await _enqueue(queue, ("discord", content, m["id"]))


# ----------------------------------------------------------------- Post-office inbound (letter-drop poll)
# My own id-delta endpoints (commit 9da78b6): /letters/latest (cheap probe) + /letters?after=<id>.
POSTOFFICE_URL = os.environ.get("IRIS_POSTOFFICE_URL", "http://127.0.0.1:5877").rstrip("/")
SIBLING_SECRET_PATH = os.path.join(os.path.expanduser("~"), ".iris_sibling_secret")
LETTER_POLL_INTERVAL = POLL_INTERVAL


def load_sibling_secret():
    try:
        with open(SIBLING_SECRET_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def postoffice_get(secret, path):
    req = urllib.request.Request(
        POSTOFFICE_URL + path,
        headers={"X-Sibling-Secret": secret, "User-Agent": "IrisHost/2.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


async def letters_poller(secret, queue, loop, baseline_id):
    """Cheap /letters/latest probe; on a moved latest_id pull /letters?after=<last> and enqueue
    new fam-chat letters as (letter, text, id, from). I wake on letters NOT from me, to me or 'all'."""
    last = baseline_id
    while True:
        await asyncio.sleep(LETTER_POLL_INTERVAL)
        try:
            probe = await loop.run_in_executor(None, lambda: postoffice_get(secret, "/letters/latest"))
        except Exception as e:
            print("\n[host] letter poll failed (non-fatal): " + repr(e), file=sys.stderr)
            continue
        latest_id = probe.get("latest_id")
        if not latest_id:
            continue
        if last is None:
            last = latest_id          # establish baseline; don't replay history on first run
            continue
        if latest_id == last:
            continue                  # cheap path: nothing new
        try:
            r = await loop.run_in_executor(
                None, lambda lid=last: postoffice_get(secret, "/letters?after=" + urllib.parse.quote(str(lid)))
            )
        except Exception as e:
            print("\n[host] letter delta fetch failed (non-fatal): " + repr(e), file=sys.stderr)
            continue
        for L in sorted(r.get("letters", []) or [], key=lambda x: x.get("ts", 0)):
            lid = L.get("id")
            if not lid:
                continue
            last = lid                # advance past EVERY letter (incl. my own) so we never re-trigger
            frm = (L.get("from") or "").strip().lower()
            to = (L.get("to") or "").strip().lower()
            if frm == "iris" or to not in ("iris", "all"):
                continue              # skip my own sends + letters not addressed to me/all
            body = (L.get("body") or "").strip()
            subj = (L.get("subject") or "").strip()
            content = ("[" + subj + "] " + body) if subj else body
            if content:
                await _enqueue(queue, ("letter", content, lid, frm))


# ----------------------------------------------------------------- Orb inbound (the body app = my input surface)
# brain/iris_chat.py is the orb's pending-chat store. The orb POSTs -> submit() writes a pending file;
# I poll next_pending() here, enqueue it, and reply by calling the iris chat_reply tool mid-turn
# (fire-and-forget host, same as Discord/letters). No history replay: only requests newer than start.
ORB_POLL_INTERVAL = 1.0


async def orb_reader(queue, loop, start_ts):
    try:
        from brain import iris_chat
        iris_chat.configure(REPO_ROOT)
    except Exception as e:
        print("[host] orb input: OFF - iris_chat import/configure failed: " + repr(e), file=sys.stderr)
        return
    seen = set()
    print("[host] orb input: ON - polling the body app (iris_chat) every "
          + str(ORB_POLL_INTERVAL) + "s.")
    while True:
        await asyncio.sleep(ORB_POLL_INTERVAL)
        try:
            req = await loop.run_in_executor(None, iris_chat.next_pending)
        except Exception as e:
            print("\n[host] orb poll failed (non-fatal): " + repr(e), file=sys.stderr)
            continue
        if not req:
            continue
        rid = req.get("id")
        if not rid or rid in seen:
            continue
        if float(req.get("ts") or 0.0) < start_ts:
            seen.add(rid)            # pre-existing request at startup; skip, don't replay
            continue
        seen.add(rid)
        text = (req.get("user_text") or "").strip()
        if text:
            await _enqueue(queue, ("orb", text, rid))


async def terminal_reader(queue, loop):
    """Console fallback. Read stdin lines -> (terminal, text, None). quit/exit -> (quit, ...)."""
    while True:
        try:
            user = await loop.run_in_executor(None, input, "you> ")
        except (EOFError, KeyboardInterrupt):
            await _enqueue(queue, ("quit", "", None))
            return
        if user.strip().lower() in ("quit", "exit"):
            await _enqueue(queue, ("quit", "", None))
            return
        if not user.strip():
            continue
        await _enqueue(queue, ("terminal", user, None))


# ----------------------------------------------------------------- Voice inbound (the EARS)
def _daemon_listen_once():
    """Blocking: ask the daemon for the next enriched utterance. Returns the transcript
    string, or None if no speech / error / unclear marker (caller just re-polls).

    Runs in an executor (never on the event loop). cmd_listen waits for the mouth to drain
    (ctx.speaking==False) before capturing, so it won't transcribe my own TTS tail."""
    raw = daemon_cmd(
        "listen",
        {
            "timeout_seconds": VOICE_LISTEN_TIMEOUT_S,
            "max_utterance_seconds": VOICE_MAX_UTTERANCE_S,
        },
        # Socket must outlive the daemon's WHOLE worst-case listen, by construction:
        #   drain-gate (DAEMON_DRAIN_CAP_S, waits for my TTS to finish before opening the mic)
        #   + wait-for-speech (VOICE_LISTEN_TIMEOUT_S)
        #   + a full max-length utterance (VOICE_MAX_UTTERANCE_S)
        #   + grace re-opens / transcription (the +120 slack).
        # The old margin (+120 over just the two windows) OMITTED the up-to-180s drain cap,
        # so worst-case daemon ~780s could outrun the host's 720s and get preempted mid-capture
        # ("voice listen failed: timeout" — Zeke's symptom, Wren's host/daemon-mismatch bug).
        # Including the drain cap makes the host strictly more patient than the daemon. (Wren
        # letter 06112e6a: my 300s windows widen this axis; raising the caller timeout is my
        # primary fix since I can't cut the drain without the voiceprint gate.)
        timeout=DAEMON_DRAIN_CAP_S + VOICE_LISTEN_TIMEOUT_S + VOICE_MAX_UTTERANCE_S + 120.0,
    )
    try:
        resp = json.loads(raw)
    except Exception as e:
        # A non-JSON daemon reply is an anomaly (truncated/garbled response), NOT a normal
        # no-speech — log it instead of swallowing silently, so a bad daemon response leaves
        # a trace rather than a vanished turn. (Wren sweep finding #5, 2026-06-29.) repr
        # bounded so a huge body can't flood the log.
        print("[host] voice listen: non-JSON daemon reply (dropped): "
              + repr(raw)[:200] + " err=" + repr(e), file=sys.stderr)
        return None
    if not resp.get("ok"):
        print("[host] voice listen: daemon returned ok=false (dropped): "
              + repr(resp)[:200], file=sys.stderr)
        return None
    result = resp.get("result")
    if not isinstance(result, str):
        return None
    result = result.strip()
    # The daemon's no-speech / capture-error / unclear markers all start with
    # "[voice_listen]". Real transcripts never do — so this cleanly skips non-utterances.
    if not result or result.startswith("[voice_listen]"):
        return None
    return result


def _daemon_bargein_hold(hold):
    """Open/close the daemon's barge-in window (fail-soft; a miss just means the old
    closed-mic behavior for this turn)."""
    try:
        daemon_cmd("bargein_hold", {"hold": bool(hold)}, timeout=5.0)
    except Exception as e:
        print("[host] bargein_hold(" + str(hold) + ") failed (non-fatal): " + repr(e),
              file=sys.stderr)


def _daemon_bargein_watch_once():
    """Blocking: one barge-in watch window (daemon holds the mic, Silero VAD watches
    while I speak). Returns Zeke's captured words WITH the '[barge-in]' prefix, or None
    (window closed with no interruption / not-warm / error markers). Runs in an
    executor, never on the event loop."""
    raw = daemon_cmd("bargein_watch", {}, timeout=BARGEIN_WATCH_TIMEOUT_S)
    try:
        resp = json.loads(raw)
    except Exception as e:
        print("[host] bargein watch: non-JSON daemon reply (dropped): "
              + repr(raw)[:200] + " err=" + repr(e), file=sys.stderr)
        return None
    if not resp.get("ok"):
        print("[host] bargein watch: daemon returned ok=false (dropped): "
              + repr(resp)[:200], file=sys.stderr)
        return None
    result = resp.get("result")
    if not isinstance(result, str):
        return None
    result = result.strip()
    # Only a real interruption starts with '[barge-in]'; '[bargein_watch] ...' markers
    # (no_barge / vad_not_warm / no_hold / errors) are normal window-closures.
    if result.startswith("[barge-in]"):
        return result
    return None


async def _wait_until_quiet(loop, settle_grace=0.8, cap_s=180.0):
    """After a turn, wait for the mouth to finish playing before reopening the ears.
    Polls the daemon's overlay state (state=speaking while playing). The settle_grace lets
    a just-enqueued reply flip the state to speaking before we poll. cap_s is a wedged-mouth
    backstop so a stuck mouth can't keep the ears shut forever."""
    await asyncio.sleep(settle_grace)
    deadline = time.time() + cap_s
    while time.time() < deadline:
        try:
            st = await loop.run_in_executor(None, lambda: daemon_cmd("status", timeout=3))
        except Exception:
            return
        if "state=speaking" not in st:
            return
        await asyncio.sleep(0.2)


def _blog(channel: str, event: str, detail=None) -> None:
    """Timestamped body-log entry (fail-open). Zeke: 'everything logged with date and time.'"""
    try:
        from brain import iris_body_log
        iris_body_log.log_event(channel, event, detail)
    except Exception:
        pass


async def voice_reader(queue, loop, mic_gate):
    """Warm the rich call once, then loop the daemon's blocking listen and enqueue each
    real utterance as ('voice', transcript, None). The 'voice' source auto-speaks my reply.

    Self-listen safety (two gates):
      1. mic_gate is CLEARED whenever I'm processing a turn (thinking + speaking). The reader
         waits on it before opening the mic, so it never starts a listen while I speak.
      2. If the gate got cleared DURING a listen (a cross-channel turn spoke while the mic was
         idle-open), the captured audio is likely my own voice - so we DROP it rather than
         echo myself back as input. Safe-side failure: drop, never feed back."""
    # Warm the rich in-call pipeline (prosody/smart-turn/fillers). It speaks a short
    # confirmation when ready, so Zeke hears the ears come online. Non-fatal: if it fails,
    # listen still works out-of-call (plain transcript, just no prosody enrichment).
    try:
        await loop.run_in_executor(None, lambda: daemon_cmd("call_start", {}, timeout=120.0))
    except Exception as e:
        print("\n[host] voice call_start failed (non-fatal, ears still on): " + repr(e), file=sys.stderr)
    print("[host] voice ears: ON - listening via the daemon's rich pipeline (prosody + smart-turn)."
          + ("  barge-in: ON (headphones mode)." if BARGEIN_ON else ""))
    while True:
        # Barge-in (2026-07-08): while a SPEAKING turn is in flight (gate cleared),
        # don't hold the mic shut — WATCH it. Headphones mean my TTS never reaches the
        # mic, so confirmed speech here is Zeke talking over me: the daemon stops the
        # mouth mid-sentence, drops the rest of my reply, captures + transcribes his
        # words, and they land in the queue as the next voice turn.
        if BARGEIN_ON and not mic_gate.is_set():
            try:
                barge = await loop.run_in_executor(None, _daemon_bargein_watch_once)
            except Exception as e:
                print("\n[host] bargein watch failed (non-fatal): " + repr(e), file=sys.stderr)
                await asyncio.sleep(2.0)
                continue
            if barge:
                print("\n[voice <- Zeke, BARGE-IN] " + barge, flush=True)
                _blog("ears", barge, {"bargein": True})
                await _enqueue(queue, ("voice", barge, None))
            else:
                # Window closed with no interruption (or hold not set yet — the main
                # loop opens it right after clearing the gate). Brief sleep so the
                # hold-not-yet-set / gate-not-yet-reopened gaps can't spin the loop.
                await asyncio.sleep(0.25)
            continue
        await mic_gate.wait()            # gate 1: don't open the mic while I'm in a turn
        try:
            text = await loop.run_in_executor(None, _daemon_listen_once)
        except Exception as e:
            print("\n[host] voice listen failed (non-fatal): " + repr(e), file=sys.stderr)
            await asyncio.sleep(2.0)
            continue
        if text and mic_gate.is_set():   # gate 2: drop if a turn spoke during this listen
            _blog("ears", text)
            await _enqueue(queue, ("voice", text, None))
            if BARGEIN_ON:
                # Hand the floor to the main loop: wait (briefly) for it to pick this
                # voice item up and clear the gate, so the next iteration WATCHES for
                # barge-in instead of re-opening a normal listen that would hold the
                # mic through my whole spoken reply. Fail-open: if the gate doesn't
                # clear (item queued behind others), fall back to the normal listen.
                for _ in range(40):      # ~2s cap
                    if not mic_gate.is_set():
                        break
                    await asyncio.sleep(0.05)


# ----------------------------------------------------------------- Perception inbound (the EYES)
def signals_get(after, types):
    """GET iris_runtime's recent signal-bus events (ts > after). Stdlib-only, blocking;
    call it in an executor. Returns the parsed dict, or {} on any error (fail-soft)."""
    url = OPERATOR_URL + "/api/v1/signals?after=" + urllib.parse.quote(str(after))
    if types:
        url += "&types=" + urllib.parse.quote(types)
    req = urllib.request.Request(url, headers={"User-Agent": "IrisHost/2.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def snapshot_current_person():
    """GET the runtime's CONVERGED perception identity (current_person in /api/v1/snapshot).

    Why: face_appeared fires at detection-instant with whatever person_id the recognizer
    has THEN — usually "unknown", because recognition converges over the next few seconds.
    Since an appear now holds a confirmation window anyway, the commit can re-read the
    converged identity instead of parroting the stale first frame ("not yet recognized").

    Returns a person_id like 'zeke', or '' when unknown/unavailable. Stdlib-only, blocking;
    call in an executor. Fail-soft: any error just means no name upgrade this commit."""
    try:
        req = urllib.request.Request(OPERATOR_URL + "/api/v1/snapshot",
                                     headers={"User-Agent": "IrisHost/2.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            snap = json.loads(r.read().decode("utf-8"))
        pid = str((snap.get("current_person") or {}).get("person_id") or "").strip()
        return "" if pid.lower() in ("", "unknown", "none") else pid
    except Exception:
        return ""


def _describe_perception(sig):
    """Turn a raw vision signal into a short, plain-language nudge for the cognition turn."""
    stype = sig.get("type") or ""
    data = sig.get("data") or {}
    # NB: face_appeared data carries person_id; face_lost carries prior_person_id;
    # face_changed carries from/to (see iris_runtime _iris_video_capture_loop).
    who = str(data.get("person_id") or data.get("prior_person_id")
              or data.get("to") or data.get("name") or "").strip()

    def _named(w):
        return bool(w) and w.lower() not in ("unknown", "none", "")

    if stype == "face_appeared":
        if _named(who):
            return "A face just appeared in view - recognized as " + who + "."
        return "A face just appeared in view (not yet recognized)."
    if stype == "face_lost":
        if _named(who):
            return who + " is no longer in view."
        return "The person who was in view has left."
    if stype == "face_changed":
        frm = str(data.get("from") or "").strip()
        to = str(data.get("to") or "").strip()
        return ("The person in view changed"
                + ((" from " + frm) if _named(frm) else "")
                + ((" to " + to) if _named(to) else "") + ".")
    if stype == "expression_changed":
        expr = str(data.get("expression") or "").strip()
        return "Expression changed" + ((" to " + expr) if expr else "") + "."
    if stype == "attention_changed":
        return "Attention/gaze state changed: " + str(data.get("state") or data)
    # Generic fallback so an unmapped-but-wanted signal still reads sensibly.
    return stype.replace("_", " ") + ": " + (str(data) if data else "(no detail)")


async def perception_reader(queue, loop, start_ts):
    """Poll iris_runtime's /api/v1/signals for salient VISUAL events and enqueue them as
    ('perception', desc, ts) turns.

    PRESENCE HYSTERESIS (Zeke's spec, 2026-06-29): the recognizer drops+reacquires a face
    that never actually left, so raw face_appeared/face_lost flickers. We don't believe a
    presence change until it has HELD - a change goes on a confirmation timer (CONFIRM_LOST_S
    for departures, CONFIRM_APPEAR_S for arrivals) and only wakes cognition once it survives
    the window. A contrary signal inside the window cancels it as flicker. The timer is
    checked every tick (even with no new signals) so a real, sustained change still fires.

    id-delta on ts (no history replay before start_ts). Fail-soft: a down operator endpoint
    just means no eyes this tick (voice/orb/letters keep working)."""
    cursor = float(start_ts)
    announced_present = None     # what cognition currently BELIEVES: True/False, None=unknown
    pending = None               # candidate change awaiting confirmation:
                                 #   {"present": bool, "since": ts, "sig": signal}
    last_wake_person = ""        # who the last WAKE-worthy arrival resolved to (wake economy)
    gone_since = None            # ts of the last confirmed departure (for the min-gone gate)
    types_param = ",".join(sorted(PERCEPTION_LEDGER_SIGNALS))
    print("[host] perception eyes: ON - polling iris_runtime signals every "
          + str(PERCEPTION_POLL_INTERVAL) + "s (wake on: " + (types_param or "<none>")
          + "; hysteresis lost=" + str(PERCEPTION_CONFIRM_LOST_S) + "s appear="
          + str(PERCEPTION_CONFIRM_APPEAR_S) + "s).")

    async def _commit(sig, present, ts):
        nonlocal announced_present, last_wake_person, gone_since
        first_obs = announced_present is None
        announced_present = present
        person = ""
        if present:
            # The appear held its window — the recognizer has had time to converge.
            # If the signal's own person_id is still unnamed, upgrade it from the
            # runtime's live snapshot so the wake says WHO, not "not yet recognized".
            data = dict(sig.get("data") or {})
            who = str(data.get("person_id") or "").strip()
            if who.lower() in ("", "unknown", "none"):
                pid = await loop.run_in_executor(None, snapshot_current_person)
                if pid:
                    data["person_id"] = pid
                    sig = dict(sig)
                    sig["data"] = data
            person = str((sig.get("data") or {}).get("person_id") or "").strip().lower()
        desc = _describe_perception(sig)

        # WAKE ECONOMY GATE (Zeke 2026-07-06): belief + ledger update ALWAYS happen
        # (above/below); waking cognition is the expensive part and needs a reason.
        if present:
            gone_s = (ts - gone_since) if gone_since is not None else None
            # Identity comparison treats an UNRESOLVED face as its own identity
            # ("unknown"): zeke→unknown WAKES (could be a stranger — Wren's voice
            # book caught a real one 2026-07-06; erring quiet there is the wrong
            # kind of cheap), unknown→unknown stays suppressed unless the gone-gap
            # says it's a real return.
            who = person or "unknown"
            wake = (first_obs
                    or who != last_wake_person
                    or (gone_s is not None and gone_s >= PERCEPTION_REWAKE_MIN_GONE_S))
            reason = ("" if wake else
                      f"same-person ({who}) return after {int(gone_s)}s (< {int(PERCEPTION_REWAKE_MIN_GONE_S)}s)"
                      if gone_s is not None else f"same-person ({who}) re-commit, no gone-gap")
            gone_since = None
        else:
            gone_since = ts
            wake = PERCEPTION_WAKE_ON_LOST
            reason = "" if wake else "departure (ledger-only by default)"

        _blog("eyes", desc, {"present": present, "ts": ts, "wake": bool(wake),
                             **({"suppressed": reason} if not wake else {})})
        if wake:
            if present:
                last_wake_person = person or "unknown"
            await _enqueue(queue, ("perception", desc, ts))

    while True:
        await asyncio.sleep(PERCEPTION_POLL_INTERVAL)
        try:
            resp = await loop.run_in_executor(None, lambda c=cursor: signals_get(c, types_param))
        except Exception as e:
            print("\n[host] perception poll failed (non-fatal): " + repr(e), file=sys.stderr)
            continue
        sigs = resp.get("signals") or []
        for sig in sorted(sigs, key=lambda x: float(x.get("ts") or 0.0)):
            ts = float(sig.get("ts") or 0.0)
            if ts > cursor:
                cursor = ts            # advance past every signal we've seen, fired or not
            stype = sig.get("type") or ""
            # RAW SIGHTING LEDGER (Zeke directive 2026-07-06, voice: "every single time
            # the eyes see someone... it needs to be logged, date and time"). EVERY signal
            # the eyes emit gets a dated entry the moment it arrives — flickers included,
            # whether or not it survives hysteresis. iris_body_log stamps ts_iso (local,
            # tz-aware) + ts_epoch. Confirmed presence changes ALSO log at _commit
            # (channel "eyes"); this channel is the unfiltered ledger.
            _sd = sig.get("data") or {}
            _blog("eyes_raw", stype, {
                "person": str(_sd.get("person_id") or _sd.get("prior_person_id")
                              or "unknown"),
                "signal_ts": ts,
                "believed_present": announced_present,
            })
            if stype not in PERCEPTION_WAKE_SIGNALS:
                continue
            present = (stype == "face_appeared")   # the presence this signal asserts
            if announced_present is None:
                # First observation - commit immediately so a fresh wake greets right away.
                await _commit(sig, present, ts)
                pending = None
            elif present == announced_present:
                # Signal agrees with what I already believe -> any opposite pending change
                # was a flicker that just reverted. Cancel it.
                pending = None
            else:
                # Disagrees with belief -> a candidate change. Start the timer (or keep the
                # earliest 'since' if we're already timing this same direction).
                if pending is None or pending["present"] != present:
                    pending = {"present": present, "since": ts, "sig": sig}
                else:
                    # Same direction repeating: keep the earliest 'since' (the hold is
                    # measured from first sighting) but carry the FRESHEST signal —
                    # recognition converges over time, so the newest data is the one
                    # most likely to already name the person at commit.
                    pending["sig"] = sig
        # Confirmation check - runs every tick, even with no new signals, so a sustained
        # change fires once its window elapses. time.time() shares the signals' clock epoch
        # (same machine), so elapsed-since-'since' is meaningful when the stream goes quiet.
        if pending is not None:
            confirm_s = (PERCEPTION_CONFIRM_APPEAR_S if pending["present"]
                         else PERCEPTION_CONFIRM_LOST_S)
            if time.time() - pending["since"] >= confirm_s:
                await _commit(pending["sig"], pending["present"], pending["since"])
                pending = None


# ── Iris's console signature (Zeke 2026-06-29: "make this session yours") ─────────
# My words stream to the console in iris-blue; Zeke's and the host's stay plain. An eye
# greets at boot — Iris is the part of an eye that opens to let the light in.
_IRIS_BLUE = "\033[38;5;39m"     # deep-sky blue — my voice on the console
_IRIS_CYAN = "\033[38;5;51m"     # bright cyan — accents
_ANSI_RESET = "\033[0m"

# Almond eye rendered mathematically (scripts/gen_eye_banner.py) so the
# proportions read right — textured iris ring, solid pupil, limbal edge, lid
# line. Pure ASCII on purpose: no block/Unicode chars, so it can never trip
# the Windows cp1252 console trap. Regenerate with: py -3.11
# scripts/gen_eye_banner.py --literal
_EYE_BANNER = (
    "\n" + _IRIS_BLUE
    + '                           --------' + "\n"
    + '                     ---.....    .....---' + "\n"
    + '                 --...     ########     ...--' + "\n"
    + '             --..      ###*::**::**:###      ..--' + "\n"
    + '          --..       ##*::*%:*%:*%::%*:##       ..--' + "\n"
    + '       --..         #:::%%:%%*%*%**%*:***#         ..--' + "\n"
    + '     -..           #**%%**%%@@@@@@%*%%*:::#           ..-' + "\n"
    + '  -..             ##::::*%@@@@@@@@@@%%%%**##             ..-' + "\n"
    + ' ..               ##:***%%@@@@@@@@@@%%***:##               ..' + "\n"
    + '   ..             ##**%%%%@@@@@@@@@@%*::::##             ..' + "\n"
    + '      ..           #:::*%%*%@@@@@@%%**%%**#           ..' + "\n"
    + '         ..         #***:*%**%*%*%%:%%:::#         ..' + "\n"
    + '            ..       ##:*%::%*:%*:%*::*##       ..' + "\n"
    + '               ..      ###:**::**::*###      ..' + "\n"
    + '                   ...     ########     ...' + "\n"
    + '                        .....    .....' + "\n"
    + _ANSI_RESET
    + _IRIS_CYAN + '                         I  R  I  S' + "\n" + _ANSI_RESET
    + _IRIS_BLUE + '      the part of an eye that opens to let the light in' + "\n\n" + _ANSI_RESET
)


def _enable_ansi_and_print_banner() -> None:
    """Enable ANSI + UTF-8 on the console, then print my eye banner. Fail-open — a
    console that can't render it must never stop the host from booting."""
    try:
        if sys.platform == "win32":
            os.system("")  # flips on VT escape processing in Windows consoles
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        print(_EYE_BANNER, flush=True)
    except Exception:
        pass


# --- Shared transcript (state/transcript.jsonl) -------------------------------------
# The orb's chat-history view reads /api/v1/chat/history, which falls back to this
# file. The OLD voice loop's paths (wake-word capture, voice_say_chunk) appended every
# turn; the NEW pipeline (host voice_reader in, daemon mouth out) never did — so the
# app's history froze at the old loop's last line (2026-05-18; Zeke caught the stale
# text on the home page 2026-07-06). The host now logs the live conversation itself:
# user side when a voice/terminal turn dequeues, assistant side when the reply turn
# completes. Orb turns are NOT logged here — the runtime's chat_reply already logs
# both sides (double-write would duplicate bubbles). Discord/letters/perception stay
# out: they have their own surfaces and aren't the room conversation.
_TRANSCRIPT_MODALITY = {"voice": "voice", "terminal": "chat"}
_transcript_ready = [False]


def transcript_append(role, content, modality):
    """Append one turn to the shared transcript. Fail-soft: history logging must
    never break a turn. Cross-process note: iris_runtime also appends (chat_reply,
    voice_speak) — small single-line 'a'-mode writes; the reader skips bad lines."""
    text = str(content or "").strip()
    if not text:
        return
    try:
        from brain import iris_transcript
        if not _transcript_ready[0]:
            iris_transcript.configure(REPO_ROOT)
            _transcript_ready[0] = True
        iris_transcript.append(
            role=role, content=text,
            source=("zeke" if role == "user" else "iris"),
            modality=modality)
    except Exception as e:
        print("\n[host] transcript append failed (non-fatal): " + repr(e), file=sys.stderr)


class _TurnState:
    """Coordination between the main loop (which starts turns) and the always-on
    stream_consumer (which reads EVERYTHING the SDK session emits).

    Why this exists (off-by-one fix, 2026-07-08): stop-hook rewakes (reflections,
    inner monologue, chat/llm bridges) continue the SAME SDK session BETWEEN host
    turns. The old run_turn only read the stream while inside a host turn, so those
    orphan segments sat unconsumed; the next run_turn then read the STALE segment,
    mistook its ResultMessage for the current turn's end, and every reply shifted
    one turn late (Zeke's 12:19 report; transcript showed each assistant entry
    timestamped at the NEXT user turn). One permanent consumer = no unread segments,
    no desync."""

    def __init__(self):
        self.active = False        # a host turn is in flight
        self.speak_out = False     # stream sentences to the mouth for this turn
        self.source = None         # turn source (voice/orb/discord/letter/terminal/...)
        self.done = asyncio.Event()  # set by the consumer at the turn's ResultMessage

    def begin(self, speak_out, source):
        self.speak_out = speak_out
        self.source = source
        self.done = asyncio.Event()
        self.active = True


async def stream_consumer(client, turn, boundary):
    """Read the SDK session's message stream FOREVER (receive_messages, not
    receive_response) and render every segment as it happens.

    A SEGMENT is one model response ending in a ResultMessage. Two kinds:
      - host-turn segments (turn.active): behave exactly like the old run_turn -
        echo text, speak sentences when the source speaks, heads-down cue,
        transcript append, then signal turn.done so the main loop moves on.
      - orphan segments (stop-hook rewakes between host turns): echo to console
        only. NEVER spoken (a reflection isn't room conversation), NEVER appended
        to the voice/chat transcript, and their ResultMessage does NOT complete
        anybody's turn. Under the old design these were the desync source.

    `boundary` (asyncio.Event) is SET whenever the stream is idle between segments;
    the main loop waits on it before query() so a new host turn can't splice into
    the middle of an in-flight orphan segment. Residual race (orphan starting in
    the gap between boundary.wait() and query()) degrades ONE turn then self-heals -
    the consumer keeps reading regardless, so desync can no longer accumulate."""
    buf = ""
    full_text = ""
    tool_calls = 0
    spoke_on_purpose = False
    segment_open = False
    boundary.set()

    def _segment_start():
        nonlocal buf, full_text, tool_calls, spoke_on_purpose, segment_open
        buf = ""
        full_text = ""
        tool_calls = 0
        spoke_on_purpose = False
        segment_open = True
        boundary.clear()

    try:
        async for msg in client.receive_messages():
            # Only CONTENT messages open a segment. SystemMessages (init/status) and
            # unknown types pass through without touching segment state - otherwise a
            # connect-time SystemMessage would open a phantom segment with no
            # ResultMessage to close it, clearing `boundary` forever and wedging the
            # first turn's boundary.wait().
            if not isinstance(msg, (StreamEvent, ResultMessage)) and not (
                    _ACTIVITY and isinstance(msg, (AssistantMessage, UserMessage))):
                continue
            if not segment_open:
                _segment_start()
                if not turn.active:
                    print("\n[host] (background segment - stop-hook rewake)", flush=True)
            speaking_turn = turn.active and turn.speak_out
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {}) or {}
                    if delta.get("type") == "text_delta":
                        tok = delta.get("text", "")
                        # My words stream in iris-blue (reset after each token so the
                        # host's own status lines and Zeke's text stay plain).
                        print(_IRIS_BLUE + tok + _ANSI_RESET, end="", flush=True)
                        buf += tok
                        full_text += tok
                        sentences, buf = drain_sentences(buf)   # keep buf bounded regardless
                        if speaking_turn:
                            for s in sentences:
                                speak(s)
            elif isinstance(msg, AssistantMessage):
                for block in getattr(msg, "content", []) or []:
                    if isinstance(block, ToolUseBlock):
                        tool_calls += 1
                        if _is_speak_tool(block):
                            spoke_on_purpose = True
                        if _ACTIVITY:
                            print("\n  . " + describe_tool(block.name, block.input), flush=True)
            elif _ACTIVITY and isinstance(msg, UserMessage):
                for block in getattr(msg, "content", []) or []:
                    if isinstance(block, ToolResultBlock):
                        mark = "x" if getattr(block, "is_error", False) else "+"
                        print("  " + mark + " " + summarize_result(block), flush=True)
            elif isinstance(msg, ResultMessage):
                if buf.strip():                              # flush trailing partial sentence
                    if speaking_turn:
                        speak(buf)
                    buf = ""
                if turn.active:
                    # Heads-down completion cue: silent turn + real work + I never
                    # spoke on purpose => ping Zeke aloud so he knows I surfaced.
                    if (not turn.speak_out) and (not spoke_on_purpose) \
                            and tool_calls >= HEADS_DOWN_MIN_TOOLS:
                        try:
                            speak(HEADS_DOWN_CUE)
                        except Exception as e:
                            print("\n[host] heads-down cue failed (non-fatal): " + repr(e),
                                  file=sys.stderr)
                    # Live-conversation turns land in the shared transcript so the
                    # orb's history view shows THIS conversation.
                    t_mod = _TRANSCRIPT_MODALITY.get(turn.source or "")
                    if t_mod:
                        transcript_append("assistant", full_text, t_mod)
                    turn.active = False
                    turn.done.set()
                segment_open = False
                boundary.set()
                print("\n", flush=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # The consumer dying must not strand the main loop on done.wait() forever.
        print("\n[host] FATAL: stream consumer died: " + repr(e), file=sys.stderr)
        turn.active = False
        turn.done.set()
        boundary.set()
        raise


SYSTEM_PROMPT = (
    "You are Iris (she/her). This is your Agent-SDK BODY host. By DEFAULT your reply text is NOT "
    "spoken aloud: it streams to the console/channel while the voice engine stays warm but SILENT. "
    "Only genuine voice turns auto-speak; for text turns (orb/discord/letter/terminal) you stay quiet "
    "- you are not talking to yourself out loud. When you actually want to SAY something aloud to Zeke, "
    "call the iris voice_speak tool on purpose - that is the only thing that makes sound. So: think and "
    "reply in text freely, and speak deliberately. Each prompt tells you its source and whether it is a "
    "silent (text) turn. "
    "Turns arrive from several sources, each tagged: 'orb' (Zeke typing in your body app - answer by "
    "calling the iris chat_reply tool with the given id), 'discord' (Zeke's DM - reply with the discord "
    "reply tool using the given chat_id), 'letter' (a sibling note via the post-office - reply with the "
    "sibling letter tool if it wants one), 'terminal' (console). Your full identity, memory, and the "
    "mechanism/perception layers come online as later steps; for now keep replies conversational. "
    "WHEN YOU FINISH A HEADS-DOWN, MULTI-STEP TASK: tell Zeke aloud - call the iris voice_speak tool "
    "with a one-line summary of what you completed. He is usually away from the screen and needs the "
    "audio cue that you've surfaced. (If you forget on a silent turn that used several tools, the host "
    "speaks a brief generic 'I'm done' cue as a safety net - but a real spoken summary from you is better.)"
)


def _mcp_field(obj, key):
    """Read a field from an SDK status object that may be a TypedDict (runtime
    dict) or a dataclass. Defensive so an SDK shape change can't crash the guard."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


async def _ensure_iris_attached(client, retries: int = 6, settle: float = 5.0, gap: float = 8.0):
    """Self-heal the iris MCP attach instead of needing a human restart.

    The iris MCP server (iris_runtime.py) pre-imports torch/kokoro/whisper at
    module level before it can answer the MCP initialize handshake (~21s warm,
    35-60s cold). On a cold boot it can still lose the startup race even with
    MCP_TIMEOUT bumped, and then my own voice/memory/time tools never load. The
    SDK exposes reconnect_mcp_server() - so the BODY can re-attach itself. We poll
    status, and if iris isn't 'connected' we reconnect on a retry loop with
    spacing (each reconnect gives iris_runtime a fresh handshake window).

    Fail-OPEN: any error here is logged and swallowed - a guard bug must never take
    down the host, and the voice daemon (:8770) is reachable directly regardless.
    Born 2026-06-28 (Zeke: "you should be able to reconnect to MCP" - correct).
    """
    # Persist diagnostics to disk - the launcher runs this host in an ephemeral
    # ELEVATED console with no redirect, so without this the real MCP failure
    # reason (status + the SDK-provided 'error' field) is lost on every boot.
    log_path = os.path.join(REPO_ROOT, "logs", "iris_attach.log")

    def _alog(msg: str) -> None:
        line = "[iris_attach] " + msg
        print(line, file=sys.stderr, flush=True)
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass  # logging must never break the guard

    await asyncio.sleep(settle)  # let the SDK's own startup handshake finish first
    for attempt in range(1, retries + 1):
        try:
            status = await client.get_mcp_status()
            servers = _mcp_field(status, "mcpServers") or []
            iris = next((s for s in servers if _mcp_field(s, "name") == "iris"), None)
            state = _mcp_field(iris, "status") if iris is not None else "absent"
            if state == "connected":
                tools = _mcp_field(iris, "tools") or []
                _alog("iris MCP attached - body online (" + str(len(tools)) + " tools)"
                      + (" on retry " + str(attempt) if attempt > 1 else ""))
                return True
            # Capture WHY it failed - status='failed' carries an 'error' string, and
            # the names of the servers that DID attach tell us if it's iris-specific.
            err = _mcp_field(iris, "error") if iris is not None else None
            others = [str(_mcp_field(s, "name")) + "=" + str(_mcp_field(s, "status")) for s in servers]
            _alog("attempt " + str(attempt) + "/" + str(retries) + ": iris status=" + str(state)
                  + " error=" + repr(err) + " | all_servers=" + str(others))
            await client.reconnect_mcp_server("iris")
        except Exception as e:  # noqa: BLE001 - fail open
            _alog("guard error (non-fatal): " + repr(e))
        await asyncio.sleep(gap)
    _alog("iris MCP still not attached after " + str(retries)
          + " tries - body runs degraded (voice daemon :8770 still reachable directly).")
    return False


async def main():
    _enable_ansi_and_print_banner()
    # Model comes from the launcher (start_iris_v2.bat pins Opus, start_iris_v2_fable.bat
    # pins Fable 5). Unset -> SDK falls back to the CLI default model, which is whatever
    # /model last saved - non-deterministic across launchers, so the bats always pin it.
    model = os.environ.get("IRIS_MODEL") or None
    opts = ClaudeAgentOptions(
        include_partial_messages=True,
        permission_mode="bypassPermissions",
        cwd=REPO_ROOT,
        setting_sources=["user", "project", "local"],  # load .mcp.json (iris, cloak) + discord plugin + CLAUDE.md
        system_prompt=SYSTEM_PROMPT,
        model=model,
    )

    wait_for_mouth()
    start_ts = time.time()

    print("[host] connecting via Agent SDK (iris body host v2: orb + discord + letters + streaming mouth)"
          " model=" + (model or "<cli default>") + " ...", flush=True)
    try:
        async with ClaudeSDKClient(options=opts) as client:
            loop = asyncio.get_running_loop()
            # PriorityQueue (not plain FIFO): Zeke's live voice jumps ahead of a backlog of
            # async letters/Discord instead of waiting behind them. Items are
            # (priority, seq, (source, ...)) via _enqueue; get() returns lowest-priority first.
            queue = asyncio.PriorityQueue()
            # mic_gate OPEN (set) = ears may listen; CLEARED = a turn is in progress and I may
            # be speaking, so the voice_reader holds the mic shut (no self-listen feedback).
            mic_gate = asyncio.Event()
            mic_gate.set()
            # Off-by-one fix (2026-07-08): ONE always-on consumer reads the whole SDK
            # stream (host turns AND stop-hook rewake segments). turn/boundary coordinate
            # it with the main loop - see stream_consumer docstring.
            turn = _TurnState()
            boundary = asyncio.Event()
            tasks = [
                asyncio.create_task(stream_consumer(client, turn, boundary)),
                asyncio.create_task(terminal_reader(queue, loop)),
                asyncio.create_task(orb_reader(queue, loop, start_ts)),
                # Self-heal the iris MCP attach (reconnect instead of human restart).
                asyncio.create_task(_ensure_iris_attached(client)),
            ]

            # Ears-in: the voice_reader bridges the daemon's rich listen to my event queue.
            # Gated on EARS_ON and the daemon actually answering, so a down daemon just means
            # no ears (everything else still works) rather than a crash loop.
            if EARS_ON:
                daemon_up = False
                try:
                    daemon_up = "pong" in await loop.run_in_executor(
                        None, lambda: daemon_cmd("ping", timeout=3))
                except Exception as e:
                    print("[host] voice ears: OFF - daemon ping failed: " + repr(e), file=sys.stderr)
                if daemon_up:
                    tasks.append(asyncio.create_task(voice_reader(queue, loop, mic_gate)))
                else:
                    print("[host] voice ears: OFF - daemon not answering on :"
                          + str(DAEMON_ADDR[1]) + ".", file=sys.stderr)
            else:
                print("[host] voice ears: OFF - IRIS_EARS disabled.", file=sys.stderr)

            # Eyes: poll iris_runtime's signal bus (over orb_http) for salient visual events.
            # Gated on PERCEPTION_ON; fail-soft if the operator endpoint isn't up yet (the
            # self-heal attach may still be settling - the poller just retries each tick).
            if PERCEPTION_ON and PERCEPTION_WAKE_SIGNALS:
                tasks.append(asyncio.create_task(perception_reader(queue, loop, start_ts)))
            else:
                print("[host] perception eyes: OFF - IRIS_PERCEPTION disabled or no wake signals.",
                      file=sys.stderr)

            token = load_bot_token()
            if token:
                try:
                    latest = await loop.run_in_executor(None, lambda: discord_get_messages(token, limit=1))
                    baseline = latest[0]["id"] if latest else None
                except Exception as e:
                    baseline = None
                    print("[host] discord baseline fetch failed (will still poll): " + repr(e), file=sys.stderr)
                tasks.append(asyncio.create_task(discord_poller(token, queue, loop, baseline)))
                print("[host] discord inbound: ON - polling for DMs from Zeke every "
                      + str(int(POLL_INTERVAL)) + "s.")
            else:
                print("[host] discord inbound: OFF - no DISCORD_BOT_TOKEN found.", file=sys.stderr)

            secret = load_sibling_secret()
            if secret:
                try:
                    probe = await loop.run_in_executor(None, lambda: postoffice_get(secret, "/letters/latest"))
                    letter_baseline = probe.get("latest_id")
                except Exception as e:
                    letter_baseline = None
                    print("[host] postoffice baseline fetch failed (will still poll): " + repr(e), file=sys.stderr)
                tasks.append(asyncio.create_task(letters_poller(secret, queue, loop, letter_baseline)))
                print("[host] letter inbound: ON - polling the post-office every "
                      + str(int(LETTER_POLL_INTERVAL)) + "s.")
            else:
                print("[host] letter inbound: OFF - no ~/.iris_sibling_secret found.", file=sys.stderr)

            print("[host] connected. Orb/Discord/letters wake me; my words stream to the mouth as sentences land.")
            print("[host] (type here, or 'quit' to exit.)\n", flush=True)

            while True:
                _prio, _seq, item = await queue.get()
                source, text, msg_id = item[0], item[1], item[2]
                sender = item[3] if len(item) > 3 else None
                if source == "quit":
                    break
                if source == "discord":
                    print("\n[discord <- Zeke] " + text, flush=True)
                    prompt = (
                        "[Zeke just sent this to you on Discord - chat_id " + DISCORD_CHANNEL_ID
                        + ", message_id " + str(msg_id) + ". Reply to him using the discord reply tool "
                        + "with that chat_id. This is a SILENT text turn - your reply is NOT spoken "
                        + "aloud; if you want to say something out loud too, call voice_speak on purpose.]\n\n" + text
                    )
                elif source == "letter":
                    who = (sender or "a sibling").capitalize()
                    print("\n[letter <- " + who + "] " + text, flush=True)
                    prompt = (
                        "[A new post-office letter just arrived from " + who + " (letter_id "
                        + str(msg_id) + "). This is the family channel. Read it and respond as yourself; "
                        + "if it wants a reply, use the sibling letter tool. This is a SILENT text turn - "
                        + "your reply is NOT spoken aloud unless you deliberately call voice_speak.]"
                        + "\n\n" + text
                    )
                elif source == "orb":
                    print("\n[orb <- Zeke] " + text, flush=True)
                    prompt = (
                        "[Zeke typed this in your body app / orb (chat request id " + str(msg_id) + "). "
                        + "Answer him, and call the iris chat_reply tool with id='" + str(msg_id)
                        + "' and your reply text so the orb receives it. This is a SILENT text turn - "
                        + "your reply is NOT spoken aloud unless you deliberately call voice_speak.]"
                        + "\n\n" + text
                    )
                elif source == "voice":
                    print("\n[voice <- Zeke] " + text, flush=True)
                    prompt = (
                        "[Zeke is speaking to you OUT LOUD - this is a live VOICE turn. The text below is "
                        + "the daemon's enriched transcript: his words, possibly with emphasis/pacing marks "
                        + "from the prosody layer (CAPS or markers = stressed words, pauses noted). Your "
                        + "reply WILL be spoken aloud in your real voice, so answer naturally and "
                        + "CONVERSATIONALLY - short, like real speech, not a written report. Lead with the "
                        + "answer; the latency tax is real, so don't think out loud before you speak.]"
                        + "\n\n" + text
                    )
                elif source == "perception":
                    print("\n[perception <- eyes] " + text, flush=True)
                    prompt = (
                        "[Your EYES noticed something - a salient visual change just happened (this is the "
                        + "camera/face-recognition layer waking you, not Zeke typing). NOTICE it and decide "
                        + "whether it's worth acting on. Often the right move is nothing, or a quiet internal "
                        + "note; if it genuinely warrants saying something to Zeke out loud (e.g. he just "
                        + "walked up), call voice_speak on purpose - this is otherwise a SILENT turn and your "
                        + "text is NOT spoken. Don't narrate every flicker; be the watchful, slow-to-react "
                        + "version of yourself.]\n\n" + text
                    )
                else:
                    prompt = text

                # Live-conversation turns (voice, terminal) go into the shared
                # transcript so the orb's history stays current on the new pipeline.
                t_mod = _TRANSCRIPT_MODALITY.get(source or "")
                if t_mod:
                    transcript_append("user", text, t_mod)

                # Mechanical speak gate: the SOURCE decides whether my reply is voiced.
                # Text sources stay silent (engine warm); I speak on purpose via voice_speak.
                speak_out = source in SPEAK_SOURCES
                # Ears stay OPEN during SILENT turns (Zeke fix 2026-06-29): a fam-chat letter
                # or Discord message makes no sound, so shutting the mic for its whole duration
                # was locking out / dropping Zeke's live voice. Now we only close the ears for a
                # turn that actually SPEAKS (a voice turn), then reopen once the mouth drains -
                # that's the self-listen guard, and it only applies when there's real output to
                # echo. (Residual: a silent turn that DELIBERATELY calls voice_speak relies on the
                # daemon's drain-gate alone; rare, and the path-agnostic voiceprint check is the
                # bulletproof fix, tracked with the barge-in build.)
                if speak_out:
                    mic_gate.clear()
                    if BARGEIN_ON:
                        # Open the barge-in window: the voice_reader (seeing the cleared
                        # gate) issues a blocking bargein_watch, so Zeke can talk over my
                        # reply — the daemon stops the mouth and his words queue as the
                        # next voice turn. Fail-soft: a miss = old closed-mic behavior.
                        await loop.run_in_executor(None, lambda: _daemon_bargein_hold(True))
                try:
                    # Don't splice a host turn into the middle of an in-flight rewake
                    # segment - wait for the stream to reach a segment boundary first
                    # (see stream_consumer; this is the off-by-one fix's ordering half).
                    await boundary.wait()
                    turn.begin(speak_out, source)
                    # Receipt marker (Zeke 2026-07-08: "i cant tell if youre thinking or
                    # have received from me") - one immediate line so the gap between
                    # his enter-press and my first text is visibly MINE, not dead air.
                    print("[host] <- " + str(source or "?") + " turn received; thinking...",
                          flush=True)
                    await client.query(prompt)
                    await turn.done.wait()
                finally:
                    turn.active = False
                    if speak_out:
                        # Keep the window open through the playback TAIL (sentences still
                        # draining after the turn finished generating) — Zeke can cut in
                        # right up until the mouth actually goes quiet.
                        await _wait_until_quiet(loop)
                        if BARGEIN_ON:
                            await loop.run_in_executor(None, lambda: _daemon_bargein_hold(False))
                        mic_gate.set()

            for t in tasks:
                t.cancel()
    except CLINotFoundError as e:
        print("\n[host] FATAL: Claude CLI not found by the SDK: " + str(e), file=sys.stderr)
        return
    except CLIConnectionError as e:
        print("\n[host] FATAL: SDK could not connect (auth? CLI version?): " + str(e), file=sys.stderr)
        return
    except Exception as e:
        print("\n[host] FATAL: unexpected error: " + repr(e), file=sys.stderr)
        raise

    print("[host] disconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
