import sys, tempfile
from pathlib import Path
sys.path.insert(0, r"D:\Wren-Companion")
from brain import planner as pl, action_ledger as al
from tools import tool_registry as tr
td = Path(tempfile.mkdtemp(dir=r"D:\Wren-Companion\scratch\tmp"))
al._DIR = td; al.ACTIONS_PATH = td/"a.json"; al.LESSONS_PATH = td/"l.jsonl"
tr._REGISTRY["_t_ok"] = tr.ToolDef(name="_t_ok", description="", tier=1, handler=lambda p, g: {"ok": True, "did": 1}) if hasattr(tr, "ToolDef") else None
tr._REGISTRY["_t_bad"] = tr.ToolDef(name="_t_bad", description="", tier=1, handler=lambda p, g: {"ok": False, "error": "nope"})
P = pl.LongHorizonPlanner(td)
plan = {"id": "p1", "goal": "g", "status": "active", "progress_notes": [], "steps": [
  {"id": "s0", "description": "run ok tool", "tool_to_use": "_t_ok", "status": "pending"},
  {"id": "s1", "description": "run bad tool", "tool_to_use": "_t_bad", "status": "pending"},
  {"id": "s2", "description": "tell Zeke the plan", "tool_to_use": "", "status": "pending"}]}
P._save([plan])
r0 = P.execute_next_step("p1"); r1 = P.execute_next_step("p1"); r2 = P.execute_next_step("p1")
print("s0", r0["status"], "| s1", r1["status"], "| s2", r2["status"], r2["result"])
w = P.execute_next_step("p1"); print("waiting:", w.get("waiting"))
aid = P.get_plan("p1")["steps"][2]["action_id"]
al.close_action(aid, outcome="success", evidence="Discord msg 123 sent")
d = P.execute_next_step("p1"); st = [s["status"] for s in P.get_plan("p1")["steps"]]
print("after evidence:", st, "| plan:", P.get_plan("p1")["status"], d)
ok = st == ["completed", "failed", "completed"] and P.get_plan("p1")["status"] == "failed"
print("PASS" if ok else "FAIL")
