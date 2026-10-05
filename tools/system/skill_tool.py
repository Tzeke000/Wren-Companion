"""skill — my library of VERIFIED recipes (brain/skill_library.py, Voyager-borrowed).

A recipe enters as `verified` only by citing an action-ledger id that closed success
with evidence (the ledger is the critic); otherwise it is `provisional` and hidden from
`find` until a successful use promotes it. Re-adding a name makes a new version.
`act open` already returns the top matching verified recipes with the lessons.

Actions:
  find     query [k] [include_provisional]   — top-k by description similarity
  get      name                              — current steps + version history + uses
  add      name description steps [verified_by] [notes]
  use      name action_id                    — link a use; its OUTCOME judges the recipe
  list
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _skill(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    from brain import skill_library as sl
    a = str(params.get("action") or "list").lower()
    try:
        if a == "find":
            return {"ok": True, "recipes": sl.find(str(params.get("query") or ""),
                                                   k=int(params.get("k", 5)),
                                                   include_provisional=bool(params.get("include_provisional")))}
        if a == "get":
            r = sl.get(str(params.get("name") or ""))
            return {"ok": r is not None, "recipe": r}
        if a == "add":
            return sl.add(str(params.get("name") or ""), str(params.get("description") or ""),
                          params.get("steps") or [], verified_by=params.get("verified_by"),
                          notes=str(params.get("notes") or ""))
        if a == "use":
            return sl.record_use(str(params.get("name") or ""), str(params.get("action_id") or ""))
        if a == "list":
            return {"ok": True, "recipes": sl.list_all()}
    except (ValueError, KeyError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"unknown action {a!r}"}


register_tool(
    "skill",
    "VERIFIED RECIPE LIBRARY (Voyager): find | get | add | use | list. A recipe is 'verified' only "
    "with a ledger action that closed success with evidence; provisional ones are hidden from find.",
    1,
    _skill,
)
