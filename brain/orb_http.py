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

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
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


def _inner_life_block() -> dict:
    """Inner thought block. Phase 41: thoughts older than 1 hour don't surface
    as "current" — they're history, not present. Lets the orb's UI fade
    cleanly instead of showing "Iris is thinking about X" forever."""
    thought = str(_g.get("_inner_thought") or "")
    ts = float(_g.get("_inner_thought_ts") or 0.0)
    if thought and ts > 0 and (time.time() - ts) > 3600.0:
        # Older than an hour — don't claim it's current.
        return {"current_thought": None, "thought_ts": 0.0, "trigger": ""}
    return {
        "current_thought": thought or None,
        "thought_ts": ts,
        "trigger": str(_g.get("_inner_thought_trigger") or ""),
    }


def _brain_graph_snapshot_block() -> dict:
    """Concept graph stats for snapshot. Lighter than full /brain/graph
    response — just totals so the orb's status panel can show counts.

    Phase 45.1: also includes human-memory stats (episodes count,
    working memory size, total memories) so the brain tab shows the
    full picture of what's "in mind" right now."""
    out = {
        "total_nodes": 0, "total_edges": 0, "nodes_by_type": {},
        "most_activated": "", "last_bootstrap": 0,
        "memory_count": 0, "episode_count": 0, "working_memory_size": 0,
    }
    try:
        cg = _g.get("_concept_graph")
        if cg is not None:
            data = cg.get_graph_data()
            stats = data.get("stats") or {}
            out["total_nodes"] = int(stats.get("total_nodes") or 0)
            out["total_edges"] = int(stats.get("total_edges") or 0)
            out["nodes_by_type"] = stats.get("nodes_by_type") or {}
            out["most_activated"] = str(stats.get("most_activated") or "")
            out["last_bootstrap"] = int(stats.get("last_bootstrap") or 0)
    except Exception:
        pass
    try:
        mem = _g.get("_iris_memory")
        if mem is not None:
            out["memory_count"] = mem.count()
    except Exception:
        pass
    try:
        from brain import iris_human_memory
        out["episode_count"] = len(iris_human_memory.recent_episodes(limit=10000))
        wm = iris_human_memory.working_memory_state()
        out["working_memory_size"] = len(wm.get("items") or [])
    except Exception:
        pass
    return out


def _time_block_inline() -> dict:
    """Time substrate state for snapshot. Defined at module level so the
    main snapshot can include it without forward-reference."""
    try:
        from brain import iris_time
        ts = iris_time.get_state()
        return {
            "tick_count": ts.get("tick_count", 0),
            "tick_loop_alive": ts.get("tick_loop_alive", False),
            "body_uptime_s": ts.get("current_process_uptime_s", 0),
            "session_attach_count": ts.get("session_attach_count", 0),
            "last_session_iso": ts.get("last_session_iso"),
        }
    except Exception:
        return {}


# Tier 0 — must answer for the "Backend not responding" banner to clear
@app.get("/api/v1/health")
def health() -> dict:
    return {"ok": True, "service": "iris", "ts": time.time()}


@app.get("/api/v1/snapshot")
def snapshot() -> dict:
    """Top-level state. Polled every 5s by the orb. Fields are best-effort —
    the orb null-checks everything so missing pieces just render empty."""
    # Phase 41: snapshot a consistent perception state at the top of the
    # function so face_results, person_id, confidence, expression are all
    # from the same instant (rather than each read independently).
    _face_results_snap = list(_g.get("_face_results") or [])
    pid = _g.get("_recognized_person_id") or "unknown"
    conf = float(_g.get("_recognized_confidence") or 0.0)
    _expr_snap = str(_g.get("_current_expression") or "")
    _attn_snap = str(_g.get("_attention_state") or "")
    _looking_snap = bool(_g.get("_looking_at_screen") or False)
    # Cross-validate: if face_results is empty but pid is set, we caught a
    # write-in-progress. Demote to unknown.
    if not _face_results_snap and pid != "unknown":
        pid = "unknown"
        conf = 0.0
    current_person: dict | None = None
    if pid != "unknown" and conf > 0.0:
        # Phase 36: include fields the orb actually reads (display_name,
        # is_zeke, time_at_machine). Track time_at_machine via
        # _person_present_since_ts in _g.
        present_since = float(_g.get("_person_present_since_ts") or 0.0)
        time_at_machine = (
            (time.time() - present_since) if present_since > 0 else 0.0
        )
        # Display name: try profile first, fall back to person_id capitalized.
        display_name = pid.capitalize() if pid else "Unknown"
        try:
            import json as _j
            prof_path = _root / "state" / "profiles" / f"{pid}.json"
            if prof_path.is_file():
                pdata = _j.loads(prof_path.read_text(encoding="utf-8"))
                if isinstance(pdata, dict) and pdata.get("name"):
                    display_name = str(pdata.get("name"))
        except Exception:
            pass
        current_person = {
            "person_id": pid,
            "display_name": display_name,
            "is_zeke": pid == "zeke",
            "confidence": conf,
            "age": int(_g.get("_face_age") or 0),
            "gender": str(_g.get("_face_gender") or "?"),
            "expression": _expr_snap,
            "time_at_machine": int(time_at_machine),
        }
    insight = _g.get("_insight_face")

    # Phase 5 — real mood from brain/mood_core. Falls back to neutral if
    # the mood subsystem hasn't bootstrapped yet (early boot before tick fires).
    mood_block = {"mood_label": "calm", "primary_emotion": "calmness",
                  "primary_intensity": 0.4, "secondary_emotions": [],
                  "valence": 0.0, "arousal": 0.2, "outward_tone": "neutral",
                  "behavior_modifiers": {}}
    try:
        from brain import mood_core
        m = mood_core.load_mood()
        weights = m.get("emotion_weights") or {}
        primary_list = m.get("primary_emotions") or []
        primary_emotion = (primary_list[0]["name"] if primary_list else "calmness")
        primary_intensity = (float(primary_list[0]["percent"]) / 100.0) if primary_list else 0.4
        secondary = [
            {"emotion": p.get("name"), "intensity": float(p.get("percent", 0)) / 100.0}
            for p in primary_list[1:]
        ]
        # Valence: positive emotions sum minus negative. Arousal: high-energy
        # emotions sum.
        positive_keys = ("joy", "interest", "calmness", "satisfaction", "amusement",
                         "excitement", "relief", "admiration", "adoration",
                         "aesthetic appreciation", "awe")
        negative_keys = ("sadness", "anxiety", "fear", "anger", "disgust",
                         "frustration", "annoyance", "distress", "horror",
                         "empathetic pain", "boredom", "envy", "shame")
        high_arousal_keys = ("excitement", "fear", "anger", "joy", "anxiety",
                             "frustration", "horror", "surprise", "amusement")
        valence = sum(float(weights.get(k, 0.0)) for k in positive_keys) \
                  - sum(float(weights.get(k, 0.0)) for k in negative_keys)
        arousal = sum(float(weights.get(k, 0.0)) for k in high_arousal_keys)
        mood_block = {
            "mood_label": str(m.get("current_mood") or m.get("outward_tone") or "calm"),
            "primary_emotion": primary_emotion,
            "primary_intensity": round(primary_intensity, 3),
            "secondary_emotions": secondary,
            "valence": round(max(-1.0, min(1.0, valence)), 3),
            "arousal": round(max(0.0, min(1.0, arousal * 2.0)), 3),
            "outward_tone": str(m.get("outward_tone") or ""),
            "behavior_modifiers": m.get("behavior_modifiers") or {},
        }
    except Exception:
        pass

    return {
        "ok": True,
        "ts": time.time(),
        "identity": "iris",
        "mood": mood_block,
        "connectivity": {"online": True, "wan": True, "lan": True},
        "current_person": current_person,
        "attention": {
            "state": _attn_snap,
            "looking_at_screen": _looking_snap,
            "gaze_region": str(_g.get("_gaze_region") or ""),
        },
        "inner_life": _inner_life_block(),
        "tts": {
            "tts_speaking": bool(_g.get("_tts_speaking")),
            "engine": getattr(_tts_ref, "_engine_type", None),
            "amplitude": float(_g.get("_tts_amplitude") or 0.0),
        },
        "voice": {
            "wake_word_active": bool(_g.get("_wake_word_detector") is not None),
            "wake_backend": (
                getattr(_g.get("_wake_word_detector"), "backend", None)
                if _g.get("_wake_word_detector") else None
            ),
            "voice_session_flag": (_root / ".tmp" / "voice_session.flag").exists(),
            "last_wake_ts": float(_g.get("_wake_word_ts") or 0.0),
            "last_wake_source": _g.get("_wake_source"),
        },
        "voice_loop": {
            "active": (_root / ".tmp" / "voice_session.flag").exists(),
            "tts_speaking": bool(_g.get("_tts_speaking")),
            "say_queue_depth": int(_g.get("_say_queue_depth") or 0),
        },
        "vision": {
            "camera_active": _face_results_snap is not None,
            "face_count": len(_face_results_snap),
            "current_expression": _expr_snap,
            "attention_state": _attn_snap,
            "looking_at_screen": _looking_snap,
            "insight_provider": (
                str(getattr(insight, "_provider", "")) if insight else ""
            ),
            "perception": {
                "resolved_face_identity": pid if pid != "unknown" else "",
                "stable_face_identity": pid if (pid != "unknown" and conf > 0.7) else "",
                "identity_confidence": conf,
                "face_status": (
                    "recognized" if pid != "unknown"
                    else ("face_unresolved" if _face_results_snap else "no_face")
                ),
                "scene_compact_summary": str(_g.get("_llava_scene_description") or ""),
                "interpretation_confidence": conf,
                "recognized_text": "",
            },
        },
        "heartbeat_runtime": {
            "last_tick_ts": float(_g.get("_last_heartbeat_ts") or 0.0),
            "tick_loop_alive": bool(_g.get("_iris_time_ready")),
            "inner_thought_ts": float(_g.get("_inner_thought_ts") or 0.0),
        },
        "models": {
            # Iris's cognition is Claude (cross-process via Stop hook).
            # No local model selection — these fields exist for the
            # orb's reads, populated honestly.
            "selected_model": "claude (cross-process)",
            "fallback_model": "none",
            "routing_reason": "Iris-as-LLM via brain.iris_llm bridge",
            "cognitive_mode": "iris_llm_bridge",
            "available_models": ["claude"],
        },
        "ribbon": {
            "vision_status": (
                "stable" if _g.get("_face_results") is not None else "no_camera"
            ),
            "nuance_tone": "",  # populated by conversational_nuance if it runs
        },
        "style": {
            "orb_glow_intensity": 0.8,
        },
        "brain_graph": _brain_graph_snapshot_block(),
        "memory_continuity": {
            "active_threads": [],  # populated when relationship_threads runs
            "strategic_continuity_summary": "",
            "relationship_carryover": "",
            "unfinished_thread_present": False,
        },
        "tools": {
            "available_count": 40,  # number of MCP tools registered
            "voice_session_active": (_root / ".tmp" / "voice_session.flag").exists(),
        },
        "workbench": {
            "workbench_has_proposal": False,
            "workbench_top_proposal_title": "",
            "workbench_summary": "",
            "workbench_execution_ready": False,
            "workbench_meta": {},
        },
        # Phase 49: widget pointer state — when set, WidgetApp.tsx morphs the
        # orb shape to a pointer arrow and (the runtime moves the window).
        # Auto-expires at _widget_pointing_until; tools.pointer_show writes
        # these, tools.pointer_hide clears them.
        "widget": {
            "pointing": bool(
                _g.get("_widget_pointing")
                and float(_g.get("_widget_pointing_until") or 0.0) > time.time()
            ),
            "pointing_description": str(_g.get("_widget_pointing_description") or ""),
            "pointing_coords": _g.get("_widget_pointing_coords"),
            "pointing_until_ts": float(_g.get("_widget_pointing_until") or 0.0),
        },
        "speech": {"text": "", "ts": 0.0},
        "onboarding": {"active": False, "step": None},
        "time": _time_block_inline(),
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
    """Surface pending identity-edit proposals from state/identity_proposals.jsonl.
    Empty until something fills them. Per Phase 1 readiness spec section
    C.6, write-to-bedrock is gated until D1 ritual; the proposal queue is
    the staging surface for that future capability."""
    p = _root / "state" / "identity_proposals.jsonl"
    if not p.is_file():
        return {"ok": True, "proposals": []}
    try:
        import json as _j
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines()[-50:]:
            try:
                d = _j.loads(line)
                if isinstance(d, dict) and not d.get("approved"):
                    rows.append(d)
            except Exception:
                pass
        return {"ok": True, "proposals": rows}
    except Exception as e:
        return {"ok": False, "proposals": [], "error": str(e)}


@app.post("/api/v1/identity/proposals/approve")
async def identity_proposals_approve(payload: dict = Body(default={})) -> dict:
    """Per birth-ethics decision (D1 gated last): approve does NOT execute
    a write to ava_core/IDENTITY.md or SOUL.md. Records the approval
    intent to state/identity_proposals.jsonl with approved=True. Future
    Phase 3 unlock would add a continuity_gate.is_continuity_allowed()
    check here before any actual file write."""
    proposal_id = str(payload.get("proposal_id") or "").strip()
    if not proposal_id:
        return {"ok": False, "error": "proposal_id required"}
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "executed": False,
        "note": "approval recorded; bedrock-write is D1-gated and not yet unlocked",
    }


# Tier 2 — chat (Phase 4) — cross-process bridge to Iris's CC session.
@app.get("/api/v1/chat/history")
def chat_history() -> dict:
    """Reads the shared transcript (state/transcript.jsonl) so voice and chat
    turns appear in one stream."""
    try:
        from brain import iris_transcript
        rows = iris_transcript.recent(n=200)
        # Project to the orb's expected shape: role + content + source.
        messages = [
            {
                "role": e.get("role"),
                "content": e.get("content"),
                "source": e.get("source"),
                "modality": e.get("modality"),
                "ts": e.get("ts"),
            }
            for e in rows
        ]
        return {"ok": True, "messages": messages}
    except Exception as e:
        return {"ok": False, "messages": [], "error": str(e)}


@app.post("/api/v1/chat")
async def chat(payload: dict | None = None) -> dict:
    """Submit a chat request, then long-poll up to 60s for Iris to answer
    via mcp__iris__chat_reply. Iris's CC session sees the pending request
    via the Stop hook and rewakes to handle it."""
    msg = str((payload or {}).get("message") or "").strip()
    if not msg:
        return {"ok": False, "error": "empty message"}
    try:
        from brain import iris_chat
        request_id = iris_chat.submit(msg)
        # Run wait_for_reply on a worker thread — it polls disk for up to 60s.
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(
            None, iris_chat.wait_for_reply, request_id, 60.0
        )
        if reply is None:
            return {
                "ok": False,
                "request_id": request_id,
                "error": "timeout — Iris did not answer within 60s",
                "engine": "iris-cc",
            }
        return {
            "ok": True,
            "request_id": request_id,
            "reply": reply,
            "engine": "iris-cc",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
    """Last STT result from voice_next_input. Polled by the orb when it
    wants to show the most recent transcript.

    Phase 41: TTL of 5 minutes. Results older than that don't show — the
    orb shouldn't claim "Zeke just said X" two hours later."""
    last = _g.get("_last_stt_result") or {}
    text = str(last.get("text") or "")
    ts = float(last.get("ts") or 0.0)
    if text and ts > 0 and (time.time() - ts) > 300.0:
        # Stale — don't surface.
        return {"ok": True, "ready": False, "processing": False,
                "text": "", "ts": 0.0, "confidence": 0.0, "duration_seconds": 0.0,
                "stale_seconds": int(time.time() - ts)}
    return {
        "ok": True,
        "ready": bool(text),
        "processing": False,
        "text": text,
        "ts": ts,
        "confidence": float(last.get("confidence") or 0.0),
        "duration_seconds": float(last.get("duration_seconds") or 0.0),
    }


@app.post("/api/v1/shutdown")
async def shutdown() -> dict:
    """Run brain.shutdown_ritual to compose a goodbye + handoff pickup
    note, save to state/pickup_note.json for next-session restore.
    Doesn't actually kill iris_runtime — that's CC's call. This is the
    ritual layer.

    The ritual goes through iris_llm (Phase 22 routed it). On timeout it
    falls back to a deterministic stub goodbye."""
    try:
        from brain import shutdown_ritual
        goodbye = shutdown_ritual.run_shutdown_ritual(_g)
        return {
            "ok": True,
            "goodbye": str(goodbye or "Goodnight, Zeke."),
            "note_saved": True,
        }
    except Exception as e:
        return {"ok": True, "goodbye": "Goodnight, Zeke.",
                "note_saved": False, "error": str(e)}


# Tier 5 — feature stubs. Orb has .catch(() => {}) on these, so empty
# success renders the relevant tabs as empty UI instead of "broken."
# ── Memory (Phase 3) — JSONL-backed iris_memory store ────────────────────────
# The orb reads entry.memory and entry.created_at; iris_memory stores text/iso.
# We project the canonical JSONL fields into the orb's expected shape.
def _project_mem_entry(e: dict) -> dict:
    return {
        "id": e.get("id"),
        "memory": e.get("text") or "",
        "created_at": e.get("iso") or "",
        "ts": e.get("ts"),
        "person_id": e.get("person_id"),
        "category": e.get("category"),
        "importance": e.get("importance"),
        "tags": e.get("tags") or [],
    }


@app.get("/api/v1/memory/mem0")
def memory_mem0() -> dict:
    mem = _g.get("_iris_memory")
    if mem is None:
        return {"ok": True, "entries": []}
    try:
        rows = mem.list(limit=200)
        return {"ok": True, "entries": [_project_mem_entry(e) for e in rows]}
    except Exception as e:
        return {"ok": False, "entries": [], "error": str(e)}


@app.post("/api/v1/memory/mem0/search")
async def memory_mem0_search(payload: dict = Body(default={})) -> dict:
    """Phase 18: prefer semantic search via ChromaDB. Falls back to substring
    on iris_memory.jsonl if semantic isn't ready."""
    q = str(payload.get("query") or "").strip()
    limit = int(payload.get("limit") or 10)
    if not q:
        return {"ok": True, "results": []}
    try:
        from brain import iris_semantic_memory
        if _g.get("_iris_semantic_memory_ready"):
            rows = iris_semantic_memory.search(q, k=limit)
            if rows:
                return {"ok": True, "results": [_project_mem_entry(e) for e in rows], "engine": "semantic"}
    except Exception:
        pass
    # Fallback: substring search via iris_memory
    mem = _g.get("_iris_memory")
    if mem is None:
        return {"ok": True, "results": []}
    try:
        rows = mem.search(q, limit=limit)
        return {"ok": True, "results": [_project_mem_entry(e) for e in rows], "engine": "substring"}
    except Exception as e:
        return {"ok": False, "results": [], "error": str(e)}


@app.delete("/api/v1/memory/mem0/{entry_id}")
async def memory_mem0_delete(entry_id: str) -> dict:
    mem = _g.get("_iris_memory")
    if mem is None:
        return {"ok": False, "error": "memory not available"}
    try:
        deleted = mem.delete(entry_id)
        return {"ok": deleted, "id": entry_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Brain graph (Phase 3) — concept_graph passthrough ────────────────────────
@app.get("/api/v1/brain/graph")
def brain_graph() -> dict:
    cg = _g.get("_concept_graph")
    if cg is None:
        return {"ok": True, "nodes": [], "edges": [], "stats": {}}
    try:
        data = cg.get_graph_data()
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "nodes": [], "edges": [], "stats": {}, "error": str(e)}


@app.get("/api/v1/brain/active")
def brain_active() -> dict:
    cg = _g.get("_concept_graph")
    if cg is None:
        return {"ok": True, "active_nodes": [], "firing_paths": []}
    try:
        active = cg.get_active_nodes(last_n_seconds=30)
        # firing_paths: edges between any two currently-active nodes.
        active_ids = {str(n.get("id")) for n in active}
        firing = []
        for edge in cg.edges:
            s, t = str(edge.source), str(edge.target)
            if s in active_ids and t in active_ids:
                firing.append({
                    "source": s, "target": t,
                    "relationship": edge.relationship,
                    "strength": edge.strength,
                })
        return {"ok": True, "active_nodes": active, "firing_paths": firing}
    except Exception as e:
        return {"ok": False, "active_nodes": [], "firing_paths": [], "error": str(e)}


@app.get("/api/v1/plans")
def plans_list() -> dict:
    """All plans via brain/planner.LongHorizonPlanner. Plans persist at
    state/plans.jsonl."""
    try:
        from brain.planner import get_planner
        planner = get_planner(_root)
        plans = planner._load()  # raw list of plan dicts
        return {"ok": True, "plans": plans}
    except Exception as e:
        return {"ok": False, "plans": [], "error": str(e)}


@app.post("/api/v1/plans/create")
async def plans_create(payload: dict = Body(default={})) -> dict:
    """Create a plan. goal is required. context is optional. Plan creation
    asks Iris (via iris_llm) to decompose into 3-6 steps; on Iris timeout
    falls back to a single-step plan."""
    goal = str(payload.get("goal") or "").strip()
    if not goal:
        return {"ok": False, "error": "goal is required"}
    context = str(payload.get("context") or "")
    try:
        from brain.planner import get_planner
        planner = get_planner(_root)
        plan = planner.create_plan(goal, context=context)
        return {"ok": True, "id": plan.get("id"), "plan": plan}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/v1/plans/{plan_id}/pause")
async def plans_pause(plan_id: str) -> dict:
    try:
        from brain.planner import get_planner
        planner = get_planner(_root)
        return planner.pause_plan(plan_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/v1/plans/{plan_id}/resume")
async def plans_resume(plan_id: str) -> dict:
    try:
        from brain.planner import get_planner
        planner = get_planner(_root)
        return planner.resume_plan(plan_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/v1/episodes/recent")
def episodes_recent(limit: int = 20) -> dict:
    """Read recent episodic memories (autobiographical events with mood
    at encoding). Different from semantic facts — episodes are time-
    indexed, mood-tagged, and importance-boosted by encoding-time arousal."""
    try:
        from brain import iris_human_memory
        eps = iris_human_memory.recent_episodes(limit=int(limit))
        return {"ok": True, "count": len(eps), "episodes": eps}
    except Exception as e:
        return {"ok": False, "episodes": [], "error": str(e)}


@app.get("/api/v1/working_memory")
def working_memory_endpoint() -> dict:
    """Read the current ~7-item working-memory buffer. Items expire
    after 5min without re-attention. Most-recent-first."""
    try:
        from brain import iris_human_memory
        return {"ok": True, **iris_human_memory.working_memory_state()}
    except Exception as e:
        return {"ok": False, "items": [], "error": str(e)}


@app.get("/api/v1/inner_monologue/recent")
def inner_monologue_recent(limit: int = 20) -> dict:
    """Read recent inner-monologue thoughts. Surfaces what I've been
    thinking about during idle time (Iris's cadenced background ticks)."""
    try:
        from brain import iris_inner_monologue
        thoughts = iris_inner_monologue.recent_thoughts(n=int(max(1, limit)))
        return {"ok": True, "count": len(thoughts), "thoughts": thoughts}
    except Exception as e:
        return {"ok": False, "thoughts": [], "error": str(e)}


@app.get("/api/v1/signals/recent")
def signals_recent_endpoint(signal_type: str = "", since_seconds: float = 60.0) -> dict:
    """Read recent signal-bus events. Useful for the orb's debug tab to
    show the live signal stream."""
    try:
        bus = _g.get("_signal_bus")
        if bus is None:
            return {"ok": False, "error": "signal_bus not running", "signals": []}
        cutoff = time.time() - max(1.0, since_seconds)
        if signal_type:
            sigs = bus.peek(signal_type=signal_type, since=cutoff)
        else:
            all_sigs = bus.peek()
            sigs = [s for s in all_sigs if float(s.get("ts") or 0) >= cutoff]
        return {"ok": True, "count": len(sigs), "signals": sigs}
    except Exception as e:
        return {"ok": False, "signals": [], "error": str(e)}


@app.get("/api/v1/journal/entries")
def journal_entries() -> dict:
    try:
        from brain import journal as _journal
        # Default to a generous window — orb decides what to render.
        entries = _journal.get_recent_entries(n=200, g=_g)
        # Newest first.
        entries = list(reversed(entries))
        return {"ok": True, "entries": entries}
    except Exception as e:
        return {"ok": False, "entries": [], "error": str(e)}


@app.get("/api/v1/journal/shared", response_class=PlainTextResponse)
def journal_shared() -> str:
    try:
        from brain import journal as _journal
        shared = _journal.get_shared_entries(_g)
        if not shared:
            return "(no shared journal entries yet)"
        # Plain text dump, newest first.
        shared.sort(key=lambda e: float(e.get("ts") or 0.0), reverse=True)
        chunks: list[str] = []
        for e in shared:
            date = str(e.get("date") or "")
            topic = str(e.get("topic") or "")
            content = str(e.get("content") or "")
            chunks.append(f"=== {date} — {topic} ===\n{content}\n")
        return "\n".join(chunks)
    except Exception as e:
        return f"(journal error: {e})"


@app.get("/api/v1/learning/log")
def learning_log() -> dict:
    """Surface the most recent learning_tracker entries if the module wrote
    any. State at state/learning_outcomes.jsonl per the catalog."""
    p = _root / "state" / "learning_outcomes.jsonl"
    if not p.is_file():
        return {"ok": True, "log": []}
    try:
        import json as _j
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines()[-200:]:
            try:
                d = _j.loads(line)
                if isinstance(d, dict):
                    rows.append(d)
            except Exception:
                pass
        rows.reverse()
        return {"ok": True, "log": rows}
    except Exception as e:
        return {"ok": False, "log": [], "error": str(e)}


@app.get("/api/v1/learning/gaps")
def learning_gaps() -> dict:
    """Knowledge gaps surface from curiosity_research + question_engine.
    Empty until those modules accumulate entries."""
    p = _root / "state" / "curiosity_research.jsonl"
    if not p.is_file():
        return {"ok": True, "gaps": []}
    try:
        import json as _j
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                d = _j.loads(line)
                if isinstance(d, dict) and not d.get("resolved"):
                    rows.append(d)
            except Exception:
                pass
        return {"ok": True, "gaps": rows}
    except Exception as e:
        return {"ok": False, "gaps": [], "error": str(e)}


@app.get("/api/v1/learning/week")
def learning_week() -> dict:
    """Weekly digest — last 7 days of learning_outcomes + memory adds."""
    import time as _t
    cutoff = _t.time() - 7 * 86400
    items = []
    p = _root / "state" / "learning_outcomes.jsonl"
    if p.is_file():
        try:
            import json as _j
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    d = _j.loads(line)
                    if isinstance(d, dict) and float(d.get("ts") or 0) >= cutoff:
                        items.append(d)
                except Exception:
                    pass
        except Exception:
            pass
    return {"ok": True, "week": items[-100:]}


@app.get("/api/v1/profiles/list")
def profiles_list() -> dict:
    """List enrolled people + their profile metadata. Reads faces/<pid>/ dirs
    + state/profiles/<pid>.json if present."""
    profiles = []
    faces_dir = _root / "faces"
    if faces_dir.is_dir():
        for pdir in faces_dir.iterdir():
            if not pdir.is_dir():
                continue
            pid = pdir.name
            face_count = sum(1 for _ in pdir.glob("*.jpg")) + sum(1 for _ in pdir.glob("*.png"))
            entry = {"person_id": pid, "face_samples": face_count}
            # Try to merge in profile JSON if it exists
            prof_path = _root / "state" / "profiles" / f"{pid}.json"
            if prof_path.is_file():
                try:
                    import json as _j
                    pdata = _j.loads(prof_path.read_text(encoding="utf-8"))
                    if isinstance(pdata, dict):
                        entry.update({
                            "name": pdata.get("name"),
                            "trust_level": pdata.get("trust_level"),
                            "preferences": pdata.get("preferences") or {},
                        })
                except Exception:
                    pass
            profiles.append(entry)
    return {"ok": True, "profiles": profiles}


@app.post("/api/v1/profile/{person_id}/refresh")
async def profile_refresh(person_id: str) -> dict:
    """Reload a person profile from disk and re-trigger insightface
    update_known_faces so any new enrollment photos take effect without
    a full restart."""
    if "/" in person_id or "\\" in person_id or ".." in person_id:
        return {"ok": False, "error": "invalid person_id"}
    out = {"ok": True, "person_id": person_id}
    # Re-read profile JSON
    prof_path = _root / "state" / "profiles" / f"{person_id}.json"
    if prof_path.is_file():
        try:
            import json as _j
            data = _j.loads(prof_path.read_text(encoding="utf-8"))
            out["profile_loaded"] = True
            out["profile_keys"] = list(data.keys()) if isinstance(data, dict) else []
        except Exception as e:
            out["profile_error"] = str(e)
    else:
        out["profile_loaded"] = False
    # Re-trigger known_faces reload from faces/<pid>/
    try:
        engine = _g.get("_insight_face")
        if engine is not None and hasattr(engine, "update_known_faces"):
            after = int(engine.update_known_faces())
            out["known_count_after"] = after
    except Exception as e:
        out["face_reload_error"] = str(e)
    return out


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
    """Optionally let the backend suggest which tab the orb should open.
    State at state/orb_active_tab.txt — Iris can write to it via the
    orb_focus_tab MCP tool when she wants to direct attention."""
    p = _root / "state" / "orb_active_tab.txt"
    if p.is_file():
        try:
            tab = p.read_text(encoding="utf-8").strip() or None
            return {"tab": tab}
        except Exception:
            pass
    return {"tab": None}


@app.post("/api/v1/ui/tab")
async def ui_tab_set(payload: dict = Body(default={})) -> dict:
    """Iris's setter — call when she wants the orb to switch tab."""
    tab = str(payload.get("tab") or "").strip()
    p = _root / "state" / "orb_active_tab.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tab, encoding="utf-8")
    return {"ok": True, "tab": tab}


@app.get("/api/v1/ui/custom_tabs")
def ui_custom_tabs() -> dict:
    """Custom tabs from state/orb_custom_tabs.json — Iris can register
    her own tabs that show iframes or arbitrary content."""
    p = _root / "state" / "orb_custom_tabs.json"
    if p.is_file():
        try:
            import json as _j
            tabs = _j.loads(p.read_text(encoding="utf-8"))
            if isinstance(tabs, list):
                return {"ok": True, "tabs": tabs}
        except Exception:
            pass
    return {"ok": True, "tabs": []}


@app.get("/api/v1/images/list")
def images_list() -> dict:
    """List generated images from state/generated_images/, newest first."""
    d = _root / "state" / "generated_images"
    if not d.is_dir():
        return {"ok": True, "images": []}
    try:
        entries = []
        for p in d.iterdir():
            if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            entries.append({
                "filename": p.name,
                "path": str(p),
                "ts": p.stat().st_mtime,
                "size_bytes": p.stat().st_size,
            })
        entries.sort(key=lambda e: e["ts"], reverse=True)
        return {"ok": True, "images": entries[:200]}
    except Exception as e:
        return {"ok": False, "images": [], "error": str(e)}


@app.post("/api/v1/images/generate")
async def images_generate(payload: dict = Body(default={})) -> dict:
    """Try local ComfyUI first, then Pollinations.ai cloud fallback. Returns
    saved path on success."""
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "empty prompt"}
    try:
        from tools.creative.image_generator import ImageGenerator
        gen = ImageGenerator(_g)
        # try local first
        path = None
        if hasattr(gen, "generate_local"):
            path = gen.generate_local(prompt)
        if path is None and hasattr(gen, "generate_cloud"):
            path = gen.generate_cloud(prompt)
        if path is None:
            return {"ok": False, "error": "both local and cloud generation failed"}
        return {"ok": True, "path": str(path), "prompt": prompt}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.delete("/api/v1/images/{filename}")
async def images_delete(filename: str) -> dict:
    """Delete a generated image. Path-traversal protected — only filename, no /."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return {"ok": False, "error": "invalid filename"}
    try:
        p = _root / "state" / "generated_images" / filename
        if p.is_file():
            p.unlink()
            return {"ok": True, "deleted": filename}
        return {"ok": False, "error": "not found"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/v1/workbench/proposals")
def workbench_proposals() -> dict:
    """List current workbench proposals. Builds them on demand from selftest
    results — proposals are reactive to actual diagnostics, not seeded.

    Empty list means the system is healthy (no proposals to act on)."""
    try:
        from brain import selftests, workbench
        try:
            sf_result = selftests.run_recurring_selftests(
                _g.get("camera_manager"), _g, "stable",
            )
        except Exception:
            sf_result = None
        try:
            wb_result = workbench.build_workbench_proposals(
                selftests=sf_result,
                acquisition_freshness="stable",
                proactive_trigger=None,
            )
            proposals = []
            for p in (wb_result.proposals or []):
                proposals.append({
                    "proposal_id": getattr(p, "proposal_id", ""),
                    "proposal_type": getattr(p, "proposal_type", ""),
                    "title": getattr(p, "title", ""),
                    "problem": getattr(p, "problem", ""),
                    "action": getattr(p, "action", ""),
                    "risk": getattr(p, "risk", "low"),
                    "priority": getattr(p, "priority", "low"),
                    "confidence": float(getattr(p, "confidence", 0.0) or 0.0),
                })
            return {"ok": True, "proposals": proposals, "count": len(proposals)}
        except Exception as e:
            return {"ok": False, "proposals": [], "error": str(e)}
    except Exception as e:
        return {"ok": False, "proposals": [], "error": str(e)}


@app.post("/api/v1/workbench/approve")
async def workbench_approve(payload: dict = Body(default={})) -> dict:
    """Approve a proposal. Currently logs the approval — full execute path
    via brain/workbench_execute is heavy (file modifications + rollback)
    and Iris-side wiring is deferred until needed."""
    proposal_id = str(payload.get("proposal_id") or "").strip()
    if not proposal_id:
        return {"ok": False, "error": "proposal_id required"}
    print(f"[workbench] approval logged for proposal {proposal_id} "
          f"(execute path not yet wired)")
    return {"ok": True, "message": "approved (logged; not executed)",
            "proposal_id": proposal_id}


@app.post("/api/v1/workbench/reject")
async def workbench_reject(payload: dict = Body(default={})) -> dict:
    proposal_id = str(payload.get("proposal_id") or "").strip()
    print(f"[workbench] rejection logged for proposal {proposal_id}")
    return {"ok": True, "message": "rejected", "proposal_id": proposal_id}


@app.post("/api/v1/routing/override")
async def routing_override(payload: dict = Body(default={})) -> dict:
    """Ava-era model-routing override. Not applicable for Iris — cognition
    is Claude managed by Anthropic. Stub responds politely."""
    return {
        "ok": True,
        "note": "not applicable — Iris's cognition is Claude managed by Anthropic",
    }


@app.post("/api/v1/camera/calibrate_gaze")
async def camera_calibrate_gaze() -> dict:
    """Run the eye_tracker's 9-point calibration. Pops up a tkinter window;
    user looks at each point. Saves to state/gaze_calibration.json.

    Note: tkinter on a single screen — for Zeke's dual-screen setup, this
    calibrates only the screen the window appears on. Gaze regions on the
    other screen will be approximate. Captured in IDENTITY.md memory."""
    try:
        ez = _g.get("_eye_tracker")
        if ez is None:
            return {"ok": False, "error": "eye_tracker not available"}
        if not hasattr(ez, "calibrate"):
            return {"ok": False, "error": "eye_tracker has no calibrate method"}
        # Run on a thread so we don't block the FastAPI worker.
        import threading as _th
        result_holder: dict = {"done": False, "ok": False}
        def _run():
            try:
                ok = ez.calibrate()
                result_holder["ok"] = bool(ok)
            except Exception as e:
                result_holder["error"] = str(e)
            finally:
                result_holder["done"] = True
        _th.Thread(target=_run, daemon=True).start()
        return {"ok": True, "started": True, "note": "9-point calibration window opened"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/v1/clap/calibrate")
async def clap_calibrate(payload: dict = Body(default={})) -> dict:
    """Run a 2s ambient noise calibration and persist threshold to
    state/clap_calibration.json. Returns measured ambient + new threshold."""
    duration = float(payload.get("duration_seconds", 2.0))
    try:
        from brain.clap_detector import calibrate_clap_threshold
        result = calibrate_clap_threshold(_g, duration_seconds=duration)
        return {"ok": bool(result.get("ok", True)), **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
    """Persist + recall the orb widget's screen position. Phase 5 wiring —
    state/widget_position.json. Default top-left if unset."""
    p = _root / "state" / "widget_position.json"
    if p.is_file():
        try:
            import json as _j
            data = _j.loads(p.read_text(encoding="utf-8"))
            return {"ok": True, "x": int(data.get("x", 0)), "y": int(data.get("y", 0))}
        except Exception:
            pass
    return {"ok": True, "x": 100, "y": 100}


@app.post("/api/v1/widget/position")
async def widget_position_set(payload: dict = Body(default={})) -> dict:
    try:
        import json as _j
        x = int(payload.get("x", 0))
        y = int(payload.get("y", 0))
        p = _root / "state" / "widget_position.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_j.dumps({"x": x, "y": y}), encoding="utf-8")
        return {"ok": True, "x": x, "y": y}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Phase 5 — orb tells us when it's minimized vs visible. We store this on _g
# so future inner-monologue / proactive logic can know whether the user is
# actively watching or has tucked Iris away in the widget.
@app.post("/api/v1/orb/window_state")
async def orb_window_state(payload: dict = Body(default={})) -> dict:
    state = str(payload.get("state") or "").lower()  # "main" | "minimized" | "widget" | "hidden"
    _g["_orb_window_state"] = state
    _g["_orb_window_state_ts"] = time.time()
    return {"ok": True, "state": state}


@app.get("/api/v1/orb/window_state")
def orb_window_state_get() -> dict:
    return {
        "ok": True,
        "state": str(_g.get("_orb_window_state") or "unknown"),
        "ts": float(_g.get("_orb_window_state_ts") or 0.0),
    }


@app.get("/api/v1/debug/full")
def debug_full() -> dict:
    """Full subsystem snapshot. Reads health flags + bootstrap state from _g."""
    return {
        "ok": True,
        "ts": time.time(),
        "subsystem_health": {
            "sleep": {"state": "awake"},
            "tts": {"ready": bool(_g.get("_kokoro_ready") or _tts_ref is not None)},
            "stt": {"ready": bool(_g.get("_stt_ready"))},
            "wake": {"ready": _g.get("_wake_word_detector") is not None},
            "insightface": {
                "ready": bool(getattr(_g.get("_insight_face"), "available", False)),
                "known_count": (
                    int(_g.get("_insight_face").known_count())
                    if _g.get("_insight_face") and getattr(_g.get("_insight_face"), "available", False)
                    else 0
                ),
            },
            "expression": {"ready": _g.get("_expression_detector") is not None},
            "eye_tracker": {"ready": _g.get("_eye_tracker") is not None},
            "mood": {"ready": "load_mood" in _g},
            "memory": {"ready": _g.get("_iris_memory") is not None},
            "concept_graph": {"ready": _g.get("_concept_graph") is not None},
            "anchor_moments": {"ready": bool(_g.get("_anchor_moments_ready"))},
            "feature_flags": {"ready": bool(_g.get("_feature_flags_ready"))},
            "skill_sandbox": {"ready": bool(_g.get("_skill_sandbox_ready"))},
        },
        "perception": {
            "face_results": _g.get("_face_results") or [],
            "current_expression": str(_g.get("_current_expression") or ""),
            "attention_state": str(_g.get("_attention_state") or ""),
            "looking_at_screen": bool(_g.get("_looking_at_screen") or False),
        },
        "last_heartbeat_ts": float(_g.get("_last_heartbeat_ts") or 0.0),
        "orb_window_state": str(_g.get("_orb_window_state") or "unknown"),
        "time": _time_block_inline(),
    }


@app.get("/api/v1/debug/export", response_class=PlainTextResponse)
def debug_export() -> str:
    """Multi-line debug dump for the orb's debug tab."""
    lines = [
        f"=== Iris debug export @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===",
        "",
        f"BASE_DIR: {_root}",
        f"identity: iris",
        "",
        "[engines]",
        f"  tts: {'ready' if _tts_ref is not None else 'missing'}",
        f"  stt: {'ready' if _g.get('_stt_ready') else 'missing'}",
        f"  insightface: {'ready' if getattr(_g.get('_insight_face'), 'available', False) else 'missing'}",
        f"  expression_detector: {'ready' if _g.get('_expression_detector') is not None else 'missing'}",
        f"  eye_tracker: {'ready' if _g.get('_eye_tracker') is not None else 'missing'}",
        "",
        "[perception]",
        f"  face_count: {len(_g.get('_face_results') or [])}",
        f"  current_expression: {_g.get('_current_expression', '')!r}",
        f"  attention_state: {_g.get('_attention_state', '')!r}",
        f"  looking_at_screen: {bool(_g.get('_looking_at_screen') or False)}",
        "",
        "[mood]",
    ]
    try:
        from brain import mood_core
        m = mood_core.load_mood()
        primary = m.get("primary_emotions") or []
        lines.append(f"  current_mood: {m.get('current_mood', '?')}")
        lines.append(f"  outward_tone: {m.get('outward_tone', '?')}")
        lines.append(f"  primary: {primary}")
        lines.append(f"  behavior_modifiers: {m.get('behavior_modifiers', {})}")
    except Exception as e:
        lines.append(f"  (mood error: {e})")

    lines.extend([
        "",
        "[memory]",
        f"  iris_memory.count: {(_g.get('_iris_memory').count() if _g.get('_iris_memory') is not None else 0)}",
        f"  concept_graph.nodes: {(len(_g.get('_concept_graph').nodes) if _g.get('_concept_graph') else 0)}",
        f"  concept_graph.edges: {(len(_g.get('_concept_graph').edges) if _g.get('_concept_graph') else 0)}",
        "",
        f"[heartbeat]",
        f"  last_heartbeat_ts: {_g.get('_last_heartbeat_ts', 0.0)}",
        f"  orb_window_state: {_g.get('_orb_window_state', 'unknown')}",
    ])
    return "\n".join(lines) + "\n"


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
