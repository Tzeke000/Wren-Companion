"""
Startup and runtime concern reconciliation: current evidence wins over historical carryover.

Resolves stale camera/maintenance/warning concerns when live signals are healthy.
Does not approve workbench actions or mutate ava_core identity files.
"""
from __future__ import annotations

import json
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .health import load_health_state
from .perception_types import (
    ConcernReconciliationResult,
    ConcernRecord,
    ConcernStatusUpdate,
    HeartbeatTickResult,
    ImprovementLoopResult,
    IdentityResolutionResult,
    QualityOutput,
    RuntimePresenceResult,
    SelfTestRunResult,
    StrategicContinuityResult,
    WorkbenchProposalResult,
)
from .shared import now_ts

_REPO = Path(__file__).resolve().parent.parent
_CONCERN_DIR = _REPO / "state" / "concerns"
_REGISTRY_PATH = _CONCERN_DIR / "concerns_registry.json"

_COOLDOWN_SEC = 6 * 3600
_RESOLVED_QUIET_SEC = 2 * 3600
_USER_DISMISS_COOLDOWN_SEC = 3600

_CAMERA_FRESH_RESOLVE_SEC = 30.0
_CAMERA_STALE_FLAG_SEC = 60.0
_CAMERA_CONF_GOOD = 0.40
_CAMERA_CONF_LOW = 0.25

CT_CAMERA_STALE = "camera_stale"
CT_RECOGNITION_UNCERTAIN = "recognition_uncertain"
CT_MAINTENANCE_WORKBENCH = "maintenance_workbench"
CT_RECURRING_WARNING = "recurring_warning"

STATUS_ACTIVE = "active"
STATUS_WEAKENED = "weakened"
STATUS_RESOLVED = "resolved"
STATUS_STALE = "stale"
STATUS_ARCHIVED = "archived"


def _trunc(s: str, n: int = 180) -> str:
    t = " ".join((s or "").split())
    if len(t) <= n:
        return t
    return t[: n - 1].rstrip() + "…"


def _ensure_dir() -> None:
    _CONCERN_DIR.mkdir(parents=True, exist_ok=True)


def _camera_age_seconds(host: dict[str, Any]) -> Optional[float]:
    p = host.get("CAMERA_LATEST_JSON_PATH")
    if not p:
        return None
    path = Path(str(p))
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = raw.get("time") or raw.get("capture_ts") or raw.get("wall_time")
        if ts is None:
            return None
        from .shared import iso_to_ts

        return max(0.0, float(now_ts() - iso_to_ts(str(ts))))
    except Exception:
        return None


def gather_startup_evidence(host: dict[str, Any]) -> dict[str, Any]:
    """Bounded evidence from filesystem + health (no full perception bundle)."""
    health = load_health_state(host)
    age = _camera_age_seconds(host)
    cam_ok = age is not None and age <= float(host.get("HEALTH_CAMERA_STALE_SECONDS", 25.0) or 25.0)
    issues = list(health.get("issues") or [])[-12:]
    degraded = str(health.get("degraded_mode") or "none")
    ev: dict[str, Any] = {
        "source": "startup",
        "camera_age_sec": age,
        "camera_recent_ok": bool(cam_ok),
        "health_degraded_mode": degraded,
        "health_issue_count": len(issues),
        "vision_trusted": None,
        "acquisition_freshness": "unknown",
        "identity_state": "unknown",
        "selftest_overall": "unknown",
        "failed_checks": [],
        "warning_checks": [],
        "workbench_has_proposal": False,
        "improvement_loop_stage": "",
        "heartbeat_mode": "",
    }
    return ev


def gather_runtime_evidence(
    *,
    trusted: bool,
    acquisition_freshness: str,
    id_res: Optional[IdentityResolutionResult],
    qual: Optional[QualityOutput],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    improvement_loop: Optional[ImprovementLoopResult],
    heartbeat: Optional[HeartbeatTickResult],
    runtime_presence: Optional[RuntimePresenceResult],
    host_or_g: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    st_ov = "ok"
    failed: list[str] = []
    warn: list[str] = []
    if selftests is not None:
        st_ov = str(selftests.summary.overall_status or "ok")
        failed = list(selftests.summary.failed_checks or [])[:16]
        warn = list(selftests.summary.warning_checks or [])[:16]

    ident = str(id_res.identity_state or "no_face") if id_res else "unknown"
    qlabel = "unreliable"
    if qual is not None and qual.structured is not None:
        qlabel = str(getattr(qual.structured, "quality_label", "") or "unreliable")

    wb_prop = bool(workbench.has_proposal) if workbench is not None else False
    ilp_stage = ""
    ilp_active = False
    if improvement_loop is not None:
        ilp_stage = str(improvement_loop.loop_stage or "")
        ilp_active = bool(improvement_loop.loop_active)

    hb_mode = str(heartbeat.heartbeat_mode or "") if heartbeat is not None else ""
    rp_mode = str(runtime_presence.presence_mode or "") if runtime_presence is not None else ""

    fq = float(getattr(qual, "frame_quality", 0.0) or 0.0) if qual is not None else None

    age = None
    if runtime_presence is not None and hasattr(runtime_presence, "meta"):
        try:
            age = runtime_presence.meta.get("camera_age_sec")
        except Exception:
            age = None
    if age is None and isinstance(heartbeat, HeartbeatTickResult):
        try:
            age = heartbeat.meta.get("camera_age_sec") if isinstance(heartbeat.meta, dict) else None
        except Exception:
            age = None
    if age is None and isinstance(host_or_g, dict):
        age = _camera_age_seconds(host_or_g)
    recog_conf = float(getattr(id_res, "identity_confidence", 0.0) or 0.0) if id_res is not None else 0.0

    ev = {
        "source": "runtime",
        "vision_trusted": bool(trusted),
        "acquisition_freshness": str(acquisition_freshness or "unknown"),
        "quality_label": qlabel,
        "identity_state": ident,
        "selftest_overall": st_ov,
        "failed_checks": failed,
        "warning_checks": warn,
        "workbench_has_proposal": wb_prop,
        "improvement_loop_stage": ilp_stage,
        "improvement_loop_active": ilp_active,
        "heartbeat_mode": hb_mode,
        "presence_mode": rp_mode,
        "camera_quality_gate": fq,
        "camera_age_sec": age,
        "recognition_confidence": recog_conf,
    }
    return ev


def _registry_load() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _ensure_dir()
    if not _REGISTRY_PATH.is_file():
        return [], {}
    try:
        raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return [], {}
        concerns = raw.get("concerns")
        if not isinstance(concerns, list):
            concerns = []
        meta = {k: v for k, v in raw.items() if k != "concerns"}
        return concerns, meta
    except Exception:
        return [], {}


def _registry_save(concerns: list[dict[str, Any]], meta_extra: dict[str, Any]) -> None:
    _ensure_dir()
    payload = {
        "version": 1,
        "concerns": concerns,
        "last_saved_ts": now_ts(),
        **meta_extra,
    }
    tmp = _REGISTRY_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(_REGISTRY_PATH)


def _record_from_dict(d: dict[str, Any]) -> ConcernRecord:
    return ConcernRecord(
        concern_id=str(d.get("concern_id") or ""),
        concern_type=str(d.get("concern_type") or ""),
        status=str(d.get("status") or STATUS_ACTIVE),
        created_at=float(d.get("created_at") or 0.0),
        last_seen_at=float(d.get("last_seen_at") or 0.0),
        supporting_evidence=dict(d.get("supporting_evidence") or {}),
        current_evidence=dict(d.get("current_evidence") or {}),
        resolution_reason=str(d.get("resolution_reason") or ""),
        should_surface_now=bool(d.get("should_surface_now", False)),
        cooldown_until=float(d.get("cooldown_until") or 0.0),
        notes=list(d.get("notes") or []),
        meta=dict(d.get("meta") or {}),
    )


def _record_to_dict(r: ConcernRecord) -> dict[str, Any]:
    return asdict(r)


def _reconcile_one(rec: ConcernRecord, ev: dict[str, Any]) -> tuple[str, str, bool]:
    """Return new_status, reason, should_surface_now."""
    now = now_ts()
    if rec.status in (STATUS_RESOLVED, STATUS_ARCHIVED):
        if now < float(rec.cooldown_until or 0) and rec.status == STATUS_RESOLVED:
            return STATUS_RESOLVED, "cooldown_quiet", False
        if rec.status == STATUS_ARCHIVED:
            return STATUS_ARCHIVED, "archived_hold", False

    src = str(ev.get("source") or "")
    vt = ev.get("vision_trusted")
    af = str(ev.get("acquisition_freshness") or "")
    ident = str(ev.get("identity_state") or "")
    st_ov = str(ev.get("selftest_overall") or "ok")
    failed = list(ev.get("failed_checks") or [])
    wb = bool(ev.get("workbench_has_proposal"))
    ilp_a = bool(ev.get("improvement_loop_active"))

    # User dismissed recently should immediately resolve and stay quiet.
    udis_map = ev.get("user_dismissed_ts")
    if isinstance(udis_map, dict):
        ts = float(udis_map.get(rec.concern_type) or 0.0)
        if ts > 0 and now - ts <= _USER_DISMISS_COOLDOWN_SEC:
            return STATUS_RESOLVED, "user_dismissed_recently", False

    # Current good state overrides stale worry (prefer present evidence).
    good_vision = bool(vt is True and af in ("fresh", "aging"))
    cam_age = ev.get("camera_age_sec")
    try:
        cam_age_f = float(cam_age) if cam_age is not None else None
    except Exception:
        cam_age_f = None
    rec_conf = float(ev.get("recognition_confidence") or 0.0)

    if rec.concern_type == CT_CAMERA_STALE:
        # Rule B:
        # - fresh (<30s) + confidence >40% => resolved
        # - moderate confidence (40-70%) is normal, not a camera problem
        # - only active concern when: no frame, stale >60s, or confidence <25%
        if cam_age_f is not None and cam_age_f < _CAMERA_FRESH_RESOLVE_SEC and rec_conf >= _CAMERA_CONF_GOOD:
            return STATUS_RESOLVED, "camera_fresh_and_confident", False
        if cam_age_f is None:
            return STATUS_ACTIVE, "no_frame_available", True
        if cam_age_f > _CAMERA_STALE_FLAG_SEC:
            return STATUS_ACTIVE, "frame_stale", True
        if rec_conf < _CAMERA_CONF_LOW:
            return STATUS_ACTIVE, "recognition_confidence_low", True
        if rec_conf >= _CAMERA_CONF_GOOD:
            return STATUS_RESOLVED, "camera_operating_normally", False
        return STATUS_RESOLVED, "moderate_confidence_normal_operation", False

        if src == "startup":
            if bool(ev.get("camera_recent_ok")) and str(ev.get("health_degraded_mode") or "none") in (
                "none",
                "",
            ):
                return STATUS_RESOLVED, "camera_recent_ok_startup_evidence", False
        else:
            if good_vision and str(ev.get("quality_label") or "") not in ("unreliable",):
                return STATUS_RESOLVED, "vision_trusted_and_acquisition_acceptable", False
            if good_vision:
                return STATUS_WEAKENED, "vision_ok_quality_marginal", False
        return rec.status, "no_resolution_yet", False

    if rec.concern_type == CT_RECOGNITION_UNCERTAIN:
        if ident == "confirmed_recognition":
            return STATUS_RESOLVED, "identity_confirmed_current_tick", False
        if ident in ("likely_identity_by_continuity", "unknown_face") and good_vision:
            return STATUS_WEAKENED, "identity_uncertain_but_vision_ok", False
        return rec.status, "identity_still_uncertain", False

    if rec.concern_type == CT_MAINTENANCE_WORKBENCH:
        if st_ov == "ok" and not failed and not wb and not ilp_a:
            return STATUS_RESOLVED, "maintenance_signals_clear", False
        if st_ov == "ok" and not wb:
            return STATUS_WEAKENED, "tests_ok_proposals_clear", False
        return rec.status, "maintenance_still_relevant", False

    if rec.concern_type == CT_RECURRING_WARNING:
        if st_ov == "ok" and not failed:
            return STATUS_RESOLVED, "warnings_cleared", False
        if failed:
            return STATUS_ACTIVE, "warnings_still_present", False
        return STATUS_WEAKENED, "warning_context_changed", False

    return rec.status, "no_rule", False


def _append_failed_selftest_concern(out_rows: list[dict[str, Any]], ev: dict[str, Any]) -> None:
    if str(ev.get("source") or "") != "runtime":
        return
    st_ov = str(ev.get("selftest_overall") or "")
    failed = list(ev.get("failed_checks") or [])
    if st_ov != "failed" and not failed:
        return
    for r in out_rows:
        if str(r.get("concern_type")) != CT_RECURRING_WARNING:
            continue
        if str(r.get("status")) in (STATUS_ACTIVE, STATUS_WEAKENED):
            return
    cid = f"selftest_{uuid.uuid4().hex[:10]}"
    rec = ConcernRecord(
        concern_id=cid,
        concern_type=CT_RECURRING_WARNING,
        status=STATUS_ACTIVE,
        created_at=now_ts(),
        last_seen_at=now_ts(),
        supporting_evidence={"failed_checks": failed[:8], "overall": st_ov},
        current_evidence=dict(ev),
        should_surface_now=False,
        meta={"auto_created": True},
    )
    out_rows.append(_record_to_dict(rec))


def reconcile_evidence(ev: dict[str, Any], host_or_g: Optional[dict[str, Any]] = None) -> ConcernReconciliationResult:
    rows, _registry_meta = _registry_load()
    prior_count = len(rows)

    updates: list[ConcernStatusUpdate] = []
    now = now_ts()
    out_rows: list[dict[str, Any]] = []

    for raw in rows:
        rec = _record_from_dict(raw)
        if not rec.concern_id:
            continue
        if rec.status == STATUS_ARCHIVED:
            rec.last_seen_at = now
            rec.current_evidence = dict(ev)
            out_rows.append(_record_to_dict(rec))
            continue

        if float(rec.cooldown_until or 0) > now and rec.status == STATUS_RESOLVED:
            rec.last_seen_at = now
            rec.current_evidence = dict(ev)
            out_rows.append(_record_to_dict(rec))
            continue

        prior = rec.status
        new_st, reason, surface = _reconcile_one(rec, ev)
        rec.current_evidence = dict(ev)
        rec.last_seen_at = now

        severe = (
            len(ev.get("failed_checks") or []) > 0
            or str(ev.get("selftest_overall") or "") == "failed"
            or (ev.get("vision_trusted") is False and str(ev.get("acquisition_freshness")) == "unavailable")
        )
        surface_ok = surface and severe and new_st in (STATUS_ACTIVE, STATUS_WEAKENED)

        if new_st != prior:
            rec.status = new_st
            rec.resolution_reason = reason if new_st in (STATUS_RESOLVED, STATUS_WEAKENED, STATUS_STALE) else ""
            if new_st == STATUS_RESOLVED:
                rec.cooldown_until = now + _COOLDOWN_SEC
                rec.should_surface_now = False
            elif new_st == STATUS_STALE:
                rec.cooldown_until = now + _RESOLVED_QUIET_SEC
                rec.should_surface_now = False
            else:
                rec.should_surface_now = bool(surface_ok)

            updates.append(
                ConcernStatusUpdate(
                    concern_id=rec.concern_id,
                    concern_type=rec.concern_type,
                    prior_status=prior,
                    new_status=new_st,
                    resolution_reason=reason,
                    should_surface_now=bool(rec.should_surface_now),
                    meta={"evidence_source": ev.get("source")},
                )
            )
        else:
            rec.should_surface_now = bool(surface_ok and prior == STATUS_ACTIVE)

        if rec.status == STATUS_RESOLVED and (now - float(rec.created_at or now)) > 86400 * 14:
            rec.status = STATUS_ARCHIVED
            rec.meta.setdefault("auto_archive", True)

        out_rows.append(_record_to_dict(rec))

    _append_failed_selftest_concern(out_rows, ev)

    active = [r for r in out_rows if str(r.get("status")) in (STATUS_ACTIVE, STATUS_WEAKENED)]
    active_count = len(active)
    top_id = str(active[0].get("concern_id") or "") if active else ""

    summary = _trunc(
        f"active={active_count} updates={len(updates)} src={ev.get('source')}",
        220,
    )

    meta_out = {
        "registry_path": str(_REGISTRY_PATH),
        "evidence_source": ev.get("source"),
        "prefer_current_evidence": True,
        "last_reconcile_ts": now_ts(),
    }
    _registry_save(out_rows, meta_out)

    surfaced = bool(any(u.should_surface_now for u in updates))

    if updates:
        r0 = updates[0].resolution_reason or "transition"
        print(
            f"[concern_reconciliation] prior={prior_count} new={len(updates)} "
            f"reason={_trunc(str(r0), 120)}"
        )
    if updates or active_count > 0:
        print(f"[concern_reconciliation] surfaced={int(surfaced)} active_count={active_count}")

    return ConcernReconciliationResult(
        updates=updates,
        active_count=active_count,
        top_active_concern=top_id,
        summary=summary,
        surfaced_any=surfaced,
        meta=meta_out,
    )


def mark_concern_user_dismissed(concern_type: str, g: dict[str, Any] | None = None) -> None:
    """
    Immediately resolve concern type due to explicit user dismissal and add 1h cooldown.
    """
    ctype = str(concern_type or "").strip()
    if not ctype:
        return
    now = now_ts()
    rows, meta = _registry_load()
    changed = False
    for raw in rows:
        if str(raw.get("concern_type") or "") != ctype:
            continue
        raw["status"] = STATUS_RESOLVED
        raw["resolution_reason"] = "user_dismissed"
        raw["cooldown_until"] = now + _USER_DISMISS_COOLDOWN_SEC
        raw["should_surface_now"] = False
        raw["last_seen_at"] = now
        n = list(raw.get("notes") or [])
        n.append("user_dismissed_recently")
        raw["notes"] = n[-12:]
        changed = True
    if changed:
        _registry_save(rows, {**meta, "last_user_dismissal_ts": now})
    if isinstance(g, dict):
        m = g.get("_concern_user_dismissed_ts")
        if not isinstance(m, dict):
            m = {}
        m[ctype] = now
        g["_concern_user_dismissed_ts"] = m


def concern_surface_gate(concern_type: str, g: dict[str, Any] | None = None) -> bool:
    """
    True only when concern is genuinely active/current and not user-dismissed/cooldowned.
    """
    ctype = str(concern_type or "").strip()
    if not ctype:
        return False
    now = now_ts()
    rows, _meta = _registry_load()
    matches = [r for r in rows if str(r.get("concern_type") or "") == ctype]
    if not matches:
        return False
    rec = sorted(matches, key=lambda r: float(r.get("last_seen_at") or 0.0), reverse=True)[0]
    status = str(rec.get("status") or "")
    if status in (STATUS_RESOLVED, STATUS_WEAKENED, STATUS_ARCHIVED, STATUS_STALE):
        return False
    if float(rec.get("cooldown_until") or 0.0) > now:
        return False
    if isinstance(g, dict):
        d = g.get("_concern_user_dismissed_ts")
        if isinstance(d, dict):
            ts = float(d.get(ctype) or 0.0)
            if ts > 0 and now - ts <= _USER_DISMISS_COOLDOWN_SEC:
                return False
    return status == STATUS_ACTIVE


def run_startup_concern_reconciliation(host: dict[str, Any]) -> ConcernReconciliationResult:
    try:
        ev = gather_startup_evidence(host)
        return reconcile_evidence(ev, host_or_g=host)
    except Exception as e:
        print(f"[concern_reconciliation] startup failed: {e}\n{traceback.format_exc()}")
        return ConcernReconciliationResult(summary="startup reconciliation skipped", meta={"error": str(e)})


def run_runtime_concern_reconciliation_safe(
    *,
    g: dict[str, Any],
    trusted: bool,
    acquisition_freshness: str,
    identity_resolution: Optional[IdentityResolutionResult],
    quality: Optional[QualityOutput],
    selftests: Optional[SelfTestRunResult],
    workbench: Optional[WorkbenchProposalResult],
    improvement_loop: Optional[ImprovementLoopResult],
    heartbeat: Optional[HeartbeatTickResult],
    runtime_presence: Optional[RuntimePresenceResult],
) -> ConcernReconciliationResult:
    try:
        ev = gather_runtime_evidence(
            trusted=trusted,
            acquisition_freshness=acquisition_freshness,
            id_res=identity_resolution,
            qual=quality,
            selftests=selftests,
            workbench=workbench,
            improvement_loop=improvement_loop,
            heartbeat=heartbeat,
            runtime_presence=runtime_presence,
            host_or_g=g if isinstance(g, dict) else None,
        )
        return reconcile_evidence(ev, host_or_g=g if isinstance(g, dict) else None)
    except Exception as e:
        print(f"[concern_reconciliation] runtime failed: {e}\n{traceback.format_exc()}")
        return ConcernReconciliationResult(summary="runtime reconciliation skipped", meta={"error": str(e)})


def apply_concern_reconciliation_to_perception_state(state: Any, bundle: Any) -> None:
    cr = getattr(bundle, "concern_reconciliation", None)
    if cr is None:
        state.active_concern_count = 0
        state.top_active_concern = ""
        state.concern_reconciliation_summary = ""
        state.concern_reconciliation_meta = {"idle": True}
        return
    state.active_concern_count = int(cr.active_count or 0)
    state.top_active_concern = str(cr.top_active_concern or "")[:200]
    state.concern_reconciliation_summary = str(cr.summary or "")[:500]
    m = dict(cr.meta or {})
    m["surfaced_any"] = bool(cr.surfaced_any)
    m["updates"] = [
        {
            "id": u.concern_id,
            "type": u.concern_type,
            "from": u.prior_status,
            "to": u.new_status,
            "reason": u.resolution_reason[:160],
        }
        for u in list(cr.updates or [])[:12]
    ]
    state.concern_reconciliation_meta = m
