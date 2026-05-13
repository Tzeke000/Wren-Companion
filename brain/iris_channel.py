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
    global _session
    # Rate-limit gate (per source)
    limit = _rate_limits.get(source)
    if limit is not None:
        now = time.time()
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
