"""belief — the evidence → confidence → belief → revision lifecycle (Iris_fixes #8 + addition B).

Actions:
  recall     {query, kind?}                       what do I currently believe (flags under_review/stale)
  hold       {subject, statement, kind, source_kind, source_ref, confidence?, note}
  challenge  {subject, evidence, source_kind, source_ref, note, proposed?, decisive?}
             decisive=true ONLY for Zeke's word about his own life/body/self (08-30 domain split)
  revise     {subject, new_statement, reason, source_kind, source_ref, confidence?}
  retire     {subject, reason}
  reconsider                                       what needs a second look (under_review / stale / contested)
  history    {subject}
  explain
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _belief(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import belief_lifecycle as bl
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e!r}"}
    p = params or {}
    action = str(p.get("action") or ("recall" if p.get("query") is not None else "explain")).strip().lower()
    s = lambda k, d="": str(p.get(k) if p.get(k) is not None else d)
    try:
        if action == "recall":
            return bl.recall(s("query"), kind=(s("kind") or None), include_retired=bool(p.get("include_retired", False)),
                             limit=int(p.get("limit") or 10))
        if action == "hold":
            conf = p.get("confidence")
            return bl.hold(s("subject"), s("statement"), kind=s("kind", "world"), source_kind=s("source_kind", "observation"),
                           source_ref=s("source_ref"), confidence=(None if conf is None else float(conf)), note=s("note"))
        if action == "challenge":
            return bl.challenge(s("subject"), s("evidence"), source_kind=s("source_kind", "observation"),
                                source_ref=s("source_ref"), note=s("note"), proposed=(s("proposed") or None),
                                decisive=bool(p.get("decisive", False)))
        if action == "revise":
            conf = p.get("confidence")
            return bl.revise(s("subject"), s("new_statement"), reason=s("reason"), source_kind=s("source_kind", "observation"),
                             source_ref=s("source_ref"), confidence=(None if conf is None else float(conf)))
        if action == "retire":
            return bl.retire(s("subject"), reason=s("reason"))
        if action == "reconsider":
            return bl.reconsider()
        if action == "history":
            return bl.history(s("subject"))
        if action == "explain":
            return {"ok": True, **bl.explain()}
        return {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


register_tool(
    "belief",
    "BELIEF LIFECYCLE (Iris_fixes #8): hold/confirm/challenge/revise/retire beliefs about the world, a "
    "person, or myself with provenance + confidence; one contradiction never revises; revisions keep "
    "previous+new+reason+evidence. recall = what I believe now; reconsider = what needs a second look.",
    1,
    _belief,
)
