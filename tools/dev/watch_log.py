"""
tools/dev/watch_log.py — live tail of Ava's debug rings.

Polls /api/v1/debug/full at 1s intervals and prints new lines from the
trace ring (default), log ring, or errors_recent ring.

Usage:
    py -3.11 tools/dev/watch_log.py                # trace lines only
    py -3.11 tools/dev/watch_log.py --kind log     # all stdout lines
    py -3.11 tools/dev/watch_log.py --kind errors  # error events
    py -3.11 tools/dev/watch_log.py --grep app_disc  # filter substring

Exits cleanly on Ctrl+C.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request


def _base_url() -> str:
    return os.environ.get("AVA_OPERATOR_URL", "http://127.0.0.1:5876").rstrip("/")


def _fetch() -> dict | None:
    try:
        with urllib.request.urlopen(f"{_base_url()}/api/v1/debug/full", timeout=3.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None
    except Exception:
        return None


def _format_error(e: dict) -> str:
    return f"{e.get('ts','')} [{e.get('module','')}] {e.get('message','')}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail Ava's debug rings.")
    ap.add_argument("--kind", choices=["trace", "log", "errors"], default="trace")
    ap.add_argument("--grep", default="", help="Substring filter (case-sensitive)")
    ap.add_argument("--interval", type=float, default=1.0, help="Poll interval seconds")
    args = ap.parse_args()

    seen: set[str] = set()
    print(f"[watch_log] tailing {args.kind} from {_base_url()} every {args.interval}s "
          f"(grep={args.grep!r})", file=sys.stderr)

    # On Ctrl+C, exit cleanly without traceback noise.
    def _bye(*_):
        sys.exit(0)
    try:
        signal.signal(signal.SIGINT, _bye)
    except Exception:
        pass

    while True:
        data = _fetch()
        if data is None:
            time.sleep(args.interval)
            continue
        if args.kind == "errors":
            items = data.get("errors_recent") or []
            new_items: list[str] = []
            for e in items:
                key = json.dumps(e, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                line = _format_error(e)
                if args.grep and args.grep not in line:
                    continue
                new_items.append(line)
            for line in new_items:
                print(line, flush=True)
        else:
            key_field = "recent_traces" if args.kind == "trace" else "recent_log_lines"
            items = data.get(key_field) or []
            for line in items:
                if line in seen:
                    continue
                seen.add(line)
                if args.grep and args.grep not in line:
                    continue
                print(line, flush=True)
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
