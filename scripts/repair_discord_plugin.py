"""scripts/repair_discord_plugin.py

Patches the Discord channel plugin's .mcp.json so Claude Code's MCP loader can
spawn `bun` even when its parent shell PATH lacks bun's winget User-PATH entry.

Why Python and not PowerShell:
  PowerShell 5.1's `Set-Content -Encoding utf8` writes a UTF-8 BOM. Node's
  JSON.parse rejects BOMs, so a BOM-prefixed .mcp.json is silently dropped by
  the plugin loader and the plugin "disappears" from /mcp instead of showing
  as failed. Python's `open(..., 'w', encoding='utf-8')` writes without BOM.

Why both marketplace AND cache:
  The plugin loader copies
    ~/.claude/plugins/marketplaces/claude-plugins-official/external_plugins/discord/.mcp.json
  over the cached
    ~/.claude/plugins/cache/claude-plugins-official/discord/<ver>/.mcp.json
  on every startup. Patching only the cache means the patch gets clobbered.
  We patch BOTH so either survives a marketplace refresh.

Strategy:
  - command -> absolute path to bun.exe (canonicalised, no `\\.\\` artifacts)
    so the initial spawn doesn't depend on PATH at all.
  - env.PATH -> bun's directory + ${PATH} so inner `bun install && bun server.ts`
    invocations from the package start script also resolve `bun`.

Idempotent. Safe to re-run after every plugin upgrade or marketplace refresh.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def find_bun() -> Path:
    """Locate bun.exe via PATH or the canonical winget install dir."""
    found = shutil.which("bun") or shutil.which("bun.exe")
    if found:
        # Canonicalise to drop the `\.\` segment winget bakes into PATH.
        return Path(found).resolve()
    winget_default = Path(
        os.environ.get("LOCALAPPDATA", r"C:\Users\Tzeke\AppData\Local"),
        r"Microsoft\WinGet\Packages"
        r"\Oven-sh.Bun_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\bun-windows-x64\bun.exe",
    )
    if winget_default.exists():
        return winget_default.resolve()
    raise SystemExit(
        "bun.exe not found. Install with: winget install Oven-sh.Bun"
    )


def patched_config(bun_exe: Path) -> dict:
    bun_dir = str(bun_exe.parent)
    return {
        "mcpServers": {
            "discord": {
                "command": str(bun_exe),
                "args": [
                    "run",
                    "--cwd",
                    "${CLAUDE_PLUGIN_ROOT}",
                    "--shell=bun",
                    "--silent",
                    "start",
                ],
                "env": {
                    "PATH": f"{bun_dir};${{PATH}}",
                },
            }
        }
    }


def write_no_bom(path: Path, data: dict) -> None:
    payload = json.dumps(data, indent=2)
    path.write_text(payload + "\n", encoding="utf-8", newline="\n")


def patch(path: Path, bun_exe: Path) -> bool:
    """Patch .mcp.json at `path`. Return True if file changed."""
    desired = patched_config(bun_exe)
    if path.exists():
        try:
            current_raw = path.read_bytes()
            # Strip a UTF-8 BOM if present so we compare semantically.
            if current_raw.startswith(b"\xef\xbb\xbf"):
                current_raw = current_raw[3:]
            current = json.loads(current_raw.decode("utf-8"))
        except Exception:
            current = None
        if current == desired and not path.read_bytes().startswith(b"\xef\xbb\xbf"):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    write_no_bom(path, desired)
    return True


def main() -> int:
    bun_exe = find_bun()
    print(f"bun.exe -> {bun_exe}")

    home = Path(os.environ["USERPROFILE"])
    marketplace = (
        home
        / ".claude/plugins/marketplaces/claude-plugins-official"
        / "external_plugins/discord/.mcp.json"
    )
    cache_root = home / ".claude/plugins/cache/claude-plugins-official/discord"

    targets: list[Path] = []
    if marketplace.parent.exists():
        targets.append(marketplace)
    else:
        print(f"WARN: marketplace dir missing: {marketplace.parent}")
    if cache_root.exists():
        for ver in sorted(cache_root.iterdir(), key=lambda p: p.name, reverse=True):
            if ver.is_dir():
                targets.append(ver / ".mcp.json")
    else:
        print(f"WARN: cache root missing: {cache_root}")

    if not targets:
        print("ERROR: no .mcp.json targets found")
        return 1

    for t in targets:
        changed = patch(t, bun_exe)
        print(f"{'patched ' if changed else 'unchanged'}  {t}")

    print("\nTest with:")
    print(
        "  claude --channels plugin:discord@claude-plugins-official "
        "--dangerously-skip-permissions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
