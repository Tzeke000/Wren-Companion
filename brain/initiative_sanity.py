from typing import List, Dict, Any


def desaturate_candidate_scores(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    adjusted = []
    seen_by_kind = {}
    for cand in candidates or []:
        c = dict(cand)
        kind = c.get("kind", "unknown")
        final = float(c.get("final", c.get("score", 0.0)) or 0.0)
        seen = seen_by_kind.get(kind, 0)
        if final >= 0.99:
            final = 0.94 - min(0.18, seen * 0.03)
        if c.get("goal") == "maintain_connection" and kind in {"current_goal", "recent_reflection"}:
            final = max(0.0, final - min(0.12, seen * 0.02))
        c["final"] = round(final, 4)
        c["score"] = round(float(c.get("score", c["final"]) or c["final"]), 4)
        adjusted.append(c)
        seen_by_kind[kind] = seen + 1
    return adjusted


def maybe_desaturate_args(args, kwargs):
    new_args = list(args)
    desaturated = False
    if new_args and isinstance(new_args[0], list):
        new_args[0] = desaturate_candidate_scores(new_args[0])
        desaturated = True
    elif isinstance(kwargs.get("candidates"), list):
        kwargs = dict(kwargs)
        kwargs["candidates"] = desaturate_candidate_scores(kwargs["candidates"])
        desaturated = True
    return tuple(new_args), kwargs, desaturated


def sanitize_candidate_result(result, g: dict | None = None):
    if not isinstance(result, tuple) or len(result) < 3:
        return result
    chosen, reason, state = result[0], result[1], result[2]
    if not isinstance(chosen, dict):
        return result
    kind = str(chosen.get("kind", ""))
    score = float(chosen.get("score", chosen.get("final", 0.0)) or 0.0)
    active_goal = ""
    confidence = None
    if isinstance(state, dict):
        active_goal = str(state.get("active_goal", ""))
        try:
            confidence = float(state.get("confidence", 0.0))
        except Exception:
            confidence = None
    if active_goal == "maintain_connection" and kind in {"current_goal", "recent_reflection"}:
        if score >= 0.99 and (confidence is None or confidence <= 0.45):
            return (None, "stage6_1_holdback_saturated_maintain_connection", state)
    return result
