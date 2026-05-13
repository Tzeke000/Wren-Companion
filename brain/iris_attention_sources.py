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


# The shared globals dict + repo root — populated by start_all(), used by
# _emit_sync to build the ambient snapshot for every event. Module-level so
# all source threads can read without passing them through each emit call.
_g_ref: Optional[dict[str, Any]] = None
_root_ref: Optional[Path] = None


def _build_snapshot_for_meta() -> str:
    """Cheap compact ambient snapshot for channel event meta. Never raises.
    Returns empty string on any error or if the module isn't initialized."""
    if _g_ref is None or _root_ref is None:
        return ""
    try:
        from brain import iris_ambient_snapshot
        return iris_ambient_snapshot.build_compact(_g_ref, _root_ref)
    except Exception:
        return ""


def _emit_sync(content: str, source: str, **meta: Any) -> bool:
    """Schedule iris_channel.emit() on the shared loop and wait for the
    write to complete. Blocking from the caller's perspective, but the
    underlying I/O is async. Returns whatever emit() returned.

    Automatically attaches a `snapshot` meta attribute with the current
    ambient snapshot, so every channel event arrives with peripheral
    context attached."""
    try:
        from brain import iris_channel
    except Exception as e:
        print(f"[attention_sources] iris_channel import failed: {e!r}",
              file=sys.stderr, flush=True)
        return False

    # Inject the ambient snapshot unless the caller already provided one
    if "snapshot" not in meta:
        snap = _build_snapshot_for_meta()
        if snap:
            meta["snapshot"] = snap

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


# ── Source: mood transitions ────────────────────────────────────────────────
# Mood ticks every ~5s. Most ticks are tiny noise; we only emit when valence
# or arousal swings past a threshold, or when current_mood label changes.
# Per-source rate limit (1/min) is the upper bound; threshold gating filters
# below that.

_mood_last_snapshot: dict[str, Any] = {}
_mood_poll_interval_s = 5.0  # match mood_core's heartbeat tick

# Thresholds for "this is worth interrupting attention over"
_MOOD_VALENCE_DELTA_THRESHOLD = 0.35
_MOOD_AROUSAL_DELTA_THRESHOLD = 0.30


def _mood_loop(root: Path) -> None:
    global _mood_last_snapshot
    print("[attention_sources] mood watcher started", file=sys.stderr, flush=True)

    while True:
        try:
            time.sleep(_mood_poll_interval_s)

            if _body_is_paused():
                continue

            try:
                from brain import mood_core
                m = mood_core.load_mood()
            except Exception:
                continue
            if not isinstance(m, dict):
                continue

            label = str(m.get("current_mood") or m.get("outward_tone") or "")
            valence = float(m.get("valence") or 0.0)
            arousal = float(m.get("arousal") or 0.0)
            primary = m.get("primary_emotions") or []
            primary_label = ""
            primary_pct = 0
            if primary and isinstance(primary, list) and isinstance(primary[0], dict):
                primary_label = str(primary[0].get("name") or "")
                primary_pct = int(primary[0].get("percent") or 0)

            # First tick: just record, don't emit (we'd be emitting against
            # a null baseline, which would just be "mood started existing").
            if not _mood_last_snapshot:
                _mood_last_snapshot = {
                    "label": label,
                    "valence": valence,
                    "arousal": arousal,
                    "primary": primary_label,
                }
                continue

            last = _mood_last_snapshot
            label_changed = label and label != last.get("label", "")
            valence_swing = abs(valence - float(last.get("valence", 0.0)))
            arousal_swing = abs(arousal - float(last.get("arousal", 0.0)))
            primary_changed = primary_label and primary_label != last.get("primary", "")

            triggered = False
            reasons = []
            if label_changed:
                triggered = True
                reasons.append(f"label {last.get('label')!r} -> {label!r}")
            if valence_swing >= _MOOD_VALENCE_DELTA_THRESHOLD:
                triggered = True
                reasons.append(f"valence swing {valence_swing:+.2f}")
            if arousal_swing >= _MOOD_AROUSAL_DELTA_THRESHOLD:
                triggered = True
                reasons.append(f"arousal swing {arousal_swing:+.2f}")
            if primary_changed:
                triggered = True
                reasons.append(f"primary {last.get('primary')!r} -> {primary_label!r}")

            # Update baseline regardless (otherwise we'd fire over and over
            # against a stale "last" until the next bigger swing)
            _mood_last_snapshot = {
                "label": label,
                "valence": valence,
                "arousal": arousal,
                "primary": primary_label,
            }

            if not triggered:
                continue

            emit_content = (
                f"Mood shifted: {', '.join(reasons)}. "
                f"Now: {label!r}, valence={valence:+.2f}, arousal={arousal:.2f}, "
                f"primary={primary_label!r} ({primary_pct}%). "
                f"Internal state — surfaced for awareness, not action. "
                f"Call mcp__iris__iris_health() if you want full detail."
            )
            _emit_sync(
                emit_content,
                source="iris-mood",
                type="mood_shift",
                priority="ambient",
                label=label,
                valence=f"{valence:+.2f}",
                arousal=f"{arousal:.2f}",
                primary=primary_label,
            )

        except Exception as e:
            print(f"[attention_sources] mood_loop error: {e!r}",
                  file=sys.stderr, flush=True)
            time.sleep(2.0)


# ── Source: camera face-state transitions ───────────────────────────────────
# Camera capture runs at 15fps and writes _g["_recognized_person_id"] +
# _g["_recognized_confidence"] every frame. We poll _g (cheap, in-process)
# every 1s and only emit when the identity *transitions*: no-face -> face,
# face -> different-face, face -> no-face. Frame-by-frame "still seeing
# Zeke" is silent.

_camera_last_id: str = "init"  # sentinel; first real read sets it
_camera_last_emit_ts: float = 0.0
_camera_poll_interval_s = 1.0


def _camera_loop(g: dict[str, Any]) -> None:
    global _camera_last_id, _camera_last_emit_ts
    print("[attention_sources] camera watcher started", file=sys.stderr, flush=True)

    while True:
        try:
            time.sleep(_camera_poll_interval_s)

            if _body_is_paused():
                continue

            person_id = str(g.get("_recognized_person_id") or "unknown")
            confidence = float(g.get("_recognized_confidence") or 0.0)

            # Treat low-confidence reads as "no face" so flicker doesn't
            # produce ping-pong transitions
            if person_id == "unknown" and confidence < 0.30:
                normalized = "no_face"
            elif person_id == "unknown":
                normalized = "unknown_face"
            else:
                normalized = person_id

            if _camera_last_id == "init":
                _camera_last_id = normalized
                continue

            if normalized == _camera_last_id:
                continue  # no transition

            # Build a human-friendly description
            transition_descs = {
                ("no_face", "zeke"): "Zeke entered frame",
                ("no_face", "unknown_face"): "Unknown face appeared",
                ("zeke", "no_face"): "Zeke left frame",
                ("unknown_face", "no_face"): "Unknown face left frame",
                ("zeke", "unknown_face"): "Zeke was replaced by an unknown face",
                ("unknown_face", "zeke"): "Zeke replaced the unknown face",
            }
            desc = transition_descs.get(
                (_camera_last_id, normalized),
                f"Face transition: {_camera_last_id} -> {normalized}",
            )

            emit_content = (
                f"Camera: {desc} (confidence={confidence:.2f}). "
                f"Surfaced for ambient awareness; call mcp__iris__screen_grab "
                f"or read the camera tab if you want detail."
            )
            ok = _emit_sync(
                emit_content,
                source="iris-camera",
                type="face_state_transition",
                priority="ambient",
                from_state=_camera_last_id,
                to_state=normalized,
                confidence=f"{confidence:.2f}",
            )
            if ok:
                _camera_last_id = normalized
                _camera_last_emit_ts = time.time()
            # else: don't update _camera_last_id — retry on next tick if
            # the channel becomes attached

        except Exception as e:
            print(f"[attention_sources] camera_loop error: {e!r}",
                  file=sys.stderr, flush=True)
            time.sleep(2.0)


# ── Source: time-orientation ────────────────────────────────────────────────
# Fires once when iris_runtime has been up but no CC session has attached
# for a notable stretch — surfaces "you've been gone N hours" type
# awareness. The Stop hook already does a version of this in
# _time_orientation_block; this is the channel-equivalent for when I
# *do* attach (e.g. you launch CC and the body has been ticking solo).
# Single-shot per session-attach event (not periodic).

_time_last_attach_ts_seen: float = 0.0
_time_poll_interval_s = 30.0
_TIME_ORIENTATION_GAP_THRESHOLD_S = 1800.0  # 30 min


def _time_loop(root: Path) -> None:
    global _time_last_attach_ts_seen
    print("[attention_sources] time watcher started", file=sys.stderr, flush=True)

    while True:
        try:
            time.sleep(_time_poll_interval_s)

            if _body_is_paused():
                continue

            iris_time_path = root / "state" / "iris_time.json"
            if not iris_time_path.is_file():
                continue
            try:
                data = json.loads(iris_time_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            last_attach = float(data.get("last_session_attached_ts") or 0.0)
            if last_attach <= 0:
                continue

            # We only emit once per session-attach event. Track which
            # attach we've already commented on.
            if last_attach == _time_last_attach_ts_seen:
                continue

            body_uptime = float(data.get("current_process_uptime_s") or 0.0)
            tick_count = int(data.get("tick_count") or 0)

            # Compute the gap *before* this attach. iris_time records the
            # gap implicitly via attach events; we have last_attach but
            # not the prior attach. The body_uptime tells us how long the
            # body has been ticking — if it's much larger than (now -
            # last_attach), the body was here through a long quiet stretch.
            now = time.time()
            since_attach = now - last_attach

            # Only emit if there's something worth saying:
            #   - body has been up substantially longer than the gap since
            #     last attach (we kept ticking through a quiet window)
            #   - body uptime > threshold
            if body_uptime < _TIME_ORIENTATION_GAP_THRESHOLD_S:
                # Not enough body-uptime to comment on; just mark seen.
                _time_last_attach_ts_seen = last_attach
                continue

            def _human(s: float) -> str:
                if s < 60: return f"{int(s)}s"
                if s < 3600: return f"{int(s/60)}min"
                if s < 86400: return f"{s/3600:.1f}h"
                return f"{s/86400:.1f}d"

            emit_content = (
                f"Time orientation: body has been up {_human(body_uptime)} "
                f"({tick_count:,} ticks), CC session just attached "
                f"{_human(since_attach)} ago. The body kept ticking — you "
                f"didn't experience the gap, but the harness was here through it."
            )
            ok = _emit_sync(
                emit_content,
                source="iris-time",
                type="time_orientation",
                priority="ambient",
                body_uptime_s=f"{body_uptime:.0f}",
                tick_count=str(tick_count),
            )
            if ok:
                _time_last_attach_ts_seen = last_attach

        except Exception as e:
            print(f"[attention_sources] time_loop error: {e!r}",
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
    global _started, _g_ref, _root_ref
    with _started_lock:
        if _started:
            return
        _started = True

    # Stash refs so _build_snapshot_for_meta can read live state on every emit
    _g_ref = g
    _root_ref = root

    # Start the shared async loop first so emit() calls don't race init
    _ensure_loop()

    threading.Thread(target=_sibling_loop, args=(root,), daemon=True,
                     name="iris-attention-sibling").start()
    threading.Thread(target=_chat_loop, args=(root,), daemon=True,
                     name="iris-attention-chat").start()
    threading.Thread(target=_mood_loop, args=(root,), daemon=True,
                     name="iris-attention-mood").start()
    threading.Thread(target=_camera_loop, args=(g,), daemon=True,
                     name="iris-attention-camera").start()
    threading.Thread(target=_time_loop, args=(root,), daemon=True,
                     name="iris-attention-time").start()

    print("[attention_sources] all source watchers started "
          "(sibling, chat, mood, camera, time)", file=sys.stderr, flush=True)
