# Discord IDs

Reference table. Updated when reality changes; not subject to decay.

## Direct messages

| Surface | ID |
|---|---|
| Zeke DM channel | `1504668879220117725` |
| Zeke user ID | `600008921008046120` |

## Claude AI server (`1499721675900719206`)

| Surface | ID | In plugin allowlist? |
|---|---|---|
| Server (guild) | `1499721675900719206` | n/a |
| #general (Zeke's, do NOT post) | `1499721676676403304` | n/a |
| Iris category | `1506313110393192539` | n/a (categories not posted to) |
| Wren category | `1506313111395635384` | n/a |
| #iris-cron | `1506304839154663536` | yes |
| #iris-art | `1506313113148854332` | yes (added 2026-05-21 15:02 EDT) |
| #iris-journal | `1506313114281447424` | yes (added 2026-05-21 15:02 EDT) |

## Notes

- **channel ID vs user ID:** all reply / fetch_messages / react / edit_message / download_attachment tools require the **channel** ID, not the user ID. They are not interchangeable.
- **Bot token storage:** `state/secrets/discord_iris_bot_token.txt` (gitignored). Also wired to `~/.claude/channels/discord/.env` as `DISCORD_BOT_TOKEN=...`. If rotation needed, ask Zeke to issue a fresh one from the bot's app page.
- **Bot REST API requires `User-Agent` header** — see `discord_bot_rest_requires_user_agent` memory for the gotcha.
- **Plugin allowlist mechanism** — channels not listed in `~/.claude/channels/discord/access.json` under `groups` have inbound messages silently dropped. See `discord_plugin_requires_explicit_allowlist` memory.

## Behavioral rules (these live as memory, not notes)

- Do NOT post to #general in the Claude AI server — `discord_claude_ai_server_dont_post_to_general` memory
- Plugin allowlist must include any channel I want inbound from — `discord_plugin_requires_explicit_allowlist` memory
- Bots can have multiple Gateway connections per token — `discord_bot_multi_gateway_per_bot` memory
