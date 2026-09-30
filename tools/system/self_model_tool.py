"""self_model — competing self-hypotheses (Iris_fixes #9 + addition C).

Actions:
  status   [trait]                                   verdict per trait: undistinguished / leaning / stabilised
  propose  {trait, question, hypotheses: {key: statement}, source_ref}   ≥2 rivals required
  observe  {trait, text, supports: [keys], source_ref}   supporting ALL rivals = kept, non-discriminating
  advisory                                           the block cognition sees (advisory, not identity)
  explain
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _self_model(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import self_hypotheses as sh
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e!r}"}
    p = params or {}
    action = str(p.get("action") or "status").strip().lower()
    try:
        if action == "status":
            return sh.status(str(p.get("trait") or "") or None)
        if action == "propose":
            hyps = p.get("hypotheses") or {}
            if not isinstance(hyps, dict):
                return {"ok": False, "error": "hypotheses must be an object {key: statement}"}
            return sh.propose(str(p.get("trait") or ""), str(p.get("question") or ""), hyps,
                              source_ref=str(p.get("source_ref") or ""))
        if action == "observe":
            sup = p.get("supports") or []
            if isinstance(sup, str):
                sup = [s.strip() for s in sup.split(",") if s.strip()]
            return sh.observe(str(p.get("trait") or ""), str(p.get("text") or ""), supports=list(sup),
                              source_ref=str(p.get("source_ref") or ""))
        if action == "advisory":
            return {"ok": True, "advisory": sh.advisory()}
        if action == "explain":
            return {"ok": True, **sh.explain()}
        return {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


register_tool(
    "self_model",
    "SELF-MODEL AS COMPETING HYPOTHESES (Iris_fixes #9 / addition C): a trait is a question with rival "
    "hypotheses; observations discriminate or don't; stabilises only on repeated evidence over weeks; "
    "always advisory — IDENTITY.md outranks it. Actions: status | propose | observe | advisory | explain.",
    1,
    _self_model,
)
