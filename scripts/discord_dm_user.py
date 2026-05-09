"""scripts/discord_dm_user.py

Send a Discord DM to a user via the configured channel bot, using the bot
token from %USERPROFILE%/.claude/channels/discord/.env.

Usage:
    py -3.11 scripts/discord_dm_user.py <user_id> "<message>"

Designed for one-shot operational notifications (e.g. confirming the
auto-spawn fix from a non-interactive context). Uses Discord's REST API
directly so we don't pay the latency of spinning up a discord.py gateway
client.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests


def load_token() -> str:
    env_path = Path(os.environ["USERPROFILE"]) / ".claude/channels/discord/.env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit(f"DISCORD_BOT_TOKEN not found in {env_path}")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: discord_dm_user.py <user_id> <message>", file=sys.stderr)
        return 2
    user_id = sys.argv[1]
    message = sys.argv[2]
    token = load_token()
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "AvaAgent (https://github.com/Tzeke000/Ava-Agent-v2, 1.0)",
    }

    # Step 1: open/get DM channel with user
    r = requests.post(
        "https://discord.com/api/v10/users/@me/channels",
        headers=headers,
        json={"recipient_id": user_id},
        timeout=15,
    )
    if r.status_code >= 300:
        print(f"create-DM failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    channel_id = r.json()["id"]

    # Step 2: send message
    r = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers=headers,
        json={"content": message},
        timeout=15,
    )
    if r.status_code >= 300:
        print(f"send-DM failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    msg = r.json()
    print(f"sent: id={msg['id']} channel={channel_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
