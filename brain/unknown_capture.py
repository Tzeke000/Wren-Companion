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

# ── Zeke-away rule (Zeke, Discord, 2026-09-02, from Cpl's Course) ──────────────
# "with your eyes a unrecognized face is kinda more important when I'm not in
#  the room. My phone not connected to the WiFi and you see a face you don't
#  know — you should study it."
# The wifi watcher (scripts/zeke_presence.py) writes state/zeke_presence.json.
# A STALE file is NOT an open gate: on 09-02 the watcher had been dead since
# 08-29 while the file still said present=true. away=None when stale/missing.
PRESENCE_FILE = ROOT / "state" / "zeke_presence.json"
PRESENCE_FRESH_S = float(os.environ.get("IRIS_PRESENCE_FRESH_S", "600"))
AWAY_COOLDOWN_S = float(os.environ.get("IRIS_UNKNOWN_CAPTURE_AWAY_COOLDOWN", "180"))
# ARRIVAL RACE (first live firing, 2026-09-02 14:49:56): the rule fired on a
# face in a MOTORCYCLE HELMET, cammies, at the door — and Zeke's phone rejoined
# the wifi 10 s later. It was him coming home; the watcher's "away" verdict
# lags arrival by up to a poll (60 s) plus the phone's wifi join. So the away
# rule holds longer than the guest rules, and cognition must re-check presence
# ~90 s after the capture before calling anyone a stranger.
AWAY_HOLD_S = float(os.environ.get("IRIS_UNKNOWN_CAPTURE_AWAY_HOLD_S", "10"))


def zeke_presence() -> dict[str, Any]:
    """Read the wifi watcher's verdict. Returns {away: True|False|None, present,
    age_s, since, ip, reason}. away is None when the watcher file is stale
    (> PRESENCE_FRESH_S) or unreadable — "don't know" must not read as "away"."""
    try:
        d = json.loads(PRESENCE_FILE.read_text(encoding="utf-8"))
        last = str(d.get("last_check") or "")
        ts = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S"))
        age = time.time() - ts
        present = bool(d.get("present"))
        stale = age > PRESENCE_FRESH_S
        away = None if stale else (not present)
        return {"away": away, "present": present, "age_s": round(age, 1),
                "since": d.get("since"), "ip": d.get("ip"),
                "reason": ("watcher stale — restart Iris-Zeke-Presence" if stale
                           else ("phone OFF wifi" if away else "phone on wifi"))}
    except Exception as e:  # noqa: BLE001
        return {"away": None, "present": None, "age_s": None, "since": None,
                "ip": None, "reason": f"unreadable: {e!r}"[:100]}


def _servo(g: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """attention_smooth through the tool registry (no import cycle)."""
    try:
        from tools.tool_registry import _REGISTRY
        td = _REGISTRY.get("attention_smooth")
        if td is None:
            return {"ok": False, "error": "attention_smooth not registered"}
        return td.handler(params, g)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:120]}


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
        pres = zeke_presence() if unknowns else {"away": None}
        # Rule 3 (2026-09-02): ONE unknown, nobody known, Zeke's phone OFF the
        # wifi — the case that matters most and used to trigger nothing.
        away_rule = bool(unknowns) and not knowns and pres.get("away") is True
        cond = (bool(knowns) and bool(unknowns)) or len(unknowns) >= 2 or away_rule
        if not cond:
            self._cond_since = 0.0
            return
        if now < self._cooldown_until:
            return
        if self._cond_since == 0.0:
            self._cond_since = now
            return
        if (now - self._cond_since) < (AWAY_HOLD_S if away_rule else HOLD_S):
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
            "rule": ("known+unknown" if knowns
                     else ("unknown-while-zeke-away" if away_rule else "multi-unknown")),
            "zeke_presence": pres,
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
        # UNKNOWN OUTRANKS KNOWN (Zeke, Discord, 2026-09-02 14:5x: "something
        # that you don't know should be studied until you have some
        # understanding of what it is" — "arguably more important than seeing
        # something that you already know"). Put the head on the unknown for
        # the capture, even beside Zeke; restore the previous target after.
        try:
            prev = str((g.get("_attention_state_obj") or {}).get("target") or "")
            r = _servo(g, {"action": "start", "target": "person:unknown", "pin": False})
            self._active["restore_target"] = prev or None
            self._active["servo"] = {"ok": bool(r.get("ok")), "prev": prev}
            _log(f"head -> person:unknown for the capture (prev={prev!r}, ok={r.get('ok')})")
        except Exception as e:  # noqa: BLE001
            self._active["servo"] = {"ok": False, "error": repr(e)[:80]}
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
            rule = str(act["meta"].get("rule") or "")
            away = rule == "unknown-while-zeke-away"
            # A stranger while he is away is worth re-evidencing sooner than a
            # guest beside him — but still not a strobe.
            self._cooldown_until = now + (AWAY_COOLDOWN_S if away else COOLDOWN_S)
            event = {
                "draft_id": act["meta"]["draft_id"],
                "dir": str(draft_dir),
                "frames": len(saved),
                "known": [k["person_id"] for k in act["meta"]["known_present"]],
                "unknown_count": act["meta"]["unknown_count"],
                "rule": rule,
                "zeke_presence": act["meta"].get("zeke_presence"),
                "note": ("unknown person alongside "
                         + ",".join(k["person_id"] for k in act["meta"]["known_present"])
                         if act["meta"]["known_present"]
                         else (f"{act['meta']['unknown_count']} unknown person(s) in frame "
                               "while Zeke's phone is OFF the wifi — STUDY THEM "
                               "(Zeke's rule 2026-09-02: study_face, look, DM him the crop)"
                               if away
                               else f"{act['meta']['unknown_count']} unknown people in frame")),
            }
            self._last_event = event
            try:
                bus = g.get("_signal_bus")
                if bus is not None:
                    bus.fire("unknown_capture", data=event, priority="high")
            except Exception as e:
                _log(f"signal fire error: {e!r}")
            _log(f"captured {event['frames']} frames -> {event['dir']} (signal fired)")
        # Give the head back unless cognition's study_face is holding it.
        prev = (act or {}).get("restore_target")
        study_active = bool((g.get("_study_face") or {}).get("active"))
        if prev and not study_active:
            try:
                r = _servo(g, {"action": "start", "target": prev})
                _log(f"head restored -> {prev!r} (ok={r.get('ok')})")
            except Exception:
                pass
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
