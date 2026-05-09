"""
Phase 21 — real-world calibration signals (descriptive only).

Accumulates lightweight counters from each perception pipeline tick and derives
rates + a human-facing watchlist. **Does not modify thresholds or auto-retune.**

Integration: :func:`record_calibration_tick` runs at the end of
:func:`brain.perception_pipeline.run_perception_pipeline`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .perception_types import (
    CalibrationObservation,
    CalibrationReport,
    PerceptionPipelineBundle,
    ThresholdReviewResult,
)

# Periodic summary log (avoid per-tick noise)
_SUMMARY_EVERY_TICKS = 96

# Minimum samples before ratio-based watch rules fire
_MIN_IDENTITY_FACE_TICKS = 18
_MIN_PROACTIVE_ELIGIBLE_TICKS = 26
_MIN_CONTINUITY_TICKS = 14
_MIN_MEMORY_EVENT_TICKS = 12

# Watch thresholds (guidance only — tune in config after evidence review)
_UNKNOWN_FACE_RATE_ATTENTION = 0.52
_CONTINUITY_SHARE_HIGH = 0.72
_CONTINUITY_SHARE_LOW = 0.06
_PROACTIVE_SUPPRESS_DOMINANCE = 0.88
_DUPLICATE_SUPPRESS_DOMINANCE = 0.82
_LOW_QUALITY_TRUSTED_RATE = 0.45
_REFLECTION_UNCERTAIN_RATE = 0.55


@dataclass
class _SessionAccum:
    """Mutable session counters (reset via :func:`reset_calibration_state`)."""

    tick_count: int = 0
    trusted_ticks: int = 0
    trusted_low_quality_ticks: int = 0
    blur_penalized_ticks: int = 0
    face_trusted_ticks: int = 0
    unknown_face_ticks: int = 0
    likely_continuity_ticks: int = 0
    confirmed_recognition_ticks: int = 0
    identity_fallback_continuity_ticks: int = 0
    no_meaningful_change_ticks: int = 0
    memory_duplicate_suppressed_ticks: int = 0
    memory_event_ticks: int = 0
    proactive_eligible_ticks: int = 0
    proactive_suppressed_ticks: int = 0
    proactive_fired_ticks: int = 0
    workbench_proposal_ticks: int = 0
    selftest_warning_ticks: int = 0
    selftest_failed_ticks: int = 0
    reflection_uncertain_ticks: int = 0
    reflection_degraded_ticks: int = 0


_acc = _SessionAccum()
_last_report: CalibrationReport | None = None
_logged_watch_once: set[str] = set()

# Phase 22 — lightweight voice turn-taking hints (counts only)
_voice_calibration_hints: dict[str, int] = {
    "interrupt_signals": 0,
    "low_readiness": 0,
    "wait_bias": 0,
}


def record_voice_calibration_hint(*, interrupt: bool, readiness: float, wait: bool) -> None:
    """Increment hint counters for later calibration review (no behavioral side effects)."""
    global _voice_calibration_hints
    if interrupt:
        _voice_calibration_hints["interrupt_signals"] += 1
    if readiness < 0.38:
        _voice_calibration_hints["low_readiness"] += 1
    if wait:
        _voice_calibration_hints["wait_bias"] += 1


def reset_calibration_state() -> None:
    """Clear session counters (tests / long-running session boundaries)."""
    global _acc, _last_report, _logged_watch_once, _voice_calibration_hints
    _acc = _SessionAccum()
    _last_report = None
    _logged_watch_once.clear()
    _voice_calibration_hints = {"interrupt_signals": 0, "low_readiness": 0, "wait_bias": 0}


def _safe_ratio(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return round(float(num) / float(den), 4)


def _quality_label(bundle: PerceptionPipelineBundle) -> str:
    sq = bundle.quality.structured
    if sq is None:
        return ""
    return str(getattr(sq, "quality_label", "") or "")


def _blur_label(bundle: PerceptionPipelineBundle) -> str:
    sq = bundle.quality.structured
    if sq is not None and getattr(sq, "blur_label", None):
        return str(sq.blur_label)
    return str(bundle.quality.blur_label or "sharp")


def record_calibration_tick(bundle: PerceptionPipelineBundle) -> None:
    """Update session calibration counters from one pipeline bundle (no side effects on behavior)."""
    global _acc
    _acc.tick_count += 1
    resolved = bundle.resolved
    trusted = bool(
        resolved is not None and bool(getattr(resolved, "visual_truth_trusted", False))
    )
    if trusted:
        _acc.trusted_ticks += 1
        ql = _quality_label(bundle)
        if ql in ("weak", "unreliable"):
            _acc.trusted_low_quality_ticks += 1
        bl = _blur_label(bundle)
        if bl != "sharp":
            _acc.blur_penalized_ticks += 1

    det = bundle.detection
    face_ok = bool(det.face_detected) and trusted
    if face_ok:
        _acc.face_trusted_ticks += 1

    id_res = bundle.identity_resolution
    if id_res is not None:
        st = id_res.identity_state
        if st == "unknown_face":
            _acc.unknown_face_ticks += 1
        if st == "likely_identity_by_continuity":
            _acc.likely_continuity_ticks += 1
        if st == "confirmed_recognition":
            _acc.confirmed_recognition_ticks += 1
        if id_res.fallback_source == "continuity":
            _acc.identity_fallback_continuity_ticks += 1

    il = bundle.interpretation_layer
    if il is not None and il.no_meaningful_change:
        _acc.no_meaningful_change_ticks += 1

    pm = bundle.perception_memory
    if pm is not None and pm.event is not None:
        _acc.memory_event_ticks += 1
        if pm.event.suppressed_duplicate:
            _acc.memory_duplicate_suppressed_ticks += 1

    pt = bundle.proactive_trigger
    if pt is not None:
        eligible = bool(pt.candidates) or float(pt.trigger_score or 0.0) >= 0.08
        if eligible:
            _acc.proactive_eligible_ticks += 1
            if (not pt.should_trigger) and (pt.suppression_reason or "").strip():
                _acc.proactive_suppressed_ticks += 1
        if pt.should_trigger:
            _acc.proactive_fired_ticks += 1

    wb = bundle.workbench
    if wb is not None and wb.has_proposal:
        _acc.workbench_proposal_ticks += 1

    st = bundle.selftests
    if st is not None:
        summ = st.summary
        if summ.warning_checks:
            _acc.selftest_warning_ticks += 1
        if summ.failed_checks:
            _acc.selftest_failed_ticks += 1

    rf = bundle.reflection
    if rf is not None:
        if rf.reflection_category == "uncertain_state_reflection":
            _acc.reflection_uncertain_ticks += 1
        if rf.reflection_category in (
            "failed_operation_reflection",
            "degraded_operation_reflection",
            "repeated_warning_reflection",
        ):
            _acc.reflection_degraded_ticks += 1

    _maybe_log_summary()


def _build_rates() -> dict[str, float]:
    a = _acc
    tc = max(1, a.tick_count)
    out: dict[str, float] = {
        "trusted_rate": _safe_ratio(a.trusted_ticks, tc),
        "trusted_low_quality_rate": _safe_ratio(a.trusted_low_quality_ticks, max(1, a.trusted_ticks)),
        "blur_penalized_while_trusted_rate": _safe_ratio(a.blur_penalized_ticks, max(1, a.trusted_ticks)),
        "unknown_face_among_face_trusted": _safe_ratio(a.unknown_face_ticks, max(1, a.face_trusted_ticks)),
        "likely_continuity_among_face_trusted": _safe_ratio(
            a.likely_continuity_ticks, max(1, a.face_trusted_ticks)
        ),
        "confirmed_recognition_among_face_trusted": _safe_ratio(
            a.confirmed_recognition_ticks, max(1, a.face_trusted_ticks)
        ),
        "continuity_fallback_rate": _safe_ratio(a.identity_fallback_continuity_ticks, max(1, a.trusted_ticks)),
        "no_meaningful_change_rate": _safe_ratio(a.no_meaningful_change_ticks, tc),
        "memory_duplicate_suppressed_rate": _safe_ratio(
            a.memory_duplicate_suppressed_ticks, max(1, a.memory_event_ticks)
        ),
        "proactive_suppressed_among_eligible": _safe_ratio(
            a.proactive_suppressed_ticks, max(1, a.proactive_eligible_ticks)
        ),
        "proactive_fire_rate": _safe_ratio(a.proactive_fired_ticks, tc),
        "workbench_proposal_rate": _safe_ratio(a.workbench_proposal_ticks, tc),
        "selftest_warning_rate": _safe_ratio(a.selftest_warning_ticks, tc),
        "selftest_failure_rate": _safe_ratio(a.selftest_failed_ticks, tc),
        "reflection_uncertain_rate": _safe_ratio(a.reflection_uncertain_ticks, tc),
        "reflection_degraded_rate": _safe_ratio(a.reflection_degraded_ticks, tc),
    }
    decided = a.confirmed_recognition_ticks + a.likely_continuity_ticks + a.unknown_face_ticks
    out["continuity_share_of_decided"] = _safe_ratio(a.likely_continuity_ticks, max(1, decided))
    return out


def _build_watchlist(rates: dict[str, float], _counters: dict[str, int]) -> list[ThresholdReviewResult]:
    """Apply conservative watch rules; directions are advisory."""
    wl: list[ThresholdReviewResult] = []
    a = _acc
    decided_id = (
        a.confirmed_recognition_ticks + a.likely_continuity_ticks + a.unknown_face_ticks
    )

    if a.face_trusted_ticks >= _MIN_IDENTITY_FACE_TICKS:
        ufr = rates.get("unknown_face_among_face_trusted", 0.0)
        if ufr >= _UNKNOWN_FACE_RATE_ATTENTION:
            wl.append(
                ThresholdReviewResult(
                    area="identity_recognition",
                    current_signal=f"unknown_face_rate={ufr:.2f}",
                    suggested_direction="lower",
                    rationale=(
                        "High unknown_face rate among trusted face ticks — LBPH / confirmation "
                        "thresholds in config may be strict for this environment."
                    ),
                    evidence_count=a.face_trusted_ticks,
                    meta={"metric": "unknown_face_share", "threshold": _UNKNOWN_FACE_RATE_ATTENTION},
                )
            )

        cs = rates.get("continuity_share_of_decided", 0.0)
        if cs >= _CONTINUITY_SHARE_HIGH:
            wl.append(
                ThresholdReviewResult(
                    area="continuity_carry",
                    current_signal=f"continuity_share={cs:.2f}",
                    suggested_direction="watch",
                    rationale=(
                        "Continuity identity dominates recognition outcomes — spatial/time gates "
                        "may be aggressive; verify against false carries."
                    ),
                    evidence_count=decided_id,
                    meta={"metric": "likely_continuity_share", "threshold_high": _CONTINUITY_SHARE_HIGH},
                )
            )
        elif decided_id >= _MIN_CONTINUITY_TICKS and cs <= _CONTINUITY_SHARE_LOW:
            wl.append(
                ThresholdReviewResult(
                    area="continuity_carry",
                    current_signal=f"continuity_share={cs:.2f}",
                    suggested_direction="raise",
                    rationale=(
                        "Rare continuity carry vs recognition — spatial/time thresholds may be "
                        "too loose or recognition always winning."
                    ),
                    evidence_count=decided_id,
                    meta={"metric": "likely_continuity_share", "threshold_low": _CONTINUITY_SHARE_LOW},
                )
            )

    dup = rates.get("memory_duplicate_suppressed_rate", 0.0)
    if a.memory_event_ticks >= _MIN_MEMORY_EVENT_TICKS and dup >= _DUPLICATE_SUPPRESS_DOMINANCE:
        wl.append(
            ThresholdReviewResult(
                area="perception_memory_duplicate",
                current_signal=f"duplicate_suppressed_share={dup:.2f}",
                suggested_direction="watch",
                rationale=(
                    "Duplicate suppression dominates memory events — tune duplicate/noise guards "
                    "if meaningful changes are being dropped."
                ),
                evidence_count=a.memory_event_ticks,
                meta={"metric": "duplicate_suppressed_rate", "threshold": _DUPLICATE_SUPPRESS_DOMINANCE},
            )
        )

    if a.proactive_eligible_ticks >= _MIN_PROACTIVE_ELIGIBLE_TICKS:
        ps = rates.get("proactive_suppressed_among_eligible", 0.0)
        if ps >= _PROACTIVE_SUPPRESS_DOMINANCE:
            wl.append(
                ThresholdReviewResult(
                    area="proactive_triggers",
                    current_signal=f"suppressed_among_eligible={ps:.2f}",
                    suggested_direction="lower",
                    rationale=(
                        "Suppression dominates eligible proactive ticks — spam/repeat guards may "
                        "be overly conservative for this usage pattern."
                    ),
                    evidence_count=a.proactive_eligible_ticks,
                    meta={"metric": "proactive_suppress_share", "threshold": _PROACTIVE_SUPPRESS_DOMINANCE},
                )
            )

    lq = rates.get("trusted_low_quality_rate", 0.0)
    if a.trusted_ticks >= 24 and lq >= _LOW_QUALITY_TRUSTED_RATE:
        wl.append(
            ThresholdReviewResult(
                area="frame_quality",
                current_signal=f"weak_or_unreliable_while_trusted={lq:.2f}",
                suggested_direction="watch",
                rationale=(
                    "Many trusted frames labeled weak/unreliable — lighting/camera environment "
                    "or quality thresholds may need environment-side fixes or calibration review."
                ),
                evidence_count=a.trusted_ticks,
                meta={"metric": "trusted_low_quality_rate", "threshold": _LOW_QUALITY_TRUSTED_RATE},
            )
        )

    ru = rates.get("reflection_uncertain_rate", 0.0)
    if a.tick_count >= 40 and ru >= _REFLECTION_UNCERTAIN_RATE:
        wl.append(
            ThresholdReviewResult(
                area="reflection",
                current_signal=f"uncertain_category_rate={ru:.2f}",
                suggested_direction="watch",
                rationale=(
                    "Reflection often lands in uncertain_state — upstream signals may be ambiguous "
                    "or reflection thresholds may need alignment after other tuning."
                ),
                evidence_count=a.tick_count,
                meta={"metric": "reflection_uncertain_rate", "threshold": _REFLECTION_UNCERTAIN_RATE},
            )
        )

    return wl


def _acc_counters() -> dict[str, int]:
    return {k: int(v) for k, v in asdict(_acc).items()}


def _observation_status(rate: float, hi: float | None, lo: float | None) -> tuple[str, str]:
    """Map a rate to (status, suggested_direction) heuristics for reporting."""
    if hi is not None and rate >= hi:
        return "attention", "lower"
    if lo is not None and rate <= lo:
        return "attention", "raise"
    return "ok", "keep"


def _build_observations(rates: dict[str, float], counters: dict[str, int]) -> list[CalibrationObservation]:
    """Structured observations for programmatic consumers (non-authoritative)."""
    obs: list[CalibrationObservation] = []
    tc = counters.get("tick_count", 0)

    def add(sub: str, metric: str, val: float, status: str, direction: str, conf: float, n: int, note: str = "") -> None:
        obs.append(
            CalibrationObservation(
                subsystem_name=sub,
                metric_name=metric,
                observed_value=val,
                status=status,
                suggested_direction=direction,
                confidence=conf,
                evidence_count=n,
                notes=note,
            )
        )

    uf = rates.get("unknown_face_among_face_trusted", 0.0)
    ft = counters.get("face_trusted_ticks", 0)
    if ft < _MIN_IDENTITY_FACE_TICKS:
        st_uf, dir_uf = "insufficient_data", "watch"
    else:
        st_uf, dir_uf = _observation_status(uf, _UNKNOWN_FACE_RATE_ATTENTION, None)
    add(
        "identity",
        "unknown_face_share_face_trusted",
        uf,
        st_uf,
        dir_uf,
        min(1.0, ft / max(40, tc)),
        ft,
    )

    ps = rates.get("proactive_suppressed_among_eligible", 0.0)
    st_p, dir_p = _observation_status(ps, _PROACTIVE_SUPPRESS_DOMINANCE, None)
    add(
        "proactive",
        "suppress_share_eligible",
        ps,
        st_p if counters.get("proactive_eligible_ticks", 0) >= _MIN_PROACTIVE_ELIGIBLE_TICKS else "insufficient_data",
        dir_p if counters.get("proactive_eligible_ticks", 0) >= _MIN_PROACTIVE_ELIGIBLE_TICKS else "watch",
        min(1.0, counters.get("proactive_eligible_ticks", 0) / max(40, tc)),
        counters.get("proactive_eligible_ticks", 0),
    )

    dup = rates.get("memory_duplicate_suppressed_rate", 0.0)
    st_d, dir_d = _observation_status(dup, _DUPLICATE_SUPPRESS_DOMINANCE, None)
    add(
        "perception_memory",
        "duplicate_suppressed_share",
        dup,
        st_d if counters.get("memory_event_ticks", 0) >= _MIN_MEMORY_EVENT_TICKS else "insufficient_data",
        dir_d if counters.get("memory_event_ticks", 0) >= _MIN_MEMORY_EVENT_TICKS else "watch",
        min(1.0, counters.get("memory_event_ticks", 0) / max(24, tc)),
        counters.get("memory_event_ticks", 0),
    )

    add(
        "frame_quality",
        "blur_penalized_while_trusted_rate",
        rates.get("blur_penalized_while_trusted_rate", 0.0),
        "ok",
        "watch",
        0.55,
        counters.get("trusted_ticks", 0),
        "non-threshold blur usage signal",
    )

    add(
        "interpretation",
        "no_meaningful_change_rate",
        rates.get("no_meaningful_change_rate", 0.0),
        "ok",
        "watch",
        0.5,
        tc,
    )

    add(
        "reflection",
        "uncertain_category_rate",
        rates.get("reflection_uncertain_rate", 0.0),
        "watch" if tc >= 40 and rates.get("reflection_uncertain_rate", 0.0) >= _REFLECTION_UNCERTAIN_RATE else "ok",
        "watch",
        min(1.0, tc / max(80, tc)),
        tc,
    )

    add(
        "workbench",
        "has_proposal_rate",
        rates.get("workbench_proposal_rate", 0.0),
        "ok",
        "watch",
        0.45,
        tc,
    )

    add(
        "selftest",
        "warning_or_failure_tick_rate",
        max(rates.get("selftest_warning_rate", 0.0), rates.get("selftest_failure_rate", 0.0)),
        "ok",
        "watch",
        0.45,
        tc,
    )

    return obs


def summarize_calibration_state() -> CalibrationReport:
    """Return a snapshot of counters, derived rates, observations, and threshold watchlist."""
    rates = _build_rates()
    counters = _acc_counters()
    watchlist = _build_watchlist(rates, counters)
    observations = _build_observations(rates, counters)
    return CalibrationReport(
        tick_count=_acc.tick_count,
        rates=rates,
        observations=observations,
        watchlist=watchlist,
        counters=counters,
        notes=["phase_21_descriptive_only"],
        meta={
            "summary_interval_ticks": _SUMMARY_EVERY_TICKS,
            "voice_calibration_hints": dict(_voice_calibration_hints),
        },
    )


def get_last_calibration_report() -> CalibrationReport | None:
    """Last report from the most recent periodic summary (or None if not yet sampled)."""
    return _last_report


def _maybe_log_summary() -> None:
    global _last_report, _logged_watch_once
    if _acc.tick_count <= 0 or (_acc.tick_count % _SUMMARY_EVERY_TICKS) != 0:
        return
    rep = summarize_calibration_state()
    _last_report = rep
    r = rep.rates
    short = (
        f"ticks={rep.tick_count} uf={r.get('unknown_face_among_face_trusted', 0):.2f} "
        f"psup={r.get('proactive_suppressed_among_eligible', 0):.2f} "
        f"dup={r.get('memory_duplicate_suppressed_rate', 0):.2f} "
        f"rw={r.get('reflection_uncertain_rate', 0):.2f} watch={len(rep.watchlist)}"
    )
    print(f"[calibration] summary={short}")
    for w in rep.watchlist:
        key = f"{w.area}:{w.current_signal}"
        if key not in _logged_watch_once:
            _logged_watch_once.add(key)
            mk = str(w.meta.get("metric") or w.area)
            print(f"[calibration] subsystem={w.area} metric={mk} direction={w.suggested_direction}")
