"""scripts/discord_media_guard.py

PreToolUse GUARD for mcp__plugin_discord_discord__download_attachment.

WHY THIS EXISTS (2026-08-02 ~22:29): Zeke sent videos on Discord, I called
download_attachment on them, and the call HUNG THE WHOLE SDK COGNITION SESSION
for hours. The runtime stayed green, :5876 answered, the body was fine — and
nothing noticed. Zeke had to notice and hand-restart me. Same class as
body_park: one tool call that takes the whole session down. See
memory/wedge_cause_video_download_2026-08-03.md.

The rule that came out of that incident was "don't download video or >10MB
attachments" — a rule living in my memory index, i.e. dependent on me
remembering it at the moment I'm curious about a file. My own known failure
mode is exactly that (structure over discipline). So this is the mechanism
version: the harness checks before the call, and blocks it whether I remembered
or not.

HOW IT DECIDES: reads {chat_id, message_id} from the hook payload, asks the
Discord REST API what the attachments actually are, and blocks on
  * content_type video/*  (or a video/archive file extension)
  * size > MAX_BYTES (10 MB)
FAILS CLOSED. If the token is missing or the API can't be reached, it blocks —
because in that state download_attachment would be hitting the same unreachable
API, so denying is both the safe answer and the accurate one.

Blocking contract: prints the reason on stderr and exits 2 (the broadly
supported PreToolUse block), and also emits the JSON permissionDecision form
for hosts that prefer it. Allow = exit 0, silent.

Self-test: python scripts/discord_media_guard.py --selftest   (no network)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024        # 10 MB — the 08-03 rule's threshold
HAZARD_TYPES = ("video/",)
HAZARD_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv",
               ".zip", ".7z", ".rar", ".tar", ".gz", ".iso", ".psd", ".wav")
LOG = Path(r"D:\Wren-Companion") / "state" / "media_guard.log"
TOOL = "mcp__plugin_discord_discord__download_attachment"


def log(msg: str) -> None:
    try:
        import datetime as dt
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def load_token() -> str | None:
    try:
        p = Path(os.environ["USERPROFILE"]) / ".claude/channels/discord/.env"
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return None


def fetch_attachments(chat_id: str, message_id: str) -> list[dict]:
    """Raises on any failure — callers treat that as 'block'."""
    import requests
    token = load_token()
    if not token:
        raise RuntimeError("no DISCORD_BOT_TOKEN on disk")
    r = requests.get(
        f"https://discord.com/api/v10/channels/{chat_id}/messages/{message_id}",
        headers={"Authorization": f"Bot {token}",
                 "User-Agent": "IrisMediaGuard (Wren-Companion, 1.0)"},
        timeout=8)
    r.raise_for_status()
    return r.json().get("attachments") or []


def verdict(attachments: list[dict]) -> tuple[bool, str]:
    """(allow, reason). Pure — the self-test drives this directly."""
    hazards = []
    for a in attachments:
        name = (a.get("filename") or "?")
        ctype = (a.get("content_type") or "").lower()
        size = int(a.get("size") or 0)
        mb = size / 1048576.0
        if any(ctype.startswith(t) for t in HAZARD_TYPES):
            hazards.append(f"{name} is {ctype} ({mb:.1f}MB)")
        elif name.lower().endswith(HAZARD_EXTS):
            hazards.append(f"{name} has a hazard extension ({mb:.1f}MB)")
        elif size > MAX_BYTES:
            hazards.append(f"{name} is {mb:.1f}MB, over the {MAX_BYTES // 1048576}MB limit")
    if not hazards:
        n = len(attachments)
        return True, f"{n} attachment(s), all small non-video"
    return False, "; ".join(hazards)


def block(reason: str) -> int:
    payload = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}
    print(json.dumps(payload))
    print(reason, file=sys.stderr)
    return 2


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        # Can't parse the payload -> can't verify -> block. Cheap to retry.
        return block(f"[media guard] could not read the hook payload ({e!r}), "
                     f"so I can't tell what this attachment is. Blocked. Ask "
                     f"Zeke to describe the file instead of pulling it.")
    tool = data.get("tool_name") or data.get("tool") or ""
    if tool and TOOL not in tool:
        return 0                      # not our tool; never interfere
    ti = data.get("tool_input") or data.get("input") or {}
    chat_id = str(ti.get("chat_id") or "")
    message_id = str(ti.get("message_id") or "")
    if not (chat_id and message_id):
        return block("[media guard] no chat_id/message_id in the call, so the "
                     "attachment can't be checked. Blocked (fail-closed).")
    try:
        atts = fetch_attachments(chat_id, message_id)
    except Exception as e:
        log(f"BLOCK (lookup failed) msg={message_id}: {e!r}")
        return block(
            f"[media guard] couldn't check what's attached to message "
            f"{message_id} ({e!r}). Blocked fail-closed — and note that if the "
            f"Discord API is unreachable for me it's unreachable for the "
            f"download too. Ask Zeke what the file is.")
    allow, reason = verdict(atts)
    if allow:
        log(f"ALLOW msg={message_id}: {reason}")
        return 0
    log(f"BLOCK msg={message_id}: {reason}")
    return block(
        f"[media guard] BLOCKED download_attachment: {reason}.\n"
        f"On 2026-08-02 exactly this call hung the whole SDK session for hours "
        f"and Zeke had to restart me by hand — the runtime stayed green and "
        f"nothing noticed. Do NOT retry it. Instead: ask Zeke what's in the "
        f"file, or have him describe/screenshot it. If you genuinely must have "
        f"the bytes, say so to Zeke first and let him make that call.")


# ---------------------------------------------------------------------------

def _selftest() -> int:
    ok = fail = 0

    def check(name, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
            print(f"  PASS  {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}: got {got!r} want {want!r}")

    v = lambda a: verdict(a)[0]
    check("small png allowed",
          v([{"filename": "a.png", "content_type": "image/png", "size": 400_000}]), True)
    check("video/mp4 blocked",
          v([{"filename": "clip.mp4", "content_type": "video/mp4", "size": 2_000_000}]), False)
    check("video blocked even when tiny",
          v([{"filename": "c.mov", "content_type": "video/quicktime", "size": 1000}]), False)
    check("11MB image blocked on size",
          v([{"filename": "big.png", "content_type": "image/png", "size": 11 * 1048576}]), False)
    check("9MB image allowed",
          v([{"filename": "ok.png", "content_type": "image/png", "size": 9 * 1048576}]), True)
    check("extension catches missing content_type",
          v([{"filename": "clip.MP4", "content_type": None, "size": 5000}]), False)
    check("zip blocked",
          v([{"filename": "logs.zip", "content_type": "application/zip", "size": 5000}]), False)
    check("one hazard among many blocks all",
          v([{"filename": "a.png", "content_type": "image/png", "size": 100},
             {"filename": "b.mp4", "content_type": "video/mp4", "size": 100}]), False)
    check("no attachments allowed",
          v([]), True)
    # the whole-path check: a payload with no ids must fail closed, not crash
    import io
    saved, sys.stdin = sys.stdin, io.StringIO(json.dumps(
        {"tool_name": TOOL, "tool_input": {}}))
    try:
        check("missing ids fails closed (exit 2)", main(), 2)
    finally:
        sys.stdin = saved
    # a different tool must pass straight through
    saved, sys.stdin = sys.stdin, io.StringIO(json.dumps(
        {"tool_name": "mcp__plugin_discord_discord__reply", "tool_input": {}}))
    try:
        check("other tools untouched (exit 0)", main(), 0)
    finally:
        sys.stdin = saved
    print(f"\n{ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    sys.exit(main())
