"""
Visual attention tools — what I'm looking at, and (on PTZ hardware) where I point.
SELF_ASSESSMENT: Tier 1 — reading and aiming my own eyes is mine to decide. Nothing
here touches the robot, spends money, or acts outside the machine. `attention_look`
sets an intent; on the current fixed webcam it cannot move anything, and it says so
rather than reporting a success it didn't earn.

Registry tools rather than @mcp.tool() on purpose: they hot-reload via
iris_tool_reload, so this went live the day it was written instead of waiting for a
stack restart, and they cost zero context until listed.

Backed by brain/visual_attention.py — read that module's header for the porting
contract. Summary: everything except the Actuator is hardware-agnostic, so moving to
the EMEET PIXY on the server means enabling one driver, not rewriting a subsystem.
"""
from __future__ import annotations

from typing import Any


def _attention_status_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import visual_attention as va
        out = va.status(g)
        out["summary"] = va.attention_now(g)
        return out
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _attention_now_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """One honest line about what I can see. Refuses to describe a room it has
    no fresh frame of — the refusal branches are the point."""
    try:
        from brain import visual_attention as va
        return {"ok": True, "summary": va.attention_now(g)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _attention_look_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """attention_look(target='zeke' | 'person3' | 'object:mug' | 'home')

    Sets what I'm attending to. Zeke's naming from the 08-06 design holds:
    bare names are people, 'object:' prefixes things, 'home' recentres.
    """
    try:
        from brain import visual_attention as va
        target = str(params.get("target") or params.get("name") or "").strip()
        if not target:
            return {"ok": False,
                    "error": "need a target: a name, 'person3', "
                             "'object:<thing>', or 'home'"}
        if target.lower() in ("none", "clear", "stop", "off"):
            r = va.clear_target(g)
            r["summary"] = va.attention_now(g)
            return r
        r = va.set_target(g, target)
        if r.get("ok"):
            va.observe(g)
            r["summary"] = va.attention_now(g)
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _attention_observe_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Run N attention updates against the shared frame buffer. Opens no camera.

    Useful for testing the lock/lose hysteresis by hand before there's a loop
    driving it.
    """
    try:
        from brain import visual_attention as va
        n = int(params.get("ticks") or 1)
        n = max(1, min(60, n))
        last: dict[str, Any] = {}
        for _ in range(n):
            last = va.observe(g)
        last["ticks"] = n
        last["summary"] = va.attention_now(g)
        return last
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _attention_cost_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """What am I NOT seeing because of where I'm pointed?

    On a fixed camera the honest answer is 'nothing' — one field of view, no
    choice made. On PTZ it names the bearing and what that bearing forfeits.
    """
    try:
        from brain import visual_attention as va
        return va.attention_cost(g)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _attention_depth_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """How near is what I'm tracking, and is it coming toward me?

    RELATIVE, never metric — a single camera has no baseline, so 'six feet' is
    not recoverable. Needs a locked target and a fresh frame; refuses rather
    than guessing from a stale view. ~40ms on the 3060.
    """
    try:
        from brain import visual_attention as va
        r = va.probe_depth(g)
        if r.get("ok"):
            r["summary"] = va.attention_now(g)
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _attention_objects_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Open-vocabulary "what's in front of me" — OWL-ViT on the LIVE webcam.

    params: prompts (comma string or list, required), threshold (default 0.05),
            max_results (12), unload ("1" to free the ~600MB after answering).

    Was previously reachable only through the robot path, which since 07-28 has
    been reading a stale jpg off disk because Vector is dark. Same detector, live
    eye. First call pays a one-time model load.
    """
    try:
        from brain import visual_attention as va
        prompts = params.get("prompts") or params.get("labels") or params.get("q")
        r = va.probe_objects(g, prompts,
                             threshold=float(params.get("threshold") or 0.05),
                             max_results=int(params.get("max_results") or 12))
        if str(params.get("unload") or "").lower() in ("1", "true", "yes"):
            from brain import vector_owl
            r["unloaded"] = vector_owl.unload()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _depth_status_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import depth_sense
        if str(params.get("unload") or "").lower() in ("1", "true", "yes"):
            return {**depth_sense.unload(), **depth_sense.status()}
        return depth_sense.status()
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _attention_selftest_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import visual_attention as va
        out = va.self_test()
        try:
            from brain import depth_sense
            out["depth"] = depth_sense.self_test()
            out["ok"] = bool(out.get("ok")) and bool(out["depth"].get("ok"))
        except Exception as e:
            out["depth"] = {"ok": False, "error": str(e)[:200]}
        return out
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


try:
    from tools.tool_registry import register_tool
    register_tool("attention_now",
                  "One honest line on what I can see right now — who's in "
                  "frame, what I'm tracking, and an explicit refusal if the "
                  "frame is stale or missing.",
                  1, _attention_now_fn)
    register_tool("attention_status",
                  "Full visual-attention state: target, lock status, actuator "
                  "and its real capabilities, bearing, and attention cost.",
                  1, _attention_status_fn)
    register_tool("attention_look",
                  "Set what I'm attending to: a name ('zeke'), a numbered "
                  "stranger ('person3'), an object ('object:mug'), or 'home' "
                  "to recentre. 'clear' stops tracking. On the current fixed "
                  "webcam this sets intent only and reports that it cannot "
                  "turn.",
                  1, _attention_look_fn)
    register_tool("attention_observe",
                  "Run N attention updates against the shared frame buffer "
                  "(opens no camera) to exercise the lock/lose hysteresis.",
                  1, _attention_observe_fn)
    register_tool("attention_cost",
                  "What I'm giving up by looking where I'm looking. A head "
                  "that turns can only watch one thing.",
                  1, _attention_cost_fn)
    register_tool("attention_objects",
                  "Open-vocabulary object detection on the LIVE webcam: give it "
                  "prompts ('mug, keyboard, dog') and it reports what it finds, "
                  "where in frame, and a rough near/mid/far band. No target "
                  "needed — this is how I find something worth attending to. "
                  "Pass unload=1 to give the ~600MB back afterwards.",
                  1, _attention_objects_fn)
    register_tool("attention_depth",
                  "How near is the thing I'm tracking, and is it approaching? "
                  "RELATIVE ordering only — never a distance in feet. Needs a "
                  "locked target and a fresh frame, and refuses otherwise.",
                  1, _attention_depth_fn)
    register_tool("depth_status",
                  "Monocular depth model state (Depth Anything V2 Small). Pass "
                  "unload=true to free its VRAM.",
                  1, _depth_status_fn)
    register_tool("attention_selftest",
                  "Hardware-free self-test of the attention subsystem, "
                  "including that the fixed-camera stub refuses to fake "
                  "movement.",
                  1, _attention_selftest_fn)
except Exception:
    pass
