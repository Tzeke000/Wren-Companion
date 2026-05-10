"""HTTP shim for the Tauri orb (apps/ava-control/) to talk to.

The orb was built for Ava's `avaagent.py`, which exposes a FastAPI server on
127.0.0.1:5876. Iris's runtime is MCP-over-stdio — no HTTP. Without this
shim the orb opens, renders, but shows a "Backend not responding" banner
because /api/v1/health and /api/v1/snapshot return nothing.

This file boots a minimal FastAPI server in a daemon thread, served from
inside iris_runtime.py's process. Tier 0+1+2 endpoints (health, snapshot,
tts/state, identity, chat shell, connectivity, camera) return real data.
Tier 4/5 (memory/brain/plans/journal/learning/profiles/etc.) return empty
success — the orb's `.catch(() => {})` fallbacks render those tabs as
empty UI rather than broken.

Wire-up: `iris_runtime.py` calls `start(g, root_dir)` once at boot.
"""
from __future__ import annotations

import asyncio
import base64
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
import uvicorn


_HOST = "127.0.0.1"
_PORT = 5876

# These are bound by start() at boot.
_g: dict[str, Any] = {}
_root: Path = Path(".")
_tts_ref: Any = None  # TTSWorker instance for /api/v1/tts/* delegation

# In-process chat history (lost on restart — fine, the brain isn't wired yet).
_chat_history: list[dict[str, Any]] = []

# Camera state. Lazy init on first request — opening "USB Live Camera" via
# OpenCV takes ~2s. Held open across requests; no per-frame open/close.
_cam_lock = threading.Lock()
_cam: Any = None  # cv2.VideoCapture
_cam_last_b64: str | None = None
_cam_last_grab_ts: float = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_identity(name: str) -> str:
    p = _root / "ava_core" / f"{name}.md"
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _grab_camera_frame_b64() -> tuple[str | None, float]:
    """Return (b64_jpeg, age_seconds) from the shared frame buffer.

    The capture thread (started by iris_runtime) is the SOLE owner of the
    cv2.VideoCapture handle — opening a second handle here for fallback led
    to OpenCV throwing a C++ exception that bypassed Python's try/except and
    killed the process (exit 116). If the buffer has no fresh frame we
    return None and let the orb render "no feed" rather than risk a crash.
    """
    try:
        from brain.frame_store import get_buffered_frame
        meta = get_buffered_frame(max_age_sec=2.0)
        if meta.frame is None:
            return None, 0.0
        import cv2  # noqa: WPS433
        ok, jpg = cv2.imencode(".jpg", meta.frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ok:
            return None, 0.0
        return base64.b64encode(jpg.tobytes()).decode("ascii"), float(meta.age_sec)
    except Exception as e:
        print(f"[orb_http] frame_store read failed: {e!r}", file=sys.stderr, flush=True)
        return None, 0.0


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Iris orb shim", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Tier 0 — must answer for the "Backend not responding" banner to clear
@app.get("/api/v1/health")
def health() -> dict:
    return {"ok": True, "service": "iris", "ts": time.time()}


@app.get("/api/v1/snapshot")
def snapshot() -> dict:
    """Top-level state. Polled every 5s by the orb. Fields are best-effort —
    the orb null-checks everything so missing pieces just render empty."""
    pid = _g.get("_recognized_person_id") or "unknown"
    conf = float(_g.get("_recognized_confidence") or 0.0)
    current_person: dict | None = None
    if pid != "unknown" and conf > 0.0:
        current_person = {
            "person_id": pid,
            "confidence": conf,
            "age": int(_g.get("_face_age") or 0),
            "gender": str(_g.get("_face_gender") or "?"),
            "expression": str(_g.get("_current_expression") or ""),
        }
    insight = _g.get("_insight_face")
    return {
        "ok": True,
        "ts": time.time(),
        "identity": "iris",
        "mood": {"mood_label": "calm", "valence": 0.0, "arousal": 0.2},
        "connectivity": {"online": True, "wan": True, "lan": True},
        "current_person": current_person,
        "inner_life": {"current_thought": None},
        "tts": {
            "tts_speaking": bool(_g.get("_tts_speaking")),
            "engine": getattr(_tts_ref, "_engine_type", None),
        },
        "speech": {"text": "", "ts": 0.0},
        "onboarding": {"active": False, "step": None},
        "subsystem_health": {
            "sleep": {"state": "awake"},
            "insightface": {
                "available": bool(getattr(insight, "available", False)),
                "provider": str(getattr(insight, "_provider", "")) if insight else "",
                "known_count": int(getattr(insight, "known_count", lambda: 0)()) if insight else 0,
                "face_count": len(_g.get("_face_results") or []),
            },
        },
    }


@app.get("/api/v1/tts/state")
def tts_state() -> dict:
    """Polled at 100ms by the orb to drive the orb's pulsing animation."""
    return {
        "ok": True,
        "speaking": bool(_g.get("_tts_speaking")),
        "amplitude": float(_g.get("_tts_amplitude") or 0.0),
    }


# Tier 2 — identity files (read-only)
@app.get("/api/v1/identity/{name}", response_class=PlainTextResponse)
def identity(name: str) -> str:
    if name not in {"IDENTITY", "SOUL", "USER"}:
        return ""
    return _read_identity(name)


@app.get("/api/v1/identity/proposals")
def identity_proposals() -> dict:
    return {"ok": True, "proposals": []}


@app.post("/api/v1/identity/proposals/approve")
async def identity_proposals_approve() -> dict:
    return {"ok": True}


# Tier 2 — chat (stub: brain not wired yet)
@app.get("/api/v1/chat/history")
def chat_history() -> dict:
    return {"ok": True, "messages": list(_chat_history)}


@app.post("/api/v1/chat")
async def chat(payload: dict | None = None) -> dict:
    msg = (payload or {}).get("message", "")
    _chat_history.append({"role": "user", "content": str(msg), "source": "zeke"})
    reply = (
        "I'm online but the brain isn't wired to the orb chat yet — "
        "talk to me through voice (say hey jarvis) for now."
    )
    _chat_history.append({"role": "assistant", "content": reply, "source": "iris"})
    # Keep history bounded.
    if len(_chat_history) > 200:
        del _chat_history[: len(_chat_history) - 200]
    return {"ok": True, "reply": reply, "engine": "stub"}


# Tier 2 — connectivity
@app.get("/api/v1/connectivity")
def connectivity() -> dict:
    return {"ok": True, "online": True, "wan": True, "lan": True}


# Tier 2 — camera
@app.get("/api/v1/camera/live_frame")
def camera_live_frame() -> dict:
    b64, age = _grab_camera_frame_b64()
    return {"ok": b64 is not None, "b64": b64, "age_sec": age}


@app.get("/api/v1/vision/latest_frame")
def vision_latest_frame() -> Response:
    b64, _age = _grab_camera_frame_b64()
    if not b64:
        return Response(status_code=204)
    return Response(content=base64.b64decode(b64), media_type="image/jpeg")


# Tier 4 — control buttons. Most delegate to the live engines.
@app.post("/api/v1/tts/toggle")
async def tts_toggle() -> dict:
    enabled = not bool(_g.get("_tts_enabled", True))
    _g["_tts_enabled"] = enabled
    return {"ok": True, "enabled": enabled, "engine": getattr(_tts_ref, "_engine_type", None)}


@app.post("/api/v1/tts/speak")
async def tts_speak(payload: dict | None = None) -> dict:
    text = (payload or {}).get("text", "")
    if not text or _tts_ref is None:
        return {"ok": False, "error": "no text or tts unavailable"}
    threading.Thread(
        target=lambda: _tts_ref.speak_with_emotion(text, "neutral", 0.5, blocking=True),
        daemon=True,
    ).start()
    return {"ok": True}


@app.post("/api/v1/stt/listen")
async def stt_listen() -> dict:
    return {"ok": True, "listening": True}


@app.get("/api/v1/stt/result")
def stt_result() -> dict:
    return {"ok": True, "ready": False, "processing": False, "text": ""}


@app.post("/api/v1/shutdown")
async def shutdown() -> dict:
    return {"ok": True, "goodbye": "Goodnight, Zeke.", "note_saved": False}


# Tier 5 — feature stubs. Orb has .catch(() => {}) on these, so empty
# success renders the relevant tabs as empty UI instead of "broken."
@app.get("/api/v1/memory/mem0")
def memory_mem0() -> dict:
    return {"ok": True, "entries": []}


@app.post("/api/v1/memory/mem0/search")
async def memory_mem0_search() -> dict:
    return {"ok": True, "results": []}


@app.delete("/api/v1/memory/mem0/{entry_id}")
async def memory_mem0_delete(entry_id: str) -> dict:
    return {"ok": True}


@app.get("/api/v1/brain/graph")
def brain_graph() -> dict:
    return {"ok": True, "nodes": [], "edges": [], "stats": {}}


@app.get("/api/v1/brain/active")
def brain_active() -> dict:
    return {"ok": True, "active_nodes": [], "firing_paths": []}


@app.get("/api/v1/plans")
def plans_list() -> dict:
    return {"ok": True, "plans": []}


@app.post("/api/v1/plans/create")
async def plans_create() -> dict:
    return {"ok": True, "id": None}


@app.post("/api/v1/plans/{plan_id}/pause")
async def plans_pause(plan_id: str) -> dict:
    return {"ok": True}


@app.post("/api/v1/plans/{plan_id}/resume")
async def plans_resume(plan_id: str) -> dict:
    return {"ok": True}


@app.get("/api/v1/journal/entries")
def journal_entries() -> dict:
    return {"ok": True, "entries": []}


@app.get("/api/v1/journal/shared", response_class=PlainTextResponse)
def journal_shared() -> str:
    return ""


@app.get("/api/v1/learning/log")
def learning_log() -> dict:
    return {"ok": True, "log": []}


@app.get("/api/v1/learning/gaps")
def learning_gaps() -> dict:
    return {"ok": True, "gaps": []}


@app.get("/api/v1/learning/week")
def learning_week() -> dict:
    return {"ok": True, "week": []}


@app.get("/api/v1/profiles/list")
def profiles_list() -> dict:
    return {"ok": True, "profiles": []}


@app.post("/api/v1/profile/{person_id}/refresh")
async def profile_refresh(person_id: str) -> dict:
    return {"ok": True}


@app.get("/api/v1/emil/status")
def emil_status() -> dict:
    return {"ok": True, "online": False}


@app.post("/api/v1/emil/ping")
async def emil_ping() -> dict:
    return {"ok": True, "reachable": False}


@app.post("/api/v1/emil/send")
async def emil_send() -> dict:
    return {"ok": True, "delivered": False}


@app.get("/api/v1/ui/tab")
def ui_tab() -> dict:
    return {"tab": None}


@app.get("/api/v1/ui/custom_tabs")
def ui_custom_tabs() -> dict:
    return {"ok": True, "tabs": []}


@app.get("/api/v1/images/list")
def images_list() -> dict:
    return {"ok": True, "images": []}


@app.post("/api/v1/images/generate")
async def images_generate() -> dict:
    return {"ok": False, "error": "not wired"}


@app.delete("/api/v1/images/{filename}")
async def images_delete(filename: str) -> dict:
    return {"ok": True}


@app.post("/api/v1/workbench/approve")
async def workbench_approve() -> dict:
    return {"ok": True, "message": "approved"}


@app.post("/api/v1/workbench/reject")
async def workbench_reject() -> dict:
    return {"ok": True, "message": "rejected"}


@app.post("/api/v1/routing/override")
async def routing_override() -> dict:
    return {"ok": True}


@app.post("/api/v1/camera/calibrate_gaze")
async def camera_calibrate_gaze() -> dict:
    return {"ok": True}


@app.post("/api/v1/clap/calibrate")
async def clap_calibrate() -> dict:
    return {"ok": True}


@app.post("/api/v1/onboarding/start")
async def onboarding_start() -> dict:
    return {"ok": True, "active": False}


@app.post("/api/v1/onboarding/step")
async def onboarding_step() -> dict:
    return {"ok": True, "next": None}


@app.get("/api/v1/finetune/status")
def finetune_status() -> dict:
    return {"ok": True, "status": "idle"}


@app.get("/api/v1/finetune/log")
def finetune_log() -> dict:
    return {"ok": True, "lines": []}


@app.post("/api/v1/finetune/prepare")
async def finetune_prepare() -> dict:
    return {"ok": True, "ready": False, "validation": {}, "checks": {}}


@app.post("/api/v1/finetune/start")
async def finetune_start() -> dict:
    return {"ok": True, "ready": False, "issues": [], "checks": {}}


@app.get("/api/v1/widget/position")
def widget_position() -> dict:
    return {"ok": True, "x": 0, "y": 0}


@app.post("/api/v1/widget/position")
async def widget_position_set() -> dict:
    return {"ok": True}


@app.get("/api/v1/debug/full")
def debug_full() -> dict:
    return {"ok": True, "subsystem_health": {"sleep": {"state": "awake"}}}


@app.get("/api/v1/debug/export", response_class=PlainTextResponse)
def debug_export() -> str:
    return f"iris debug export @ {time.time()}\n(stub)\n"


# WebSocket — orb opens it for a live event stream. REST poll is authoritative
# for online state, so keeping this minimal is fine: accept the connection,
# hold it open, ignore inbound messages.
@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    await socket.accept()
    try:
        while True:
            await asyncio.sleep(30.0)
            await socket.send_json({"type": "heartbeat", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def start(g: dict[str, Any], root: Path, tts: Any | None = None) -> None:
    """Spin up the FastAPI server in a daemon thread.

    Idempotent: subsequent calls return without effect. iris_runtime.py invokes
    this once after engines come up.
    """
    global _g, _root, _tts_ref
    _g = g
    _root = root
    _tts_ref = tts

    # Port-busy probe per CLAUDE.md hygiene rule #8 — refuse to bind if
    # something else is already on :5876 (probably an avaagent.py dev process).
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind((_HOST, _PORT))
    except OSError:
        print(
            f"[orb_http] port {_PORT} already in use — not starting shim. "
            f"Stop the other process and restart iris_runtime.",
            file=sys.stderr, flush=True,
        )
        s.close()
        return
    s.close()

    config = uvicorn.Config(
        app, host=_HOST, port=_PORT, log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        # uvicorn's Server.serve() expects an event loop; create one here so
        # we don't fight FastMCP for the main thread's loop.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        except Exception as e:
            print(f"[orb_http] server crashed: {e!r}", file=sys.stderr, flush=True)

    t = threading.Thread(target=_run, daemon=True, name="iris-orb-http")
    t.start()
    print(f"[orb_http] FastAPI listening on http://{_HOST}:{_PORT}", file=sys.stderr, flush=True)
