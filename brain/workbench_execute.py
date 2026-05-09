"""
Phase 16.5 — supervised code/file execution layer.

Executes approved workbench change plans with strict path controls, backups, and
rollback support. No execution is performed implicitly by the perception pipeline.
"""
from __future__ import annotations

import difflib
import shutil
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .perception_types import (
    FileChangePlan,
    FileChangeRecord,
    RepairProposal,
    WorkbenchExecutionRequest,
    WorkbenchExecutionResult,
)
from .shared import atomic_json_save, json_load, now_iso

_ROOT = Path(__file__).resolve().parents[1]
_WORKBENCH_STATE = _ROOT / "state" / "workbench"
_BACKUP_DIR = _WORKBENCH_STATE / "backups"
_STAGED_DIR = _WORKBENCH_STATE / "staged"
_HISTORY_DIR = _WORKBENCH_STATE / "history"
_LAST_EXECUTION_MANIFEST = _HISTORY_DIR / "last_execution.json"

_ALLOWED_PREFIXES = (
    "brain/",
    "docs/",
    "config/",
    "state/workbench/",
    "workbench/",
    "proposals/",
)
_BLOCKED_PREFIXES = (
    ".git/",
    ".venv/",
    "venv/",
    "memory/chroma.sqlite3",
    "memory/chroma/",
    ".env",
    "secrets/",
)
_SENSITIVE_PATHS = {
    "ava_core/IDENTITY.md",
    "ava_core/SOUL.md",
    "ava_core/USER.md",
    "avaagent.py",
}
_SUPPORTED_ACTIONS = {
    "create_file",
    "modify_file",
    "replace_file",
    "append_file",
    "create_directory",
    "write_patch_plan",
    "apply_patch_plan",
    "rollback_last_change",
}
_PROPOSAL_ACTION_ALLOW = {
    "camera_diagnostics_proposal": {"write_patch_plan"},
    "frame_freshness_investigation_proposal": {"write_patch_plan", "modify_file"},
    "file_restore_or_validate_proposal": {"create_file", "replace_file", "modify_file", "create_directory"},
    "model_hook_configuration_proposal": {"write_patch_plan", "modify_file"},
    "audio_path_diagnostics_proposal": {"write_patch_plan", "modify_file"},
    "recurring_warning_review_proposal": {"write_patch_plan"},
    "no_action_needed": set(),
}


def _ensure_dirs() -> None:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    _STAGED_DIR.mkdir(parents=True, exist_ok=True)
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_rel(path_str: str) -> str:
    p = Path(path_str.replace("\\", "/"))
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(_ROOT.resolve())
            return str(rel).replace("\\", "/")
        except Exception:
            return ""
    return str(p).lstrip("./").replace("\\", "/")


def _is_allowed_path(rel_path: str) -> tuple[bool, str, bool]:
    if not rel_path:
        return False, "empty_or_outside_workspace_path", False
    if any(rel_path == b or rel_path.startswith(b) for b in _BLOCKED_PREFIXES):
        return False, "blocked_prefix_or_secret_path", False
    allowed = any(rel_path.startswith(prefix) for prefix in _ALLOWED_PREFIXES)
    sensitive = rel_path in _SENSITIVE_PATHS
    if not allowed and not sensitive:
        return False, "path_not_in_allowlist", sensitive
    return True, "", sensitive


def _proposal_allows_action(proposal: RepairProposal, action_type: str) -> bool:
    allowed = _PROPOSAL_ACTION_ALLOW.get(proposal.proposal_type, {"write_patch_plan"})
    return action_type in allowed


def _build_diff_summary(before_text: str, after_text: str, rel_path: str) -> str:
    diff = list(
        difflib.unified_diff(
            (before_text or "").splitlines(),
            (after_text or "").splitlines(),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
            n=2,
        )
    )
    if not diff:
        return f"{rel_path}: no textual diff"
    return "\n".join(diff[:120])


def _backup_existing_file(target: Path, execution_id: str) -> str:
    rel = _normalize_rel(str(target))
    safe = rel.replace("/", "__")
    backup_name = f"{execution_id}__{safe}.bak"
    dst = _BACKUP_DIR / backup_name
    shutil.copy2(target, dst)
    return str(dst)


def _record_manifest(execution_id: str, created: list[str], backups: dict[str, str]) -> None:
    _ensure_dirs()
    payload = {
        "execution_id": execution_id,
        "created_files": created,
        "backups": backups,
        "timestamp": now_iso(),
        "rollback_available": True,
    }
    atomic_json_save(str(_LAST_EXECUTION_MANIFEST), payload)


def _build_empty_result(req: WorkbenchExecutionRequest) -> WorkbenchExecutionResult:
    return WorkbenchExecutionResult(
        execution_id=req.execution_id or str(uuid4()),
        proposal_id=req.proposal_id,
        approved=bool(req.approved),
        execution_mode=req.execution_mode,
        action_type=req.action_type,
        target_paths=list(req.target_paths),
        success=False,
        blocked=False,
        rollback_available=False,
    )


def execute_workbench_request(
    request: WorkbenchExecutionRequest,
    *,
    proposal: Optional[RepairProposal] = None,
) -> WorkbenchExecutionResult:
    """
    Execute a supervised request using approval, allowlist, and backup/rollback rules.
    """
    _ensure_dirs()
    req = request
    res = _build_empty_result(req)
    print(
        f"[workbench_execute] mode={req.execution_mode} approved={req.approved} "
        f"action={req.action_type}"
    )

    if req.action_type not in _SUPPORTED_ACTIONS:
        res.blocked = True
        res.denial_reason = "unsupported_action_type"
        print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
        return res
    if req.execution_mode not in {"dry_run", "staged", "apply"}:
        res.blocked = True
        res.denial_reason = "unsupported_execution_mode"
        print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
        return res
    if req.action_type == "rollback_last_change":
        return rollback_last_change(req)

    # Proposal gating for actions that can mutate content.
    if req.execution_mode in {"staged", "apply"}:
        if proposal is None or proposal.proposal_id != req.proposal_id:
            res.blocked = True
            res.denial_reason = "valid_proposal_required"
            print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
            return res
        if not _proposal_allows_action(proposal, req.action_type):
            res.blocked = True
            res.denial_reason = "proposal_action_mismatch"
            print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
            return res
    if req.execution_mode == "apply" and not req.approved:
        res.blocked = True
        res.denial_reason = "approval_required_for_apply"
        print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
        return res
    if req.execution_mode == "staged" and not req.approved:
        # conservative interpretation: staged artifacts also require approval.
        res.blocked = True
        res.denial_reason = "approval_required_for_staged"
        print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
        return res

    plans = list(req.change_plans or [])
    if not plans and req.target_paths:
        plans = [
            FileChangePlan(action_type=req.action_type, target_path=p, reason="auto_plan_from_target_paths")
            for p in req.target_paths
        ]
    if not plans:
        res.blocked = True
        res.denial_reason = "no_change_plan_provided"
        print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
        return res

    backup_map: dict[str, str] = {}
    created_files: list[str] = []
    modified_files: list[str] = []
    diff_summary: list[str] = []
    file_records: list[FileChangeRecord] = []
    had_error = False

    for plan in plans:
        rel = _normalize_rel(plan.target_path)
        ok_path, deny_reason, sensitive = _is_allowed_path(rel)
        rec = FileChangeRecord(target_path=rel, action_type=plan.action_type or req.action_type)
        if not ok_path:
            rec.error_message = deny_reason
            file_records.append(rec)
            had_error = True
            continue
        if sensitive and not req.elevated_approval:
            rec.error_message = "requires_elevated_approval"
            rec.notes.append("sensitive_target")
            file_records.append(rec)
            had_error = True
            res.requires_elevated_approval = True
            continue

        tgt = _ROOT / rel
        action = plan.action_type or req.action_type
        try:
            before_text = ""
            if tgt.exists() and tgt.is_file():
                before_text = tgt.read_text(encoding="utf-8", errors="ignore")
            after_text = before_text

            if action == "create_directory":
                if req.execution_mode == "apply":
                    tgt.mkdir(parents=True, exist_ok=True)
                elif req.execution_mode == "staged":
                    pass
                rec.success = True
                rec.notes.append("directory_create_requested")
            elif action == "create_file":
                after_text = plan.content or ""
                if req.execution_mode == "apply":
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    tgt.write_text(after_text, encoding="utf-8")
                    created_files.append(rel)
                    rec.created = True
                elif req.execution_mode == "staged":
                    staged_file = _STAGED_DIR / f"{res.execution_id}__{rel.replace('/', '__')}.staged.txt"
                    staged_file.parent.mkdir(parents=True, exist_ok=True)
                    staged_file.write_text(after_text, encoding="utf-8")
                    rec.notes.append(f"staged_artifact={staged_file}")
                rec.success = True
            elif action in {"modify_file", "replace_file", "apply_patch_plan"}:
                replacement = plan.content or ""
                if action == "apply_patch_plan" and not replacement:
                    raise ValueError("apply_patch_plan requires plan.content in this phase")
                after_text = replacement if action != "modify_file" else (plan.content or before_text)
                if req.execution_mode == "apply":
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    if tgt.exists():
                        bk = _backup_existing_file(tgt, res.execution_id)
                        backup_map[rel] = bk
                        rec.backup_path = bk
                    tgt.write_text(after_text, encoding="utf-8")
                    modified_files.append(rel)
                    rec.modified = True
                elif req.execution_mode == "staged":
                    staged_file = _STAGED_DIR / f"{res.execution_id}__{rel.replace('/', '__')}.staged.txt"
                    staged_file.parent.mkdir(parents=True, exist_ok=True)
                    staged_file.write_text(after_text, encoding="utf-8")
                    rec.notes.append(f"staged_artifact={staged_file}")
                rec.success = True
            elif action == "append_file":
                append_text = plan.append_text or plan.content or ""
                after_text = before_text + append_text
                if req.execution_mode == "apply":
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    if tgt.exists():
                        bk = _backup_existing_file(tgt, res.execution_id)
                        backup_map[rel] = bk
                        rec.backup_path = bk
                    tgt.write_text(after_text, encoding="utf-8")
                    modified_files.append(rel)
                    rec.modified = True
                elif req.execution_mode == "staged":
                    staged_file = _STAGED_DIR / f"{res.execution_id}__{rel.replace('/', '__')}.staged.txt"
                    staged_file.parent.mkdir(parents=True, exist_ok=True)
                    staged_file.write_text(after_text, encoding="utf-8")
                    rec.notes.append(f"staged_artifact={staged_file}")
                rec.success = True
            elif action == "write_patch_plan":
                patch = plan.patch_text or _build_diff_summary(before_text, plan.content or before_text, rel)
                staged_patch = _STAGED_DIR / f"{res.execution_id}__{rel.replace('/', '__')}.patch"
                staged_patch.parent.mkdir(parents=True, exist_ok=True)
                if req.execution_mode in {"staged", "apply"}:
                    staged_patch.write_text(patch, encoding="utf-8")
                    rec.notes.append(f"patch_plan={staged_patch}")
                rec.success = True
                after_text = plan.content or before_text
            else:
                raise ValueError(f"unsupported_action:{action}")

            rec.diff_summary = _build_diff_summary(before_text, after_text, rel)
            diff_summary.append(rec.diff_summary)
        except Exception as e:
            rec.error_message = str(e)
            had_error = True
        file_records.append(rec)

    if req.execution_mode == "apply" and not had_error:
        _record_manifest(res.execution_id, created_files, backup_map)

    res.success = bool(not had_error)
    res.blocked = bool(not res.success and not any(r.success for r in file_records))
    res.target_paths = [r.target_path for r in file_records]
    res.created_files = created_files
    res.modified_files = modified_files
    res.backup_paths = list(backup_map.values())
    res.diff_summary = diff_summary[:24]
    res.rollback_available = bool(req.execution_mode == "apply" and not had_error and (created_files or backup_map))
    res.rollback_hint = "Use action_type=rollback_last_change with approved=true." if res.rollback_available else ""
    res.file_records = file_records
    if had_error:
        res.error_message = "one_or_more_file_actions_failed"
    print(
        f"[workbench_execute] modified={len(res.modified_files)} backups={len(res.backup_paths)} "
        f"success={res.success}"
    )
    return res


def rollback_last_change(request: WorkbenchExecutionRequest) -> WorkbenchExecutionResult:
    """Rollback the latest applied execution by restoring backups and removing created files."""
    req = request
    res = _build_empty_result(req)
    res.action_type = "rollback_last_change"
    if not req.approved:
        res.blocked = True
        res.denial_reason = "approval_required_for_rollback"
        print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
        return res

    manifest = json_load(str(_LAST_EXECUTION_MANIFEST), {})
    if not manifest:
        res.blocked = True
        res.denial_reason = "no_rollback_manifest"
        print(f"[workbench_execute] blocked=True reason={res.denial_reason}")
        return res

    backups: dict = dict(manifest.get("backups") or {})
    created: list = list(manifest.get("created_files") or [])
    file_records: list[FileChangeRecord] = []
    had_error = False

    for rel, bk in backups.items():
        rec = FileChangeRecord(target_path=rel, action_type="restore_backup")
        try:
            src = Path(bk)
            dst = _ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            rec.success = True
            rec.modified = True
            rec.backup_path = str(src)
        except Exception as e:
            rec.error_message = str(e)
            had_error = True
        file_records.append(rec)

    for rel in created:
        rec = FileChangeRecord(target_path=rel, action_type="remove_created_file")
        try:
            tgt = _ROOT / rel
            if tgt.exists() and tgt.is_file():
                tgt.unlink()
            rec.success = True
            rec.notes.append("removed_created_file")
        except Exception as e:
            rec.error_message = str(e)
            had_error = True
        file_records.append(rec)

    res.success = not had_error
    res.file_records = file_records
    res.modified_files = [r.target_path for r in file_records if r.modified]
    res.created_files = []
    res.rollback_available = False
    res.notes.append(f"rolled_back_execution_id={manifest.get('execution_id')}")
    print(
        f"[workbench_execute] rollback modified={len(res.modified_files)} "
        f"success={res.success}"
    )
    return res
