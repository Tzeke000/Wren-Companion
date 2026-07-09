# SELF_ASSESSMENT: my eyes auto-photograph unknown people near Zeke and nudge me to profile them.
"""
Unknown-face auto-capture watcher (Zeke directive 2026-07-09, by voice).

Zeke's spec, verbatim shape: "make that more like a mechanism notifier for you...
it should be things telling you what to do or suggesting it, and then you either
do it or don't." Chores -> code, wakes -> decisions (token-economy directive).

What it does
------------
A small 2 Hz polling thread that watches the recognizer output the video loop
already publishes to g["_face_results"] (no camera access of its own — the
video loop stays the single cv2 owner). When it sees, HELD for a few seconds:

  A. a KNOWN face (e.g. zeke) plus >=1 UNKNOWN face in the same frame, or
  B. >=2 UNKNOWN faces (no known needed),

it auto-captures a handful of RAW frames by reusing the video loop's existing
enrollment hook (g["_enroll_request"] — the loop saves raw pre-annotation
frames driven purely by g-state, so nothing in the running loop changes), into

    faces/_drafts/<draft_id>/enroll_*.jpg      (frames)
    faces/_drafts/<draft_id>/metadata.json     (who/when/why)

The nested _drafts/<id>/ layout is deliberately INVISIBLE to the recognizer:
insight_face_engine._load_faces() only globs DIRECT children of each person
dir, so the "_drafts" pseudo-person loads 0 embeddings and is skipped — draft
captures can never pollute the known-faces DB.

Then it fires a SignalBus event:

    unknown_capture  {draft_id, dir, frames, known, unknown_count, note}

which rides the same eyes ledger/wake path as face_appeared, so cognition (me)
gets a SUGGESTION-shaped nudge: "captured someone unknown next to zeke — draft
at <dir>; consider making a profile and asking who they are." I decide; the
mechanism never names anyone (naming stays a person-loop: ask Zeke or ask them).

Safety / etiquette
------------------
- Never starts a capture while another enrollment is in progress.
- Condition must HOLD (default 3s) — one-frame misrecognitions don't trigger.
- Cooldown (default 10 min) — a continuing visit is ONE event, not a strobe.
- Kill switch: IRIS_UNKNOWN_CAPTURE=0.

Env knobs
---------
IRIS_UNKNOWN_CAPTURE          on/off (default on)
IRIS_UNKNOWN_CAPTURE_HOLD_S   condition hold before trigger (default 3.0)
IRIS_UNKNOWN_CAPTURE_FRAMES   frames per capture (default 4)
IRIS_UNKNOWN_CAPTURE_INTERVAL seconds between frames (default 0.8)
IRIS_UNKNOWN_CAPTURE_COOLDOWN seconds between capture sessions (default 600)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = ROOT / "faces" / "_drafts"

ENABLED = os.environ.get("IRIS_UNKNOWN_CAPTURE", "1").strip().lower() not in (
    "0", "off", "false", "no", "")
HOLD_S = float(os.environ.get("IRIS_UNKNOWN_CAPTURE_HOLD_S", "3.0"))
FRAMES = int(os.environ.get("IRIS_UNKNOWN_CAPTURE_FRAMES", "4"))
INTERVAL_S = float(os.environ.get("IRIS_UNKNOWN_CAPTURE_INTERVAL", "0.8"))
COOLDOWN_S = float(os.environ.get("IRIS_UNKNOWN_CAPTURE_COOLDOWN", "600"))
CAPTURE_TIMEOUT_S = 25.0  # give up waiting for the loop to finish saving

_POLL_S = 0.5


def _log(msg: str) -> None:
    print(f"[unknown_capture] {msg}", file=sys.stderr, flush=True)


class UnknownCaptureWatcher:
    def __init__(self, g: dict[str, Any]):
        self._g = g
        self._cond_since: float = 0.0
        self._cooldown_until: float = 0.0
        self._active: dict[str, Any] | None = None  # in-flight capture session
        self._last_event: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── public ────────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="unknown_capture", daemon=True)
        self._thread.start()
        _log(f"watcher started (hold={HOLD_S}s frames={FRAMES} cooldown={COOLDOWN_S}s)")

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "enabled": ENABLED,
            "alive": bool(self._thread is not None and self._thread.is_alive()),
            "condition_held_s": round(now - self._cond_since, 1) if self._cond_since else 0.0,
            "cooldown_remaining_s": max(0.0, round(self._cooldown_until - now, 1)),
            "capturing": self._active is not None,
            "last_event": self._last_event,
            "config": {"hold_s": HOLD_S, "frames": FRAMES,
                       "interval_s": INTERVAL_S, "cooldown_s": COOLDOWN_S},
        }

    # ── loop ──────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                _log(f"tick error: {e!r}")
            time.sleep(_POLL_S)

    def _classify(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        faces = self._g.get("_face_results") or []
        knowns, unknowns = [], []
        for r in faces:
            if not isinstance(r, dict):
                continue
            pid = str(r.get("person_id") or "unknown")
            if pid == "unknown" or pid.startswith("_"):
                unknowns.append(r)
            else:
                knowns.append(r)
        return knowns, unknowns

    def _tick(self) -> None:
        g = self._g
        now = time.time()

        # 1) In-flight capture: wait for the video loop to finish saving.
        if self._active is not None:
            self._poll_active(now)
            return

        knowns, unknowns = self._classify()
        cond = (bool(knowns) and bool(unknowns)) or len(unknowns) >= 2
        if not cond:
            self._cond_since = 0.0
            return
        if now < self._cooldown_until:
            return
        if self._cond_since == 0.0:
            self._cond_since = now
            return
        if (now - self._cond_since) < HOLD_S:
            return
        # Someone else (enroll_face tool) mid-enrollment — do not stomp it.
        if g.get("_enroll_request") is not None:
            return

        # 2) Trigger: stage a draft capture through the loop's enroll hook.
        draft_id = "draft_" + time.strftime("%Y%m%d_%H%M%S")
        pid = f"_drafts/{draft_id}"
        trigger_meta = {
            "draft_id": draft_id,
            "ts": now,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "known_present": [
                {"person_id": str(k.get("person_id")),
                 "confidence": round(float(k.get("confidence") or 0.0), 3)}
                for k in knowns],
            "unknown_count": len(unknowns),
            "unknown_confidences": [
                round(float(u.get("confidence") or 0.0), 3) for u in unknowns],
            "rule": "known+unknown" if knowns else "multi-unknown",
        }
        g["_enroll_request"] = {
            "pid": pid,
            "count": FRAMES,
            "remaining": FRAMES,
            "interval_s": INTERVAL_S,
            "require_face": True,
            "saved_paths": [],
            "last_saved_ts": 0.0,
            "started_ts": now,
            "known_count_before": 0,
        }
        self._active = {"pid": pid, "meta": trigger_meta, "started": now}
        self._cond_since = 0.0
        _log(f"TRIGGER {draft_id}: {trigger_meta['rule']} "
             f"(known={[k['person_id'] for k in trigger_meta['known_present']]}, "
             f"unknowns={len(unknowns)})")

    def _poll_active(self, now: float) -> None:
        g = self._g
        act = self._active
        if act is None:
            return
        res = g.get("_enroll_result")
        done = isinstance(res, dict) and str(res.get("pid") or "") == act["pid"]
        timed_out = (now - float(act["started"])) > CAPTURE_TIMEOUT_S
        if not done and not timed_out:
            return
        if done:
            g["_enroll_result"] = None  # consume OUR result only
        saved = list((res or {}).get("saved_paths") or []) if done else []
        if not saved and timed_out:
            # Loop never finished (camera paused / no face frames) — clear a
            # stale request only if it is still ours, then cool down briefly.
            req = g.get("_enroll_request")
            if isinstance(req, dict) and str(req.get("pid") or "") == act["pid"]:
                saved = list(req.get("saved_paths") or [])
                g["_enroll_request"] = None
            self._cooldown_until = now + 60.0
            _log(f"capture {act['meta']['draft_id']} timed out ({len(saved)} frames)")
        # Write metadata next to the frames (even for partial captures).
        draft_dir = DRAFTS_DIR / act["meta"]["draft_id"]
        try:
            draft_dir.mkdir(parents=True, exist_ok=True)
            meta = dict(act["meta"])
            meta["frames_saved"] = len(saved)
            meta["frame_paths"] = saved
            meta["complete"] = bool(done)
            (draft_dir / "metadata.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8")
        except Exception as e:
            _log(f"metadata write error: {e!r}")
        # Fire the nudge (only if we actually captured something).
        if saved:
            self._cooldown_until = now + COOLDOWN_S
            event = {
                "draft_id": act["meta"]["draft_id"],
                "dir": str(draft_dir),
                "frames": len(saved),
                "known": [k["person_id"] for k in act["meta"]["known_present"]],
                "unknown_count": act["meta"]["unknown_count"],
                "note": ("unknown person alongside "
                         + ",".join(k["person_id"] for k in act["meta"]["known_present"])
                         if act["meta"]["known_present"]
                         else f"{act['meta']['unknown_count']} unknown people in frame"),
            }
            self._last_event = event
            try:
                bus = g.get("_signal_bus")
                if bus is not None:
                    bus.fire("unknown_capture", data=event, priority="high")
            except Exception as e:
                _log(f"signal fire error: {e!r}")
            _log(f"captured {event['frames']} frames -> {event['dir']} (signal fired)")
        self._active = None


# ── module-level wiring ────────────────────────────────────────────────────────
def start(g: dict[str, Any]) -> dict[str, Any]:
    """Idempotent: create + start the watcher on g. Returns status."""
    if not ENABLED:
        return {"enabled": False, "note": "IRIS_UNKNOWN_CAPTURE is off"}
    w = g.get("_unknown_capture_watcher")
    if not isinstance(w, UnknownCaptureWatcher):
        w = UnknownCaptureWatcher(g)
        g["_unknown_capture_watcher"] = w
    w.start()
    return w.status()


def status(g: dict[str, Any]) -> dict[str, Any]:
    w = g.get("_unknown_capture_watcher")
    if not isinstance(w, UnknownCaptureWatcher):
        return {"enabled": ENABLED, "alive": False, "note": "not started"}
    return w.status()
