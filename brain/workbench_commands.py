"""
Phase 16.6 — workbench approval and execution command layer.

Provides a structured operator-facing command flow for listing/reviewing proposals
and invoking supervised execution (dry-run/staged/apply/rollback) without bypassing
Phase 16.5 safety checks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .perception_types import (
    RepairProposal,
    WorkbenchCommandRequest,
    WorkbenchCommandResult,
    WorkbenchExecutionRequest,
    WorkbenchProposalResult,
    WorkbenchProposalView,
    WorkbenchQueueState,
)
from .shared import json_load, now_iso
from .workbench_execute import execute_workbench_request

_ROOT = Path(__file__).resolve().parents[1]
_LAST_MANIFEST = _ROOT / "state" / "workbench" / "history" / "last_execution.json"

_last_execution_info: dict = {}
_last_rollback_info: dict = {}
_selected_proposal_id: str = ""


def _proposal_view(p: RepairProposal) -> WorkbenchProposalView:
    return WorkbenchProposalView(
        proposal_id=p.proposal_id,
        proposal_type=p.proposal_type,
        title=p.title,
        priority=p.priority,
        risk_level=p.risk_level,
        confidence=float(p.confidence),
        requires_human_review=bool(p.requires_human_review),
        summary=p.problem_detected,
        meta={"source_checks": list(p.source_checks)},
    )


def _proposals_from_result(wb: Optional[WorkbenchProposalResult]) -> list[RepairProposal]:
    if wb is None:
        return []
    return list(wb.proposals or [])


def _find_proposal(proposals: list[RepairProposal], proposal_id: str) -> Optional[RepairProposal]:
    if proposal_id:
        for p in proposals:
            if p.proposal_id == proposal_id:
                return p
        return None
    if proposals:
        return proposals[0]
    return None


def _manifest_info() -> dict:
    return dict(json_load(str(_LAST_MANIFEST), {}))


def _queue_state(proposals: list[RepairProposal]) -> WorkbenchQueueState:
    top = proposals[0] if proposals else RepairProposal()
    return WorkbenchQueueState(
        has_proposals=bool(proposals),
        proposal_count=len(proposals),
        selected_proposal_id=_selected_proposal_id or (top.proposal_id if proposals else ""),
        top_proposal_id=top.proposal_id,
        top_proposal_type=top.proposal_type,
        top_proposal_title=top.title,
        top_proposal_priority=top.priority,
        top_proposal_risk=top.risk_level,
        approval_needed=True,
        last_execution_info=dict(_last_execution_info),
        last_rollback_info=dict(_last_rollback_info),
        meta={"manifest": _manifest_info()},
    )


def _command_alias(name: str) -> str:
    n = (name or "").strip().lower()
    aliases = {
        "approve_dry_run": "approve_proposal_dry_run",
        "approve_staged": "approve_proposal_staged",
        "approve_apply": "approve_proposal_apply",
        "rollback_last": "rollback_last_change",
    }
    return aliases.get(n, n)


def _result_base(req: WorkbenchCommandRequest, proposals: list[RepairProposal]) -> WorkbenchCommandResult:
    return WorkbenchCommandResult(
        command_name=req.command_name,
        proposal_id=req.proposal_id,
        execution_mode=req.execution_mode,
        approved=bool(req.approved),
        elevated_approval=bool(req.elevated_approval),
        available_proposals=[_proposal_view(p) for p in proposals],
        queue_state=_queue_state(proposals),
        last_execution_info=dict(_last_execution_info),
        last_rollback_info=dict(_last_rollback_info),
    )


def handle_workbench_command(
    request: WorkbenchCommandRequest,
    *,
    proposal_result: Optional[WorkbenchProposalResult],
) -> WorkbenchCommandResult:
    """
    Handle review/approval command requests for workbench proposals.
    """
    global _selected_proposal_id, _last_execution_info, _last_rollback_info
    req = request
    cmd = _command_alias(req.command_name)
    proposals = _proposals_from_result(proposal_result)
    res = _result_base(req, proposals)
    res.command_name = cmd

    if cmd == "list_proposals":
        res.success = True
        res.summary = f"{len(proposals)} proposal(s) available."
        print(f"[workbench_commands] cmd={cmd} proposal=- success=True")
        return res

    if cmd == "show_workbench_status":
        res.success = True
        res.summary = (
            f"has_proposals={bool(proposals)} selected={_selected_proposal_id or '-'} "
            f"last_execution={bool(_last_execution_info)}"
        )
        print(f"[workbench_commands] cmd={cmd} proposal=- success=True")
        return res

    if cmd == "show_last_execution":
        info = _last_execution_info or _manifest_info()
        if not info:
            res.success = False
            res.blocked_reason = "no_last_execution_available"
            res.summary = "No execution info available."
        else:
            res.success = True
            res.details = {"last_execution": info}
            res.summary = "Loaded last execution info."
        print(f"[workbench_commands] cmd={cmd} proposal=- success={res.success}")
        return res

    if cmd == "show_proposal":
        selected = _find_proposal(proposals, req.proposal_id)
        if selected is None:
            res.success = False
            res.blocked_reason = "proposal_not_found_or_unavailable"
            res.summary = "Proposal not found in current in-memory proposal set."
            print(f"[workbench_commands] blocked=True reason={res.blocked_reason}")
            return res
        _selected_proposal_id = selected.proposal_id
        res.proposal_id = selected.proposal_id
        res.success = True
        res.details = {"proposal": _proposal_view(selected).__dict__}
        res.summary = f"Selected proposal `{selected.proposal_id}`."
        print(f"[workbench_commands] cmd={cmd} proposal={selected.proposal_id} success=True")
        return res

    if cmd in {"approve_proposal_dry_run", "approve_proposal_staged", "approve_proposal_apply"}:
        selected = _find_proposal(proposals, req.proposal_id or _selected_proposal_id)
        if selected is None:
            res.success = False
            res.blocked_reason = "proposal_not_found_or_stale"
            res.summary = "No matching proposal available for approval command."
            print(f"[workbench_commands] blocked=True reason={res.blocked_reason}")
            return res
        _selected_proposal_id = selected.proposal_id

        mode = {
            "approve_proposal_dry_run": "dry_run",
            "approve_proposal_staged": "staged",
            "approve_proposal_apply": "apply",
        }[cmd]
        approved_flag = bool(req.approved) if mode in {"staged", "apply"} else True
        ex_req = WorkbenchExecutionRequest(
            proposal_id=selected.proposal_id,
            approved=approved_flag,
            elevated_approval=bool(req.elevated_approval),
            execution_mode=mode,
            action_type="write_patch_plan" if mode == "dry_run" else "modify_file",
            target_paths=[],
            change_plans=[],
            requested_by=req.requested_by or "operator",
            meta={"command": cmd, "created_at": now_iso()},
        )
        ex_res = execute_workbench_request(ex_req, proposal=selected)
        res.execution_result = ex_res
        res.proposal_id = selected.proposal_id
        res.execution_mode = mode
        res.success = bool(ex_res.success)
        if ex_res.blocked:
            res.blocked_reason = ex_res.denial_reason or "execution_blocked"
        res.summary = (
            f"{cmd} -> success={ex_res.success} blocked={ex_res.blocked} "
            f"modified={len(ex_res.modified_files)}"
        )
        _last_execution_info = {
            "command": cmd,
            "proposal_id": selected.proposal_id,
            "mode": mode,
            "success": bool(ex_res.success),
            "blocked": bool(ex_res.blocked),
            "denial_reason": ex_res.denial_reason,
            "modified_files": list(ex_res.modified_files),
            "backup_paths": list(ex_res.backup_paths),
            "rollback_available": bool(ex_res.rollback_available),
            "timestamp": now_iso(),
        }
        print(
            f"[workbench_commands] cmd={cmd} proposal={selected.proposal_id} "
            f"success={res.success}"
        )
        print(f"[workbench_commands] execution mode={mode} summary={res.summary}")
        return res

    if cmd == "rollback_last_change":
        ex_req = WorkbenchExecutionRequest(
            proposal_id=req.proposal_id or _selected_proposal_id,
            approved=bool(req.approved),
            elevated_approval=bool(req.elevated_approval),
            execution_mode="apply",
            action_type="rollback_last_change",
            requested_by=req.requested_by or "operator",
            meta={"command": cmd, "created_at": now_iso()},
        )
        ex_res = execute_workbench_request(ex_req, proposal=None)
        res.execution_result = ex_res
        res.success = bool(ex_res.success)
        if ex_res.blocked:
            res.blocked_reason = ex_res.denial_reason or "rollback_blocked"
        res.summary = (
            f"rollback_last_change -> success={ex_res.success} "
            f"blocked={ex_res.blocked}"
        )
        _last_rollback_info = {
            "success": bool(ex_res.success),
            "blocked": bool(ex_res.blocked),
            "denial_reason": ex_res.denial_reason,
            "modified_files": list(ex_res.modified_files),
            "timestamp": now_iso(),
        }
        print(
            f"[workbench_commands] cmd={cmd} proposal={req.proposal_id or '-'} "
            f"success={res.success}"
        )
        return res

    res.success = False
    res.blocked_reason = "unsupported_command_name"
    res.summary = "Unsupported workbench command."
    print(f"[workbench_commands] blocked=True reason={res.blocked_reason}")
    return res
