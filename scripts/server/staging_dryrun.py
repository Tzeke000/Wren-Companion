"""scripts/server/staging_dryrun.py - boot iris_runtime.py in IRIS_ROLE=staging and
exercise it over MCP stdio, the way Claude Code would, with NO cognition attached.

Written 2026-10-07 for the server port (step 1 of the move: "dry-run the port with the
tower still live and authoritative ... talking to nothing"). Safe by construction:
staging skips servo/resilience/app-discovery (brain/iris_bootstrap.py), and nothing here
starts claude, Discord, the post-office or the Vector.

usage: python scripts/server/staging_dryrun.py [settle_seconds=90]
stderr of the runtime -> /tmp/iris_dryrun_stderr.log
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[2]
SETTLE = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0


def _short(res, n=400):
    try:
        txt = res.content[0].text
    except Exception:
        txt = repr(res)
    return txt[:n].replace("\n", " ")


async def main() -> int:
    env = dict(os.environ)
    env["IRIS_ROLE"] = "staging"
    env.setdefault("PYTHONUNBUFFERED", "1")
    params = StdioServerParameters(command=sys.executable, args=[str(REPO / "iris_runtime.py")],
                                   cwd=str(REPO), env=env)
    errlog = open("/tmp/iris_dryrun_stderr.log", "w")
    t0 = time.time()
    async with stdio_client(params, errlog=errlog) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print(f"[dryrun] initialize OK in {time.time()-t0:.1f}s: {init.serverInfo.name}")
            tools = await s.list_tools()
            print(f"[dryrun] {len(tools.tools)} MCP tools advertised")
            for name, args in (("time_check", {}),):
                res = await s.call_tool(name, args)
                print(f"[dryrun] {name}: {_short(res)}")
            print(f"[dryrun] settling {SETTLE:.0f}s for eager init ...")
            await asyncio.sleep(SETTLE)
            for name, args in (("memory_search", {"query": "V100 cable riser"}),
                               ("iris_health", {}),
                               ("iris_tool_list", {}),
                               ("iris_tool_call", {"name": "self_claim_check", "params": {}})):
                try:
                    res = await s.call_tool(name, args)
                    print(f"[dryrun] {name}: {_short(res, 900)}")
                except Exception as e:
                    print(f"[dryrun] {name} FAILED: {e!r}")
    print(f"[dryrun] clean shutdown after {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
