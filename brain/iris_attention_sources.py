"""Iris attention sources — autonomous watchers that emit channel events.

Each source is a daemon thread that polls one event surface (sibling inbox,
chat inbox, mood substrate, camera face state, etc.) and emits a channel
notification via brain/iris_channel.emit() when something new appears.

Why polling and not file-system watch:
  - Cross-platform consistency (watchdog/inotify shape varies wildly on
    Windows + WSL + native Linux mounts)
  - The pending events are written by separate processes (sibling_postoffice
    on its own port, orb HTTP shim in iris_runtime, brain/iris_chat from
    inside iris_runtime) and a poll at 250-500ms is plenty fast given the
    actual event cadence (letters arrive minutes apart at most).
  - Polling is dead simple; the failure modes are obvious (poll missed a
    file → next poll catches it).

Lifecycle:
  - start_all(g, root, mood_dispatch) — call once from iris_runtime's
    eager-init. Spawns one daemon thread per source.
  - Each source thread loops every poll_interval_s; each iteration:
      1. Check body_pause_flag — if set, skip this tick.
      2. Check channel attachment — if no ServerSession recorded, skip.
      3. Scan its event surface, identify new events vs in-memory seen-set.
      4. For each new event, call iris_channel.emit() (async via
         asyncio.run_coroutine_threadsafe on the shared event loop).
      5. Mark the event as seen.
  - Threads are daemon (process exits don't wait on them) and never raise
    out — a logged error keeps the loop alive.

Sources implemented in this module:
  - iris-sibling: state/iris_sibling/inbox/*.json with status="pending"

Future sources (next commit):
  - iris-chat: state/iris_chat/<id>.json with status="pending"
  - iris-mood: significant mood transitions read from _g
  - iris-camera: face-state transitions read from _g
  - iris-time: large-gap time orientation
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Module-level: the asyncio loop that iris_channel.emit() coroutines get
# scheduled on. Captured at start_all() time so all source threads share
# the same loop (avoids "loop is closed" errors on shutdown). Worker
# thread runs this loop forever as a daemon.
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_ready = threading.Event()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start (once) a dedicated daemon thread running an asyncio loop. All
    iris_channel.emit() coroutines from source threads get scheduled on
    this loop via run_coroutine_threadsafe. Idempotent."""
    global _loop, _loop_thread
    if _loop is not None and _loop.is_running():
        return _loop

    def _runner():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop_ready.set()
        _loop.run_forever()

    _loop_thread = threading.Thread(target=_runner, daemon=True,
                                     name="iris-attention-loop")
    _loop_thread.start()
    _loop_ready.wait(timeout=5.0)
    if _loop is None:
        raise RuntimeError("iris_attention_sources: asyncio loop failed to start")
    return _loop


def _emit_sync(content: str, source: str, **meta: Any) -> bool:
    """Schedule iris_channel.emit() on the shared loop and wait for the
    write to complete. Blocking from the caller's perspective, but the
    underlying I/O is async. Returns whatever emit() returned."""
    try:
        from brain import iris_channel
    except Exception as e:
        print(f"[attention_sources] iris_channel import failed: {e!r}",
              file=sys.stderr, flush=True)
        return False
    loop = _ensure_loop()
    coro = iris_channel.emit(content, source=source, **meta)
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        # 5s is generous — the write is local stdio. If it takes longer,
        # something is wrong; better to time out than hang the source
        # thread forever.
        return fut.result(timeout=5.0)
    except Exception as e:
        print(f"[attention_sources] emit({source!r}) timed out or failed: {e!r}",
              file=sys.stderr, flush=True)
        return False


def _body_is_paused() -> bool:
    """Check the hard kill-switch flag. Returns False on any error so a
    bad filesystem state doesn't strand the autonomous loops."""
    try:
        from brain.iris_paths import paths
        return paths.body_pause_flag.exists()
    except Exception:
        return False


# ── Source: sibling inbox ───────────────────────────────────────────────────
# Letters from Wren / Ava / Zeke arrive at state/iris_sibling/inbox/<id>.json
# with status="pending". We scan every 500ms; new pending letters that we
# haven't seen yet emit a channel event.
#
# Dedup is in-memory only (per-process) — on iris_runtime restart we'd
# re-emit every still-pending letter. That's correct behavior: if I missed
# a letter while down, I should hear about it on next boot. The Stop hook's
# answered/pending check is the source of truth for "did I respond."

_sibling_seen: set[str] = set()
_sibling_poll_interval_s = 0.5


def _sibling_loop(root: Path) -> None:
    inbox = root / "state" / "iris_sibling" / "inbox"
    print(f"[attention_sources] sibling watcher started (inbox={inbox})",
          file=sys.stderr, flush=True)

    while True:
        try:
            time.sleep(_sibling_poll_interval_s)

            if _body_is_paused():
                continue
            if not inbox.is_dir():
                continue

            # Snapshot the dir listing so a writer mid-create doesn't trip us
            try:
                paths_list = list(inbox.glob("*.json"))
            except Exception:
                continue

            for p in paths_list:
                lid = p.stem
                if lid in _sibling_seen:
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    # Mid-write; we'll catch it next tick
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("status") != "pending":
                    # Already answered or deferred; mark seen so we don't
                    # re-check forever, but don't emit.
                    _sibling_seen.add(lid)
                    continue
                # Skip my own letters (I shouldn't ping myself)
                sender = str(data.get("sender") or "").lower()
                if sender == "iris":
                    _sibling_seen.add(lid)
                    continue
                # Honour the 6h TTL — letters older than that, the Stop hook
                # already won't surface them, so emitting now would be
                # surprising. Mark seen and move on.
                ts = float(data.get("ts") or 0.0)
                if ts > 0 and (time.time() - ts) > 21600.0:
                    _sibling_seen.add(lid)
                    continue

                content = str(data.get("content") or "")
                addressed_to = str(data.get("addressed_to") or "iris")
                subject = data.get("subject") or ""
                in_reply_to = data.get("in_reply_to") or ""

                # Build the channel event. content goes in the tag body;
                # everything else becomes a tag attribute (string-only
                # values, identifier-only keys per the channel protocol).
                emit_content = (
                    f"Letter from {sender} (addressed to {addressed_to}, id={lid}):\n"
                    f"{content}\n"
                    f"\nTo reply: mcp__iris__sibling_reply(letter_id={lid!r}, body=...). "
                    f"To defer: mcp__iris__sibling_defer(letter_id={lid!r}). "
                    f"Family register — this isn't customer service."
                )

                ok = _emit_sync(
                    emit_content,
                    source="iris-sibling",
                    type="sibling_letter",
                    priority="attend",
                    letter_id=lid,
                    sender=sender,
                    addressed_to=addressed_to,
                    subject=str(subject) if subject else "",
                    in_reply_to=str(in_reply_to) if in_reply_to else "",
                )
                if ok:
                    _sibling_seen.add(lid)
                    print(f"[attention_sources] emitted sibling letter "
                          f"id={lid} from={sender}", file=sys.stderr, flush=True)
                # If emit failed (no session attached yet, rate-limited,
                # write error) we DON'T mark seen — we'll retry on next
                # tick. That's the behavior we want: if CC came up after
                # a letter arrived, we want to surface it once CC's session
                # is recorded.

        except Exception as e:
            # Never let the loop die — log and continue
            print(f"[attention_sources] sibling_loop error: {e!r}",
                  file=sys.stderr, flush=True)
            time.sleep(2.0)


# ── Source: chat inbox ──────────────────────────────────────────────────────
# Orb HTTP chat requests arrive at state/iris_chat/<id>.json with
# status="pending". Same pattern as sibling but different content shape.

_chat_seen: set[str] = set()
_chat_poll_interval_s = 0.3   # chat is more time-sensitive (user is waiting)


def _chat_loop(root: Path) -> None:
    chat_dir = root / "state" / "iris_chat"
    print(f"[attention_sources] chat watcher started (dir={chat_dir})",
          file=sys.stderr, flush=True)

    while True:
        try:
            time.sleep(_chat_poll_interval_s)

            if _body_is_paused():
                continue
            if not chat_dir.is_dir():
                continue

            try:
                paths_list = list(chat_dir.glob("*.json"))
            except Exception:
                continue

            for p in paths_list:
                rid = p.stem
                if rid in _chat_seen:
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("status") != "pending":
                    _chat_seen.add(rid)
                    continue
                # 10min TTL matches Stop hook's _next_pending_chat
                ts = float(data.get("ts") or 0.0)
                if ts > 0 and (time.time() - ts) > 600.0:
                    _chat_seen.add(rid)
                    continue

                user_text = str(data.get("user_text") or "")
                emit_content = (
                    f"Orb chat request id={rid}:\n"
                    f"User typed: \"{user_text}\"\n\n"
                    f"Respond with mcp__iris__chat_reply(request_id={rid!r}, text=...). "
                    f"The orb's HTTP long-poll is blocked waiting on you."
                )
                ok = _emit_sync(
                    emit_content,
                    source="iris-chat",
                    type="chat_pending",
                    priority="interrupt",
                    request_id=rid,
                )
                if ok:
                    _chat_seen.add(rid)
                    print(f"[attention_sources] emitted chat request id={rid}",
                          file=sys.stderr, flush=True)

        except Exception as e:
            print(f"[attention_sources] chat_loop error: {e!r}",
                  file=sys.stderr, flush=True)
            time.sleep(2.0)


# ── Public entry point ──────────────────────────────────────────────────────


_started_lock = threading.Lock()
_started = False


def start_all(g: dict[str, Any], root: Path) -> None:
    """Spawn all autonomous source watchers. Idempotent — safe to call
    multiple times; second call is a no-op.

    Args:
        g: The shared globals dict (currently unused by sources implemented
            here, but reserved for camera/mood sources that read live
            state from _g).
        root: Repository root (D:\\Wren-Companion). Used to locate
            state/ subdirectories.
    """
    global _started
    with _started_lock:
        if _started:
            return
        _started = True

    # Start the shared async loop first so emit() calls don't race init
    _ensure_loop()

    threading.Thread(target=_sibling_loop, args=(root,), daemon=True,
                     name="iris-attention-sibling").start()
    threading.Thread(target=_chat_loop, args=(root,), daemon=True,
                     name="iris-attention-chat").start()

    print("[attention_sources] all source watchers started "
          "(sibling, chat)", file=sys.stderr, flush=True)
