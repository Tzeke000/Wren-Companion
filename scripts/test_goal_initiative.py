"""Adversarial tests for brain/goal_initiative (Iris_fixes #10).

Goals must influence initiative only inside their defined, bounded points — and NEVER override
a conversation, safety, circumstances, or a live higher-priority event.

Run:  .venv\\Scripts\\python.exe scripts\\test_goal_initiative.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix="goal_init_")
os.environ["GOAL_INITIATIVE_BASE"] = _tmp
os.environ["ATTENTION_ARBITER_LEDGER"] = os.path.join(_tmp, "state", "attention", "situations.jsonl")
(Path(_tmp) / "state").mkdir(parents=True, exist_ok=True)

from brain import goal_initiative as gi  # noqa: E402
from brain import iris_transcript  # noqa: E402

iris_transcript.configure(Path(_tmp))   # empty transcript in the sandbox

RESULTS: list[tuple[str, bool, str]] = []
# a daytime, non-quiet-hours instant for deterministic precedence checks
NOW = time.mktime(time.strptime("2026-09-30 14:30:00", "%Y-%m-%d %H:%M:%S"))
NIGHT = time.mktime(time.strptime("2026-09-30 23:30:00", "%Y-%m-%d %H:%M:%S"))


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def fresh() -> None:
    for p in ("state/ava_goals.json", "state/goal_meta.json", "state/transcript.jsonl", "state/gpu_park.json", "state/body_pause.flag",
              "state/attention/situations.jsonl"):
        q = Path(_tmp) / p
        if q.exists():
            q.unlink()
    try:
        from brain import attention_arbiter as _arb
        _arb._CACHE["mtime"] = -1.0
    except Exception:
        pass


# 1. validation + store
fresh()
check("goal needs a why", gi.add("do x", "")["ok"] is False and gi.add("", "why")["ok"] is False)
r = gi.add("Close the eyes-wake loop", "so my verdicts are consumed", next_step="record a verdict on every wake for a week",
           topics=["wake_salience", "verdicts"])
check("add works (goal_system_v2 store)", r["ok"] and (Path(_tmp) / "state" / "ava_goals.json").exists())
gid = r["id"]
check("active lists it with next_step", gi.active()[0]["next_step"].startswith("record a verdict"))
check("no goals → advisory empty", True)  # placeholder ordering; real check below after fresh()

# 2. precedence: nothing else needs me → may pursue (daytime, empty transcript, no flags)
ok, why = gi.may_pursue_now({}, now=NOW)
check("idle daytime → may pursue", ok, why)
# conversation: a live voice session
ok, why = gi.may_pursue_now({"_voice_session_active": True}, now=NOW)
check("live voice session blocks goals", not ok and "conversation" in why, why)
# conversation: something said recently
iris_transcript.append(role="user", source="zeke", modality="voice", content="hey Iris")
ok, why = gi.may_pursue_now({}, now=time.time())
check("recent conversation blocks goals", not ok and "conversation" in why, why)
fresh(); gi.add("g", "why", next_step="s")
# circumstances: quiet hours
ok, why = gi.may_pursue_now({}, now=NIGHT)
check("quiet hours block goals", not ok and "quiet" in why, why)
# circumstances: parked body
(Path(_tmp) / "state" / "gpu_park.json").write_text(json.dumps({"parked": True}), encoding="utf-8")
ok, why = gi.may_pursue_now({}, now=NOW)
check("GPU-parked body blocks goals", not ok and "parked" in why, why)
(Path(_tmp) / "state" / "gpu_park.json").write_text("{corrupt", encoding="utf-8")
ok, why = gi.may_pursue_now({}, now=NOW)
check("unreadable park flag → goals WAIT (fail-safe)", not ok, why)
(Path(_tmp) / "state" / "gpu_park.json").unlink()
(Path(_tmp) / "state" / "body_pause.flag").write_text("1", encoding="utf-8")
ok, why = gi.may_pursue_now({}, now=NOW)
check("pause flag blocks goals", not ok and "pause" in why, why)
(Path(_tmp) / "state" / "body_pause.flag").unlink()
# higher-priority events: the arbiter says something needs cognition now
from brain import attention_arbiter as arb  # noqa: E402
arb.claim("unknown_person", "host-eyes", wake=True, now=NOW - 10)
ok, why = gi.may_pursue_now({}, now=NOW)
check("a live attention event blocks goals", not ok and "higher-priority" in why, why)
ok, why = gi.may_pursue_now({}, now=NOW + 600)
check("...and clears once it is old", ok, why)
# no goals → nothing to pursue
fresh()
ok, why = gi.may_pursue_now({}, now=NOW)
check("no active goals → nothing to pursue", not ok and "no active goals" in why)
check("advisory empty with no goals", gi.advisory(now=NOW) == "")

# 3. leisure candidate: bounded by precedence, the hourly gap, and the share
fresh()
r = gi.add("Understand why :5876's accept loop dies", "it has bitten me twice", next_step="read uvicorn's serve loop")
c = gi.leisure_candidate({}, now=NOW)
check("leisure candidate offered when free", c is not None and c["activity"] == "pursue_goal_step" and abs(c["weight_share"] - 1 / 3) < 1e-9)
gi.mark_leisure_goal(now=NOW)
check("not twice within the gap", gi.leisure_candidate({}, now=NOW + 600) is None)
check("offered again after the gap", gi.leisure_candidate({}, now=NOW + 3700) is not None)
check("no candidate in quiet hours", gi.leisure_candidate({}, now=NIGHT + 7200) is None)
check("no candidate without a next_step", (fresh(), gi.add("g", "why"), gi.leisure_candidate({}, now=NOW))[2] is None)

# 4. step records evidence; progress bounded; completion; abandon needs a reason
fresh()
gid = gi.add("Close the loop", "why", next_step="a")["id"]
check("step requires a note", gi.step(gid, "")["ok"] is False)
r = gi.step(gid, "recorded 12 verdicts today", progress=0.4, next_step="measure unrecorded ratio", now=NOW)
check("step records + advances", r["ok"] and r["goal"]["steps"] == 1 and abs(r["goal"]["progress"] - 0.4) < 1e-9 and r["goal"]["next_step"].startswith("measure"))
r = gi.step(gid, "done", progress=1.7, now=NOW)
check("progress bounded and completes at 1", r["goal"]["progress"] == 1.0 and r["goal"]["status"] == "completed")
check("completed goal is no longer active", gi.active() == [])
check("step on a completed goal errors", gi.step(gid, "x")["ok"] is False)
gid2 = gi.add("g2", "why", next_step="s")["id"]
check("abandon needs a reason", gi.abandon(gid2, "")["ok"] is False)
check("abandon works", gi.abandon(gid2, "no longer mine")["ok"] and gi.active() == [])

# 5. curiosity boost: bounded, by tag or by description words, 0 otherwise
fresh()
gi.add("Understand uvicorn accept-loop failures on Windows", "why", topics=["uvicorn", "asyncio"])
check("tagged topic boosted", abs(gi.curiosity_boost("how uvicorn handles sockets") - gi.CURIOSITY_BOOST) < 1e-9)
check("description-word overlap gets half boost", abs(gi.curiosity_boost("windows accept-loop failures") - gi.CURIOSITY_BOOST * 0.5) < 1e-9)
check("unrelated topic gets 0", gi.curiosity_boost("what Steam is like") == 0.0)
check("boost never exceeds the cap", gi.curiosity_boost("uvicorn asyncio accept-loop failures windows") <= gi.CURIOSITY_BOOST)

# 6. next_actions rotates: the least-recently-stepped goal comes first
fresh()
a = gi.add("A", "why", next_step="a1")["id"]
b = gi.add("B", "why", next_step="b1")["id"]
gi.step(a, "did a1", next_step="a2", now=NOW)
check("least-recently-stepped goal first", gi.next_actions(2)[0]["id"] == b)
adv = gi.advisory(now=NOW)
check("advisory names precedence and next steps", "wait behind conversation" in adv and "b1" in adv)

# 7. max goals honoured (goal_system_v2's cap)
fresh()
for i in range(12):
    r = gi.add("g%d" % i, "why")
check("store cap surfaces as an error, not silence", r["ok"] is False and "max" in r["error"])

# 8. the tool wrapper
try:
    from tools.system.goal_tool import _goal
    fresh()
    t = _goal({"action": "add", "description": "t", "motivation": "m", "next_step": "n", "topics": "x, y"}, {})
    check("tool add accepts comma topics", t.get("ok") and t["goal"]["topics"] == ["x", "y"])
    t = _goal({"action": "may_pursue"}, {"_voice_session_active": True})
    check("tool may_pursue reports the reason", t.get("ok") and t.get("may_pursue") is False and "conversation" in t["reason"])
    t = _goal({"action": "step", "id": "nope", "note": "x"}, {})
    check("tool surfaces errors", t.get("ok") is False)
except Exception as e:
    check("tool wrapper importable", False, repr(e))

n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
print("\n%d/%d passed" % (len(RESULTS) - n_fail, len(RESULTS)))
sys.exit(1 if n_fail else 0)
