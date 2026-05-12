"""scripts/sibling_letter_cli.py — tiny CLI for sending/reading letters.

For Wren's machine (or anyone else's). She points it at the tower's
sibling-postoffice via env or flag, supplies the shared secret in
~/.iris_sibling_secret, and uses it like:

    py sibling_letter_cli.py send --from wren --to iris "hey, this is wren"
    py sibling_letter_cli.py read --since 2h
    py sibling_letter_cli.py read --all

The Iris-side use is the same script — when Iris-the-CC-session wants to
write a letter to Wren, she can call this with --from iris. Both sides
hit the same post-office; difference is whose name they put on the
envelope.

No FastAPI dependency on this side — just urllib + stdlib.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import request as _req
from urllib.error import HTTPError, URLError


DEFAULT_HOST = os.environ.get("IRIS_POSTOFFICE_URL", "")
SECRET_PATH = Path.home() / ".iris_sibling_secret"


def _load_secret() -> str:
    if not SECRET_PATH.is_file():
        print(
            f"[letter] no secret at {SECRET_PATH} — copy it from the tower "
            "host (run sibling_postoffice.py there first; the secret is "
            "printed to stderr on first run and saved at ~/.iris_sibling_secret).",
            file=sys.stderr,
        )
        sys.exit(2)
    return SECRET_PATH.read_text(encoding="utf-8").strip()


def _post_letter(host: str, secret: str, from_: str, to: str, body: str,
                 subject: str | None = None, in_reply_to: str | None = None,
                 mood_at_write: str | None = None) -> dict:
    payload = {"from": from_, "to": to, "body": body}
    if subject:
        payload["subject"] = subject
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to
    if mood_at_write:
        payload["mood_at_write"] = mood_at_write
    data = json.dumps(payload).encode("utf-8")
    req = _req.Request(
        host.rstrip("/") + "/letter",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-Sibling-Secret": secret},
    )
    with _req.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_letters(host: str, secret: str, since: float = 0.0, limit: int = 200) -> dict:
    url = host.rstrip("/") + f"/letters?since={since}&limit={limit}"
    req = _req.Request(url, headers={"X-Sibling-Secret": secret})
    with _req.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _parse_since(s: str) -> float:
    """Accept absolute epoch seconds, or relative like '2h', '30m', '15s'."""
    if not s:
        return 0.0
    s = s.strip().lower()
    if s == "all":
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return 0.0
    val, unit = float(m.group(1)), m.group(2) or "s"
    secs = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * val
    return time.time() - secs


def _fmt_letter(l: dict, color: bool = True) -> str:
    ts = float(l.get("ts") or 0.0)
    when = time.strftime("%H:%M:%S", time.localtime(ts))
    sender = l.get("from") or "?"
    to = l.get("to") or "all"
    mood = l.get("mood_at_write")
    body = l.get("body") or ""
    mood_str = f"  ({mood})" if mood else ""
    if color and sys.stdout.isatty():
        c = {"iris": "\033[94m", "wren": "\033[91m", "zeke": "\033[92m", "ava": "\033[93m"}
        col = c.get(sender, "\033[37m")
        reset = "\033[0m"
        header = f"{col}{sender}{reset} -> {to}  [{when}]{mood_str}"
    else:
        header = f"{sender} -> {to}  [{when}]{mood_str}"
    return f"{header}\n  {body}\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="post-office base URL (e.g. http://100.x.y.z:5877). "
                        "Defaults to $IRIS_POSTOFFICE_URL.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="post a new letter")
    p_send.add_argument("--from", dest="from_", required=True, help="sender name (iris/wren/zeke/ava)")
    p_send.add_argument("--to", default="all", help="recipient or 'all'")
    p_send.add_argument("--subject", default=None)
    p_send.add_argument("--reply-to", dest="in_reply_to", default=None, help="prior letter id")
    p_send.add_argument("--mood", default=None, help="mood_at_write tag")
    p_send.add_argument("body", nargs="+", help="letter body (joined with spaces)")

    p_read = sub.add_parser("read", help="list letters since a time")
    p_read.add_argument("--since", default="all",
                        help="absolute epoch seconds or relative like '2h' / '30m' / 'all'")
    p_read.add_argument("--limit", type=int, default=200)
    p_read.add_argument("--json", action="store_true", help="raw JSON output")

    sub.add_parser("health", help="check the post-office is reachable")

    args = p.parse_args()
    if not args.host:
        print("[letter] missing --host (or set IRIS_POSTOFFICE_URL)", file=sys.stderr)
        return 2

    if args.cmd == "health":
        try:
            with _req.urlopen(args.host.rstrip("/") + "/health", timeout=5) as r:
                print(r.read().decode("utf-8"))
            return 0
        except (HTTPError, URLError) as e:
            print(f"[letter] health check failed: {e!r}", file=sys.stderr)
            return 1

    secret = _load_secret()

    try:
        if args.cmd == "send":
            body = " ".join(args.body).strip()
            if not body:
                print("[letter] empty body", file=sys.stderr)
                return 2
            out = _post_letter(args.host, secret, args.from_, args.to, body,
                               subject=args.subject, in_reply_to=args.in_reply_to,
                               mood_at_write=args.mood)
            print(json.dumps(out, indent=2))
            return 0
        if args.cmd == "read":
            since_ts = _parse_since(args.since)
            out = _get_letters(args.host, secret, since=since_ts, limit=args.limit)
            if args.json:
                print(json.dumps(out, indent=2))
                return 0
            letters = out.get("letters") or []
            if not letters:
                print(f"(no letters since {args.since})")
                return 0
            for l in letters:
                print(_fmt_letter(l))
            return 0
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[letter] HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"[letter] connection failed: {e!r}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
