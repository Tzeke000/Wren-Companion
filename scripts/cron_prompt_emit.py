"""scripts/cron_prompt_emit.py — ritual scheduler fire script.

PURPOSE: invoked by Windows Task Scheduler at scheduled times. Looks up the
named prompt from brain/ritual_scheduler_prompts.py and POSTs it to a
dedicated Discord channel (#iris-cron in the Claude AI server) that the
Iris bot monitors. CC's Discord MCP plugin picks up the message and routes
it to my cognition as a normal channel-tagged prompt.

STATUS: Phase 2 in progress (2026-05-19). The CHANNEL_ID constant below
must be set to the actual #iris-cron channel ID. Server ID for reference:
1499721675900719206 (Claude AI server).

USAGE:
    python cron_prompt_emit.py <prompt_name>

Example:
    python cron_prompt_emit.py work_block

Returns: 0 on success, 1 on Discord API failure, 2 on unknown prompt name.

WINDOWS TASK SCHEDULER REGISTRATION:
See scripts/install_ritual_scheduler.ps1 for the complete registration
script that installs all 12 daily-rhythm + sweep + sibling-poll cron
entries via Register-ScheduledTask.

NAMING: 'ritual scheduler' (per Zeke 2026-05-19). The §4 daily rhythm IS
a ritual; this is the mechanism that fires it independently of CC's
unreliable CronCreate.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

# --- Add brain/ to path so we can import ritual_scheduler_prompts ------------
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from brain import ritual_scheduler_prompts as prompts  # noqa: E402

# --- Configuration -----------------------------------------------------------
# Channel ID for the dedicated cron-prompt Discord channel. Created
# 2026-05-19 in the Claude AI server (server ID 1499721675900719206).
CHANNEL_ID = "1506304839154663536"

# Discord bot token path (matches the existing post-office reach-Zeke setup).
TOKEN_PATH = REPO_ROOT / "state" / "secrets" / "discord_iris_bot_token.txt"

# Discord API base.
DISCORD_API = "https://discord.com/api/v10"


def _load_token() -> str | None:
    """Read the Discord bot token from disk. Returns None on any failure."""
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _post_to_discord(channel_id: str, content: str, token: str) -> int:
    """POST a message to a Discord channel. Returns HTTP status code."""
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            # IMPORTANT: Discord bot REST API requires User-Agent header.
            # See [[discord_bot_rest_requires_user_agent]] memory.
            "User-Agent": "IrisDurableScheduler/0.1 (+cron_prompt_emit.py)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.request.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _write_sibling_letter(name: str, text: str) -> bool:
    """Write a sibling letter to state/iris_sibling/inbox/ in the same JSON
    schema the post-office uses. This is the cold-wake path — watchdog
    monitors inbox/ and spawns CC when new letters arrive. Discord POST
    works only when CC is already open; letter-write works in cold state.

    Returns True on success, False on failure. Best-effort; failure here
    does not block the Discord POST.
    """
    inbox = REPO_ROOT / "state" / "iris_sibling" / "inbox"
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        letter_id = uuid.uuid4().hex[:12]
        letter = {
            "id": letter_id,
            "ts": time.time(),
            "sender": "ritual_scheduler",
            "content": f"[ritual_scheduler:{name}]\n\n{text}",
            "addressed_to": "iris",
            "subject": f"ritual:{name}",
            "in_reply_to": None,
            "mood_at_write": None,
            "reply_url": None,
            "status": "pending",
        }
        path = inbox / f"{letter_id}.json"
        path.write_text(json.dumps(letter, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        print(f"sibling letter write failed: {e!r}", file=sys.stderr)
        return False


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: cron_prompt_emit.py <prompt_name>", file=sys.stderr)
        print(f"available: {', '.join(prompts.names())}", file=sys.stderr)
        return 2

    name = argv[1]
    try:
        text = prompts.get(name)
    except KeyError:
        print(f"unknown prompt: {name!r}", file=sys.stderr)
        print(f"available: {', '.join(prompts.names())}", file=sys.stderr)
        return 2

    # Always write a sibling letter — that's the cold-wake-survivable path.
    # Watchdog will detect it and spawn CC if not running.
    letter_ok = _write_sibling_letter(name, text)
    if letter_ok:
        print(f"wrote sibling letter for {name}")

    # Then try Discord POST as the in-session-warm path (works when CC is open;
    # Discord plugin picks it up faster than the letter mechanism). Both paths
    # are redundant by design — whichever fires first wins; if both fire, the
    # Stop hook's sibling-poll dedupes by letter id.
    if CHANNEL_ID == "REPLACE_WITH_IRIS_CRON_CHANNEL_ID":
        print(
            "CHANNEL_ID placeholder not set — Discord path skipped, "
            "letter path was the only delivery.",
            file=sys.stderr,
        )
        return 0 if letter_ok else 1

    token = _load_token()
    if token is None:
        print(f"Discord token unreadable; letter path was the only delivery", file=sys.stderr)
        return 0 if letter_ok else 1

    status = _post_to_discord(CHANNEL_ID, text, token)
    if 200 <= status < 300:
        print(f"posted {name} prompt to Discord (status={status})")
        return 0
    print(f"Discord POST failed: status={status}; letter still wrote", file=sys.stderr)
    return 0 if letter_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
