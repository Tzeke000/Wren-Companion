"""Habituation salience filter — Marsland 2000 / Stanley 1976 substrate.

Generalizes the per-source rate-limit and subsumption window in
brain/iris_channel.py into a principled attention model. Each event class
has a habituation value y in [floor, 1.0]:
  - 1.0 = fully novel; emits pass through at full priority.
  - floor = fully habituated; emits dampened but never silenced.
  - Between events, y recovers exponentially toward 1.0 with time constant
    T_recover (per source).
  - On every emit of the same class, y is knocked down by gamma.
  - If the cognition acts on an event (calls a tool tagged with it) within
    act_window seconds, the source's acted_ratio rises; events from sources
    with high acted_ratio habituate LESS (because acting is itself
    dishabituating). Events from sources with low acted_ratio habituate
    MORE aggressively.

Why this exists:
  - Hysteresis fixed flap at the camera source level.
  - Subsumption fixed cross-source competition during voice events.
  - Habituation fixes the third-identical-event-in-10-minutes problem
    that neither of the above touches. The mood ticking to "interest"
    is interesting once; the third time it does so without me acting,
    it should fade.

Operationally:
  - Caller decides the event_class key. Recommended scheme is a tuple
    (source, semantic_bucket) — e.g. ("iris-mood", "interest"),
    ("iris-camera", "zeke_returned"). Different buckets habituate
    independently.
  - Caller decides whether to drop the event entirely or just attach the
    score as meta. Current callers in iris_channel use it as a soft gate:
    emit_score < HABITUATION_GATE_FLOOR is dropped at the channel layer.
  - context_shift("iris-mood") fully resets all mood keys, e.g. when a
    new conversation starts after >30min idle.

Per-source defaults (sane starting points; tune via iris_tune later):
  iris-camera:  T_recover=180s, gamma=0.55
  iris-mood:    T_recover=120s, gamma=0.60
  iris-time:    T_recover=600s, gamma=0.50
  iris-sibling: T_recover=300s, gamma=0.70 (recovers fast — letters are
                                            often acted on; gamma higher
                                            means less knockdown)
  iris-chat:    T_recover=60s,  gamma=0.85 (almost no habituation —
                                            user-driven, every event
                                            matters)

Anti-patterns explicitly avoided:
  - Habituation key never includes identity (zeke/wren/unknown) — habituate
    on "face transition" as a class, not on "seeing Zeke". Otherwise the
    owner walking in after 4 hours would be ignored.
  - Hard threshold replaced by soft floor: y is bounded below by 0.15 so
    high-priority events still leak through.
  - Per-source time constants (not a single global tau).
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from typing import Any


# Per-source parameters. Caller can override via configure().
_DEFAULTS: dict[str, dict[str, float]] = {
    "iris-camera":  {"T_recover": 180.0, "gamma": 0.55},
    "iris-mood":    {"T_recover": 120.0, "gamma": 0.60},
    "iris-time":    {"T_recover": 600.0, "gamma": 0.50},
    "iris-sibling": {"T_recover": 300.0, "gamma": 0.70},
    "iris-chat":    {"T_recover": 60.0,  "gamma": 0.85},
}
_FALLBACK = {"T_recover": 120.0, "gamma": 0.60}

_FLOOR = 0.15          # soft floor — y never drops below this
_ACT_WINDOW_S = 60.0   # time after emit during which a tool call counts as "acted"
_ACT_DECAY_PER_HOUR = 0.95  # both acted/ignored counters decay so old behavior fades


_lock = threading.Lock()
# Habituation state: y[key] -> current habituation value in [floor, 1.0]
_y: dict[tuple, float] = defaultdict(lambda: 1.0)
# Last emit timestamp per key (for recovery integration)
_last: dict[tuple, float] = {}
# Acted/ignored counters per source (Laplace-smoothed)
_acted: dict[str, list[float]] = defaultdict(lambda: [1.0, 1.0])  # [acted+1, ignored+1]
_acted_last_decay: dict[str, float] = {}
# Pending emits awaiting action signal: event_id -> (source, deadline)
_pending_action: dict[str, tuple[str, float]] = {}


def configure(source: str, *, T_recover: float | None = None,
              gamma: float | None = None) -> None:
    """Override defaults for a source. Idempotent."""
    with _lock:
        cur = _DEFAULTS.setdefault(source, dict(_FALLBACK))
        if T_recover is not None:
            cur["T_recover"] = float(T_recover)
        if gamma is not None:
            cur["gamma"] = float(gamma)


def _params(source: str) -> tuple[float, float]:
    cfg = _DEFAULTS.get(source, _FALLBACK)
    return float(cfg["T_recover"]), float(cfg["gamma"])


def _decay_acted_counters(source: str, now: float) -> None:
    """Time-decay the acted/ignored counters so old behavior loses weight."""
    last = _acted_last_decay.get(source)
    if last is None:
        _acted_last_decay[source] = now
        return
    dt_hours = (now - last) / 3600.0
    if dt_hours <= 0:
        return
    factor = _ACT_DECAY_PER_HOUR ** dt_hours
    a, i = _acted[source]
    # Decay toward 1.0 (the Laplace prior) so very-stale data converges to neutral
    _acted[source] = [1.0 + (a - 1.0) * factor, 1.0 + (i - 1.0) * factor]
    _acted_last_decay[source] = now


def _recover(key: tuple, now: float) -> None:
    """Integrate y[key] forward from _last[key] to now."""
    last = _last.get(key)
    if last is None:
        _last[key] = now
        return
    dt = now - last
    if dt <= 0:
        return
    source = key[0] if key else ""
    T_recover, _ = _params(source)
    # Exponential recovery toward 1.0:
    #   y(t+dt) = 1.0 - (1.0 - y(t)) * exp(-dt / T_recover)
    y = _y[key]
    y = 1.0 - (1.0 - y) * math.exp(-dt / T_recover)
    _y[key] = max(_FLOOR, min(1.0, y))
    _last[key] = now


def score(key: tuple, base_priority: float = 1.0,
          event_id: str | None = None, now: float | None = None) -> float:
    """Score an incoming event. Side-effects: applies habituation knockdown
    on this key, registers a pending-action entry tied to event_id.

    Returns a multiplicative weight in [floor*0.3, 1.0+]. Caller multiplies
    against base_priority or compares against a gate.

    Args:
        key: tuple identifying the event class. Convention: (source, bucket).
            E.g. ("iris-mood", "interest"), ("iris-camera", "transition").
        base_priority: numeric prior from the source (typically 1.0).
        event_id: optional unique id so the caller can later call
            register_action(event_id) when a tool tied to this event fires.
            If None, this event isn't tracked for action.
        now: timestamp override (test convenience).
    """
    now = now if now is not None else time.time()
    source = key[0] if key else ""

    with _lock:
        _decay_acted_counters(source, now)
        _recover(key, now)
        a, i = _acted[source]
        acted_ratio = a / (a + i)
        # Score combines base, habituation, acted-ratio (lift)
        s = base_priority * max(_y[key], _FLOOR) * (0.4 + 0.6 * acted_ratio)
        # Knockdown: events that get acted on habituate less aggressively.
        T_recover, gamma = _params(source)
        knockdown = gamma + (1.0 - gamma) * acted_ratio
        _y[key] = max(_FLOOR, _y[key] * knockdown)
        _last[key] = now
        if event_id:
            _pending_action[event_id] = (source, now + _ACT_WINDOW_S)
        return s


def register_action(event_id: str, now: float | None = None) -> bool:
    """Mark that the cognition acted on this event. Increments the source's
    acted counter. Returns True if the event was still pending and within
    its action window; False if it had already expired or wasn't tracked.

    `now` is optional and primarily for tests / when the caller already has
    a timestamp matching what was passed to score()."""
    now = now if now is not None else time.time()
    with _lock:
        entry = _pending_action.pop(event_id, None)
        if entry is None:
            return False
        source, deadline = entry
        if now > deadline:
            # Late action — fold it into ignored instead of acted, since the
            # window has closed (this prevents very-late actions from gaming
            # the metric).
            _acted[source][1] += 1.0
            return False
        _acted[source][0] += 1.0
        return True


def sweep_expired(now: float | None = None) -> int:
    """Move expired pending actions into the ignored count. Call from a
    timer / periodic sweep. Returns count of swept entries."""
    now = now if now is not None else time.time()
    swept = 0
    with _lock:
        for eid, (source, deadline) in list(_pending_action.items()):
            if now > deadline:
                _acted[source][1] += 1.0
                _pending_action.pop(eid)
                swept += 1
    return swept


def context_shift(source: str) -> int:
    """Reset all habituation values for the given source back to 1.0.
    Call when context changes meaningfully (long idle gap, new
    conversation, owner returns after >30min). Returns count of keys reset."""
    reset = 0
    with _lock:
        for key in list(_y.keys()):
            if key and key[0] == source:
                _y[key] = 1.0
                reset += 1
    return reset


def snapshot() -> dict[str, Any]:
    """Diagnostic readout. Returns a dict suitable for iris_health-style
    introspection."""
    with _lock:
        return {
            "keys": {
                "::".join(str(p) for p in k): round(v, 3)
                for k, v in _y.items()
            },
            "acted_ratio": {
                src: round(a / (a + i), 3)
                for src, (a, i) in _acted.items()
            },
            "pending_action_count": len(_pending_action),
        }
