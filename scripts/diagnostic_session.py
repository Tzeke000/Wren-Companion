"""scripts/diagnostic_session.py — Doctor harness driver.

Starts a diagnostic session against a running Ava instance. Mints an HMAC-
signed token from `state/doctor.secret`, declares the session, runs a series
of synthetic turns via /api/v1/debug/inject_transcript (gated by AVA_DEBUG=1
on Ava's side), captures latencies + reply text, then ends the session and
prints the audit log path.

Usage:
    py -3.11 scripts/diagnostic_session.py
    py -3.11 scripts/diagnostic_session.py --turns 3 --base-url http://127.0.0.1:5876
    py -3.11 scripts/diagnostic_session.py --probe   # just authenticate, no turns

The audio loopback (Component 1 of the autonomous testing work order) is
deferred — this script uses the inject_transcript path to test the
diagnostic protocol end-to-end without needing the virtual audio cable
yet. Once VB-CABLE is installed, a separate harness can drive real audio.

Per docs/AUTONOMOUS_TESTING.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests


def _read_secret(repo_root: Path) -> bytes:
    secret_path = repo_root / "state" / "doctor.secret"
    if not secret_path.is_file():
        raise SystemExit(
            f"doctor secret not found at {secret_path}.\n"
            "Start Ava once (the operator HTTP server creates it on first call to "
            "/api/v1/diagnostic/declare). Or generate manually: "
            "py -3.11 -c \"from brain.doctor_session import get_or_create_secret; "
            "get_or_create_secret({'BASE_DIR': '.'})\""
        )
    return secret_path.read_bytes()


def _mint_token(secret: bytes, session_id: str, ttl_sec: int = 900) -> str:
    """Inline doctor-side mint to avoid a heavy import path."""
    import base64
    import hashlib
    import hmac as _hmac

    payload = {
        "sub": "claude_doctor",
        "role": "doctor",
        "session_id": session_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + int(ttl_sec),
    }
    header = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    sig = _hmac.new(secret, header.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{header}.{sig}"


def _post(url: str, token: str, body: dict | None = None, timeout: float = 15.0) -> dict:
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body or {},
        timeout=timeout,
    )
    if r.status_code >= 300:
        return {"ok": False, "http_status": r.status_code, "text": r.text[:200]}
    return r.json()


def _get(url: str, token: str, timeout: float = 15.0) -> dict:
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if r.status_code >= 300:
        return {"ok": False, "http_status": r.status_code, "text": r.text[:200]}
    return r.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a diagnostic session against Ava.")
    parser.add_argument("--base-url", default="http://127.0.0.1:5876", help="Operator HTTP base URL.")
    parser.add_argument("--turns", type=int, default=2, help="Number of synthetic turns to run.")
    parser.add_argument("--probe", action="store_true", help="Just declare session + end, no turns.")
    parser.add_argument(
        "--prompts",
        nargs="*",
        default=["hey ava what time is it", "tell me a one sentence joke about clouds"],
        help="Prompts to inject (used in order; cycles if --turns > len).",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    secret = _read_secret(repo_root)
    session_id = f"sess_{int(time.time())}"
    token = _mint_token(secret, session_id)
    base = args.base_url.rstrip("/")

    print(f"[diag] session_id={session_id}")
    print(f"[diag] declaring session at {base}/api/v1/diagnostic/declare ...")
    declare = _post(f"{base}/api/v1/diagnostic/declare", token, {"session_id": session_id})
    print(f"[diag] declare: {declare}")
    if not declare.get("ok"):
        return 1

    if args.probe:
        # Just authenticate, fetch full diagnostic, and end.
        full = _get(f"{base}/api/v1/diagnostic/full", token)
        print(f"[diag] /diagnostic/full keys: {sorted(list(full.keys()))[:15]}")
        end = _post(f"{base}/api/v1/diagnostic/end", token)
        print(f"[diag] end: {end}")
        return 0

    # Run turns via inject_transcript (needs AVA_DEBUG=1 on the server).
    last_event_id = 0
    for i in range(args.turns):
        prompt = args.prompts[i % len(args.prompts)]
        t0 = time.time()
        result = _post(
            f"{base}/api/v1/debug/inject_transcript",
            token,
            {"text": prompt, "speak": False, "as_user": "claude_code", "wait_for_audio": False},
            timeout=120.0,
        )
        ms = int((time.time() - t0) * 1000)
        reply = (result or {}).get("reply") or ""
        print(f"[diag] turn {i + 1}/{args.turns}: {prompt!r} -> ms={ms} reply={reply[:80]!r}")

        # Pull events emitted during this turn.
        ev_response = _get(f"{base}/api/v1/diagnostic/events?since={last_event_id}", token)
        if ev_response.get("ok"):
            events = ev_response.get("events", [])
            for ev in events:
                last_event_id = max(last_event_id, ev["id"])
                print(f"  [event] id={ev['id']} kind={ev['kind']} ts_ms={ev['ts_ms']}")

    end = _post(f"{base}/api/v1/diagnostic/end", token)
    print(f"[diag] end: {end}")
    if end.get("log_path"):
        print(f"[diag] audit log: {end['log_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
