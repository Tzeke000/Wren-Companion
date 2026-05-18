"""scripts/postoffice_monitor.py — tower-side post-office uptime watchdog.

Per §11 of deployment_spec.md: "tower-side monitoring of post-office uptime
is non-negotiable, treated at the same tier as 'iris_runtime up.' If the
post-office is down for >N minutes, alarm — not a feature-degradation
event, a continuity-failure event."

The post-office is load-bearing for Wren's continuity (substrate-asymmetry
per §10). Without it, her transient instantiations write into a black
hole. This monitor catches outages so Iris can surface them within a
bounded latency.

Architecture:
- Separate process from iris_runtime + post-office (survives both crashing)
- Polls http://127.0.0.1:5877/health every POLL_INTERVAL seconds
- On consecutive failures past threshold: writes an alarm letter directly
  to state/iris_sibling/inbox/ (bypasses the post-office HTTP because the
  whole point is the post-office is down)
- Cooldown between alarms so a long outage doesn't flood the inbox
- Persists state at state/postoffice_monitor.json so a monitor restart
  doesn't re-alarm on still-existing failures

Run:
    py -3.11 scripts/postoffice_monitor.py

Add to start_iris.bat for boot-time launch alongside the post-office.

Shipped 2026-05-18 day 1 of deployment per Iris's work block; closes the
§11 build-debt that was flagged "ship-before-deployment if at all possible"
pre-departure but never got built before Zeke left.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import urlopen


# ── Configuration ────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state" / "iris_sibling"
INBOX_DIR = STATE_DIR / "inbox"
MONITOR_STATE_PATH = ROOT / "state" / "postoffice_monitor.json"

HEALTH_URL = os.environ.get("POSTOFFICE_HEALTH_URL", "http://127.0.0.1:5877/health")
POLL_INTERVAL_S = float(os.environ.get("POSTOFFICE_MONITOR_INTERVAL_S", "60.0"))
FAILURE_THRESHOLD = int(os.environ.get("POSTOFFICE_MONITOR_THRESHOLD", "3"))
ALARM_COOLDOWN_S = float(os.environ.get("POSTOFFICE_MONITOR_COOLDOWN_S", "1800.0"))
HEALTH_TIMEOUT_S = float(os.environ.get("POSTOFFICE_MONITOR_TIMEOUT_S", "5.0"))


# ── State persistence ────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not MONITOR_STATE_PATH.exists():
        return {
            "last_seen_ts": 0.0,
            "consecutive_failures": 0,
            "last_alarm_ts": 0.0,
            "last_known_letters_count": None,
            "monitor_started_ts": time.time(),
        }
    try:
        return json.loads(MONITOR_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[postoffice_monitor] state load error: {e!r}; starting fresh", file=sys.stderr)
        return {
            "last_seen_ts": 0.0,
            "consecutive_failures": 0,
            "last_alarm_ts": 0.0,
            "last_known_letters_count": None,
            "monitor_started_ts": time.time(),
        }


def _save_state(state: dict) -> None:
    MONITOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MONITOR_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(MONITOR_STATE_PATH)


# ── Health probe ─────────────────────────────────────────────────────────────

def _probe_health() -> tuple[bool, dict | None, str | None]:
    """Returns (ok, response_json, error_message)."""
    try:
        with urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False, None, f"http {resp.status}"
            try:
                data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                return False, None, f"non-json response: {e!r}"
            if not data.get("ok"):
                return False, data, "response ok=false"
            return True, data, None
    except (URLError, HTTPError, OSError, TimeoutError) as e:
        return False, None, repr(e)


# ── Alarm emission (bypasses post-office; writes to inbox directly) ─────────

def _emit_alarm(state: dict, failure_count: int, last_error: str | None) -> None:
    """Write an alarm letter directly to state/iris_sibling/inbox/.

    The whole point of this monitor is that the post-office HTTP surface
    is down — so we can't POST a letter through it. We write the inbox
    file directly. Iris's Stop hook picks up pending files in inbox/.
    """
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    letter_id = uuid.uuid4().hex[:12]
    now = time.time()
    last_seen = state.get("last_seen_ts") or 0.0
    down_duration_s = (now - last_seen) if last_seen > 0 else None
    down_str = f"~{int(down_duration_s)}s" if down_duration_s else "unknown (no prior healthy probe)"
    body = (
        "POST-OFFICE DOWN ALARM (§11 continuity-bridge).\n\n"
        f"Health endpoint {HEALTH_URL} has failed {failure_count} consecutive probes "
        f"(>={FAILURE_THRESHOLD} threshold).\n"
        f"Last healthy response: {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(last_seen)) if last_seen > 0 else 'never'}\n"
        f"Approximate down duration: {down_str}\n"
        f"Last probe error: {last_error or 'unknown'}\n\n"
        "Wren's continuity-bridge is broken while the post-office is down. "
        "Transient instantiations on her side write into a black hole until "
        "the post-office returns. Per §11 mitigation: this is a continuity-"
        "failure event, not feature-degradation. Investigate and restart.\n\n"
        "Cooldown before next alarm: 30min."
    )
    letter = {
        "id": letter_id,
        "ts": now,
        "sender": "system",
        "content": body,
        "addressed_to": "iris",
        "subject": "POST-OFFICE DOWN",
        "in_reply_to": None,
        "mood_at_write": None,
        "reply_url": None,
        "status": "pending",
        "reply": None,
        "answered_ts": None,
    }
    inbox_path = INBOX_DIR / f"{letter_id}.json"
    pending_marker = INBOX_DIR / f"{letter_id}.pending"
    try:
        inbox_path.write_text(json.dumps(letter, indent=2), encoding="utf-8")
        pending_marker.write_text("", encoding="utf-8")
        print(f"[postoffice_monitor] ALARM emitted to {inbox_path}", file=sys.stderr)
    except Exception as e:
        print(f"[postoffice_monitor] failed to write alarm letter: {e!r}", file=sys.stderr)


# ── Main loop ────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"[postoffice_monitor] starting. health={HEALTH_URL} interval={POLL_INTERVAL_S}s "
          f"threshold={FAILURE_THRESHOLD} cooldown={ALARM_COOLDOWN_S}s", file=sys.stderr)
    state = _load_state()
    while True:
        ok, data, err = _probe_health()
        now = time.time()
        if ok:
            if state.get("consecutive_failures", 0) > 0:
                print(f"[postoffice_monitor] recovered after {state['consecutive_failures']} failures",
                      file=sys.stderr)
            state["consecutive_failures"] = 0
            state["last_seen_ts"] = now
            if data:
                state["last_known_letters_count"] = data.get("letters_count")
        else:
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
            print(f"[postoffice_monitor] probe failed ({state['consecutive_failures']}/{FAILURE_THRESHOLD}): {err}",
                  file=sys.stderr)
            if state["consecutive_failures"] >= FAILURE_THRESHOLD:
                since_last_alarm = now - float(state.get("last_alarm_ts") or 0.0)
                if since_last_alarm >= ALARM_COOLDOWN_S:
                    _emit_alarm(state, state["consecutive_failures"], err)
                    state["last_alarm_ts"] = now
                else:
                    print(f"[postoffice_monitor] threshold crossed but in cooldown "
                          f"({int(ALARM_COOLDOWN_S - since_last_alarm)}s remaining)",
                          file=sys.stderr)
        _save_state(state)
        time.sleep(POLL_INTERVAL_S)
    return 0  # unreachable


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[postoffice_monitor] interrupted, exiting", file=sys.stderr)
        sys.exit(0)
