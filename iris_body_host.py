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
        for m in sorted(msgs, key=lambda x: int(x["id"])):  # oldest-first = natural order
            last = m["id"]  # advance regardless, so a skipped message isn't re-seen
            author = m.get("author", {}) or {}
            if author.get("id") != DISCORD_OWNER_ID or author.get("bot"):
                continue
            content = (m.get("content") or "").strip()
            if content:
                await queue.put(("discord", content, m["id"]))


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
                await queue.put(("letter", content, lid, frm))


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
            await queue.put(("orb", text, rid))


async def terminal_reader(queue, loop):
    """Console fallback. Read stdin lines -> (terminal, text, None). quit/exit -> (quit, ...)."""
    while True:
        try:
            user = await loop.run_in_executor(None, input, "you> ")
        except (EOFError, KeyboardInterrupt):
            await queue.put(("quit", "", None))
            return
        if user.strip().lower() in ("quit", "exit"):
            await queue.put(("quit", "", None))
            return
        if not user.strip():
            continue
        await queue.put(("terminal", user, None))


async def run_turn(client, speak_out=True, source=None):
    """Drive one response. Always echo text to the console; enqueue sentences to the
    mouth ONLY when speak_out is True (i.e. the turn's source is a speaking source).
    The voice engine/daemon stays warm either way - this gates OUTPUT, not the engine.
    On a SILENT turn that worked heads-down (>= HEADS_DOWN_MIN_TOOLS tools) without a
    deliberate voice_speak, fire a brief generic completion cue so Zeke is pinged aloud."""
    buf = ""
    tool_calls = 0
    spoke_on_purpose = False
    async for msg in client.receive_response():
        if isinstance(msg, StreamEvent):
            ev = msg.event or {}
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {}) or {}
                if delta.get("type") == "text_delta":
                    tok = delta.get("text", "")
                    print(tok, end="", flush=True)      # console echo (always)
                    buf += tok
                    sentences, buf = drain_sentences(buf)   # keep buf bounded regardless
                    if speak_out:
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
                if speak_out:
                    speak(buf)
                buf = ""
            # Heads-down completion cue: silent turn + real work + I never spoke on
            # purpose => ping Zeke aloud so he knows I surfaced (safety net for the
            # voice_speak summary I'm supposed to give myself).
            if (not speak_out) and (not spoke_on_purpose) and tool_calls >= HEADS_DOWN_MIN_TOOLS:
                try:
                    speak(HEADS_DOWN_CUE)
                except Exception as e:
                    print("\n[host] heads-down cue failed (non-fatal): " + repr(e), file=sys.stderr)
            print("\n", flush=True)


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


async def main():
    opts = ClaudeAgentOptions(
        include_partial_messages=True,
        permission_mode="bypassPermissions",
        cwd=REPO_ROOT,
        setting_sources=["user", "project", "local"],  # load .mcp.json (iris, cloak) + discord plugin + CLAUDE.md
        system_prompt=SYSTEM_PROMPT,
    )

    wait_for_mouth()
    start_ts = time.time()

    print("[host] connecting via Agent SDK (iris body host v2: orb + discord + letters + streaming mouth)...",
          flush=True)
    try:
        async with ClaudeSDKClient(options=opts) as client:
            loop = asyncio.get_running_loop()
            queue = asyncio.Queue()
            tasks = [
                asyncio.create_task(terminal_reader(queue, loop)),
                asyncio.create_task(orb_reader(queue, loop, start_ts)),
            ]

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
                item = await queue.get()
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
                else:
                    prompt = text

                # Mechanical speak gate: the SOURCE decides whether my reply is voiced.
                # Text sources stay silent (engine warm); I speak on purpose via voice_speak.
                speak_out = source in SPEAK_SOURCES
                await client.query(prompt)
                await run_turn(client, speak_out, source)

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
