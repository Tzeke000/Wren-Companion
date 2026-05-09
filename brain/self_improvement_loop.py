"""
Phase 30 — Supervised self-improvement loop (structured awareness only).

Connects self-tests, workbench proposals, execution/rollback hooks in ``g``, reflection,
contemplation, outcome learning, and strategic continuity into one reviewable snapshot.

Does **not** auto-approve, auto-execute, bypass allowlists, or override ``ava_core`` identity
anchors (Phase 29); maintenance framing remains subordinate to IDENTITY/SOUL/USER.
"""
from __future__ import annotations

import traceback
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

from .perception_types import (
    ContemplationResult,
    ImprovementCycle,
    ImprovementLoopResult,
    ImprovementStepStatus,
    OutcomeLearningResult,
    ReflectionResult,
    SelfTestRunResult,
    StrategicContinuityResult,
    WorkbenchProposalResult,
)

_SIL_ISSUE = "_sil_carry_issue"
_SIL_STAGE = "_sil_carry_stage"
_SIL_PROP = "_sil_carry_proposal_id"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _trunc(s: str, n: int = 280) -> str:
    t = " ".join((s or "").split())
    if len(t) <= n:
        return t
    return t[: n - 1].rstrip() + "…"


def _to_map(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if is_dataclass(obj):
        try:
            return asdict(obj)
        except Exception:
            pass
    out: dict[str, Any] = {}
    for k in (
        "success",
        "blocked",
        "approved",
        "proposal_id",
        "rollback_available",
        "error_message",
        "denial_reason",
        "execution_mode",
        "command_name",
        "execution_result",
        "summary",
        "success",
    ):
        if hasattr(obj, k):
            try:
                out[k] = getattr(obj, k)
            except Exception:
                pass
    return out


def _nested_exec(cmd_map: dict[str, Any]) -> dict[str, Any]:
    er = cmd_map.get("execution_result")
    return _to_map(er) if er else {}


def build_supervised_self_improvement_loop_safe(
    *,
    g: dict[str, Any] | None,
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    outcome_learning: Optional[OutcomeLearningResult],
    strategic_continuity: Optional[StrategicContinuityResult],
) -> ImprovementLoopResult:
    try:
        return _build_improvement_loop(
            g=g if isinstance(g, dict) else {},
            selftests=selftests,
            workbench=workbench,
            reflection=reflection,
            contemplation=contemplation,
            outcome_learning=outcome_learning,
            strategic_continuity=strategic_continuity,
        )
    except Exception as e:
        print(f"[self_improvement_loop] safe_fallback err={e!r}\n{traceback.format_exc()}")
        return _idle_result(str(e)[:120])


def _idle_result(err: str = "") -> ImprovementLoopResult:
    r = ImprovementLoopResult(
        loop_summary="Supervised improvement loop idle (safe fallback).",
        notes=["Phase 30 fallback — no autonomous action."],
        meta={"phase": 30, "error": err} if err else {"phase": 30},
    )
    return r


def _maintenance_snippets(sc: Optional[StrategicContinuityResult]) -> tuple[str, list[str]]:
    if sc is None:
        return "", []
    lines: list[str] = []
    mcarry = _trunc(str(sc.maintenance_carryover or ""), 400)
    if mcarry:
        lines.append(mcarry)
    for t in sc.active_threads or []:
        if getattr(t, "category", "") == "maintenance_or_repair_thread":
            lines.append(_trunc(str(getattr(t, "summary", "") or ""), 240))
    return (" | ".join(lines))[:520], lines


def _build_improvement_loop(
    *,
    g: dict[str, Any],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    reflection: Optional[ReflectionResult],
    contemplation: Optional[ContemplationResult],
    outcome_learning: Optional[OutcomeLearningResult],
    strategic_continuity: Optional[StrategicContinuityResult],
) -> ImprovementLoopResult:
    notes: list[str] = [
        "Phase 30 — supervised snapshot only; identity anchors (ava_core) outrank maintenance framing.",
    ]

    raw_ex = g.get("_last_workbench_execution_result")
    raw_cmd = g.get("_last_workbench_command_result")
    ex = _to_map(raw_ex)
    cmd = _to_map(raw_cmd)
    cmd_ex = _nested_exec(cmd)

    # Prefer explicit execution payload from command wrapper when present
    eff: dict[str, Any] = {**ex}
    if cmd_ex:
        for k, v in cmd_ex.items():
            eff.setdefault(k, v)

    st = selftests.summary if selftests is not None else None
    failed: list[str] = list(getattr(st, "failed_checks", []) or []) if st else []
    warnings: list[str] = list(getattr(st, "warning_checks", []) or []) if st else []
    overall = str(getattr(st, "overall_status", "") or "ok") if st else "ok"
    has_fail = len(failed) > 0

    wb = workbench
    tp = getattr(wb, "top_proposal", None) if wb is not None else None
    has_prop = bool(wb and getattr(wb, "has_proposal", False))
    ptype = str(getattr(tp, "proposal_type", "") or "") if tp else ""
    if ptype in ("", "no_action_needed"):
        has_prop = False
    pid = str(getattr(tp, "proposal_id", "") or "") if tp else ""
    ptitle = str(getattr(tp, "title", "") or "") if tp else ""
    requires_review = bool(getattr(tp, "requires_human_review", True)) if tp else True

    maint_join, maint_parts = _maintenance_snippets(strategic_continuity)

    issue_bits: list[str] = []
    if failed:
        issue_bits.append("Self-test failures: " + ", ".join(failed[:6]))
    if warnings and has_fail:
        issue_bits.append("Warnings: " + ", ".join(warnings[:4]))
    if maint_parts and not issue_bits:
        issue_bits.append("Maintenance continuity: " + maint_parts[0][:200])

    active_issue = _trunc("; ".join(issue_bits), 520)
    if not active_issue:
        active_issue = _trunc(str(g.get(_SIL_ISSUE) or ""), 520)

    prev_stage = str(g.get(_SIL_STAGE) or ImprovementStepStatus.NO_ACTIVE_LOOP)
    prev_prop = str(g.get(_SIL_PROP) or "")

    exec_success = bool(eff.get("success"))
    exec_blocked = bool(eff.get("blocked"))
    rollback_avail = bool(eff.get("rollback_available"))
    cmd_name = str(cmd.get("command_name") or "").lower()
    cmd_success = bool(cmd.get("success"))
    rollback_used = (
        ("rollback" in cmd_name and cmd_success)
        or bool(g.get("_sil_rollback_used_tick"))
        or bool((eff.get("meta") or {}).get("rollback_applied"))
    )

    awaiting_exec_flag = bool(g.get("_workbench_execution_in_progress"))

    rf = reflection or None
    ct = contemplation or None
    ol = outcome_learning or None

    post_reflect = False
    if rf and float(getattr(rf, "confidence", 0) or 0) >= 0.38:
        rs = str(getattr(rf, "reflection_summary", "") or "").lower()
        if any(x in rs for x in ("workbench", "repair", "rollback", "execution", "self-test")):
            post_reflect = True
    if ol and str(getattr(ol, "adjustment_target", "") or "") == "workbench_execution":
        post_reflect = True

    stage = ImprovementStepStatus.NO_ACTIVE_LOOP
    loop_active = False
    awaiting_approval = False
    awaiting_execution = awaiting_exec_flag
    exec_succ_flag = False
    exec_fail_flag = False
    rollback_used_flag = bool(rollback_used)
    suggested = "No supervised maintenance action indicated."
    cycles: list[ImprovementCycle] = []

    # --- Highest priority: explicit execution / rollback signals (this tick context in g) ---
    if raw_ex is not None or raw_cmd is not None:
        if rollback_used_flag:
            stage = ImprovementStepStatus.ROLLBACK_USED
            loop_active = True
            suggested = "Rollback ran; review diagnostics and self-tests before retrying execution."
            exec_succ_flag = False
            exec_fail_flag = False
        elif exec_success:
            exec_succ_flag = True
            stage = (
                ImprovementStepStatus.POST_EXECUTION_REFLECTION
                if post_reflect
                else ImprovementStepStatus.EXECUTION_SUCCEEDED
            )
            loop_active = True
            suggested = "Verify self-tests and outcomes; capture lessons in supervised follow-up."
        elif exec_blocked:
            stage = (
                ImprovementStepStatus.AWAITING_APPROVAL
                if has_prop
                else ImprovementStepStatus.EXECUTION_FAILED
            )
            loop_active = True
            suggested = "Execution blocked — confirm approvals, allowlists, and elevated rules."
            exec_fail_flag = stage == ImprovementStepStatus.EXECUTION_FAILED
        elif eff and not exec_success and not exec_blocked:
            exec_fail_flag = True
            if rollback_avail:
                stage = ImprovementStepStatus.ROLLBACK_AVAILABLE
                suggested = "Execution failed; rollback may be available — human review before retry."
            else:
                stage = ImprovementStepStatus.EXECUTION_FAILED
                suggested = "Execution failed; review logs and proposal before another attempt."
            loop_active = True
        elif awaiting_exec_flag:
            stage = ImprovementStepStatus.EXECUTION_IN_PROGRESS
            loop_active = True
            suggested = "Supervised execution in progress — wait for completion before new approvals."

    # --- Proposal / issue ladder ---
    if stage == ImprovementStepStatus.NO_ACTIVE_LOOP:
        approved_id = str(g.get("_workbench_operator_approved_proposal_id") or "").strip()
        if has_prop and approved_id and approved_id == pid:
            stage = ImprovementStepStatus.APPROVED_READY_FOR_EXECUTION
            loop_active = True
            awaiting_approval = False
            suggested = "Proposal approved for supervised execution — run via command layer when ready."
        elif has_prop:
            awaiting_approval = bool(requires_review)
            stage = (
                ImprovementStepStatus.AWAITING_APPROVAL
                if awaiting_approval
                else ImprovementStepStatus.PROPOSAL_READY
            )
            loop_active = True
            suggested = "Review workbench proposal and approve through supervised command flow (no auto-approve)."
        elif has_fail:
            stage = ImprovementStepStatus.ISSUE_DETECTED
            loop_active = True
            suggested = "Diagnostics failed — capture evidence; consider generating/reviewing a workbench proposal."

    # --- Carry-forward / resolution ---
    if stage == ImprovementStepStatus.NO_ACTIVE_LOOP and prev_stage not in (
        ImprovementStepStatus.NO_ACTIVE_LOOP,
        ImprovementStepStatus.RESOLVED,
    ):
        if active_issue or prev_prop:
            stage = str(prev_stage)
            loop_active = stage not in (
                ImprovementStepStatus.NO_ACTIVE_LOOP,
                ImprovementStepStatus.RESOLVED,
            )
            suggested = "Carry-over maintenance context — confirm whether issue still applies before acting."

    if (
        stage
        in (
            ImprovementStepStatus.EXECUTION_SUCCEEDED,
            ImprovementStepStatus.POST_EXECUTION_REFLECTION,
        )
        and not has_fail
        and overall == "ok"
        and not has_prop
    ):
        stage = ImprovementStepStatus.RESOLVED
        suggested = "Recent path healthy; loop marked resolved pending new diagnostics."
        loop_active = False

    if stage == ImprovementStepStatus.NO_ACTIVE_LOOP and not loop_active and not active_issue:
        suggested = "No active supervised improvement cycle."

    # Confidence
    evidence_hits = sum(
        bool(x)
        for x in (
            has_fail,
            has_prop,
            maint_join,
            eff,
            cmd,
            exec_succ_flag or exec_fail_flag,
        )
    )
    conf = _clamp01(0.28 + 0.11 * evidence_hits + (0.06 if strategic_continuity else 0))

    summary = _trunc(
        f"stage={stage}; issue={active_issue[:120]} proposal={pid or '—'}",
        320,
    )

    if pid or ptitle:
        cycles.append(
            ImprovementCycle(
                cycle_key=pid or ptitle[:48],
                headline=_trunc(ptitle or ptype, 160),
                source="workbench",
                linked_proposal_id=pid,
                status_hint=stage,
            )
        )

    meta = {
        "phase": 30,
        "respect_identity_anchors": True,
        "overall_status": overall,
        "failed_checks": failed[:12],
        "maintenance_continuity_line": maint_join[:400],
        "outcome_adjustment_target": str(getattr(ol, "adjustment_target", "") or ""),
    }

    _persist_carry(g, stage, active_issue, pid)

    final_loop_active = stage not in (
        ImprovementStepStatus.NO_ACTIVE_LOOP,
        ImprovementStepStatus.RESOLVED,
    )

    result = ImprovementLoopResult(
        loop_active=final_loop_active,
        loop_stage=stage,
        loop_summary=summary,
        active_issue=active_issue,
        active_proposal_id=pid,
        active_proposal_type=ptype,
        awaiting_approval=awaiting_approval,
        awaiting_execution=awaiting_execution,
        execution_recently_succeeded=exec_succ_flag,
        execution_recently_failed=exec_fail_flag,
        rollback_recently_used=rollback_used_flag,
        suggested_next_supervised_step=_trunc(suggested, 400),
        loop_confidence=conf,
        cycles=cycles,
        notes=notes,
        meta=meta,
    )

    print(
        f"[self_improvement_loop] stage={result.loop_stage} active={result.loop_active} "
        f"summary={result.loop_summary[:140]!r}"
    )
    print(
        f"[self_improvement_loop] proposal={result.active_proposal_id or '—'} "
        f"next={result.suggested_next_supervised_step[:120]!r} "
        f"approval_pending={result.awaiting_approval}"
    )
    return result


def _persist_carry(g: dict[str, Any], stage: str, issue: str, proposal_id: str) -> None:
    if stage in (ImprovementStepStatus.NO_ACTIVE_LOOP, ImprovementStepStatus.RESOLVED):
        g.pop(_SIL_ISSUE, None)
        g.pop(_SIL_STAGE, None)
        g.pop(_SIL_PROP, None)
        return
    if issue:
        g[_SIL_ISSUE] = issue[:600]
    g[_SIL_STAGE] = stage
    if proposal_id:
        g[_SIL_PROP] = proposal_id[:160]


def apply_improvement_loop_to_perception_state(state: Any, bundle: Any) -> None:
    """Phase 30 — map :class:`ImprovementLoopResult` onto :class:`~brain.perception.PerceptionState`."""
    ilp = getattr(bundle, "improvement_loop", None)
    if ilp is None:
        state.improvement_loop_active = False
        state.improvement_loop_stage = ImprovementStepStatus.NO_ACTIVE_LOOP
        state.improvement_loop_summary = ""
        state.improvement_active_issue = ""
        state.improvement_active_proposal_id = ""
        state.improvement_awaiting_approval = False
        state.improvement_execution_success = False
        state.improvement_execution_failed = False
        state.improvement_suggested_next_step = ""
        state.improvement_loop_meta = {"phase": 30, "idle": True}
        return

    state.improvement_loop_active = bool(ilp.loop_active)
    state.improvement_loop_stage = str(ilp.loop_stage or ImprovementStepStatus.NO_ACTIVE_LOOP)[:64]
    state.improvement_loop_summary = str(ilp.loop_summary or "")[:520]
    state.improvement_active_issue = str(ilp.active_issue or "")[:520]
    state.improvement_active_proposal_id = str(ilp.active_proposal_id or "")[:160]
    state.improvement_awaiting_approval = bool(ilp.awaiting_approval)
    state.improvement_execution_success = bool(ilp.execution_recently_succeeded)
    state.improvement_execution_failed = bool(ilp.execution_recently_failed)
    state.improvement_suggested_next_step = str(ilp.suggested_next_supervised_step or "")[:520]
    m = dict(ilp.meta or {})
    m["awaiting_execution"] = bool(ilp.awaiting_execution)
    m["rollback_recently_used"] = bool(ilp.rollback_recently_used)
    m["loop_confidence"] = float(ilp.loop_confidence)
    m["cycles"] = [asdict(c) for c in (ilp.cycles or [])[:8]]
    state.improvement_loop_meta = m
