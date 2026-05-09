"""
tools/dev/inject_test_turn.py — drive a synthetic turn through Ava.

Usage:
  py -3.11 tools/dev/inject_test_turn.py --text "what time is it"
  py -3.11 tools/dev/inject_test_turn.py --text "..." --wait-audio
  py -3.11 tools/dev/inject_test_turn.py --text "..." --no-speak
  py -3.11 tools/dev/inject_test_turn.py --text "..." --as-user zeke

By default the turn is routed through the claude_code developer
profile so test runs don't pollute Zeke's relationship state, mood
history, or memory. Use --as-user zeke to simulate a real user turn
(or any other registered profile in profiles/).

Exits 0 if Ava produced a non-empty reply, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _base_url() -> str:
    return os.environ.get("AVA_OPERATOR_URL", "http://127.0.0.1:5876").rstrip("/")


def main() -> int:
    ap = argparse.ArgumentParser(description="Inject a synthetic turn into Ava.")
    ap.add_argument("--text", required=True, help="Transcript text to feed run_ava")
    ap.add_argument("--source", default="test_wake", help="Wake source label (default test_wake)")
    ap.add_argument("--wait-audio", action="store_true", help="Block until TTS playback completes")
    ap.add_argument("--no-speak", action="store_true", help="Skip TTS — return reply text only")
    ap.add_argument(
        "--as-user",
        default="claude_code",
        help="Profile id to attribute the turn to (default claude_code). Use 'zeke' to simulate the real user.",
    )
    ap.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout seconds")
    args = ap.parse_args()

    body = {
        "text": args.text,
        "wake_source": args.source,
        "wait_for_audio": bool(args.wait_audio),
        "speak": not bool(args.no_speak),
        "as_user": args.as_user,
        "timeout_seconds": float(args.timeout),
    }
    url = f"{_base_url()}/api/v1/debug/inject_transcript"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=args.timeout + 5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: HTTP {e.code}\n{body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach {url}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
