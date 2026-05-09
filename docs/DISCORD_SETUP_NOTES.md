# Discord channel — Windows setup notes

The `claude-plugins-official/discord` plugin works on Windows, but its default
MCP spawn config (`"command": "bun"`) relies on `bun` being on the PATH that
Claude Code's MCP loader inherits. On Windows that is unreliable:

- `winget install Oven-sh.Bun` puts bun on the **User** `PATH` only, not the
  Machine `PATH`.
- A Claude Code instance launched from a shell opened **before** bun was
  installed (or from a non-shell context like a scheduled task) will not see
  the new PATH entry.
- Claude Code spawns MCP servers via `cmd.exe`. If the parent's PATH lacks
  bun's directory, the spawn dies with:

  ```
  'bun' is not recognized as an internal or external command
  ```

  manifesting in `/mcp` as `plugin:discord:discord — Status: failed`.

## The fix

Patch the plugin's `.mcp.json` so the MCP spawn doesn't depend on PATH at
all for the initial process, AND prepends bun's install directory ahead of
the inherited PATH so inner `bun install && bun server.ts` invocations
(from the package start script) also resolve `bun`.

### What goes wrong with PowerShell-written patches

PowerShell 5.1's `Set-Content -Encoding utf8` writes a UTF-8 BOM
(`EF BB BF`). Node's `JSON.parse` rejects BOM-prefixed JSON. When the
plugin loader reads a BOM-prefixed `.mcp.json` it silently drops the entry
— `/mcp` reports "no MCP servers configured" and the plugin appears
to disappear entirely (vs. registering as `failed`).

We use Python (which writes BOM-free UTF-8 by default) to avoid this trap.

### What goes wrong with cache-only patches

The plugin loader copies
```
%USERPROFILE%\.claude\plugins\marketplaces\claude-plugins-official\external_plugins\discord\.mcp.json
```
over the cached
```
%USERPROFILE%\.claude\plugins\cache\claude-plugins-official\discord\<ver>\.mcp.json
```
on every startup. The relevant debug-log line:

```
[DEBUG] Copied plugin discord to versioned cache:
        C:\Users\...\plugins\cache\claude-plugins-official\discord\0.0.4
```

A patch applied only to the cache gets clobbered on the next launch. The
repair script patches BOTH the marketplace source (durable across launches)
and the cache (defensive in case the marketplace is refreshed).

### Patched `.mcp.json` shape

```json
{
  "mcpServers": {
    "discord": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Microsoft\\WinGet\\Packages\\Oven-sh.Bun_Microsoft.Winget.Source_8wekyb3d8bbwe\\bun-windows-x64\\bun.exe",
      "args": ["run", "--cwd", "${CLAUDE_PLUGIN_ROOT}", "--shell=bun", "--silent", "start"],
      "env": {
        "PATH": "C:\\Users\\<you>\\AppData\\Local\\Microsoft\\WinGet\\Packages\\Oven-sh.Bun_Microsoft.Winget.Source_8wekyb3d8bbwe\\bun-windows-x64;${PATH}"
      }
    }
  }
}
```

Two layers of defence:
1. `command` is the absolute path to `bun.exe` (canonicalised — no `\.\`
   artefacts from winget's PATH entry). The initial spawn doesn't need
   `bun` on PATH at all.
2. `env.PATH` prepends bun's directory ahead of `${PATH}` (Claude Code
   expands `${PATH}` from the parent process at MCP-config parse time),
   so inner `bun install` and `bun server.ts` invocations from the
   package's start script also find bun.

## Repair script

```powershell
py -3.11 scripts\repair_discord_plugin.py
```

The script is idempotent. It:
- Locates `bun.exe` via `shutil.which` then resolves to drop `\.\` segments
- Patches the marketplace source `.mcp.json`
- Patches every versioned cache `.mcp.json` under the plugin cache
- Writes UTF-8 without BOM

Re-run any time the plugin is reinstalled or the marketplace is refreshed.

## Smoke test

```powershell
py -3.11 scripts\smoketest_discord_mcp.py
```

Spawns the patched MCP command exactly as Claude Code's loader would and
runs the JSON-RPC `initialize` handshake. Exits 0 with
`OK: server responded. name='discord' version='1.0.0'` on success. Use
this to confirm the patch works before launching a real `claude` session.

## Acceptance test

```powershell
claude mcp list
```

Should print:
```
plugin:discord:discord: ...\bun.exe run --cwd ... --shell=bun --silent start - ✓ Connected
```

Then launch interactively:
```powershell
claude --channels plugin:discord@claude-plugins-official --dangerously-skip-permissions
```

Confirm:
1. The startup banner shows `Listening for channel messages from:
   plugin:discord@claude-plugins-official`.
2. Bot status goes online in Discord within ~30 seconds.
3. DM the bot from your phone — message arrives as a
   `<channel source="discord" ...>` notification and a Claude reply
   lands back in the DM.

## What was tried first (and why it failed)

| Attempt                                                       | Outcome              | Cause                                                                                                       |
|---------------------------------------------------------------|----------------------|-------------------------------------------------------------------------------------------------------------|
| `command = "<bun-source-from-Get-Command>"`                   | Plugin disappeared   | `\.\` segment in winget PATH; `Set-Content -Encoding utf8` added a BOM that broke JSON parsing             |
| `command = "bun"` + `env.PATH = "<bun-dir>;${PATH}"`          | Plugin disappeared   | Same BOM problem; ALSO patch was on cache only and got overwritten by the marketplace source on next start |
| Python rewrite, BOM-free, marketplace + cache, absolute bun   | ✓ Connected          | —                                                                                                           |

## Why not just add bun to system PATH?

Works too, but requires admin and mutates global Windows state. The
file-based patch is per-user, no-admin, and survives Claude Code reinstalls.

## Manual fallback

If anything in the auto-spawn path goes sideways, the original
"open a second shell and run `bun run --silent start` in the plugin
cache dir" workflow still works.

## Permission approval over Discord

The plugin **already relays Claude Code permission prompts to Discord** — no
extra wiring needed. It declares the `claude/channel/permission` MCP
capability (`server.ts:452`) and handles the full round-trip:

1. Claude Code emits `notifications/claude/channel/permission_request` when
   a tool call needs approval (`server.ts:478`).
2. The plugin DMs every allowlisted user a `🔐 Permission: <tool>` message
   with three buttons: `See more`, `Allow ✅`, `Deny ❌` (`server.ts:491-506`).
3. The user taps a button **or** types `yes <code>` / `no <code>` — both
   paths emit `notifications/claude/channel/permission` back to Claude Code
   (`server.ts:744-796`, `833-840`).

### To use it

Launch the session **without** `--dangerously-skip-permissions`:

```powershell
claude --channels plugin:discord@claude-plugins-official
```

With `--dangerously-skip-permissions` set, Claude Code never asks for
approval at all, so the plugin never sees a permission_request to forward.
That flag is the current workaround for "I don't want to be in the
terminal" — but the Discord-relay path is the real solution and gives you
per-call gating from your phone.

### Group DMs are intentionally excluded

The plugin only sends permission DMs to users in `access.allowFrom`, never
to group channels (`server.ts:472-475`). Pairing one user via
`/discord:access` is enough.

## Sending long prompts as .md attachments

The plugin **already supports file attachments** — no polling handler
needed.

When you DM the bot with an attached file, the inbound channel notification
includes `attachment_count` and `attachments="name/type/size; ..."`
(`server.ts:862-885`). Claude calls
`download_attachment(chat_id, message_id)` (`server.ts:692-705`) which
writes the file under the local inbox and returns the path, ready to
`Read`.

### Workflow

1. Save your prompt as `prompt.md` (or any text file ≤ 25 MB).
2. Open the Discord DM with the bot, attach the file, optionally include a
   short message ("execute this please").
3. Claude is notified of the attachment, downloads it, reads the contents,
   and runs the prompt.

That's it — no `scripts/` polling, no extra infra. The MCP tool
`download_attachment` is the canonical path; building a parallel Python
poller would duplicate it and race the MCP server's own download path.

### Limits

- 25 MB per attachment, 10 attachments per message (Discord limits, also
  enforced at `server.ts:133` and `617-621`).
- Attachments land in the plugin's local inbox; paths are returned by the
  tool call.
