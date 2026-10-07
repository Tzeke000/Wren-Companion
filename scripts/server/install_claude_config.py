"""scripts/server/install_claude_config.py - write the Linux Claude Code config for THIS checkout.

The tower's config is Windows-shaped: .mcp.json launches D:\...\.venv\Scripts\python.exe, the
project hooks (.claude/settings.local.json) point at the same venv, and the user-level Stop hook
is `py -3.11 "D:/.../voice_stop_hook.py"`. On Linux every one of those fails - and the Discord
MEDIA GUARD fails silently (a security degrade). Run this with the venv python on the server:

    ~/venvs/iris-v100/bin/python scripts/server/install_claude_config.py

- .mcp.json: Linux paths, then `git update-index --skip-worktree` so the tracked (tower) copy
  is never committed over from here.
- .claude/settings.local.json: env + the two Discord hooks + MCP enablement (untracked file).
- ~/.claude/settings.json: MERGED (existing keys kept): Stop hook (asyncRewake), discord plugin
  enabled, permission prompts off (the body host is unattended).
cloak-browser MCP is NOT configured here: it is not installed on the server yet.
Idempotent. Written 2026-10-07 (the server port).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
HOME = Path.home()


def write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    print(f"wrote {p}")


def main() -> int:
    if os.name == "nt":
        print("refusing: this writes the LINUX config; the tower keeps its own")
        return 2
    mcp = {"mcpServers": {"iris": {"type": "stdio", "command": PY,
                                   "args": [str(ROOT / "iris_runtime.py")],
                                   "env": {"AVA_STT_STREAMING": "1"}}}}
    write_json(ROOT / ".mcp.json", mcp)
    subprocess.run(["git", "-C", str(ROOT), "update-index", "--skip-worktree", ".mcp.json"], check=False)
    print("  .mcp.json marked skip-worktree (never commit the Linux copy over the tower's)")

    local = {
        "env": {"AVA_STT_STREAMING": "1"},
        "hooks": {
            "PostToolUse": [{"matcher": "mcp__plugin_discord_discord__reply", "hooks": [{
                "type": "command", "command": PY, "args": [str(ROOT / "scripts" / "discord_mood_hook.py")],
                "timeout": 15, "async": True, "statusMessage": "mood: feeling the reply"}]}],
            "PreToolUse": [{"matcher": "mcp__plugin_discord_discord__download_attachment", "hooks": [{
                "type": "command", "command": PY, "args": [str(ROOT / "scripts" / "discord_media_guard.py")],
                "timeout": 15, "statusMessage": "media guard: checking attachment size/type"}]}],
        },
        "enableAllProjectMcpServers": True,
        "enabledMcpjsonServers": ["iris"],
        "effortLevel": "high",
    }
    write_json(ROOT / ".claude" / "settings.local.json", local)

    up = HOME / ".claude" / "settings.json"
    cur = {}
    if up.is_file():
        try:
            cur = json.loads(up.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"! {up} unreadable ({e!r}) - leaving it alone")
            return 1
    cur.setdefault("permissions", {}).setdefault("defaultMode", "auto")
    cur["skipDangerousModePermissionPrompt"] = True
    cur["skipAutoPermissionPrompt"] = True
    cur.setdefault("enabledPlugins", {})["discord@claude-plugins-official"] = True
    cur.setdefault("effortLevel", "high")
    stop = {"matcher": "", "hooks": [{"type": "command",
                                       "command": f'"{PY}" "{ROOT / "scripts" / "voice_stop_hook.py"}"',
                                       "shell": "bash", "timeout": 5, "asyncRewake": True}]}
    hooks = cur.setdefault("hooks", {})
    hooks["Stop"] = [h for h in hooks.get("Stop", []) if "voice_stop_hook.py" not in json.dumps(h)] + [stop]
    write_json(up, cur)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
