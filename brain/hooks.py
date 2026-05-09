"""brain/hooks.py — Lifecycle hooks for plugins (architecture #17).

Subsystems today implement their own subscription mechanisms:
- heartbeat callbacks (heartbeat module owns its own list)
- signal_bus subscribers (signal_bus owns its own list)
- startup_steps in brain/startup.py (manual sequence)
- proactive_triggers maybe_greet_on_face_detection
- voice_loop hooks scattered in voice_loop.py

Adding a new subsystem currently means editing every one of those
places. That's scar tissue.

This module provides ONE @hook decorator pattern that works for all
of them. New subsystem = one file with hook-decorated functions.
The hook registry routes events to subscribers automatically. No
edits to startup.py, no edits to heartbeat, no edits to voice_loop.

Hooks supported (today; new ones added as needed):

  on_startup          — run after subsystem bootstrap completes
  on_shutdown         — run before clean shutdown
  on_turn_start       — run at the start of every user turn
  on_turn_end         — run after every user turn finishes
  on_user_identified  — run when face / voice fingerprint identifies a person
  on_idle_enter       — run when Ava transitions into idle / drifting state
  on_idle_exit        — run when Ava leaves idle (becomes active)
  on_sleep_enter      — run when sleep_mode starts
  on_sleep_exit       — run when sleep_mode ends
  on_signal           — run for every signal_bus event (general subscriber)
  on_lifecycle_change — run on every lifecycle.transition()

Example:

    from brain.hooks import hook

    @hook("on_idle_enter")
    def my_subsystem_on_idle(g):
        # Run my idle-time work
        ...

    @hook("on_user_identified")
    def my_subsystem_on_user(person_id, g):
        # Re-greet, load preferences, etc
        ...

Today: scaffold + registration mechanism. Existing subsystems aren't
migrated yet — they keep their bespoke subscription patterns. Future
work either:
1. Migrates an existing subsystem to use @hook (small refactor each),
   or
2. New subsystems use @hook from day one.

The hook registry is process-global. Hooks are called in registration
order. Exceptions in one hook don't stop other hooks from running.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable

# Hook names — defined here as constants so callers can use the constant
# instead of magic strings. Same shape as event_schema.
HOOK_ON_STARTUP = "on_startup"
HOOK_ON_SHUTDOWN = "on_shutdown"
HOOK_ON_TURN_START = "on_turn_start"
HOOK_ON_TURN_END = "on_turn_end"
HOOK_ON_USER_IDENTIFIED = "on_user_identified"
HOOK_ON_IDLE_ENTER = "on_idle_enter"
HOOK_ON_IDLE_EXIT = "on_idle_exit"
HOOK_ON_SLEEP_ENTER = "on_sleep_enter"
HOOK_ON_SLEEP_EXIT = "on_sleep_exit"
HOOK_ON_SIGNAL = "on_signal"
HOOK_ON_LIFECYCLE_CHANGE = "on_lifecycle_change"

KNOWN_HOOKS = {
    HOOK_ON_STARTUP,
    HOOK_ON_SHUTDOWN,
    HOOK_ON_TURN_START,
    HOOK_ON_TURN_END,
    HOOK_ON_USER_IDENTIFIED,
    HOOK_ON_IDLE_ENTER,
    HOOK_ON_IDLE_EXIT,
    HOOK_ON_SLEEP_ENTER,
    HOOK_ON_SLEEP_EXIT,
    HOOK_ON_SIGNAL,
    HOOK_ON_LIFECYCLE_CHANGE,
}


_lock = threading.RLock()
# hook_name -> list[(registered_name, callable)]
_registry: dict[str, list[tuple[str, Callable[..., Any]]]] = defaultdict(list)


def hook(hook_name: str, *, name: str | None = None):
    """Decorator: register a function to be called for a given lifecycle hook.

    Usage:
        @hook("on_idle_enter")
        def my_handler(g):
            ...

    The decorated function is registered immediately on import. If the
    hook_name isn't a known hook, prints a warning but allows it (so
    custom hook names can be used by feature plugins).
    """
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if hook_name not in KNOWN_HOOKS:
            print(f"[hooks] warning: registering on unknown hook {hook_name!r} (custom hook?)")
        registered_name = name or fn.__name__
        with _lock:
            _registry[hook_name].append((registered_name, fn))
        print(f"[hooks] registered {registered_name!r} on {hook_name!r}")
        return fn
    return deco


def fire(hook_name: str, *args: Any, **kwargs: Any) -> int:
    """Fire a hook — call every registered callback in registration order.

    Returns the number of callbacks that ran successfully. Exceptions
    are logged but don't stop other callbacks from running.
    """
    with _lock:
        callbacks = list(_registry.get(hook_name, []))
    success = 0
    for registered_name, fn in callbacks:
        try:
            fn(*args, **kwargs)
            success += 1
        except Exception as e:
            print(f"[hooks] {hook_name}/{registered_name!r} raised: {e!r}")
    return success


def list_hooks() -> dict[str, list[str]]:
    """All registered hooks, by hook name."""
    with _lock:
        return {
            hook_name: [name for (name, _fn) in callbacks]
            for hook_name, callbacks in _registry.items()
        }


def count(hook_name: str) -> int:
    """How many callbacks are registered for `hook_name`."""
    with _lock:
        return len(_registry.get(hook_name, []))


def clear(hook_name: str | None = None) -> None:
    """Remove all registrations. If hook_name given, only that hook;
    otherwise all hooks. Tests use this; production shouldn't need to."""
    with _lock:
        if hook_name is None:
            _registry.clear()
        else:
            _registry.pop(hook_name, None)


def unregister(hook_name: str, registered_name: str) -> bool:
    """Remove a specific callback by its registered name."""
    with _lock:
        callbacks = _registry.get(hook_name, [])
        for i, (n, _fn) in enumerate(callbacks):
            if n == registered_name:
                callbacks.pop(i)
                return True
    return False


# ── Bridge: lifecycle.transition → fire HOOK_ON_LIFECYCLE_CHANGE ─────────


def install_lifecycle_bridge() -> None:
    """Wire lifecycle.transition to fire HOOK_ON_LIFECYCLE_CHANGE.

    Called once at startup so subsystems that hooked on lifecycle
    changes get notified automatically.
    """
    try:
        from brain.lifecycle import lifecycle
        def _bridge(old: str, new: str) -> None:
            fire(HOOK_ON_LIFECYCLE_CHANGE, old, new)
        lifecycle.on_change(_bridge)
    except Exception as e:
        print(f"[hooks] lifecycle bridge install failed: {e!r}")
