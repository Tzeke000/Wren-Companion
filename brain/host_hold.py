"""brain/host_hold.py — round-2 fix 1.2 (2026-10-01): a CONSUMER for cognition holds, and a
Discord fallback for replies the mouth cannot carry.

The disease it closes (Zeke's open-loop rule): on 2026-09-30 the session token cap held
cognition 14:3x→17:42 and again 21:26→22:44. `state/stop_hook.log` shows ZERO turn-ends in
either window — the Claude CLI retried the API internally, the host sat in `turn.done.wait()`,
and nothing anywhere said "held". Zeke's message waited 3.4 h in silence. The second hold ended
inside quiet hours, so my answer streamed into a mouth that was flagged off and was lost.

Two loops, each with a consumer:

  1. HoldDetector — pure state machine fed by the host's stream consumer (activity, tool
     start/finish, turn start/end, CLI stderr lines). `check(now)` yields a 'held' transition
     once when an ACTIVE turn has produced nothing for HOLD_AFTER_S with no tool in flight
     (or sooner when the CLI's stderr already named a rate/usage limit), and a 'released'
     transition once when output resumes or the turn ends. The host writes/clears
     `state/cognition_held.json` and DMs Discord ONCE per transition. A later self-check reads
     the flag.

  2. VoiceFlagCache + route_reply — the voice flag is re-read (cheaply, TTL-cached) at the
     moment a sentence would be SPOKEN, not only when the turn began. A voice reply whose mouth
     is off is routed to Discord as text instead of into the void.

Nothing here raises into the host: every I/O helper is fail-soft and returns a dict.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELD_FLAG = os.path.join(ROOT, "state", "cognition_held.json")
VOICE_OFF_FLAG = os.path.join(ROOT, "state", "voice_deliberately_off.json")
HOLD_AFTER_S = float(os.environ.get("IRIS_HOLD_AFTER_S", "180"))
HOLD_FAST_AFTER_S = float(os.environ.get("IRIS_HOLD_FAST_AFTER_S", "30"))
DISCORD_API = "https://discord.com/api/v10"

# Substrings (lower-cased) in a CLI stderr line that name a quota/overload condition.
RATE_HINTS: tuple[str, ...] = (
    "rate limit", "rate_limit", "ratelimit", "usage limit", "limit reached", "429",
    "overloaded", "overloaded_error", "retrying", "retry in", "resets at", "too many requests",
)


def human_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# ── voice flag ────────────────────────────────────────────────────────────────
def voice_flag_says_off(path: str = VOICE_OFF_FLAG) -> bool:
    """True only if the flag EXISTS and says {"off": true}. Absent/garbage → False (fail-open,
    the same semantics as iris_body_host._voice_flag_says_off and scripts/voice_watchdog.py)."""
    try:
        if not os.path.isfile(path):
            return False
        with open(path, "r", encoding="utf-8") as fh:
            return bool((json.load(fh) or {}).get("off"))
    except Exception:
        return False


class VoiceFlagCache:
    """Re-reads the voice flag at most every `ttl_s` so the per-sentence speak path can ask
    'is the mouth still allowed?' without a stat per token."""

    def __init__(self, path: str = VOICE_OFF_FLAG, ttl_s: float = 5.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.ttl_s = float(ttl_s)
        self.clock = clock
        self._val = False
        self._ts = -1e18

    def off(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        if now - self._ts >= self.ttl_s:
            self._val = voice_flag_says_off(self.path)
            self._ts = now
        return self._val

    def invalidate(self) -> None:
        self._ts = -1e18


def route_reply(source: str | None, speak_sources: Iterable[str], voice_off: bool) -> tuple[bool, bool]:
    """(speak_out, route_to_discord). A speaking source whose mouth is flagged off becomes a
    silent turn whose final text goes to Discord. Text sources are unchanged (silent, no route)."""
    speaks = (source or "") in set(speak_sources)
    if speaks and voice_off:
        return False, True
    return speaks, False


# ── hold detector ─────────────────────────────────────────────────────────────
class HoldDetector:
    """Pure state machine — no I/O, injectable clock, transitions reported ONCE each."""

    def __init__(self, hold_after_s: float = HOLD_AFTER_S, fast_after_s: float = HOLD_FAST_AFTER_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.hold_after_s = float(hold_after_s)
        self.fast_after_s = float(fast_after_s)
        self.clock = clock
        self.turn_active = False
        self.source: str | None = None
        self.turn_started_ts = 0.0
        self.last_activity_ts = 0.0
        self.tools_in_flight = 0
        self.held_since: float | None = None
        self.stderr_hint: str | None = None
        self.stderr_hint_ts = 0.0
        self.hold_count = 0
        self.api_error_hint: str | None = None   # 2026-10-01: last API error seen on the stream
        self.api_error_ts = 0.0
        self._pending: dict[str, Any] | None = None

    def _now(self, now: float | None) -> float:
        return self.clock() if now is None else now

    # events ------------------------------------------------------------------
    def turn_started(self, source: str | None, now: float | None = None) -> None:
        now = self._now(now)
        self.turn_active = True
        self.source = source
        self.turn_started_ts = now
        self.last_activity_ts = now
        self.tools_in_flight = 0

    def turn_ended(self, now: float | None = None) -> None:
        self.turn_active = False
        self.tools_in_flight = 0

    def activity(self, now: float | None = None) -> None:
        self.last_activity_ts = self._now(now)

    def tool_started(self, now: float | None = None) -> None:
        self.tools_in_flight += 1
        self.activity(now)

    def tool_finished(self, now: float | None = None) -> None:
        self.tools_in_flight = max(0, self.tools_in_flight - 1)
        self.activity(now)

    def note_stderr(self, line: str, now: float | None = None) -> bool:
        """Remember a CLI stderr line that names a quota/overload condition. Returns True if it did."""
        low = (line or "").lower()
        if any(h in low for h in RATE_HINTS):
            self.stderr_hint = (line or "").strip()[:200]
            self.stderr_hint_ts = self._now(now)
            return True
        return False

    def api_error(self, kind: str, status: int | str | None = None,
                  now: float | None = None) -> dict[str, Any] | None:
        """An API error message arrived on the stream (rate_limit 429, overloaded 529, ...).
        2026-10-01: the 15:35->18:40 hold was 42 such messages and this detector never fired, because
        they arrived as AssistantMessages (counted as activity) and never touched stderr. They are NOT
        output - they ARE the hold. Hold immediately; the event is handed to the next check() so the
        flag/DM path stays single. Repeats while held are absorbed; real output releases as before."""
        now = self._now(now)
        self.api_error_hint = (str(kind) + (" (" + str(status) + ")" if status else "")).strip()[:120]
        self.api_error_ts = now
        if not self.turn_active or self.held_since is not None:
            return None
        self.held_since = now
        self.hold_count += 1
        ev = {"event": "held", "since": now, "silent_s": round(self.silent_for(now), 1),
              "source": self.source, "hint": self.api_error_hint,
              "reason": "API error on an active turn: " + self.api_error_hint}
        self._pending = ev
        return ev

    # query -------------------------------------------------------------------
    def silent_for(self, now: float | None = None) -> float:
        now = self._now(now)
        return now - max(self.last_activity_ts, self.turn_started_ts)

    def check(self, now: float | None = None) -> dict[str, Any] | None:
        now = self._now(now)
        if self._pending is not None:          # an api_error() hold waiting for the flag/DM path
            ev, self._pending = self._pending, None
            return ev
        if self.held_since is None:
            if not self.turn_active or self.tools_in_flight > 0:
                return None
            silent = self.silent_for(now)
            hint_recent = bool(self.stderr_hint) and (now - self.stderr_hint_ts) <= self.hold_after_s
            if silent >= self.hold_after_s or (hint_recent and silent >= self.fast_after_s):
                self.held_since = now
                self.hold_count += 1
                return {"event": "held", "since": now, "silent_s": round(silent, 1), "source": self.source,
                        "hint": self.stderr_hint if hint_recent else None,
                        "reason": "cli stderr named a limit" if hint_recent else
                                  f"no model output for {int(silent)}s on an active turn, no tool in flight"}
            return None
        if (not self.turn_active) or self.last_activity_ts > self.held_since:
            since = self.held_since
            self.held_since = None
            return {"event": "released", "since": since, "duration_s": round(now - since, 1),
                    "source": self.source, "how": "turn ended" if not self.turn_active else "output resumed"}
        return None


# ── flag file (the thing a self-check can READ) ───────────────────────────────
def write_held_flag(info: dict[str, Any], path: str = HELD_FLAG) -> dict[str, Any]:
    since = float(info.get("since") or time.time())
    payload = {
        "held": True,
        "since": since,
        "since_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since)),
        "source": info.get("source"),
        "silent_s": info.get("silent_s"),
        "reason": info.get("reason"),
        "hint": info.get("hint"),
        "pid": os.getpid(),
        "restore": "host clears this itself when output resumes; stale (>6 h) with a live host = investigate",
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
        return {"ok": True, "path": path}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}


def read_held_flag(path: str = HELD_FLAG) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def clear_held_flag(path: str = HELD_FLAG) -> dict[str, Any] | None:
    prev = read_held_flag(path)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return prev


# ── Discord outbound (REST, no gateway) ───────────────────────────────────────
def discord_send(token: str | None, channel_id: str, text: str, timeout: float = 10.0,
                 opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    """POST one message. Never raises. Discord caps content at 2000 chars — long text is split."""
    if not token:
        return {"ok": False, "error": "no bot token"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [text]
    ids: list[str] = []
    for chunk in chunks:
        body = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(
            f"{DISCORD_API}/channels/{channel_id}/messages", data=body, method="POST",
            headers={"Authorization": "Bot " + token, "Content-Type": "application/json",
                     "User-Agent": "IrisHost/2.0"})
        try:
            with opener(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8") or "{}")
                ids.append(str(data.get("id", "")))
        except urllib.error.HTTPError as e:
            return {"ok": False, "status": e.code, "error": f"HTTP {e.code}", "sent": ids}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e), "sent": ids}
    return {"ok": True, "ids": ids, "chunks": len(chunks)}


def format_held_message(info: dict[str, Any]) -> str:
    src = info.get("source") or "a"
    hint = info.get("hint")
    why = f" — the CLI said: `{hint}`" if hint else " (usage cap or API overload, most likely)"
    return (f"⏳ Held — no model output for {human_duration(float(info.get('silent_s') or 0))} on a {src} turn{why}. "
            "Your messages are queued, not lost; I'll answer the moment it releases.")


def format_released_message(info: dict[str, Any]) -> str:
    return f"✅ Released after {human_duration(float(info.get('duration_s') or 0))} — answering what queued up now."


def format_routed_reply(text: str, reason: str = "mouth is off") -> str:
    return f"🔇 ({reason} — answering here instead)\n{text.strip()}"
