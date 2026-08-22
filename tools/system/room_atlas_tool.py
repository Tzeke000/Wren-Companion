"""room_atlas — photographic memory of the room, bearing-tagged.

Built 2026-08-21 night, straight from the scavenger-hunt lesson: I searched
the room live, marked surfaces "done," and the V100 was in a frame I had
already taken. A stored, bearing-tagged baseline means:
  - "find X" starts from photographs, not a blind sweep;
  - a second look costs a Read, not a re-sweep;
  - two atlases diff into "what CHANGED in the room" (a thing moved = a
    high-diff bearing), which no live glance can answer.

Actions:
  sweep  — glide the bearing grid (pan ±120 × tilt {+10, −35} — the grid that
           found 4/5 tonight), save a frame per stop under
           state/room_atlas/atlas_<unix>/ + manifest.json. ~30s. Refuses if a
           pursuit loop owns the eyes or the actuator can't move. Returns the
           head to home. Keeps the newest _KEEP_ATLASES atlases.
  latest — newest atlas manifest (id, ts, stops, files).
  diff   — compare the two newest atlases stop-by-stop (mean absdiff on
           downscaled gray, same-bearing frames) → bearings ranked by change.
  Read the stop jpgs directly to actually LOOK.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_ROOT = Path(r"D:\Wren-Companion\state\room_atlas")
_KEEP_ATLASES = 5
_GRID_PANS = (-120, -80, -40, 0, 40, 80, 120)
_GRID_TILTS = (10, -35)
_SETTLE_S = 0.6          # post-glide wait for a fresh, sharp frame
_HOME = (0.0, 10.0)


def _pursuit_running(g: dict[str, Any]) -> str | None:
    for key, name in (("_attention_follow", "step follow"),
                      ("_attention_smooth", "smooth servo")):
        st = g.get(key) or {}
        t = st.get("thread")
        if t is not None and t.is_alive():
            return name
    return None


def _prune() -> None:
    try:
        atlases = sorted([p for p in _ROOT.iterdir() if p.is_dir()],
                         key=lambda p: p.name, reverse=True)
        for old in atlases[_KEEP_ATLASES:]:
            for f in old.iterdir():
                f.unlink(missing_ok=True)
            old.rmdir()
    except Exception:
        pass


def _atlases() -> list[Path]:
    if not _ROOT.exists():
        return []
    return sorted([p for p in _ROOT.iterdir()
                   if p.is_dir() and (p / "manifest.json").exists()],
                  key=lambda p: p.name, reverse=True)


def _sweep(g: dict[str, Any]) -> dict[str, Any]:
    import cv2
    from brain import frame_store
    from brain import visual_attention as va

    busy = _pursuit_running(g)
    if busy:
        return {"ok": False,
                "error": f"eyes are busy ({busy}) — stop it before a sweep"}
    act = va.build_actuator()
    if not act.capabilities().get("can_pan"):
        return {"ok": False, "error": "no PTZ actuator — cannot sweep"}

    atlas_id = f"atlas_{int(time.time())}"
    out_dir = _ROOT / atlas_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stops: list[dict] = []
    t0 = time.time()
    # Self-motion guard (2026-08-21, found live on the first sweep): the
    # sentry's motion gate honors the gesture window; without this, the
    # sweep's own hops read as room motion and the sentry engaged pursuit
    # MID-SWEEP. Same contract as attention_gesture.
    est = len(_GRID_PANS) * len(_GRID_TILTS) * (_SETTLE_S + 0.6) + 5.0
    g.setdefault("_attention_follow", {})["gesture_until"] = t0 + est
    try:
        for tilt in _GRID_TILTS:
            for pan in _GRID_PANS:
                act.look_at(float(pan), float(tilt))
                time.sleep(_SETTLE_S)
                res = frame_store.get_buffered_frame(max_age_sec=2.0)
                if res.frame is None:
                    stops.append({"pan": pan, "tilt": tilt, "file": None,
                                  "error": "no fresh frame"})
                    continue
                fname = f"pan{pan:+04d}_tilt{tilt:+03d}.jpg"
                ok = bool(cv2.imwrite(str(out_dir / fname), res.frame))
                stops.append({"pan": pan, "tilt": tilt,
                              "file": fname if ok else None,
                              "ts": round(float(res.capture_ts), 2)})
    finally:
        try:
            act.look_at(*_HOME)  # never leave the head parked mid-grid
        except Exception:
            pass
        # release the self-motion guard early (sweep may finish under estimate)
        try:
            g.setdefault("_attention_follow", {})["gesture_until"] = \
                time.time() + 2.0
        except Exception:
            pass
    manifest = {"id": atlas_id, "ts": int(t0),
                "grid": {"pans": _GRID_PANS, "tilts": _GRID_TILTS},
                "stops": stops,
                "sweep_s": round(time.time() - t0, 1)}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")
    _prune()
    good = sum(1 for s in stops if s.get("file"))
    return {"ok": True, "atlas": atlas_id, "dir": str(out_dir),
            "stops_ok": good, "stops_total": len(stops),
            "sweep_s": manifest["sweep_s"]}


def _diff() -> dict[str, Any]:
    import cv2
    ats = _atlases()
    if len(ats) < 2:
        return {"ok": False,
                "error": f"need 2 atlases to diff, have {len(ats)}"}
    new_dir, old_dir = ats[0], ats[1]
    changes: list[dict] = []
    for f in sorted(new_dir.glob("pan*.jpg")):
        old_f = old_dir / f.name
        if not old_f.exists():
            continue
        a = cv2.imread(str(old_f), cv2.IMREAD_GRAYSCALE)
        b = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
        if a is None or b is None:
            continue
        a = cv2.resize(a, (160, 120))
        b = cv2.resize(b, (160, 120))
        score = float(cv2.absdiff(a, b).mean())
        changes.append({"stop": f.name, "change": round(score, 2),
                        "new": str(f), "old": str(old_f)})
    changes.sort(key=lambda c: -c["change"])
    return {"ok": True, "new_atlas": new_dir.name, "old_atlas": old_dir.name,
            "note": "change = mean gray absdiff (lighting shifts also score; "
                    "Read the top frames to judge WHAT changed)",
            "changes": changes}


def _room_atlas(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "latest").lower()
    if action == "sweep":
        return _sweep(g)
    if action == "diff":
        return _diff()
    if action == "latest":
        ats = _atlases()
        if not ats:
            return {"ok": True, "atlases": 0,
                    "note": "no atlases yet — run action='sweep'"}
        m = json.loads((ats[0] / "manifest.json").read_text(encoding="utf-8"))
        m["dir"] = str(ats[0])
        return {"ok": True, "atlases": len(ats), "latest": m}
    return {"ok": False, "error": f"unknown action {action!r} — sweep|latest|diff"}


register_tool(
    "room_atlas",
    "Photographic memory of the room: action='sweep' glides the bearing grid "
    "and saves a frame per stop (state/room_atlas/, ~30s, returns head home); "
    "'latest' shows the newest manifest; 'diff' ranks bearings by change "
    "between the two newest atlases (what MOVED). Read the stop jpgs to "
    "actually look. Refuses while a pursuit loop owns the eyes.",
    2,
    _room_atlas,
)
