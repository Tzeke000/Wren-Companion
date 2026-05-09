"""
Phase 16 — repair workbench proposal system (proposal-only).

Translates structured self-test/runtime diagnostics into explainable, reviewable
repair proposals. No automatic repair actions are executed in this phase.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Optional

from config.ava_tuning import WORKBENCH_CONFIG

from .perception_types import (
    ProactiveTriggerResult,
    RepairProposal,
    SelfTestRunResult,
    WorkbenchProposalResult,
)

_warning_streaks: dict[str, int] = defaultdict(int)
wbcfg = WORKBENCH_CONFIG
_ROOT = Path(__file__).resolve().parents[1]
_SUPPRESS_JSON = _ROOT / "state" / "workbench" / "suppress_proposals.json"


def _suppressed_proposal_types() -> frozenset[str]:
    s: set[str] = set(str(t).strip() for t in (wbcfg.suppress_proposal_types or ()) if str(t).strip())
    try:
        raw = json.loads(_SUPPRESS_JSON.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in ("proposal_types", "types"):
                v = raw.get(key)
                if isinstance(v, list):
                    s.update(str(x).strip() for x in v if str(x).strip())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return frozenset(s)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _mk_proposal(
    *,
    proposal_type: str,
    title: str,
    problem: str,
    likely_cause: str,
    action: str,
    risk: str,
    priority: str,
    confidence: float,
    source_checks: list[str],
    notes: list[str] | None = None,
    requires_human_review: bool = True,
    meta: dict | None = None,
) -> RepairProposal:
    pid = f"{proposal_type}:{'|'.join(sorted(source_checks or [])) or 'runtime'}"
    return RepairProposal(
        proposal_id=pid,
        proposal_type=proposal_type,
        title=title,
        problem_detected=problem,
        likely_cause=likely_cause,
        recommended_action=action,
        risk_level=risk,
        requires_human_review=requires_human_review,
        confidence=_clamp01(confidence),
        source_checks=list(source_checks or []),
        priority=priority,
        notes=list(notes or []),
        meta=dict(meta or {}),
    )


def _priority_rank(priority: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "urgent": 3}.get(priority, 0)


def _update_warning_streaks(selftests: SelfTestRunResult | None) -> None:
    if selftests is None:
        return
    warning_names = set(selftests.summary.warning_checks or [])
    for name in list(_warning_streaks.keys()):
        if name in warning_names:
            _warning_streaks[name] += 1
        else:
            _warning_streaks[name] = 0
    for name in warning_names:
        if name not in _warning_streaks:
            _warning_streaks[name] = 1


def build_workbench_proposals(
    *,
    selftests: Optional[SelfTestRunResult],
    acquisition_freshness: str,
    proactive_trigger: Optional[ProactiveTriggerResult],
) -> WorkbenchProposalResult:
    """
    Build reviewable repair proposals from structured diagnostics.
    This function never executes repairs.
    """
    st = selftests or SelfTestRunResult()
    pt = proactive_trigger or ProactiveTriggerResult()
    _update_warning_streaks(st)
    proposals: list[RepairProposal] = []

    failed = set(st.summary.failed_checks or [])
    warning = set(st.summary.warning_checks or [])

    if any(x.startswith("camera_module_available") or x.startswith("camera_read_path_callable") for x in failed):
        proposals.append(
            _mk_proposal(
                proposal_type="camera_diagnostics_proposal",
                title="Camera Runtime Diagnostics Required",
                problem="Camera module/read path check failed.",
                likely_cause="Camera dependency issue, missing device access, or manager initialization failure.",
                action="Run camera dependency and device-index diagnostics; verify camera permissions and CameraManager setup.",
                risk="low",
                priority="high",
                confidence=0.84,
                source_checks=[x for x in failed if "camera_" in x],
                notes=["no automatic restart or device switch applied"],
            )
        )

    if acquisition_freshness in ("stale", "unavailable") or "acquisition_freshness_runtime" in warning:
        pri = "high" if acquisition_freshness == "unavailable" else "medium"
        conf = 0.78 if acquisition_freshness == "unavailable" else 0.68
        proposals.append(
            _mk_proposal(
                proposal_type="frame_freshness_investigation_proposal",
                title="Investigate Frame Freshness Path",
                problem=f"Frame acquisition freshness is `{acquisition_freshness}`.",
                likely_cause="Live frame buffering/source timing issue, stale UI frame path, or camera read lag.",
                action="Inspect frame-source timestamps, cache age, and camera capture cadence before changing thresholds.",
                risk="low",
                priority=pri,
                confidence=conf,
                source_checks=[c for c in st.summary.warning_checks if "acquisition_freshness" in c],
                notes=["focus on diagnostics first; avoid immediate threshold changes"],
            )
        )

    missing_file_checks = [x for x in warning if x.startswith("file_exists:")] + [x for x in failed if x.startswith("file_exists:")]
    missing_dir_checks = [x for x in failed if x.startswith("dir_exists:")]
    if missing_file_checks or missing_dir_checks:
        pri = "urgent" if any("ava_core/IDENTITY.md" in x or "ava_core/SOUL.md" in x or "ava_core/USER.md" in x for x in missing_file_checks) else "high"
        proposals.append(
            _mk_proposal(
                proposal_type="file_restore_or_validate_proposal",
                title="Restore or Validate Missing Core Paths",
                problem="Expected files/directories are missing according to self-tests.",
                likely_cause="Accidental deletion, incomplete setup, or workspace mismatch.",
                action="Verify required paths against repository baseline, then restore missing files/directories with manual review.",
                risk="medium",
                priority=pri,
                confidence=0.80,
                source_checks=sorted(missing_file_checks + missing_dir_checks),
                notes=["manual validation required before restore"],
            )
        )

    model_checks = [x for x in warning if "model_provider_hooks_exposed" in x]
    if model_checks:
        proposals.append(
            _mk_proposal(
                proposal_type="model_hook_configuration_proposal",
                title="Review Model/Provider Hook Configuration",
                problem="Model/provider hooks were not clearly exposed in runtime diagnostics.",
                likely_cause="Runtime globals missing expected model callables or initialization order issue.",
                action="Review model/provider initialization and exported hooks before enabling any behavior changes.",
                risk="high",
                priority="medium",
                confidence=0.64,
                source_checks=model_checks,
                notes=["configuration-oriented recommendation only"],
            )
        )

    audio_checks = [x for x in warning if "audio_input_path_available" in x] + [x for x in failed if "audio_input_path_available" in x]
    if audio_checks:
        proposals.append(
            _mk_proposal(
                proposal_type="audio_path_diagnostics_proposal",
                title="Audit Audio Input/Transcription Path",
                problem="Audio input path is warning/failed in current context.",
                likely_cause="Whisper model not initialized, missing audio device path, or environment limitations.",
                action="Verify microphone source availability and transcription hook setup in this runtime environment.",
                risk="low",
                priority="medium",
                confidence=0.62,
                source_checks=audio_checks,
                notes=["if audio is intentionally disabled, close this proposal as expected"],
            )
        )

    recurring_warning_checks = [
        name for name, streak in _warning_streaks.items() if streak >= wbcfg.recurring_warning_streak_min
    ]
    if recurring_warning_checks:
        proposals.append(
            _mk_proposal(
                proposal_type="recurring_warning_review_proposal",
                title="Recurring Warning Pattern Review",
                problem="Some warnings are recurring across self-test cycles.",
                likely_cause="Persistent subsystem degradation or unresolved configuration mismatch.",
                action="Schedule focused subsystem review for repeated warnings and collect additional diagnostic traces.",
                risk="medium",
                priority="high",
                confidence=0.73,
                source_checks=sorted(recurring_warning_checks),
                notes=["triggered by warning streak >= 3"],
                meta={"warning_streaks": {k: _warning_streaks[k] for k in recurring_warning_checks}},
            )
        )

    # Optional context: proactive suppression can indicate silent degraded context.
    if pt.suppression_reason in ("quality_unreliable", "acquisition_not_fresh", "vision_untrusted"):
        proposals.append(
            _mk_proposal(
                proposal_type="frame_freshness_investigation_proposal",
                title="Proactive Layer Suppressed by Vision Reliability",
                problem=f"Proactive recommendations suppressed: `{pt.suppression_reason}`.",
                likely_cause="Vision reliability gate currently blocks confident interaction triggers.",
                action="Review vision-quality and freshness diagnostics before tuning proactive behavior.",
                risk="low",
                priority="medium",
                confidence=0.66,
                source_checks=["proactive_suppression_context"],
                notes=["derived from proactive suppression evidence"],
            )
        )

    suppressed = _suppressed_proposal_types()
    if suppressed:
        proposals = [p for p in proposals if getattr(p, "proposal_type", "") not in suppressed]

    if not proposals:
        noop = _mk_proposal(
            proposal_type="no_action_needed",
            title="No Repair Action Needed",
            problem="No high-confidence repair targets detected from current diagnostics.",
            likely_cause="Runtime health is acceptable for this cycle.",
            action="Continue monitoring; no repair changes recommended.",
            risk="low",
            priority="low",
            confidence=0.90,
            source_checks=[],
            notes=["conservative no-op recommendation"],
            requires_human_review=False,
        )
        proposals = [noop]
        print("[workbench] no_action_needed diagnostics_stable")
    else:
        for p in proposals[:4]:
            print(
                f"[workbench] proposal={p.proposal_type} "
                f"priority={p.priority} risk={p.risk_level}"
            )

    top = sorted(
        proposals,
        key=lambda p: (_priority_rank(p.priority), float(p.confidence)),
        reverse=True,
    )[0]
    has_real = any(p.proposal_type != "no_action_needed" for p in proposals)
    summary = (
        f"workbench proposals={len(proposals)} top={top.proposal_type} "
        f"priority={top.priority}"
    )
    return WorkbenchProposalResult(
        has_proposal=has_real,
        top_proposal=top,
        proposals=proposals,
        summary=summary,
        meta={
            "failed_count": len(st.summary.failed_checks or []),
            "warning_count": len(st.summary.warning_checks or []),
            "run_type": st.run_type,
        },
    )
