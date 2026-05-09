"""
Phase 71 — Long-Horizon Planning System.

Ava creates and executes her own multi-step plans.
Bootstrap: Plans emerge from Ava's goals and curiosity — she decides
the priority, the approach, and when to work on them. She is not assigned
plans by us.

Plan status: pending | active | paused | completed | failed
Step status:  pending | running | completed | failed | skipped
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLANS_FILE = "state/plans.jsonl"


@dataclass
class AvaStep:
    id: str = ""
    description: str = ""
    tool_to_use: str = ""
    estimated_duration: str = ""
    status: str = "pending"
    result: str = ""


@dataclass
class AvaPlan:
    id: str = ""
    goal: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"
    created_ts: float = 0.0
    deadline: float | None = None
    progress_notes: list[str] = field(default_factory=list)
    source: str = "ava_initiative"


def _path(base_dir: Path) -> Path:
    return base_dir / PLANS_FILE


def _load_plans(base_dir: Path) -> list[dict[str, Any]]:
    p = _path(base_dir)
    if not p.is_file():
        return []
    plans: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                plans.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return plans


def _save_plans(base_dir: Path, plans: list[dict[str, Any]]) -> None:
    p = _path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for plan in plans:
            f.write(json.dumps(plan, ensure_ascii=False) + "\n")
    tmp.replace(p)


class LongHorizonPlanner:
    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        return _load_plans(self._base)

    def _save(self, plans: list[dict[str, Any]]) -> None:
        _save_plans(self._base, plans)

    def create_plan(self, goal: str, context: str = "") -> dict[str, Any]:
        """
        Uses qwen2.5:14b to break a goal into steps.
        Bootstrap: Ava calls this herself from her goals/curiosity — not assigned by us.
        """
        plan_id = str(uuid.uuid4())[:8]
        steps: list[dict[str, Any]] = []
        try:
            from langchain_ollama import ChatOllama
            from langchain_core.messages import SystemMessage, HumanMessage
            llm = ChatOllama(model="qwen2.5:14b", temperature=0.3)
            try:
                from brain.identity_loader import identity_anchor_prompt
                _anchor = identity_anchor_prompt() + "\n\n"
            except Exception:
                _anchor = ""
            sys_prompt = (
                f"{_anchor}"
                "You are helping Ava plan how to achieve a goal. "
                "Break the goal into 3-6 concrete, achievable steps. "
                "Reply as JSON only: "
                '{\"steps\": [{\"description\": str, \"tool_to_use\": str or null, \"estimated_duration\": str}]}'
            )
            out = llm.invoke([
                SystemMessage(content=sys_prompt),
                HumanMessage(content=f"GOAL: {str(goal)[:800]}\nCONTEXT: {str(context)[:600]}"),
            ])
            txt = (getattr(out, "content", None) or str(out)).strip()
            blob = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            if isinstance(blob, dict) and isinstance(blob.get("steps"), list):
                for i, s in enumerate(blob["steps"][:6]):
                    if isinstance(s, dict):
                        steps.append({
                            "id": f"{plan_id}-{i}",
                            "description": str(s.get("description") or "")[:300],
                            "tool_to_use": str(s.get("tool_to_use") or "")[:80] if s.get("tool_to_use") else "",
                            "estimated_duration": str(s.get("estimated_duration") or "")[:60],
                            "status": "pending",
                            "result": "",
                        })
        except Exception:
            pass
        if not steps:
            steps = [{
                "id": f"{plan_id}-0",
                "description": str(goal)[:300],
                "tool_to_use": "",
                "estimated_duration": "unknown",
                "status": "pending",
                "result": "",
            }]
        plan: dict[str, Any] = {
            "id": plan_id,
            "goal": str(goal)[:500],
            "steps": steps,
            "status": "active",
            "created_ts": time.time(),
            "deadline": None,
            "progress_notes": [f"created at {time.strftime('%Y-%m-%d %H:%M')}"],
            "source": "ava_initiative",
        }
        with self._lock:
            plans = self._load()
            plans.append(plan)
            self._save(plans)
        print(f"[planner] created plan {plan_id}: {goal[:60]!r} ({len(steps)} steps)")
        return plan

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        for p in self._load():
            if str(p.get("id") or "") == plan_id:
                return p
        return None

    def get_active_plans(self) -> list[dict[str, Any]]:
        return [p for p in self._load() if str(p.get("status") or "") == "active"]

    def execute_next_step(self, plan_id: str) -> dict[str, Any]:
        """Run the next pending step using available tools."""
        with self._lock:
            plans = self._load()
            plan = next((p for p in plans if str(p.get("id") or "") == plan_id), None)
            if plan is None:
                return {"ok": False, "error": "plan_not_found"}
            if str(plan.get("status") or "") != "active":
                return {"ok": False, "error": f"plan_status={plan.get('status')}"}
            steps = list(plan.get("steps") or [])
            step = next((s for s in steps if str(s.get("status") or "") == "pending"), None)
            if step is None:
                plan["status"] = "completed"
                notes = list(plan.get("progress_notes") or [])
                notes.append(f"all steps completed at {time.strftime('%Y-%m-%d %H:%M')}")
                plan["progress_notes"] = notes[-20:]
                self._save(plans)
                return {"ok": True, "done": True, "plan_id": plan_id}
            step["status"] = "running"
            self._save(plans)

        result = f"attempted: {step['description'][:200]}"
        try:
            tool_name = str(step.get("tool_to_use") or "").strip()
            if tool_name:
                from tools.tool_registry import _REGISTRY
                tr = _REGISTRY.get(tool_name)
                if tr is not None and callable(getattr(tr, "fn", None)):
                    out = tr.fn({"description": step["description"]}, {})
                    result = str(out)[:400]
        except Exception as e:
            result = f"error: {str(e)[:180]}"

        with self._lock:
            plans = self._load()
            plan = next((p for p in plans if str(p.get("id") or "") == plan_id), None)
            if plan is not None:
                for s in plan.get("steps") or []:
                    if s.get("id") == step.get("id"):
                        s["status"] = "completed"
                        s["result"] = result[:300]
                        break
                notes = list(plan.get("progress_notes") or [])
                notes.append(f"[{time.strftime('%H:%M')}] {step['description'][:60]}: done")
                plan["progress_notes"] = notes[-20:]
                self._save(plans)
        return {"ok": True, "step_id": step.get("id"), "result": result}

    def check_progress(self, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan is None:
            return {"ok": False, "error": "not_found"}
        steps = list(plan.get("steps") or [])
        total = len(steps)
        done = sum(1 for s in steps if str(s.get("status") or "") in ("completed", "skipped"))
        return {
            "ok": True,
            "plan_id": plan_id,
            "goal": plan.get("goal"),
            "status": plan.get("status"),
            "steps_total": total,
            "steps_done": done,
            "pct": round(done / total * 100) if total else 0,
            "progress_notes": list(plan.get("progress_notes") or [])[-5:],
        }

    def pause_plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            plans = self._load()
            plan = next((p for p in plans if str(p.get("id") or "") == plan_id), None)
            if plan is None:
                return {"ok": False, "error": "not_found"}
            plan["status"] = "paused"
            notes = list(plan.get("progress_notes") or [])
            notes.append(f"paused at {time.strftime('%Y-%m-%d %H:%M')}")
            plan["progress_notes"] = notes[-20:]
            self._save(plans)
        return {"ok": True, "plan_id": plan_id, "status": "paused"}

    def resume_plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            plans = self._load()
            plan = next((p for p in plans if str(p.get("id") or "") == plan_id), None)
            if plan is None:
                return {"ok": False, "error": "not_found"}
            plan["status"] = "active"
            notes = list(plan.get("progress_notes") or [])
            notes.append(f"resumed at {time.strftime('%Y-%m-%d %H:%M')}")
            plan["progress_notes"] = notes[-20:]
            self._save(plans)
        return {"ok": True, "plan_id": plan_id, "status": "active"}

    def report_to_zeke(self, plan_id: str) -> dict[str, Any]:
        """Natural language progress report."""
        prog = self.check_progress(plan_id)
        if not prog.get("ok"):
            return {"ok": False, "error": "not_found"}
        plan = self.get_plan(plan_id)
        assert plan is not None
        lines = [f"Plan: {plan.get('goal', 'unknown')[:200]}"]
        lines.append(
            f"Status: {plan.get('status')} — {prog['steps_done']}/{prog['steps_total']} steps ({prog['pct']}%)"
        )
        notes = prog.get("progress_notes") or []
        if notes:
            lines.append("Recent progress:")
            for n in notes:
                lines.append(f"  - {n}")
        return {"ok": True, "plan_id": plan_id, "report": "\n".join(lines)}

    def active_plans_summary(self) -> str:
        active = self.get_active_plans()
        if not active:
            return ""
        parts = []
        for p in active[:3]:
            steps = list(p.get("steps") or [])
            total = len(steps)
            done = sum(1 for s in steps if str(s.get("status") or "") in ("completed", "skipped"))
            parts.append(f"[{p['id']}] {str(p.get('goal') or '')[:80]} ({done}/{total} steps)")
        return "ACTIVE PLANS:\n" + "\n".join(parts)

    def tick_active_plans(self, g: dict[str, Any]) -> None:
        """Called from heartbeat/leisure — execute next step of each active plan if appropriate."""
        try:
            active = self.get_active_plans()
            if not active:
                return
            plan = active[0]
            plan_id = str(plan.get("id") or "")
            last_key = f"_planner_last_step_{plan_id}"
            now = time.time()
            last = float(g.get(last_key) or 0)
            if (now - last) < 120.0:
                return
            result = self.execute_next_step(plan_id)
            g[last_key] = now
            if result.get("done"):
                print(f"[planner] plan {plan_id} completed all steps")
            elif result.get("ok"):
                print(f"[planner] plan {plan_id} step done: {str(result.get('result') or '')[:80]}")
        except Exception as e:
            print(f"[planner] tick failed: {e}")


_planner_instance: LongHorizonPlanner | None = None


def get_planner(base_dir: str | Path | None = None) -> LongHorizonPlanner:
    global _planner_instance
    if _planner_instance is None:
        _planner_instance = LongHorizonPlanner(Path(base_dir or "."))
    return _planner_instance
