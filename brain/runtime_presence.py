"""
Phase 32 — runtime presence: bounded operator visibility and proactive timing hints.

Aggregates heartbeat, adaptive learning, strategic continuity, and maintenance signals into
one lightweight surface. Does not write ava_core identity files or approve workbench actions.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any, Optional

from .adaptive_learning import PREFERENCES_PATH
from .heartbeat import HEARTBEAT_STATE_PATH, load_heartbeat_state
from .perception_types import (
    AdaptiveLearningResult,
    HeartbeatTickResult,
    ImprovementLoopResult,
    OperatorStatusSnapshot,
    RuntimePresenceResult,
    SelfTestRunResult,
    SocialContinuityResult,
    StartupResumeSummary,
    StrategicContinuityResult,
    WorkbenchProposalResult,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SESSION_STATE_PATH = _REPO_ROOT / "state" / "session_state.json"

# Warm self-snapshot TTL hint for consumers (seconds); snapshot refreshes each perception tick.
_RUNTIME_SELF_SNAPSHOT_MAX_STALE_SEC = 90.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _trunc(s: str, n: int = 240) -> str:
    t = " ".join((s or "").split())
    if len(t) <= n:
        return t
    return t[: n - 1].rstrip() + "…"


def _heartbeat_focus_line(hb: Optional[HeartbeatTickResult]) -> str:
    if hb is None:
        return ""
    parts = [
        str(getattr(hb, "heartbeat_mode", "") or "")[:48],
        str(getattr(hb, "tick_reason", "") or "")[:120],
        str(getattr(hb, "suggested_action", "") or "")[:120],
    ]
    parts = [p for p in parts if p.strip()]
    if getattr(hb, "heartbeat_summary", "").strip():
        parts.append(_trunc(str(hb.heartbeat_summary), 160))
    return _trunc(" | ".join(parts), 260)


def _current_focus_composite(
    hb: Optional[HeartbeatTickResult],
    al: Optional[AdaptiveLearningResult],
) -> str:
    hb_line = _heartbeat_focus_line(hb)
    lf = ""
    if al is not None:
        lf = _trunc(
            str(getattr(al, "learning_focus", "") or getattr(al, "learning_summary", "") or ""),
            200,
        )
    if hb_line and lf:
        return _trunc(f"heartbeat: {hb_line}; learning: {lf}", 320)
    return hb_line or lf or "steady_observation"


def _store_runtime_self_snapshot(
    g: dict[str, Any],
    *,
    presence_mode: str,
    ready_state: str,
    active_issue: str,
    threads_sum: str,
    maintenance_status_summary: str,
    operator_status_summary: str,
    learn_sum: str,
    hb_sum: str,
    heartbeat: Optional[HeartbeatTickResult],
    adaptive_learning: Optional[AdaptiveLearningResult],
    strategic_continuity: Optional[StrategicContinuityResult],
    improvement_loop: Optional[ImprovementLoopResult],
    workbench: Optional[WorkbenchProposalResult],
) -> None:
    """Bounded warm state for hosts (no full pipeline replay). Refreshed each presence build."""
    if not isinstance(g, dict):
        return
    now = time.time()
    thread_hint = ""
    if strategic_continuity is not None and strategic_continuity.active_threads:
        t0 = strategic_continuity.active_threads[0]
        thread_hint = _trunc(str(getattr(t0, "summary", "") or getattr(t0, "category", "") or ""), 200)
    if not thread_hint:
        thread_hint = str(threads_sum or "")[:200]

    snap = {
        "version": 1,
        "ts": now,
        "max_stale_hint_sec": _RUNTIME_SELF_SNAPSHOT_MAX_STALE_SEC,
        "heartbeat_mode": str(getattr(heartbeat, "heartbeat_mode", "") or presence_mode)[:48],
        "current_focus": _current_focus_composite(heartbeat, adaptive_learning),
        "active_issue": str(active_issue or "")[:400],
        "active_thread": thread_hint,
        "runtime_readiness": str(ready_state or "steady")[:32],
        "learning_focus": str(getattr(adaptive_learning, "learning_focus", "") or "")[:220],
        "maintenance_state": str(maintenance_status_summary or "")[:400],
        "operator_summary": str(operator_status_summary or "")[:500],
        "presence_mode": str(presence_mode or "")[:48],
        "learning_status_summary": str(learn_sum or "")[:260],
        "heartbeat_status_summary": str(hb_sum or "")[:260],
        "workbench_signal": "",
        "improvement_stage": str(getattr(improvement_loop, "loop_stage", "") or "")[:80]
        if improvement_loop is not None
        else "",
    }
    if workbench is not None and getattr(workbench, "has_proposal", False):
        snap["workbench_signal"] = _trunc(str(getattr(workbench.top_proposal, "proposal_type", "") or ""), 120)
    g["_runtime_self_snapshot"] = snap
    g["_runtime_self_snapshot_ts"] = now


def bootstrap_startup_resume(g: dict[str, Any]) -> None:
    """Quiet startup: confirm carryover paths; sets g snapshot for pipeline (no chatter)."""
    if not isinstance(g, dict):
        return
    try:
        hb = load_heartbeat_state()
        adapt_ok = PREFERENCES_PATH.is_file()
        sess_ok = _SESSION_STATE_PATH.is_file()
        hb_ok = HEARTBEAT_STATE_PATH.is_file()
        sr = StartupResumeSummary(
            heartbeat_carryover_loaded=bool(hb_ok),
            adaptive_preferences_loaded=adapt_ok,
            session_state_present=sess_ok,
            quiet_monitoring_default=True,
            meta={"identity_anchors_read_only": True, "no_ava_core_write": True},
        )
        g["_startup_resume_snapshot"] = sr
        if not g.get("_startup_resume_logged"):
            loaded = any((hb_ok, adapt_ok, sess_ok))
            print(
                f"[startup_resume] continuity=heartbeat:{int(hb_ok)} "
                f"adaptive:{int(adapt_ok)} session:{int(sess_ok)} "
                f"loaded={int(loaded)} quiet_monitoring=1"
            )
            g["_startup_resume_logged"] = True
    except Exception as e:
        print(f"[startup_resume] bootstrap skipped: {e}")


def _proactive_silence_bias(
    hb: Optional[HeartbeatTickResult], al: Optional[AdaptiveLearningResult]
) -> float:
    b = 0.18
    if hb is not None:
        if bool(getattr(hb, "should_remain_silent", True)):
            b = max(b, 0.68)
        mode = str(getattr(hb, "heartbeat_mode", "") or "")
        if mode in ("quiet_recovery", "idle_monitoring", "learning_review"):
            b = max(b, 0.52)
        if bool(getattr(hb, "important_state_change", False)):
            b = min(b, 0.45)
    if al is not None:
        w = (al.meta or {}).get("weights_preview") or {}
        sil = float(w.get("silence_when_better_response", 0.5) or 0.5)
        if sil >= 0.62:
            b = max(b, 0.42)
        lf = str(al.learning_focus or "")
        if "silence" in lf:
            b = max(b, 0.38)
    return _clamp01(b)


def build_runtime_presence_safe(
    *,
    g: dict[str, Any],
    heartbeat: Optional[HeartbeatTickResult],
    adaptive_learning: Optional[AdaptiveLearningResult],
    strategic_continuity: Optional[StrategicContinuityResult],
    improvement_loop: Optional[ImprovementLoopResult],
    workbench: Optional[WorkbenchProposalResult],
    selftests: Optional[SelfTestRunResult],
    social_continuity: Optional[SocialContinuityResult],
) -> RuntimePresenceResult:
    try:
        return _build_runtime_presence(
            g=g if isinstance(g, dict) else {},
            heartbeat=heartbeat,
            adaptive_learning=adaptive_learning,
            strategic_continuity=strategic_continuity,
            improvement_loop=improvement_loop,
            workbench=workbench,
            selftests=selftests,
            social_continuity=social_continuity,
        )
    except Exception as e:
        print(f"[runtime_presence] failed: {e}\n{traceback.format_exc()}")
        return RuntimePresenceResult(
            notes=[_trunc(str(e), 160)],
            meta={"error": True},
        )


def _build_runtime_presence(
    *,
    g: dict[str, Any],
    heartbeat: Optional[HeartbeatTickResult],
    adaptive_learning: Optional[AdaptiveLearningResult],
    strategic_continuity: Optional[StrategicContinuityResult],
    improvement_loop: Optional[ImprovementLoopResult],
    workbench: Optional[WorkbenchProposalResult],
    selftests: Optional[SelfTestRunResult],
    social_continuity: Optional[SocialContinuityResult],
) -> RuntimePresenceResult:
    sr_in = g.get("_startup_resume_snapshot")
    startup_loaded = isinstance(sr_in, StartupResumeSummary)
    continuity_loaded = strategic_continuity is not None and bool(
        str(getattr(strategic_continuity.session_carryover, "headline", "") or "").strip()
        or list(strategic_continuity.active_threads or [])
    )

    presence_mode = "quiet_monitoring"
    ilp = improvement_loop
    wb = workbench
    sc = strategic_continuity
    soc = social_continuity

    if ilp is not None and bool(getattr(ilp, "loop_active", False)):
        presence_mode = "maintenance_focus"
    elif wb is not None and bool(getattr(wb, "has_proposal", False)):
        presence_mode = "maintenance_focus"
    elif soc is not None and bool(getattr(soc, "unfinished_thread_present", False)):
        presence_mode = "attention_needed"
    elif heartbeat is not None and str(getattr(heartbeat, "heartbeat_mode", "") or "") == "conversation_active":
        presence_mode = "conversation_adjacent"

    active_issue = ""
    if ilp is not None:
        active_issue = _trunc(str(getattr(ilp, "active_issue", "") or getattr(ilp, "loop_summary", "") or ""), 200)

    threads_sum = ""
    if sc is not None:
        threads_sum = _trunc(str(sc.session_carryover.headline or ""), 200)
        if not threads_sum and sc.active_threads:
            t0 = sc.active_threads[0]
            threads_sum = _trunc(str(getattr(t0, "summary", "") or getattr(t0, "category", "") or ""), 200)

    stsum = selftests.summary if selftests is not None else None
    st_overall = str(stsum.overall_status or "ok") if stsum else "ok"

    maint_parts: list[str] = []
    if ilp is not None:
        maint_parts.append(f"loop={str(ilp.loop_stage or '')[:48]}")
        if bool(getattr(ilp, "awaiting_approval", False)):
            maint_parts.append("awaiting_approval")
        if bool(getattr(ilp, "awaiting_execution", False)):
            maint_parts.append("awaiting_execution")
    if wb is not None and wb.has_proposal:
        maint_parts.append(f"proposal={wb.top_proposal.proposal_type[:40]}")
    if st_overall != "ok":
        maint_parts.append(f"selftests={st_overall}")
    maintenance_status_summary = _trunc("; ".join(maint_parts), 260) if maint_parts else "none_pending"

    learn_sum = ""
    if adaptive_learning is not None:
        learn_sum = _trunc(
            f"focus={adaptive_learning.learning_focus or '—'} "
            f"upd={int(adaptive_learning.learning_update_applied)} "
            f"conf={adaptive_learning.learning_confidence:.2f}",
            220,
        )

    hb_sum = ""
    if heartbeat is not None:
        hb_sum = _trunc(
            f"{heartbeat.heartbeat_mode} tick={heartbeat.heartbeat_tick_id} "
            f"silent={int(bool(heartbeat.should_remain_silent))}",
            200,
        )

    ready_state = "steady"
    if presence_mode == "maintenance_focus":
        ready_state = "observe"
    elif presence_mode == "attention_needed":
        ready_state = "attention"

    op_hb = hb_sum or "heartbeat=idle"
    op_learn = learn_sum or "learning=steady"
    op_maint = maintenance_status_summary[:180]
    op_thr = threads_sum or "threads=quiet"
    op_wb = "workbench=clear"
    if wb is not None and wb.has_proposal:
        op_wb = _trunc(f"workbench={wb.top_proposal.proposal_type}", 120)

    rollup = _trunc(f"{presence_mode}; {ready_state}; {op_maint[:80]}", 280)

    op_view = OperatorStatusSnapshot(
        heartbeat_line=op_hb,
        learning_line=op_learn,
        maintenance_line=op_maint,
        threads_line=op_thr,
        workbench_line=op_wb,
        rollup=rollup,
    )

    operator_status_summary = rollup

    _store_runtime_self_snapshot(
        g,
        presence_mode=presence_mode,
        ready_state=ready_state,
        active_issue=active_issue,
        threads_sum=threads_sum,
        maintenance_status_summary=maintenance_status_summary,
        operator_status_summary=operator_status_summary,
        learn_sum=learn_sum,
        hb_sum=hb_sum,
        heartbeat=heartbeat,
        adaptive_learning=adaptive_learning,
        strategic_continuity=sc,
        improvement_loop=ilp,
        workbench=wb,
    )

    pb = _proactive_silence_bias(heartbeat, adaptive_learning)

    meta = {
        "proactive_silence_bias": pb,
        "presence_mode": presence_mode,
        "identity_anchors_respected": True,
        "runtime_self_snapshot_ts": g.get("_runtime_self_snapshot_ts"),
    }

    sig = f"{presence_mode}|{maint_parts and 'm' or ''}|{threads_sum[:40]}|{active_issue[:40]}"
    last = g.get("_runtime_presence_last_sig")
    quiet_chatter = (
        presence_mode == "quiet_monitoring"
        and not (active_issue or "").strip()
        and maintenance_status_summary == "none_pending"
        and not (threads_sum or "").strip()
    )
    issue_log = _trunc(active_issue, 88) if active_issue else "—"
    if not quiet_chatter and last != sig:
        print(f"[runtime_presence] mode={presence_mode} ready={ready_state} issue={issue_log}")
    g["_runtime_presence_last_sig"] = sig

    return RuntimePresenceResult(
        presence_mode=presence_mode,
        startup_loaded=bool(startup_loaded),
        continuity_loaded=bool(continuity_loaded),
        active_issue_summary=active_issue,
        active_threads_summary=threads_sum,
        maintenance_status_summary=maintenance_status_summary,
        learning_status_summary=learn_sum,
        heartbeat_status_summary=hb_sum,
        operator_status_summary=operator_status_summary,
        ready_state=ready_state,
        operator_view=op_view,
        startup_resume=sr_in if isinstance(sr_in, StartupResumeSummary) else None,
        notes=[],
        meta=meta,
    )


def apply_runtime_presence_to_perception_state(state: Any, bundle: Any) -> None:
    """Map :class:`RuntimePresenceResult` onto :class:`~brain.perception.PerceptionState` (Phase 32)."""
    rp = getattr(bundle, "runtime_presence", None)
    if rp is None:
        state.runtime_presence_mode = "unknown"
        state.runtime_ready_state = "steady"
        state.runtime_active_issue_summary = ""
        state.runtime_threads_summary = ""
        state.runtime_maintenance_summary = ""
        state.runtime_learning_summary = ""
        state.runtime_operator_summary = ""
        state.runtime_presence_meta = {"phase": 32, "idle": True}
        return

    state.runtime_presence_mode = str(rp.presence_mode or "quiet_monitoring")[:48]
    state.runtime_ready_state = str(rp.ready_state or "steady")[:32]
    state.runtime_active_issue_summary = str(rp.active_issue_summary or "")[:400]
    state.runtime_threads_summary = str(rp.active_threads_summary or "")[:400]
    state.runtime_maintenance_summary = str(rp.maintenance_status_summary or "")[:400]
    state.runtime_learning_summary = str(rp.learning_status_summary or "")[:400]
    state.runtime_operator_summary = str(rp.operator_status_summary or "")[:500]
    m = dict(rp.meta or {})
    ov = getattr(rp, "operator_view", None)
    if ov is not None:
        m["operator_view"] = {
            "heartbeat_line": ov.heartbeat_line[:220],
            "learning_line": ov.learning_line[:220],
            "maintenance_line": ov.maintenance_line[:260],
            "threads_line": ov.threads_line[:220],
            "workbench_line": ov.workbench_line[:160],
            "rollup": ov.rollup[:320],
        }
    m["heartbeat_status_summary"] = str(rp.heartbeat_status_summary or "")[:260]
    state.runtime_presence_meta = m


# ── Phase 82: Multi-person awareness ──────────────────────────────────────────

_PERSON_CHANGE_COOLDOWN = 10.0  # seconds before acknowledging a face change


def tick_multi_person_awareness(g: dict[str, Any]) -> dict[str, Any]:
    """
    Called from heartbeat. Checks current recognized face and updates who is at the machine.
    Returns current_person block for snapshot.

    Prefers the InsightFace cache (`_recognized_person_id` / `_recognized_confidence`
    populated by background_ticks ~5 times/sec). Falls back to face_recognition
    if InsightFace isn't running.
    """
    try:
        from pathlib import Path as _Path
        from brain.frame_store import read_live_frame_with_meta

        base_dir = _Path(g.get("BASE_DIR") or ".")

        # Fast path: InsightFace already analysed a recent frame.
        insight = g.get("_insight_face")
        if insight is not None and getattr(insight, "available", False):
            person_id = str(g.get("_recognized_person_id") or "unknown")
            confidence = float(g.get("_recognized_confidence") or 0.0)
        else:
            from brain.face_recognizer import get_recognizer
            rec = get_recognizer(base_dir)
            if not rec.available:
                return _empty_person_block(g)
            meta = read_live_frame_with_meta()
            frame = meta.frame
            if frame is None:
                return _empty_person_block(g)
            person_id, confidence = rec.get_best_match(frame)
        g["_face_recognizer_last_person_id"] = person_id
        g["_face_recognizer_last_confidence"] = confidence

        prev_person = g.get("_current_person_at_machine") or "unknown"
        now = time.time()

        if person_id != prev_person and confidence > 0.5:
            last_change = float(g.get("_last_face_change_ts") or 0)
            if (now - last_change) > _PERSON_CHANGE_COOLDOWN:
                g["_last_face_change_ts"] = now
                g["_current_person_at_machine"] = person_id
                g["_person_appeared_at"] = now
                g["_current_person_is_zeke"] = (person_id == str(g.get("OWNER_PERSON_ID") or "zeke"))
                if prev_person and prev_person != "unknown":
                    g["_person_transition_note"] = f"Person changed: {prev_person} → {person_id}"
                else:
                    g["_person_transition_note"] = f"Person appeared: {person_id}"
                print(f"[multi_person] face change {prev_person} → {person_id} conf={confidence:.2f}")
                # Wire face-detection greeting (best-effort, never fail the tick).
                try:
                    from brain.proactive_triggers import maybe_greet_on_face_detection
                    maybe_greet_on_face_detection(g, person_id, prev_person)
                except Exception as _ge:
                    print(f"[multi_person] greet hook error: {_ge}")

        appeared_at = float(g.get("_person_appeared_at") or now)
        time_at_machine = now - appeared_at

        return _build_person_block(g, person_id, confidence, time_at_machine, base_dir)

    except Exception as e:
        print(f"[multi_person] tick error: {e}")
        return _empty_person_block(g)


def _empty_person_block(g: dict[str, Any]) -> dict[str, Any]:
    return {
        "person_id": g.get("_current_person_at_machine") or "unknown",
        "display_name": "Unknown",
        "confidence": 0.0,
        "time_at_machine": 0.0,
        "is_zeke": False,
    }


def _build_person_block(
    g: dict[str, Any], person_id: str, confidence: float,
    time_at_machine: float, base_dir: "Path",
) -> dict[str, Any]:
    import json as _json
    display_name = person_id
    try:
        p_path = base_dir / "profiles" / f"{person_id}.json"
        if p_path.is_file():
            data = _json.loads(p_path.read_text(encoding="utf-8"))
            display_name = str(data.get("name") or person_id)
    except Exception:
        pass

    owner = str(g.get("OWNER_PERSON_ID") or "zeke")
    return {
        "person_id": person_id,
        "display_name": display_name,
        "confidence": round(confidence, 3),
        "time_at_machine": round(time_at_machine, 1),
        "is_zeke": person_id == owner,
        "transition_note": str(g.get("_person_transition_note") or ""),
    }


def get_current_person_block(g: dict[str, Any]) -> dict[str, Any]:
    """Returns the most recent person block without triggering a new frame capture."""
    base_dir = Path(g.get("BASE_DIR") or ".")
    person_id = str(g.get("_current_person_at_machine") or "unknown")
    confidence = float(g.get("_face_recognizer_last_confidence") or 0.0)
    appeared_at = float(g.get("_person_appeared_at") or time.time())
    time_at_machine = time.time() - appeared_at
    return _build_person_block(g, person_id, confidence, time_at_machine, base_dir)
