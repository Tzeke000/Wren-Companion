"""goal_initiative — persistent self-chosen goals with a DEFINED, BOUNDED influence on initiative
(Iris_fixes #10, 2026-09-30).

Zeke (Iris_fixes.docx): "goal_system_v2.py allows durable self-chosen goals. The main initiative
path appears to rely primarily on the older operational goal system. Give persistent goals a
defined influence on attention, planning and initiative. They shouldn't automatically override
conversation, safety, current circumstances, or higher-priority events. Goal: if Iris develops
a long-term goal, it can actually influence what she chooses to pursue later."

What the audit found: neither goal system (goal_system_v2 → state/ava_goals.json, goals.py →
state/goal_system.json) has a caller in the Iris process or a state file on disk. Initiative
here actually comes from three places: the heartbeat's leisure chooser (brain/leisure.py picks
a free-time activity at random, weighted by recency), curiosity prioritisation
(brain/curiosity_topics.prioritize_curiosities) and the reflection wakes I spend on open threads
(Zeke 08-22: "reflection wakes are FREE TIME"). None of them could see a goal.

This module keeps goal_system_v2 as the STORE (unified, not replaced) and defines the influence:
  PRECEDENCE (fixed, not tunable):  conversation > safety > current circumstances > higher-priority
  events > goals.  may_pursue_now(g) encodes it: a live voice/call, a conversation in the last
  RECENT_TALK_S, quiet hours, a parked body, a pause flag, or an attention event that needs
  cognition now → NO goal step. Goals are what I do when nothing else needs me.
  INFLUENCE (three defined points, each bounded):
    1. leisure: leisure_candidate(g) offers ONE activity, "pursue_goal_step", weighted to win at
       most ~1 in 3 free-time slots, only when may_pursue_now says yes.
    2. curiosity: curiosity_boost(topic) adds ≤ CURIOSITY_BOOST to a topic linked to a goal.
    3. cognition: advisory() is one line in the reflection prompt + ambient snapshot [goals], and
       the `goal` tool is how a free-time wake takes a step and RECORDS it (progress is evidence,
       not intent — the recorder-vs-actor hypothesis is watching).
Nothing here executes anything on its own. Fail-open everywhere: any error → no influence.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

RECENT_TALK_S = 600.0        # a conversation this recent = Zeke may still be engaged
QUIET_HOURS = ((22, 30), (5, 10))
CURIOSITY_BOOST = 0.2
LEISURE_GOAL_SHARE = 1 / 3   # a goal step wins at most about this share of free-time slots
GOAL_STEP_MIN_GAP_S = 3600.0 # and never twice within an hour


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _base() -> Path:
    return Path(os.environ.get("GOAL_INITIATIVE_BASE", "").strip() or _repo_root())


def _store():
    from brain import goal_system_v2 as g2
    # goal_system_v2's singleton is keyed on first use; construct per base to keep tests isolated
    return g2.GoalSystemV2(_base())


def _meta_path() -> Path:
    return _base() / "state" / "goal_meta.json"


def _load_meta() -> dict[str, Any]:
    try:
        with open(_meta_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_meta(d: dict[str, Any]) -> None:
    try:
        p = _meta_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


# ---------------------------------------------------------------- goals (store = goal_system_v2)
def add(description: str, motivation: str, *, next_step: str = "", topics: Optional[list[str]] = None,
        target_days: float = 30.0) -> dict[str, Any]:
    if not (description or "").strip() or not (motivation or "").strip():
        return {"ok": False, "error": "description and motivation are required (a goal without a why is a task)"}
    try:
        gid = _store().set_goal(description.strip(), motivation.strip(), target_days=float(target_days))
        if gid.startswith("already_at_max"):
            return {"ok": False, "error": gid}
        meta = _load_meta()
        meta.setdefault("goals", {})[gid] = {"next_step": (next_step or "").strip()[:300],
                                              "topics": [t.strip().lower() for t in (topics or []) if t.strip()][:10],
                                              "last_step_ts": 0.0, "steps": 0}
        _save_meta(meta)
        return {"ok": True, "id": gid, "goal": _view(gid)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _view(gid: str) -> Optional[dict[str, Any]]:
    for g in _store().get_all_goals():
        if g["id"] == gid:
            m = _load_meta().get("goals", {}).get(gid, {})
            return {**g, "next_step": m.get("next_step", ""), "topics": m.get("topics", []),
                    "steps": int(m.get("steps", 0)), "last_step_ts": float(m.get("last_step_ts", 0.0))}
    return None


def active() -> list[dict[str, Any]]:
    try:
        out = []
        meta = _load_meta().get("goals", {})
        for g in _store().get_active_goals():
            m = meta.get(g["id"], {})
            out.append({**g, "next_step": m.get("next_step", ""), "topics": m.get("topics", []),
                        "steps": int(m.get("steps", 0)), "last_step_ts": float(m.get("last_step_ts", 0.0))})
        return out
    except Exception:
        return []


def step(goal_id: str, note: str, *, progress: Optional[float] = None, next_step: str = "",
         now: Optional[float] = None) -> dict[str, Any]:
    """Record a REAL step taken toward a goal (evidence, not intent). Optionally set progress and
    the next step. Completes the goal at progress >= 1."""
    if not (note or "").strip():
        return {"ok": False, "error": "note is required — say what was actually done"}
    t = float(now) if now is not None else time.time()
    try:
        st = _store()
        cur = next((g for g in st.get_all_goals() if g["id"] == goal_id), None)
        if cur is None:
            return {"ok": False, "error": "unknown goal %r" % goal_id}
        if cur["status"] != "active":
            return {"ok": False, "error": "goal %r is %s" % (goal_id, cur["status"])}
        p = float(progress) if progress is not None else float(cur["progress"])
        st.update_progress(goal_id, p, note=note.strip())
        meta = _load_meta()
        m = meta.setdefault("goals", {}).setdefault(goal_id, {"next_step": "", "topics": [], "last_step_ts": 0.0, "steps": 0})
        m["last_step_ts"] = t
        m["steps"] = int(m.get("steps", 0)) + 1
        if next_step is not None and next_step != "":
            m["next_step"] = next_step.strip()[:300]
        _save_meta(meta)
        return {"ok": True, "goal": _view(goal_id)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def abandon(goal_id: str, reason: str) -> dict[str, Any]:
    if not (reason or "").strip():
        return {"ok": False, "error": "reason is required"}
    try:
        st = _store()
        ok = st.update_progress(goal_id, next((g["progress"] for g in st.get_all_goals() if g["id"] == goal_id), 0.0),
                                note="abandoned: " + reason.strip())
        ok = st.abandon_goal(goal_id) and ok
        return {"ok": bool(ok), "goal": _view(goal_id)} if ok else {"ok": False, "error": "unknown goal %r" % goal_id}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- precedence
def _in_quiet_hours(now: Optional[float] = None) -> bool:
    lt = time.localtime(now if now is not None else time.time())
    hm = lt.tm_hour * 60 + lt.tm_min
    start = QUIET_HOURS[0][0] * 60 + QUIET_HOURS[0][1]
    end = QUIET_HOURS[1][0] * 60 + QUIET_HOURS[1][1]
    return hm >= start or hm < end


def may_pursue_now(g: Optional[dict[str, Any]] = None, *, now: Optional[float] = None) -> tuple[bool, str]:
    """The precedence rule as code. Goals never override conversation, safety, circumstances or
    higher-priority events. Any error → (False, reason): when unsure, goals wait."""
    t = float(now) if now is not None else time.time()
    g = g or {}
    try:
        base = _base()
        # conversation: a live voice session / call, or anything said in the last RECENT_TALK_S
        if g.get("_voice_session_active") or g.get("_voice_call_active"):
            return False, "conversation: a voice session is live"
        try:
            from brain import iris_transcript
            rec = iris_transcript.recent(n=3)
            if rec and (t - float(rec[-1].get("ts") or 0.0)) < RECENT_TALK_S:
                return False, "conversation: something was said %ds ago" % int(t - float(rec[-1].get("ts") or 0.0))
        except Exception:
            pass
        # safety / circumstances: parked body, pause flag, quiet hours, deliberate-off flags
        for flag in ("state/body_pause.flag", "state/gpu_park.json"):
            p = base / flag
            if p.exists():
                try:
                    if flag.endswith(".json"):
                        d = json.loads(p.read_text(encoding="utf-8"))
                        if isinstance(d, dict) and d.get("parked"):
                            return False, "circumstances: body is GPU-parked (Zeke's GPU)"
                    else:
                        return False, "circumstances: body pause flag is set"
                except Exception:
                    return False, "circumstances: could not read %s (goals wait)" % flag
        if _in_quiet_hours(t):
            return False, "circumstances: quiet hours"
        # higher-priority events: anything the arbiter says needs cognition now
        try:
            from brain import attention_arbiter
            ex = attention_arbiter.explain(g=g, now=t, window_s=120.0)
            if ex.get("needs_cognition_now"):
                return False, "higher-priority event: %s" % ex["needs_cognition_now"][0].get("situation")
        except Exception:
            pass
        if not active():
            return False, "no active goals"
        return True, "nothing else needs me"
    except Exception as e:
        return False, "unsure (%r) — goals wait" % (e,)


# ---------------------------------------------------------------- the three influence points
def next_actions(limit: int = 3) -> list[dict[str, Any]]:
    """Goals with a next step, oldest-stepped first (so no goal starves)."""
    gs = [g for g in active() if g.get("next_step")]
    gs.sort(key=lambda g: float(g.get("last_step_ts") or 0.0))
    return [{"id": g["id"], "description": g["description"], "next_step": g["next_step"],
             "progress": g["progress"], "steps": g["steps"]} for g in gs[:max(1, int(limit))]]


def leisure_candidate(g: Optional[dict[str, Any]] = None, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """For the leisure chooser: ONE extra activity, or None. Bounded by precedence, the hourly gap,
    and a weight that lets it win about LEISURE_GOAL_SHARE of slots against the default set."""
    t = float(now) if now is not None else time.time()
    ok, why = may_pursue_now(g, now=t)
    if not ok:
        return None
    acts = next_actions(1)
    if not acts:
        return None
    if (t - float(_load_meta().get("last_leisure_goal_ts") or 0.0)) < GOAL_STEP_MIN_GAP_S:
        return None
    return {"activity": "pursue_goal_step", "goal": acts[0], "weight_share": LEISURE_GOAL_SHARE, "why": why}


def mark_leisure_goal(now: Optional[float] = None) -> None:
    meta = _load_meta()
    meta["last_leisure_goal_ts"] = float(now) if now is not None else time.time()
    _save_meta(meta)


def curiosity_boost(topic: str) -> float:
    """≤ CURIOSITY_BOOST for a curiosity topic linked to an active goal (by topic tag or by a
    word of the goal description). Fail-open 0."""
    try:
        tl = (topic or "").strip().lower()
        if not tl:
            return 0.0
        for g in active():
            tags = [x for x in g.get("topics", []) if x]
            if any(x in tl or tl in x for x in tags):
                return CURIOSITY_BOOST
            words = set(re.findall(r"[a-z]{5,}", g["description"].lower()))
            if len(words & set(re.findall(r"[a-z]{5,}", tl))) >= 2:
                return CURIOSITY_BOOST * 0.5
        return 0.0
    except Exception:
        return 0.0


def advisory(*, now: Optional[float] = None) -> str:
    """One block for prompts. Empty when no goals. Never raises."""
    try:
        gs = active()
        if not gs:
            return ""
        ok, why = may_pursue_now(None, now=now)
        lines = ["GOALS (mine, advisory — they wait behind conversation, safety, circumstances and live events; %s):"
                 % ("free to take a step now" if ok else "not now: " + why)]
        for g in sorted(gs, key=lambda x: float(x.get("last_step_ts") or 0.0))[:3]:
            lines.append("  - %s (%.0f%%, %d steps) — next: %s" % (
                g["description"], 100 * float(g["progress"]), int(g["steps"]), g.get("next_step") or "(no next step set)"))
        return "\n".join(lines)
    except Exception:
        return ""


def explain() -> dict[str, Any]:
    return {
        "store": str(_base() / "state" / "ava_goals.json") + " (goal_system_v2, unified) + state/goal_meta.json (next_step/topics/steps)",
        "precedence": "conversation > safety > current circumstances (parked body, pause flag, quiet hours) > "
                      "higher-priority events (attention_arbiter needs_cognition_now) > goals",
        "influence": {
            "leisure": "leisure_candidate() offers 'pursue_goal_step' to brain/leisure's chooser; wins ≤ ~%d%% of free-time "
                       "slots, never twice within %d min, only when may_pursue_now" % (int(LEISURE_GOAL_SHARE * 100), int(GOAL_STEP_MIN_GAP_S / 60)),
            "curiosity": "curiosity_boost(topic) adds ≤ %.2f to prioritize_curiosities for goal-linked topics" % CURIOSITY_BOOST,
            "cognition": "advisory() in the reflection prompt + ambient snapshot [goals]; the goal tool records real steps",
        },
        "never": "executes anything on its own; overrides a conversation; runs in quiet hours or while parked",
    }
