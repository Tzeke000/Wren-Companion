"""
Phase 25 — Dynamic cognitive routing to Ollama models (same Ava mind; different inference engines).

Includes **live availability discovery** (HTTP ``/api/tags`` + ``ollama list`` fallback), a **capability registry**
filtered by runtime tags, **stickiness / anti-thrashing**, and **social continuity bias** toward keeping the
current engine when classification margin is weak.

Does **not** alter identity, memory retrieval, persona text, relationship state, or continuity — only selects
which ``ChatOllama`` model tag to bind for inference.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from config.ava_tuning import (
    DEFAULT_MODEL_CAPABILITY_PROFILES,
    MODEL_ROUTING_CONFIG,
    ModelCapabilityProfileDef,
    ModelRoutingConfig,
)

from .perception_types import (
    CognitiveModeResult,
    ContemplationResult,
    InterpretationLayerResult,
    MemoryRefinementResult,
    ModelCapabilityEntry,
    ModelRouteCandidate,
    ModelRoutingResult,
    QualityOutput,
    ReflectionResult,
    SocialContinuityResult,
    WorkbenchProposalResult,
)

# Cognitive routing categories (not alternate personas).
SOCIAL_CHAT_MODE = "social_chat_mode"
DEEP_REASONING_MODE = "deep_reasoning_mode"
CODING_REPAIR_MODE = "coding_repair_mode"
MEMORY_MAINTENANCE_MODE = "memory_maintenance_mode"
PERCEPTION_SUPPORT_MODE = "perception_support_mode"
FALLBACK_SAFE_MODE = "fallback_safe_mode"

_ALL_MODES: tuple[str, ...] = (
    SOCIAL_CHAT_MODE,
    DEEP_REASONING_MODE,
    CODING_REPAIR_MODE,
    MEMORY_MAINTENANCE_MODE,
    PERCEPTION_SUPPORT_MODE,
    FALLBACK_SAFE_MODE,
)

_URGENT_MODES = frozenset({CODING_REPAIR_MODE, DEEP_REASONING_MODE})
_CHAT_EXCLUDED_PREFIXES = ("nomic-embed-text",)

_TAGS_LOCK = threading.Lock()
_TAGS_CACHE: Optional[frozenset[str]] = None
_TAGS_MONO: float = 0.0
_TAGS_SOURCE: str = "unknown"


def _mode_to_model_name(mode: str, cfg: ModelRoutingConfig) -> str:
    m = {
        SOCIAL_CHAT_MODE: cfg.social_chat_model,
        DEEP_REASONING_MODE: cfg.deep_reasoning_model,
        CODING_REPAIR_MODE: cfg.coding_repair_model,
        MEMORY_MAINTENANCE_MODE: cfg.memory_maintenance_model,
        PERCEPTION_SUPPORT_MODE: cfg.perception_support_model,
        FALLBACK_SAFE_MODE: cfg.fallback_safe_model,
    }.get(mode)
    return str(m or cfg.default_model).strip() or cfg.default_model


def _ollama_base_url() -> str:
    import os

    return (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")


def _parse_ollama_list_stdout(text: str) -> frozenset[str]:
    names: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.upper().startswith("NAME"):
            continue
        parts = re.split(r"\s+", line)
        if not parts or parts[0].startswith("---"):
            continue
        n = parts[0].strip()
        if len(n) >= 2 and not n.startswith("#"):
            names.append(n)
    return frozenset(names)


def _discover_via_ollama_cli() -> Optional[frozenset[str]]:
    try:
        kwargs: dict[str, Any] = {
            "args": ["ollama", "list"],
            "capture_output": True,
            "text": True,
            "timeout": 3.5,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        proc = subprocess.run(**kwargs)
        if proc.returncode != 0 or not proc.stdout:
            return None
        return _parse_ollama_list_stdout(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return None


def fetch_ollama_model_tags(*, force: bool = False) -> Optional[frozenset[str]]:
    """Cached ``/api/tags`` discovery (compat name for callers)."""
    tags, _src = discover_available_model_tags(force=force)
    return tags


def discover_available_model_tags(*, force: bool = False) -> tuple[Optional[frozenset[str]], str]:
    """
    Returns ``(tags, source)`` where source is ``api_tags``, ``ollama_list_cli``, or ``unavailable``.
    Updates the process cache when HTTP succeeds; CLI path also seeds cache when HTTP failed.
    """
    global _TAGS_CACHE, _TAGS_MONO, _TAGS_SOURCE
    poll = float(getattr(MODEL_ROUTING_CONFIG, "ollama_tags_poll_seconds", 55.0) or 55.0)
    now = time.monotonic()
    with _TAGS_LOCK:
        if not force and _TAGS_CACHE is not None and (now - _TAGS_MONO) < poll:
            return _TAGS_CACHE, _TAGS_SOURCE or "cached"

    # 1) HTTP /api/tags (same host as ChatOllama)
    try:
        url = f"{_ollama_base_url()}/api/tags"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.35) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        models = data.get("models") if isinstance(data, dict) else None
        names: set[str] = set()
        if isinstance(models, list):
            for item in models:
                if isinstance(item, dict):
                    n = item.get("name")
                    if isinstance(n, str) and n.strip():
                        names.add(n.strip())
        frozen = frozenset(names)
        with _TAGS_LOCK:
            _TAGS_CACHE = frozen
            _TAGS_MONO = time.monotonic()
            _TAGS_SOURCE = "api_tags"
        return frozen, "api_tags"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError, TypeError):
        pass

    # 2) CLI `ollama list` (same data, no HTTP)
    cli = _discover_via_ollama_cli()
    if cli is not None:
        with _TAGS_LOCK:
            _TAGS_CACHE = cli
            _TAGS_MONO = time.monotonic()
            _TAGS_SOURCE = "ollama_list_cli"
        return cli, "ollama_list_cli"

    with _TAGS_LOCK:
        _TAGS_SOURCE = "unavailable"
    return None, "unavailable"


def try_fetch_model_card_digest(model_name: str) -> Optional[dict[str, Any]]:
    """Lightweight ``/api/show`` probe (best-effort; routing does not depend on success)."""
    try:
        q = urllib.parse.quote(model_name, safe="")
        url = f"{_ollama_base_url()}/api/show?name={q}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=0.85) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return {
            "model": str(data.get("model", model_name))[:128],
            "parameter_size": str(data.get("details", {}).get("parameter_size", ""))[:32]
            if isinstance(data.get("details"), dict)
            else "",
            "quantization": str(data.get("details", {}).get("quantization_level", ""))[:32]
            if isinstance(data.get("details"), dict)
            else "",
        }
    except Exception:
        return None


_PREFS_PATH: Optional[str] = None
_PREFS_LOCK = threading.Lock()


def _track_model_preference(
    selected: str, mode: str, online: bool, g: dict[str, Any]
) -> None:
    """Bootstrap: record which model was chosen for which mode. Ava's preferences emerge from this data."""
    try:
        import json as _json
        from pathlib import Path as _Path
        base = _Path(g.get("BASE_DIR") or ".")
        path = base / "state" / "model_preferences.json"
        with _PREFS_LOCK:
            prefs: dict[str, Any] = {}
            if path.is_file():
                try:
                    prefs = _json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            key = f"{mode}:{'cloud' if online else 'local'}"
            counts = prefs.get("counts") or {}
            counts[key] = counts.get(key, {})
            counts[key][selected] = int(counts[key].get(selected) or 0) + 1
            prefs["counts"] = counts
            prefs["last_selected"] = selected
            prefs["last_mode"] = mode
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _neutral_entry(model_name: str, *, available: bool = True) -> ModelCapabilityEntry:
    return ModelCapabilityEntry(
        model_name=model_name,
        available=available,
        cognitive_modes=list(_ALL_MODES),
        latency_tendency=0.5,
        reasoning_strength=0.5,
        coding_suitability=0.5,
        summarization_suitability=0.5,
        fallback_priority=999,
        source="discovered",
    )


def _is_internet_available(g: Optional[dict[str, Any]]) -> bool:
    """Fast read of connectivity state — no blocking call."""
    if g is None:
        return False
    return bool(g.get("_is_online", False))


def build_runtime_capability_registry(
    available: Optional[frozenset[str]],
    profiles: tuple[ModelCapabilityProfileDef, ...],
    g: Optional[dict[str, Any]] = None,
) -> list[ModelCapabilityEntry]:
    """Merge config profiles with discovered tag names not present in config (neutral profile).
    When offline (g._is_online == False), profiles with requires_internet=True are excluded.
    """
    online = _is_internet_available(g)
    entries: dict[str, ModelCapabilityEntry] = {}
    for p in profiles:
        name = str(p.model_name).strip()
        if not name:
            continue
        # Phase cloud-routing: exclude internet-required models when offline
        needs_inet = bool(getattr(p, "requires_internet", False))
        if needs_inet and not online:
            continue
        avail = True if available is None else name in available
        entries[name] = ModelCapabilityEntry(
            model_name=name,
            available=avail,
            cognitive_modes=list(p.cognitive_modes),
            latency_tendency=float(p.latency_tendency),
            reasoning_strength=float(p.reasoning_strength),
            coding_suitability=float(p.coding_suitability),
            summarization_suitability=float(p.summarization_suitability),
            fallback_priority=int(p.fallback_priority),
            source="config",
        )

    filtered_available = (
        frozenset(n for n in (available or frozenset()) if not any(str(n).startswith(px) for px in _CHAT_EXCLUDED_PREFIXES))
        if available is not None
        else None
    )

    if filtered_available:
        for name in sorted(filtered_available):
            if name in entries:
                continue
            entries[name] = ModelCapabilityEntry(
                model_name=name,
                available=True,
                cognitive_modes=list(_ALL_MODES),
                latency_tendency=0.5,
                reasoning_strength=0.5,
                coding_suitability=0.5,
                summarization_suitability=0.5,
                fallback_priority=500,
                source="discovered",
            )

    # Also strip configured entries that are embedding-only by tag prefix.
    entries = {k: v for k, v in entries.items() if not any(str(k).startswith(px) for px in _CHAT_EXCLUDED_PREFIXES)}
    out = sorted(entries.values(), key=lambda e: (e.fallback_priority, e.model_name))
    return out


def _fit_for_mode(mode: str, e: ModelCapabilityEntry) -> float:
    """Scalar suitability of profile ``e`` for cognitive ``mode`` (0..1)."""
    if mode == SOCIAL_CHAT_MODE:
        base = 0.58 * float(e.latency_tendency) + 0.42 * float(e.reasoning_strength)
    elif mode == DEEP_REASONING_MODE:
        base = 0.85 * float(e.reasoning_strength) + 0.15 * float(e.coding_suitability)
    elif mode == CODING_REPAIR_MODE:
        base = 0.72 * float(e.coding_suitability) + 0.28 * float(e.reasoning_strength)
    elif mode == MEMORY_MAINTENANCE_MODE:
        base = 0.68 * float(e.summarization_suitability) + 0.32 * float(e.reasoning_strength)
    elif mode == PERCEPTION_SUPPORT_MODE:
        base = (
            0.38 * float(e.reasoning_strength)
            + 0.34 * float(e.latency_tendency)
            + 0.28 * float(e.coding_suitability)
        )
    else:
        base = (
            0.25 * float(e.latency_tendency)
            + 0.25 * float(e.reasoning_strength)
            + 0.25 * float(e.coding_suitability)
            + 0.25 * float(e.summarization_suitability)
        )
    if mode in e.cognitive_modes:
        base += 0.065
    return float(max(0.0, min(1.0, base)))


def _mode_suitability_floor(mode: str, cfg: ModelRoutingConfig) -> float:
    bump = {
        CODING_REPAIR_MODE: 0.16,
        DEEP_REASONING_MODE: 0.12,
        MEMORY_MAINTENANCE_MODE: 0.08,
    }
    return min(0.9, float(cfg.routing_suitability_floor) + bump.get(mode, 0.0))


def _social_switch_resistance(soc: SocialContinuityResult | None, cfg: ModelRoutingConfig) -> float:
    soc = soc or SocialContinuityResult()
    fam = float(getattr(soc, "familiarity_score", 0.5) or 0.5)
    unfinished = bool(getattr(soc, "unfinished_thread_present", False))
    base = float(cfg.routing_social_stickiness_weight)
    return float(min(0.36, base * (0.45 + 0.55 * fam) * (1.25 if unfinished else 1.0)))


def _resolve_warm_model_for_mode(
    mode: str,
    cfg: ModelRoutingConfig,
    registry: list[ModelCapabilityEntry],
    available: Optional[frozenset[str]],
) -> tuple[str, str]:
    """
    Pick the best **available** model for ``mode``.
    Never assumes the config preferred name exists without a tag check when tags are known.
    """
    preferred = _mode_to_model_name(mode, cfg).strip()
    global_fb = (cfg.global_fallback_model or cfg.default_model or "").strip()
    default_m = (cfg.default_model or "").strip()

    candidates = [e for e in registry if e.available]

    def _pick_best_fit(reason: str) -> tuple[str, str]:
        if not candidates:
            return preferred or default_m or global_fb, reason + "|no_registry_candidates"
        ranked = sorted(
            candidates,
            key=lambda e: (-_fit_for_mode(mode, e), int(e.fallback_priority), e.model_name),
        )
        best = ranked[0]
        return best.model_name, reason + f"|best_fit={best.model_name}"

    if available is None:
        return preferred, "availability_unknown_use_config_preferred"

    if preferred and preferred in available:
        return preferred, "preferred_available"

    # Explicit fallbacks from legacy chain
    chain = [
        preferred,
        global_fb,
        default_m,
        _mode_to_model_name(FALLBACK_SAFE_MODE, cfg),
    ]
    for name in chain:
        if name and name in available:
            return name, f"chain_fallback={name}"

    best_name, br = _pick_best_fit("warm_registry")
    return best_name, br


def _pick_available(
    preferred: str,
    mode_fallback: str,
    global_fb: str,
    available: Optional[frozenset[str]],
) -> tuple[str, str]:
    """Legacy ordered pick (still used when registry empty)."""
    pref = (preferred or "").strip()
    fb = (mode_fallback or "").strip()
    gb = (global_fb or "").strip()
    if available is not None:
        available = frozenset(
            n for n in available if not any(str(n).startswith(px) for px in _CHAT_EXCLUDED_PREFIXES)
        )
    if available is None:
        return pref, "tags_unavailable_assume_ok"
    if pref and pref in available:
        return pref, "preferred_available"
    if fb and fb in available:
        return fb, "preferred_missing_used_mode_fallback"
    if gb and gb in available:
        return gb, "used_global_fallback"
    if available:
        return sorted(available)[0], "first_lexical_available_tag"
    return gb or pref, "empty_tag_set"


def _score_modes(
    *,
    user_text: str,
    cfg: ModelRoutingConfig,
    g: dict[str, Any] | None,
    qual: QualityOutput,
    wb: WorkbenchProposalResult | None,
    mr: MemoryRefinementResult | None,
    soc: SocialContinuityResult | None,
    rf: ReflectionResult | None,
    ct: ContemplationResult | None,
    il: InterpretationLayerResult | None,
) -> tuple[dict[str, float], list[str]]:
    ut = (user_text or "").strip()
    ut_low = ut.lower()
    ut_words = re.findall(r"[a-z0-9']+", ut_low)
    word_count = len(ut_words)
    signals: list[str] = []
    scores: dict[str, float] = {m: 0.0 for m in _ALL_MODES}

    voice_priority = bool(g.get("_voice_user_turn_priority")) if isinstance(g, dict) else False
    if voice_priority:
        scores[SOCIAL_CHAT_MODE] += 0.55
        signals.append("voice_turn_priority_latency")

    wb = wb or WorkbenchProposalResult()
    top = wb.top_proposal
    exec_ctx = isinstance(g, dict) and (
        g.get("_last_workbench_execution_result") is not None
        or g.get("_last_workbench_command_result") is not None
    )
    risky_proposal = bool(wb.has_proposal) and str(getattr(top, "priority", "low") or "").lower() in (
        "medium",
        "high",
        "urgent",
    )
    ptype = str(getattr(top, "proposal_type", "") or "").lower()
    code_type = any(
        x in ptype for x in ("patch", "repair", "code", "lint", "test", "diagnostic", "health", "file")
    )
    if exec_ctx:
        scores[CODING_REPAIR_MODE] += 0.62
        signals.append("workbench_execution_or_command_context")
    if risky_proposal and code_type:
        scores[CODING_REPAIR_MODE] += 0.48
        signals.append("workbench_high_priority_code_shape")
    elif risky_proposal:
        scores[CODING_REPAIR_MODE] += 0.22
        signals.append("workbench_priority_non_code")

    code_kw = (
        "traceback",
        "syntaxerror",
        "patch",
        "diff",
        "workbench",
        ".py",
        "pull request",
        "stack trace",
        "apply_patch",
    )
    if any(k in ut_low for k in code_kw):
        scores[CODING_REPAIR_MODE] += 0.35
        signals.append("user_text_code_repair_cues")

    mr = mr or MemoryRefinementResult()
    try:
        d = mr.decision
        rpri = float(getattr(d, "retrieval_priority", 0.0) or 0.0)
        rcls = str(getattr(d, "refined_memory_class", "ignore") or "ignore")
    except Exception:
        rpri = 0.0
        rcls = "ignore"
    mem_kw = ("summarize memories", "prune memory", "memory cleanup", "forget old", "dedupe")
    if rpri >= 0.42 and rcls != "ignore":
        scores[MEMORY_MAINTENANCE_MODE] += 0.44
        signals.append("memory_refinement_retrieval_priority")
    if any(k in ut_low for k in mem_kw):
        scores[MEMORY_MAINTENANCE_MODE] += 0.5
        signals.append("user_memory_maintenance_intent")

    trusted = bool(getattr(qual, "visual_truth_trusted", True))
    visual_q = (
        "what do you see",
        "camera",
        "on screen",
        "in the frame",
        "in the image",
        "look at",
    )
    if not trusted and any(k in ut_low for k in visual_q):
        scores[PERCEPTION_SUPPORT_MODE] += 0.52
        signals.append("untrusted_vision_visual_question")

    il = il or InterpretationLayerResult()
    pe = str(getattr(il, "primary_event", "") or "")
    if pe in ("scene_changed", "person_entered", "unknown_person_present") and float(
        getattr(il, "event_confidence", 0.0) or 0.0
    ) >= 0.55:
        scores[PERCEPTION_SUPPORT_MODE] += 0.25
        signals.append("semantic_scene_event_active")

    deep_kw = (
        "prove",
        "formal proof",
        "step by step",
        "analyze in depth",
        "tradeoff",
        "evaluate carefully",
        "philosophical",
        "what are the implications",
    )
    if len(ut) > 900 or any(k in ut_low for k in deep_kw):
        scores[DEEP_REASONING_MODE] += 0.4
        signals.append("long_or_analytic_user_text")

    rf = rf or ReflectionResult()
    rc = str(getattr(rf, "reflection_category", "") or "").lower()
    if "uncertain" in rc or "deep" in rc or "maintain" in rc:
        scores[DEEP_REASONING_MODE] += 0.18
        signals.append("reflection_category_depth_cue")

    ct = ct or ContemplationResult()
    if str(getattr(ct, "contemplation_theme", "") or "").startswith(("significance", "boundary", "ethics")):
        scores[DEEP_REASONING_MODE] += 0.14
        signals.append("contemplation_theme_depth")

    soc = soc or SocialContinuityResult()
    if float(getattr(soc, "familiarity_score", 0.5) or 0.5) >= 0.62 and len(ut) < 220:
        scores[SOCIAL_CHAT_MODE] += 0.22
        signals.append("high_familiarity_short_turn")

    if word_count <= 10 and not exec_ctx:
        scores[SOCIAL_CHAT_MODE] = max(scores[SOCIAL_CHAT_MODE], float(cfg.social_short_message_base_score))
        scores[SOCIAL_CHAT_MODE] += float(cfg.social_short_message_boost)
        signals.append("short_message_social_boost")
    if "?" not in ut and ut.strip():
        scores[SOCIAL_CHAT_MODE] += float(cfg.social_no_question_boost)
        signals.append("no_question_mark_social_boost")
    if any(gw in ut_low for gw in ("hey", "hi", "hello", "how are")):
        scores[SOCIAL_CHAT_MODE] += float(cfg.social_greeting_boost)
        signals.append("greeting_social_boost")
    try:
        hh = int(time.strftime("%H"))
        if hh >= 18 or hh <= 4:
            scores[SOCIAL_CHAT_MODE] += float(cfg.social_evening_boost)
            signals.append("evening_night_social_boost")
    except Exception:
        pass
    scores[SOCIAL_CHAT_MODE] = min(float(cfg.social_score_ceiling), max(0.0, scores[SOCIAL_CHAT_MODE]))

    # Fallback starts lower; only rises in explicit recovery/error-like contexts.
    error_recovery_cues = ("error", "failed", "exception", "recover", "crash", "traceback")
    explicit_error_recovery = any(tok in ut_low for tok in error_recovery_cues)
    scores[FALLBACK_SAFE_MODE] = float(cfg.fallback_base_score) + (0.18 if explicit_error_recovery else 0.0)
    if explicit_error_recovery:
        signals.append("explicit_error_recovery_context")

    social_or_deep = max(scores[SOCIAL_CHAT_MODE], scores[DEEP_REASONING_MODE])
    if social_or_deep > 0.35:
        scores[FALLBACK_SAFE_MODE] = min(scores[FALLBACK_SAFE_MODE], max(0.12, social_or_deep - 0.03))
        signals.append("anti_fallback_dominance_social_or_deep")

    return scores, signals


def _priorities_for_mode(mode: str) -> tuple[float, float, float]:
    if mode == SOCIAL_CHAT_MODE:
        return 0.92, 0.35, 0.42
    if mode == DEEP_REASONING_MODE:
        return 0.28, 0.88, 0.9
    if mode == CODING_REPAIR_MODE:
        return 0.45, 0.62, 0.88
    if mode == MEMORY_MAINTENANCE_MODE:
        return 0.62, 0.85, 0.55
    if mode == PERCEPTION_SUPPORT_MODE:
        return 0.55, 0.58, 0.62
    return 0.5, 0.55, 0.62


def _registry_map(registry: list[ModelCapabilityEntry]) -> dict[str, ModelCapabilityEntry]:
    return {e.model_name: e for e in registry}


def build_model_routing_result(
    *,
    user_text: str,
    g: dict[str, Any] | None,
    quality: QualityOutput,
    workbench: WorkbenchProposalResult | None,
    memory_refinement: MemoryRefinementResult | None,
    social_continuity: SocialContinuityResult | None,
    reflection: ReflectionResult | None,
    contemplation: ContemplationResult | None,
    interpretation_layer: InterpretationLayerResult | None,
    config: Optional[ModelRoutingConfig] = None,
) -> ModelRoutingResult:
    cfg = config or MODEL_ROUTING_CONFIG
    profiles = DEFAULT_MODEL_CAPABILITY_PROFILES

    scores, signals = _score_modes(
        user_text=user_text,
        cfg=cfg,
        g=g if isinstance(g, dict) else {},
        qual=quality,
        wb=workbench,
        mr=memory_refinement,
        soc=social_continuity,
        rf=reflection,
        ct=contemplation,
        il=interpretation_layer,
    )

    override_mode: Optional[str] = None
    override_model: Optional[str] = None
    last_effective: Optional[str] = None
    if isinstance(g, dict):
        om = g.get("_routing_cognitive_mode_override")
        if isinstance(om, str) and om.strip():
            override_mode = om.strip()
        omd = g.get("_routing_model_override")
        if isinstance(omd, str) and omd.strip():
            override_model = omd.strip()
        le = g.get("_routing_last_effective_model")
        if isinstance(le, str) and le.strip():
            last_effective = le.strip()

    ranked = sorted(((m, scores.get(m, 0.0)) for m in _ALL_MODES), key=lambda x: x[1], reverse=True)
    winner, win_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = float(win_score - second_score)

    if override_mode and override_mode in _ALL_MODES:
        winner = override_mode
        win_score = scores.get(winner, win_score)
        signals.append("override_cognitive_mode")

    routing_confidence = float(min(1.0, max(0.08, win_score * (0.65 + 0.35 * min(1.0, margin * 4.0)))))

    if win_score < 0.18 and winner != FALLBACK_SAFE_MODE:
        winner = FALLBACK_SAFE_MODE
        signals.append("confidence_gate_fallback_safe")
        routing_confidence = min(routing_confidence, 0.45)

    soc = social_continuity or SocialContinuityResult()
    social_res = _social_switch_resistance(soc, cfg)

    available, discovery_source = discover_available_model_tags(force=False)
    _g_dict = g if isinstance(g, dict) else {}
    online = _is_internet_available(_g_dict)
    registry = build_runtime_capability_registry(available, profiles, g=_g_dict)
    reg_by_name = _registry_map(registry)

    # Cloud routing rules: social_chat and memory_maintenance always stay local
    if winner == SOCIAL_CHAT_MODE:
        signals.append("social_chat_force_local")
    if winner == MEMORY_MAINTENANCE_MODE:
        signals.append("memory_maintenance_force_local")
    # For deep_reasoning + coding online → cloud models are now in registry

    # Fallback should win only when: no mode > 0.40, explicit recovery context, or capability uncertainty.
    score_social = float(scores.get(SOCIAL_CHAT_MODE, 0.0) or 0.0)
    score_deep = float(scores.get(DEEP_REASONING_MODE, 0.0) or 0.0)
    max_other = max(float(v) for k, v in scores.items() if k != FALLBACK_SAFE_MODE)
    explicit_error_context = "explicit_error_recovery_context" in signals
    capability_uncertain = discovery_source == "unavailable"
    fallback_allowed = (max_other <= 0.40) or explicit_error_context or capability_uncertain
    if winner == FALLBACK_SAFE_MODE and not fallback_allowed:
        for m, sc in ranked:
            if m != FALLBACK_SAFE_MODE:
                winner, win_score = m, sc
                second_score = float(scores.get(FALLBACK_SAFE_MODE, 0.0) or 0.0)
                margin = float(win_score - second_score)
                signals.append("fallback_suppressed_non_dominant")
                break

    primary_warm, warm_note = _resolve_warm_model_for_mode(winner, cfg, registry, available)
    mode_fallback_name = (cfg.global_fallback_model or "").strip()
    explicit_fallback = mode_fallback_name or (cfg.default_model or "").strip()

    # Cloud-first routing: when online + cloud reachable, prefer cloud for
    # reasoning/coding/fallback. Social and memory always stay local.
    _LOCAL_ONLY_MODES = frozenset({SOCIAL_CHAT_MODE, MEMORY_MAINTENANCE_MODE})
    _CLOUD_PREFERRED: dict[str, str] = {
        DEEP_REASONING_MODE: "kimi-k2.6:cloud",
        CODING_REPAIR_MODE:  "qwen3.5:cloud",
        FALLBACK_SAFE_MODE:  "minimax-m2.7:cloud",
    }
    _cloud_ok = online and bool(_g_dict.get("_ollama_cloud_reachable"))
    _cloud_preferred_model: str | None = None
    if _cloud_ok and winner not in _LOCAL_ONLY_MODES and not override_model:
        _preferred_cloud = _CLOUD_PREFERRED.get(winner)
        if _preferred_cloud and (available is None or _preferred_cloud in (available or frozenset())):
            _cloud_preferred_model = _preferred_cloud
            primary_warm = _preferred_cloud
            warm_note = f"cloud_preferred_for_{winner}"
            signals.append(f"cloud_preferred={_preferred_cloud}")

    if override_model:
        candidate_model = override_model
        warm_note = "user_override_model_tag"
        signals.append("override_model_tag")
        if available is not None and candidate_model not in available:
            candidate_model, ov_note = _pick_available(
                candidate_model,
                explicit_fallback,
                cfg.global_fallback_model,
                available,
            )
            warm_note = f"user_override_unavailable_clamped|{ov_note}"
            signals.append("override_model_clamped_to_available")
    else:
        candidate_model = primary_warm

    # Social mode should always target the social chat model directly.
    if winner == SOCIAL_CHAT_MODE and not override_model:
        candidate_model = str(cfg.social_chat_model).strip() or "mistral:7b"
        warm_note = "force_social_chat_model"

    # Ordered fallback label for transparency (legacy path when registry thin)
    _, avail_note_legacy = _pick_available(
        candidate_model,
        explicit_fallback,
        cfg.global_fallback_model,
        available,
    )

    selected = candidate_model
    # When discovery succeeded, never leave routing on a tag we know is absent.
    availability_clamp_note = ""
    if available is not None and selected not in available:
        selected, availability_clamp_note = _pick_available(
            selected,
            explicit_fallback,
            cfg.global_fallback_model,
            available,
        )
        signals.append("selection_clamped_to_available_set")
    initial_resolved_engine = selected
    stickiness_applied = False
    stickiness_reason = "none"
    cooldown_blocked = False

    floor = _mode_suitability_floor(winner, cfg)
    urgent = winner in _URGENT_MODES and win_score >= 0.34
    margin_bypass = margin >= float(cfg.routing_cooldown_bypass_margin)

    sel_entry = reg_by_name.get(selected)
    if sel_entry is None:
        sel_entry = _neutral_entry(selected, available=available is None or selected in (available or frozenset()))
    fit_candidate = _fit_for_mode(winner, sel_entry)

    last_entry: Optional[ModelCapabilityEntry] = None
    fit_last = 0.0
    last_mode = str(g.get("_routing_last_cognitive_mode") or "").strip() if isinstance(g, dict) else ""
    if last_effective:
        last_entry = reg_by_name.get(last_effective)
        if last_entry is None and (
            available is None or last_effective in (available or frozenset())
        ):
            last_entry = _neutral_entry(last_effective, available=True)
        if last_entry is not None:
            fit_last = _fit_for_mode(winner, last_entry)

    weak_mode = margin < float(cfg.routing_weak_mode_margin_stick) + social_res * 0.55
    gain_needed = float(cfg.routing_min_switch_gain) + social_res

    if (
        last_effective
        and available is not None
        and last_effective in available
        and last_entry is not None
        and not override_model
        and last_mode == winner
    ):
        gain = fit_candidate - fit_last
        stay_viable = fit_last >= floor

        if stay_viable and weak_mode and gain < gain_needed:
            selected = last_effective
            stickiness_applied = True
            stickiness_reason = "weak_mode_margin_and_low_gain"
        elif stay_viable and gain < gain_needed and winner not in _URGENT_MODES:
            selected = last_effective
            stickiness_applied = True
            stickiness_reason = "insufficient_fit_gain_vs_last_engine"

    # Cooldown (anti-thrashing): avoid rapid oscillation unless urgent or strong margin
    last_switch_mono = float(g.get("_routing_last_switch_monotonic", 0.0)) if isinstance(g, dict) else 0.0
    cooldown = float(cfg.routing_switch_cooldown_seconds)
    since_switch = time.monotonic() - last_switch_mono if last_switch_mono > 0 else cooldown + 1.0
    if (
        isinstance(g, dict)
        and last_effective
        and selected != last_effective
        and available is not None
        and last_effective in available
        and last_entry
        and not override_model
        and last_mode == winner
    ):
        if (
            since_switch < cooldown
            and not urgent
            and not margin_bypass
            and _fit_for_mode(winner, last_entry) >= floor
        ):
            selected = last_effective
            cooldown_blocked = True
            stickiness_applied = True
            stickiness_reason = "switch_cooldown_active"

    if available is not None and selected not in available:
        selected, availability_clamp_note = _pick_available(
            selected,
            explicit_fallback,
            cfg.global_fallback_model,
            available,
        )
        signals.append("post_stickiness_availability_clamp")

    engine_changed_from_last = bool(last_effective) and last_effective != (selected or "")

    latency_priority, context_priority, quality_priority = _priorities_for_mode(winner)

    cand_models: list[ModelRouteCandidate] = []
    for m, sc in ranked[:5]:
        nm = _mode_to_model_name(m, cfg)
        cand_models.append(ModelRouteCandidate(model_name=nm, cognitive_mode=m, score=float(sc), reason="score_rank"))

    cm_result = CognitiveModeResult(
        cognitive_mode=winner,
        classification_confidence=routing_confidence,
        signals=list(dict.fromkeys(signals))[:28],
    )

    if not last_effective:
        routing_transition = "initial"
        switch_reason_explain = f"bootstrap_selection|{warm_note}"
        no_switch_reason_explain = ""
    elif engine_changed_from_last:
        routing_transition = "changed"
        switch_reason_explain = f"adopt_engine|warm={warm_note}|legacy={avail_note_legacy}"
        if availability_clamp_note:
            switch_reason_explain += f"|clamp={availability_clamp_note}"
        no_switch_reason_explain = ""
    else:
        routing_transition = "hold"
        switch_reason_explain = ""
        if stickiness_applied:
            no_switch_reason_explain = stickiness_reason
        else:
            no_switch_reason_explain = "same_engine_as_last_turn|no_switch_needed"

    reason = (
        f"mode={winner} score={win_score:.3f} margin={margin:.3f} transition={routing_transition}; "
        f"online={online} cloud_preferred={_cloud_preferred_model or 'no'}; "
        f"switch_explain={switch_reason_explain or '-'} "
        f"no_switch_explain={no_switch_reason_explain or '-'}; "
        f"discovery={discovery_source}"
    )

    notes = [
        "Ava identity, persona, memory access, and relationship continuity are unchanged; "
        "only the Ollama inference tag may differ.",
        "Anti-thrashing prefers the previous effective model when it remains suitable and the cognitive "
        "classification margin is weak, or within a short cooldown window.",
    ]

    available_names = sorted(available) if available else []
    digest_sample: Optional[dict[str, Any]] = None
    if selected and discovery_source != "unavailable":
        digest_sample = try_fetch_model_card_digest(selected)

    meta = {
        "discovery_source": discovery_source,
        "availability_unknown": available is None,
        "available_count": len(available_names),
        "available_names_sample": available_names[:24],
        "discovered_models": list(available_names[:48]),
        "warm_resolution": warm_note,
        "legacy_availability": avail_note_legacy,
        "margin": round(margin, 4),
        "social_switch_resistance": round(social_res, 4),
        "engine_changed_from_last_turn": engine_changed_from_last,
        "stickiness_applied": stickiness_applied,
        "stickiness_reason": stickiness_reason,
        "cooldown_blocked": cooldown_blocked,
        "urgent_mode": urgent,
        "margin_bypass_cooldown": margin_bypass,
        "score_by_mode": {k: round(float(v), 4) for k, v in scores.items()},
        "override_mode": override_mode,
        "override_model": bool(override_model),
        "fit_last_vs_candidate": round(fit_last - fit_candidate, 4) if last_entry else None,
        "registry_size": len(registry),
        "model_card_digest": digest_sample,
        "capability_registry": [
            {
                "model": e.model_name,
                "available": e.available,
                "latency_t": round(e.latency_tendency, 3),
                "reasoning": round(e.reasoning_strength, 3),
                "coding": round(e.coding_suitability, 3),
                "summarize": round(e.summarization_suitability, 3),
                "fallback_pri": e.fallback_priority,
                "modes": list(e.cognitive_modes)[:8],
                "src": e.source,
            }
            for e in registry[:32]
        ],
        "routing_transition": routing_transition,
        "switch_reason": switch_reason_explain,
        "no_switch_reason": no_switch_reason_explain,
        "initial_resolved_engine": initial_resolved_engine,
        "availability_clamp": availability_clamp_note or None,
    }

    res = ModelRoutingResult(
        classification=cm_result,
        cognitive_mode=winner,
        selected_model=selected,
        fallback_model=explicit_fallback or cfg.global_fallback_model,
        routing_reason=reason,
        routing_confidence=routing_confidence,
        latency_priority=latency_priority,
        context_priority=context_priority,
        quality_priority=quality_priority,
        model_candidates=cand_models,
        continuity_preserved=True,
        notes=notes,
        meta=meta,
    )

    # Bootstrap model preference tracking — Ava develops her own sense of when cloud is worth it
    _track_model_preference(selected, winner, online, _g_dict)

    discovered_compact = ",".join(available_names[:40])
    if len(discovered_compact) > 260:
        discovered_compact = discovered_compact[:257] + "..."

    print(
        f"[model_routing] discovered_models n={len(available_names)} source={discovery_source} "
        f"models=[{discovered_compact}]"
    )
    print(
        f"[model_routing] selected={selected} fallback={explicit_fallback} "
        f"transition={routing_transition}"
    )
    if routing_transition == "hold":
        print(
            f"[model_routing] no_switch_reason={no_switch_reason_explain} "
            f"anti_thrash_state={stickiness_reason} continuity_preserved=yes"
        )
    elif routing_transition == "changed":
        print(f"[model_routing] switch_reason={switch_reason_explain} continuity_preserved=yes")
    else:
        print(f"[model_routing] switch_reason={switch_reason_explain} continuity_preserved=yes")

    print(
        f"[model_routing] mode={winner} conf={routing_confidence:.2f} margin={margin:.3f} "
        f"engine_changed_from_prior={engine_changed_from_last}"
    )
    print(f"[model_routing] detail={reason[:380]}")

    if isinstance(g, dict):
        prev_sel = str(g.get("_routing_last_effective_model") or "").strip()
        g["_routing_last_effective_model"] = selected
        g["_routing_last_cognitive_mode"] = winner
        if prev_sel != selected:
            g["_routing_last_switch_monotonic"] = time.monotonic()

    return res


def _trunc_route_reason(s: str, n: int = 220) -> str:
    t = " ".join((s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def resolve_model_for_execution_path(
    path: str,
    g: dict[str, Any] | None,
    *,
    user_text: str = "",
    config: Optional[ModelRoutingConfig] = None,
    commit_to_globals: bool = True,
) -> tuple[str, str, str]:
    """
    Pick an Ollama model tag for non-pipeline callers (initiative, memory annotation, event extract, etc.).

    Reuses availability discovery, capability registry, warm resolution, stickiness, and cooldown
    policies from :func:`build_model_routing_result` without requiring a full perception bundle.
    Returns ``(selected_model, cognitive_mode, compact_reason)``.
    """
    cfg = config or MODEL_ROUTING_CONFIG
    profiles = DEFAULT_MODEL_CAPABILITY_PROFILES
    ut = (user_text or "").strip()
    ut_low = ut.lower()

    mode = FALLBACK_SAFE_MODE
    path_l = (path or "default").strip().lower()
    if path_l in ("initiative", "autonomous_initiative", "proactive"):
        mode = SOCIAL_CHAT_MODE
    elif path_l in ("prospective_events", "event_extract", "calendar_extract"):
        mode = MEMORY_MAINTENANCE_MODE
    elif path_l in ("memory_metadata", "memory_tagging", "autoremember"):
        mode = MEMORY_MAINTENANCE_MODE
    elif path_l in ("beliefs_narrative", "self_narrative", "narrative_llm"):
        mode = MEMORY_MAINTENANCE_MODE
    elif path_l in ("workbench", "coding", "repair", "patch"):
        mode = CODING_REPAIR_MODE
    elif path_l in ("reflection_digest", "reflection_heavy", "deep_aux"):
        mode = DEEP_REASONING_MODE
    elif path_l in ("voice_aux",):
        mode = SOCIAL_CHAT_MODE

    code_kw = ("traceback", "syntaxerror", "patch", "diff", ".py", "compile", "exception")
    if any(k in ut_low for k in code_kw):
        mode = CODING_REPAIR_MODE
    mem_kw = ("summarize memory", "prune", "forget old", "dedupe memories")
    if any(k in ut_low for k in mem_kw):
        mode = MEMORY_MAINTENANCE_MODE

    available, discovery_source = discover_available_model_tags(force=False)
    _fast_g: dict[str, Any] = {}
    registry = build_runtime_capability_registry(available, profiles, g=_fast_g)
    reg_by_name = _registry_map(registry)

    primary_warm, warm_note = _resolve_warm_model_for_mode(mode, cfg, registry, available)
    explicit_fallback = (cfg.global_fallback_model or cfg.default_model or "").strip()

    selected = primary_warm
    if available is not None and selected not in available:
        selected, clamp_note = _pick_available(selected, explicit_fallback, cfg.global_fallback_model, available)
        warm_note = f"{warm_note}|clamp={clamp_note}"

    last_effective: Optional[str] = None
    if isinstance(g, dict):
        le = g.get("_routing_last_effective_model")
        if isinstance(le, str) and le.strip():
            last_effective = le.strip()

    floor = _mode_suitability_floor(mode, cfg)
    urgent = mode in _URGENT_MODES
    gain_needed = float(cfg.routing_min_switch_gain)

    sel_entry = reg_by_name.get(selected)
    if sel_entry is None:
        sel_entry = _neutral_entry(
            selected, available=available is None or selected in (available or frozenset())
        )
    fit_candidate = _fit_for_mode(mode, sel_entry)

    last_entry: Optional[ModelCapabilityEntry] = None
    fit_last = 0.0
    if last_effective:
        last_entry = reg_by_name.get(last_effective)
        if last_entry is None and (available is None or last_effective in (available or frozenset())):
            last_entry = _neutral_entry(last_effective, available=True)
        if last_entry is not None:
            fit_last = _fit_for_mode(mode, last_entry)

    stick_reason = "none"
    if (
        last_effective
        and last_entry is not None
        and available is not None
        and last_effective in available
        and fit_last >= floor
        and (fit_candidate - fit_last) < gain_needed
        and not urgent
    ):
        selected = last_effective
        stick_reason = "prefer_last_engine_branch"

    last_switch_mono = float(g.get("_routing_last_switch_monotonic", 0.0)) if isinstance(g, dict) else 0.0
    cooldown = float(cfg.routing_switch_cooldown_seconds)
    since_switch = time.monotonic() - last_switch_mono if last_switch_mono > 0 else cooldown + 1.0
    if (
        isinstance(g, dict)
        and last_effective
        and selected != last_effective
        and last_entry is not None
        and available is not None
        and last_effective in available
        and since_switch < cooldown
        and not urgent
        and fit_last >= floor
    ):
        selected = last_effective
        stick_reason = "branch_cooldown_hold"

    if available is not None and selected not in available:
        selected, _ = _pick_available(selected, explicit_fallback, cfg.global_fallback_model, available)

    reason = f"path={path_l} mode={mode} warm={warm_note} stick={stick_reason} discovery={discovery_source}"

    if commit_to_globals or path_l not in ("prospective_events", "event_extract", "calendar_extract"):
        print(f"[model_routing] path={path_l} selected={selected} reason={_trunc_route_reason(reason)}")

    if commit_to_globals and isinstance(g, dict):
        prev_sel = str(g.get("_routing_last_effective_model") or "").strip()
        g["_routing_last_effective_model"] = selected
        g["_routing_last_cognitive_mode"] = mode
        if prev_sel != selected:
            g["_routing_last_switch_monotonic"] = time.monotonic()

    return selected, mode, reason


def apply_model_routing_to_perception_state(state: Any, routing: ModelRoutingResult | None) -> None:
    """Copy routing snapshot onto PerceptionState (safe defaults if missing)."""
    if routing is None:
        state.cognitive_mode = FALLBACK_SAFE_MODE
        state.routing_selected_model = ""
        state.routing_fallback_model = ""
        state.routing_reason = "model_routing_unavailable"
        state.routing_confidence = 0.0
        state.routing_meta = {}
        return
    state.cognitive_mode = routing.cognitive_mode
    state.routing_selected_model = routing.selected_model
    state.routing_fallback_model = routing.fallback_model
    state.routing_reason = routing.routing_reason
    state.routing_confidence = float(routing.routing_confidence)
    state.routing_meta = {
        "latency_priority": routing.latency_priority,
        "context_priority": routing.context_priority,
        "quality_priority": routing.quality_priority,
        "continuity_preserved": routing.continuity_preserved,
        "classification_signals": list(routing.classification.signals),
        "notes": list(routing.notes),
        **dict(routing.meta or {}),
    }



def propose_routing_adjustment(mode: str, adjustment: str, reason: str, g: dict) -> dict:
    """Phase 68: Ava suggests a routing adjustment. Stored for Zeke review."""
    import json, time
    from pathlib import Path
    base = Path(g.get("BASE_DIR") or ".")
    proposal = {"ts": time.time(), "mode": mode, "adjustment": adjustment[:300], "reason": reason[:300], "status": "pending"}
    p = base / "state" / "routing_proposals.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(proposal, ensure_ascii=False) + "\n")
    return {"ok": True, "proposal": proposal}
