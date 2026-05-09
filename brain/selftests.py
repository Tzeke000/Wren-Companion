"""
Phase 15 — startup and recurring self-tests (diagnostics only).

Lightweight, deterministic, non-destructive checks that classify runtime health.
No automatic repair actions are performed in this phase.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from config.ava_tuning import SELFTEST_CONFIG

from .perception_types import (
    HealthSummaryResult,
    SelfTestCheckResult,
    SelfTestRunResult,
)

_startup_done: bool = False
_last_recurring_ts: float = 0.0
_last_recurring_result: SelfTestRunResult | None = None
_RECURRING_INTERVAL_SEC = SELFTEST_CONFIG.recurring_interval_sec


def _now() -> float:
    return time.time()


def _check(
    name: str,
    status: str,
    severity: str,
    message: str,
    details: dict[str, Any] | None = None,
    recommended: str = "",
) -> SelfTestCheckResult:
    st = status if status in ("ok", "warning", "failed", "skipped") else "skipped"
    sev = severity if severity in ("info", "warning", "critical") else "info"
    return SelfTestCheckResult(
        check_name=name,
        status=st,
        severity=sev,
        passed=st == "ok",
        message=message,
        details=dict(details or {}),
        timestamp=_now(),
        recommended_next_step=recommended,
    )


def _summarize(checks: list[SelfTestCheckResult]) -> HealthSummaryResult:
    failed = [c.check_name for c in checks if c.status == "failed"]
    warning = [c.check_name for c in checks if c.status == "warning"]
    passed = [c.check_name for c in checks if c.status == "ok"]
    skipped = [c.check_name for c in checks if c.status == "skipped"]
    if failed:
        overall_status = "failed"
        overall_severity = "critical"
    elif warning:
        overall_status = "warning"
        overall_severity = "warning"
    else:
        overall_status = "ok"
        overall_severity = "info"
    msg = (
        f"selftests {overall_status}: failed={len(failed)} warning={len(warning)} "
        f"ok={len(passed)} skipped={len(skipped)}"
    )
    return HealthSummaryResult(
        overall_status=overall_status,
        overall_severity=overall_severity,
        failed_checks=failed,
        warning_checks=warning,
        passed_checks=passed,
        skipped_checks=skipped,
        message=msg,
        meta={"check_count": len(checks)},
    )


def _check_camera_module() -> SelfTestCheckResult:
    try:
        from .camera import CameraManager  # noqa: F401

        return _check("camera_module_available", "ok", "info", "camera module importable")
    except Exception as e:
        return _check(
            "camera_module_available",
            "failed",
            "critical",
            f"camera module import failed: {e}",
            recommended="verify OpenCV/camera dependencies",
        )


def _check_camera_read_callable(camera_manager: Any) -> SelfTestCheckResult:
    ok = bool(camera_manager is not None and callable(getattr(camera_manager, "resolve_frame_detailed", None)))
    if ok:
        return _check(
            "camera_read_path_callable",
            "ok",
            "info",
            "resolve_frame_detailed callable",
        )
    return _check(
        "camera_read_path_callable",
        "failed",
        "critical",
        "camera manager missing or resolve_frame_detailed not callable",
        recommended="initialize CameraManager before perception ticks",
    )


def _check_acquisition_freshness_path() -> SelfTestCheckResult:
    try:
        from .frame_store import classify_acquisition_freshness

        sample = classify_acquisition_freshness(True, 0.1)
        return _check(
            "acquisition_freshness_path_callable",
            "ok",
            "info",
            "acquisition freshness classifier callable",
            details={"sample_result": str(sample)},
        )
    except Exception as e:
        return _check(
            "acquisition_freshness_path_callable",
            "failed",
            "critical",
            f"freshness classifier unavailable: {e}",
            recommended="verify frame_store module and imports",
        )


def _check_perception_pipeline_callable() -> SelfTestCheckResult:
    try:
        from . import perception_pipeline

        fn = getattr(perception_pipeline, "run_perception_pipeline", None)
        if callable(fn):
            return _check("perception_pipeline_callable", "ok", "info", "pipeline callable")
        return _check(
            "perception_pipeline_callable",
            "failed",
            "critical",
            "run_perception_pipeline missing",
            recommended="restore perception pipeline entrypoint",
        )
    except Exception as e:
        return _check(
            "perception_pipeline_callable",
            "failed",
            "critical",
            f"pipeline import failed: {e}",
            recommended="fix pipeline import errors",
        )


def _check_key_paths() -> list[SelfTestCheckResult]:
    checks: list[SelfTestCheckResult] = []
    root = Path(__file__).resolve().parents[1]
    required_dirs = ["brain", "state", "memory", "profiles", "docs"]
    required_files = [
        "avaagent.py",
        "ava_core/IDENTITY.md",
        "ava_core/SOUL.md",
        "ava_core/USER.md",
    ]
    for rel in required_dirs:
        p = root / rel
        if p.exists() and p.is_dir():
            checks.append(_check(f"dir_exists:{rel}", "ok", "info", "directory present"))
        else:
            checks.append(
                _check(
                    f"dir_exists:{rel}",
                    "failed",
                    "critical",
                    "required directory missing",
                    details={"path": str(p)},
                    recommended=f"restore directory `{rel}`",
                )
            )
    for rel in required_files:
        p = root / rel
        if p.exists():
            checks.append(_check(f"file_exists:{rel}", "ok", "info", "file present"))
        else:
            checks.append(
                _check(
                    f"file_exists:{rel}",
                    "warning",
                    "warning",
                    "expected file missing",
                    details={"path": str(p)},
                    recommended=f"restore or regenerate `{rel}`",
                )
            )
    return checks


def _check_memory_path() -> SelfTestCheckResult:
    root = Path(__file__).resolve().parents[1]
    db_path = root / "memory" / "chroma.sqlite3"
    if db_path.exists():
        return _check(
            "memory_store_path_exists",
            "ok",
            "info",
            "memory sqlite path present",
            details={"path": str(db_path)},
        )
    return _check(
        "memory_store_path_exists",
        "warning",
        "warning",
        "memory sqlite path not found",
        details={"path": str(db_path)},
        recommended="initialize memory store if memory features are expected",
    )


def _check_audio_path(g: dict) -> SelfTestCheckResult:
    has_whisper = "whisper_model" in g
    transcribe_fn = g.get("transcribe_audio")
    if has_whisper and callable(transcribe_fn):
        return _check(
            "audio_input_path_available",
            "ok",
            "info",
            "audio transcription path available",
        )
    if callable(transcribe_fn):
        return _check(
            "audio_input_path_available",
            "warning",
            "warning",
            "transcribe path callable but whisper model presence unclear",
            recommended="verify whisper model initialization at startup",
        )
    return _check(
        "audio_input_path_available",
        "skipped",
        "info",
        "audio path not exposed in this runtime context",
    )


def _check_model_readiness(g: dict) -> SelfTestCheckResult:
    # Lightweight only: detect whether model/provider hooks exist.
    candidates = ("chat_with_memory", "run_ava", "transcribe_audio")
    present = [name for name in candidates if callable(g.get(name))]
    if present:
        return _check(
            "model_provider_hooks_exposed",
            "ok",
            "info",
            "model/provider hooks detected",
            details={"hooks": present},
        )
    return _check(
        "model_provider_hooks_exposed",
        "warning",
        "warning",
        "no obvious model/provider hooks exposed in globals",
        recommended="verify runtime globals initialization",
    )


def _check_tick_readiness(g: dict) -> SelfTestCheckResult:
    tick_hooks = [
        ("camera_tick_fn", callable(g.get("camera_tick_fn"))),
        ("chat_fn", callable(g.get("chat_fn"))),
        ("voice_fn", callable(g.get("voice_fn"))),
    ]
    present = [name for name, ok in tick_hooks if ok]
    if present:
        return _check(
            "recurring_tick_readiness",
            "ok",
            "info",
            "runtime tick hooks available",
            details={"hooks": present},
        )
    return _check(
        "recurring_tick_readiness",
        "skipped",
        "info",
        "tick hooks not available in current context",
    )


def run_startup_selftests(camera_manager: Any, g: dict) -> SelfTestRunResult:
    """Run one-time startup self-tests."""
    checks: list[SelfTestCheckResult] = []
    checks.append(_check_camera_module())
    checks.append(_check_camera_read_callable(camera_manager))
    checks.append(_check_acquisition_freshness_path())
    checks.append(_check_perception_pipeline_callable())
    checks.extend(_check_key_paths())
    checks.append(_check_memory_path())
    checks.append(_check_audio_path(g))
    checks.append(_check_model_readiness(g))
    checks.append(_check_tick_readiness(g))
    summary = _summarize(checks)
    print(f"[selftests] run=startup overall={summary.overall_status}")
    for c in checks[:8]:
        print(f"[selftests] check={c.check_name} status={c.status} severity={c.severity}")
    print(
        f"[selftests] summary failed={len(summary.failed_checks)} warning={len(summary.warning_checks)}"
    )
    return SelfTestRunResult(
        run_type="startup",
        checks=checks,
        summary=summary,
        timestamp=_now(),
        meta={"lightweight": True, "non_destructive": True},
    )


def run_recurring_selftests(camera_manager: Any, g: dict, acquisition_freshness: str) -> SelfTestRunResult:
    """Run recurring self-tests (lightweight cadence checks)."""
    checks: list[SelfTestCheckResult] = []
    checks.append(_check_camera_read_callable(camera_manager))
    checks.append(_check_perception_pipeline_callable())
    checks.append(_check_audio_path(g))
    checks.append(_check_model_readiness(g))
    checks.append(_check_tick_readiness(g))
    if acquisition_freshness in ("fresh", "aging"):
        checks.append(
            _check(
                "acquisition_freshness_runtime",
                "ok",
                "info",
                "acquisition freshness acceptable",
                details={"acquisition_freshness": acquisition_freshness},
            )
        )
    elif acquisition_freshness == "stale":
        checks.append(
            _check(
                "acquisition_freshness_runtime",
                "warning",
                "warning",
                "acquisition freshness stale",
                details={"acquisition_freshness": acquisition_freshness},
                recommended="check camera feed recency and UI/live source timing",
            )
        )
    else:
        checks.append(
            _check(
                "acquisition_freshness_runtime",
                "warning",
                "warning",
                "acquisition freshness unavailable",
                details={"acquisition_freshness": acquisition_freshness},
                recommended="verify camera path and frame source",
            )
        )
    summary = _summarize(checks)
    print(f"[selftests] run=recurring overall={summary.overall_status}")
    print(
        f"[selftests] summary failed={len(summary.failed_checks)} warning={len(summary.warning_checks)}"
    )
    return SelfTestRunResult(
        run_type="recurring",
        checks=checks,
        summary=summary,
        timestamp=_now(),
        meta={"lightweight": True, "non_destructive": True},
    )


def maybe_run_selftests(
    *,
    camera_manager: Any,
    g: dict,
    acquisition_freshness: str,
) -> SelfTestRunResult:
    """
    Run startup self-tests once, then recurring self-tests on cadence.
    Returns latest result with safe defaults.
    """
    global _startup_done, _last_recurring_ts, _last_recurring_result
    now = _now()
    if not _startup_done:
        _startup_done = True
        _last_recurring_result = run_startup_selftests(camera_manager, g)
        _last_recurring_ts = now
        return _last_recurring_result
    if (now - _last_recurring_ts) >= _RECURRING_INTERVAL_SEC or _last_recurring_result is None:
        _last_recurring_result = run_recurring_selftests(
            camera_manager,
            g,
            acquisition_freshness=acquisition_freshness,
        )
        _last_recurring_ts = now
        return _last_recurring_result
    return SelfTestRunResult(
        run_type="recurring",
        checks=[],
        summary=HealthSummaryResult(
            overall_status=(_last_recurring_result.summary.overall_status if _last_recurring_result else "ok"),
            overall_severity=(_last_recurring_result.summary.overall_severity if _last_recurring_result else "info"),
            message="recurring selftest cadence not due",
        ),
        timestamp=now,
        meta={"cadence_skipped": True, "seconds_until_next": max(0.0, _RECURRING_INTERVAL_SEC - (now - _last_recurring_ts))},
    )
