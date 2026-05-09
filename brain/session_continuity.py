"""
Phase 29 — Multi-session strategic continuity (bounded, evidence-backed carryover).

Merges durable repo state (profiles, goals, self-model headlines) with current-tick
pipeline outputs. Descriptive guidance only — no prompt dumps, no fake threads, no auto repair.

**Identity anchors (read-only here):** ``ava_core/IDENTITY.md`` (core self/profile),
``ava_core/SOUL.md`` (values, boundaries, self-guidance), and ``ava_core/USER.md``
(durable relationship anchor for the user) load **first** as highest-priority continuity
threads. This module **never writes** those files; edits belong in supervised / approval
flows (IDENTITY/SOUL sensitive; USER reviewable). ``ava_core/BOOTSTRAP.md`` is first-run
scaffolding only and is **omitted** once IDENTITY/SOUL establish substantive content.
"""
from __future__ import annotations

import json
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .perception_types import (
    ContemplationResult,
    ConversationalNuanceResult,
    ContinuityThread,
    CuriosityResult,
    IdentityResolutionResult,
    MemoryRefinementResult,
    OutcomeLearningResult,
    ReflectionResult,
    SelfTestRunResult,
    SessionCarryoverSummary,
    SocialContinuityResult,
    StrategicContinuityResult,
    WorkbenchProposalResult,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _REPO_ROOT / "state"
_PROFILES_DIR = _REPO_ROOT / "profiles"
_SELF_MODEL_PATH = _REPO_ROOT / "memory" / "self reflection" / "self_model.json"
_AVA_CORE_DIR = _REPO_ROOT / "ava_core"

# Carryover categories (soft labels)
CAT_IDENTITY_ANCHOR = "identity_anchor_thread"
CAT_UNFINISHED_CONVERSATION = "unfinished_conversation_thread"
CAT_UNRESOLVED_CURIOSITY = "unresolved_curiosity_thread"
CAT_STRATEGIC_GOAL = "strategic_goal_thread"
CAT_RELATIONSHIP_CONTEXT = "relationship_context_thread"
CAT_MAINTENANCE_REPAIR = "maintenance_or_repair_thread"
CAT_OUTCOME_ADJUSTMENT = "outcome_adjustment_thread"
CAT_NONE = "no_relevant_carryover"

_SCOPE_IMMEDIATE = "immediate"
_SCOPE_RECENT = "recent"
_SCOPE_BACKGROUND = "background"
_SCOPE_NONE = "none"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _trunc(s: str, n: int = 200) -> str:
    t = " ".join((s or "").split())
    if len(t) <= n:
        return t
    return t[: n - 1].rstrip() + "…"


def _safe_read_json(path: Path, *, max_bytes: int = 512_000) -> Any | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) > max_bytes:
            return None
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _active_person_id(g: dict[str, Any], id_res: Optional[IdentityResolutionResult]) -> str:
    if id_res is not None:
        rid = getattr(id_res, "resolved_identity", None) or getattr(id_res, "raw_identity", None)
        if rid:
            return str(rid).strip().lower() or "unknown"
    for key in ("_active_person_id", "active_person_id"):
        v = g.get(key)
        if v:
            return str(v).strip().lower()
    blob = _safe_read_json(_STATE_DIR / "active_person.json", max_bytes=16_384)
    if isinstance(blob, dict):
        pid = blob.get("person_id")
        if pid:
            return str(pid).strip().lower()
    return "unknown"


def _profile_path(person_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in (person_id or "unknown").lower())
    return _PROFILES_DIR / f"{safe}.json"


def _read_utf8_limited(path: Path, *, max_bytes: int = 196_608) -> str:
    """Read text for anchor files; bounded size — not a full memory dump."""
    if not path.is_file():
        return ""
    try:
        raw = path.read_bytes()
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _anchor_excerpt(text: str, *, max_chars: int = 400) -> str:
    """Use first H1 title if present, then body (skip subsequent # lines until prose starts)."""
    lines = (text or "").splitlines()
    h1 = ""
    rest_i = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and not s.startswith("##"):
            h1 = s.lstrip("#").strip()
            rest_i = i + 1
            break

    buf: list[str] = []
    total = 0
    started = False
    for line in lines[rest_i:]:
        s = line.strip()
        if not s:
            if started:
                break
            continue
        if s.startswith("#") and not started:
            continue
        started = True
        buf.append(s)
        total += len(s) + 1
        if total >= max_chars:
            break
    body = " ".join(buf).strip()
    if h1:
        out = f"{h1} — {body}" if body else h1
    else:
        out = body
    return _trunc(out, max_chars)


def _identity_core_established(identity_txt: str, soul_txt: str) -> bool:
    """Once core files have real content, BOOTSTRAP is not used as ongoing self."""
    return len(identity_txt.strip()) >= 140 or len(soul_txt.strip()) >= 140


def _collect_ava_core_anchor_threads() -> tuple[list[ContinuityThread], dict[str, Any]]:
    """
    Primary continuity anchors — always before goals/social/reflection layering.

    Does not invent text; missing/empty files produce no thread. Does not write files.
    """
    p_id = _AVA_CORE_DIR / "IDENTITY.md"
    p_so = _AVA_CORE_DIR / "SOUL.md"
    p_us = _AVA_CORE_DIR / "USER.md"
    p_bo = _AVA_CORE_DIR / "BOOTSTRAP.md"

    id_txt = _read_utf8_limited(p_id)
    soul_txt = _read_utf8_limited(p_so)
    user_txt = _read_utf8_limited(p_us)
    established = _identity_core_established(id_txt, soul_txt)

    threads: list[ContinuityThread] = []
    if id_txt.strip():
        threads.append(
            ContinuityThread(
                category=CAT_IDENTITY_ANCHOR,
                summary=_trunc(f"IDENTITY anchor: {_anchor_excerpt(id_txt)}", 460),
                relevance=0.98,
                confidence=0.95,
                scope=_SCOPE_IMMEDIATE,
                source="ava_core/IDENTITY.md",
                evidence_note="authoritative_self_profile",
                meta={"anchor_role": "identity", "file": str(p_id.relative_to(_REPO_ROOT))},
            )
        )
    if soul_txt.strip():
        threads.append(
            ContinuityThread(
                category=CAT_IDENTITY_ANCHOR,
                summary=_trunc(f"SOUL anchor (values/boundaries): {_anchor_excerpt(soul_txt)}", 460),
                relevance=0.97,
                confidence=0.94,
                scope=_SCOPE_IMMEDIATE,
                source="ava_core/SOUL.md",
                evidence_note="authoritative_values_and_boundaries",
                meta={"anchor_role": "soul", "file": str(p_so.relative_to(_REPO_ROOT))},
            )
        )
    if user_txt.strip():
        threads.append(
            ContinuityThread(
                category=CAT_IDENTITY_ANCHOR,
                summary=_trunc(f"USER relationship anchor: {_anchor_excerpt(user_txt)}", 460),
                relevance=0.95,
                confidence=0.9,
                scope=_SCOPE_IMMEDIATE,
                source="ava_core/USER.md",
                evidence_note="durable_user_relationship_context",
                meta={"anchor_role": "user", "file": str(p_us.relative_to(_REPO_ROOT))},
            )
        )

    bootstrap_included = False
    if not established:
        bo_txt = _read_utf8_limited(p_bo)
        if bo_txt.strip():
            threads.append(
                ContinuityThread(
                    category=CAT_IDENTITY_ANCHOR,
                    summary=_trunc(f"BOOTSTRAP (first-run scaffold only): {_anchor_excerpt(bo_txt, max_chars=280)}", 400),
                    relevance=0.32,
                    confidence=0.38,
                    scope=_SCOPE_BACKGROUND,
                    source="ava_core/BOOTSTRAP.md",
                    evidence_note="scaffolding_until_core_identity_populated",
                    meta={
                        "anchor_role": "bootstrap_scaffold",
                        "file": str(p_bo.relative_to(_REPO_ROOT)),
                    },
                )
            )
            bootstrap_included = True

    bundle = {
        "identity_anchor_excerpt": _anchor_excerpt(id_txt, max_chars=360) if id_txt.strip() else "",
        "soul_anchor_excerpt": _anchor_excerpt(soul_txt, max_chars=360) if soul_txt.strip() else "",
        "user_anchor_excerpt": _anchor_excerpt(user_txt, max_chars=360) if user_txt.strip() else "",
        "identity_core_established": established,
        "bootstrap_included": bootstrap_included,
        "anchoring_policy": (
            "IDENTITY.md / SOUL.md / USER.md are authoritative continuity anchors; "
            "this phase reads them only. IDENTITY/SOUL edits require elevated approval; "
            "USER.md via reviewable flows. BOOTSTRAP.md is not primary self once core "
            "IDENTITY/SOUL are established."
        ),
    }
    return threads, bundle


def _anchor_sort_tuple(t: ContinuityThread) -> tuple[int, int, float]:
    """Identity anchors first: identity → soul → user → bootstrap scaffold; then score."""
    if t.category != CAT_IDENTITY_ANCHOR:
        return (1, 99, -(t.confidence * t.relevance))
    role = str((t.meta or {}).get("anchor_role") or "")
    order = {"identity": 0, "soul": 1, "user": 2, "bootstrap_scaffold": 8}.get(role, 5)
    return (0, order, -(t.confidence * t.relevance))


def _merge_goal_priorities() -> tuple[list[str], list[tuple[str, float]]]:
    """Return (priority_lines, scored tuples) from goal_system.json only."""
    data = _safe_read_json(_STATE_DIR / "goal_system.json")
    pairs: list[tuple[str, float]] = []
    if not isinstance(data, dict):
        return [], pairs
    goals = data.get("goals")
    if not isinstance(goals, list):
        return [], pairs
    for g in goals:
        if not isinstance(g, dict):
            continue
        if str(g.get("status") or "") != "active":
            continue
        text = str(g.get("text") or "").strip()
        if len(text) < 8:
            continue
        pr = float(g.get("current_priority") or g.get("importance") or 0.0)
        pairs.append((text, pr))
    pairs.sort(key=lambda x: x[1], reverse=True)
    lines = [_trunc(t[0], 140) for t in pairs[:4]]
    return lines, pairs[:6]


def _self_model_goal_snippets(limit: int = 2) -> list[str]:
    data = _safe_read_json(_SELF_MODEL_PATH)
    if not isinstance(data, dict):
        return []
    cg = data.get("current_goals")
    if not isinstance(cg, list):
        return []
    out: list[str] = []
    for item in cg[: max(limit * 4, 8)]:
        if isinstance(item, str) and len(item.strip()) >= 12:
            out.append(_trunc(item.strip(), 140))
        if len(out) >= limit:
            break
    return out


def build_strategic_continuity_safe(
    *,
    g: dict[str, Any] | None,
    user_text: str,
    identity_resolution: Optional[IdentityResolutionResult],
    social_continuity: SocialContinuityResult,
    memory_refinement: MemoryRefinementResult,
    workbench: WorkbenchProposalResult,
    reflection: ReflectionResult,
    contemplation: ContemplationResult,
    curiosity: CuriosityResult,
    outcome_learning: OutcomeLearningResult,
    conversational_nuance: Optional[ConversationalNuanceResult],
    selftests: Optional[SelfTestRunResult],
) -> StrategicContinuityResult:
    try:
        return _build_strategic_continuity(
            g=g if isinstance(g, dict) else {},
            user_text=user_text or "",
            identity_resolution=identity_resolution,
            social_continuity=social_continuity,
            memory_refinement=memory_refinement,
            workbench=workbench,
            reflection=reflection,
            contemplation=contemplation,
            curiosity=curiosity,
            outcome_learning=outcome_learning,
            conversational_nuance=conversational_nuance,
            selftests=selftests,
        )
    except Exception as e:
        print(f"[session_continuity] safe_fallback err={e!r}\n{traceback.format_exc()}")
        return _default_result(str(e)[:120])


def _default_result(err: str = "") -> StrategicContinuityResult:
    try:
        anchor_t, anchor_b = _collect_ava_core_anchor_threads()
    except Exception:
        anchor_t, anchor_b = [], {}
    r = StrategicContinuityResult(
        continuity_summary="No grounded multi-session carryover this tick.",
        continuity_confidence=0.18,
        continuity_scope=_SCOPE_NONE,
        session_carryover=SessionCarryoverSummary(
            headline="No grounded carryover.",
            thread_count=0,
            top_category=CAT_NONE,
        ),
        notes=["Phase 29 idle / safe fallback."],
        meta={"phase": 29, "error": err} if err else {"phase": 29},
    )
    r.meta.update(anchor_b)
    r.active_threads.extend(anchor_t)
    r.active_threads.append(
        ContinuityThread(
            category=CAT_NONE,
            summary="Insufficient grounded evidence for cross-session threads.",
            relevance=0.12,
            scope=_SCOPE_NONE,
            confidence=0.22,
            source="fallback",
        )
    )
    return r


def _build_strategic_continuity(
    *,
    g: dict[str, Any],
    user_text: str,
    identity_resolution: Optional[IdentityResolutionResult],
    social_continuity: SocialContinuityResult,
    memory_refinement: MemoryRefinementResult,
    workbench: WorkbenchProposalResult,
    reflection: ReflectionResult,
    contemplation: ContemplationResult,
    curiosity: CuriosityResult,
    outcome_learning: OutcomeLearningResult,
    conversational_nuance: Optional[ConversationalNuanceResult],
    selftests: Optional[SelfTestRunResult],
) -> StrategicContinuityResult:
    threads: list[ContinuityThread] = []
    notes: list[str] = []
    anchor_threads, anchor_bundle = _collect_ava_core_anchor_threads()
    threads.extend(anchor_threads)
    notes.append(
        "Authoritative anchors: ava_core/IDENTITY.md, SOUL.md, USER.md precede speculative "
        "reflection/social layers; this module reads them only (no writes). IDENTITY/SOUL edits "
        "require elevated approval; USER.md via reviewable flows."
    )

    person_id = _active_person_id(g, identity_resolution)
    prof = _safe_read_json(_profile_path(person_id))
    profile_last_topic = ""
    profile_relationship = ""
    if isinstance(prof, dict):
        profile_last_topic = str(prof.get("last_topic") or "").strip()
        profile_relationship = str(prof.get("relationship") or "").strip()

    goal_lines, goal_scored = _merge_goal_priorities()
    sm_goals = _self_model_goal_snippets(2)

    soc = social_continuity
    unfinished = bool(getattr(soc, "unfinished_thread_present", False))
    rel_summary = _trunc(str(getattr(soc, "relationship_summary", "") or ""), 240)
    fam = float(getattr(soc, "familiarity_score", 0.5) or 0.5)
    trust = float(getattr(soc, "trust_signal", 0.5) or 0.5)

    mr = memory_refinement.decision if memory_refinement else None
    mr_unfinished = bool(getattr(mr, "unfinished_thread_candidate", False)) if mr else False

    for link in (getattr(memory_refinement, "link_targets", None) or [])[:2]:
        stg = float(getattr(link, "strength", 0) or 0)
        if stg < 0.38:
            continue
        hint = str(getattr(link, "target_hint", "") or "").strip()
        kind = str(getattr(link, "link_kind", "") or "").strip()
        if len(hint) < 4:
            continue
        threads.append(
            ContinuityThread(
                category=CAT_UNFINISHED_CONVERSATION,
                summary=_trunc(f"Memory link ({kind or 'hint'}): {hint}", 200),
                relevance=_clamp01(0.33 + 0.25 * stg),
                scope=_SCOPE_RECENT,
                confidence=_clamp01(0.36 + 0.3 * stg),
                source="memory_refinement",
            )
        )

    # --- Unfinished conversation (needs social or MR signal; not text alone) ---
    if unfinished or mr_unfinished:
        ev = []
        if unfinished:
            ev.append("social_unfinished_flag")
        if mr_unfinished:
            ev.append("memory_refinement_candidate")
        rel_line = rel_summary or (profile_last_topic and f"Last topic noted: {_trunc(profile_last_topic, 100)}")
        if rel_line:
            threads.append(
                ContinuityThread(
                    category=CAT_UNFINISHED_CONVERSATION,
                    summary=_trunc(rel_line, 200),
                    relevance=_clamp01(0.42 + 0.12 * fam + (0.1 if mr_unfinished else 0)),
                    scope=_SCOPE_IMMEDIATE if unfinished else _SCOPE_RECENT,
                    confidence=_clamp01(0.35 + 0.18 * trust + (0.12 if mr_unfinished else 0)),
                    source="pipeline+profile",
                    evidence_note=",".join(ev),
                )
            )

    # --- Strategic goals from disk (bounded) ---
    seen_gl: set[str] = set()
    for text, pri in goal_scored[:3]:
        key = text[:48]
        if key in seen_gl:
            continue
        seen_gl.add(key)
        threads.append(
            ContinuityThread(
                category=CAT_STRATEGIC_GOAL,
                summary=_trunc(text, 180),
                relevance=_clamp01(0.38 + 0.02 * min(pri, 1.0)),
                scope=_SCOPE_RECENT,
                confidence=_clamp01(0.4 + 0.12 * min(pri, 1.0)),
                source="state/goal_system.json",
                evidence_note=f"priority={pri:.2f}",
            )
        )

    # Self-model goals: only if not redundant
    for sg in sm_goals:
        if any(sg[:40] in t.summary for t in threads if t.category == CAT_STRATEGIC_GOAL):
            continue
        threads.append(
            ContinuityThread(
                category=CAT_STRATEGIC_GOAL,
                summary=_trunc(sg, 180),
                relevance=0.36,
                scope=_SCOPE_BACKGROUND,
                confidence=0.33,
                source="self_model.json",
            )
        )
        break  # at most one extra

    # --- Curiosity ---
    cq = curiosity
    if (
        bool(getattr(cq, "curiosity_triggered", False))
        and str(getattr(cq, "curiosity_theme", "") or "") != "no_curiosity_needed"
    ):
        qn = _trunc(str(getattr(cq, "curiosity_question", "") or ""), 180)
        if len(qn) >= 10 and (bool(getattr(cq, "should_clarify", False)) or float(getattr(cq, "curiosity_confidence", 0) or 0) >= 0.38):
            threads.append(
                ContinuityThread(
                    category=CAT_UNRESOLVED_CURIOSITY,
                    summary=qn,
                    relevance=_clamp01(0.34 + 0.2 * float(getattr(cq, "curiosity_confidence", 0) or 0)),
                    scope=_SCOPE_RECENT,
                    confidence=_clamp01(0.32 + 0.35 * float(getattr(cq, "curiosity_confidence", 0) or 0)),
                    source="curiosity",
                )
            )

    # --- Outcome learning adjustment ---
    ol = outcome_learning
    adj = str(getattr(ol, "suggested_adjustment", "") or "").strip()
    adj_conf = float(getattr(ol, "adjustment_confidence", 0) or 0)
    cat = str(getattr(ol, "outcome_category", "") or "")
    if adj and adj_conf >= 0.22 and cat not in ("", "no_adjustment_needed"):
        threads.append(
            ContinuityThread(
                category=CAT_OUTCOME_ADJUSTMENT,
                summary=_trunc(adj, 200),
                relevance=_clamp01(0.35 + 0.45 * adj_conf),
                scope=_SCOPE_RECENT,
                confidence=_clamp01(0.3 + 0.5 * adj_conf),
                source="outcome_learning",
                evidence_note=cat[:80],
            )
        )

    # --- Maintenance / workbench / self-tests ---
    maint_parts: list[str] = []
    wb = workbench
    if bool(getattr(wb, "has_proposal", False)):
        tp = getattr(wb, "top_proposal", None)
        title = ""
        if tp is not None:
            title = str(getattr(tp, "title", "") or "").strip()
        if not title:
            title = str(getattr(wb, "summary", "") or "").strip()
        title = _trunc(title, 120)
        if title:
            maint_parts.append(f"Workbench proposal: {title}")
            threads.append(
                ContinuityThread(
                    category=CAT_MAINTENANCE_REPAIR,
                    summary=_trunc(title, 180),
                    relevance=0.52,
                    scope=_SCOPE_IMMEDIATE,
                    confidence=0.44,
                    source="workbench",
                )
            )
    if g.get("_last_workbench_execution_result") or g.get("_last_workbench_command_result"):
        maint_parts.append("Recent supervised workbench activity on record.")
        if not any(t.category == CAT_MAINTENANCE_REPAIR for t in threads):
            threads.append(
                ContinuityThread(
                    category=CAT_MAINTENANCE_REPAIR,
                    summary="Recent workbench execution or command context present (see meta).",
                    relevance=0.4,
                    scope=_SCOPE_RECENT,
                    confidence=0.36,
                    source="g_workbench_globals",
                )
            )
    st = selftests
    if st is not None:
        summ = getattr(st, "summary", None)
        failed = list(getattr(summ, "failed_checks", []) or []) if summ is not None else []
        if isinstance(failed, list) and failed:
            fc = ", ".join(str(x) for x in failed[:4])
            maint_parts.append(f"Self-tests: {fc}")
            threads.append(
                ContinuityThread(
                    category=CAT_MAINTENANCE_REPAIR,
                    summary=_trunc(f"Diagnostics need attention: {fc}", 200),
                    relevance=0.48,
                    scope=_SCOPE_IMMEDIATE,
                    confidence=0.5,
                    source="selftests",
                )
            )

    # --- Relationship context (profile + social) ---
    rel_bits: list[str] = []
    if profile_relationship:
        rel_bits.append(f"Relationship (profile): {profile_relationship}")
    if profile_last_topic and not any(profile_last_topic in t.summary for t in threads):
        rel_bits.append(f"Last topic (profile): {_trunc(profile_last_topic, 100)}")
    if rel_bits:
        threads.append(
            ContinuityThread(
                category=CAT_RELATIONSHIP_CONTEXT,
                summary=_trunc(" · ".join(rel_bits), 220),
                relevance=_clamp01(0.32 + 0.15 * fam),
                scope=_SCOPE_BACKGROUND,
                confidence=_clamp01(0.34 + 0.2 * trust),
                source="profile+social",
            )
        )

    # --- Reflection / contemplation short lines (only when confident) ---
    rf = reflection
    if float(getattr(rf, "confidence", 0) or 0) >= 0.42 and str(getattr(rf, "reflection_summary", "") or "").strip():
        rs = _trunc(str(getattr(rf, "reflection_summary", "") or ""), 160)
        if len(rs) > 20:
            threads.append(
                ContinuityThread(
                    category=CAT_RELATIONSHIP_CONTEXT,
                    summary=f"Reflection context: {rs}",
                    relevance=0.3,
                    scope=_SCOPE_BACKGROUND,
                    confidence=0.34,
                    source="reflection",
                )
            )

    ct = contemplation
    ct_question = _trunc(str(getattr(ct, "contemplation_question", "") or ""), 140)
    if ct_question and len(ct_question) > 15 and float(getattr(ct, "contemplation_confidence", 0) or 0) >= 0.4:
        threads.append(
            ContinuityThread(
                category=CAT_RELATIONSHIP_CONTEXT,
                summary=f"Contemplation focus: {ct_question}",
                relevance=0.28,
                scope=_SCOPE_BACKGROUND,
                confidence=0.32,
                source="contemplation",
            )
        )

    # --- Nuance as style carryover hint (weak; does not invent affect) ---
    cn = conversational_nuance
    if cn is not None and float(getattr(cn, "confidence", 0) or 0) >= 0.45:
        nt = str(getattr(cn, "nuance_tone", "") or "")
        ns = _trunc(str(getattr(cn, "nuance_summary", "") or ""), 120)
        if nt and ns:
            threads.append(
                ContinuityThread(
                    category=CAT_RELATIONSHIP_CONTEXT,
                    summary=f"Style continuity: {nt} — {ns}",
                    relevance=0.22,
                    scope=_SCOPE_BACKGROUND,
                    confidence=0.28,
                    source="conversational_nuance",
                    evidence_note="non-affective_hint",
                )
            )

    # Dedup (identity anchors keyed by anchor_role); sort anchors before other threads
    deduped: list[ContinuityThread] = []
    seen_s: set[str] = set()
    for t in sorted(threads, key=_anchor_sort_tuple):
        role = str((t.meta or {}).get("anchor_role", ""))
        key = (
            f"{t.category}:{role}:{t.summary[:52]}"
            if t.category == CAT_IDENTITY_ANCHOR
            else f"{t.category}:{t.summary[:52]}"
        )
        if key in seen_s:
            continue
        seen_s.add(key)
        deduped.append(t)

    anchors_first = [t for t in deduped if t.category == CAT_IDENTITY_ANCHOR][:4]
    others = [t for t in deduped if t.category != CAT_IDENTITY_ANCHOR]
    active = anchors_first + others[: max(0, 7 - len(anchors_first))]

    # Filter clutter: drop weak threads unless identity anchors
    if len(active) > 1:
        stronger = [
            t
            for t in active
            if t.category == CAT_IDENTITY_ANCHOR or t.relevance >= 0.28 or t.confidence >= 0.42
        ]
        if stronger:
            anchors_kept = [t for t in stronger if t.category == CAT_IDENTITY_ANCHOR][:4]
            non_anchor = [t for t in stronger if t.category != CAT_IDENTITY_ANCHOR]
            active = anchors_kept + non_anchor[: max(0, 7 - len(anchors_kept))]

    unfinished_only = [t for t in active if t.category in (CAT_UNFINISHED_CONVERSATION, CAT_UNRESOLVED_CURIOSITY)]

    relationship_carryover = ""
    if rel_bits:
        relationship_carryover = _trunc(" · ".join(rel_bits), 320)
    elif rel_summary:
        relationship_carryover = rel_summary
    ua_ex = str(anchor_bundle.get("user_anchor_excerpt") or "").strip()
    if ua_ex and len(ua_ex) > 24:
        prefix = "USER.md anchor · "
        relationship_carryover = _trunc(
            f"{prefix}{ua_ex}"
            + (f" · {relationship_carryover}" if relationship_carryover else ""),
            520,
        )

    maintenance_carryover = _trunc(" | ".join(maint_parts), 280) if maint_parts else ""

    recent_adj = ""
    if adj and adj_conf >= 0.2:
        recent_adj = _trunc(adj, 220)

    # Global confidence
    if not active:
        out = _default_result()
        out.notes = list(out.notes or []) + notes
        return out

    top_scores = sorted((t.relevance * t.confidence for t in active), reverse=True)[:3]
    mean_top = sum(top_scores) / max(len(top_scores), 1)
    diversity = min(1.0, len(set(t.category for t in active)) / 4.5)
    carry_conf = _clamp01(0.22 + 0.55 * mean_top + 0.08 * diversity)

    scope_rank = {_SCOPE_IMMEDIATE: 3, _SCOPE_RECENT: 2, _SCOPE_BACKGROUND: 1, _SCOPE_NONE: 0}
    best_scope = _SCOPE_NONE
    for t in active:
        if scope_rank.get(t.scope, 0) > scope_rank.get(best_scope, 0):
            best_scope = t.scope

    top_cat = active[0].category if active else CAT_NONE
    headline = active[0].summary[:140] + ("…" if len(active[0].summary) > 140 else "")
    session_sum = SessionCarryoverSummary(
        headline=headline or "Carryover threads present.",
        thread_count=len(active),
        top_category=top_cat,
    )

    continuity_summary = _trunc(
        f"{len(active)} carryover thread(s); focus={top_cat}. {headline}",
        280,
    )

    meta: dict[str, Any] = {
        "phase": 29,
        "person_id": person_id,
        "goal_lines_sample": goal_lines[:3],
        "unfinished_thread_present": unfinished,
        "tone_hint": getattr(cn, "nuance_tone", "") if cn else "",
    }
    meta.update(anchor_bundle)

    _ap = []
    if anchor_bundle.get("identity_anchor_excerpt"):
        _ap.append("identity")
    if anchor_bundle.get("soul_anchor_excerpt"):
        _ap.append("soul")
    if anchor_bundle.get("user_anchor_excerpt"):
        _ap.append("user")
    _boot = "on" if anchor_bundle.get("bootstrap_included") else "off"
    print(
        f"[session_continuity] anchors={'+'.join(_ap) or 'none'} bootstrap={_boot} "
        f"established={anchor_bundle.get('identity_core_established')}"
    )

    print(
        f"[session_continuity] threads={len(active)} conf={carry_conf:.2f} "
        f"summary={continuity_summary[:160]!r}"
    )
    pri_str = ", ".join(goal_lines[:3]) if goal_lines else "—"
    maint_short = maintenance_carryover[:120] if maintenance_carryover else "—"
    print(f"[session_continuity] priorities={pri_str[:200]!r} maintenance={maint_short!r}")

    return StrategicContinuityResult(
        active_threads=active,
        unfinished_threads=unfinished_only[:5],
        strategic_priorities=list(goal_lines[:5]),
        relationship_carryover=relationship_carryover,
        maintenance_carryover=maintenance_carryover,
        recent_adjustment_carryover=recent_adj,
        session_carryover=session_sum,
        continuity_summary=continuity_summary,
        continuity_confidence=carry_conf,
        continuity_scope=best_scope,
        notes=notes + ["Phase 29 — descriptive carryover only; no auto actions."],
        meta=meta,
    )


def apply_strategic_continuity_to_perception_state(state: Any, bundle: Any) -> None:
    """Phase 29 — map :class:`StrategicContinuityResult` onto flat :class:`~brain.perception.PerceptionState`."""
    sc = getattr(bundle, "strategic_continuity", None)
    if sc is None:
        state.strategic_continuity_summary = ""
        state.strategic_continuity_confidence = 0.0
        state.active_threads = []
        state.strategic_priorities = []
        state.relationship_carryover = ""
        state.maintenance_carryover = ""
        state.continuity_scope = _SCOPE_NONE
        state.continuity_meta = {"phase": 29, "idle": True}
        return

    state.strategic_continuity_summary = str(sc.continuity_summary or "")[:520]
    state.strategic_continuity_confidence = float(sc.continuity_confidence)
    state.active_threads = [asdict(t) for t in (sc.active_threads or [])[:10]]
    state.strategic_priorities = [str(x)[:300] for x in (sc.strategic_priorities or [])[:12]]
    state.relationship_carryover = str(sc.relationship_carryover or "")[:520]
    state.maintenance_carryover = str(sc.maintenance_carryover or "")[:520]
    state.continuity_scope = str(sc.continuity_scope or _SCOPE_NONE)[:48]
    sc_meta = dict(sc.meta or {})
    sc_meta["recent_adjustment_carryover"] = str(sc.recent_adjustment_carryover or "")[:400]
    sc_meta["unfinished_thread_count"] = len(sc.unfinished_threads or [])
    sc_meta["session_carryover"] = asdict(sc.session_carryover) if sc.session_carryover else {}
    state.continuity_meta = sc_meta
