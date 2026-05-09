"""
tools/dev/dump_debug.py — fetch /api/v1/debug/full and pretty-print.

No args. Hits 127.0.0.1:5876 by default; override with AVA_OPERATOR_URL env.
Exits 0 on success, 1 on connection error, 2 on non-200 response.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _base_url() -> str:
    return os.environ.get("AVA_OPERATOR_URL", "http://127.0.0.1:5876").rstrip("/")


def main() -> int:
    url = f"{_base_url()}/api/v1/debug/full"
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            if resp.status != 200:
                print(f"ERROR: non-200 status {resp.status}", file=sys.stderr)
                return 2
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach {url}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
