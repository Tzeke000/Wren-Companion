# SELF_ASSESSMENT: I am the little-brain flight recorder — one JSON line per
# cerebellum turn, written AT TURN TIME (grading spec: capture cannot be
# retrofitted). I must NEVER break a turn: every public call is try/except-
# swallowed; a lost record is acceptable, a broken reply is not.
"""
lb_turn_capture — evidence-bundle capture for the little-brain grading loop.

Schema: docs/grading_capture_schema_v0.md (v0.1 after Zeke's 2026-07-27
amendments). Spec: memory/little_brain_grading_loop_spec_2026-07-25.md +
little_brain_grading_rulings_2026-07-27.md. Wired into
scripts/vector_brain_server.py (chat_completions + _ask_local tool loop).

Records land in state/little_brain/turn_log/YYYY-MM-DD.jsonl (daily rotation,
Zeke amendment #3: bounded graders work yesterday's closed file; watermark is
(date, turn_id)).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_V = "0.1"
_EXCERPT_CAP = 500          # amendment #1
_EXCERPT_HEAD = 300
_EXCERPT_TAIL = 180         # head+tail: malformed output usually breaks at the END
_FULL_RESULT_TOOLS = {"senses_now"}  # amendment #1: never clip grounding values
_LONG_OUTPUT_CHARS = 400
_LIVE_AGE_S = 2.0           # 15Hz feed: <2s = live
_STALE_AGE_S = 30.0

_lock = threading.Lock()
_root = Path(__file__).resolve().parent.parent


def _log_dir() -> Path:
    return _root / "state" / "little_brain" / "turn_log"


def _excerpt(tool_name: str, text: str) -> dict[str, Any]:
    """Amendment #1: head+tail truncation with provenance; grounding tools
    (senses_now) are stored whole so the grader keeps the exact values."""
    text = str(text)
    n = len(text)
    if tool_name in _FULL_RESULT_TOOLS or n <= _EXCERPT_CAP:
        return {"result": text, "truncated": False, "orig_len": n}
    return {
        "result": text[:_EXCERPT_HEAD] + " …[cut]… " + text[-_EXCERPT_TAIL:],
        "truncated": True,
        "orig_len": n,
    }


def _senses_snapshot() -> dict[str, Any]:
    """Verbatim copy of senses_live.json AT STIMULUS TIME + freshness verdict.
    Fossil detection is deliberately NOT here — it is a structural cross-record
    check that belongs to the grader (ruling #4: tolerance and fossil stay
    strictly separate; capture just preserves the evidence)."""
    out: dict[str, Any] = {"snapshot": None,
                           "freshness": {"age_ms": None, "verdict": "absent"}}
    try:
        p = _root / "state" / "vector" / "senses_live.json"
        if not p.is_file():
            return out
        age_s = max(0.0, time.time() - p.stat().st_mtime)
        out["snapshot"] = json.loads(p.read_text(encoding="utf-8"))
        out["freshness"] = {
            "age_ms": int(age_s * 1000),
            "verdict": ("live" if age_s < _LIVE_AGE_S
                        else "stale" if age_s < _STALE_AGE_S else "absent"),
        }
    except Exception:
        pass
    return out


def new_turn(stimulus_clean: str, raw_stt: str | None, lane: str,
             model: str) -> dict[str, Any]:
    """Open a turn record. Senses snapshot is taken HERE (stimulus time, not
    response time — grounding is judged against what she COULD have read)."""
    now = time.time()
    return {
        "schema_v": SCHEMA_V,
        "turn_id": ("lb_" + time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
                    + "_" + uuid.uuid4().hex[:4]),
        "ts": now,
        "model": model,
        "source": lane,
        "stimulus": {"raw_stt": raw_stt, "clean": str(stimulus_clean)[:2000]},
        "tools": [],
        "response": None,
        "answered_by": None,
        "latency_ms": {"total": None, "first_llm_hop": None,
                       "first_tool_call": None},
        "senses": _senses_snapshot(),
        "escalated": False,
        "flags": [],
        "_t0": now,          # stripped before write
    }


def log_tool(turn: dict[str, Any], name: str, args: Any, ok: bool,
             t_ms: int, result: str) -> None:
    try:
        if turn.get("latency_ms", {}).get("first_tool_call") is None:
            turn["latency_ms"]["first_tool_call"] = int(
                (time.time() - turn["_t0"]) * 1000) - t_ms
        entry = {"name": str(name), "args": args, "ok": bool(ok),
                 "t_ms": int(t_ms)}
        entry.update(_excerpt(str(name), result))
        turn["tools"].append(entry)
        if name == "ask_big_iris":
            turn["escalated"] = True
    except Exception:
        pass


def log_first_hop(turn: dict[str, Any], t_ms: int) -> None:
    """Amendment #2 caveat: the local Ollama call is NON-streaming, so true
    TTFT is not observable; first_llm_hop (hop-0 completion) is the honest
    proxy until the call is switched to streaming. Documented in the schema."""
    try:
        if turn.get("latency_ms", {}).get("first_llm_hop") is None:
            turn["latency_ms"]["first_llm_hop"] = int(t_ms)
    except Exception:
        pass


def finish(turn: dict[str, Any], response: str | None, answered_by: str,
           hop_limit_hit: bool = False) -> None:
    """Close + append the record. Swallows everything — a turn must never
    fail because its recorder did."""
    try:
        resp = str(response or "")
        turn["response"] = resp[:2000]
        turn["answered_by"] = answered_by
        turn["latency_ms"]["total"] = int((time.time() - turn["_t0"]) * 1000)

        flags: list[str] = []
        names = [t["name"] for t in turn["tools"]]
        if any(not t["ok"] for t in turn["tools"]):
            flags.append("tool_error")
        if len(names) != len(set(names)):
            flags.append("tool_retry")
        if hop_limit_hit:
            flags.append("hop_limit")
        low = resp.lower()
        if any(m in low for m in ("can't measure", "couldn't find",
                                  "ask for help", "can't check",
                                  "won't invent")):
            flags.append("refusal")
        if len(resp) > _LONG_OUTPUT_CHARS:
            flags.append("long_output")
        if turn["senses"]["freshness"]["verdict"] != "live":
            flags.append("senses_not_live")
        if answered_by == "canned":
            flags.append("canned_fallback")
        turn["flags"] = flags or ["none"]

        turn.pop("_t0", None)
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / (time.strftime("%Y-%m-%d") + ".jsonl")
        line = json.dumps(turn, ensure_ascii=False) + "\n"
        with _lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        try:
            print(f"[lb_capture] record dropped (non-fatal): {e!r}")
        except Exception:
            pass
