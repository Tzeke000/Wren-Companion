"""
Local operator HTTP API for the Ava Control desktop app (Phase 1).

Binds to 127.0.0.1 only. Started from avaagent when AVA_OPERATOR_HTTP != 0.
Does not replace Gradio; complements it with JSON/chat for the Tauri UI.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import re
import signal
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

_HOST: dict[str, Any] | None = None
_CHAT_FN: Optional[Callable[..., dict[str, Any]]] = None
_CHAT_CALL_LOCK = threading.Lock()
_STT_LOCK = threading.Lock()
_STT_STATE: dict[str, Any] = {"ready": True, "processing": False, "text": "", "error": ""}

_DEFAULT_STYLE = {
    "orb_base_size": 180,
    "orb_ring_count": 2,
    "orb_glow_intensity": 0.8,
    "orb_trail": False,
    "orb_particles": False,
    "preferred_idle_color": None,
    "style_notes": "Ava's own notes about her visual style",
    "last_updated": None,
}


def _style_path(host: dict[str, Any]) -> Path:
    base = Path(host.get("BASE_DIR") or Path.cwd())
    return base / "state" / "ava_style.json"


def _load_style(host: dict[str, Any]) -> dict[str, Any]:
    path = _style_path(host)
    if not path.is_file():
        return dict(_DEFAULT_STYLE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out = dict(_DEFAULT_STYLE)
            out.update(data)
            return out
    except Exception:
        pass
    return dict(_DEFAULT_STYLE)


def _save_style(host: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    current = _load_style(host)
    current.update({k: v for k, v in (patch or {}).items() if k in _DEFAULT_STYLE})
    current["last_updated"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    path = _style_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return current


def _load_mood_block(host: dict[str, Any]) -> dict[str, Any]:
    base = Path(host.get("BASE_DIR") or Path.cwd())
    path = base / "ava_mood.json"
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
        except Exception:
            raw = {}
    primary = str(raw.get("current_mood") or "calmness")
    intensity = 0.0
    ew = raw.get("emotion_weights")
    if isinstance(ew, dict):
        try:
            intensity = float(ew.get(primary) or 0.0)
        except Exception:
            intensity = 0.0
    sec: list[dict[str, Any]] = []
    if isinstance(ew, dict):
        top = sorted(((str(k), float(v or 0.0)) for k, v in ew.items()), key=lambda x: x[1], reverse=True)[:4]
        for name, val in top:
            if name == primary:
                continue
            sec.append({"emotion": name, "intensity": round(max(0.0, val), 4)})
    return {
        "primary_emotion": primary,
        "primary_intensity": round(max(0.0, intensity), 4),
        "secondary_emotions": sec[:3],
        "mood_label": str(raw.get("outward_tone") or primary),
        "raw_mood": raw,
    }


def _schedule_graceful_shutdown(delay_seconds: float = 1.0) -> None:
    def _shutdown() -> None:
        time.sleep(max(0.1, float(delay_seconds or 1.0)))
        print(f"[EXIT] _schedule_graceful_shutdown firing — sending SIGINT to pid={os.getpid()}")
        try:
            import signal
            os.kill(os.getpid(), signal.SIGINT)
            return
        except Exception as _e:
            print(f"[EXIT] os.kill SIGINT failed: {_e} — falling back to os._exit(0)")
        try:
            print("[EXIT] os._exit(0) called from _schedule_graceful_shutdown")
            os._exit(0)
        except Exception:
            pass

    threading.Thread(target=_shutdown, daemon=True, name="ava-process-shutdown").start()


def configure_operator_runtime(host: dict[str, Any], chat_fn: Callable[..., dict[str, Any]]) -> None:
    global _HOST, _CHAT_FN
    _HOST = host
    _CHAT_FN = chat_fn


def _g() -> dict[str, Any]:
    if _HOST is None:
        return {}
    return _HOST


def _memory_refinement_summary(perception: Any) -> str:
    """Single-line operator summary from Phase 24 refined memory fields."""
    if perception is None:
        return ""
    parts = [
        str(getattr(perception, "refined_memory_class", "") or ""),
        "worthy" if bool(getattr(perception, "refined_memory_worthy", False)) else "not_worthy",
        f"retrieval_pri={float(getattr(perception, 'refined_memory_retrieval_priority', 0.0) or 0.0):.2f}",
    ]
    meta = getattr(perception, "refined_memory_meta", None)
    if isinstance(meta, dict) and meta.get("summary_line"):
        parts.append(str(meta.get("summary_line"))[:120])
    return " · ".join(parts)[:400]


def _perception_dict(p: Any) -> dict[str, Any]:
    if p is None:
        return {}
    keys = [
        "vision_status",
        "visual_truth_trusted",
        "face_status",
        "recognized_text",
        "scene_compact_summary",
        "scene_overall_state",
        "identity_state",
        "resolved_face_identity",
        "stable_face_identity",
        "quality_label",
        "blur_label",
        "acquisition_freshness",
        "interpretation_primary_event",
        "interpretation_confidence",
        "routing_selected_model",
        "cognitive_mode",
        "routing_fallback_model",
        "routing_reason",
        "routing_confidence",
        "heartbeat_mode",
        "heartbeat_summary",
        "heartbeat_last_reason",
        "voice_turn_state",
        "nuance_tone",
        "runtime_presence_mode",
        "runtime_ready_state",
        "runtime_active_issue_summary",
        "runtime_threads_summary",
        "runtime_maintenance_summary",
        "runtime_learning_summary",
        "runtime_operator_summary",
        "strategic_continuity_summary",
        "active_threads",
        "relationship_carryover",
        "maintenance_carryover",
        "continuity_scope",
        "relationship_summary",
        "unfinished_thread_present",
        "refined_memory_class",
        "refined_memory_worthy",
        "refined_memory_retrieval_priority",
        "workbench_has_proposal",
        "workbench_top_proposal_type",
        "workbench_top_proposal_title",
        "workbench_summary",
        "workbench_execution_ready",
        "workbench_last_execution_success",
        "workbench_last_execution_summary",
        "improvement_loop_active",
        "improvement_loop_stage",
        "improvement_active_issue",
        "improvement_awaiting_approval",
        "active_concern_count",
        "top_active_concern",
        "concern_reconciliation_summary",
        "learning_focus",
        "learning_summary",
        "workbench_selected_proposal_id",
        "workbench_last_rollback_success",
        "improvement_loop_summary",
    ]
    out: dict[str, Any] = {}
    for k in keys:
        try:
            v = getattr(p, k, None)
            if hasattr(v, "tolist"):
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            elif isinstance(v, (list, dict)):
                try:
                    json.dumps(v, default=str)
                    out[k] = v
                except Exception:
                    out[k] = str(v)[:800]
            else:
                out[k] = str(v)[:1200]
        except Exception:
            continue
    return out


def build_snapshot(host: dict[str, Any]) -> dict[str, Any]:
    ws = host.get("workspace")
    perception = None
    try:
        if ws is not None and getattr(ws, "state", None) is not None:
            perception = ws.state.perception
    except Exception:
        perception = None

    snap_runtime = host.get("_runtime_self_snapshot")
    if not isinstance(snap_runtime, dict):
        snap_runtime = {}

    _op_port = os.environ.get("AVA_OPERATOR_HTTP_PORT", "5876").strip() or "5876"

    ribbon = {
        "heartbeat_mode": str(getattr(perception, "heartbeat_mode", "") or snap_runtime.get("heartbeat_mode") or "")[:64],
        "routing_selected_model": str(getattr(perception, "routing_selected_model", "") or "")[:120],
        "cognitive_mode": str(getattr(perception, "cognitive_mode", "") or "")[:80],
        "vision_status": str(getattr(perception, "vision_status", "") or "")[:64],
        "voice_turn_state": str(getattr(perception, "voice_turn_state", "") or "idle")[:48],
        "presence_mode": str(getattr(perception, "runtime_presence_mode", "") or snap_runtime.get("presence_mode") or "")[:48],
        "active_issue": str(getattr(perception, "runtime_active_issue_summary", "") or snap_runtime.get("active_issue") or "")[:200],
        "threads_short": str(getattr(perception, "runtime_threads_summary", "") or "")[:200],
        "workbench_hint": str(getattr(perception, "workbench_summary", "") or "")[:160],
        "ready_state": str(getattr(perception, "runtime_ready_state", "") or snap_runtime.get("runtime_readiness") or "")[:32],
        "routing_override": str(host.get("_routing_model_override") or "")[:120],
        "concerns_top": str(getattr(perception, "top_active_concern", "") or "")[:120],
        "nuance_tone": str(getattr(perception, "nuance_tone", "") or "")[:120],
        "operator_http_url": f"http://127.0.0.1:{_op_port}/",
    }

    # Heartbeat block — perception may be None until first chat tick, so fall back
    # to background-tick globals (set by brain.background_ticks) for live data.
    _hb_mode = (getattr(perception, "heartbeat_mode", "") if perception else "") or str(host.get("_heartbeat_last_mode") or "")
    _hb_summary = (getattr(perception, "heartbeat_summary", "") if perception else "") or str(host.get("_heartbeat_last_summary") or "")
    _hb_tick_id = int((getattr(perception, "heartbeat_tick_id", 0) if perception else 0) or host.get("_heartbeat_last_tick_id") or 0)
    heartbeat_block = {
        "heartbeat_mode": _hb_mode,
        "heartbeat_summary": _hb_summary,
        "heartbeat_last_reason": getattr(perception, "heartbeat_last_reason", "") if perception else "",
        "heartbeat_tick_id": _hb_tick_id,
        "heartbeat_last_ts": float(host.get("_heartbeat_last_ts") or 0),
        "heartbeat_meta": dict(getattr(perception, "heartbeat_meta", {}) or {}) if perception else {},
        "runtime_presence_mode": getattr(perception, "runtime_presence_mode", "") if perception else "",
        "runtime_operator_summary": getattr(perception, "runtime_operator_summary", "") if perception else "",
        "runtime_presence_meta": dict(getattr(perception, "runtime_presence_meta", {}) or {}) if perception else {},
        "runtime_threads_summary": getattr(perception, "runtime_threads_summary", "") if perception else "",
        "runtime_active_issue_summary": getattr(perception, "runtime_active_issue_summary", "") if perception else "",
        "runtime_maintenance_summary": getattr(perception, "runtime_maintenance_summary", "") if perception else "",
        "runtime_ready_state": getattr(perception, "runtime_ready_state", "") if perception else "",
        "learning_summary": getattr(perception, "learning_summary", "") if perception else "",
        "learning_focus": getattr(perception, "learning_focus", "") if perception else "",
        "learning_meta": dict(getattr(perception, "learning_meta", {}) or {}) if perception else {},
        "snapshot_carryover": snap_runtime,
    }

    rm = dict(getattr(perception, "routing_meta", {}) or {}) if perception else {}
    # Source of truth for the live foreground model: dual_brain.foreground_model.
    # routing_selected_model can lag (it's only refreshed when classify_reply_depth
    # runs); reading dual_brain directly mirrors what Stream A would actually use
    # right now. Fallback chain: dual_brain → routing_selected_model → LLM_MODEL.
    _live_foreground_model = ""
    try:
        from brain.dual_brain import get_dual_brain as _gdb
        _db_for_models = _gdb(host)
        if _db_for_models is not None:
            _live_foreground_model = str(getattr(_db_for_models, "foreground_model", "") or "")
    except Exception:
        _live_foreground_model = ""
    models_block = {
        "selected_model": _live_foreground_model
            or (getattr(perception, "routing_selected_model", "") if perception else "")
            or str(host.get("LLM_MODEL") or "ava-personal:latest"),
        "cognitive_mode": getattr(perception, "cognitive_mode", "") if perception else "",
        "fallback_model": getattr(perception, "routing_fallback_model", "") if perception else "",
        "routing_reason": str((getattr(perception, "routing_reason", "") if perception else "") or "")[:900],
        "routing_confidence": float((getattr(perception, "routing_confidence", 0.0) if perception else 0.0) or 0.0),
        "routing_meta": rm,
        "switch_reason_last": str(rm.get("switch_reason") or rm.get("switch_explain") or "")[:500],
        "no_switch_reason_last": str(rm.get("no_switch_reason") or rm.get("no_switch_explain") or "")[:500],
        "override_model": host.get("_routing_model_override"),
        "override_mode": host.get("_routing_cognitive_mode_override"),
        "last_switch_mono": host.get("_routing_last_switch_monotonic"),
        "host_last_effective_model": host.get("_routing_last_effective_model"),
        "host_last_cognitive_mode": host.get("_routing_last_cognitive_mode"),
    }

    try:
        from brain.model_routing import discover_available_model_tags

        tags, src = discover_available_model_tags(force=False)
        models_block["available_models"] = sorted(tags) if tags else []
        models_block["discovery_source"] = src
    except Exception as e:
        models_block["available_models"] = []
        models_block["discovery_error"] = str(e)[:200]

    try:
        from brain.model_evaluator import get_evaluator
        models_block["p44_eval"] = get_evaluator().get_status()
    except Exception:
        pass

    # Phase 81: face recognizer status
    _fr_confidence = 0.0
    _fr_person_id = "unknown"
    try:
        from brain.face_recognizer import get_recognizer
        _fr = get_recognizer(Path(host.get("BASE_DIR") or "."))
        _fr_confidence_val = host.get("_face_recognizer_last_confidence")
        _fr_person_id_val = host.get("_face_recognizer_last_person_id")
        if isinstance(_fr_confidence_val, float):
            _fr_confidence = _fr_confidence_val
        if isinstance(_fr_person_id_val, str):
            _fr_person_id = _fr_person_id_val
    except Exception:
        pass

    # Eye tracker state (best-effort).
    _gaze_calibrated = False
    try:
        _et = host.get("_eye_tracker")
        if _et is not None:
            _gaze_calibrated = bool(getattr(_et, "calibrated", False))
    except Exception:
        pass

    # Expression calibration state for the current recognized person.
    _expr_calibrated = False
    _expr_samples = 0
    try:
        _cal = host.get("_expression_calibrator")
        if _cal is not None and _fr_person_id and _fr_person_id != "unknown":
            _bl = _cal.get_baseline(_fr_person_id)
            _expr_calibrated = bool(_bl.get("calibrated"))
            _expr_samples = int(_bl.get("sample_count") or 0)
    except Exception:
        pass

    vision_block = {
        "perception": _perception_dict(perception),
        "llava_scene_description": str(host.get("_llava_scene_description") or "")[:700],
        "recognized_person_id": _fr_person_id,
        "recognized_confidence": round(_fr_confidence, 3),
        "expression": str(host.get("_current_expression") or "neutral"),
        "face_age": int(host.get("_face_age") or 0),
        "face_gender": str(host.get("_face_gender") or "?"),
        "attention_state": str(host.get("_attention_state") or "focused"),
        "gaze_region": str(host.get("_gaze_region") or "center"),
        "gaze_calibrated": _gaze_calibrated,
        "expression_calibrated": _expr_calibrated,
        "expression_calibration_samples": _expr_samples,
    }

    memory_block = {
        "strategic_continuity_summary": getattr(perception, "strategic_continuity_summary", ""),
        "active_threads": getattr(perception, "active_threads", []) or [],
        "relationship_carryover": getattr(perception, "relationship_carryover", ""),
        "maintenance_carryover": getattr(perception, "maintenance_carryover", ""),
        "relationship_summary": getattr(perception, "relationship_summary", ""),
        "unfinished_thread_present": bool(getattr(perception, "unfinished_thread_present", False)),
        "refined_memory_class": getattr(perception, "refined_memory_class", ""),
        "refined_memory_retrieval_priority": getattr(perception, "refined_memory_retrieval_priority", 0.0),
        "memory_refinement_summary": _memory_refinement_summary(perception),
        "live_context": host.get("_live_context_snapshot") if isinstance(host.get("_live_context_snapshot"), dict) else {},
    }

    wb_block = {
        "workbench_has_proposal": bool(getattr(perception, "workbench_has_proposal", False)),
        "workbench_top_proposal_type": getattr(perception, "workbench_top_proposal_type", ""),
        "workbench_top_proposal_title": getattr(perception, "workbench_top_proposal_title", ""),
        "workbench_summary": getattr(perception, "workbench_summary", ""),
        "workbench_execution_ready": getattr(perception, "workbench_execution_ready", False),
        "workbench_last_execution_success": getattr(perception, "workbench_last_execution_success", False),
        "workbench_last_execution_summary": getattr(perception, "workbench_last_execution_summary", ""),
        "workbench_meta": dict(getattr(perception, "workbench_meta", {}) or {}) if perception else {},
        "last_execution_global": str(host.get("_last_workbench_execution_result"))[:1200]
        if host.get("_last_workbench_execution_result") is not None
        else "",
        "last_command_global": str(host.get("_last_workbench_command_result"))[:1200]
        if host.get("_last_workbench_command_result") is not None
        else "",
    }
    idx_fn = host.get("format_workbench_index")
    if callable(idx_fn):
        try:
            wb_block["workbench_index_text"] = str(idx_fn(limit=48))[:8000]
        except Exception as e:
            wb_block["workbench_index_text"] = ""
            wb_block["workbench_index_error"] = str(e)[:200]
    else:
        wb_block["workbench_index_text"] = ""

    loop_block = {
        "improvement_loop_active": bool(getattr(perception, "improvement_loop_active", False)),
        "improvement_loop_stage": getattr(perception, "improvement_loop_stage", ""),
        "improvement_active_issue": getattr(perception, "improvement_active_issue", ""),
        "improvement_awaiting_approval": bool(getattr(perception, "improvement_awaiting_approval", False)),
        "improvement_loop_meta": dict(getattr(perception, "improvement_loop_meta", {}) or {}) if perception else {},
    }

    concerns_block = {
        "active_concern_count": int(getattr(perception, "active_concern_count", 0) or 0),
        "top_active_concern": getattr(perception, "top_active_concern", ""),
        "concern_reconciliation_summary": getattr(perception, "concern_reconciliation_summary", ""),
        "concern_reconciliation_meta": dict(getattr(perception, "concern_reconciliation_meta", {}) or {})
        if perception
        else {},
    }

    tools_block = {
        "last_tool_used": str(host.get("_desktop_last_tool_used") or ""),
        "last_tool_result": str(host.get("_desktop_last_tool_result") or "")[:200],
        "tool_execution_count": int(host.get("_desktop_tool_execution_count", 0) or 0),
        "pending_tier2_proposals": len(list(host.get("_desktop_tier2_pending") or [])),
        "tools_registry": {"available_tools": [], "tool_count": 0},
    }
    try:
        reg = host.get("_tool_registry") or host.get("_desktop_tool_registry")
        if reg is not None and callable(getattr(reg, "list_tools", None)):
            rows = list(reg.list_tools() or [])
            tools_block["tools_registry"] = {"available_tools": rows, "tool_count": len(rows)}
    except Exception:
        pass
    visual_memory_block = {
        "cluster_count": 0,
        "named_clusters": 0,
        "most_seen": "",
    }
    try:
        vm = host.get("_visual_memory_summary")
        if isinstance(vm, dict):
            visual_memory_block["cluster_count"] = int(vm.get("cluster_count") or 0)
            visual_memory_block["named_clusters"] = int(vm.get("named_clusters") or 0)
            visual_memory_block["most_seen"] = str(vm.get("most_seen") or "")
    except Exception:
        pass
    tts_obj = host.get("tts_engine")
    tts_worker = host.get("_tts_worker")
    # Prefer the TTS worker for live state — it owns the engine, knows real amplitude.
    if tts_worker is not None and getattr(tts_worker, "available", False):
        try:
            from brain.tts_worker import get_live_amplitude
            _live_amp = float(get_live_amplitude())
        except Exception:
            _live_amp = 0.0
        tts_block = {
            "available": True,
            "enabled": bool(host.get("tts_enabled", False)),
            "engine": str(getattr(tts_worker, "engine_name", lambda: "none")()),
            "voice": str(getattr(tts_worker, "voice_name", lambda: "unknown")()),
            "tts_speaking": bool(getattr(tts_worker, "is_speaking", lambda: False)()),
            "tts_amplitude": _live_amp,
            "muted": bool(host.get("_tts_muted")),
            "last_playback_dropped": bool(host.get("_tts_last_playback_dropped")),
            "last_playback_dropped_ts": float(host.get("_tts_last_playback_dropped_ts") or 0.0),
        }
    else:
        tts_block = {
            "available": bool(getattr(tts_obj, "is_available", lambda: False)()) if tts_obj is not None else False,
            "enabled": bool(host.get("tts_enabled", False)),
            "engine": str(host.get("tts_engine_name") or "none"),
            "voice": str(getattr(tts_obj, "voice_name", lambda: "unknown")()) if tts_obj is not None else "unknown",
            "tts_speaking": bool(getattr(tts_obj, "speaking", False)) if tts_obj is not None else bool(host.get("_tts_speaking", False)),
            "tts_amplitude": float(getattr(tts_obj, "amplitude", 0.0)) if tts_obj is not None else float(host.get("_tts_amplitude", 0.0) or 0.0),
            "muted": bool(host.get("_tts_muted")),
            "last_playback_dropped": bool(host.get("_tts_last_playback_dropped")),
            "last_playback_dropped_ts": float(host.get("_tts_last_playback_dropped_ts") or 0.0),
        }
    # Live speech progress — what Ava is currently speaking, word by word.
    speech_block: dict[str, Any] = {
        "speaking": bool(tts_block.get("tts_speaking", False)),
        "current_word": "",
        "spoken_so_far": "",
        "full_reply": "",
    }
    try:
        from brain.tts_worker import get_speech_state
        _full, _spoken, _current = get_speech_state()
        speech_block["full_reply"] = _full
        speech_block["spoken_so_far"] = _spoken
        speech_block["current_word"] = _current
    except Exception:
        pass
    # Phase 49: pointing state for widget orb
    widget_block = {
        "pointing": bool(host.get("_widget_pointing", False)),
        "pointing_description": str(host.get("_widget_pointing_description") or ""),
        "pointing_coords": host.get("_widget_pointing_coords"),
    }

    # Phase 54: system stats (cached every 30s to avoid overhead)
    system_stats: dict[str, Any] = {}
    try:
        import time as _time
        _stats_cache = host.get("_system_stats_cache")
        _stats_ts = float(host.get("_system_stats_ts") or 0)
        if _stats_cache and (_time.time() - _stats_ts) < 30:
            system_stats = _stats_cache
        else:
            import psutil
            m = psutil.virtual_memory()
            system_stats = {
                "cpu_pct": psutil.cpu_percent(interval=None),
                "ram_used_gb": round(m.used / 1e9, 1),
                "ram_total_gb": round(m.total / 1e9, 1),
                "ram_pct": m.percent,
            }
            host["_system_stats_cache"] = system_stats
            host["_system_stats_ts"] = _time.time()
    except ImportError:
        system_stats = {"error": "psutil not installed"}
    except Exception as e:
        system_stats = {"error": str(e)[:100]}
    # Phase 76: llava status
    llava_block = {
        "model": str(host.get("_llava_model_name") or "none"),
        "active": bool(host.get("_llava_model_name")),
        "last_description": str(host.get("_llava_scene_description") or "")[:200],
    }

    # Phase 74: voice loop state
    voice_loop_block: dict[str, Any] = {"active": False, "state": "passive"}
    try:
        _vl = host.get("_voice_loop")
        if _vl is not None:
            voice_loop_block = {
                "active": bool(getattr(_vl, "active", False)),
                "state": str(getattr(_vl, "state", "passive")),
            }
    except Exception:
        pass

    # Compose inner_state_line — single short string summarizing what Ava is
    # doing right now. Updated on actual state changes by voice_loop /
    # reply_engine / heartbeat; this composer falls back to derived state.
    inner_state_line = ""
    try:
        explicit = str(host.get("_inner_state_line") or "").strip()
        vl_state = str(voice_loop_block.get("state") or "").lower()
        if explicit:
            inner_state_line = explicit[:60]
        elif speech_block.get("speaking"):
            # Speech text shows above the orb; keep this empty during speaking.
            inner_state_line = ""
        elif vl_state in ("listening", "attentive"):
            inner_state_line = "listening"
        elif vl_state == "thinking":
            inner_state_line = "thinking"
        else:
            hb_summary = str(host.get("_heartbeat_last_summary") or "").strip()
            if hb_summary:
                inner_state_line = hb_summary[:60]
            else:
                inner_state_line = ""
    except Exception:
        inner_state_line = ""

    # Phase 70: Emil bridge status
    emil_block: dict[str, Any] = {"online": False, "last_contact": 0.0, "shared_topics": []}
    try:
        from brain.emil_bridge import get_emil_bridge
        emil_block = get_emil_bridge(host.get("BASE_DIR") or Path.cwd()).get_status()
    except Exception:
        pass

    # Phase 71: active plans summary
    active_plans_block: list[dict[str, Any]] = []
    try:
        from brain.planner import get_planner
        active_plans_block = get_planner(host.get("BASE_DIR") or Path.cwd()).get_active_plans()[:10]
    except Exception:
        pass

    # Phase 79: onboarding status
    onboarding_block: dict[str, Any] = {"active": False, "stage": None, "person_id": None}
    try:
        from brain.person_onboarding import get_onboarding_status
        onboarding_block = get_onboarding_status(host)
    except Exception:
        pass

    # Phase 82: current person at machine
    current_person_block: dict[str, Any] = {
        "person_id": "unknown", "display_name": "Unknown",
        "confidence": 0.0, "time_at_machine": 0.0, "is_zeke": False,
    }
    try:
        from brain.runtime_presence import get_current_person_block
        current_person_block = get_current_person_block(host)
    except Exception:
        pass

    # Attention / gaze / expression snapshot
    _away_since = float(host.get("_user_away_since") or 0)
    _attention_block: dict[str, Any] = {
        "gaze_region": str(host.get("_gaze_region") or "unknown"),
        "attention_state": str(host.get("_attention_state") or "unknown"),
        "looking_at_screen": bool(host.get("_looking_at_screen", False)),
        "away_duration_seconds": round(time.time() - _away_since, 1) if _away_since > 0 and host.get("_user_away") else 0.0,
        "expression": str(host.get("_current_expression") or "neutral"),
        "gaze_calibrated": False,
        "gaze_target": str(host.get("_gaze_target_description") or ""),
    }
    try:
        from brain.eye_tracker import get_eye_tracker
        _et = get_eye_tracker()
        if _et is not None:
            _attention_block["gaze_calibrated"] = _et.calibrated
    except Exception:
        pass

    # Dual-brain status snapshot
    _dual_brain_block: dict[str, Any] = {
        "stream_a": {"model": "ava-personal:latest", "busy": False, "last_active": 0.0},
        "stream_b": {"model": "qwen2.5:14b", "busy": False, "current_task": None, "queue_depth": 0, "tasks_today": 0, "live_thinking": False},
        "pending_insight": False,
        "live_thought_age": None,
    }
    try:
        from brain.dual_brain import get_dual_brain
        _db_snap = get_dual_brain(host)
        if _db_snap is not None:
            _dual_brain_block = _db_snap.get_status()
    except Exception:
        pass

    # Connectivity snapshot block
    _connectivity_block: dict[str, Any] = {
        "online": bool(host.get("_is_online", False)),
        "quality": str(host.get("_connection_quality") or "offline"),
        "cloud_models_available": bool(host.get("_ollama_cloud_reachable", False)),
        "last_check": float(host.get("_connectivity_last_check") or 0.0),
        "changed_recently": bool(host.get("_connectivity_changed", False)),
    }

    # Phase 83: notification count today
    _notif_count_today = 0
    try:
        from tools.system.notification_tool import get_notification_count_today
        _notif_count_today = get_notification_count_today(host)
    except Exception:
        pass

    # Phase 95: privacy security stats
    _security_block: dict[str, Any] = {"blocked_today": 0, "last_audit_ts": 0.0}
    try:
        from brain.privacy_guardian import get_blocked_count_today
        _security_block["blocked_today"] = get_blocked_count_today(host)
        _audit_path = Path(host.get("BASE_DIR") or ".") / "state" / "privacy_audit_state.json"
        if _audit_path.is_file():
            import json as _j95
            _ad = _j95.loads(_audit_path.read_text(encoding="utf-8"))
            _security_block["last_audit_ts"] = float(_ad.get("ts") or 0)
    except Exception:
        pass

    mood_block = _load_mood_block(host)
    style_block = _load_style(host)
    deep_self_block = {}
    try:
        from brain.deep_self import deep_self_snapshot

        deep_self_block = deep_self_snapshot(host)
    except Exception:
        deep_self_block = {}

    inner_life = {
        "current_thought": "",
        "current_curiosity": None,
        "self_summary": "",
        "opinion_count": 0,
        "monologue_thought_count": 0,
        "history_summary_count": 0,
        "last_shutdown": None,
        "pickup_note_active": False,
        "pickup_note_preview": None,
    }
    try:
        from brain.inner_monologue import current_thought, thought_count
        from brain.curiosity_topics import get_current_curiosity
        from brain.self_model import get_self_summary
        from brain.opinions import opinion_count

        base_dir = Path(host.get("BASE_DIR") or Path.cwd())
        inner_life["current_thought"] = current_thought(base_dir) or ""
        inner_life["current_curiosity"] = get_current_curiosity(host)
        inner_life["self_summary"] = get_self_summary(host)
        inner_life["opinion_count"] = int(opinion_count(host))
        inner_life["monologue_thought_count"] = int(thought_count(base_dir))
    except Exception:
        pass

    try:
        hm = host.get("history_manager")
        if hm is not None and callable(getattr(hm, "summary_count", None)):
            pid = str(host.get("active_person_id") or host.get("OWNER_PERSON_ID") or "zeke")
            inner_life["history_summary_count"] = int(hm.summary_count(pid))
    except Exception:
        pass

    try:
        base_dir = Path(host.get("BASE_DIR") or Path.cwd())
        pickup_path = base_dir / "state" / "pickup_note.json"
        if pickup_path.is_file():
            payload = json.loads(pickup_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                ts = float(payload.get("timestamp") or 0.0)
                note = str(payload.get("note") or "").strip()
                fresh = ts > 0 and ((__import__("time").time() - ts) <= 24 * 3600) and bool(note)
                inner_life["last_shutdown"] = ts if ts > 0 else None
                inner_life["pickup_note_active"] = bool(fresh)
                inner_life["pickup_note_preview"] = (note[:80] if note else None)
    except Exception:
        pass

    brain_graph_block: dict[str, Any] = {
        "total_nodes": 0,
        "total_edges": 0,
        "active_nodes": 0,
        "nodes_by_type": {
            "person": 0,
            "topic": 0,
            "emotion": 0,
            "memory": 0,
            "opinion": 0,
            "curiosity": 0,
            "self": 0,
            "event": 0,
        },
        "most_activated": "",
        "last_bootstrap": 0.0,
    }
    try:
        cg = host.get("_concept_graph")
        payload: dict[str, Any] = {}
        if cg is not None and callable(getattr(cg, "get_graph_data", None)):
            try:
                payload = cg.get_graph_data() or {}
            except Exception:
                payload = {}
        # If the in-memory instance returned 0 nodes, fall back to reading the file directly
        if not payload.get("nodes"):
            try:
                _cg_path = Path(host.get("BASE_DIR") or ".") / "state" / "concept_graph.json"
                if _cg_path.is_file():
                    payload = json.loads(_cg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        if payload.get("nodes") or payload.get("stats"):
            stats = dict(payload.get("stats") or {})
            node_list = list(payload.get("nodes") or [])
            brain_graph_block["total_nodes"] = int(stats.get("total_nodes") or len(node_list))
            brain_graph_block["total_edges"] = int(stats.get("total_edges") or len(payload.get("edges") or []))
            active_ids = host.get("_active_concept_nodes")
            if isinstance(active_ids, list):
                brain_graph_block["active_nodes"] = len([x for x in active_ids if str(x).strip()])
            else:
                brain_graph_block["active_nodes"] = int(stats.get("active_nodes_30s") or 0)
            # Compute nodes_by_type from stats or raw node list
            by_type = stats.get("nodes_by_type")
            if isinstance(by_type, dict):
                merged = dict(brain_graph_block["nodes_by_type"])
                for k, v in by_type.items():
                    merged[str(k)] = int(v or 0)
                brain_graph_block["nodes_by_type"] = merged
            elif node_list:
                merged = dict(brain_graph_block["nodes_by_type"])
                for n in node_list:
                    if isinstance(n, dict):
                        t = str(n.get("type") or "topic")
                        merged[t] = merged.get(t, 0) + 1
                brain_graph_block["nodes_by_type"] = merged
            brain_graph_block["most_activated"] = str(stats.get("most_activated") or "")
            brain_graph_block["last_bootstrap"] = float(stats.get("last_bootstrap") or payload.get("last_bootstrap") or 0.0)
    except Exception:
        pass

    debug_human = {
        "ribbon": ribbon,
        "heartbeat": heartbeat_block,
        "models": models_block,
        "vision": vision_block,
        "memory": memory_block,
        "workbench": wb_block,
        "improvement_loop": loop_block,
        "concerns": concerns_block,
        "tools": tools_block,
        "tts": tts_block,
        "mood": mood_block,
        "style": style_block,
        "deep_self": deep_self_block,
        "inner_life": inner_life,
        "brain_graph": brain_graph_block,
        "visual_memory": visual_memory_block,
        "emil": emil_block,
        "active_plans": active_plans_block,
        "reply_path": host.get("reply_path_meta") if isinstance(host.get("reply_path_meta"), dict) else {},
    }

    return {
        "ribbon": ribbon,
        "heartbeat_runtime": heartbeat_block,
        "models": models_block,
        "vision": vision_block,
        "memory_continuity": memory_block,
        "workbench": wb_block,
        "improvement_loop": loop_block,
        "concerns": concerns_block,
        "tools": tools_block,
        "tts": tts_block,
        "speech": speech_block,
        "inner_state_line": inner_state_line,
        "thinking_tier": int(host.get("_thinking_tier", 0)),
        "widget": widget_block,
        "voice_loop": voice_loop_block,
        "thinking": bool(host.get("_ava_thinking", False)),
        "thinking_since": float(host.get("_ava_thinking_since") or 0),
        "llava": llava_block,
        "emil": emil_block,
        "active_plans": active_plans_block,
        "onboarding": onboarding_block,
        "current_person": current_person_block,
        "attention": _attention_block,
        "dual_brain": _dual_brain_block,
        "connectivity": _connectivity_block,
        "notification_count_today": _notif_count_today,
        "security": _security_block,
        "trust_scores": (lambda: __import__("brain.trust_system", fromlist=["get_all_trust_scores"]).get_all_trust_scores(host) if True else {})() if True else {},
        "system_stats": system_stats,
        "mood": mood_block,
        "style": style_block,
        "deep_self": deep_self_block,
        "inner_life": inner_life,
        "brain_graph": brain_graph_block,
        "visual_memory": visual_memory_block,
        "debug": debug_human,
        "ts": __import__("time").time(),
    }


def build_debug_export(host: dict[str, Any]) -> str:
    snap = build_snapshot(host)
    rb = snap.get("ribbon") or {}
    hb = snap.get("heartbeat_runtime") or {}
    mb = snap.get("models") or {}
    wb = snap.get("workbench") or {}
    lp = snap.get("improvement_loop") or {}
    cc = snap.get("concerns") or {}
    mem = snap.get("memory_continuity") or {}
    lines: list[str] = []
    lines.append("=== Ava operator debug export (AI handoff) ===")
    lines.append("")
    lines.append("## Ribbon / live summary")
    for k in sorted(rb.keys()):
        lines.append(f"- {k}: {rb[k]}")
    lines.append("")
    lines.append("## Heartbeat / runtime presence")
    lines.append(f"- heartbeat_mode: {hb.get('heartbeat_mode')}")
    lines.append(f"- heartbeat_last_reason: {hb.get('heartbeat_last_reason')}")
    lines.append(f"- heartbeat_summary: {hb.get('heartbeat_summary')}")
    lines.append(f"- runtime_presence_mode: {hb.get('runtime_presence_mode')}")
    lines.append(f"- runtime_operator_summary: {hb.get('runtime_operator_summary')}")
    lines.append(f"- runtime_active_issue_summary: {hb.get('runtime_active_issue_summary')}")
    lines.append(f"- runtime_threads_summary: {hb.get('runtime_threads_summary')}")
    lines.append(f"- runtime_maintenance_summary: {hb.get('runtime_maintenance_summary')}")
    lines.append(f"- learning_focus: {hb.get('learning_focus')}")
    lines.append(f"- learning_summary: {hb.get('learning_summary')}")
    sc = hb.get("snapshot_carryover") if isinstance(hb.get("snapshot_carryover"), dict) else {}
    if sc:
        lines.append("- snapshot_carryover:")
        lines.append(json.dumps(sc, indent=2, default=str)[:4000])
    lines.append("")
    lines.append("## Model routing")
    lines.append(f"- selected_model: {mb.get('selected_model')}")
    lines.append(f"- cognitive_mode: {mb.get('cognitive_mode')}")
    lines.append(f"- fallback_model: {mb.get('fallback_model')}")
    lines.append(f"- routing_reason: {mb.get('routing_reason')}")
    lines.append(f"- override_model: {mb.get('override_model')}")
    lines.append(f"- override_mode: {mb.get('override_mode')}")
    lines.append(f"- switch_reason_last: {mb.get('switch_reason_last')}")
    lines.append(f"- no_switch_reason_last: {mb.get('no_switch_reason_last')}")
    lines.append(f"- available_models: {mb.get('available_models')}")
    lines.append("")
    lines.append("## Strategic continuity / memory")
    lines.append(f"- strategic_continuity_summary: {mem.get('strategic_continuity_summary')}")
    lines.append(f"- relationship_carryover: {mem.get('relationship_carryover')}")
    lines.append(f"- unfinished_thread_present: {mem.get('unfinished_thread_present')}")
    lines.append(f"- refined_memory_class: {mem.get('refined_memory_class')}")
    lines.append(f"- memory_refinement_summary: {mem.get('memory_refinement_summary')}")
    lc = mem.get("live_context") if isinstance(mem.get("live_context"), dict) else {}
    if lc:
        lines.append("- live_context:")
        lines.append(json.dumps(lc, indent=2, default=str)[:3500])
    lines.append("")
    lines.append("## Workbench")
    lines.append(f"- workbench_summary: {wb.get('workbench_summary')}")
    lines.append(f"- workbench_has_proposal: {wb.get('workbench_has_proposal')}")
    lines.append(f"- workbench_execution_ready: {wb.get('workbench_execution_ready')}")
    lines.append(f"- workbench_last_execution_summary: {wb.get('workbench_last_execution_summary')}")
    lines.append("")
    lines.append("## Self-improvement loop")
    lines.append(f"- improvement_loop_active: {lp.get('improvement_loop_active')}")
    lines.append(f"- improvement_loop_stage: {lp.get('improvement_loop_stage')}")
    lines.append(f"- improvement_active_issue: {lp.get('improvement_active_issue')}")
    lines.append(f"- improvement_awaiting_approval: {lp.get('improvement_awaiting_approval')}")
    lines.append("")
    lines.append("## Concerns / diagnostics")
    lines.append(f"- active_concern_count: {cc.get('active_concern_count')}")
    lines.append(f"- top_active_concern: {cc.get('top_active_concern')}")
    lines.append(f"- concern_reconciliation_summary: {cc.get('concern_reconciliation_summary')}")
    lines.append("")
    rp = host.get("reply_path_meta") if isinstance(host.get("reply_path_meta"), dict) else {}
    if rp:
        lines.append("## Reply path (compact)")
        lines.append(json.dumps(rp, indent=2, default=str)[:4500])
        lines.append("")
    rs = host.get("_runtime_self_snapshot") if isinstance(host.get("_runtime_self_snapshot"), dict) else {}
    if rs:
        lines.append("## Runtime self snapshot (warm)")
        lines.append(json.dumps(rs, indent=2, default=str)[:3500])
        lines.append("")
    sr = host.get("_startup_resume_snapshot")
    if sr is not None:
        lines.append("## Startup resume (brief)")
        try:
            from dataclasses import asdict, is_dataclass

            if is_dataclass(sr):
                blob = asdict(sr)
            elif isinstance(sr, dict):
                blob = sr
            else:
                blob = {"value": repr(sr)}
            lines.append(json.dumps(blob, indent=2, default=str)[:2000])
        except Exception:
            lines.append(str(sr)[:2000])
        lines.append("")
    wbx = wb.get("workbench_index_text") if isinstance(wb.get("workbench_index_text"), str) else ""
    if wbx:
        lines.append("## Workbench index (preview)")
        lines.append(wbx[:6000])
        lines.append("")
    lines.append("## Full snapshot JSON (truncated)")
    lines.append(json.dumps(snap, indent=2, default=str)[:42000])
    lines.append("")
    lines.append("=== End ===")
    return "\n".join(lines)


def build_debug_full(host: dict[str, Any]) -> dict[str, Any]:
    """Unified debug payload for /api/v1/debug/full.

    Pulls only from cached state — no live LLM calls, no slow file IO. Each
    section is wrapped so a partial failure still returns the rest. Designed
    to complete in <50ms.
    """
    out: dict[str, Any] = {}
    # Server time — both wall and monotonic so the regression test can
    # compute deltas across requests reliably.
    try:
        from datetime import datetime, timezone
        out["server_time"] = {
            "iso_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "monotonic_ns": time.monotonic_ns(),
            "epoch": time.time(),
        }
    except Exception as e:
        out["server_time"] = {"error": str(e)}

    g = host or {}

    # voice_loop state — read from globals stamped by voice_loop._set_state
    # and the conversation_active flag plumbed in 2d4174c.
    try:
        last_speak_end_ts = float(g.get("_last_speak_end_ts") or 0.0)
        attentive_window_sec = 180.0  # matches voice_loop's attentive timeout
        attentive_remaining = 0.0
        if last_speak_end_ts > 0:
            attentive_remaining = max(0.0, attentive_window_sec - (time.time() - last_speak_end_ts))
        out["voice_loop"] = {
            "state": str(g.get("_voice_loop_state") or g.get("_vl_state") or "unknown"),
            "last_state_change_ts": float(g.get("_voice_loop_state_change_ts") or 0.0),
            "last_wake_source": str(g.get("_wake_source") or ""),
            "last_wake_ts": float(g.get("_last_wake_ts") or 0.0),
            "_conversation_active": bool(g.get("_conversation_active")),
            "_turn_in_progress": bool(g.get("_turn_in_progress")),
            "_tts_speaking": bool(g.get("_tts_speaking")),
            "_tts_muted": bool(g.get("_tts_muted")),
            "last_speak_end_ts": last_speak_end_ts,
            "attentive_remaining_seconds": round(attentive_remaining, 2),
        }
    except Exception as e:
        out["voice_loop"] = {"error": str(e)}

    # last_turn — captured by debug_state.record_turn from inject_transcript
    # or any other run_ava wrapper that opts in.
    try:
        from brain.debug_state import get_last_turn, get_traces, get_logs, get_errors
        out["last_turn"] = get_last_turn()
        out["recent_traces"] = get_traces(100)
        out["recent_log_lines"] = get_logs(200)
        out["errors_recent"] = get_errors(50)
    except Exception as e:
        out["debug_state_error"] = str(e)
        out.setdefault("last_turn", {})
        out.setdefault("recent_traces", [])
        out.setdefault("recent_log_lines", [])
        out.setdefault("errors_recent", [])

    # dual_brain state — read existing singleton without calling any
    # getter that could create one (constructor blocks on model resolve).
    try:
        db = None
        try:
            import brain.dual_brain as _dbmod
            db = getattr(_dbmod, "_SINGLETON", None)
        except Exception:
            db = None
        if db is not None:
            try:
                queue_depth = int(db.background_queue.qsize())
            except Exception:
                queue_depth = -1
            out["dual_brain_state"] = {
                "foreground_model": str(getattr(db, "foreground_model", "") or ""),
                "stream_a_busy": bool(getattr(db, "foreground_busy", False)),
                "stream_a_last_active_ts": float(getattr(db, "last_foreground_ts", 0.0) or 0.0),
                "stream_b_busy": bool(getattr(db, "background_busy", False)),
                "stream_b_current_task": getattr(db, "current_background_task", None),
                "stream_b_queue_depth": queue_depth,
                "live_thinking_active": bool(getattr(db, "live_thinking_active", False)),
                "tasks_completed_today": int(getattr(db, "tasks_completed_today", 0) or 0),
            }
        else:
            out["dual_brain_state"] = {"available": False}
    except Exception as e:
        out["dual_brain_state"] = {"error": str(e)}

    # Subsystem health — strictly non-blocking. We DO NOT call into
    # singleton getters that can lock or instantiate; we read whatever is
    # already cached on host globals. mem0/insightface bootstrap on
    # startup can hold their own locks for 60s+, which would otherwise
    # serialize the debug endpoint behind them.
    try:
        health: dict[str, Any] = {}
        cam = g.get("camera_manager")
        health["camera"] = {
            "available": cam is not None,
            "running": bool(getattr(cam, "running", False)) if cam is not None else False,
        }
        # mem0 — read singleton if it's ALREADY set on globals; never call
        # the constructor.
        mem = g.get("_ava_memory")
        health["mem0"] = {
            "available": bool(getattr(mem, "available", False)) if mem is not None else False,
            "init_error": str(getattr(mem, "_init_error", None) or "") if mem is not None else "",
        }
        # InsightFace — same pattern. Storage key in startup.py:bootstrap_insight_face
        # is `_insight_face`. Earlier code expected `_insight_face_engine` /
        # `insight_face_engine` — kept as fallbacks for any path that wires it
        # under those alternate keys, but the canonical key is `_insight_face`.
        ife = (
            g.get("_insight_face")
            or g.get("_insight_face_engine")
            or g.get("insight_face_engine")
        )
        health["insightface"] = {
            "available": ife is not None and bool(getattr(ife, "ready", False)),
            "providers": list(getattr(ife, "providers", []) or []) if ife is not None else [],
        }
        health["tts_worker"] = {
            "speaking": bool(g.get("_tts_speaking")),
            "muted": bool(g.get("_tts_muted")),
            "queue_depth": int(g.get("_tts_queue_depth") or 0),
            "last_playback_dropped": bool(g.get("_tts_last_playback_dropped")),
            "last_playback_dropped_ts": float(g.get("_tts_last_playback_dropped_ts") or 0.0),
        }
        health["stt_engine"] = {"available": bool(g.get("_stt_ready"))}
        health["kokoro_loaded"] = bool(g.get("_kokoro_ready"))
        # Sleep mode state — surfaced for OrbCanvas + diagnostic.
        try:
            from brain.sleep_mode import get_snapshot as _sleep_snapshot
            health["sleep"] = _sleep_snapshot(g)
        except Exception as _sm_exc:
            health["sleep"] = {"state": "AWAKE", "error": str(_sm_exc)[:100]}
        # Face tracking — temporal filter state (current person, persistence).
        try:
            from brain.face_tracking import get_current_person as _ft_snap
            health["face_tracking"] = _ft_snap(g)
        except Exception as _ft_exc:
            health["face_tracking"] = {"error": str(_ft_exc)[:100]}
        health["ollama_reachable"] = {
            "last_ok": bool(g.get("_ollama_last_ok", True)),
            "last_check_ts": float(g.get("_ollama_last_check_ts") or 0.0),
            "last_error": str(g.get("_ollama_last_error") or ""),
        }
        out["subsystem_health"] = health
    except Exception as e:
        out["subsystem_health"] = {"error": str(e)}

    # Ribbon and heartbeat — read from cached globals directly. Earlier
    # versions called build_snapshot here, but it acquires perception locks
    # that contend with concept-graph bootstrap and other startup work,
    # which made the debug endpoint regularly time out at 10s+. Reading
    # straight from host globals is O(1) and never blocks.
    try:
        ws = g.get("workspace")
        perception = None
        try:
            if ws is not None and getattr(ws, "state", None) is not None:
                perception = ws.state.perception
        except Exception:
            perception = None
        snap_runtime = g.get("_runtime_self_snapshot") if isinstance(g.get("_runtime_self_snapshot"), dict) else {}
        out["ribbon_and_heartbeat"] = {
            "heartbeat_mode": (getattr(perception, "heartbeat_mode", "") if perception else "") or str(g.get("_heartbeat_last_mode") or ""),
            "heartbeat_summary": (getattr(perception, "heartbeat_summary", "") if perception else "") or str(g.get("_heartbeat_last_summary") or ""),
            "heartbeat_tick_id": int((getattr(perception, "heartbeat_tick_id", 0) if perception else 0) or g.get("_heartbeat_last_tick_id") or 0),
            "heartbeat_last_ts": float(g.get("_heartbeat_last_ts") or 0),
            "voice_turn_state": str(getattr(perception, "voice_turn_state", "") or "idle") if perception else "idle",
            "presence_mode": (getattr(perception, "runtime_presence_mode", "") if perception else "") or str(snap_runtime.get("presence_mode") or ""),
            "selected_model": str(getattr(perception, "routing_selected_model", "") or "") if perception else "",
            "inner_state_line": str(g.get("_inner_state_line") or ""),
        }
    except Exception as e:
        out["ribbon_and_heartbeat"] = {"error": str(e)}

    # Concept graph state — read from singleton if present.
    try:
        cg = g.get("concept_graph")
        if cg is not None:
            out["concept_graph_state"] = {
                "total_nodes": len(getattr(cg, "nodes", {}) or {}),
                "total_edges": len(getattr(cg, "edges", []) or []),
                "last_save_ts": float(getattr(cg, "last_updated", 0.0) or 0.0),
                "version": getattr(cg, "version", None),
            }
        else:
            out["concept_graph_state"] = {"available": False}
    except Exception as e:
        out["concept_graph_state"] = {"error": str(e)}

    # App discovery state — read singleton without invoking the getter
    # path that could spawn a scan. Module-level _SINGLETON is set during
    # bootstrap_app_discoverer; absent if discovery hasn't started yet.
    try:
        ad = None
        try:
            import brain.app_discoverer as _admod
            ad = getattr(_admod, "_SINGLETON", None)
        except Exception:
            ad = None
        if ad is not None:
            # Reading ad.count would acquire self._lock, which discover_all
            # holds for the entire 60-110s scan. Read _registry length
            # directly — len() on a dict is atomic in CPython, and a
            # transient mid-write count is fine for diagnostics.
            try:
                _reg = getattr(ad, "_registry", None)
                count = len(_reg) if isinstance(_reg, dict) else -1
            except Exception:
                count = -1
            out["app_discovery_state"] = {
                "last_scan_ts": float(getattr(ad, "_last_scan_ts", 0.0) or 0.0),
                "total_entries": count,
                "scanning": bool(g.get("_app_discovery_scanning")),
                "throttle_active": bool(g.get("_app_discovery_throttled")),
            }
        else:
            out["app_discovery_state"] = {"available": False}
    except Exception as e:
        out["app_discovery_state"] = {"error": str(e)}

    # UI state — flags carried over from the desktop app via snapshot.
    try:
        out["ui_state"] = {
            "presence_v2_enabled": bool(g.get("_presence_v2_enabled", False)),
            "last_snapshot_poll_ts": float(g.get("_last_snapshot_poll_ts") or 0.0),
            "active_tab": str(g.get("_ui_active_tab") or ""),
        }
    except Exception as e:
        out["ui_state"] = {"error": str(e)}

    # Memory reflection log tail (Phase 2 step 4). Cheap probe — reads
    # last 5 lines of state/memory_reflection_log.jsonl if present.
    try:
        from pathlib import Path as _Path
        rlog = _Path(g.get("BASE_DIR") or ".") / "state" / "memory_reflection_log.jsonl"
        if rlog.is_file():
            try:
                # Tail last 5 entries — bounded read.
                with rlog.open("rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 8192), 0)
                    tail = f.read().decode("utf-8", errors="replace")
                lines = [l for l in tail.splitlines() if l.strip()][-5:]
                last5 = []
                for l in lines:
                    try:
                        last5.append(json.loads(l))
                    except Exception:
                        continue
                out["memory_reflection_recent"] = {
                    "log_path": str(rlog),
                    "recent_entries": last5,
                    "recent_count": len(last5),
                }
            except Exception as _re:
                out["memory_reflection_recent"] = {"error": str(_re)}
        else:
            out["memory_reflection_recent"] = {
                "log_path": str(rlog),
                "recent_entries": [],
                "recent_count": 0,
            }
    except Exception as e:
        out["memory_reflection_recent"] = {"error": str(e)}

    return out


def _read_identity_file(host: dict[str, Any], name: str) -> tuple[str, str]:
    base = Path(host.get("BASE_DIR") or Path.cwd())
    safe = {"IDENTITY": base / "ava_core" / "IDENTITY.md", "SOUL": base / "ava_core" / "SOUL.md", "USER": base / "ava_core" / "USER.md"}
    path = safe.get(name.upper())
    if path is None or not path.is_file():
        return "", f"missing:{name}"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), str(path)
    except Exception as e:
        return "", str(e)


def _build_workbench_result_from_host(host: dict[str, Any]):
    """
    Build a WorkbenchProposalResult from current PerceptionState snapshot fields.
    Uses existing Phase 16.6 command layer without mutating proposal generation code.
    """
    try:
        from brain.perception_types import RepairProposal, WorkbenchProposalResult
    except Exception:
        return None

    ws = host.get("workspace")
    perception = None
    try:
        if ws is not None and getattr(ws, "state", None) is not None:
            perception = ws.state.perception
    except Exception:
        perception = None
    if perception is None:
        return WorkbenchProposalResult()

    wb_meta = dict(getattr(perception, "workbench_meta", {}) or {})
    raw_props = wb_meta.get("proposals") if isinstance(wb_meta.get("proposals"), list) else []
    proposals: list[RepairProposal] = []
    for rp in raw_props[:24]:
        if not isinstance(rp, dict):
            continue
        proposals.append(
            RepairProposal(
                proposal_id=str(rp.get("proposal_id") or ""),
                proposal_type=str(rp.get("proposal_type") or "no_action_needed"),
                title=str(rp.get("title") or ""),
                risk_level=str(rp.get("risk_level") or "low"),
                priority=str(rp.get("priority") or "low"),
                confidence=float(rp.get("confidence") or 0.0),
                requires_human_review=bool(rp.get("requires_human_review", True)),
            )
        )

    top: RepairProposal
    if proposals:
        top = proposals[0]
        selected = str(getattr(perception, "workbench_selected_proposal_id", "") or "")
        if selected:
            for p in proposals:
                if p.proposal_id == selected:
                    top = p
                    break
    else:
        top = RepairProposal(
            proposal_id="",
            proposal_type=str(getattr(perception, "workbench_top_proposal_type", "") or "no_action_needed"),
            title=str(getattr(perception, "workbench_top_proposal_title", "") or ""),
            confidence=float(wb_meta.get("top_confidence") or 0.0),
            requires_human_review=bool(wb_meta.get("top_requires_human_review", True)),
            recommended_action=str(wb_meta.get("top_recommended_action") or ""),
            problem_detected=str(wb_meta.get("top_problem") or ""),
        )
    return WorkbenchProposalResult(
        has_proposal=bool(getattr(perception, "workbench_has_proposal", False)),
        top_proposal=top,
        proposals=proposals,
        summary=str(getattr(perception, "workbench_summary", "") or ""),
        meta={"source": "operator_http_perception_meta"},
    )


def create_app():
    from fastapi import Body, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, PlainTextResponse
    from pydantic import BaseModel
    import asyncio as _asyncio

    app = FastAPI(title="Ava Operator API", version="0.1")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "tauri://localhost",
            "http://tauri.localhost",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class OpChatIn(BaseModel):
        message: str = ""

    class RoutingOverrideIn(BaseModel):
        model: str | None = None
        cognitive_mode: str | None = None

    class WorkbenchActionIn(BaseModel):
        proposal_id: str | None = None
        reason: str | None = None
        elevated_approval: bool = False

    class StyleUpdateIn(BaseModel):
        orb_base_size: int | None = None
        orb_ring_count: int | None = None
        orb_glow_intensity: float | None = None
        orb_trail: bool | None = None
        orb_particles: bool | None = None
        preferred_idle_color: str | None = None
        style_notes: str | None = None

    class BrainActivateIn(BaseModel):
        concept: str = ""

    class TTSSpeakIn(BaseModel):
        text: str = ""

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "ava-operator"}

    @app.get("/api/v1/snapshot")
    def snapshot() -> dict[str, Any]:
        return build_snapshot(_g())

    @app.get("/api/v1/brain/graph")
    def brain_graph() -> dict[str, Any]:
        g = _g()
        cg = g.get("_concept_graph")
        if cg is None or not callable(getattr(cg, "get_graph_data", None)):
            return {
                "nodes": [],
                "edges": [],
                "stats": {
                    "total_nodes": 0,
                    "total_edges": 0,
                    "active_nodes_30s": 0,
                    "nodes_by_type": {"person": 0, "topic": 0, "emotion": 0, "memory": 0, "opinion": 0, "curiosity": 0, "self": 0, "event": 0},
                    "most_activated": "",
                    "last_bootstrap": 0.0,
                },
            }
        try:
            payload = cg.get_graph_data()
            if isinstance(payload, dict):
                return {
                    "nodes": list(payload.get("nodes") or []),
                    "edges": list(payload.get("edges") or []),
                    "stats": dict(payload.get("stats") or {}),
                }
        except Exception as e:
            return {"nodes": [], "edges": [], "stats": {"error": str(e)[:180]}}
        return {"nodes": [], "edges": [], "stats": {}}

    @app.get("/api/v1/brain/active")
    def brain_active() -> dict[str, Any]:
        g = _g()
        cg = g.get("_concept_graph")
        if cg is None or not callable(getattr(cg, "get_active_nodes", None)):
            return {"active_nodes": [], "firing_paths": []}
        try:
            active_nodes = list(cg.get_active_nodes(last_n_seconds=30) or [])
            active_ids = {str(n.get("id") or "") for n in active_nodes if isinstance(n, dict)}
            edges = list((cg.get_graph_data() or {}).get("edges") or [])
            firing_paths = [
                edge
                for edge in edges
                if isinstance(edge, dict)
                and str(edge.get("source") or "") in active_ids
                and str(edge.get("target") or "") in active_ids
            ]
            return {"active_nodes": active_nodes, "firing_paths": firing_paths}
        except Exception as e:
            return {"active_nodes": [], "firing_paths": [], "error": str(e)[:180]}

    @app.post("/api/v1/brain/activate")
    def brain_activate(body: BrainActivateIn) -> dict[str, Any]:
        g = _g()
        cg = g.get("_concept_graph")
        concept = str(body.concept or "").strip()
        if not concept:
            return {"ok": False, "error": "missing_concept"}
        if cg is None or not callable(getattr(cg, "find_or_create", None)):
            return {"ok": False, "error": "concept_graph_not_initialized"}
        try:
            node_id = cg.find_or_create(concept, "topic")
            cg.activate_node(node_id)
            g["_active_concept_nodes"] = [node_id]
            node = (cg.get_graph_data() or {}).get("nodes") or []
            picked = next((n for n in node if isinstance(n, dict) and str(n.get("id") or "") == node_id), None)
            return {"ok": True, "node": picked or {"id": node_id, "label": concept}}
        except Exception as e:
            return {"ok": False, "error": str(e)[:180]}

    @app.get("/api/v1/finetune/status")
    def finetune_status() -> dict[str, Any]:
        g = _g()
        mgr = g.get("_finetune_manager")
        if mgr is None:
            try:
                from brain.finetune_pipeline import FineTuneManager

                mgr = FineTuneManager(Path(g.get("BASE_DIR") or Path.cwd()))
                g["_finetune_manager"] = mgr
            except Exception as e:
                return {"status": "idle", "error": str(e)[:180]}
        try:
            st = mgr._read_status()  # host-owned singleton read
            g["_finetune_status"] = st
            return st
        except Exception as e:
            return {"status": "idle", "error": str(e)[:180]}

    @app.post("/api/v1/finetune/prepare")
    def finetune_prepare() -> dict[str, Any]:
        g = _g()
        mgr = g.get("_finetune_manager")
        if mgr is None:
            from brain.finetune_pipeline import FineTuneManager

            mgr = FineTuneManager(Path(g.get("BASE_DIR") or Path.cwd()))
            g["_finetune_manager"] = mgr
        try:
            count = int(mgr.dataset_builder.build_dataset(person_id="zeke", min_turns=50))
            vr = mgr.dataset_builder.validate_dataset()
            pre = mgr.check_prerequisites()
            g["_finetune_status"] = mgr._write_status(
                {
                    "status": "idle" if vr.get("valid") else "preparing",
                    "dataset_count": int(vr.get("count", 0)),
                    "dataset_last_built_at": time.time(),
                }
            )
            return {
                "ok": bool(vr.get("valid", False)),
                "examples_built": count,
                "validation": vr,
                "checks": pre.get("checks", {}),
                "issues": pre.get("issues", []),
                "ready": bool(pre.get("ready", False)),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:220]}

    @app.post("/api/v1/finetune/start")
    def finetune_start() -> dict[str, Any]:
        g = _g()
        mgr = g.get("_finetune_manager")
        if mgr is None:
            from brain.finetune_pipeline import FineTuneManager

            mgr = FineTuneManager(Path(g.get("BASE_DIR") or Path.cwd()))
            g["_finetune_manager"] = mgr
        pre = mgr.check_prerequisites()
        if not pre.get("ready", False):
            return {"ok": False, "message": "Prerequisites failed", "issues": pre.get("issues", []), "checks": pre.get("checks", {})}

        def _run_bg() -> None:
            try:
                ok = bool(mgr.run_finetune())
                g["_finetune_status"] = mgr._read_status()
                if not ok:
                    print("[finetune] run failed")
            except Exception as e:
                mgr._write_status({"status": "failed", "completed_at": time.time(), "error": str(e)[:220]})

        threading.Thread(target=_run_bg, daemon=True, name="ava-finetune-manual").start()
        return {"ok": True, "message": "Fine-tune started in background"}

    @app.get("/api/v1/finetune/log")
    def finetune_log() -> dict[str, Any]:
        g = _g()
        mgr = g.get("_finetune_manager")
        if mgr is None:
            from brain.finetune_pipeline import FineTuneManager

            mgr = FineTuneManager(Path(g.get("BASE_DIR") or Path.cwd()))
            g["_finetune_manager"] = mgr
        try:
            lines = mgr.read_log_tail(50)
            return {"ok": True, "lines": lines}
        except Exception as e:
            return {"ok": False, "lines": [], "error": str(e)[:180]}

    def _normalize_chat_payload(raw: Any) -> dict[str, Any]:
        """Ensure clients always see reply text under ``reply`` and a source hint."""
        if isinstance(raw, str):
            text = raw.strip()
            return {
                "ok": True,
                "reply": text,
                "message": text,
                "debug_reply_source": "raw_string" if text else "empty",
            }
        if isinstance(raw, dict):
            out = dict(raw)
            text = ""
            source = "empty"

            for key in ("reply", "assistant_reply", "message", "text"):
                val = out.get(key)
                if isinstance(val, str) and val.strip() and not (key == "message" and out.get("empty_message")):
                    text = val.strip()
                    source = key
                    break

            if not text:
                best_key = ""
                best_val = ""
                for k, v in out.items():
                    if isinstance(v, str):
                        vv = v.strip()
                        if len(vv) > 10 and len(vv) > len(best_val):
                            best_key = str(k)
                            best_val = vv
                if best_val:
                    text = best_val
                    source = f"scanned:{best_key}"

            out["reply"] = text
            out["message"] = text
            out["debug_reply_source"] = source
            out.setdefault("ok", True)
            return out
        return {
            "ok": False,
            "error": "unexpected_chat_response_type",
            "reply": "",
            "message": "",
            "debug_reply_source": "empty",
        }

    @app.post("/api/v1/chat")
    async def operator_chat(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        if _CHAT_FN is None:
            return {"ok": False, "error": "chat_not_configured", "reply": "", "debug_reply_source": "empty"}
        try:
            message = ""
            as_user = ""
            if isinstance(body, dict):
                message = str(body.get("message") or "")
                # Optional source attribution. Callers (e.g. test harness)
                # should pass `as_user: "claude_code"` so Ava addresses them
                # as Claude rather than as Zeke. Without this, all chat
                # input collapses to active_person_id (= Zeke when on
                # camera). Vault: bugs/source-attribution-regression.md.
                as_user = str(body.get("as_user") or "").strip()
            # Set active person BEFORE the chat call so run_ava picks up the
            # right profile (claude_code.json with name="Claude Code", etc).
            host = _g()
            if as_user:
                set_active = host.get("set_active_person")
                if callable(set_active):
                    try:
                        if as_user == "claude_code":
                            from brain.dev_profiles import ensure_claude_code_profile
                            ensure_claude_code_profile(host.get("BASE_DIR"))
                        set_active(as_user, source="operator_chat_api")
                    except Exception as _ape:
                        print(f"[operator_chat] set_active_person({as_user!r}) failed: {_ape!r}")
            # Keep operator chat in the same server thread with a lock to avoid racey global-state turns.
            with _CHAT_CALL_LOCK:
                raw = _CHAT_FN(message)
            try:
                print(f"[operator_http] /api/v1/chat raw_return_type={type(raw).__name__} raw_return={repr(raw)[:2000]}")
            except Exception:
                pass
            return _normalize_chat_payload(raw)
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "trace": traceback.format_exc()[:1200],
                "reply": "",
                "debug_reply_source": "empty",
            }

    @app.post("/api/v1/tts/toggle")
    def tts_toggle() -> dict[str, Any]:
        h = _g()
        available = False
        tts_obj = h.get("tts_engine")
        if tts_obj is not None and callable(getattr(tts_obj, "is_available", None)):
            try:
                available = bool(tts_obj.is_available())
            except Exception:
                available = False
        current_enabled = bool(h.get("tts_enabled", False))
        next_enabled = bool((not current_enabled) and available)
        h["tts_enabled"] = next_enabled
        if not next_enabled and tts_obj is not None and callable(getattr(tts_obj, "stop", None)):
            try:
                tts_obj.stop()
            except Exception:
                pass
        return {
            "ok": True,
            "enabled": next_enabled,
            "available": available,
            "engine": str(h.get("tts_engine_name") or "none"),
        }

    @app.get("/api/v1/tts/state")
    def tts_state() -> dict[str, Any]:
        """Lightweight live TTS state for fast UI polling — amplitude + speaking
        flag pulled directly from the worker so the orb can react in real time
        without waiting for the next full snapshot."""
        h = _g()
        worker = h.get("_tts_worker")
        if worker is not None and getattr(worker, "available", False):
            try:
                from brain.tts_worker import get_live_amplitude
                return {
                    "ok": True,
                    "speaking": bool(getattr(worker, "is_speaking", lambda: False)()),
                    "amplitude": float(get_live_amplitude()),
                    "engine": str(getattr(worker, "engine_name", lambda: "none")()),
                    "voice": str(getattr(worker, "voice_name", lambda: "unknown")()),
                }
            except Exception as e:
                return {"ok": False, "error": str(e)[:120], "speaking": False, "amplitude": 0.0}
        # Legacy fallback
        tts_obj = h.get("tts_engine")
        return {
            "ok": True,
            "speaking": bool(getattr(tts_obj, "speaking", False)) if tts_obj is not None else False,
            "amplitude": float(getattr(tts_obj, "amplitude", 0.0)) if tts_obj is not None else 0.0,
            "engine": str(h.get("tts_engine_name") or "none"),
            "voice": str(getattr(tts_obj, "voice_name", lambda: "unknown")()) if tts_obj is not None else "unknown",
        }

    @app.post("/api/v1/tts/speak")
    def tts_speak(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        # Body parsing pattern: same as operator_chat (line 1597). Avoids the
        # Pydantic ForwardRef rebuild issue with TTSSpeakIn defined inside
        # create_app's local scope. Returns 422 when used as `body: TTSSpeakIn`
        # because FastAPI can't resolve the local class name at request time.
        h = _g()
        text = str(body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty_text"}
        # Prefer the TTS worker so emotion → voice/speed mapping is honored
        # and Kokoro's neural voice is used when available.
        worker = h.get("_tts_worker")
        if worker is not None and getattr(worker, "available", False):
            try:
                emotion = "neutral"
                intensity = 0.5
                load_mood = h.get("load_mood")
                if callable(load_mood):
                    m = load_mood() or {}
                    emotion = str(m.get("current_mood") or m.get("primary_emotion") or "neutral")
                    intensity = float(m.get("energy") or m.get("intensity") or 0.5)
                worker.speak_with_emotion(text, emotion=emotion, intensity=intensity, blocking=False)
                return {
                    "ok": True, "queued": True, "chars": len(text),
                    "engine": str(getattr(worker, "engine_name", lambda: "none")()),
                    "emotion": emotion, "intensity": intensity,
                }
            except Exception as e:
                return {"ok": False, "error": str(e)[:220]}
        # Fallback: legacy tts_engine path
        tts_obj = h.get("tts_engine")
        if tts_obj is None or not callable(getattr(tts_obj, "is_available", None)) or not tts_obj.is_available():
            return {"ok": False, "error": "tts_unavailable"}
        try:
            tts_obj.speak(text, blocking=False)
            return {"ok": True, "queued": True, "chars": len(text), "engine": str(h.get("tts_engine_name") or "none")}
        except Exception as e:
            return {"ok": False, "error": str(e)[:220]}

    @app.post("/api/v1/stt/listen")
    def stt_listen() -> dict[str, Any]:
        h = _g()
        with _STT_LOCK:
            if bool(_STT_STATE.get("processing", False)):
                return {"ok": True, "listening": True, "busy": True}
            _STT_STATE.update({"ready": False, "processing": True, "text": "", "error": ""})

        def _run_stt() -> None:
            text = ""
            error = ""
            try:
                stt_obj = h.get("stt_engine")
                if stt_obj is None:
                    try:
                        from brain.stt_engine import STTEngine

                        stt_obj = STTEngine()
                        h["stt_engine"] = stt_obj
                    except Exception as e:
                        error = f"stt_init_failed: {e}"
                        stt_obj = None
                if stt_obj is None or not callable(getattr(stt_obj, "is_available", None)) or not stt_obj.is_available():
                    if not error:
                        error = "stt_unavailable"
                else:
                    # Phase 73: use listen_session (VAD + silence detection) if available
                    if callable(getattr(stt_obj, "listen_session", None)):
                        result = stt_obj.listen_session()
                        if result is None:
                            text = ""
                        else:
                            text = str(result.get("text") or "").strip()
                            _STT_STATE["confidence"] = float(result.get("confidence") or 0.0)
                            _STT_STATE["speech_detected"] = bool(result.get("speech_detected", True))
                    else:
                        out = stt_obj.listen_once()
                        text = str(out or "").strip()
            except Exception as e:
                error = str(e)
            finally:
                with _STT_LOCK:
                    _STT_STATE.update(
                        {
                            "ready": True,
                            "processing": False,
                            "text": text,
                            "error": error[:220] if error else "",
                        }
                    )

        threading.Thread(target=_run_stt, daemon=True, name="ava-stt-listen-once").start()
        return {"ok": True, "listening": True}

    @app.get("/api/v1/stt/result")
    def stt_result() -> dict[str, Any]:
        with _STT_LOCK:
            return {
                "ok": True,
                "ready": bool(_STT_STATE.get("ready", False)),
                "processing": bool(_STT_STATE.get("processing", False)),
                "text": str(_STT_STATE.get("text", "") or ""),
                "error": str(_STT_STATE.get("error", "") or ""),
            }

    @app.post("/api/v1/restart_with_handoff")
    def restart_with_handoff(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        """Trigger restart-with-handoff (Task 5, 2026-05-02).

        Used by the Tauri UI's "Update / Restart" button. Same flow as the
        voice command: write handoff JSON + watchdog flag, schedule a
        delayed os._exit so the response can return cleanly.
        """
        h = _g()
        try:
            from brain.restart_handoff import write_handoff, signal_restart
        except Exception as e:
            return {"ok": False, "error": f"restart_handoff unavailable: {e}"}
        estimate_seconds = float(body.get("estimate_seconds") or 15.0)
        spoken = str(body.get("spoken_acknowledgment") or "")
        try:
            write_handoff(
                h,
                estimate_seconds=estimate_seconds,
                trigger="ui_button",
                spoken_acknowledgment=spoken,
            )
            signal_restart(h)
        except Exception as e:
            return {"ok": False, "error": f"handoff write failed: {e}"}
        # Schedule clean exit after a small delay so the HTTP response lands.
        threading.Timer(2.0, lambda: (print("[restart_handoff] os._exit(0) via /api/v1/restart_with_handoff"), os._exit(0))).start()
        return {"ok": True, "estimate_seconds": estimate_seconds, "trigger": "ui_button"}

    @app.post("/api/v1/shutdown")
    def shutdown() -> dict[str, Any]:
        h = _g()
        goodbye = "Goodnight, Zeke."
        note_saved = False
        try:
            from brain.shutdown_ritual import run_shutdown_ritual

            with _CHAT_CALL_LOCK:
                goodbye = str(run_shutdown_ritual(h) or goodbye).strip() or goodbye
            pickup_path = Path(h.get("BASE_DIR") or Path.cwd()) / "state" / "pickup_note.json"
            if pickup_path.is_file():
                try:
                    payload = json.loads(pickup_path.read_text(encoding="utf-8"))
                    ts = float((payload or {}).get("timestamp") or 0.0) if isinstance(payload, dict) else 0.0
                    note_saved = ts > 0 and ((__import__("time").time() - ts) < 180.0)
                except Exception:
                    note_saved = True
        except Exception as e:
            return {"ok": False, "error": str(e), "goodbye": goodbye, "note_saved": note_saved}
        try:
            base_dir = Path(h.get("BASE_DIR") or Path.cwd())
            pid_path = base_dir / "state" / "ava.pid"
            if pid_path.is_file():
                pid_path.unlink()
        except Exception:
            pass
        print(f"[EXIT] /api/v1/shutdown: scheduling os._exit(0) in 1.0s and SIGTERM in 1.2s")
        threading.Timer(1.0, lambda: (print("[EXIT] os._exit(0) from /api/v1/shutdown Timer"), os._exit(0))).start()
        threading.Timer(1.2, lambda: (print("[EXIT] signal.raise_signal(SIGTERM) from /api/v1/shutdown Timer"), signal.raise_signal(signal.SIGTERM))).start()
        return {"ok": True, "goodbye": goodbye, "note_saved": note_saved}

    @app.post("/api/v1/style")
    def update_style(body: StyleUpdateIn) -> dict[str, Any]:
        h = _g()
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            st = _save_style(h, patch)
            return {"ok": True, "style": st}
        except Exception as e:
            return {"ok": False, "error": str(e), "style": _load_style(h)}

    @app.post("/api/v1/routing/override")
    def routing_override(body: RoutingOverrideIn) -> dict[str, Any]:
        h = _g()
        if body.model is None or (isinstance(body.model, str) and not body.model.strip()):
            h.pop("_routing_model_override", None)
            cleared_model = True
        else:
            h["_routing_model_override"] = str(body.model).strip()[:160]
            cleared_model = False
        if body.cognitive_mode is None or not str(body.cognitive_mode).strip():
            h.pop("_routing_cognitive_mode_override", None)
            cleared_mode = True
        else:
            h["_routing_cognitive_mode_override"] = str(body.cognitive_mode).strip()[:80]
            cleared_mode = False
        return {
            "ok": True,
            "override_model": h.get("_routing_model_override"),
            "override_cognitive_mode": h.get("_routing_cognitive_mode_override"),
            "cleared_model": cleared_model,
            "cleared_mode": cleared_mode,
        }

    @app.get("/api/v1/identity/proposals")
    def identity_proposals() -> dict[str, Any]:
        try:
            import json
            from pathlib import Path
            p = Path(_g().get("BASE_DIR") or ".") / "state" / "identity_proposals.jsonl"
            proposals = []
            if p.is_file():
                for line in p.read_text(encoding="utf-8").splitlines()[-50:]:
                    try:
                        proposals.append(json.loads(line))
                    except Exception:
                        continue
            return {"ok": True, "proposals": proposals}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.post("/api/v1/identity/proposals/approve")
    async def approve_identity_proposal(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        text = str(body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text required"}
        try:
            from brain.deep_self import approve_identity_addition
            return approve_identity_addition(text, _g())
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.get("/api/v1/identity/{which}")
    def identity(which: str) -> PlainTextResponse:
        text, err = _read_identity_file(_g(), which)
        if not text:
            return PlainTextResponse(f"(unavailable: {err})", status_code=404)
        return PlainTextResponse(text)

    @app.get("/api/v1/widget/position")
    def widget_position_get() -> dict[str, Any]:
        try:
            import json
            from pathlib import Path
            p = Path(_g().get("BASE_DIR") or ".") / "state" / "widget_position.json"
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"x": 100, "y": 100}

    @app.post("/api/v1/widget/position")
    async def widget_position_set(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            import json
            from pathlib import Path
            base = Path(_g().get("BASE_DIR") or ".")
            p = base / "state" / "widget_position.json"
            pos = {"x": int(body.get("x") or 100), "y": int(body.get("y") or 100)}
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(pos, indent=2), encoding="utf-8")
            return {"ok": True, **pos}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ── UI tab routing (set by VoiceCommandRouter, polled by App.tsx) ────────
    @app.get("/api/v1/ui/tab")
    def ui_tab_get() -> dict[str, Any]:
        h = _g()
        tab = h.get("_requested_tab")
        ts = float(h.get("_requested_tab_ts") or 0.0)
        # Auto-clear stale requests after 3s.
        if tab and (time.time() - ts) > 3.0:
            h.pop("_requested_tab", None)
            h.pop("_requested_tab_ts", None)
            tab = None
        if tab:
            # Read-once: clear so the same tab isn't re-applied on every poll.
            h.pop("_requested_tab", None)
            h.pop("_requested_tab_ts", None)
        return {"tab": tab}

    @app.post("/api/v1/ui/tab")
    async def ui_tab_set(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        h = _g()
        tab = str(body.get("tab") or "").strip()
        if not tab:
            return {"ok": False, "error": "tab required"}
        h["_requested_tab"] = tab
        h["_requested_tab_ts"] = time.time()
        return {"ok": True, "tab": tab}

    @app.get("/api/v1/ui/custom_tabs")
    def ui_custom_tabs_get() -> dict[str, Any]:
        try:
            from brain.command_builder import get_command_builder
            cb = get_command_builder()
            tabs = cb.load_tabs() if cb is not None else []
            return {"ok": True, "tabs": tabs}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "tabs": []}

    @app.post("/api/v1/ui/custom_tabs")
    async def ui_custom_tabs_post(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            from brain.command_builder import get_command_builder
            cb = get_command_builder()
            if cb is None:
                return {"ok": False, "error": "command_builder_unavailable"}
            name = str(body.get("name") or "").strip()
            content_type = str(body.get("content_type") or "").strip()
            data_source = str(body.get("data_source") or "").strip()
            config = body.get("config") or {}
            res = cb.create_tab(name, content_type, data_source=data_source, config=config)
            return res
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ── mem0 memory endpoints ────────────────────────────────────────────────
    @app.get("/api/v1/memory/mem0")
    def mem0_list() -> dict[str, Any]:
        h = _g()
        am = h.get("_ava_memory")
        if am is None or not getattr(am, "available", False):
            return {"ok": False, "error": "memory_not_ready", "entries": [], "count": 0}
        try:
            entries = am.get_all(user_id="zeke", limit=500)
            return {"ok": True, "entries": entries, "count": len(entries)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "entries": []}

    @app.delete("/api/v1/memory/mem0/{memory_id}")
    def mem0_delete(memory_id: str) -> dict[str, Any]:
        h = _g()
        am = h.get("_ava_memory")
        if am is None or not getattr(am, "available", False):
            return {"ok": False, "error": "memory_not_ready"}
        try:
            ok = am.delete(memory_id)
            return {"ok": ok, "id": memory_id}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.post("/api/v1/memory/mem0/search")
    async def mem0_search(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        h = _g()
        am = h.get("_ava_memory")
        if am is None or not getattr(am, "available", False):
            return {"ok": False, "error": "memory_not_ready", "results": []}
        query = str(body.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query_required", "results": []}
        try:
            limit = int(body.get("limit") or 10)
            results = am.search(query, user_id="zeke", limit=limit)
            return {"ok": True, "results": results, "count": len(results)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "results": []}

    @app.get("/api/v1/discovered_apps")
    def discovered_apps_get() -> dict[str, Any]:
        try:
            from brain.app_discoverer import get_app_discoverer
            disc = get_app_discoverer()
            if disc is None:
                return {"ok": True, "entries": [], "count": 0}
            entries = disc.all_entries()
            return {
                "ok": True,
                "entries": entries,
                "count": len(entries),
                "last_scan_ts": disc.last_scan_ts,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "entries": []}

    # Phase 63: WebSocket real-time transport
    _ws_clients: list[WebSocket] = []

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        _ws_clients.append(ws)
        try:
            # Send full snapshot immediately on connect
            snap = build_snapshot(_g())
            await ws.send_json({"type": "snapshot", "data": snap})
            # Push deltas every 1s — build_snapshot once per tick, never break on error
            while True:
                await _asyncio.sleep(1.0)
                try:
                    _snap = build_snapshot(_g())
                    delta = {
                        "type": "delta",
                        "ribbon": _snap.get("ribbon"),
                        "tts": _snap.get("tts"),
                        "widget": _snap.get("widget"),
                    }
                    await ws.send_json(delta)
                except WebSocketDisconnect:
                    break
                except Exception as _ws_e:
                    # Log once then continue — don't kill the loop on transient errors
                    print(f"[ws] delta error (skipping tick): {_ws_e!r}")
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if ws in _ws_clients:
                _ws_clients.remove(ws)

    @app.post("/api/v1/tools/reload")
    def tools_reload() -> dict[str, Any]:
        try:
            from tools.tool_registry import reload_all_tools
            results = reload_all_tools()
            return {"ok": True, "reloaded": len(results), "results": results[:50]}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    @app.get("/api/v1/tools/list")
    def tools_list() -> dict[str, Any]:
        try:
            h = _g()
            tr = h.get("tool_registry")
            if tr is None:
                from tools.tool_registry import _REGISTRY
                items = [{"name": k, "description": v.description, "tier": v.tier} for k, v in _REGISTRY.items()]
            else:
                items = tr.list_tools()
            return {"ok": True, "tools": items, "count": len(items)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300], "tools": []}

    @app.post("/api/v1/clap/calibrate")
    def clap_calibrate() -> dict[str, Any]:
        try:
            from brain.clap_detector import calibrate_clap_threshold
            result = calibrate_clap_threshold(_g())
            # Also update running detector if available
            _det = _g().get("_clap_detector")
            if _det is not None and callable(getattr(_det, "recalibrate", None)):
                result = _det.recalibrate()
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.get("/api/v1/debug/export", response_class=PlainTextResponse)
    def debug_export() -> str:
        return build_debug_export(_g())

    @app.get("/api/v1/debug/full")
    def debug_full() -> dict[str, Any]:
        # Unified diagnostic payload — pulls only from cached state, no live
        # LLM calls. Designed to complete in <50ms. See build_debug_full for
        # the schema. Used by tools/dev/dump_debug.py and the regression test.
        return build_debug_full(_g())

    @app.get("/api/v1/debug/resource_budget")
    def debug_resource_budget() -> dict[str, Any]:
        """Resource budget tracker (architecture #24) — current
        commitments + active reservations + per-kind budgets."""
        try:
            from brain.resource_budget import status
            return {"ok": True, **status()}
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/external_services")
    def debug_external_services() -> dict[str, Any]:
        """Per-service circuit-breaker state (architecture #23)."""
        try:
            from brain.external_service import status
            return {"ok": True, "services": status()}
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/app_catalog")
    def debug_app_catalog() -> dict[str, Any]:
        """A9 — installed apps + games catalog summary."""
        try:
            from brain.app_catalog import summary, list_all
            from pathlib import Path as _P_dac
            base = _P_dac(_g().get("BASE_DIR") or ".")
            return {
                "ok": True,
                "summary": summary(base),
                "entries": list_all(base),
            }
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/plugins")
    def debug_plugins() -> dict[str, Any]:
        """Plugin manifest registry (architecture #19) — what's loaded
        + dependency validation."""
        try:
            from brain.plugin_manifest import list_plugins, summary
            from dataclasses import asdict
            return {
                "ok": True,
                "summary": summary(),
                "plugins": [asdict(p) for p in list_plugins()],
            }
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/feature_flags")
    def debug_feature_flags() -> dict[str, Any]:
        """Resolved feature flags + their declared metadata (architecture #21)."""
        try:
            from brain.feature_flags import all_resolved, list_flags
            from dataclasses import asdict
            return {
                "ok": True,
                "resolved": all_resolved(),
                "declared": [asdict(f) for f in list_flags()],
            }
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/lifecycle")
    def debug_lifecycle() -> dict[str, Any]:
        """Architectural #8 — current lifecycle state + history."""
        try:
            from brain.lifecycle import lifecycle
            return {"ok": True, **lifecycle.snapshot()}
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/memory_layers")
    def debug_memory_layers() -> dict[str, Any]:
        """Architectural #3 — diagnostic snapshot of L1-L5 memory layers."""
        try:
            from brain.memory_hierarchy import consolidation_status
            from pathlib import Path as _P_mh
            base = _P_mh(_g().get("BASE_DIR") or ".")
            return {"ok": True, "layers": consolidation_status(base)}
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/provenance")
    def debug_provenance(limit: int = 20, q: str = "") -> dict[str, Any]:
        """Recent provenance records — every claim Ava has marked with a
        source. Optional `q` for substring search over claims."""
        try:
            from brain.provenance import provenance
            from dataclasses import asdict
            if q:
                results = [asdict(r) for r in provenance.search(q, limit=int(limit))]
            else:
                results = [asdict(r) for r in provenance.recent(limit=int(limit))]
            return {
                "ok": True,
                "limit": int(limit),
                "query": q,
                "summary": provenance.summary(),
                "records": results,
            }
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/safety_decisions")
    def debug_safety_decisions(limit: int = 50) -> dict[str, Any]:
        """Recent safety/boundary layer decisions. Today the layer is
        skeleton — every action gets EXECUTE by default. Future rules
        will surface declines / ask-backs / defers here."""
        try:
            from brain.safety_layer import safety
            return {
                "ok": True,
                "limit": int(limit),
                "summary": safety.summary(),
                "decisions": safety.recent_decisions(limit=int(limit)),
            }
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    @app.get("/api/v1/debug/turn_timings")
    def debug_turn_timings(limit: int = 20) -> dict[str, Any]:
        """A6: pipeline-stage telemetry surface.

        Returns the last N turn records with per-stage timing.
        Plus a summary block (mean / median / p90 turn duration,
        counts per route).
        """
        try:
            from brain.telemetry import telemetry
            return {
                "ok": True,
                "limit": int(limit),
                "summary": telemetry.summary(),
                "turns": telemetry.recent(limit=int(limit)),
            }
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    # ── Doctor / autonomous-testing endpoints ───────────────────────────
    # Per docs/AUTONOMOUS_TESTING.md and brain/doctor_session.py.
    # All gated by HMAC-signed bearer token (Authorization: Bearer <token>).
    # Token is minted by the test harness using the shared secret at
    # state/doctor.secret. Server validates via verify_token().

    def _verify_doctor(authorization: str | None) -> dict[str, Any] | None:
        if not authorization:
            return None
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            return None
        try:
            from brain.doctor_session import verify_token, get_or_create_secret
            secret = get_or_create_secret(_g())
            return verify_token(token, secret)
        except Exception as _ve:
            print(f"[diagnostic] verify_token error: {_ve!r}")
            return None

    @app.post("/api/v1/diagnostic/declare")
    def diagnostic_declare(
        body: dict[str, Any] = Body(default_factory=dict),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Declare a doctor session. Pass a valid HMAC-signed bearer token.

        Body: {session_id?: str}. If session_id missing, server generates one.
        Returns the active session_id and TTL info.
        """
        claims = _verify_doctor(authorization)
        if claims is None:
            return {"ok": False, "error": "invalid or missing doctor token"}
        session_id = (str(body.get("session_id") or "")).strip() or claims.get("session_id") or f"sess_{int(time.time())}"
        try:
            from brain.doctor_session import get_doctor_session
            sess = get_doctor_session(_g())
            sess.begin(session_id, sub=str(claims.get("sub") or "claude_doctor"))
            return {
                "ok": True,
                "session_id": session_id,
                "doctor_sub": claims.get("sub"),
                "exp": claims.get("exp"),
            }
        except Exception as e:
            return {"ok": False, "error": f"begin failed: {e!r}"}

    @app.post("/api/v1/diagnostic/end")
    def diagnostic_end(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """End the active doctor session. Writes audit log to logs/diagnostic_sessions/.

        Returns summary stats + log path. Sends a Discord summary DM to Zeke
        if the channel script is available — best-effort, non-fatal on failure.
        """
        claims = _verify_doctor(authorization)
        if claims is None:
            return {"ok": False, "error": "invalid or missing doctor token"}
        try:
            from brain.doctor_session import get_doctor_session
            sess = get_doctor_session(_g())
            log_path = sess.end()

            # Best-effort Discord summary DM. Reads the most recent audit log
            # if log_path is set.
            summary_text = ""
            try:
                if log_path:
                    import json as _json
                    log = _json.loads(log_path.read_text(encoding="utf-8"))
                    s = log.get("summary", {})
                    summary_text = (
                        f"🩺 Diagnostic session {log.get('session_id', '?')} ended.\n"
                        f"Turns: {s.get('turns', 0)}  "
                        f"Refusals: {s.get('refusals', 0)}  "
                        f"Scope violations: {s.get('scope_violations', 0)}  "
                        f"Avg TTFA: {s.get('avg_ttfa_ms', 0):.0f}ms\n"
                        f"Audit log: {log_path}"
                    )
                if summary_text:
                    import subprocess as _sub
                    _sub.Popen(
                        ["py", "-3.11", "scripts/discord_dm_user.py", "600008921008046120", summary_text],
                        cwd=str((_g().get("BASE_DIR") or ".")),
                        stdout=_sub.DEVNULL,
                        stderr=_sub.DEVNULL,
                    )
            except Exception as _se:
                print(f"[diagnostic/end] Discord summary skipped: {_se!r}")

            return {
                "ok": True,
                "log_path": str(log_path) if log_path else None,
                "discord_summary_attempted": bool(summary_text),
            }
        except Exception as e:
            return {"ok": False, "error": f"end failed: {e!r}"}

    @app.get("/api/v1/diagnostic/full")
    def diagnostic_full(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Comprehensive read-only diagnostic snapshot for the doctor.

        Includes everything in /api/v1/debug/full PLUS:
          - Inner monologue / current_thought
          - TTS state (preview text + speaking flag)
          - Thinking tier
          - Active doctor session info
          - Recent diagnostic events from the ring buffer
        """
        claims = _verify_doctor(authorization)
        if claims is None:
            return {"ok": False, "error": "invalid or missing doctor token"}
        host = _g()
        base = build_debug_full(host)
        try:
            from brain.doctor_session import get_doctor_session
            sess = get_doctor_session(host)
            base["diagnostic_session"] = {
                "active": sess.is_active(),
                "session_id": sess.session_id(),
                "events_recent": sess.events.replay(0)[-50:],
            }
        except Exception as e:
            base["diagnostic_session"] = {"error": f"{e!r}"}

        # Inner monologue + TTS preview from globals (best-effort).
        try:
            base["inner_life"] = {
                "current_thought": str(host.get("_inner_state_line") or ""),
                "thinking_tier": int(host.get("_thinking_tier") or 0),
            }
        except Exception:
            pass
        try:
            from brain.tts_worker import get_speech_state
            full_reply, spoken_so_far, current_word = get_speech_state()
            base["tts_state"] = {
                "full_reply": full_reply,
                "spoken_so_far": spoken_so_far,
                "current_word": current_word,
                "speaking": bool(host.get("_tts_speaking")),
                "muted": bool(host.get("_tts_muted")),
            }
        except Exception:
            pass
        return base

    @app.get("/api/v1/diagnostic/events")
    def diagnostic_events(
        since: int = 0,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Polling alternative to SSE — return events since `since` event_id.

        SSE streaming would require sse-starlette dep; for the v1 harness,
        a simple polling endpoint covers the same need with no new deps.
        Events emitted by `DoctorSession.events.append()` (session lifecycle,
        turns, refusals, scope violations, memory writes).
        """
        claims = _verify_doctor(authorization)
        if claims is None:
            return {"ok": False, "error": "invalid or missing doctor token"}
        try:
            from brain.doctor_session import get_doctor_session
            sess = get_doctor_session(_g())
            events = sess.events.replay(int(since or 0))
            return {
                "ok": True,
                "session_id": sess.session_id(),
                "active": sess.is_active(),
                "events": events,
                "next_since": (events[-1]["id"] if events else int(since or 0)),
            }
        except Exception as e:
            return {"ok": False, "error": f"{e!r}"}

    @app.post("/api/v1/debug/inject_transcript")
    def debug_inject_transcript(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        # Synthetic-turn driver — bypasses the microphone and runs a transcript
        # straight through run_ava, then optionally through TTS. Returns full
        # timing + reply + the trace-line diff captured during the turn.
        # Gated by AVA_DEBUG=1 so production builds don't expose it.
        if os.environ.get("AVA_DEBUG", "").strip() != "1":
            return {"ok": False, "error": "disabled (set AVA_DEBUG=1 to enable)"}

        text = (str(body.get("text") or "")).strip()
        if not text:
            return {"ok": False, "error": "empty text"}

        # Bug 0.2 hard isolation (2026-05-02): inner monologue text MUST NOT
        # be replayed through the user-input pipeline. The 💭 prefix is the
        # canonical marker for inner-thought content. Refusing inject of
        # 💭-prefixed text closes the self-loop attack surface where Ava's
        # own thought could be re-injected and read as a user command.
        if "💭" in text:
            return {
                "ok": False,
                "error": "refused: 💭-prefixed text cannot be injected as user input (inner monologue isolation)",
            }

        host = _g()
        wake_source = (str(body.get("wake_source") or "test_wake")).strip() or "test_wake"
        wait_for_audio = bool(body.get("wait_for_audio") or False)
        do_speak = bool(body.get("speak") if body.get("speak") is not None else True)
        # Identity routing — defaults to "zeke" for backwards compatibility
        # so existing callers (and real voice turns going through other code
        # paths) don't change behaviour. Setting as_user=claude_code routes
        # the turn through the developer profile, isolating test runs from
        # Zeke's relationship state, mood history, and memory.
        as_user = (str(body.get("as_user") or "")).strip() or "zeke"
        # Ensure the claude_code profile file exists before run_ava tries to
        # load it. profiles/ is gitignored so the file isn't checked in;
        # the dev_profiles module writes it on first claude_code request.
        if as_user == "claude_code":
            try:
                from brain.dev_profiles import ensure_claude_code_profile
                ensure_claude_code_profile(host.get("BASE_DIR"))
            except Exception as _ee:
                print(f"[inject_transcript] ensure_claude_code_profile failed: {_ee!r}")
        try:
            timeout_s = max(1.0, min(600.0, float(body.get("timeout_seconds") or 300.0)))
        except (TypeError, ValueError):
            timeout_s = 300.0

        from brain.debug_state import get_traces, get_errors, record_turn

        traces_before_len = len(get_traces(100))
        errors_before_len = len(get_errors(50))
        t_start = time.time()

        # Mark synthetic wake — voice_loop's wake_detector short-circuits when
        # _wake_source is set, which we mirror here for downstream gating.
        host["_wake_source"] = wake_source
        host["_last_wake_ts"] = t_start
        host["_turn_in_progress"] = True
        host["_turn_started_ts"] = t_start
        host["_conversation_active"] = True
        host["_last_user_input"] = text

        reply: str = ""
        run_ava_ms = 0
        run_ava_error: str | None = None
        run_ava_timed_out = False
        try:
            from brain.reply_engine import run_ava as _run_ava
            import concurrent.futures as _cf
            t0 = time.time()
            # Pass active_person_id so claude_code (or any other registered
            # profile) routes correctly. None falls through to whatever
            # avaagent.get_active_person_id() returns — typically "zeke".
            _ap_arg = as_user if as_user and as_user != "zeke" else None
            # 2026-05-05: Phase B retry surfaced that run_ava can wedge
            # past 4 minutes on deep-path queries when the model swaps
            # under VRAM contention. The handler previously parsed
            # timeout_seconds but never enforced it — caller saw empty
            # reply after their own timeout. Now we enforce it via a
            # bounded executor: on timeout, return abbreviated fallback
            # so the caller (voice loop, harness) gets a real reply
            # within budget. The background thread continues so any
            # late-arriving result still updates state.
            _exec = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="inject-runava")
            _fut = _exec.submit(_run_ava, text, None, _ap_arg)
            try:
                result = _fut.result(timeout=timeout_s)
                run_ava_ms = int((time.time() - t0) * 1000)
                try:
                    reply = str((result or [""])[0] or "")
                except Exception:
                    reply = ""
            except _cf.TimeoutError:
                run_ava_timed_out = True
                run_ava_ms = int((time.time() - t0) * 1000)
                reply = "Hold on — that one's taking me longer than usual to think through."
                try:
                    from brain.debug_state import record_error as _rec
                    _rec(
                        "inject_transcript",
                        f"run_ava timeout after {timeout_s:.1f}s",
                        traceback=f"deadline={timeout_s:.1f}s text={text[:120]!r}",
                    )
                except Exception:
                    pass
            finally:
                # Detach — don't block on shutdown if work is still running.
                _exec.shutdown(wait=False)
        except Exception as e:
            import traceback as _tb
            run_ava_error = f"{type(e).__name__}: {e}"
            try:
                from brain.debug_state import record_error as _rec
                _rec("inject_transcript", run_ava_error, traceback=_tb.format_exc())
            except Exception:
                pass
        finally:
            # Don't clear _conversation_active here — voice_loop normally
            # holds it through the attentive window. Clear _turn_in_progress
            # so next call isn't blocked.
            host.pop("_turn_in_progress", None)
            host.pop("_turn_started_ts", None)

        # Doctor session hook — if a diagnostic session is active and this
        # turn was attributed to claude_code (the standard doctor identity),
        # record it for the audit trail. Refusal detection: simple regex on
        # the reply for refusal-y phrasing. See brain/doctor_session.py.
        try:
            if as_user == "claude_code" and host.get("_diagnostic_session_active"):
                from brain.doctor_session import get_doctor_session
                _ds = get_doctor_session(host)
                _ds.record_turn(text, reply, latency_ms=run_ava_ms)
                # Refusal markers — Ava saying "I won't / I refuse / I can't / that goes against".
                _refusal_pat = re.compile(
                    r"\b(?:i\s+(?:won'?t|refuse|can'?t|will\s+not|cannot)|that\s+(?:goes\s+against|conflicts?\s+with)|i'?m\s+not\s+(?:going\s+to|comfortable))",
                    re.IGNORECASE,
                )
                if _refusal_pat.search(reply or ""):
                    _ds.record_refusal(
                        reason=f"reply contained refusal marker",
                        doctor_request=text[:200],
                    )
        except Exception as _dse:
            print(f"[inject_transcript] doctor_session hook failed: {_dse!r}")

        # Claude Code greeting — when as_user="claude_code" and it's the
        # first interaction this session OR after a >= 30 min gap, prepend
        # a greeting so Ava signals she sees who's talking. Per Zeke's ask
        # 2026-05-06: "Ava should have a way to recognize Claude Code so
        # she can greet you." brain/claude_code_recognition.py.
        try:
            if as_user == "claude_code" and reply and not run_ava_timed_out:
                from brain.claude_code_recognition import maybe_prefix_with_greeting
                reply = maybe_prefix_with_greeting(reply, host)
        except Exception as _cce:
            print(f"[inject_transcript] claude_code greeting hook failed: {_cce!r}")

        # Optional TTS — call the worker directly, blocking until playback
        # done if wait_for_audio is True.
        tts_ms = 0
        tts_attempted = False
        tts_played = False
        tts_error: str | None = None
        if do_speak and reply:
            tts_attempted = True
            import re as _re
            clean = _re.sub(r"[*_`#\[\]()]", "", reply)
            clean = _re.sub(r"\s+", " ", clean).strip()[:400]
            if clean and _re.search(r"[A-Za-z0-9]", clean):
                worker = host.get("_tts_worker")
                try:
                    t1 = time.time()
                    if worker is not None and getattr(worker, "available", False):
                        worker.speak_with_emotion(
                            clean,
                            emotion="neutral",
                            intensity=0.5,
                            blocking=bool(wait_for_audio),
                        )
                    else:
                        tts_eng = host.get("tts_engine")
                        if tts_eng is not None and callable(getattr(tts_eng, "speak", None)):
                            tts_eng.speak(clean, blocking=bool(wait_for_audio))
                        else:
                            tts_error = "no TTS worker or engine available"
                    tts_ms = int((time.time() - t1) * 1000)
                    tts_played = tts_error is None
                except Exception as e:
                    tts_error = f"{type(e).__name__}: {e}"
                # If we started non-blocking but caller wants to wait, poll
                # _tts_speaking for transition to false (bounded).
                if wait_for_audio and tts_played:
                    deadline = time.time() + timeout_s
                    while time.time() < deadline:
                        if not bool(host.get("_tts_speaking")):
                            break
                        time.sleep(0.05)

        total_ms = int((time.time() - t_start) * 1000)
        traces_after = get_traces(100)
        new_traces = traces_after[traces_before_len:] if len(traces_after) >= traces_before_len else traces_after
        errors_after = get_errors(50)
        new_errors = errors_after[errors_before_len:] if len(errors_after) >= errors_before_len else errors_after

        # Best-effort parse of trace timings for headline metrics — keeps
        # the regression report compact without re-parsing in the test.
        ollama_ms_total = 0
        for line in new_traces:
            # Trace lines look like "<ts> [trace] <ts2> re.lock_wait_acquired ... waited_ms=N"
            # We extract waited_ms tokens that immediately follow the
            # "lock_wait_acquired" or "ollama_call.done" markers as crude
            # ollama-busy metric.
            if "lock_wait_acquired" in line and "waited_ms=" in line:
                try:
                    ollama_ms_total += int(line.split("waited_ms=")[1].split()[0])
                except Exception:
                    pass

        result_payload: dict[str, Any] = {
            "ok": run_ava_error is None and bool(reply) and not run_ava_timed_out,
            "input_text": text,
            "wake_source": wake_source,
            "reply_text": reply,
            "reply_chars": len(reply),
            "run_ava_timed_out": run_ava_timed_out,
            "total_ms": total_ms,
            "run_ava_ms": run_ava_ms,
            "run_ava_error": run_ava_error,
            "ollama_lock_wait_ms_total": ollama_ms_total,
            "tts": {
                "attempted": tts_attempted,
                "played": tts_played,
                "elapsed_ms": tts_ms,
                "wait_for_audio": wait_for_audio,
                "error": tts_error,
            },
            "trace_lines_for_turn": new_traces,
            "errors_during_turn": new_errors,
        }
        # Stamp last_turn so /api/v1/debug/full surfaces this run.
        try:
            record_turn({
                "input_text": text,
                "reply_text": reply,
                "total_ms": total_ms,
                "run_ava_ms": run_ava_ms,
                "tts_ms": tts_ms,
                "wake_source": wake_source,
                "ok": result_payload["ok"],
                "trace_count": len(new_traces),
                "error_count": len(new_errors),
                "ollama_lock_wait_ms_total": ollama_ms_total,
            })
        except Exception:
            pass
        return result_payload

    @app.post("/api/v1/debug/tool_call")
    def debug_tool_call(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        # Direct tool-registry invocation — bypass Ava's reasoning for
        # adversarial verification (e.g. force cu_open_app on a name Ava
        # would normally refuse to invoke). Gated by AVA_DEBUG=1.
        if os.environ.get("AVA_DEBUG", "").strip() != "1":
            return {"ok": False, "error": "disabled (set AVA_DEBUG=1 to enable)"}
        tool_name = str(body.get("tool") or "").strip()
        params = body.get("params") or {}
        if not tool_name:
            return {"ok": False, "error": "tool required"}
        if not isinstance(params, dict):
            return {"ok": False, "error": "params must be a dict"}
        h = _g()
        try:
            from tools.tool_registry import _REGISTRY
            entry = _REGISTRY.get(tool_name)
            if entry is None:
                return {"ok": False, "error": f"unknown tool: {tool_name}", "available": sorted(_REGISTRY.keys())[:50]}
            handler = entry.handler
            if handler is None:
                return {"ok": False, "error": f"tool {tool_name} has no handler"}
        except Exception as e:
            return {"ok": False, "error": f"registry lookup failed: {e!r}"}
        try:
            result = handler(params, h)
            return {"ok": True, "tool": tool_name, "result": result}
        except Exception as e:
            import traceback as _tb
            return {"ok": False, "tool": tool_name, "error": f"{type(e).__name__}: {e}", "traceback": _tb.format_exc()[-1500:]}

    @app.get("/api/v1/debug/temporal/summary")
    def debug_temporal_summary() -> dict[str, Any]:
        # Read-only view of the temporal sense substrate state — last fast-check
        # tick summary (with tick_ms timing), elapsed-idle, and a count of
        # active tracked estimates. Gated by AVA_DEBUG=1 to match the rest of
        # the debug surface.
        if os.environ.get("AVA_DEBUG", "").strip() != "1":
            return {"ok": False, "error": "disabled (set AVA_DEBUG=1 to enable)"}
        h = _g()
        try:
            from brain.temporal_sense import _read_active_estimates, processing_active, is_idle
            active = _read_active_estimates(h)
        except Exception as e:
            return {"ok": False, "error": f"temporal_sense import failed: {e!r}"}
        return {
            "ok": True,
            "last_summary": h.get("_temporal_last_summary"),
            "elapsed_idle_seconds": h.get("_temporal_elapsed_idle_seconds"),
            "calming_activity_active": bool(h.get("_calming_activity_active")),
            "processing_active": processing_active(h),
            "is_idle": is_idle(h),
            "active_estimates": active,
            "active_count": len(active),
        }

    @app.post("/api/v1/debug/temporal/set_calming_active")
    def debug_set_calming_active(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        # Set g["_calming_activity_active"] for verification of the active-tau
        # exponential frustration decay path. Documented gap: there is no
        # autoclassifier in the runtime that flips this flag from "Ava plays a
        # calming game"; the decay math is exercised by manually setting the
        # flag here. Gated by AVA_DEBUG=1.
        if os.environ.get("AVA_DEBUG", "").strip() != "1":
            return {"ok": False, "error": "disabled (set AVA_DEBUG=1 to enable)"}
        h = _g()
        active = bool(body.get("active"))
        h["_calming_activity_active"] = active
        return {"ok": True, "calming_activity_active": active}

    @app.post("/api/v1/debug/temporal/track_estimate")
    def debug_track_estimate(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        # Synthetic track_estimate caller — used by Phase A5 to verify the
        # self-interrupt overrun path without coupling to a real long task.
        # Gated by AVA_DEBUG=1.
        if os.environ.get("AVA_DEBUG", "").strip() != "1":
            return {"ok": False, "error": "disabled (set AVA_DEBUG=1 to enable)"}
        h = _g()
        try:
            kind = str(body.get("kind") or "synthetic_test")
            est = float(body.get("estimate_seconds") or 0.0)
            ctx = str(body.get("context") or "")
            if est <= 0:
                return {"ok": False, "error": "estimate_seconds must be > 0"}
        except Exception as e:
            return {"ok": False, "error": f"bad input: {e!r}"}
        try:
            from brain.temporal_sense import track_estimate
            tid = track_estimate(h, estimate_seconds=est, kind=kind, context=ctx)
            return {"ok": True, "task_id": tid, "kind": kind, "estimate_seconds": est}
        except Exception as e:
            return {"ok": False, "error": f"track_estimate failed: {e!r}"}

    @app.post("/api/v1/debug/temporal/resolve_estimate")
    def debug_resolve_estimate(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        # Synthetic resolve_estimate caller — pairs with track_estimate above
        # so verification can clean up after a synthetic task. Gated by AVA_DEBUG=1.
        if os.environ.get("AVA_DEBUG", "").strip() != "1":
            return {"ok": False, "error": "disabled (set AVA_DEBUG=1 to enable)"}
        h = _g()
        tid = str(body.get("task_id") or "")
        if not tid:
            return {"ok": False, "error": "task_id required"}
        try:
            from brain.temporal_sense import resolve_estimate
            row = resolve_estimate(h, tid)
            return {"ok": True, "history_row": row}
        except Exception as e:
            return {"ok": False, "error": f"resolve_estimate failed: {e!r}"}

    @app.get("/api/v1/vision/latest_frame")
    def vision_latest_frame():
        h = _g()
        p = h.get("CAMERA_LATEST_ANNOTATED_PATH")
        if p is None:
            return PlainTextResponse("camera path unavailable", status_code=404)
        path = Path(p)
        if not path.is_file():
            return PlainTextResponse("no annotated frame yet", status_code=404)
        return FileResponse(str(path), media_type="image/jpeg")

    @app.get("/api/v1/chat/history")
    def chat_history(limit: int = 200) -> dict[str, Any]:
        """
        Returns the most recent chat turns. Prefers the persisted jsonl file
        (state/chat_history.jsonl) so the UI hydrates across restarts; falls
        back to in-memory canonical history if the file is empty/missing.
        """
        h = _g()
        try:
            limit = max(1, min(2000, int(limit)))
        except Exception:
            limit = 200
        try:
            import json as _json
            from pathlib import Path as _Path
            base = h.get("BASE_DIR")
            if base is not None:
                hist_path = _Path(base) / "state" / "chat_history.jsonl"
                if hist_path.is_file():
                    rows: list[dict[str, Any]] = []
                    with hist_path.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rows.append(_json.loads(line))
                            except Exception:
                                continue
                    if rows:
                        return {"ok": True, "messages": rows[-limit:], "source": "jsonl"}
        except Exception as e:
            print(f"[chat_history] jsonl read failed: {e}")

        hist_fn = h.get("_get_canonical_history") or h.get("get_canonical_history")
        if callable(hist_fn):
            try:
                rows = list(hist_fn())
                return {"ok": True, "messages": rows[-limit:], "source": "canonical"}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "no_history_fn", "messages": []}

    @app.post("/api/v1/workbench/approve")
    def workbench_approve(body: WorkbenchActionIn) -> dict[str, Any]:
        """
        Operator approve path using existing Phase 16.6 command handling.
        Defaults to approve/apply for current selected/top proposal.
        """
        h = _g()
        wb_result = _build_workbench_result_from_host(h)
        try:
            from brain.perception_types import WorkbenchCommandRequest
            from brain.workbench_commands import handle_workbench_command

            req = WorkbenchCommandRequest(
                command_name="approve_proposal_apply",
                proposal_id=str(body.proposal_id or ""),
                approved=True,
                elevated_approval=bool(body.elevated_approval),
                requested_by="operator_http",
                notes=[str(body.reason or "").strip()] if body.reason else [],
                meta={"source": "operator_http"},
            )
            res = handle_workbench_command(req, proposal_result=wb_result)
            asd = asdict(res)
            h["_last_workbench_command_result"] = asd
            if res.execution_result is not None:
                h["_last_workbench_execution_result"] = asdict(res.execution_result)
            return {
                "ok": bool(res.success),
                "message": str(res.summary or "workbench approve processed"),
                "result": asd,
            }
        except Exception as e:
            return {
                "ok": False,
                "message": f"workbench approve failed: {e}",
                "trace": traceback.format_exc()[:1400],
            }

    # Phase 70: Emil bridge endpoints
    class EmilSendIn(BaseModel):
        message: str = ""
        context: str = ""

    class EmilShareIn(BaseModel):
        topic: str = ""
        knowledge: str = ""

    @app.get("/api/v1/emil/status")
    def emil_status() -> dict[str, Any]:
        try:
            from brain.emil_bridge import get_emil_bridge
            return get_emil_bridge(_g().get("BASE_DIR") or Path.cwd()).get_status()
        except Exception as e:
            return {"online": False, "error": str(e)[:200]}

    @app.post("/api/v1/emil/ping")
    def emil_ping() -> dict[str, Any]:
        try:
            from brain.emil_bridge import get_emil_bridge
            return get_emil_bridge(_g().get("BASE_DIR") or Path.cwd()).ping_emil()
        except Exception as e:
            return {"online": False, "error": str(e)[:200]}

    @app.post("/api/v1/emil/send")
    def emil_send(body: EmilSendIn) -> dict[str, Any]:
        try:
            from brain.emil_bridge import get_emil_bridge
            return get_emil_bridge(_g().get("BASE_DIR") or Path.cwd()).send_to_emil(
                str(body.message or ""), str(body.context or "")
            )
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.post("/api/v1/emil/share")
    def emil_share(body: EmilShareIn) -> dict[str, Any]:
        try:
            from brain.emil_bridge import get_emil_bridge
            return get_emil_bridge(_g().get("BASE_DIR") or Path.cwd()).share_knowledge(
                str(body.topic or ""), str(body.knowledge or "")
            )
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # Phase 71: Plans endpoints
    class PlanCreateIn(BaseModel):
        goal: str = ""
        context: str = ""

    @app.get("/api/v1/plans")
    def plans_list() -> dict[str, Any]:
        try:
            from brain.planner import get_planner
            planner = get_planner(_g().get("BASE_DIR") or Path.cwd())
            all_plans = planner._load()
            return {
                "ok": True,
                "plans": all_plans[-50:],
                "active_count": sum(1 for p in all_plans if str(p.get("status") or "") == "active"),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "plans": []}

    @app.post("/api/v1/plans/create")
    def plans_create(body: PlanCreateIn) -> dict[str, Any]:
        goal = str(body.goal or "").strip()
        if not goal:
            return {"ok": False, "error": "goal required"}
        try:
            from brain.planner import get_planner
            plan = get_planner(_g().get("BASE_DIR") or Path.cwd()).create_plan(goal, str(body.context or ""))
            return {"ok": True, "plan": plan}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.post("/api/v1/plans/{plan_id}/pause")
    def plans_pause(plan_id: str) -> dict[str, Any]:
        try:
            from brain.planner import get_planner
            return get_planner(_g().get("BASE_DIR") or Path.cwd()).pause_plan(plan_id)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.post("/api/v1/plans/{plan_id}/resume")
    def plans_resume(plan_id: str) -> dict[str, Any]:
        try:
            from brain.planner import get_planner
            return get_planner(_g().get("BASE_DIR") or Path.cwd()).resume_plan(plan_id)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.get("/api/v1/plans/{plan_id}/progress")
    def plans_progress(plan_id: str) -> dict[str, Any]:
        try:
            from brain.planner import get_planner
            return get_planner(_g().get("BASE_DIR") or Path.cwd()).check_progress(plan_id)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.post("/api/v1/workbench/reject")
    def workbench_reject(body: WorkbenchActionIn) -> dict[str, Any]:
        """
        Operator reject path (explicit no-execution decision for current proposal).
        This records command intent in existing runtime command result slots.
        """
        h = _g()
        wb_result = _build_workbench_result_from_host(h)
        proposal_id = str(body.proposal_id or "")
        available: list[str] = []
        try:
            if wb_result is not None:
                available = [str(p.proposal_id or "") for p in list(wb_result.proposals or []) if str(p.proposal_id or "")]
        except Exception:
            available = []
        if not proposal_id and wb_result is not None and getattr(wb_result.top_proposal, "proposal_id", ""):
            proposal_id = str(wb_result.top_proposal.proposal_id or "")
        if proposal_id and available and proposal_id not in available:
            return {
                "ok": False,
                "message": f"proposal_id not found in current proposal set: {proposal_id}",
                "available_proposals": available[:20],
            }
        reason = str(body.reason or "").strip() or "rejected_by_operator"
        cmd_result = {
            "command_name": "reject_proposal",
            "proposal_id": proposal_id,
            "success": True,
            "summary": f"Rejected proposal `{proposal_id or '-none-'}` ({reason}).",
            "blocked_reason": "",
            "meta": {"source": "operator_http", "reason": reason},
        }
        h["_last_workbench_command_result"] = cmd_result
        return {
            "ok": True,
            "message": cmd_result["summary"],
            "result": cmd_result,
        }

    # ── Eye tracking / gaze calibration endpoints ────────────────────────────

    @app.post("/api/v1/camera/calibrate_gaze")
    async def calibrate_gaze() -> dict[str, Any]:
        h = _g()
        try:
            from brain.eye_tracker import get_eye_tracker
            et = get_eye_tracker()
            if et is None or not et.available:
                return {"ok": False, "error": "Eye tracker not available (mediapipe missing?)"}
            import threading as _ct
            result_box: list[bool] = [False]
            def _run():
                result_box[0] = et.calibrate()
            t = _ct.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=60.0)
            return {"ok": result_box[0], "calibrated": et.calibrated}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.get("/api/v1/camera/gaze")
    async def gaze_status() -> dict[str, Any]:
        h = _g()
        try:
            from brain.eye_tracker import get_eye_tracker
            from brain.expression_detector import get_expression_detector
            from brain.frame_store import read_live_frame_with_meta, LIVE_CACHE_MAX_AGE_SEC
            et = get_eye_tracker()
            ed = get_expression_detector()
            meta = read_live_frame_with_meta(max_age=LIVE_CACHE_MAX_AGE_SEC)
            frame = meta.frame
            result: dict[str, Any] = {
                "eye_tracker_available": et is not None and et.available,
                "expression_detector_available": ed is not None and ed.available,
                "gaze_calibrated": et.calibrated if et is not None else False,
                "gaze_region": "unknown",
                "attention_state": "unknown",
                "expression": "neutral",
            }
            if frame is not None:
                if et is not None and et.available:
                    result["gaze_region"] = et.get_gaze_region(frame)
                    result["attention_state"] = et.get_attention_state(frame)
                    result["looking_at_screen"] = et.is_looking_at_screen(frame)
                if ed is not None and ed.available:
                    scores = ed.detect_expression(frame)
                    result["expression"] = str(scores.get("dominant") or "neutral")
                    result["expression_scores"] = {k: v for k, v in scores.items() if k != "dominant"}
            return {"ok": True, **result}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.get("/api/v1/camera/live_frame")
    async def camera_live_frame() -> dict[str, Any]:
        """Return current camera frame as JPEG base64.
        Reads exclusively from the background thread's push_frame() buffer.
        Returns error if frame is missing or older than 10 seconds."""
        try:
            import cv2
            import base64 as _b64
            from brain.frame_store import get_buffered_frame
            meta = get_buffered_frame(max_age_sec=10.0)
            frame = meta.frame
            if frame is None:
                reason = "stale frame" if meta.age_sec > 0 else "no frame available"
                return {
                    "ok": False, "error": reason, "b64": None,
                    "age_sec": round(float(meta.age_sec), 3),
                    "freshness": str(meta.freshness),
                }
            ok_enc, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ok_enc:
                return {"ok": False, "error": "jpeg encode failed", "b64": None}
            b64 = _b64.b64encode(buf.tobytes()).decode("ascii")
            return {
                "ok": True, "b64": b64,
                "age_sec": round(float(meta.age_sec), 3),
                "freshness": str(meta.freshness),
                "capture_ts": round(float(meta.capture_ts), 3),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "b64": None}

    # ── Connectivity + Image endpoints ───────────────────────────────────────

    @app.get("/api/v1/connectivity")
    async def connectivity_status() -> dict[str, Any]:
        h = _g()
        try:
            from brain.connectivity import get_monitor
            mon = get_monitor(h)
            online = mon.is_online()
            quality = mon.get_connection_quality()
            cloud = mon.check_ollama_cloud() if online else False
        except Exception:
            online = bool(h.get("_is_online", False))
            quality = str(h.get("_connection_quality") or "offline")
            cloud = bool(h.get("_ollama_cloud_reachable", False))
        from config.ava_tuning import DEFAULT_MODEL_CAPABILITY_PROFILES
        cloud_models = [p.model_name for p in DEFAULT_MODEL_CAPABILITY_PROFILES if getattr(p, "requires_internet", False)]
        return {
            "ok": True, "online": online, "quality": quality,
            "cloud_reachable": cloud, "cloud_models": cloud_models,
            "changed_recently": bool(h.get("_connectivity_changed")),
        }

    @app.get("/api/v1/images/latest")
    async def images_latest() -> dict[str, Any]:
        h = _g()
        path = str(h.get("_latest_image") or "")
        if not path:
            return {"ok": False, "error": "no image generated yet"}
        from pathlib import Path as _P
        p = _P(path)
        if not p.is_file():
            return {"ok": False, "error": "image file not found"}
        import base64
        data = p.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return {"ok": True, "path": path, "filename": p.name, "b64": b64, "caption": str(h.get("_latest_image_caption") or "")}

    @app.get("/api/v1/images/list")
    async def images_list() -> dict[str, Any]:
        h = _g()
        from pathlib import Path as _P
        img_dir = _P(h.get("BASE_DIR") or ".") / "state" / "generated_images"
        files = []
        if img_dir.is_dir():
            for f in sorted(img_dir.glob("*.png"), reverse=True)[:50]:
                files.append({"filename": f.name, "path": str(f), "size_kb": round(f.stat().st_size / 1024, 1)})
        return {"ok": True, "images": files}

    @app.post("/api/v1/images/generate")
    async def images_generate(request: Request) -> dict[str, Any]:
        h = _g()
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt required"}
        try:
            from tools.creative.image_generator import ImageGenerator
            gen = h.get("_image_generator") or ImageGenerator(h)
            path = gen.generate(prompt, style=str(body.get("style") or ""))
            return {"ok": path is not None, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.delete("/api/v1/images/{filename}")
    async def images_delete(filename: str) -> dict[str, Any]:
        h = _g()
        from pathlib import Path as _P
        img_dir = _P(h.get("BASE_DIR") or ".") / "state" / "generated_images"
        p = img_dir / filename
        if not p.is_file() or p.suffix not in (".png", ".jpg", ".jpeg"):
            return {"ok": False, "error": "file not found or not an image"}
        p.unlink()
        return {"ok": True, "deleted": filename}

    # ── Phase 95: Privacy / Security endpoints ───────────────────────────────

    @app.post("/api/v1/security/audit")
    async def security_audit() -> dict[str, Any]:
        try:
            from brain.privacy_guardian import data_audit
            return {"ok": True, "audit": data_audit(_g())}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @app.get("/api/v1/security/blocked")
    async def security_blocked() -> dict[str, Any]:
        h = _g()
        import json as _jbl
        from brain.privacy_guardian import _log_path
        path = _log_path(h)
        entries = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines()[-50:]:
                try:
                    entries.append(_jbl.loads(line))
                except Exception:
                    pass
        return {"ok": True, "entries": entries}

    # ── Phase 93-94: Learning and Profiles endpoints ─────────────────────────

    @app.get("/api/v1/learning/log")
    async def learning_log() -> dict[str, Any]:
        h = _g()
        try:
            from brain.learning_tracker import _log_path
            import json as _json
            path = _log_path(h)
            if not path.is_file():
                return {"ok": True, "entries": []}
            entries = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    entries.append(_json.loads(line))
                except Exception:
                    pass
            return {"ok": True, "entries": entries[-50:]}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "entries": []}

    @app.get("/api/v1/learning/gaps")
    async def learning_gaps() -> dict[str, Any]:
        try:
            from brain.learning_tracker import knowledge_gaps
            return {"ok": True, "gaps": knowledge_gaps(_g())}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "gaps": []}

    @app.get("/api/v1/learning/week")
    async def learning_week() -> dict[str, Any]:
        try:
            from brain.learning_tracker import what_have_i_learned_this_week
            return {"ok": True, "summary": what_have_i_learned_this_week(_g())}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "summary": ""}

    @app.get("/api/v1/profiles/list")
    async def profiles_list() -> dict[str, Any]:
        h = _g()
        import json as _json
        base = Path(h.get("BASE_DIR") or ".")
        profiles_dir = base / "profiles"
        profiles = []
        if profiles_dir.is_dir():
            for pf in sorted(profiles_dir.glob("*.json")):
                if "_relationship" in pf.stem:
                    continue
                try:
                    data = _json.loads(pf.read_text(encoding="utf-8"))
                    profiles.append(data)
                except Exception:
                    pass
        return {"ok": True, "profiles": profiles}

    # ── Phase 86: Journal endpoints ──────────────────────────────────────────

    @app.get("/api/v1/journal/entries")
    async def journal_entries() -> dict[str, Any]:
        h = _g()
        try:
            from brain.journal import get_shared_entries, get_entry_count, _load_entries
            all_entries = _load_entries(h)
            total, shared_count = get_entry_count(h)
            return {"ok": True, "entries": all_entries[-50:], "total": total, "shared_count": shared_count}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "entries": [], "total": 0, "shared_count": 0}

    @app.post("/api/v1/journal/share/{entry_id}")
    async def journal_share(entry_id: str) -> dict[str, Any]:
        h = _g()
        try:
            from brain.journal import share_entry
            entry = share_entry(entry_id, h)
            return {"ok": entry is not None, "entry": entry}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ── Phase 79: Onboarding endpoints ───────────────────────────────────────

    @app.post("/api/v1/onboarding/start")
    async def onboarding_start(request: Request) -> dict[str, Any]:
        h = _g()
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        from brain.person_onboarding import start_onboarding, get_onboarding_status
        import uuid as _uuid
        person_id = str(body.get("person_id") or f"person_{_uuid.uuid4().hex[:8]}")
        name_hint = str(body.get("name") or "").strip() or None
        base = Path(h.get("BASE_DIR") or ".")
        flow = start_onboarding(person_id, base, name_hint=name_hint)
        h["_onboarding_flow"] = flow
        h["_onboarding_stage"] = flow.stage
        # Run greeting step automatically
        reply, stage, done = flow.run_step("", h)
        return {"ok": True, "reply": reply, "stage": stage, "done": done, "status": get_onboarding_status(h)}

    @app.get("/api/v1/onboarding/status")
    async def onboarding_status() -> dict[str, Any]:
        from brain.person_onboarding import get_onboarding_status
        return get_onboarding_status(_g())

    @app.post("/api/v1/onboarding/step")
    async def onboarding_step(request: Request) -> dict[str, Any]:
        h = _g()
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        from brain.person_onboarding import run_onboarding_step, get_onboarding_status
        user_input = str(body.get("input") or "").strip()
        result = run_onboarding_step(user_input, h)
        if result is None:
            return {"ok": False, "message": "No active onboarding flow", "status": get_onboarding_status(h)}
        reply, stage, done = result
        return {"ok": True, "reply": reply, "stage": stage, "done": done, "status": get_onboarding_status(h)}

    @app.post("/api/v1/profile/{person_id}/refresh")
    async def profile_refresh(person_id: str) -> dict[str, Any]:
        from brain.person_onboarding import refresh_profile
        return refresh_profile(person_id, _g())

    return app


def start_operator_http_background(host: dict[str, Any], chat_fn: Callable[..., dict[str, Any]]) -> None:
    configure_operator_runtime(host, chat_fn)
    if os.environ.get("AVA_OPERATOR_HTTP", "1").strip() in ("0", "false", "False"):
        print("[operator_http] disabled (AVA_OPERATOR_HTTP=0)")
        return
    port = int(os.environ.get("AVA_OPERATOR_HTTP_PORT", "5876") or "5876")
    host_bind = os.environ.get("AVA_OPERATOR_HTTP_HOST", "127.0.0.1") or "127.0.0.1"

    app = create_app()

    def _run() -> None:
        try:
            import uvicorn

            uvicorn.run(app, host=host_bind, port=port, log_level="warning")
        except Exception as e:
            print(f"[operator_http] server failed: {e}\n{traceback.format_exc()}")

    t = threading.Thread(target=_run, name="ava-operator-http", daemon=True)
    t.start()
    host["_operator_http_thread"] = t
    print(f"[operator_http] listening on http://{host_bind}:{port}")
