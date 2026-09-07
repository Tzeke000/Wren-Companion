"""gpu_park_tool — ONE command to park / un-park Iris's GPU body, with verification.

Why (2026-09-07, Zeke: "you can go ahead and do them now"):
  The park recipe (durable_scars §Parking my own body) is six-plus separate tool calls.
  On 2026-09-06 22:0x the re-park skipped `attention_smooth stop` on the belief that the servo
  was already off — the motion sentry had re-armed it 90 s after the earlier stop — and the head
  jogged blind for three hours on a frozen frame (117k PTZ writes). A recipe executed by hand can
  skip a step; this tool cannot, and it VERIFIES every step against the live process (thread list,
  engine stash, flags) instead of trusting return dicts.

  gpu_park(action='park')    → all steps, then verify. Idempotent.
  gpu_park(action='unpark')  → checks frame freshness FIRST (a frozen camera stream is what the
                               park masked on 09-06 — heals with eyes_reload + release_ptz), then
                               restores eyes / hands / pose / body / sentry / voice body.
                               The tracking SERVO is NOT restarted unless servo=true (target
                               selection fix still owed — a wrong-target servo is worse than none).
  gpu_park(action='status')  → the same verification without changing anything.

Never call this 'body_park' — that name belongs to the Vector tool that wedges the SDK.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from tools.tool_registry import register_tool

_FRESH_S = 5.0          # a frame older than this on un-park = frozen stream → reload
_PARK_THREADS = ("attention_smooth", "attention_sentry", "human_pose_loop", "iris-body-worker")


def _log(msg: str) -> None:
    import sys
    print(f"[gpu_park] {msg}", file=sys.stderr, flush=True)


def _threads() -> set[str]:
    return {t.name for t in threading.enumerate()}


def _frame_age_s() -> float | None:
    try:
        from brain import frame_store
        res = frame_store.get_buffered_frame(max_age_sec=1e9)
        if res is None or res.frame is None:
            return None
        return max(0.0, time.time() - float(res.capture_ts))
    except Exception as e:  # noqa: BLE001
        _log(f"frame age probe failed: {e!r}")
        return None


def _flag_path():
    from brain.iris_paths import paths
    return paths.body_pause_flag


def _verify(g: dict[str, Any], want_parked: bool) -> dict[str, Any]:
    """Every check reads LIVE state — threads, engine stash, flag — never a return dict."""
    th = _threads()
    stash = g.get("_eyes_rest_stash") or {}
    checks = {
        "eyes_resting": bool(stash),
        "hands_off": not bool(g.get("_hands_enabled", True)),
        "servo_thread_absent": "attention_smooth" not in th,
        "sentry_thread_absent": "attention_sentry" not in th,
        "pose_loop_absent": "human_pose_loop" not in th,
        "body_worker_absent": "iris-body-worker" not in th,
        "voice_body_paused": _flag_path().exists(),
    }
    if want_parked:
        ok = all(checks.values())
    else:
        # un-parked = the inverse of every check except the servo, which stays off by design
        inv = {k: (not v) for k, v in checks.items() if k != "servo_thread_absent"}
        ok = all(inv.values())
        checks = {**{k: (not v) for k, v in checks.items() if k != "servo_thread_absent"},
                  "servo_thread_absent": checks["servo_thread_absent"]}
    return {"ok": ok, "checks": checks, "threads_of_interest": sorted(t for t in th if t in _PARK_THREADS)}


def _park(g: dict[str, Any]) -> dict[str, Any]:
    from tools.system.runtime_repair_tool import _eyes_rest, _hands_rest
    from tools.system.attention_smooth_tool import _attention_smooth
    from tools.system.attention_engage_tool import _attention_sentry
    from tools.system.attention_follow_tool import _attention_follow
    from tools.system.human_pose_tool import _human_pose
    steps: dict[str, Any] = {}
    # Order matters: stop the things that can RE-ARM others first (sentry), then the servo,
    # then the perception workers, then the engines, then the voice body.
    steps["sentry_stop"] = _attention_sentry({"action": "stop"}, g)
    steps["follow_stop"] = _attention_follow({"action": "stop"}, g)
    steps["servo_stop"] = _attention_smooth({"action": "stop"}, g)
    steps["pose_loop_stop"] = _human_pose({"action": "loop", "mode": "stop"}, g)
    steps["body_loop_stop"] = _human_pose({"action": "body_loop", "mode": "stop"}, g)
    steps["hands_rest"] = _hands_rest({"rest": True}, g)
    steps["eyes_rest"] = _eyes_rest({"rest": True}, g)
    try:
        flag = _flag_path()
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(f"reason: gpu_park\npaused_at: {time.time()}\npaused_by: gpu_park tool\n",
                        encoding="utf-8")
        steps["voice_body_pause"] = {"ok": True, "flag": str(flag)}
    except Exception as e:  # noqa: BLE001
        steps["voice_body_pause"] = {"ok": False, "error": repr(e)}
    # Threads exit asynchronously — give them a moment before verifying.
    time.sleep(1.5)
    v = _verify(g, want_parked=True)
    return {"ok": v["ok"], "action": "park", "verify": v, "steps": steps,
            "note": ("PARKED and verified" if v["ok"] else
                     "PARK INCOMPLETE — see verify.checks; do not assume the GPU is free")}


def _unpark(g: dict[str, Any], servo: bool) -> dict[str, Any]:
    from tools.system.runtime_repair_tool import _eyes_rest, _hands_rest
    from tools.system.attention_smooth_tool import _attention_smooth
    from tools.system.attention_engage_tool import _attention_sentry
    from tools.system.attention_follow_tool import _attention_follow
    from tools.system.human_pose_tool import _human_pose
    steps: dict[str, Any] = {}
    # 1. voice body first (ears back), 2. engines, 3. frame freshness gate, 4. workers, 5. sentry
    try:
        flag = _flag_path()
        if flag.exists():
            flag.unlink()
        steps["voice_body_resume"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        steps["voice_body_resume"] = {"ok": False, "error": repr(e)}
    steps["eyes_resume"] = _eyes_rest({"rest": False}, g)
    steps["hands_on"] = _hands_rest({"rest": False}, g)
    # The camera loop RELEASES the capture while the shared pause flag exists (iris_runtime honours
    # body_pause_flag.exists()), so a parked body always shows a stale frame. After clearing the
    # flag give the loop up to ~10 s to reopen DSHOW and push a fresh frame before calling it frozen.
    age = _frame_age_s()
    t_wait = time.time()
    while (age is None or age > _FRESH_S) and time.time() - t_wait < 10.0:
        time.sleep(0.5)
        age = _frame_age_s()
    steps["frame_age_s_before"] = age
    steps["reopen_wait_s"] = round(time.time() - t_wait, 1)
    if age is None or age > _FRESH_S:
        # The camera stream froze behind the park on 09-06 (79-min-old frame while the capture
        # loop still 'read' at 29 fps). Heal before letting anything aim from it.
        _log(f"frame stale ({age}) — eyes_reload + release_ptz")
        try:
            from tools.system.eyes_reload_tool import _eyes_reload_fn
            steps["eyes_reload"] = _eyes_reload_fn({"deep": True}, g)
        except Exception as e:  # noqa: BLE001
            steps["eyes_reload"] = {"ok": False, "error": repr(e)}
        try:
            steps["release_ptz"] = _attention_follow({"action": "release_ptz"}, g)
        except Exception as e:  # noqa: BLE001
            steps["release_ptz"] = {"ok": False, "error": repr(e)}
        steps["frame_age_s_after"] = _frame_age_s()
    steps["pose_loop_start"] = _human_pose({"action": "loop", "mode": "start"}, g)
    steps["body_loop_start"] = _human_pose({"action": "body_loop", "mode": "start"}, g)
    steps["sentry_start"] = _attention_sentry({"action": "start"}, g)
    if servo:
        steps["servo_start"] = _attention_smooth({"action": "start", "target": "zeke"}, g)
    time.sleep(1.5)
    v = _verify(g, want_parked=False)
    return {"ok": v["ok"], "action": "unpark", "servo_started": bool(servo), "verify": v, "steps": steps,
            "note": ("UN-PARKED and verified (servo left OFF unless servo=true)" if v["ok"] else
                     "UN-PARK INCOMPLETE — see verify.checks")}


def _gpu_park(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """gpu_park(action='park'|'unpark'|'status', servo=false)"""
    action = str(params.get("action") or "status").lower()
    if action == "park":
        return _park(g)
    if action == "unpark":
        return _unpark(g, servo=bool(params.get("servo", False)))
    if action == "status":
        v_p = _verify(g, want_parked=True)
        return {"ok": True, "parked": v_p["ok"], "checks": v_p["checks"],
                "threads_of_interest": v_p["threads_of_interest"],
                "frame_age_s": _frame_age_s(), "voice_body_flag": str(_flag_path())}
    return {"ok": False, "error": f"unknown action {action!r} — park|unpark|status"}


register_tool(
    name="gpu_park",
    description=(
        "Park (action='park') or un-park (action='unpark', servo=false) Iris's GPU body in ONE "
        "command with LIVE verification (thread list, engine stash, voice-body flag). Park = sentry, "
        "follow, servo, pose+body loops, hands, eyes, voice body. Un-park checks frame freshness first "
        "and heals a frozen camera (eyes_reload + release_ptz) before restoring; the tracking servo "
        "stays OFF unless servo=true. action='status' verifies without changing anything. Tier 2."
    ),
    tier=2,
    handler=_gpu_park,
)
