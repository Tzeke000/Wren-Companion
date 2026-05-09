"""
Reply path routing: lightweight fast turns vs full deep context.

Fast path skips expensive prompt assembly and (when safe) skips a redundant workspace tick.
Does not bypass safety routes (self-state, camera identity, block/deflect).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .concern_reconciliation import concern_surface_gate

# Max age of last workspace refresh before forcing a fresh tick (seconds).
FAST_PATH_MAX_WS_AGE_SEC = 15.0


@dataclass
class ReplyComplexitySignal:
    """Heuristic complexity estimate for one user message."""

    complexity_score: float
    requires_deep_context: bool
    requires_maintenance_context: bool
    requires_vision_context: bool
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FastPathSnapshot:
    """Compact warm-state strings for fast-path prompts (no heavy subsystem rebuild)."""

    mood_summary: str
    camera_line: str
    runtime_presence_line: str
    concern_line: str
    relationship_hint: str
    perception_trusted: bool
    vision_status: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplyPathDecision:
    selected_path: Literal["fast", "deep"]
    reason: str
    complexity_signal: ReplyComplexitySignal
    complexity_score: float
    requires_deep_context: bool
    requires_maintenance_context: bool
    requires_vision_context: bool
    safe_to_use_cached_state: bool
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


_RE_FAST_ACK = re.compile(
    r"^(?:"
    r"hi\b|hello\b|hey\b|hiya\b|yo\b|sup\b|"
    r"ok\b|okay\b|k\b|sure\b|yep\b|yeah\b|yes\b|no\b|nope\b|nah\b|"
    r"thanks?\b|thank you\b|thx\b|ty\b|"
    r"good(?:\s+(?:morning|afternoon|evening|night))?\b|"
    r"bye\b|goodbye\b|later\b|gn\b"
    r")[\s\.\!\?]*$",
    re.I,
)

_RE_FAST_SOCIAL = re.compile(
    r"^[\s\,]*(how\s+are\s+you|how('?s| is)\s+it\s+going|what'?s\s+up|you\s+alright|"
    r"everything\s+ok|nice\s+to\s+(?:see|hear)\s+you|good\s+to\s+see\s+you)[\s\.\!\?]*$",
    re.I,
)

_RE_DEEP_MAINT = re.compile(
    r"\b(workbench|approval|patch|deploy|repair\s+proposal|self[- ]?improve|"
    r"maintenance\s+mode|execute\s+plan|file\s+change\s+plan)\b",
    re.I,
)

_RE_DEEP_VISION = re.compile(
    r"\b(camera|webcam|snapshot|frame|what\s+do\s+you\s+see|can\s+you\s+see|"
    r"recogni[sz]e\s+me|do\s+you\s+see\s+me|vision|facial\s+recognition)\b",
    re.I,
)

_RE_DEEP_TECH = re.compile(
    r"\b(debug|traceback|stack\s*trace|compile\s+error|exception|unit\s+test|"
    r"pytest|refactor|pull\s+request|github|api\s+key|stack\s+overflow)\b",
    re.I,
)

_RE_DEEP_META = re.compile(
    r"\b(roadmap|architecture|how\s+are\s+you\s+built|your\s+codebase|"
    r"self[- ]model|identity\s+file|system\s+prompt|reflection\s+log|"
    r"vector\s*store|chromadb|ollama\s+model)\b",
    re.I,
)

_RE_DEEP_AGENTIC = re.compile(
    r"\b(search\s+(?:the\s+)?(?:repo|code|files)|open\s+file|read\s+avaagent|"
    r"edit\s+|apply\s+patch|run\s+command)\b",
    re.I,
)


def classify_message_complexity(text: str, *, voice_active: bool = False) -> ReplyComplexitySignal:
    """Rule-based complexity; conservative bias toward deep when uncertain."""
    raw = (text or "").strip()
    notes: list[str] = []
    meta: dict[str, Any] = {}
    score = 0.12
    req_deep = False
    req_maint = False
    req_vis = False

    if not raw:
        return ReplyComplexitySignal(
            complexity_score=0.0,
            requires_deep_context=False,
            requires_maintenance_context=False,
            requires_vision_context=False,
            notes=["empty"],
            meta={"empty": True},
        )

    lowered = raw.lower()
    n = len(raw)
    word_count = len(raw.split())

    if _RE_DEEP_MAINT.search(raw):
        req_maint = True
        req_deep = True
        score = max(score, 0.82)
        notes.append("maintenance/workbench cue")

    if _RE_DEEP_VISION.search(raw):
        req_vis = True
        req_deep = True
        score = max(score, 0.78)
        notes.append("vision cue")

    if _RE_DEEP_TECH.search(raw):
        req_deep = True
        score = max(score, 0.74)
        notes.append("technical cue")

    if _RE_DEEP_META.search(raw):
        req_deep = True
        score = max(score, 0.76)
        notes.append("meta/self-model cue")

    if _RE_DEEP_AGENTIC.search(raw):
        req_deep = True
        score = max(score, 0.8)
        notes.append("agentic cue")

    if n > 260 or word_count > 42:
        req_deep = True
        score = max(score, 0.72)
        notes.append("long_message")

    if "?" in raw and n > 96:
        score += 0.18
        notes.append("question_mark_long")

    if voice_active:
        score += 0.06
        meta["voice_session"] = True
        notes.append("voice_session_bias")

    # Short social / ack — pull score down only if no deep triggers
    if not req_deep and not req_maint and not req_vis:
        if _RE_FAST_ACK.match(raw.strip()) or _RE_FAST_SOCIAL.match(raw.strip()):
            score = min(score, 0.28)
            notes.append("fast_ack_pattern")
        elif n <= 90 and word_count <= 10 and "?" not in raw:
            score = min(max(score, 0.22), 0.42)
            notes.append("short_low_question")

    score = max(0.0, min(1.0, score))
    req_deep = req_deep or score >= 0.62

    return ReplyComplexitySignal(
        complexity_score=score,
        requires_deep_context=req_deep,
        requires_maintenance_context=req_maint,
        requires_vision_context=req_vis,
        notes=notes,
        meta=meta,
    )


def decide_reply_path(
    signal: ReplyComplexitySignal,
    *,
    workspace_has_state: bool,
    ws_age_sec: float,
    voice_priority: bool = False,
    max_ws_age_sec: float = FAST_PATH_MAX_WS_AGE_SEC,
) -> ReplyPathDecision:
    """Choose fast vs deep; cached workspace is allowed only when fresh enough."""
    meta: dict[str, Any] = {"ws_age_sec": round(ws_age_sec, 3), "max_ws_age_sec": max_ws_age_sec}
    notes = list(signal.notes)

    if voice_priority:
        meta["voice_turn_priority"] = True
        return ReplyPathDecision(
            selected_path="deep",
            reason="voice_turn_priority",
            complexity_signal=signal,
            complexity_score=signal.complexity_score,
            requires_deep_context=True,
            requires_maintenance_context=signal.requires_maintenance_context,
            requires_vision_context=signal.requires_vision_context,
            safe_to_use_cached_state=False,
            notes=notes + ["voice_priority_deep"],
            meta=meta,
        )

    if signal.requires_maintenance_context or signal.requires_vision_context:
        return ReplyPathDecision(
            selected_path="deep",
            reason="subsystem_hint",
            complexity_signal=signal,
            complexity_score=signal.complexity_score,
            requires_deep_context=True,
            requires_maintenance_context=signal.requires_maintenance_context,
            requires_vision_context=signal.requires_vision_context,
            safe_to_use_cached_state=False,
            notes=notes,
            meta=meta,
        )

    if signal.requires_deep_context or signal.complexity_score >= 0.62:
        return ReplyPathDecision(
            selected_path="deep",
            reason="complexity_threshold",
            complexity_signal=signal,
            complexity_score=signal.complexity_score,
            requires_deep_context=True,
            requires_maintenance_context=signal.requires_maintenance_context,
            requires_vision_context=signal.requires_vision_context,
            safe_to_use_cached_state=False,
            notes=notes,
            meta=meta,
        )

    cache_ok = workspace_has_state and ws_age_sec <= max_ws_age_sec
    if not workspace_has_state:
        notes.append("no_workspace_cache")
    elif ws_age_sec > max_ws_age_sec:
        notes.append("workspace_stale")

    path: Literal["fast", "deep"] = "fast" if signal.complexity_score < 0.52 else "deep"
    reason = "fast_social_turn" if path == "fast" else "complexity_mid"

    if path == "fast":
        return ReplyPathDecision(
            selected_path="fast",
            reason=reason,
            complexity_signal=signal,
            complexity_score=signal.complexity_score,
            requires_deep_context=False,
            requires_maintenance_context=False,
            requires_vision_context=False,
            safe_to_use_cached_state=cache_ok,
            notes=notes,
            meta=meta,
        )

    return ReplyPathDecision(
        selected_path="deep",
        reason=reason,
        complexity_signal=signal,
        complexity_score=signal.complexity_score,
        requires_deep_context=True,
        requires_maintenance_context=False,
        requires_vision_context=False,
        safe_to_use_cached_state=False,
        notes=notes,
        meta=meta,
    )


def build_fast_path_snapshot(perception: Any | None, g: dict[str, Any]) -> FastPathSnapshot:
    """Warm-state only; reads small globals / perception fields — no vector search."""
    meta: dict[str, Any] = {}

    mood_summary = ""
    try:
        lm = g.get("load_mood")
        mt = g.get("mood_to_prompt_text")
        if callable(lm) and callable(mt):
            mood_summary = mt(lm())[:900]
        elif callable(lm):
            m = lm()
            mood_summary = str(m.get("current_mood", "steady"))[:400]
    except Exception as e:
        meta["mood_err"] = str(e)[:120]
        mood_summary = ""

    cam = "Vision not loaded."
    trusted = False
    vs = "unknown"
    if perception is not None:
        try:
            trusted = bool(getattr(perception, "visual_truth_trusted", False))
            vs = str(getattr(perception, "vision_status", "") or "")
            fs = str(getattr(perception, "face_status", "") or "")[:160]
            rt = str(getattr(perception, "recognized_text", "") or "")[:120]
            cam = f"trusted={trusted} vision={vs} face={fs} recognition={rt}"
        except Exception:
            cam = "Vision snapshot unavailable."

    rp_line = ""
    try:
        if perception is not None:
            rp_line = (
                f"presence={str(getattr(perception, 'runtime_presence_mode', '') or '')[:48]} "
                f"ready={str(getattr(perception, 'runtime_ready_state', '') or '')[:32]} "
                f"issue={str(getattr(perception, 'runtime_active_issue_summary', '') or '')[:140]}"
            ).strip()
    except Exception:
        rp_line = ""

    concern_line = ""
    try:
        if perception is not None:
            cc = int(getattr(perception, "active_concern_count", 0) or 0)
            top = str(getattr(perception, "top_active_concern", "") or "")[:120]
            crs = str(getattr(perception, "concern_reconciliation_summary", "") or "")[:200]
            cam_gate = concern_surface_gate("camera_stale", g) or concern_surface_gate("recognition_uncertain", g)
            maint_gate = concern_surface_gate("maintenance_workbench", g) or concern_surface_gate("recurring_warning", g)
            if cam_gate or maint_gate:
                concern_line = f"concerns_active={cc} top={top!r} recap={crs}"
    except Exception:
        concern_line = ""

    rel_hint = ""
    try:
        if perception is not None:
            rel_hint = str(getattr(perception, "relationship_summary", "") or "")[:240]
            if not rel_hint.strip():
                rel_hint = str(getattr(perception, "strategic_continuity_summary", "") or "")[:240]
    except Exception:
        rel_hint = ""

    return FastPathSnapshot(
        mood_summary=mood_summary,
        camera_line=cam,
        runtime_presence_line=rp_line,
        concern_line=concern_line,
        relationship_hint=rel_hint,
        perception_trusted=trusted,
        vision_status=vs,
        meta=meta,
    )


def attach_reply_path_globals(g: dict[str, Any], decision: ReplyPathDecision) -> None:
    """Expose last routing decision for UI / debugging (bounded strings)."""
    g["_reply_path_decision"] = decision
    g["reply_path_selected"] = decision.selected_path
    g["reply_path_reason"] = str(decision.reason or "")[:120]
    g["reply_path_meta"] = {
        "complexity": round(float(decision.complexity_score), 4),
        "cached_safe": bool(decision.safe_to_use_cached_state),
        "requires_deep": bool(decision.requires_deep_context),
        "notes": list(decision.notes)[:8],
        **dict(decision.meta or {}),
    }


def prepare_reply_path_for_turn(
    g: dict[str, Any],
    user_text: str,
    workspace: Any,
    *,
    voice_session: bool = False,
) -> ReplyPathDecision:
    """Classify message, decide path, attach globals, return decision."""
    voice_prio = bool(g.get("_voice_user_turn_priority"))
    sig = classify_message_complexity(user_text, voice_active=voice_session)
    ws = getattr(workspace, "state", None)
    ws_age_sec = 9999.0
    if ws is not None:
        try:
            ws_age_sec = max(0.0, time.time() - float(getattr(ws, "timestamp", 0.0) or 0.0))
        except Exception:
            ws_age_sec = 9999.0

    decision = decide_reply_path(
        sig,
        workspace_has_state=ws is not None,
        ws_age_sec=ws_age_sec,
        voice_priority=voice_prio,
    )

    print(f"[reply_path] path={decision.selected_path} reason={decision.reason}")
    print(
        f"[reply_path] complexity={decision.complexity_score:.2f} "
        f"cached={int(decision.safe_to_use_cached_state)}"
    )

    attach_reply_path_globals(g, decision)
    return decision


def should_skip_initial_workspace_tick(decision: ReplyPathDecision) -> bool:
    """True when chat/voice may reuse the last workspace tick."""
    return decision.selected_path == "fast" and bool(decision.safe_to_use_cached_state)
