"""scripts/smoketest_discord_mcp.py

Spawn the discord plugin's MCP command exactly the way Claude Code's MCP
loader would, then send a JSON-RPC 'initialize' over stdio. Confirms:

  1. bun.exe is reachable (no `'bun' is not recognized` errors)
  2. The MCP server stays alive past the bun-install step
  3. JSON-RPC handshake completes (server responds with serverInfo)

Exits 0 on success, non-zero on failure. Prints diagnostics on stderr.

This is the deterministic local check before we launch a fresh `claude`
process to verify auto-spawn end-to-end.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    home = Path(os.environ["USERPROFILE"])
    cache_root = home / ".claude/plugins/cache/claude-plugins-official/discord"
    versions = sorted(
        (p for p in cache_root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    if not versions:
        print("ERROR: no cached discord plugin version found", file=sys.stderr)
        return 1
    plugin_root = versions[0]
    mcp_json = plugin_root / ".mcp.json"
    cfg = json.loads(mcp_json.read_text(encoding="utf-8"))
    spec = cfg["mcpServers"]["discord"]

    cmd = spec["command"]
    args = [
        a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
        for a in spec["args"]
    ]

    env = os.environ.copy()
    for k, v in spec.get("env", {}).items():
        env[k] = v.replace("${PATH}", os.environ.get("PATH", ""))
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)

    print(f"command: {cmd}", file=sys.stderr)
    print(f"args: {args}", file=sys.stderr)
    print(f"cwd: {plugin_root}", file=sys.stderr)
    print(f"PATH first 200: {env['PATH'][:200]}", file=sys.stderr)

    proc = subprocess.Popen(
        [cmd] + args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(plugin_root),
        env=env,
    )

    # Send JSON-RPC initialize
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoketest", "version": "1.0"},
        },
    }
    payload = (json.dumps(init) + "\n").encode("utf-8")

    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
    except Exception as e:
        print(f"failed to write to stdin: {e}", file=sys.stderr)

    # Wait up to 60s for first stdout line (bun install can take time on cold cache)
    deadline = time.time() + 60.0
    response_line = None
    early_exit = False
    while time.time() < deadline:
        if proc.poll() is not None:
            early_exit = True
            break
        # readline with a short blocking poll
        try:
            line = proc.stdout.readline()
        except Exception:
            line = b""
        if line:
            response_line = line
            break
        time.sleep(0.2)

    stderr_data = b""
    if early_exit or response_line is None:
        # collect stderr to diagnose
        try:
            stderr_data = proc.stderr.read() or b""
        except Exception:
            pass

    if early_exit:
        print(
            f"ERROR: server exited before responding. exit={proc.returncode}",
            file=sys.stderr,
        )
        print("--- stderr ---", file=sys.stderr)
        sys.stderr.write(stderr_data.decode("utf-8", errors="replace"))
        return 2

    if response_line is None:
        print("ERROR: no response within 60s", file=sys.stderr)
        proc.kill()
        try:
            stderr_data = proc.stderr.read() or b""
        except Exception:
            pass
        print("--- stderr ---", file=sys.stderr)
        sys.stderr.write(stderr_data.decode("utf-8", errors="replace"))
        return 3

    try:
        resp = json.loads(response_line.decode("utf-8"))
    except Exception:
        print(
            f"ERROR: response not JSON: {response_line!r}",
            file=sys.stderr,
        )
        proc.kill()
        return 4

    server_info = resp.get("result", {}).get("serverInfo", {})
    print(
        f"OK: server responded. name={server_info.get('name')!r} "
        f"version={server_info.get('version')!r}",
        file=sys.stderr,
    )

    # Clean shutdown
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
