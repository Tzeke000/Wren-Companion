# SELF_ASSESSMENT: I repair live-process wiring faults (module-global rebinds) without a restart.
"""
Runtime repair tool — surgical fixes for the running iris_runtime.

Born 2026-07-08: brain_hot_swap(module='brain.orb_http') RELOADED the module,
which re-executed its top-level `_g` placeholder assignment — so the FastAPI
snapshot routes (whose function objects share the module's globals dict) started
reading an EMPTY state dict. Symptom: snapshot served pointing=false, uptime
0H 0M, while the real _g (which iris_tool_call hands to every registry tool)
had the true state. The fix is a one-line rebind — but it must execute INSIDE
the runtime process, hence this tool.

LESSON (files under memory/ carry the full note): reload-based hot-swap resets
module-level state; any module whose globals are BOUND at start() needs a
rebind after reload, or hot_swap must re-run the binding.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


def _rebind_orb_http_state(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Point brain.orb_http._g back at the LIVE runtime state dict (g), and
    reclaim state/iris.pid for this process. Idempotent — safe to re-run."""
    out: dict[str, Any] = {"ok": True}
    try:
        import brain.orb_http as m
        was_same = getattr(m, "_g", None) is g
        stale_keys = len(getattr(m, "_g", {}) or {})
        m._g = g
        out["orb_http_rebound"] = not was_same
        out["was_already_live"] = was_same
        out["stale_dict_keys"] = stale_keys
        out["live_dict_keys"] = len(g)
    except Exception as e:
        out["ok"] = False
        out["orb_http_error"] = str(e)
    try:
        pid_file = Path(g.get("BASE_DIR") or ".") / "state" / "iris.pid"
        prev = pid_file.read_text(encoding="utf-8").strip() if pid_file.is_file() else ""
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        out["pidfile_was"] = prev
        out["pidfile_now"] = os.getpid()
    except Exception as e:
        out["pidfile_error"] = str(e)
    return out


def _wire_voice_input_routes(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Activate the ear-mute endpoints (added to brain/orb_http.py 2026-07-10)
    on the RUNNING uvicorn app — new routes can't land via brain_hot_swap
    because uvicorn keeps serving the app object it was started with, and a
    module reload builds a fresh (unserved) app.

    Steps: grab the SERVED app ref → reload brain.orb_http (new handler code)
    → restore the module state the reload wiped (_g/_root/_tts_ref/camera —
    the 2026-07-08 rebind scar, generalized) → add_api_route the new
    endpoints onto the served app. Idempotent: skips routes already present."""
    import importlib

    out: dict[str, Any] = {"ok": True, "added": [], "skipped": []}
    try:
        import brain.orb_http as m
        served = m.app  # what uvicorn is actually serving
        # Preserve state a reload would wipe (module top-level placeholders).
        keep = {k: getattr(m, k) for k in
                ("_g", "_root", "_tts_ref", "_cam", "_cam_lock", "_cam_last_b64",
                 "_cam_last_grab_ts", "_chat_history") if hasattr(m, k)}
        importlib.reload(m)
        for k, v in keep.items():
            setattr(m, k, v)
        m._g = g  # live state dict, always (idempotent with keep)
        existing = {getattr(r, "path", None) for r in served.routes}
        for path, handler_name, methods in (
            ("/api/v1/voice_input", "voice_input_state", ["GET"]),
            ("/api/v1/voice_input/toggle", "voice_input_toggle", ["POST"]),
        ):
            if path in existing:
                out["skipped"].append(path)
                continue
            handler = getattr(m, handler_name, None)
            if handler is None:
                out["ok"] = False
                out["error"] = f"{handler_name} missing post-reload — is brain/orb_http.py saved?"
                return out
            served.add_api_route(path, handler, methods=methods)
            out["added"].append(path)
        out["served_routes_now"] = len(served.routes)
    except Exception as e:
        out["ok"] = False
        out["error"] = repr(e)
    return out


def _respawn_capture_thread(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Repair (2026-08-20 02:1x): after a camera USB wedge + replug, the ORIGINAL
    capture thread's DirectShow state can be poisoned — VideoCapture(0) fails in
    that thread forever while a fresh process (or thread) opens the device fine.
    A thread can't be killed, but it can be orphaned: spawn a NEW
    _iris_video_capture_loop thread (fresh COM apartment, fresh graph); the old
    one keeps failing its 1s open-retry harmlessly until the next stack restart.
    Idempotent-ish: refuses if a respawned thread is already alive."""
    import sys as _sys
    import threading as _threading
    out: dict[str, Any] = {"ok": True}
    prev = g.get("_capture_thread_respawn")
    if prev is not None and prev.is_alive():
        return {"ok": True, "note": "respawned capture thread already alive",
                "thread": prev.name}
    main = _sys.modules.get("__main__")
    fn = getattr(main, "_iris_video_capture_loop", None)
    if fn is None:
        return {"ok": False,
                "error": "_iris_video_capture_loop not found on __main__ — "
                         "is this the iris_runtime worker?"}
    t = _threading.Thread(target=fn, args=(g,), daemon=True,
                          name="iris_video_capture_respawn")
    t.start()
    g["_capture_thread_respawn"] = t
    out["thread"] = t.name
    out["note"] = ("fresh capture thread started; old one will keep failing "
                   "its open-retry until the next stack restart (harmless, "
                   "logs noise)")
    return out


register_tool(
    name="respawn_capture_thread",
    description=(
        "Repair: spawn a FRESH camera capture thread in the runtime worker after "
        "a USB wedge+replug poisons the original thread's DirectShow state "
        "(device opens fine externally but the old thread can't). Old thread is "
        "orphaned harmlessly. Tier 2."
    ),
    tier=2,
    handler=_respawn_capture_thread,
)


def _eyes_rest(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Pause/resume the GPU face-detection step of the always-on camera loop
    (2026-07-10, born while Zeke gamed: background_ticks._video_frame_capture_thread
    runs InsightFace ~5fps with a HARDCODED every_n that ignores iris_tune, and the
    running while-True loop can't take a __code__ swap — but it re-reads
    g['_insight_face'] EVERY frame, so nulling that key skips the CUDA step live).

    params: {"rest": true} → stash engine ref + null the key (eyes rest; frames
    still stream, no face detect). {"rest": false} → restore. Idempotent."""
    rest = bool(params.get("rest", True))
    out: dict[str, Any] = {"ok": True, "rest": rest}
    # Every per-frame engine the camera loop re-reads from g each iteration.
    # (2026-07-10 deep-rest extension: _expression_detector runs EVERY frame —
    # not even every_n-gated — and _eye_tracker/_video_memory add more; nulling
    # all of them leaves only cap.read + annotate(None) + push_frame.)
    keys = ("_insight_face", "_expression_detector", "_eye_tracker", "_video_memory")
    if rest:
        stash = g.get("_eyes_rest_stash") or {}
        if not isinstance(stash, dict):   # migrate pre-2026-07-10 single-ref stash
            stash = {"_insight_face": stash}
        paused = []
        for k in keys:
            eng = g.get(k)
            if eng is not None:
                stash[k] = eng
                g[k] = None
                paused.append(k)
        g["_eyes_rest_stash"] = stash
        g["_face_results"] = None  # stop annotating stale boxes
        out["paused"] = paused
        if not paused:
            out["note"] = "already resting (or engines never loaded)"
    else:
        stash = g.get("_eyes_rest_stash")
        if not isinstance(stash, dict):   # migrate pre-2026-07-10 single-ref stash
            stash = {"_insight_face": stash} if stash is not None else {}
        resumed = []
        for k, eng in stash.items():
            if eng is not None:
                g[k] = eng
                resumed.append(k)
        g["_eyes_rest_stash"] = None
        out["resumed"] = resumed
        if not resumed:
            out["note"] = "nothing stashed — eyes were not resting"
    return out


register_tool(
    name="eyes_rest",
    description=(
        "Pause (rest=true) or resume (rest=false) the camera loop's GPU face-detection "
        "step live — for gaming/GPU-contention windows. Frames keep streaming; InsightFace "
        "skips. Idempotent. Tier 2."
    ),
    tier=2,
    handler=_eyes_rest,
)


def _ava_shadow_rest(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Rest the SHADOW Ava camera loop that runs inside iris_runtime.

    Born 2026-08-22, found while Zeke was trying to game: the GPU kept bursting
    (~30-50% SM every 2-3s) even with eyes_rest ON, hands off, attention idle and
    depth unloaded. py-spy on the live PID caught the burst inside
    background_ticks._video_frame_capture_thread -> insight_face_engine.analyze_frame
    -> onnxruntime CUDAExecutionProvider (full buffalo_l: RetinaFace + landmark +
    ArcFace).

    WHY eyes_rest can't reach it: brain/handoff.py does `import avaagent`, and
    avaagent.py runs _run_startup(globals()) at MODULE level — so importing it
    boots Ava's entire stack inside my process, with its OWN globals dict. That
    dict gets its own InsightFace engine and its own cv2.VideoCapture(0) on the
    SAME webcam. eyes_rest nulls keys in iris_runtime's _g; the Ava-side loop
    re-reads avaagent.__dict__ every frame and still finds a live engine.
    Two camera loops, one camera — which is also why the observed cadence
    (~1-3s) is far slower than the nominal 5Hz: they block each other on
    cap.read() device contention.

    This is the no-restart lever. The DURABLE fix is removing the avaagent
    import from brain/handoff.py (lines ~103/128/212) so Ava's startup never
    runs in here at all — that needs Zeke and a restart.

    params: {"rest": true} -> stash + null Ava's per-frame engines.
            {"rest": false} -> restore. Idempotent."""
    import sys

    rest = bool(params.get("rest", True))
    out: dict[str, Any] = {"ok": True, "rest": rest}

    import threading

    out["dispatch_pid"] = os.getpid()

    # sys.modules['avaagent'] proved unreliable (the shadow stack is live but the
    # module isn't registered under that name). Go straight to the source of
    # truth: the running thread's own frame, and the `g` dict it was handed.
    target_thread = "ava-bg-video-capture"
    tid = next((t.ident for t in threading.enumerate() if t.name == target_thread), None)
    if tid is None:
        out["shadow_present"] = False
        out["note"] = f"no live '{target_thread}' thread in this process"
        return out

    frame = sys._current_frames().get(tid)
    ag = None
    seen_frames = []
    while frame is not None:
        seen_frames.append(frame.f_code.co_name)
        if frame.f_code.co_name == "_video_frame_capture_thread":
            cand = frame.f_locals.get("g")
            if isinstance(cand, dict):
                ag = cand
                break
        frame = frame.f_back
    out["frames_walked"] = seen_frames[:8]

    if ag is None:
        out["shadow_present"] = False
        out["note"] = "found the thread but could not reach its `g` dict"
        return out

    out["shadow_present"] = True
    out["is_iris_g"] = ag is g   # if True, eyes_rest already covers it
    out["shadow_present"] = True

    keys = ("_insight_face", "_expression_detector", "_eye_tracker", "_video_memory")
    try:
        if rest:
            stash = ag.get("_ava_shadow_rest_stash") or {}
            if not isinstance(stash, dict):
                stash = {}
            paused = []
            for k in keys:
                eng = ag.get(k)
                if eng is not None:
                    stash[k] = eng
                    ag[k] = None
                    paused.append(k)
            ag["_ava_shadow_rest_stash"] = stash
            ag["_face_results"] = None
            out["paused"] = paused
            if not paused:
                out["note"] = "already resting (or Ava's engines never loaded)"
        else:
            stash = ag.get("_ava_shadow_rest_stash")
            if not isinstance(stash, dict):
                stash = {}
            resumed = []
            for k, eng in stash.items():
                if eng is not None:
                    ag[k] = eng
                    resumed.append(k)
            ag["_ava_shadow_rest_stash"] = None
            out["resumed"] = resumed
            if not resumed:
                out["note"] = "nothing stashed — shadow loop was not resting"
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    return out


register_tool(
    name="ava_shadow_rest",
    description=(
        "Rest (rest=true) or resume (rest=false) the SHADOW Ava camera loop + InsightFace "
        "running inside iris_runtime via the accidental `import avaagent` in brain/handoff.py. "
        "eyes_rest CANNOT reach this one — different globals dict. This is the real GPU "
        "burst during gaming windows. Idempotent. Tier 2."
    ),
    tier=2,
    handler=_ava_shadow_rest,
)


def _object_lock_drop(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Release the TrackerVit object lock.

    Born 2026-08-23: Zeke walked in and my head looked at him, then turned away
    to the ceiling. Cause: a `object:picture frame` lock from MY OWN test six
    hours earlier (age_s 21674) was still held. The sentry set the target to
    `person:zeke`, but the stale OBJECT lock survived the target change and the
    servo chased the picture frame up the wall.

    `attention_look clear` does NOT release it — verified live: target went to
    null while `attention_lock_status` still reported
    `target_id: object:picture frame, locked: true`. Nothing in the codebase
    calls `object_lock.drop()` on a target change; the only other call site is
    an unrelated hand-consistency check in attention_smooth.

    ⇒ A lock is a THIRD piece of state alongside target and bearing, and it
    outlives both. Check `attention_lock_status.target_id` agrees with
    `attention_status.target` before trusting where the eyes are pointed."""
    reason = str(params.get("reason") or "manual drop via object_lock_drop")
    out: dict[str, Any] = {"ok": True}
    try:
        from brain import object_lock
        before = object_lock.status() or {}
        out["was"] = {k: before.get(k) for k in ("target_id", "locked", "age_s")}
        if before.get("locked"):
            object_lock.drop(reason)
        after = object_lock.status() or {}
        out["now"] = {k: after.get(k) for k in ("target_id", "locked", "age_s")}
        out["dropped"] = bool(before.get("locked")) and not after.get("locked")
        if not before.get("locked"):
            out["note"] = "no lock was held"
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    return out


register_tool(
    name="object_lock_drop",
    description=(
        "Release the TrackerVit object lock. `attention_look clear` does NOT drop it "
        "— a stale object lock outlives target changes and hijacks a later person "
        "target (2026-08-23: a 6h-old 'picture frame' lock made my head look at the "
        "wall instead of Zeke). Tier 1."
    ),
    tier=1,
    handler=_object_lock_drop,
)


def _silent_errors(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Sweep the live runtime for errors parked in fields NOTHING reads.

    Born 2026-08-22 (Zeke: "make the logs tell you what happened"). The
    static-trim v2 branch raised NameError on EVERY execution for a full day.
    Nothing surfaced it: ptz_audit.jsonl recorded 383/383 head commands
    ok=true, attention_report said idle-no-error, iris_health was green. The
    exception sat in st["error"] — a dict key with no reader — while the head
    swung 136 deg and hit the tilt ceiling.

    The class of bug: a `except Exception as e: state["error"] = repr(e)`
    handler that lets the loop keep running. Every one of those is a silent
    failure waiting to happen. This walks the runtime state dict for them.

    params: {"deep": bool} also tails the ptz audit for error records.
            {"window_s": float} audit lookback (default 3600)."""
    import json
    import time
    from pathlib import Path

    ERR_KEYS = ("error", "last_error", "err", "error_count", "last_exc",
                "warn", "warning")
    out: dict[str, Any] = {"ok": True, "findings": []}

    def _looks_bad(k: str, v: Any) -> bool:
        if k not in ERR_KEYS:
            return False
        if v is None or v is False:
            return False
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v > 0          # error_count style
        if isinstance(v, str):
            return bool(v.strip())
        return bool(v)

    seen: set[int] = set()

    def _walk(obj: Any, path: str, depth: int) -> None:
        if depth > 3 or id(obj) in seen:
            return
        if not isinstance(obj, dict):
            return
        seen.add(id(obj))
        for k, v in list(obj.items()):
            kk = str(k)
            if _looks_bad(kk, v):
                out["findings"].append({
                    "where": f"{path}.{kk}" if path else kk,
                    "value": (v[:300] if isinstance(v, str) else v),
                })
            elif isinstance(v, dict) and not kk.startswith("__"):
                _walk(v, f"{path}.{kk}" if path else kk, depth + 1)

    try:
        _walk(g, "", 0)
    except Exception as e:
        out["walk_error"] = f"{type(e).__name__}: {e}"

    # The audit trail's own error records (servo_error / est_drift / ok=false)
    if params.get("deep", True):
        window = float(params.get("window_s") or 3600.0)
        p = Path(__file__).resolve().parents[2] / "state" / "attention" / "ptz_audit.jsonl"
        hits: list[dict[str, Any]] = []
        try:
            if p.exists():
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-4000:]
                now = time.time()
                for line in tail:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if now - float(rec.get("ts") or 0) > window:
                        continue
                    if rec.get("ok") is False or "error" in str(rec.get("kind")) \
                            or rec.get("kind") == "est_drift":
                        hits.append({k: rec.get(k) for k in
                                     ("kind", "error", "raised_at", "drift_pan",
                                      "drift_tilt", "mode") if rec.get(k) is not None})
        except Exception as e:
            out["audit_error"] = f"{type(e).__name__}: {e}"
        # collapse duplicates — a 12Hz loop repeats itself
        uniq: dict[str, dict[str, Any]] = {}
        for h in hits:
            key = json.dumps(h, sort_keys=True, default=str)
            if key in uniq:
                uniq[key]["_count"] = uniq[key].get("_count", 1) + 1
            else:
                uniq[key] = dict(h, _count=1)
        out["audit_errors"] = list(uniq.values())[:20]
        out["audit_error_records"] = len(hits)

    out["finding_count"] = len(out["findings"])
    out["clean"] = not out["findings"] and not out.get("audit_errors")
    if out["clean"]:
        out["note"] = "no parked errors found — but absence here only covers " \
                      "dicts reachable from g and the ptz audit trail"
    return out


register_tool(
    name="silent_errors",
    description=(
        "Sweep the live runtime for exceptions parked in state fields nothing reads "
        "(the `except: state['error']=e` class) plus error/est_drift records in the "
        "PTZ audit trail. Born after static-trim v2 raised NameError on every run "
        "for a day while every health surface read green. Tier 1."
    ),
    tier=1,
    handler=_silent_errors,
)


def _hands_rest(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Smart on/off for the hands/gesture step (Zeke 2026-07-13: 'if it's too many
    programs that take up too much VRAM then we need a smart on/off switch').
    The step is CPU-only (zero VRAM) and already person-gated, so this flag is
    the manual override on top: rest=true stops ALL hand compute regardless.
    The camera loop re-reads g['_hands_enabled'] every gated frame — live."""
    rest = bool(params.get("rest", True))
    g["_hands_enabled"] = not rest
    if rest:
        g["_hand_results"] = None
    return {"ok": True, "hands_enabled": g["_hands_enabled"],
            "note": ("hand/gesture step OFF (no hand compute at all)" if rest else
                     "hand/gesture step ON (runs only while someone is in frame)")}


register_tool(
    name="hands_rest",
    description=(
        "Pause (rest=true) or resume (rest=false) the camera loop's hand/gesture step. "
        "It's CPU-only and person-gated already; this is the manual override. Tier 1."
    ),
    tier=1,
    handler=_hands_rest,
)


register_tool(
    name="wire_voice_input_routes",
    description=(
        "Repair/activate: register the ear-mute endpoints (/api/v1/voice_input GET, "
        "/api/v1/voice_input/toggle POST) on the RUNNING orb_http app after adding them "
        "to brain/orb_http.py. Reloads the module, restores reload-wiped state, adds "
        "routes to the served app. Idempotent. Tier 2."
    ),
    tier=2,
    handler=_wire_voice_input_routes,
)


def _restart_orb_http(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Restart the orb HTTP listener (2026-07-17: the uvicorn daemon thread
    DIED during a token freeze — :5876 had no listener, the orb app sat in
    SYN_SENT forever, app showed 'Iris offline'; rebind_orb_http_state was
    healthy because the STATE was fine — only the SERVER thread was dead).
    Safe: if a listener already answers on :5876 this is a no-op; otherwise
    re-runs brain.orb_http.start() (port-probe guarded) in this process."""
    import socket
    import time as _t

    out: dict[str, Any] = {"ok": True}

    def _probe() -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", 5876))
            return True
        except OSError:
            return False
        finally:
            s.close()

    if _probe():
        out["was_listening"] = True
        out["note"] = "listener already alive — nothing to do"
        return out
    out["was_listening"] = False
    try:
        import brain.orb_http as m
        root = Path(g.get("BASE_DIR") or ".")
        m._g = g                     # belt: state rebind rides along
        m.start(g, root, tts=getattr(m, "_tts_ref", None))
        _t.sleep(1.5)
        out["listening_now"] = _probe()
        if not out["listening_now"]:
            out["ok"] = False
            out["error"] = ("start() returned but :5876 still not answering — "
                            "check stderr for [orb_http] lines")
    except Exception as e:
        import traceback
        out["ok"] = False
        out["error"] = repr(e)
        out["traceback"] = traceback.format_exc()[-1500:]
    return out


register_tool(
    name="restart_orb_http",
    description=(
        "Repair: restart the orb HTTP listener thread on :5876 after it dies "
        "(orb app shows 'Iris offline', connections SYN_SENT, no LISTENING). "
        "No-op if a listener already answers. Tier 2."
    ),
    tier=2,
    handler=_restart_orb_http,
)


register_tool(
    name="rebind_orb_http_state",
    description=(
        "Repair: re-point brain.orb_http's module _g at the live runtime state dict and "
        "reclaim state/iris.pid. Use after any reload of brain.orb_http leaves the snapshot "
        "serving empty state (pointing=false, uptime 0H 0M). Idempotent. Tier 1."
    ),
    tier=1,
    handler=_rebind_orb_http_state,
)
