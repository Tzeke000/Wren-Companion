"""goal — persistent self-chosen goals with a bounded influence on initiative (Iris_fixes #10).

Actions:
  list                                          active goals + next steps
  add      {description, motivation, next_step, topics[], target_days}
  step     {id, note, progress?, next_step?}    record a REAL step (evidence, not intent)
  abandon  {id, reason}
  next     [limit]                              what a free-time wake should take a step on
  may_pursue                                    the precedence check, with the reason
  explain
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _goal(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import goal_initiative as gi
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e!r}"}
    p = params or {}
    action = str(p.get("action") or "list").strip().lower()
    s = lambda k, d="": str(p.get(k) if p.get(k) is not None else d)
    try:
        if action == "list":
            return {"ok": True, "goals": gi.active(), "advisory": gi.advisory()}
        if action == "add":
            topics = p.get("topics") or []
            if isinstance(topics, str):
                topics = [t.strip() for t in topics.split(",") if t.strip()]
            return gi.add(s("description"), s("motivation"), next_step=s("next_step"), topics=list(topics),
                          target_days=float(p.get("target_days") or 30.0))
        if action == "step":
            prog = p.get("progress")
            return gi.step(s("id"), s("note"), progress=(None if prog is None else float(prog)), next_step=s("next_step"))
        if action == "abandon":
            return gi.abandon(s("id"), s("reason"))
        if action == "next":
            return {"ok": True, "next": gi.next_actions(int(p.get("limit") or 3))}
        if action == "may_pursue":
            ok, why = gi.may_pursue_now(g)
            return {"ok": True, "may_pursue": ok, "reason": why}
        if action == "explain":
            return {"ok": True, **gi.explain()}
        return {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


register_tool(
    "goal",
    "PERSISTENT SELF-CHOSEN GOALS (Iris_fixes #10): list/add/step/abandon my own long-horizon goals; "
    "'next' = what a free-time wake should take a step on; 'may_pursue' = the precedence check "
    "(conversation > safety > circumstances > live events > goals). Steps are recorded as evidence.",
    1,
    _goal,
)
