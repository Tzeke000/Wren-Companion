"""
Vector nerves on/off switch — Zeke's ask 2026-08-07: "since your body is dead
just have it not try to connect to your body for now."
SELF_ASSESSMENT: Tier 1 for status, Tier 2 for the flips — they change what the
body daemon does. Both are local, reversible, and the OFF direction only ever
reduces activity. Nothing here touches the robot itself.

Writes state/vector_deliberately_off.json, deliberately the SAME schema as
state/voice_deliberately_off.json ({"off", "by", "ts"}) so the standing rule
"check state/ for a deliberate-off flag before healing a down service" covers
the nerves too, without anyone needing to learn a second convention.

The daemon re-reads the flag every loop, so a flip takes effect within ~60s with
NO restart. While off it makes zero connection attempts — which is what takes
the thread leak to nil rather than merely slowing it.

★ WHAT THIS ACTUALLY COSTS — checked in the source, not assumed. My first
version of this file claimed "nothing will notice Vector waking up," which is
FALSE and would have been a scary wrong caveat sitting in a flag file for weeks.
The daemon reaches the robot three different ways and the switch only gates one:

  GATED (all inside run_once(), all need the gRPC anki_vector.Robot() session —
  and this is where 100% of the thread leak came from):
    * _nervous_loop   15Hz senses: petting / picked-up / cliff / charger, camera
    * _ears_loop      robot audio
    * _nav_map_loop   room blueprint
    * _possession_loop RESERVE_CONTROL

  STILL RUNNING (started before the loop, different transports, no leak):
    * _battery_watch_loop   wire-pod HTTP  -> battery json AND the lost-contact
                            alarm stay ARMED
    * _transcript_tap_loop  HTTP
    * _interoception_loop   SSH to the robot (SoC temp, wifi link)

⇒ So we WOULD still notice him coming back: the battery poll and the SSH
interoception both start succeeding the moment he's reachable. What's lost while
parked is the nervous system — senses, camera, nav map, ears, possession — not
our ability to see him return. Turn it back on when the body returns, to get the
senses back.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_FLAG = _REPO / "state" / "vector_deliberately_off.json"
_LOG = _REPO / "state" / "vector" / "inhabit_daemon.log"


def _read() -> dict[str, Any]:
    try:
        if not _FLAG.exists():
            return {}
        return json.loads(_FLAG.read_text(encoding="utf-8"))
    except Exception as e:
        return {"_parse_error": str(e)[:120]}


def _nerves_status_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    d = _read()
    off = bool(d.get("off"))
    out: dict[str, Any] = {
        "ok": True,
        "off": off,
        "flag_file": str(_FLAG),
        "by": d.get("by"),
        "set_ts": d.get("ts"),
    }
    if d.get("_parse_error"):
        out["warning"] = (f"flag file is unparseable ({d['_parse_error']}) — the "
                          f"daemon FAILS SAFE and treats that as ON")
        out["off"] = False
    if off:
        out["cost"] = ("nervous system parked: no senses (petting/pickup/cliff/"
                       "charger), no camera, no nav map, no ears, no possession. "
                       "The battery poll (wire-pod HTTP), lost-contact alarm and "
                       "SSH interoception all KEEP RUNNING, so we would still "
                       "notice him coming back")
    # what the daemon has actually done lately — evidence, not assumption
    try:
        tail = _LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        out["daemon_last_line"] = tail[-1] if tail else None
        out["recent_connect_attempts"] = sum(1 for ln in tail if "retry #" in ln)
        out["parked_line_seen"] = any("NERVES DELIBERATELY OFF" in ln for ln in tail)
    except Exception:
        pass
    return out


def _nerves_off_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    reason = str(params.get("reason") or params.get("by") or "").strip()
    if not reason:
        return {"ok": False,
                "error": "give a reason — this flag is read by future-me and by "
                         "Zeke, and an unexplained off-switch on the body is "
                         "exactly the kind of artifact that misleads later"}
    try:
        _FLAG.parent.mkdir(parents=True, exist_ok=True)
        _FLAG.write_text(json.dumps(
            {"off": True, "by": reason, "ts": round(time.time(), 1)},
            indent=2), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "off": True, "by": reason,
            "takes_effect": "within ~60s, no restart needed (daemon re-reads "
                            "the flag each loop) — but ONLY if the running "
                            "daemon has the switch code; restart it if unsure",
            "cost": "nervous system parked (senses/camera/nav-map/ears/"
                    "possession). Battery poll + lost-contact alarm + SSH "
                    "interoception keep running, so a return IS still visible. "
                    "Turn it back on when the body returns, to get senses back"}


def _nerves_on_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        existed = _FLAG.exists()
        prev = _read()
        if existed:
            _FLAG.unlink()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "off": False, "was_off": bool(prev.get("off")),
            "flag_removed": existed,
            "takes_effect": "within ~60s — daemon logs 'nerves re-enabled' and "
                            "resumes connection attempts at the 15s-then-backoff "
                            "ladder"}


try:
    from tools.tool_registry import register_tool
    register_tool("vector_nerves_status",
                  "Is the body daemon deliberately parked? Reports the flag, "
                  "who set it and why, plus what the daemon has actually been "
                  "doing in its log (recent connect attempts / parked line).",
                  1, _nerves_status_fn)
    register_tool("vector_nerves_off",
                  "Park the body daemon's gRPC nervous system: stop trying to "
                  "reach Vector (zero connect attempts, zero thread leak). "
                  "Requires a reason. COSTS: senses (petting/pickup/cliff/"
                  "charger), camera, nav map, ears, possession. KEEPS RUNNING: "
                  "battery poll, lost-contact alarm, SSH interoception — so his "
                  "return is still visible.",
                  2, _nerves_off_fn)
    register_tool("vector_nerves_on",
                  "Un-park the body daemon: resume gRPC connection attempts and "
                  "restore the senses. Do this whenever the body might be "
                  "coming back.",
                  2, _nerves_on_fn)
except Exception:
    pass
