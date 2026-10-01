"""wake_verdict — write back what I decided about an eyes-wake (Iris_fixes #5).

The loop: host wakes me with a SALIENCE line + signature → I look → I record
`acted` or `nothing` here → next time the host asks brain/wake_salience.judge(sig)
BEFORE waking me. Without this write the gate learns nothing; that is the whole
point of the tool existing (the counterfactual archive had a writer that never
wrote — this one raises on a bad call instead of swallowing it).

Actions:
  record  (default when `verdict` given)  params: signature, verdict[acted|nothing], note
  judge   what the gate would do for `signature` right now
  stats   counts + recent verdicts for `signature`
  recent  last N ledger rows (optionally filtered by signature)
  explain the rules and thresholds
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _wake_verdict(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import wake_salience as ws
    except Exception as e:
        return {"ok": False, "error": f"import failed: {e!r}"}
    p = params or {}
    action = str(p.get("action") or ("record" if p.get("verdict") else "explain")).strip().lower()
    sig = str(p.get("signature") or p.get("sig") or "").strip()
    try:
        if action == "record":
            verdict = str(p.get("verdict") or "").strip().lower()
            if verdict not in ws.EXPLICIT:
                return {"ok": False, "error": "verdict must be 'acted' or 'nothing'", "got": verdict}
            rec = ws.record(sig, verdict, note=str(p.get("note") or ""), source="cognition")
            after = ws.judge(sig)
            return {"ok": True, "recorded": rec, "gate_now": {"wake": after["wake"], "reason": after["reason"]},
                    "stats": after.get("stats", {})}
        if action == "judge":
            if not sig:
                return {"ok": False, "error": "signature required"}
            return {"ok": True, **ws.judge(sig)}
        if action == "stats":
            if not sig:
                return {"ok": False, "error": "signature required"}
            return {"ok": True, "stats": ws.stats(sig), "history": ws.history_line(sig)}
        if action == "recent":
            return {"ok": True, "rows": ws.recent(int(p.get("n") or 20), sig=sig or None)}
        if action == "explain":
            return {"ok": True, **ws.explain()}
        if action == "missed":
            # Zeke 2026-10-01: 'you can't perceive what didn't wake you ... look in the logs at what
            # didn't wake you'. brain/wake_missed reads eyes_raw/eyes commits/the salience ledger/the
            # arbiter ledger/the wifi log side by side and labels every episode that did NOT wake me.
            from brain import wake_missed as wmiss
            age = None
            try:
                ts = float(g.get("_host_eyes_poll_ts") or 0.0)
                age = (time.time() - ts) if ts > 0 else None
            except Exception:
                age = None
            res = wmiss.missed(float(p.get("hours") or 3.0), host_eyes_poll_age_s=age)
            res["summary"] = wmiss.summary_line(res)
            return res
        return {"ok": False, "error": f"unknown action {action!r}"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


register_tool(
    "wake_verdict",
    "WRITE BACK my verdict on an eyes-wake so the wake gate can learn (Iris_fixes #5). "
    "params: signature (from the SALIENCE line in the wake prompt), verdict 'acted'|'nothing', "
    "note. Other actions: judge | stats | recent | explain | missed [hours=3] (what did NOT wake me: episodes + reasons, "
    "dead host eyes, arrivals unseen, unowned situations). An unrecorded verdict teaches nothing.",
    1,
    _wake_verdict,
)
