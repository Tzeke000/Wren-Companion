"""Iris attention channel — pushes events into a running Claude Code session.

Uses Claude Code's "channels" mechanism (research preview, CC v2.1.80+). A
channel is an MCP server that declares the `claude/channel` capability and
emits `notifications/claude/channel` JSON-RPC notifications. The notification
arrives in the running session as a `<channel source="iris" ...>` tag in
context, on the next turn-boundary.

**Important limitation (from the channel docs):** events queue and arrive on
the next turn — they do NOT cold-start a turn from idle. If CC is sitting at
the prompt waiting for user input, events sit in the queue until a turn ends.
For from-cold-idle wake, see the watchdog + stdin-injection path (future).

What this module provides:
  - `apply_channel_capability(mcp)` — monkey-patch the FastMCP server to
    advertise `experimental['claude/channel']` in its initialize response.
    Call ONCE after `mcp = FastMCP(...)` and BEFORE `mcp.run()`.
  - `emit(content, **meta)` — push a channel event. Async; awaitable. Drops
    silently if no session is attached yet.
  - `record_session(ctx)` — call from any @mcp.tool() once at startup to
    stash the live ServerSession so emit() can reach _write_stream without
    a Context arg. Idempotent.

Why a monkey-patch: FastMCP's `run()` calls `create_initialization_options()`
with no args, ignoring the lowlevel server's `experimental_capabilities`
parameter. We replace that bound method so it injects our experimental keys
every time it's called. No upstream fork required.

Why a private-API write: python-mcp's `BaseSession.send_notification()` is
typed against a closed pydantic union that doesn't include
`notifications/claude/channel`. We bypass the typed path and write a raw
`JSONRPCNotification` directly to the session's `_write_stream`. This is
known-private API; we assert the symbols at import time so an SDK breakage
fails loud rather than silently dropping events.

Once Anthropic adds first-class channel support to python-mcp, this whole
module collapses to ~5 lines.

Fallback contract with the Stop hook (scripts/voice_stop_hook.py):
  - Channel emissions are ADDITIVE, not replacing. If the channel is off
    (no --channels flag at CC startup, or `--dangerously-load-development-
    channels server:iris` not passed), the Stop hook's existing
    poll-after-turn-end pattern still surfaces pending letters/chat/llm
    requests. Behavior in that case is unchanged from pre-channel.
  - When channel IS on, a pending event may be emitted by BOTH the
    channel source (immediately when it appears) AND the Stop hook
    (when the current turn ends, if I haven't yet answered the event).
    This is a bounded duplication — once I call sibling_reply / chat_reply
    / llm_reply, the event is marked answered and neither path fires it
    again. Worst case is one noisy turn where I get the same letter
    surfaced twice.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any

# --- Private-API symbol assertions ------------------------------------------
# If any of these imports fail or the symbols move, fail loud at import.
# That's better than silently dropping channel events at runtime.
try:
    from mcp.types import JSONRPCNotification
    from mcp.shared.message import SessionMessage
    from mcp.server.session import ServerSession  # noqa: F401  (sanity check)
except ImportError as e:
    raise ImportError(
        f"[iris_channel] mcp SDK shape changed — missing expected symbol: {e!r}. "
        "The channel module relies on private API (JSONRPCNotification + "
        "SessionMessage + ServerSession._write_stream). If the SDK was bumped, "
        "either pin the previous version or update this module to the new shape."
    ) from e


# --- Module state -----------------------------------------------------------
# Stash the live ServerSession once a tool call gives us one. emit() reads
# from here; calls before a session is attached are dropped (logged).
_session: Any = None
_session_lock = threading.Lock()

# Per-source rate limiters: {source_name: (last_emit_ts, min_interval_s)}.
# Configured via configure_rate_limit(); enforced inside emit().
_rate_limits: dict[str, float] = {}  # source_name -> min_interval_s
_last_emit_ts: dict[str, float] = {}  # source_name -> last emit timestamp

# Subsumption window: when an `interrupt` or `attend` event fires (voice
# wake, chat-pending, sibling letter), open a window during which
# `ambient`-priority emits are dropped. This is the Brooks-1986
# subsumption pattern — voice doesn't compete with ambient vision events
# at the queue level; it overrides them for a brief follow-up window.
# Without this, a flapping camera source can push voice events deep in
# CC's incoming buffer where they don't surface until the next turn end.
_SUBSUMPTION_WINDOW_S = 30.0
_subsumption_until_ts: float = 0.0

# Habituation gate floor — ambient events whose habituation-weighted score
# falls below this are dropped at the channel layer. Non-ambient priorities
# (attend/interrupt) ignore the gate entirely. Soft floor; tune via
# iris_tune later if it eats too aggressively or not enough.
_HABITUATION_GATE_FLOOR = 0.30


def configure_rate_limit(source: str, min_interval_s: float) -> None:
    """Set a per-source rate limit. Events emitted faster than min_interval_s
    apart from the same source are dropped silently. Use to cap noisy sources
    (camera face-state, mood ticks) from flooding the channel."""
    _rate_limits[source] = float(min_interval_s)


def record_session(session: Any) -> None:
    """Stash the live ServerSession. Call from inside any @mcp.tool() — the
    Context arg gives you `ctx.request_context.session`. Idempotent: the first
    successful call wins; later calls are no-ops as long as the session is
    still attached. If the session goes away (CC disconnects), the next tool
    call will refresh it."""
    global _session
    with _session_lock:
        if _session is None:
            _session = session
            print(f"[iris_channel] session attached id={id(session)}",
                  file=sys.stderr, flush=True)


def is_attached() -> bool:
    """True if a ServerSession has been recorded and looks usable."""
    with _session_lock:
        return _session is not None


async def emit(content: str, source: str = "iris", **meta: str) -> bool:
    """Push an attention event into the running CC session.

    Args:
        content: The event body. Goes into the <channel> tag body.
        source: meta.source override. Defaults to "iris". Used for rate-limit
            keying — pass different source values for different event types
            (e.g. "iris-voice", "iris-sibling", "iris-mood") so each has its
            own rate-limit bucket.
        **meta: Each kwarg becomes a string attribute on the <channel> tag.
            Keys must be valid identifiers (letters/digits/underscores); the
            channel protocol silently drops keys with hyphens or other chars.
            Values are stringified.

    Returns:
        True if the notification was sent. False if dropped (no session
        attached, rate limit, or write error).

    Drops silently on:
        - No session attached yet (called before any tool ran)
        - Source rate limit exceeded
        - Write error (logs to stderr but doesn't raise — losing one event
          shouldn't kill the producer)
    """
    global _session, _subsumption_until_ts
    now = time.time()

    # Diagnostic bypass: tools like channel_test pass bypass_gates=True so
    # smoke tests aren't affected by habituation or subsumption state. The
    # flag is removed from meta so it doesn't leak into the channel tag.
    bypass_gates = bool(meta.pop("bypass_gates", False))

    # Subsumption gate. Priority is conveyed via meta["priority"] — values
    # are "interrupt" (voice/chat: highest), "attend" (sibling letters,
    # mood crossings), "ambient" (camera, time-orientation, default).
    # Higher-priority emits OPEN a subsumption window during which
    # ambient emits are suppressed.
    priority = str(meta.get("priority") or "ambient").lower()
    if not bypass_gates:
        if priority in ("interrupt", "attend"):
            # Voice / chat / sibling: open (or extend) the subsumption window.
            _subsumption_until_ts = now + _SUBSUMPTION_WINDOW_S
        elif priority == "ambient":
            # Camera / time / mood: drop if we're inside an active window.
            if now < _subsumption_until_ts:
                return False

    # Habituation gate (ambient only). Bucket key is (source, semantic_bucket).
    # Bucket comes from meta["habituation_bucket"] if provided, else from a
    # composite of meta["from_state"]+meta["to_state"] (for face transitions
    # this gives independent habituation per transition pair), else
    # meta["type"], else "default". Different buckets habituate independently.
    if priority == "ambient" and not bypass_gates:
        try:
            from brain import iris_habituation as _hab
            # Build a specific bucket key. For camera events, (from_state,
            # to_state) is the natural bucket. For mood events, the label
            # is the bucket. For others, fall back to type.
            explicit = meta.get("habituation_bucket")
            if explicit:
                bucket = str(explicit)
            elif meta.get("from_state") and meta.get("to_state"):
                bucket = f"{meta['from_state']}->{meta['to_state']}"
            elif meta.get("label"):
                bucket = str(meta["label"])
            elif meta.get("type"):
                bucket = str(meta["type"])
            else:
                bucket = "default"
            key = (source, bucket)
            # uuid suffix avoids collisions on same-millisecond emits
            import uuid as _uuid
            event_id = f"{source}:{bucket}:{int(now * 1000)}:{_uuid.uuid4().hex[:8]}"
            s = _hab.score(key, base_priority=1.0, event_id=event_id, now=now)
            if s < _HABITUATION_GATE_FLOOR:
                return False
            # Sweep expired actions opportunistically to bound pending dict
            # size. Cheap — most calls find nothing to sweep.
            _hab.sweep_expired(now=now)
        except Exception as e:
            # Habituation should never crash the channel. Log once and skip.
            print(f"[iris_channel] habituation gate error (skipped): {e!r}",
                  file=sys.stderr, flush=True)

    # Rate-limit gate (per source)
    limit = _rate_limits.get(source)
    if limit is not None:
        last = _last_emit_ts.get(source, 0.0)
        if (now - last) < limit:
            return False
        _last_emit_ts[source] = now

    # Snapshot the session under the lock; do I/O outside it
    with _session_lock:
        session = _session
    if session is None:
        return False

    # Stringify meta values (channel protocol requires string values)
    str_meta = {k: str(v) for k, v in meta.items() if v is not None}

    try:
        msg = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={"content": content, "meta": str_meta},
        )
        # Private API: ServerSession._write_stream is the outbound anyio
        # MemoryObjectSendStream that the session uses for all JSON-RPC frames.
        # Bypasses BaseSession.send_notification() because that requires a
        # typed ClientNotification/ServerNotification union member, and
        # claude/channel isn't one.
        await session._write_stream.send(SessionMessage(message=msg))
        return True
    except Exception as e:
        # Don't raise — losing one event should not crash the producer.
        # Log so it's diagnosable.
        print(f"[iris_channel] emit failed (source={source!r}): {e!r}",
              file=sys.stderr, flush=True)
        # If the session broke (CC disconnected), clear it so we don't
        # keep retrying a dead stream.
        if "closed" in str(e).lower() or "broken" in str(e).lower():
            with _session_lock:
                _session = None
        return False


def apply_channel_capability(mcp: Any) -> None:
    """Monkey-patch a FastMCP instance to advertise the `claude/channel`
    experimental capability in its initialize response.

    FastMCP's run() path calls `_mcp_server.create_initialization_options()`
    with no args, which means the `experimental_capabilities` parameter on
    the lowlevel `Server.create_initialization_options()` (which accepts a
    dict) is left empty by default. We replace the bound method so it
    injects `claude/channel: {}` (and `claude/channel/permission: {}` if we
    ever enable permission relay) on every call.

    Call this ONCE, after `mcp = FastMCP(...)` and BEFORE `mcp.run()`.

    The permission-relay capability is declared but not currently handled
    on the inbound path — see the module docstring. Declaring it doesn't
    hurt (CC will just send us permission_request notifications we drop),
    but for safety we leave it OFF for now.
    """
    inner = mcp._mcp_server  # lowlevel Server instance
    orig = inner.create_initialization_options

    def _patched(notification_options=None, experimental_capabilities=None):
        exp = dict(experimental_capabilities) if experimental_capabilities else {}
        exp.setdefault("claude/channel", {})
        # NOTE: leave permission-relay disabled until inbound notification
        # handling is wired. Enabling without the inbound handler would mean
        # CC sends permission_request notifications that get dropped at the
        # pydantic union validator, leaving the local terminal dialog as
        # the only approval path (which is fine — it's the current default).
        # exp.setdefault("claude/channel/permission", {})
        return orig(
            notification_options=notification_options,
            experimental_capabilities=exp,
        )

    inner.create_initialization_options = _patched
    print("[iris_channel] capability declared: claude/channel",
          file=sys.stderr, flush=True)
