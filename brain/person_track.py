"""person_track — the SEEING half of person pursuit: YOLOX + ByteTrack + faces.

Built 2026-08-25 (Fable) per the handoff: fix OUR tracker instead of leaning on
the PIXY's firmware chip. This module replaces the TrackerVit face-fast-path as
the position source for person targets. What each piece contributes:

  YOLOX-s (vendor/yolox, 14-25ms GPU)  -> person-SHAPED boxes, every frame
  ByteTrack (vendor/bytetrack, ~0ms)   -> stable track ids across frames,
                                          occlusion, low-confidence dips
                                          (bent-over, head-down — the exact
                                          case that loses the face pipeline)
  InsightFace (existing pipeline)      -> IDENTITY. The only thing allowed to
                                          say a track is *zeke*.

★ IDENTITY RULE (inherited from brain/yolox_person.py, Zeke's own words:
"someone is not always me. I am someone, but I'm Zeke to you"): a body track
may only ever CONTINUE an identity a face match established — binding happens
exclusively when a recognized face lands inside a track's box. An unbound
track is "a person", never a name. ByteTrack's association replaces the old
nearest-body guesswork: once bound, the id itself carries the identity through
crossings and occlusions, and a fresh face match REBINDS (face outranks
association, always).

What the servo gets from target_offset():
  - dx, dy   normalized aim-point offset (aim at the FACE when fresh, else
             the head region of the Kalman-PREDICTED body box)
  - vx, vy   normalized offset-rate from the Kalman velocity state — a real
             motion model, not a finite difference over jittery boxes. This
             is what lets the D-term lead a mover without chasing noise.
  - staleness + provenance, so the caller can decide instead of guess.

Threading: step() is called from the servo tick (30Hz, only on fresh frames);
all state behind one lock. Pure decision helpers are module-level functions so
they test offline without a camera (scripts/test_person_track.py).
"""
from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.Lock()

# ── tracker tuning ──────────────────────────────────────────────────────────
_TRACK_THRESH = 0.50   # first-association gate. New tracks need +0.1 = 0.60,
                       # which mirrors yolox_person's Zeke-vs-helmet line
                       # (0.625 vs 0.509 measured 08-25).
_MATCH_THRESH = 0.80
_TRACK_BUFFER = 60     # frames a lost id survives (~2s at 30fps). Longer
                       # coasting is the DECISION layer's job, not the KF's.
_DET_FLOOR = 0.10      # feed ByteTrack everything down to here — low-conf
                       # boxes are the whole point (second association).
_FACE_FRESH_S = 0.7    # face position newer than this aims the head directly
_HEAD_FRAC = 0.18      # aim point inside a body box: this far below the top
_BIND_MAX_AGE_S = 3600.0  # a binding never expires on its own; the track
                          # dying or a rebind is what ends it.

_state: dict[str, Any] = {
    "tracker": None,
    "fps_ema": 30.0,
    "last_step_ts": 0.0,
    "tracks": [],            # last output: list of dicts (id, tlwh, score, mean)
    "bindings": {},          # label -> {track_id, bound_ts, via}
    "faces_last": {},        # label -> {bbox, ts}
    "frame_shape": None,
    "steps": 0,
    "detect_ms_ema": 0.0,
    "error": None,
}


def reset() -> None:
    with _LOCK:
        _state.update({"tracker": None, "tracks": [], "bindings": {},
                       "faces_last": {}, "steps": 0, "error": None})


# ── pure helpers (offline-testable) ─────────────────────────────────────────

def _point_in_box(px: float, py: float, tlwh) -> bool:
    x, y, w, h = tlwh
    return x <= px <= x + w and y <= py <= y + h


def bind_faces(tracks: list[dict], faces: list[dict]) -> dict[str, int]:
    """Which track does each RECOGNIZED face belong to right now?

    A face binds to the smallest track box containing its center (smallest =
    the person actually wearing the face when boxes overlap). Faces without a
    person_id bind nothing. Pure function: returns {label: track_id}.
    """
    out: dict[str, int] = {}
    for f in faces or []:
        label = str(f.get("person_id") or "").lower()
        bb = f.get("bbox") or f.get("box")
        if not label or label.startswith("person") or not bb or len(bb) < 4:
            continue  # unknowns ("person3") never grant identity
        fx = (float(bb[0]) + float(bb[2])) / 2.0
        fy = (float(bb[1]) + float(bb[3])) / 2.0
        candidates = [t for t in tracks if _point_in_box(fx, fy, t["tlwh"])]
        if not candidates:
            continue
        best = min(candidates, key=lambda t: t["tlwh"][2] * t["tlwh"][3])
        out[label] = best["id"]
    return out


def aim_point(tlwh, head_frac: float = _HEAD_FRAC) -> tuple[float, float]:
    """Where to point the head for a body box: center-x, head-height."""
    x, y, w, h = tlwh
    return x + w / 2.0, y + h * head_frac


# ── live pipeline ───────────────────────────────────────────────────────────

def _ensure_tracker():
    if _state["tracker"] is None:
        from vendor.bytetrack import BYTETracker, TrackerArgs
        _state["tracker"] = BYTETracker(
            TrackerArgs(track_thresh=_TRACK_THRESH,
                        track_buffer=_TRACK_BUFFER,
                        match_thresh=_MATCH_THRESH),
            frame_rate=30)
    return _state["tracker"]


def step(frame, faces: list[dict], capture_ts: float) -> dict[str, Any]:
    """Advance the tracker one fresh frame. Cheap to call; refuses reruns on
    the same capture_ts. Returns a small status dict."""
    with _LOCK:
        if capture_ts <= float(_state["last_step_ts"] or 0.0):
            return {"ok": True, "skipped": "stale frame"}
        now = time.time()
        dt = capture_ts - float(_state["last_step_ts"] or capture_ts)
        if 0.0 < dt < 1.0:
            _state["fps_ema"] = 0.9 * _state["fps_ema"] + 0.1 * (1.0 / dt)
        _state["last_step_ts"] = capture_ts
        try:
            from brain import yolox_person
            t0 = time.time()
            dets = yolox_person.detect(frame, min_score=_DET_FLOOR)
            _state["detect_ms_ema"] = (0.8 * _state["detect_ms_ema"]
                                       + 0.2 * (time.time() - t0) * 1000.0)
            # Known NON-persons (2026-09-02): the Spartan-helmet statue on the
            # dresser held a body track and corroborated a pose false-positive.
            # brain/body_pose remembers such shapes by head bearing; drop them
            # before they become tracks the head could chase.
            try:
                from brain import body_pose as _bp
                _n0 = len(dets)
                dets = [d for d in dets if not _bp.is_static_box(d.get("bbox") or [0, 0, 0, 0])]
                _state["static_dropped"] = int(_state.get("static_dropped") or 0) + (_n0 - len(dets))
            except Exception:
                pass
            import numpy as np
            arr = (np.array([[*d["bbox"], d["confidence"]] for d in dets],
                            dtype=float).reshape(-1, 5))
            h, w = frame.shape[:2]
            trk = _ensure_tracker()
            stracks = trk.update(arr, (h, w), (h, w))
            _state["tracks"] = [
                {"id": t.track_id,
                 "tlwh": [float(v) for v in t.tlwh],
                 "score": float(t.score),
                 # KF state: [cx, cy, a, h, vcx, vcy, va, vh] per UPDATE
                 "vel": ([float(t.mean[4]), float(t.mean[5])]
                         if t.mean is not None else [0.0, 0.0]),
                 # height-rate: how fast the BOX is changing shape. Arms going
                 # up grow the box upward — that is shape morph, not motion
                 # (referee test 2026-08-25: "hands up and your head went
                 # WILD"). Kept so target_offset can report subject velocity
                 # instead of box-center velocity.
                 "vh": (float(t.mean[7]) if t.mean is not None else 0.0),
                 "ts": capture_ts}
                for t in stracks]
            _state["frame_shape"] = (h, w)
            _state["steps"] += 1
            # identity: face matches bind labels to track ids (face outranks
            # any previous binding — rebind moves the label, never copies it)
            for label, tid in bind_faces(_state["tracks"], faces).items():
                prev = _state["bindings"].get(label) or {}
                _state["bindings"][label] = {"track_id": tid, "bound_ts": now,
                                             "via": "face_in_box",
                                             "face_up_px": prev.get("face_up_px")}
                # BOTTOM-ANCHORED HEAD MEMORY (2026-08-25 referee fix): while
                # the face is visible, remember how far the head sits above
                # the track box's BOTTOM edge. Feet stay planted when arms go
                # up — the bottom edge is the only stable reference on a box
                # whose top morphs with gesture. When the face later goes
                # stale, we aim at bottom-minus-this instead of a top-anchored
                # fraction (top-anchored is what chased raised hands).
                f = next((f for f in faces or []
                          if str(f.get("person_id") or "").lower() == label), None)
                trk_box = next((t for t in _state["tracks"] if t["id"] == tid),
                               None)
                if f is not None and trk_box is not None:
                    bb = f.get("bbox") or f.get("box")
                    if bb and len(bb) >= 4:
                        fy = (float(bb[1]) + float(bb[3])) / 2.0
                        x, y, w2, h2 = trk_box["tlwh"]
                        up = (y + h2) - fy
                        if up > 0:
                            _state["bindings"][label]["face_up_px"] = up
            for f in faces or []:
                label = str(f.get("person_id") or "").lower()
                bb = f.get("bbox") or f.get("box")
                if label and not label.startswith("person") and bb:
                    _state["faces_last"][label] = {"bbox": list(bb),
                                                   "ts": capture_ts}
            _state["error"] = None
            return {"ok": True, "tracks": len(_state["tracks"]),
                    "detect_ms": round(_state["detect_ms_ema"], 1)}
        except Exception as e:  # noqa: BLE001 — seeing must degrade, not raise
            _state["error"] = repr(e)
            return {"ok": False, "error": repr(e)}


def target_offset(label: str) -> dict[str, Any] | None:
    """Aim data for a bound label, or None when there is no honest answer.

    dx/dy: normalized offset of the aim point from frame center (right/down
    positive — same convention as the servo). vx/vy: normalized offset-rate
    per second from the Kalman velocity. source: 'face' | 'body'.
    """
    label = str(label or "").lower()
    with _LOCK:
        shape = _state.get("frame_shape")
        if not shape:
            return None
        h, w = shape
        binding = _state["bindings"].get(label)
        face = _state["faces_last"].get(label)
        now_ts = float(_state["last_step_ts"] or 0.0)

        track = None
        if binding is not None:
            track = next((t for t in _state["tracks"]
                          if t["id"] == binding["track_id"]), None)

        # face fresh -> aim at the face itself (best aim point there is),
        # but still report the KF velocity if the bound track is alive.
        if face and now_ts - float(face["ts"]) <= _FACE_FRESH_S:
            bb = face["bbox"]
            ax = (float(bb[0]) + float(bb[2])) / 2.0
            ay = (float(bb[1]) + float(bb[3])) / 2.0
            source = "face"
        elif track is not None:
            # BOTTOM-ANCHORED aim (2026-08-25 referee fix): the old aim point
            # hung a fixed fraction below the box TOP — and the top is exactly
            # the edge that leaps upward when arms go up, so the head chased
            # gestures. The bottom edge (feet) stays planted through arm
            # raises and bends. Aim at bottom minus the remembered face
            # height; fall back to the top-fraction only when no face was
            # ever measured for this binding.
            x, y, bw, bh = track["tlwh"]
            up = (binding or {}).get("face_up_px")
            if up and 0 < float(up) <= bh * 1.2:
                ax = x + bw / 2.0
                ay = (y + bh) - float(up)
            else:
                ax, ay = aim_point(track["tlwh"])
            source = "body"
        else:
            return None  # no fresh face, no live bound track — honestly lost

        fps = max(5.0, min(60.0, float(_state["fps_ema"])))
        vx = vy = 0.0
        if track is not None:
            # KF velocity is px per tracker-update; updates happen per fresh
            # frame, so px/s = v * fps. Normalize like the offsets.
            # SHAPE-MORPH COMPENSATION (2026-08-25 referee fix): vcy is the
            # BOX CENTER's velocity. When the box grows upward (arms up), the
            # center rises at vh/2 with the subject standing still — that
            # phantom rate is what the D-term amplified into the wild swing.
            # The BOTTOM edge's velocity is vcy + vh/2; with feet planted it
            # is ~0 during a gesture and tracks real body motion otherwise.
            # Our aim point rides the bottom edge, so its velocity is the
            # honest vertical rate to feed forward.
            vx = float(track["vel"][0]) * fps / (w / 2.0)
            vy = ((float(track["vel"][1]) + float(track.get("vh") or 0.0) / 2.0)
                  * fps / (h / 2.0))
        return {"dx": (ax - w / 2.0) / (w / 2.0),
                "dy": (ay - h / 2.0) / (h / 2.0),
                "vx": vx, "vy": vy,
                "source": source,
                "track_id": track["id"] if track else None,
                "age_s": round(max(0.0, now_ts - (float(face["ts"]) if source == "face" else float(track["ts"]))), 3)}


def track_boxes() -> tuple[list[list[float]], tuple[int, int] | None]:
    """All live track boxes (tlwh, original frame px) + the frame shape they
    were measured on. For the servo's background-only odometry (2026-08-25,
    modeled on BoT-SORT gmc.py): every person box gets masked OUT of the
    camera-motion estimate, so people moving can't read as my head moving."""
    with _LOCK:
        return ([list(t["tlwh"]) for t in _state["tracks"]],
                _state.get("frame_shape"))


def status() -> dict[str, Any]:
    with _LOCK:
        return {"steps": _state["steps"],
                "tracks": [{k: t[k] for k in ("id", "score")}
                           for t in _state["tracks"]],
                "bindings": {k: dict(v) for k, v in _state["bindings"].items()},
                "fps_ema": round(_state["fps_ema"], 1),
                "detect_ms_ema": round(_state["detect_ms_ema"], 1),
                "error": _state["error"]}


# =============================================================================
# LIVE DETECTION LOOP — "get the body at 30 FPS" (Zeke, Discord 2026-09-03)
#
# Until today, step() ran INLINE ON THE SERVO TICK: the pursuit loop paid for a
# full YOLOX pass before it could steer, so body boxes only refreshed as often
# as the servo ticked. Measured 09-03: servo ~13.5 Hz, true step rate ~5/s, and
# Zeke's own eyes on the HUD — "it looks like it's glitching along."
#
# Meanwhile the four perception workers (face/hands/expr/attn) hold the camera's
# full 29.2 fps by doing exactly one thing: watch the newest frame, process it
# once, sleep the rest of the 33 ms budget. This is that same worker, for bodies.
#
# ★ WHY THE SERVO NEEDS NO EDIT: step() already refuses a frame it has seen
#   (`capture_ts <= last_step_ts` → "stale frame"). Once this loop is stepping
#   every fresh frame, the servo's own step() call finds nothing new and returns
#   for free — and if this thread ever dies, the servo silently goes back to
#   doing the work itself. Self-healing in both directions, no flag to forget.
#   That only holds because BOTH read frame_store.get_buffered_frame(), so they
#   share one capture_ts clock. Do not "optimise" this loop onto the raw frame
#   slot (g["_raw_frame_slot"]) — its timestamps are a DIFFERENT clock and the
#   stale-frame guard would stop working, silently doubling the detection load.
# =============================================================================
_LOOP_BUDGET_S = 0.033       # one camera frame at ~30fps
_loop_thread: Any = None
_loop_stop: Any = None
_loop_stats: dict[str, Any] = {"alive": False, "ticks": 0, "steps": 0,
                               "errors": 0, "last_ms": None, "started_ts": 0.0}


def loop_status() -> dict[str, Any]:
    alive = bool(_loop_thread is not None and _loop_thread.is_alive())
    out = dict(_loop_stats)
    out["alive"] = alive
    if alive and out.get("started_ts"):
        el = time.time() - float(out["started_ts"])
        out["steps_per_s"] = round(out["steps"] / el, 1) if el > 0 else None
        out["elapsed_s"] = round(el, 1)
    return out


def _loop(g: dict[str, Any], stop) -> None:
    from brain import frame_store
    last_ts = 0.0
    while not stop.is_set():
        try:
            _loop_stats["ticks"] += 1
            res = frame_store.get_buffered_frame(max_age_sec=1.0)
            if res.frame is None or float(res.capture_ts) <= last_ts:
                stop.wait(0.004)          # no new frame yet — poll cheaply
                continue
            last_ts = float(res.capture_ts)
            t0 = time.time()
            step(res.frame, g.get("_face_results") or [], res.capture_ts)
            dt = time.time() - t0
            _loop_stats["steps"] += 1
            _loop_stats["last_ms"] = round(dt * 1000.0, 1)
            # A pass slower than the budget just runs flat out at its own rate.
            stop.wait(max(0.0, _LOOP_BUDGET_S - dt))
        except Exception as e:  # noqa: BLE001 — seeing degrades, never raises
            _loop_stats["errors"] += 1
            _loop_stats["error"] = repr(e)[:160]
            stop.wait(0.5)
    _loop_stats["alive"] = False


def start_loop(g: dict[str, Any]) -> dict[str, Any]:
    """Start the 30Hz body-detection worker. Idempotent."""
    global _loop_thread, _loop_stop
    if _loop_thread is not None and _loop_thread.is_alive():
        return {"already_running": True, **loop_status()}
    _loop_stop = threading.Event()
    _loop_stats.update({"ticks": 0, "steps": 0, "errors": 0,
                        "started_ts": time.time(), "alive": True})
    _loop_thread = threading.Thread(target=_loop, args=(g, _loop_stop),
                                    daemon=True, name="iris-body-worker")
    _loop_thread.start()
    return {"started": True, **loop_status()}


def stop_loop() -> dict[str, Any]:
    """Stop the worker. The servo transparently resumes stepping inline."""
    global _loop_stop
    if _loop_stop is not None:
        _loop_stop.set()
    return {"stopped": True, **loop_status()}
