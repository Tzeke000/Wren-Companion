# SELF_ASSESSMENT: I activate committed brain/* fixes in the LIVE runtime by reloading the module and code-swapping named functions, so long-lived threads holding old references pick up new behavior without a restart.
"""
hot_reload_tool — activate a committed brain/* fix in the running process.

The recurring scar ([[committed_fix_inert_in_live_daemon]]): background threads
bind `from brain.x import fn as _fn` ONCE at thread start, so importlib.reload
alone never reaches them — the thread keeps calling the old function object.

brain_hot_swap fixes that for pure-function modules:
  1. importlib.reload(module) — re-executes source into the SAME module dict,
     so module-level helpers/constants refresh and both old and new functions
     share that dict as __globals__.
  2. old_fn.__code__ = new_fn.__code__ — every existing reference (including
     the one captured inside a running thread) now executes the new body.

Limits (by design):
  - brain.* modules only.
  - Functions only; won't swap classes or methods of live instances (bounce or
    build a repair tool for those — see face_db_tool for the instance pattern).
  - Functions with closures are refused (__code__ swap would break cell vars).

Born 2026-07-08 to activate camera_annotator box smoothing without restarting
the runtime mid-evening (Zeke's one-restart directive).
"""
from __future__ import annotations

import importlib
import sys
from typing import Any

from tools.tool_registry import register_tool


def _brain_hot_swap(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    module_name = str(params.get("module") or "")
    func_names = params.get("funcs") or []
    if not module_name.startswith("brain."):
        return {"ok": False, "error": "only brain.* modules are swappable"}
    if not isinstance(func_names, list) or not func_names:
        return {"ok": False, "error": "funcs must be a non-empty list of function names"}

    mod = sys.modules.get(module_name)
    if mod is None:
        return {"ok": False, "error": f"{module_name} not imported in this process — nothing holds old refs; plain import will get the new code"}

    old_funcs: dict[str, Any] = {}
    for name in func_names:
        fn = getattr(mod, name, None)
        if fn is None or not callable(fn):
            return {"ok": False, "error": f"{module_name}.{name} not found or not callable"}
        if getattr(fn, "__closure__", None):
            return {"ok": False, "error": f"{module_name}.{name} has a closure — refusing __code__ swap"}
        old_funcs[name] = fn

    try:
        importlib.reload(mod)
    except Exception as e:
        return {"ok": False, "error": f"reload failed (old code still active): {e!r}"}

    swapped = []
    for name, old_fn in old_funcs.items():
        new_fn = getattr(mod, name, None)
        if new_fn is None or not callable(new_fn):
            return {"ok": False, "error": f"post-reload {module_name}.{name} missing — module reloaded but {swapped} swapped, {name} NOT", "swapped": swapped}
        try:
            old_fn.__code__ = new_fn.__code__  # type: ignore[attr-defined]
            swapped.append(name)
        except Exception as e:
            return {"ok": False, "error": f"__code__ swap failed for {name}: {e!r}", "swapped": swapped}

    return {"ok": True, "module": module_name, "swapped": swapped,
            "note": "existing thread-held references now execute the new body"}


register_tool(
    "brain_hot_swap",
    "Reload a brain.* module AND code-swap named functions so live threads holding old refs pick up the fix (no restart).",
    2,
    _brain_hot_swap,
)
