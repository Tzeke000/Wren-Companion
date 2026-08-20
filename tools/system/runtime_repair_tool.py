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
