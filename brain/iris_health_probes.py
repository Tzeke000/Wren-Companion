"""Real operational health probes for iris_health().

Built 2026-09-30 (Zeke's "Iris_fixes.docx" list, item #1: make 'healthy'
actually mean healthy). The pre-existing iris_health() reported subsystems
as OK based on *existence / initialization* (e.g. `_tts is not None`), which
produces false-green states: a subsystem can be "healthy" because it booted
even though it stopped functioning afterward.

These probes instead *exercise* the thing:
  - camera:     is a FRESH frame actually in the buffer? (age, not existence)
  - tick_loop:  is the 1Hz substrate heartbeat still advancing? (last_tick_ts age)
  - memory:     can I actually READ the count AND WRITE to the store dir?
  - voice:      are the mouth/daemon TCP ports actually accepting connections?
  - cognition:  how stale is the last session-attach? (advisory, not a fault)

Design rules (all load-bearing):
  * A subsystem with a deliberate-off flag reports state="disabled" and does
    NOT drag the overall verdict down. (CORE lesson: check state/ for a
    deliberate-off flag before treating a down service as broken.)
  * wake-word is off BY DESIGN on this host (IRIS_RUNTIME_OWNS_MIC=0); it is
    reported "disabled", never "down".
  * The overall verdict is the WORST non-disabled probe. `ok` only if every
    non-disabled critical probe is ok.
  * Every probe records `probe=` describing what was actually exercised, so a
    green result is auditable.

This module is import-safe both in-process (called from iris_runtime.iris_health)
and standalone (the adversarial test harness runs it via python directly), so
it reads on-disk state files rather than the runtime's in-process globals where
it can. Params exist purely so the adversarial test can point a probe at a
known-bad target and confirm it goes RED.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Optional

# state ranking: higher = worse. "disabled" is excluded from the overall verdict.
_RANK = {"ok": 0, "degraded": 1, "down": 2, "unknown": 1}
_STATE_FROM_RANK = {0: "ok", 1: "degraded", 2: "down"}


def _repo_root() -> Path:
    # brain/ is one level under the repo root.
    return Path(__file__).resolve().parent.parent


def _mk(name: str, state: str, detail: str, probe: str, **extra) -> dict:
    d = {"name": name, "state": state, "detail": detail, "probe": probe}
    d.update(extra)
    return d


# ─────────────────────────── individual probes ───────────────────────────

def probe_tick_loop(time_json: Optional[Path] = None,
                    fresh_s: float = 30.0, degraded_s: float = 120.0) -> dict:
    """Is the 1Hz time substrate still advancing? Real signal is the age of
    last_tick_ts on disk — NOT the self-reported tick_loop_alive bool, which
    would stay True if the loop set it and then died.

    Thresholds are wide (30s/120s) on purpose: the tick loop shares the runtime
    event loop with cognition, so it is legitimately STARVED for the duration of
    an active turn. A few seconds stale is normal thinking, not a stall; only a
    multi-minute gap means the loop is actually dead (the watchdog uses 180s)."""
    p = time_json or (_repo_root() / "state" / "iris_time.json")
    try:
        st = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception as e:
        return _mk("tick_loop", "down", f"time state unreadable: {e}",
                   "read state/iris_time.json")
    last = float(st.get("last_tick_ts") or 0.0)
    if last <= 0:
        return _mk("tick_loop", "down", "no last_tick_ts recorded",
                   "age(last_tick_ts)")
    age = time.time() - last
    interval = float(st.get("tick_interval_s") or 1.0)
    if age <= fresh_s:
        state = "ok"
    elif age <= degraded_s:
        state = "degraded"
    else:
        state = "down"
    return _mk("tick_loop", state, f"last tick {age:.1f}s ago (interval {interval}s)",
               "age(last_tick_ts) vs now", age_s=round(age, 2))


def _probe_camera_http(url: str, timeout: float = 1.5) -> Optional[dict]:
    """Cross-process camera freshness via the runtime HTTP endpoint. The route
    serves a frame ONLY if it is fresh (get_buffered_frame max_age_sec=2.0),
    returning 204 otherwise — so HTTP status is a real freshness signal, not an
    existence check. Returns None if the endpoint itself is unreachable (so the
    caller can distinguish 'camera dead' from 'HTTP layer dead')."""
    try:
        import urllib.request
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            n = len(resp.read())
        if code == 200 and n > 0:
            return _mk("camera", "ok", f"fresh frame served ({n} B, HTTP 200, <=2s)",
                       "HTTP GET latest_frame (200=fresh)")
        return _mk("camera", "down", f"no fresh frame (HTTP {code})",
                   "HTTP GET latest_frame (204=stale/none)")
    except Exception:
        return None


def probe_camera(peek_fn=None, fresh_s: float = 5.0, degraded_s: float = 12.0,
                 http_url: str = "http://127.0.0.1:5876/api/v1/vision/latest_frame") -> dict:
    """Is a FRESH frame actually in the capture buffer? Uses the live buffer age
    (frame_store.peek_buffer_age_sec) when running IN-PROCESS inside the runtime.

    The frame buffer is PROCESS-LOCAL, so a probe running in a separate process
    (my standalone test harness, a cron) sees an empty buffer and would report a
    FALSE-RED. To stay honest cross-process, when the local buffer is empty we
    fall back to the runtime HTTP endpoint, which only serves frames <=2s old
    (204 otherwise) — real freshness, the exact endpoint CORE says to trust.
    peek_fn is injectable so the adversarial test can simulate a frozen camera."""
    injected = peek_fn is not None
    if peek_fn is None:
        try:
            from brain.frame_store import peek_buffer_age_sec as peek_fn  # type: ignore
        except Exception:
            peek_fn = None
    age = None
    if peek_fn is not None:
        try:
            age = peek_fn()
        except Exception as e:
            age = e  # sentinel: peek raised
    # In-process live buffer path.
    if isinstance(age, (int, float)):
        if age <= fresh_s:
            state = "ok"
        elif age <= degraded_s:
            state = "degraded"
        else:
            state = "down"
        return _mk("camera", state, f"frame {age:.1f}s old (in-process buffer)",
                   "frame_store.peek_buffer_age_sec", age_s=round(float(age), 2))
    # Injected test that explicitly forced a frozen/None buffer: trust it, no HTTP.
    if injected:
        detail = "peek raised" if isinstance(age, Exception) else "no frame in buffer (injected)"
        return _mk("camera", "down", detail, "frame_store.peek_buffer_age_sec (injected)")
    # Cross-process (or empty local buffer): fall back to HTTP freshness.
    http = _probe_camera_http(http_url)
    if http is not None:
        return http
    return _mk("camera", "down", "no local frame and HTTP endpoint unreachable",
               "frame_store + HTTP fallback both failed")


def probe_memory(mem_path: Optional[Path] = None, mem_obj=None) -> dict:
    """Can I actually read the memory count AND write to the store directory?
    Existence of the file is not enough — this does a real temp-file write+delete
    in the memory dir to confirm writability, and calls count() to confirm read.
    The temp file is a sidecar; the canonical iris_memory.jsonl is never touched."""
    p = mem_path or (_repo_root() / "state" / "iris_memory.jsonl")
    p = Path(p)
    # READ side — count entries by reading the store.
    read_ok = False
    count = None
    try:
        if mem_obj is not None and hasattr(mem_obj, "count"):
            count = int(mem_obj.count())
            read_ok = True
        elif p.exists():
            with p.open("r", encoding="utf-8") as fh:
                count = sum(1 for _ in fh)
            read_ok = True
        else:
            return _mk("memory", "down", f"store missing: {p}",
                       "read+write round-trip")
    except Exception as e:
        return _mk("memory", "down", f"read failed: {e}",
                   "read+write round-trip")
    # WRITE side — real temp-file round-trip in the store dir.
    write_ok = False
    try:
        d = p.parent
        d.mkdir(parents=True, exist_ok=True)
        probe_file = d / f".health_write_probe_{os.getpid()}"
        probe_file.write_text("ok", encoding="utf-8")
        back = probe_file.read_text(encoding="utf-8")
        probe_file.unlink()
        write_ok = (back == "ok")
    except Exception as e:
        return _mk("memory", "down", f"read ok (count={count}) but WRITE failed: {e}",
                   "read+write round-trip", count=count, read_ok=read_ok, write_ok=False)
    state = "ok" if (read_ok and write_ok) else "down"
    return _mk("memory", state, f"read+write ok (count={count})",
               "read+write round-trip", count=count, read_ok=read_ok, write_ok=write_ok)


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def probe_voice(mouth_port: int = 8769, daemon_port: int = 8770,
                off_flag: Optional[Path] = None, host: str = "127.0.0.1") -> dict:
    """Are the mouth (:8769) and daemon (:8770) TCP ports actually accepting
    connections? Honors the deliberate-off flag first: if voice is deliberately
    off, report 'disabled' (NOT down) so it doesn't drag the verdict and doesn't
    invite healing. Ports are injectable so the test can prove the socket probe
    is real by pointing at a dead port."""
    flag = off_flag if off_flag is not None else (_repo_root() / "state" / "voice_deliberately_off.json")
    # Deliberate-off: flag exists AND does not say {"off": false}. (2026-09-05:
    # absent flag = on; a present flag that says off:false is also on.)
    try:
        if Path(flag).exists():
            try:
                data = json.loads(Path(flag).read_text(encoding="utf-8"))
                off = bool(data.get("off", True))
            except Exception:
                off = True
            if off:
                return _mk("voice", "disabled", "deliberate-off flag set",
                           "off-flag check", off_flag=str(flag))
    except Exception:
        pass
    mouth = _port_open(host, mouth_port)
    daemon = _port_open(host, daemon_port)
    if mouth and daemon:
        state, detail = "ok", f"mouth:{mouth_port} + daemon:{daemon_port} listening"
    elif mouth or daemon:
        state = "degraded"
        detail = f"only {'mouth' if mouth else 'daemon'} up ({mouth_port if mouth else daemon_port})"
    else:
        state, detail = "down", f"neither {mouth_port} nor {daemon_port} listening"
    return _mk("voice", state, detail, "TCP connect to mouth+daemon ports",
               mouth=mouth, daemon=daemon)


def probe_cognition_attach(time_json: Optional[Path] = None,
                           fresh_s: float = 600.0) -> dict:
    """How stale is the last session-attach? Advisory only — I can legitimately
    be idle for a while, so staleness is 'degraded' at worst, never 'down'."""
    p = time_json or (_repo_root() / "state" / "iris_time.json")
    try:
        st = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception as e:
        return _mk("cognition_attach", "unknown", f"time state unreadable: {e}",
                   "age(last_session_attached_ts)")
    last = float(st.get("last_session_attached_ts") or 0.0)
    if last <= 0:
        return _mk("cognition_attach", "degraded", "never attached this process",
                   "age(last_session_attached_ts)")
    age = time.time() - last
    state = "ok" if age <= fresh_s else "degraded"
    return _mk("cognition_attach", state, f"last attach {age:.0f}s ago",
               "age(last_session_attached_ts)", age_s=round(age, 1))


def probe_wake() -> dict:
    """Wake word is OFF BY DESIGN on this host (IRIS_RUNTIME_OWNS_MIC=0 set
    nowhere). Always 'disabled', never a fault — encoded so a future me can't
    'fix' it."""
    return _mk("wake", "disabled", "wake-word off by design (IRIS_RUNTIME_OWNS_MIC=0)",
               "design invariant")


# ─────────────────────────── aggregator ───────────────────────────

# Which probes gate the overall verdict. cognition_attach is advisory/context.
_CRITICAL = {"tick_loop", "camera", "memory", "voice"}


def compute_health(mem_obj=None, extra_probes: Optional[list] = None) -> dict:
    """Run every real probe and return an authoritative verdict.

    Returns:
      {
        verdict: "ok"|"degraded"|"down",
        checked_ts, checked_iso,
        probes: {name: {state, detail, probe, ...}},
        degraded: [names of critical probes not ok],
        disabled: [names deliberately off],
        false_green_guard: True,   # marker that this is the real aggregator
      }
    """
    probes = [
        probe_tick_loop(),
        probe_camera(),
        probe_memory(mem_obj=mem_obj),
        probe_voice(),
        probe_cognition_attach(),
        probe_wake(),
    ]
    if extra_probes:
        probes.extend(extra_probes)
    by_name = {p["name"]: p for p in probes}
    disabled = [p["name"] for p in probes if p["state"] == "disabled"]
    # overall = worst rank among CRITICAL, non-disabled probes.
    worst = 0
    degraded = []
    for p in probes:
        if p["name"] not in _CRITICAL:
            continue
        if p["state"] == "disabled":
            continue
        r = _RANK.get(p["state"], 1)
        if p["state"] != "ok":
            degraded.append(p["name"])
        worst = max(worst, r)
    verdict = _STATE_FROM_RANK.get(worst, "degraded")
    now = time.time()
    return {
        "verdict": verdict,
        "checked_ts": now,
        "checked_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "probes": by_name,
        "degraded": degraded,
        "disabled": disabled,
        "false_green_guard": True,
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(compute_health())
