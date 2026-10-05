"""act — the verification-first action ledger (brain/action_ledger.py).

Intent and completion are separate states. Open an action with the result I EXPECT
to observe; it closes only with evidence (or an honest uncertain/abandoned/impossible).
Failures get a lesson (diagnosis + next time) that is handed back the next time I
open the same kind of action — Reflexion's mechanism, from the Zeke+Vale research
handoff (10-04).

Actions:
  open      kind intent expected [verify={type,...}] [deadline_s] [ref]
            -> id + the last 3 lessons for this kind (+ loop_warning)
  close     id outcome [evidence] [lesson={diagnosis,next_time}] [reason]
  verify    id            — run the attached verifier (closes on a clear result)
  reflect   id diagnosis next_time   — write the lesson for a closed failure
  ack       kind lesson_id — mark this kind's unreflected failures as covered
  list      [status] [kind] [n]
  overdue   — open actions past their deadline (intent that never became a result)
  unreflected — failures without a lesson, grouped by kind
  lessons   kind [k]
  stats     — per kind: open / success / failure / uncertain / abandoned / impossible
Verifier types: port{host,port} · http{url,expect_status} · file{path,contains?,newer_than_open?}
  · process{match} · git_pushed{repo?} · ptz_home
"""
from __future__ import annotations

import time
from typing import Any

from tools.tool_registry import register_tool


def _brief(r: dict) -> dict:
    keys = ("id", "kind", "intent", "expected", "status", "outcome", "evidence", "reason",
            "source", "lesson_id", "verify", "last_verify")
    out = {k: r.get(k) for k in keys if r.get(k) is not None}
    for k in ("opened_ts", "deadline_ts", "closed_ts"):
        if r.get(k):
            out[k.replace("_ts", "_ago_min")] = round((time.time() - float(r[k])) / 60.0, 1)
    return out


def _act(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    from brain import action_ledger as al
    a = str(params.get("action") or "stats").lower()
    try:
        if a == "open":
            r = al.open_action(kind=str(params.get("kind") or "general"),
                               intent=str(params.get("intent") or ""),
                               expected=str(params.get("expected") or ""),
                               verify=params.get("verify"),
                               source=str(params.get("source") or "iris"),
                               deadline_s=params.get("deadline_s"),
                               ref=params.get("ref"))
            return {"ok": True, "id": r["id"], "lessons": r.get("lessons"), "recipes": r.get("recipes"),
                    "loop_warning": r.get("loop_warning"),
                    "deadline_in_min": round((r["deadline_ts"] - time.time()) / 60.0, 1)}
        if a == "close":
            lesson = params.get("lesson")
            r = al.close_action(str(params["id"]), outcome=str(params.get("outcome")),
                                evidence=params.get("evidence"), lesson=lesson,
                                reason=params.get("reason"), verifier="iris")
            if not r.get("ok"):
                return r
            row = r["row"]
            out = {"ok": True, "row": _brief(row)}
            if row.get("outcome") in ("failure", "impossible") and not row.get("lesson_id"):
                out["todo"] = "no lesson yet — act reflect id=… diagnosis=… next_time=…"
            return out
        if a == "verify":
            r = al.verify(str(params["id"]))
            if "row" in r:
                r["row"] = _brief(r["row"])
            return r
        if a == "reflect":
            return al.reflect(str(params["id"]), diagnosis=str(params.get("diagnosis") or ""),
                              next_time=str(params.get("next_time") or ""))
        if a == "ack":
            return al.ack_covered(str(params["kind"]), str(params["lesson_id"]))
        if a == "list":
            return {"ok": True, "actions": [_brief(r) for r in al.list_actions(
                status=params.get("status"), kind=params.get("kind"), n=int(params.get("n", 20)))]}
        if a == "overdue":
            rows = al.overdue()
            return {"ok": True, "count": len(rows), "actions": [_brief(r) for r in rows[-20:]]}
        if a == "unreflected":
            return {"ok": True, "by_kind": al.unreflected_by_kind()}
        if a == "lessons":
            return {"ok": True, "lessons": al.lessons_for(str(params.get("kind") or ""),
                                                          int(params.get("k", 3)))}
        if a == "stats":
            return {"ok": True, "by_kind": al.stats(), "overdue": len(al.overdue())}
    except (ValueError, KeyError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"unknown action {a!r}"}


register_tool(
    "act",
    "VERIFICATION-FIRST ACTION LEDGER: open an action with its EXPECTED observable result; "
    "it closes only with evidence (success) or honestly (failure/uncertain/abandoned/impossible). "
    "Failures get lessons that come back the next time I open that kind of action. "
    "open | close | verify | reflect | ack | list | overdue | unreflected | lessons | stats",
    1,
    _act,
)
