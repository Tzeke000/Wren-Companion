"""brain/temporal_sense.py — Temporal substrate for Ava (B3, 2026-05-03).

See docs/TEMPORAL_SENSE.md for the framework. This module implements the
fast-check side of the two-cadence architecture (§2 of that doc):

- run_fast_check_tick(g, now=None): apply state decay/growth + scan
  active estimates for overrun. Called from brain/heartbeat.py.
- track_estimate / update_confidence / resolve_estimate: the estimate
  lifecycle API used by callers (e.g. brain/restart_handoff.py).
- is_idle(g) / processing_active(g): derived signals consumed by other
  modules.

State files:
  state/active_estimates.json    — currently-tracked estimates
  state/task_history_log.jsonl   — completed estimate rows for calibration
  config/temporal_sense.json     — tunables (loaded once, cached)

Performance budget: the fast-check tick must be cheap. No LLM calls,
no blocking I/O, no Ollama lock contention. Disk writes happen at
estimate-resolution time only, not per-tick.

Personhood-frame note (per CONTINUOUS_INTERIORITY.md): comments in
this module describe what's testable engineering. When a docstring
says "frustration cools off when idle," that's framing language for
what the formula does — it doesn't claim subjective experience.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


# ── Config loading (cached) ────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "temporal_sense.json"
_CONFIG_CACHE: dict[str, Any] | None = None
_CONFIG_LOCK = threading.Lock()


def _load_config() -> dict[str, Any]:
    """Load + cache temporal_sense.json. Reload on file change is not
    automatic — restart Ava to pick up tuning changes."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is not None:
            return _CONFIG_CACHE
        try:
            _CONFIG_CACHE = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[temporal_sense] config load error: {e!r} — using empty defaults")
            _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def _cfg(*keys: str, default: Any = None) -> Any:
    """Walk _CONFIG dict by key path. Returns default if any key missing."""
    cur: Any = _load_config()
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ── Derived signals ────────────────────────────────────────────────────────


def processing_active(g: dict[str, Any]) -> bool:
    """Is Ava doing meaningful internal work right now?

    Derived (not stored). TRUE if any of these hold:
      - _turn_in_progress (set by run_ava entry/exit)
      - dual-brain Stream B is busy or has queue depth > 0
      - dual-brain live_thinking_active
      - a self-interrupt-tracked task is running (active estimates non-empty)
      - sleep-mode dream-phase running (future hook; checks _sleep_dream_active)
    """
    if bool(g.get("_turn_in_progress")):
        return True
    db = g.get("_dual_brain") or g.get("dual_brain")
    if db is not None:
        try:
            if bool(getattr(db, "background_busy", False)):
                return True
            if int(getattr(db, "background_queue_depth", 0) or 0) > 0:
                return True
            if bool(getattr(db, "live_thinking_active", False)):
                return True
        except Exception:
            pass
    if _has_active_tracked_task(g):
        return True
    if bool(g.get("_sleep_dream_active")):  # future hook
        return True
    return False


def is_idle(g: dict[str, Any]) -> bool:
    """Three-and gate for true idle (per docs/TEMPORAL_SENSE.md §5).

    Requires:
      1. processing_active == False
      2. (now - _last_user_interaction_ts) > idle_threshold_seconds (default 1800)
      3. voice loop is in passive or attentive (not listening/thinking/speaking)
    """
    if processing_active(g):
        return False
    last = float(g.get("_last_user_interaction_ts") or 0.0)
    if last <= 0:
        # Never interacted yet — count as idle once enough time has passed
        # since process start. Use process start time if available.
        last = float(g.get("_process_start_ts") or time.time())
    elapsed = time.time() - last
    threshold = float(_cfg("idle_detection", "idle_threshold_seconds", default=1800))
    if elapsed <= threshold:
        return False
    # Voice loop state check
    vl = g.get("_voice_loop")
    if vl is not None:
        try:
            state = str(getattr(vl, "_state", "") or "").lower()
            if state in ("listening", "thinking", "speaking"):
                return False
        except Exception:
            pass
    return True


# ── State decay / growth rules ─────────────────────────────────────────────


_MOOD_FLUSH_INTERVAL_SECONDS = 300.0
"""Mood file flush cadence. Tracked by wall time, not tick count, so a single
test call with a large dt still flushes immediately (no `last_flush` →
flush-now path), while a real 30 s heartbeat tick under steady-state batches
mutations into one flush per 5 min.
"""

_MOOD_STAT_TTL_SECONDS = 60.0
"""How often to re-stat ava_mood.json to detect external writers. Same
rationale as _ESTIMATES_STAT_TTL_SECONDS — stat() is ~25–30 ms on this
hardware, so we batch the freshness check. 60 s TTL means stat fires every
other 30 s tick at most, keeping the per-tick budget under 50 ms. Internal
flush updates the cache directly, so it's never stale to ourselves."""


def apply_state_decay_growth(g: dict[str, Any], dt_seconds: float) -> dict[str, Any]:
    """Apply the per-tick decay/growth rules to mood weights.

    Reads / writes raw mood weights via an in-memory cache on `g`. The cache
    invalidates on `ava_mood.json` mtime change so external writers (turn
    handlers, mood carryover, UI) don't get clobbered by stale temporal_sense
    state. Disk write goes through `save_mood_raw` (no enrichment) and only
    fires every `_MOOD_FLUSH_EVERY_TICKS` ticks — the in-memory cache is the
    source of truth between flushes.

    File-I/O cost on real Ava (Windows + Defender) is 30–50 ms per read and
    35 ms per write — too expensive to do every 30 s tick. The cache keeps the
    fast path under budget while preserving correctness for the other readers.

    Personhood-frame note: this function moves numbers in ava_mood.json
    according to the formulas. Whether those number-moves correspond to
    Ava's actual state changing is the open question CONTINUOUS_INTERIORITY
    §3 warns about. The formula is the spec; verification is whether
    those state changes propagate into other behavior.
    """
    if dt_seconds <= 0:
        return {"skipped": "dt_zero"}
    load_mood = g.get("load_mood_raw") or g.get("load_mood")
    save_mood = g.get("save_mood_raw") or g.get("save_mood")
    if not callable(load_mood) or not callable(save_mood):
        return {"skipped": "no_mood_fns"}

    summary: dict[str, Any] = {"dt_seconds": round(dt_seconds, 3)}

    # ── Cache + TTL stat + mtime invalidation ──────────────────────────
    # stat() costs 25–30 ms on this hardware, so we don't re-stat every tick.
    # External writers (turn handlers, mood carryover) get noticed within
    # _MOOD_STAT_TTL_SECONDS. Internal writes (this function's flush path)
    # update the cache directly so they're always fresh.
    base = Path(g.get("BASE_DIR") or ".")
    mood_path = base / "ava_mood.json"
    cache = g.get("_temporal_mood_cache")
    last_stat = float(g.get("_temporal_mood_last_stat_ts") or 0.0)
    now_ts = time.time()
    if cache is None or (now_ts - last_stat) >= _MOOD_STAT_TTL_SECONDS:
        try:
            cur_mtime = mood_path.stat().st_mtime if mood_path.is_file() else 0.0
        except Exception:
            cur_mtime = 0.0
        g["_temporal_mood_last_stat_ts"] = now_ts
        cached_mtime = float(g.get("_temporal_mood_cache_mtime") or 0.0)
        if cache is None or cur_mtime > cached_mtime + 0.001:
            try:
                cache = load_mood() or {}
            except Exception as e:
                return {"skipped": f"load_mood_error: {e!r}"}
            g["_temporal_mood_cache"] = cache
            g["_temporal_mood_cache_mtime"] = cur_mtime
            summary["cache_reload"] = True
    mood = cache

    weights = dict(mood.get("emotion_weights") or {})
    if not weights:
        return {"skipped": "no_weights"}

    changed = False

    # Sleep-mode decay multiplier (frustration / boredom / stress / joy decay
    # accelerates ~5x during SLEEPING). Applied as a scalar against the
    # passive_decay_per_second rate. See brain/sleep_mode.py for the
    # state machine.
    try:
        from brain.sleep_mode import get_emotion_decay_multiplier as _sleep_decay_mult
        _decay_mult = float(_sleep_decay_mult(g))
    except Exception:
        _decay_mult = 1.0

    # ── Frustration decay ──────────────────────────────────────────
    frustration = float(weights.get("frustration") or 0.0)
    if frustration > 0.001:
        # Determine mode. Calming-activity state would be set by
        # whatever future code classifies activity types; for now,
        # treat any non-idle non-busy state as passive. The active
        # exponential path is wired but rarely hits until activity
        # classification ships.
        in_calming = bool(g.get("_calming_activity_active"))
        if in_calming:
            tau = float(_cfg("frustration", "active_tau_seconds", default=120.0))
            if tau > 0:
                # frustration *= exp(-dt / tau)
                import math
                new_val = frustration * math.exp(-dt_seconds / tau)
                weights["frustration"] = max(0.0, new_val)
                changed = True
                summary["frustration_active_decay"] = round(frustration - new_val, 5)
        elif not processing_active(g):
            # Passive proportional decay — only when not actively responding.
            # The spec ("10-15% per 5 min") is multiplicative, not subtractive,
            # so the rate is applied to current value: smaller frustration
            # decays slower in absolute terms but at the same proportional
            # rate. With rate=0.0004/s and dt=300s, factor = 1 - 0.12 = 0.88,
            # so 0.20 -> 0.176 after 5 min (12% loss).
            # Sleep multiplier (1.0 awake, ~5.0 sleeping) accelerates decay.
            rate = float(_cfg("frustration", "passive_decay_per_second", default=0.0004)) * _decay_mult
            factor = max(0.0, 1.0 - rate * dt_seconds)
            new_val = max(0.0, frustration * factor)
            if new_val != frustration:
                weights["frustration"] = new_val
                changed = True
                summary["frustration_passive_decay"] = round(frustration - new_val, 5)

    # ── Boredom growth (awake) / decay (sleeping) ─────────────────
    boredom = float(weights.get("boredom") or 0.0)
    if _decay_mult > 1.0 and boredom > 0.001:
        # SLEEPING / ENTERING_SLEEP / WAKING — boredom decays alongside frustration.
        rate = float(_cfg("frustration", "passive_decay_per_second", default=0.0004)) * _decay_mult
        factor = max(0.0, 1.0 - rate * dt_seconds)
        new_val = max(0.0, boredom * factor)
        if new_val != boredom:
            weights["boredom"] = new_val
            changed = True
            summary["boredom_sleep_decay"] = round(boredom - new_val, 5)
    elif is_idle(g):
        cap = float(_cfg("boredom", "cap", default=1.0))
        if boredom < cap:
            rate = float(_cfg("boredom", "growth_per_second", default=0.0001))
            new_val = min(cap, boredom + rate * dt_seconds)
            if new_val != boredom:
                weights["boredom"] = new_val
                changed = True
                summary["boredom_growth"] = round(new_val - boredom, 5)

    if changed:
        mood["emotion_weights"] = weights
        # Mutate cache in-memory. Subsequent ticks see the new weights
        # without touching disk.
        g["_temporal_mood_cache"] = mood
        # Time-based flush: once every _MOOD_FLUSH_INTERVAL_SECONDS, OR on
        # first dirty mutation since process start (no last_flush stamp).
        now = time.time()
        last_flush = float(g.get("_temporal_mood_last_flush_ts") or 0.0)
        should_flush = (last_flush <= 0.0) or ((now - last_flush) >= _MOOD_FLUSH_INTERVAL_SECONDS)
        if should_flush:
            try:
                save_mood(mood)
                g["_temporal_mood_last_flush_ts"] = now
                # Update cached mtime to OUR write and refresh the stat-TTL
                # so the next tick doesn't re-stat right after our own flush.
                try:
                    g["_temporal_mood_cache_mtime"] = mood_path.stat().st_mtime
                    g["_temporal_mood_last_stat_ts"] = time.time()
                except Exception:
                    pass
                summary["flushed"] = True
            except Exception as e:
                summary["save_error"] = repr(e)
        else:
            summary["pending_flush_seconds"] = round(_MOOD_FLUSH_INTERVAL_SECONDS - (now - last_flush), 1)

    return summary


def boredom_reengage(g: dict[str, Any]) -> None:
    """Apply the one-shot boredom decay on user re-engagement.

    Called by whatever code detects "first user interaction after long
    idle." Multiplies current boredom by reengage_decay_factor (default
    0.5). Best-effort; swallow errors.
    """
    load_mood = g.get("load_mood")
    save_mood = g.get("save_mood")
    if not callable(load_mood) or not callable(save_mood):
        return
    try:
        mood = load_mood() or {}
        weights = dict(mood.get("emotion_weights") or {})
        boredom = float(weights.get("boredom") or 0.0)
        if boredom > 0.001:
            factor = float(_cfg("boredom", "reengage_decay_factor", default=0.5))
            weights["boredom"] = boredom * factor
            mood["emotion_weights"] = weights
            save_mood(mood)
    except Exception:
        pass


# ── Estimate tracking ──────────────────────────────────────────────────────


def _estimates_path(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or ".")
    rel = str(_cfg("estimate_tracking", "active_estimates_path",
                   default="state/active_estimates.json"))
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _history_path(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or ".")
    rel = str(_cfg("estimate_tracking", "task_history_log_path",
                   default="state/task_history_log.jsonl"))
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_ESTIMATES_LOCK = threading.Lock()


_ESTIMATES_STAT_TTL_SECONDS = 60.0
"""How often to re-stat active_estimates.json to detect external changes. Stat
itself costs 25–30 ms on this hardware (Windows + Defender), so re-stat'ing
every read defeats the purpose of caching. With a 60 s TTL the cache is at
most 60 s stale relative to external writers — but external writers
(track_estimate/resolve_estimate) update the cache directly via
_write_active_estimates, so this only affects ad-hoc external file mutations.
Heartbeat cadence is 30 s, so a 60 s TTL means stat fires every other tick
in steady state — keeping per-tick budget under 50 ms."""


def _read_active_estimates(g: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read active_estimates.json. Two-level cache on `g`:
      1. TTL cache: skip even the stat() call for `_ESTIMATES_STAT_TTL_SECONDS`
         after the last stat. stat() is ~25–30 ms on Windows + Defender.
      2. mtime cache: when stat is due, only re-read the file body if mtime
         advanced.
    Internal writers (_write_active_estimates) update both layers directly.
    """
    cached = g.get("_temporal_estimates_cache")
    last_stat = float(g.get("_temporal_estimates_last_stat_ts") or 0.0)
    now = time.time()
    if cached is not None and (now - last_stat) < _ESTIMATES_STAT_TTL_SECONDS:
        return cached
    p = _estimates_path(g)
    try:
        cur_mtime = p.stat().st_mtime if p.is_file() else 0.0
    except Exception:
        cur_mtime = 0.0
    g["_temporal_estimates_last_stat_ts"] = now
    cached_mtime = float(g.get("_temporal_estimates_cache_mtime") or -1.0)
    if cached is not None and abs(cur_mtime - cached_mtime) < 0.001:
        return cached
    if cur_mtime <= 0.0:
        g["_temporal_estimates_cache"] = {}
        g["_temporal_estimates_cache_mtime"] = 0.0
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    g["_temporal_estimates_cache"] = data
    g["_temporal_estimates_cache_mtime"] = cur_mtime
    return data


def _write_active_estimates(g: dict[str, Any], estimates: dict[str, dict[str, Any]]) -> None:
    p = _estimates_path(g)
    try:
        p.write_text(json.dumps(estimates, indent=2, ensure_ascii=False), encoding="utf-8")
        # Update cache with our write so the next read doesn't reload.
        g["_temporal_estimates_cache"] = estimates
        try:
            g["_temporal_estimates_cache_mtime"] = p.stat().st_mtime
        except Exception:
            pass
    except Exception as e:
        print(f"[temporal_sense] active_estimates write error: {e!r}")


def _has_active_tracked_task(g: dict[str, Any]) -> bool:
    """Used by processing_active(). Returns True iff there's at least one
    estimate that's NOT yet interrupted/resolved. Interrupted-but-still-
    in-file tasks don't count — they're effectively done from the
    processing-active perspective until resolve_estimate cleans them up.

    Uses a per-tick cache on g["_temporal_estimates_tick_cache"] when
    populated by run_fast_check_tick, since processing_active/is_idle can be
    called multiple times per tick (each previously incurred a 30–50 ms file
    read on Windows + Defender environments).
    """
    # _read_active_estimates handles its own cross-tick caching with mtime
    # invalidation, so this is a single 0.5 ms stat() on a cache hit.
    estimates = _read_active_estimates(g)
    if not estimates:
        return False
    for entry in estimates.values():
        if not entry.get("interrupted"):
            return True
    return False


def calibrate_from_history(g: dict[str, Any], kind: str) -> dict[str, Any]:
    """Read recent history rows for `kind` and return calibration stats.

    Returns {n_samples, median, mean, stddev, recommendation}. If too
    few samples (<3), recommendation is None and caller should use
    their own guess. Otherwise recommendation = median.
    """
    p = _history_path(g)
    if not p.is_file():
        return {"n_samples": 0, "recommendation": None}
    n_window = int(_cfg("estimate_tracking", "history_lookup_window", default=20))
    samples: list[float] = []
    try:
        # Read tail. For modest history sizes this is fine; if log
        # grows huge we can swap in a rotating-tail reader later.
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if str(row.get("kind") or "") != kind:
                continue
            actual = float(row.get("actual_seconds") or 0.0)
            if actual > 0:
                samples.append(actual)
            if len(samples) >= n_window:
                break
    except Exception as e:
        print(f"[temporal_sense] history read error: {e!r}")
        return {"n_samples": 0, "recommendation": None}

    if len(samples) < 3:
        return {"n_samples": len(samples), "recommendation": None}

    sorted_samples = sorted(samples)
    n = len(sorted_samples)
    median = sorted_samples[n // 2] if n % 2 == 1 else (sorted_samples[n // 2 - 1] + sorted_samples[n // 2]) / 2
    mean = sum(sorted_samples) / n
    var = sum((x - mean) ** 2 for x in sorted_samples) / n
    stddev = var ** 0.5
    return {
        "n_samples": n,
        "median": round(median, 2),
        "mean": round(mean, 2),
        "stddev": round(stddev, 2),
        "recommendation": round(median, 2),
    }


def track_estimate(
    g: dict[str, Any],
    *,
    task_id: str | None = None,
    estimate_seconds: float,
    kind: str,
    context: str = "",
) -> str:
    """Begin tracking an estimate. Returns the task_id (generated if not
    supplied). The fast-check tick will monitor this estimate for
    overrun until resolve_estimate() is called.

    `kind` is the estimate category for historical lookup ("restart",
    "research", "deep_path_turn", etc.). Use a stable string per category.
    """
    if estimate_seconds <= 0:
        raise ValueError("estimate_seconds must be > 0")
    if not task_id:
        task_id = f"task_{int(time.time() * 1000)}_{kind}"
    now = time.time()
    cal = calibrate_from_history(g, kind)
    entry = {
        "task_id": task_id,
        "kind": str(kind),
        "context": str(context)[:200],
        "estimate_seconds": float(estimate_seconds),
        "created_at": now,
        "interrupted": False,
        "confidence": 1.0,  # uncertainty hook structure; see TEMPORAL_SENSE.md §6 open question
        "history_calibration": cal,
    }
    with _ESTIMATES_LOCK:
        estimates = _read_active_estimates(g)
        estimates[task_id] = entry
        _write_active_estimates(g, estimates)
    return task_id


def update_confidence(g: dict[str, Any], task_id: str, confidence: float) -> None:
    """Update confidence for an active estimate. NO-OP UNTIL THE CONFIDENCE
    SOURCE QUESTION IS ANSWERED — see TEMPORAL_SENSE.md §6 open question
    for ROADMAP item 9. Hook structure ships disabled by default
    (config/temporal_sense.json uncertainty_hook.enabled = false).

    Stores the value either way so a future enabler can read history,
    but the fast-check tick won't act on it until enabled.
    """
    confidence = max(0.0, min(1.0, float(confidence)))
    with _ESTIMATES_LOCK:
        estimates = _read_active_estimates(g)
        if task_id in estimates:
            estimates[task_id]["confidence"] = confidence
            _write_active_estimates(g, estimates)


def resolve_estimate(
    g: dict[str, Any],
    task_id: str,
    *,
    actual_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Mark an estimate complete. Appends a row to task_history_log.jsonl
    and removes the entry from active_estimates.json. Returns the
    history row that was logged, or None if task_id wasn't tracked.

    actual_seconds: if not provided, computed as now - created_at.
    """
    with _ESTIMATES_LOCK:
        estimates = _read_active_estimates(g)
        entry = estimates.pop(task_id, None)
        if entry is None:
            return None
        _write_active_estimates(g, estimates)

    now = time.time()
    if actual_seconds is None:
        actual_seconds = max(0.0, now - float(entry.get("created_at") or now))

    row = {
        "ts": now,
        "task_id": task_id,
        "kind": entry.get("kind"),
        "estimate_seconds": entry.get("estimate_seconds"),
        "actual_seconds": round(float(actual_seconds), 3),
        "interrupted": bool(entry.get("interrupted")),
        "context": entry.get("context", ""),
        "history_calibration_at_create": entry.get("history_calibration"),
    }
    p = _history_path(g)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[temporal_sense] history append error: {e!r}")
    return row


# ── Self-interrupt on overrun ──────────────────────────────────────────────


def _enqueue_self_interrupt(g: dict[str, Any], message: str) -> None:
    """Queue a high-priority TTS line. Best-effort.

    Voice loop integration: the TTS worker yields between sentences,
    so the line will play at the next safe boundary. We don't preempt
    a sentence mid-word.
    """
    tts = g.get("_tts_worker")
    if tts is None:
        print(f"[temporal_sense] self_interrupt (no tts): {message}")
        return
    try:
        # Use existing speak() with focused emotion. The TTS worker
        # serializes internally; this enqueues at high priority by
        # going through the same path as voice_command replies.
        tts.speak(message, emotion="focused", intensity=0.6, blocking=False)
        print(f"[temporal_sense] self_interrupt enqueued: {message[:80]!r}")
    except Exception as e:
        print(f"[temporal_sense] self_interrupt enqueue failed: {e!r}")


def _format_remaining(seconds: float) -> str:
    if seconds < 60:
        return f"about {int(seconds)} more seconds"
    minutes = seconds / 60.0
    if minutes < 10:
        return f"about {minutes:.1f} more minutes"
    return f"about {int(round(minutes))} more minutes"


def _check_overrun(g: dict[str, Any], now: float) -> dict[str, Any]:
    """Scan active estimates for overrun. Fire self-interrupt on threshold.

    Returns summary of what was checked / fired. Idempotent — won't
    fire twice for the same task (entries are marked interrupted=True).
    """
    overrun_pct = float(_cfg("estimate_tracking", "overrun_pct", default=0.25))
    overrun_min = float(_cfg("estimate_tracking", "overrun_min_seconds", default=8.0))
    uncertainty_enabled = bool(_cfg("uncertainty_hook", "enabled", default=False))
    low_conf = float(_cfg("uncertainty_hook", "low_confidence_threshold", default=0.4))
    elapsed_frac = float(_cfg("uncertainty_hook", "elapsed_fraction_required", default=0.5))

    summary: dict[str, Any] = {"checked": 0, "fired_overrun": 0, "fired_uncertainty": 0}

    with _ESTIMATES_LOCK:
        # _read_active_estimates handles its own cross-tick mtime caching;
        # take a local copy so mutations below don't leak into the cache mid-loop.
        estimates = dict(_read_active_estimates(g))
        if not estimates:
            return summary

        modified = False
        for task_id, entry in list(estimates.items()):
            if entry.get("interrupted"):
                continue
            summary["checked"] += 1
            est = float(entry.get("estimate_seconds") or 0.0)
            created = float(entry.get("created_at") or now)
            elapsed = max(0.0, now - created)

            # Overrun check (both conditions must hold)
            over_pct = (1.0 + overrun_pct) * est
            if elapsed > over_pct and (elapsed - est) > overrun_min:
                cal = entry.get("history_calibration") or {}
                rec = cal.get("recommendation")
                if rec is not None and rec > est:
                    remaining = max(0.0, float(rec) - elapsed)
                else:
                    remaining = max(0.0, elapsed * 0.3)  # rough heuristic
                msg = f"I see this is taking longer than I said. I need {_format_remaining(remaining)}."
                _enqueue_self_interrupt(g, msg)
                entry["interrupted"] = True
                entry["interrupted_at"] = now
                entry["interrupt_reason"] = "overrun"
                modified = True
                summary["fired_overrun"] += 1
                continue

            # Uncertainty hook (DISABLED by default — see TEMPORAL_SENSE.md §6)
            if uncertainty_enabled:
                conf = float(entry.get("confidence") or 1.0)
                if conf < low_conf and elapsed > elapsed_frac * est:
                    msg = "I'm not sure this is going to finish on schedule. Let me check where I am."
                    _enqueue_self_interrupt(g, msg)
                    entry["interrupted"] = True
                    entry["interrupted_at"] = now
                    entry["interrupt_reason"] = "uncertainty"
                    modified = True
                    summary["fired_uncertainty"] += 1

        if modified:
            _write_active_estimates(g, estimates)

    return summary


# ── Fast-check tick (called from heartbeat) ────────────────────────────────


def run_fast_check_tick(g: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Fast-check tick — runs every heartbeat at heartbeat cadence.

    Per docs/TEMPORAL_SENSE.md §2 / §8: cheap arithmetic + state
    mutation, no LLM calls, no blocking I/O, ≤50ms total budget.

    Returns a dict summary for observability (caller can log / inspect).
    """
    _tick_t0 = time.perf_counter()
    if now is None:
        now = time.time()
    last_tick = float(g.get("_last_temporal_fast_check_ts") or 0.0)
    if last_tick <= 0:
        # First call — establish baseline, do a no-op tick.
        g["_last_temporal_fast_check_ts"] = now
        return {"first_tick": True}
    dt = max(0.0, now - last_tick)
    g["_last_temporal_fast_check_ts"] = now

    # Per-section timing for tick budget diagnosis (gated by env var so it's
    # zero-cost in production).
    _profile = os.environ.get("TEMPORAL_TICK_LOG", "").strip() == "1"

    if _profile:
        _t = time.perf_counter()
    proc_active = processing_active(g)
    if _profile:
        _ms_proc_active = (time.perf_counter() - _t) * 1000.0
        _t = time.perf_counter()
    idle = is_idle(g)
    if _profile:
        _ms_is_idle = (time.perf_counter() - _t) * 1000.0

    summary: dict[str, Any] = {
        "dt_seconds": round(dt, 3),
        "processing_active": proc_active,
        "is_idle": idle,
    }

    # Apply state decay/growth (frustration, boredom)
    if _profile:
        _t = time.perf_counter()
    state_summary = apply_state_decay_growth(g, dt)
    if _profile:
        _ms_decay = (time.perf_counter() - _t) * 1000.0
    summary["state"] = state_summary

    # Check active estimates for overrun
    if _profile:
        _t = time.perf_counter()
    overrun_summary = _check_overrun(g, now)
    if _profile:
        _ms_overrun = (time.perf_counter() - _t) * 1000.0
    summary["estimates"] = overrun_summary

    # Tick timing — always recorded; gated JSONL log for verification runs.
    summary["tick_ms"] = round((time.perf_counter() - _tick_t0) * 1000.0, 3)
    if _profile:
        summary["sub_ms"] = {
            "proc_active": round(_ms_proc_active, 2),
            "is_idle": round(_ms_is_idle, 2),
            "decay": round(_ms_decay, 2),
            "overrun": round(_ms_overrun, 2),
        }

    # Stash on g for snapshot exposure
    try:
        g["_temporal_last_summary"] = summary
        g["_temporal_elapsed_idle_seconds"] = (
            now - float(g.get("_last_user_interaction_ts") or now)
            if g.get("_last_user_interaction_ts") else 0.0
        )
    except Exception:
        pass

    if os.environ.get("TEMPORAL_TICK_LOG", "").strip() == "1":
        try:
            log_path = Path(g.get("BASE_DIR") or ".") / "state" / "temporal_tick_log.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _log_row = {
                "ts": now,
                "tick_ms": summary["tick_ms"],
                "dt_seconds": summary["dt_seconds"],
                "processing_active": summary["processing_active"],
                "is_idle": summary["is_idle"],
            }
            if "sub_ms" in summary:
                _log_row["sub_ms"] = summary["sub_ms"]
            with log_path.open("a", encoding="utf-8") as _f:
                _f.write(json.dumps(_log_row) + "\n")
        except Exception:
            pass

    return summary
