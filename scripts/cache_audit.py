"""cache_audit.py — measure prompt-cache effectiveness from Claude Code session transcripts.

Built 2026-07-25 to answer the question the whole token-lean plan hinges on:
*are the ~210 MCP tool schemas actually being served from cache, or are we paying
full freight for them on every single turn?*

Cached input bills at ~0.1x, cache writes at ~1.25x, uncached input at 1.0x.
If cache_read is high, the 210 schemas are cheap and trimming them is a rounding
error. If cache_read is ~0, the prefix is being invalidated every turn and that IS
the problem. Same work, opposite payoff -- hence: measure before building.

Usage:
    python scripts/cache_audit.py                 # newest session in this project
    python scripts/cache_audit.py --all           # every session, newest first
    python scripts/cache_audit.py --file X.jsonl  # one specific transcript

No deps beyond stdlib. Read-only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

PROJECT_DIR = Path(
    os.environ.get(
        "IRIS_CC_PROJECT_DIR",
        r"C:\Users\Owner\.claude\projects\D--Wren-Companion",
    )
)

# Anthropic list price, Opus-tier, $/token. Output includes thinking.
PRICE_IN = 5.00 / 1_000_000
PRICE_OUT = 25.00 / 1_000_000
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def iter_usage(path: Path):
    """Yield each assistant message's usage dict from a session transcript."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if isinstance(usage, dict) and usage:
                yield usage


def audit(path: Path) -> dict | None:
    n = 0
    fresh = cread = cwrite = out = 0
    zero_read_turns = 0
    for u in iter_usage(path):
        n += 1
        f = u.get("input_tokens", 0) or 0
        r = u.get("cache_read_input_tokens", 0) or 0
        w = u.get("cache_creation_input_tokens", 0) or 0
        o = u.get("output_tokens", 0) or 0
        fresh += f
        cread += r
        cwrite += w
        out += o
        if r == 0:
            zero_read_turns += 1
    if not n:
        return None

    total_in = fresh + cread + cwrite
    hit_rate = (cread / total_in * 100) if total_in else 0.0

    # What we actually pay vs. what the same prompt would cost with no cache at all.
    billed = (
        fresh * PRICE_IN
        + cread * PRICE_IN * CACHE_READ_MULT
        + cwrite * PRICE_IN * CACHE_WRITE_MULT
        + out * PRICE_OUT
    )
    uncached = total_in * PRICE_IN + out * PRICE_OUT

    return {
        "file": path.name,
        "turns": n,
        "fresh_in": fresh,
        "cache_read": cread,
        "cache_write": cwrite,
        "output": out,
        "total_in": total_in,
        "hit_rate": hit_rate,
        "zero_read_turns": zero_read_turns,
        "avg_in_per_turn": total_in / n,
        "avg_out_per_turn": out / n,
        "billed_usd": billed,
        "uncached_usd": uncached,
        "saved_usd": uncached - billed,
        "out_cost_share": (out * PRICE_OUT / billed * 100) if billed else 0.0,
    }


def show(a: dict) -> None:
    print(f"\n=== {a['file']} ===")
    print(f"  assistant turns      {a['turns']}")
    print(f"  input  total         {a['total_in']:>12,}  ({a['avg_in_per_turn']:,.0f}/turn)")
    print(f"    fresh (full price) {a['fresh_in']:>12,}")
    print(f"    cache READ  (0.1x) {a['cache_read']:>12,}")
    print(f"    cache WRITE (1.25x){a['cache_write']:>12,}")
    print(f"  output (5x in)       {a['output']:>12,}  ({a['avg_out_per_turn']:,.0f}/turn)")
    print(f"  --")
    print(f"  CACHE HIT RATE       {a['hit_rate']:>11.1f}%   <-- the number that decides the plan")
    print(f"  turns w/ zero read   {a['zero_read_turns']:>12} / {a['turns']}")
    print(f"  output share of cost {a['out_cost_share']:>11.1f}%")
    print(f"  billed  ~${a['billed_usd']:.2f}   vs uncached ~${a['uncached_usd']:.2f}"
          f"   (cache saved ~${a['saved_usd']:.2f})")

    print("  --")
    if a["hit_rate"] >= 70:
        print("  VERDICT: cache is working. The tool-schema prefix is already ~10x discounted,")
        print("           so trimming schemas buys LESS than the raw token count suggests.")
        print("           Look at the OUTPUT side instead (it bills 5x, and includes thinking).")
    elif a["hit_rate"] >= 30:
        print("  VERDICT: partial caching. Something is invalidating the prefix some turns --")
        print("           suspect the 20-block lookback, or a volatile value in the prefix.")
    else:
        print("  VERDICT: cache is NOT working. The prefix is being rebuilt nearly every turn.")
        print("           THIS is the problem -- fixing it beats every other item on the list.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="audit every session")
    ap.add_argument("--file", help="audit one transcript by name or path")
    args = ap.parse_args()

    if args.file:
        p = Path(args.file)
        paths = [p if p.is_absolute() else PROJECT_DIR / p]
    else:
        found = sorted(
            PROJECT_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if not found:
            print(f"no transcripts under {PROJECT_DIR}")
            return
        paths = found if args.all else found[:1]

    results = [a for p in paths if (a := audit(p))]
    for a in results:
        show(a)

    if len(results) > 1:
        ti = sum(a["total_in"] for a in results)
        cr = sum(a["cache_read"] for a in results)
        print(f"\n=== ALL {len(results)} SESSIONS ===")
        print(f"  overall cache hit rate {cr / ti * 100:.1f}%" if ti else "  no input")
        print(f"  total billed ~${sum(a['billed_usd'] for a in results):.2f}")


if __name__ == "__main__":
    main()
