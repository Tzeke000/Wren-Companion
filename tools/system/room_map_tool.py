"""room_map — a WORLD-FRAME memory of who is where, so people survive leaving my view.

Zeke, 2026-08-29, after watching my head swing between him and Q: *"me and Q are
far enough away that you cant have both of us in veiw at one time"* — then:
*"go ahead and build multi lock how you want it set up and research how robotics
has solved this."*

THE PROBLEM WITH WHAT I HAD
---------------------------
Two layers existed and neither could do this:
  - `attention_smooth` / `attention_follow` — pursue ONE target, in IMAGE space.
    When the target leaves the frame there is nothing left; the state is a bbox.
  - `attention_multi` — holds N TrackerVit locks, and since v2 they even survive
    a panning head. But its targets are OWL-ViT text prompts, so two people come
    back as two boxes both labelled "person". **No identity.** It cannot tell me
    which box is Zeke.

Both track in the CAMERA's frame. That is the actual bug. A bbox is a statement
about pixels, and pixels stop existing the moment the head turns. So "where is Q"
became unanswerable the instant I looked at Zeke — not because the information was
hard, but because I was storing it in the wrong coordinate system.

THE FIX, WHICH IS THE STANDARD ROBOTICS ANSWER
----------------------------------------------
Track in the ROOM's frame, not the image's. The head knows its own pan/tilt, so a
face at pixel x while the head is at pan P sits at absolute bearing

    pan_abs = P + PAN_SIGN * ((cx - w/2) / (w/2)) * (HFOV / 2)

and that number does not change when the head turns away. Identity comes from the
face recogniser at the moment of sighting and is *attached to the bearing*, so it
persists through absence. The roster is then a small world model:

    q    -> pan -14 deg, tilt +3, last seen 40s ago, sigma 12 deg, REMEMBERED
    zeke -> pan +31 deg, tilt -12, seen now,          sigma  2 deg, VISIBLE

That is enough to answer "where is Q" while looking at Zeke, to decide who to look
at next, and to know when a memory has decayed into a guess.

WHAT I DELIBERATELY DID NOT DO
------------------------------
No Kalman filter. A KF's payoff is fusing a motion model with noisy repeated
observations; here observations are *sparse and absent for whole seconds*, the
process (a human deciding to stand up) is nothing like Gaussian, and range is
unobservable from one camera. A filter would produce a confident covariance
ellipse over a fiction. Growing scalar uncertainty is honest about the same
ignorance and has no tuning surface to get wrong. If the research says otherwise
I will revisit — but the default should be the model whose failure is visible.

SIGN CONVENTION IS MEASURED, NOT ASSUMED
----------------------------------------
`PAN_SIGN` decides whether a face on the right of frame means a higher or lower
absolute pan, and getting it backwards silently mirrors the entire room map —
every bearing wrong, nothing crashing. The servo's jog units and the odometry's
scene-shift sign point opposite ways in the source and I could not derive it with
confidence from reading, so this module REFUSES to convert bearings until
`action='calibrate'` has measured it against the real hardware. An uncalibrated
room map returns an honest error instead of a mirrored world.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

# ── Geometry ───────────────────────────────────────────────────────────────
# 68 deg nominal, matching visual_attention._HEADROOM_HFOV_DEG and the
# actuator's own capability note. Measured nearer 70.5 on 08-25; kept at 68 for
# consistency with the rest of the codebase (a 4% conservative field errs toward
# "I might have lost them", which is the safe direction).
_HFOV_DEG = 68.0

_GEOM_PATH = Path(__file__).resolve().parents[2] / "state" / "room_geometry.json"

# How fast a person's position becomes unknown once I stop looking. A seated
# person barely moves; someone walking crosses the room in seconds. This is a
# deliberately pessimistic scalar, not a physics model: it answers "how much of
# the room might they be in now", and it saturates because after long enough the
# only honest answer is "no idea".
_SIGMA_FRESH_DEG = 2.0        # uncertainty of a bearing I am looking at now
# ⚠ TUNED BY TEST, 2026-08-29. First value was 3.5 deg/s, reasoned from "a
# person walking at 1 m/s three metres away sweeps ~19 deg/s, so 3.5 is
# conservative". It saturated the estimate in 21 SECONDS. The look-away test
# then reported Zeke's position as decayed-beyond-use after 78s while he was
# sitting perfectly still in a chair — useless exactly in the window the
# feature exists for (10-60s glances away).
# The error was modelling the WORST case as the TYPICAL one. People in a room
# are stationary almost all the time and relocate occasionally; "might be
# walking" is not "is walking". 0.35 deg/s puts sigma at ~10 deg after 30s
# (honest: "within about ten degrees of there") and reaches useless at ~3.5
# min, which is roughly when I would genuinely stop believing a memory.
# Still a guess — `last_jump_deg` is recorded on every re-sighting precisely so
# this can be replaced with a MEASURED distribution of how far people actually
# move between my glances.
_SIGMA_RATE_DEG_S = 0.35      # growth per second unobserved
_SIGMA_MAX_DEG = 75.0         # past this the memory is not a location any more

# ── THE LADDER (2026-08-30) ────────────────────────────────────────────────
# v1 had a single `_FORGET_S = 900` that deleted the whole entry, which
# conflated two completely different claims: WHERE someone is, and THAT they
# exist and look like this. The first expires in seconds. The second never
# expires at all. Deleting "Zeke is at +15 deg" after a while is correct;
# deleting "Zeke is a person I know" because I looked away is not.
#
# Tiers are set by how much of my own field of view the uncertainty has eaten,
# because that is the question that actually matters operationally: can ONE
# glance still find them, or does it now take a search?
_TIER_TRACKED_DEG = 8.5       # <= FOV/8: one glance lands them comfortably
_TIER_REMEMBERED_DEG = 34.0   # <= FOV/2: a glance MIGHT find them
#   beyond that -> DORMANT: the position claim is dropped, the person is kept.

# Negative evidence: looking at where someone should be and NOT seeing them.
# Miss probability of the face detector on someone who really is in frame —
# poses, turning away, motion blur. Deliberately generous: a confident
# detector makes absence damning, and mine is not that good.
_P_MISS = 0.35

# A sighting this far (deg) from a remembered bearing is treated as the same
# person having moved, rather than a second instance. Widened by the entry's own
# uncertainty at match time.
_ASSOC_GATE_DEG = 18.0


def _state(g: dict[str, Any]) -> dict:
    st = g.setdefault("_room_map", {})
    st.setdefault("people", {})
    return st


def _load_geom() -> dict:
    try:
        return json.loads(_GEOM_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pan_sign() -> float | None:
    v = _load_geom().get("pan_sign")
    try:
        return float(v) if v in (1, -1, 1.0, -1.0) else None
    except Exception:
        return None


def _head_bearing(g: dict[str, Any]) -> dict | None:
    """Live bearing from the actuator, or the attention layer's cached copy.

    Prefers a CONFIRMED read. `attention_status` has been caught serving a
    stale `actuator`/`can_move` before, so a cached bearing is used only as a
    fallback and is flagged when it is.
    """
    try:
        from brain import visual_attention as va
        act = va.build_actuator()
        b = act.bearing()
        if b and b.get("confirmed"):
            return {"pan_deg": float(b.get("pan_deg") or 0.0),
                    "tilt_deg": float(b.get("tilt_deg") or 0.0),
                    "confirmed": True}
        st = va._state(g)
        cached = st.get("bearing") or {}
        if cached:
            return {"pan_deg": float(cached.get("pan_deg") or 0.0),
                    "tilt_deg": float(cached.get("tilt_deg") or 0.0),
                    "confirmed": False}
        if b:
            return {"pan_deg": float(b.get("pan_deg") or 0.0),
                    "tilt_deg": float(b.get("tilt_deg") or 0.0),
                    "confirmed": False}
    except Exception:
        pass
    return None


def _frame_shape(g: dict[str, Any]) -> tuple[int, int] | None:
    try:
        from brain import frame_store
        res = frame_store.get_buffered_frame(max_age_sec=3.0)
        if res.frame is None:
            return None
        return int(res.frame.shape[1]), int(res.frame.shape[0])
    except Exception:
        return None


def face_bearing(face: dict, frame_w: int, frame_h: int, head: dict,
                 pan_sign: float) -> tuple[float, float]:
    """Absolute room bearing of a face, in degrees. Exact, not small-angle.

    ⚠ REWRITTEN 2026-08-30. v1 did two things that are wrong and that no test
    would have caught, because both produce plausible numbers:

    1. **VFOV as HFOV x (H/W).** That linear form is simply not the projection.
       The truth is `VFOV = 2*atan((H/W)*tan(HFOV/2))`, and at 68 deg on a 16:9
       frame the linear version reads 38.25 deg against a true 41.55 — an 8%
       error on every tilt I recorded.
    2. **Azimuth treated as linear in pixel offset.** Off the centre row the
       horizontal angle is inflated by `1/cos(tilt)`: negligible at the -14 deg
       I happened to test at (x1.03), but x1.41 at 45 deg and x2.0 at 60 deg,
       which are both inside this head's range. So the map would have been
       progressively more wrong the further from level I looked, and correct
       exactly where I checked it.

    Both now use the exact composition. Pixel -> camera-relative (a, e) via
    atan on the normalised image plane, then rotate by the head's tilt:

        a = atan(x)                       x = (u - W/2)/fx
        e = atan(-y / sqrt(x^2 + 1))      y = (v - H/2)/fy      (+y is DOWN)
        dbeta = atan2( cos e sin a,  cos0 cos e cos a - sin0 sin e )
        phi   = asin(  sin0 cos e cos a + cos0 sin e )

    Sanity anchors, both verified: a face at frame centre returns the head's
    own bearing exactly, and with the head level `dbeta` collapses to `a`.

    `pan_sign` (MEASURED, see _calibrate) maps the geometric offset onto this
    actuator's pan convention; it is applied once, here, so nothing downstream
    has to know the handedness.
    """
    x1, y1, x2, y2 = [float(v) for v in list(face.get("bbox") or [0, 0, 0, 0])[:4]]
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    fx = (frame_w / 2.0) / math.tan(math.radians(_HFOV_DEG) / 2.0)
    fy = fx                                    # square pixels, one optical axis
    x = (cx - frame_w / 2.0) / fx
    y = (cy - frame_h / 2.0) / fy
    a = math.atan(x)
    e = math.atan(-y / math.sqrt(x * x + 1.0))
    th = math.radians(float(head["tilt_deg"]))
    dbeta = math.atan2(math.cos(e) * math.sin(a),
                       math.cos(th) * math.cos(e) * math.cos(a)
                       - math.sin(th) * math.sin(e))
    phi = math.asin(max(-1.0, min(1.0,
                                  math.sin(th) * math.cos(e) * math.cos(a)
                                  + math.cos(th) * math.sin(e))))
    pan = float(head["pan_deg"]) + pan_sign * math.degrees(dbeta)
    return pan, math.degrees(phi)


def _sigma_now(entry: dict, now: float) -> float:
    age = max(0.0, now - float(entry.get("last_seen_ts") or 0.0))
    if age <= 0.5:
        return _SIGMA_FRESH_DEG
    grown = math.sqrt(_SIGMA_FRESH_DEG ** 2 + (_SIGMA_RATE_DEG_S * age) ** 2)
    return min(_SIGMA_MAX_DEG, grown)


def _entry_view(pid: str, e: dict, now: float, in_frame_ids: set) -> dict:
    sigma = _sigma_now(e, now)
    age = now - float(e.get("last_seen_ts") or 0.0)
    if pid in in_frame_ids:
        state = "visible"
    elif sigma <= _TIER_TRACKED_DEG:
        state = "tracked"
    elif sigma <= _TIER_REMEMBERED_DEG:
        state = "remembered"
    else:
        state = "dormant"
    out = {
        "person": pid,
        "state": state,
        "sigma_deg": round(sigma, 1),
        "age_s": round(age, 1),
        "sightings": int(e.get("sightings") or 0),
        "last_conf": round(float(e.get("last_conf") or 0.0), 3),
        "p_present": round(float(e.get("p_present", 1.0)), 3),
        "negative_looks": int(e.get("negative_looks") or 0),
    }
    # DORMANT drops the POSITION, never the person. Reporting a bearing here
    # would be the whole failure this ladder exists to prevent: a number that
    # reads as present-tense fact when it is a several-minute-old rumour.
    if state != "dormant":
        out["pan_deg"] = round(float(e.get("pan_deg") or 0.0), 1)
        out["tilt_deg"] = round(float(e.get("tilt_deg") or 0.0), 1)
        pose = e.get("pose") or {}
        if pose and (now - float(pose.get("ts") or 0.0)) <= 30.0:
            out["distance_m"] = pose.get("distance_m")
            out["posture"] = pose.get("posture")
            out["activity"] = pose.get("activity")
            out["extent"] = pose.get("extent")
            out["pose_age_s"] = round(now - float(pose.get("ts") or 0.0), 1)
    else:
        out["pan_deg"] = None
        out["tilt_deg"] = None
        out["last_known_pan_deg"] = round(float(e.get("pan_deg") or 0.0), 1)
    return out


def _in_my_view(pan: float, head: dict, margin_deg: float = 4.0) -> bool:
    """Was that bearing actually inside the frame I just looked at?

    ★ THE GATE THAT PREVENTS 'OUT OF SIGHT, OUT OF MIND'. Not seeing someone
    is only evidence of absence if I was LOOKING somewhere they would have
    shown up. Decaying belief in a person because I was pointed at a wall is
    the single most common way these systems convince themselves an entire
    human has left the room. Margin pulls the edges in, because a face
    straddling the frame border is a coin flip, not an observation.
    """
    half = (_HFOV_DEG / 2.0) - margin_deg
    return abs(pan - float(head["pan_deg"])) <= half


def ingest(g: dict[str, Any]) -> dict:
    """Fold the current frame's recognised faces into the room map.

    Only NAMED faces are stored. An unrecognised face is a real thing in the
    room but it is not an identity, and inventing 'person_2' would create
    exactly the phantom-second-person error I made by eye earlier tonight.
    """
    sign = _pan_sign()
    if sign is None:
        return {"ok": False, "error": "uncalibrated",
                "detail": "pan sign has never been measured — run "
                          "room_map action='calibrate' first. Refusing to "
                          "guess, because the wrong sign mirrors every "
                          "bearing without failing."}
    head = _head_bearing(g)
    if head is None:
        return {"ok": False, "error": "no bearing available"}
    shape = _frame_shape(g)
    if shape is None:
        return {"ok": False, "error": "no fresh frame"}
    fw, fh = shape
    faces = g.get("_face_results") or []
    st = _state(g)
    people = st["people"]
    now = time.time()
    seen, unknown = [], 0

    # UNKNOWN OUTRANKS KNOWN (Zeke 2026-09-02): the largest unrecognised face
    # is kept as ONE pseudo-person "unknown" so the scheduler can prefer it.
    # It is a bearing with no identity — it expires when nobody unrecognised
    # has been seen for a while (identities persist; this is not one).
    unk_faces = [f for f in faces
                 if str(f.get("person_id") or "").strip().lower() in ("", "unknown", "none")]
    if unk_faces:
        def _area(f):
            b = f.get("bbox") or [0, 0, 0, 0]
            return max(0, b[2] - b[0]) * max(0, b[3] - b[1])
        big = max(unk_faces, key=_area)
        upan, utilt = face_bearing(big, fw, fh, head, sign)
        prev_u = people.get("unknown")
        if prev_u is not None:
            prev_u.update({"pan_deg": upan, "tilt_deg": utilt, "last_seen_ts": now,
                           "last_conf": 0.0, "sightings": int(prev_u.get("sightings") or 0) + 1,
                           "p_present": 1.0, "negative_looks": 0, "novel": True})
        else:
            people["unknown"] = {"pan_deg": upan, "tilt_deg": utilt, "last_seen_ts": now,
                                 "last_conf": 0.0, "first_seen_ts": now, "sightings": 1,
                                 "last_jump_deg": 0.0, "teleported": False,
                                 "p_present": 1.0, "negative_looks": 0, "novel": True}
    elif "unknown" in people and (now - float(people["unknown"].get("last_seen_ts") or 0.0)) > 600.0:
        del people["unknown"]          # no identity to keep — the position was all it was

    for f in faces:
        pid = str(f.get("person_id") or "").strip().lower()
        if not pid or pid in ("unknown", "none"):
            unknown += 1
            continue
        pan, tilt = face_bearing(f, fw, fh, head, sign)
        conf = float(f.get("confidence") or 0.0)
        prev = people.get(pid)
        if prev is not None:
            gate = _ASSOC_GATE_DEG + _sigma_now(prev, now)
            jump = abs(pan - float(prev.get("pan_deg") or pan))
            prev.update({"pan_deg": pan, "tilt_deg": tilt,
                         "last_seen_ts": now, "last_conf": conf,
                         "sightings": int(prev.get("sightings") or 0) + 1,
                         "last_jump_deg": round(jump, 1),
                         "teleported": bool(jump > gate),
                         # seeing them settles it: present, and the run of
                         # negative looks is over
                         "p_present": 1.0, "negative_looks": 0})
        else:
            people[pid] = {"pan_deg": pan, "tilt_deg": tilt,
                           "last_seen_ts": now, "last_conf": conf,
                           "first_seen_ts": now, "sightings": 1,
                           "last_jump_deg": 0.0, "teleported": False,
                           "p_present": 1.0, "negative_looks": 0}
        # BODY (2026-09-02, task 2): the live pose loop's read of this person —
        # distance from the eyes ruler / their true height, posture, activity.
        # A bearing says which way; this says how far and what they're doing.
        try:
            from brain import body_pose as _bp
            lp = _bp.live_person(g, pid, max_age_s=3.0)
            if lp is not None:
                people[pid]["pose"] = {
                    "ts": now,
                    "distance_m": (lp.get("distance") or {}).get("m"),
                    "ruler": (lp.get("distance") or {}).get("ruler"),
                    "assumed_height": (lp.get("distance") or {}).get("assumed_height"),
                    "posture": lp.get("posture"), "activity": lp.get("activity"),
                    "extent": lp.get("extent")}
        except Exception:
            pass
        seen.append(pid)

    # ── NEGATIVE EVIDENCE, GATED ON VISIBILITY ─────────────────────────────
    # Someone I remember, whose remembered bearing was inside this frame, and
    # who was NOT recognised in it. That is real evidence they have moved or
    # left — but ONLY because I was actually pointed at them. Everyone outside
    # the cone gets no update at all, which is the correct likelihood for
    # "I wasn't looking".
    looked_past = []
    for pid, e in people.items():
        if pid in seen:
            continue
        if float(e.get("last_seen_ts") or 0.0) <= 0:
            continue
        if not _in_my_view(float(e.get("pan_deg") or 0.0), head):
            continue                       # not my business this frame
        p = float(e.get("p_present", 1.0))
        # Bayes with a deliberately weak detector: P(present | miss)
        e["p_present"] = (_P_MISS * p) / max(1e-9, _P_MISS * p + (1.0 - p))
        e["negative_looks"] = int(e.get("negative_looks") or 0) + 1
        looked_past.append(pid)

    # NOTHING IS EVER DELETED. Positions expire (via the sigma ladder);
    # identities do not. A person who has been gone for hours is still a
    # person I know, with a last-known bearing and a time attached to it.
    st["last_ingest_ts"] = now
    return {"ok": True, "seen": seen, "unknown_faces": unknown,
            "head": head, "tracked": len(people),
            "absent_where_i_looked": looked_past,
            "bearing_confirmed": bool(head.get("confirmed"))}


def _roster(g: dict[str, Any]) -> dict:
    st = _state(g)
    now = time.time()
    in_frame = {str(f.get("person_id") or "").lower()
                for f in (g.get("_face_results") or [])}
    rows = [_entry_view(pid, e, now, in_frame)
            for pid, e in st["people"].items()]
    rows.sort(key=lambda r: r["age_s"])
    return {"ok": True, "people": rows, "count": len(rows),
            "last_ingest_age_s": (round(now - float(st.get("last_ingest_ts") or 0), 1)
                                  if st.get("last_ingest_ts") else None)}


def _where(g: dict[str, Any], pid: str) -> dict:
    st = _state(g)
    e = st["people"].get(pid.strip().lower())
    if not e:
        return {"ok": False, "person": pid, "known": False,
                "summary": f"I have no record of where {pid} is. Not 'they "
                           f"left' — I have simply never placed them."}
    now = time.time()
    in_frame = {str(f.get("person_id") or "").lower()
                for f in (g.get("_face_results") or [])}
    v = _entry_view(pid.strip().lower(), e, now, in_frame)
    head = _head_bearing(g)
    rel = None
    if head is not None and v.get("pan_deg") is not None:
        rel = round(v["pan_deg"] - float(head["pan_deg"]), 1)
    if v["state"] == "visible":
        s = f"{pid} is in view, at pan {v['pan_deg']:+.0f} deg"
        if v.get("distance_m"):
            s += f", about {v['distance_m']:.1f} m away"
        if v.get("posture"):
            s += f", {v['posture']}"
        if v.get("activity"):
            s += f" ({v['activity']})"
        s += "."
    elif v["state"] == "dormant":
        mins = v["age_s"] / 60.0
        s = (f"I know {pid}, but I do not know where they are. Last placed at "
             f"pan {v['last_known_pan_deg']:+.0f} about {mins:.0f} min ago — "
             f"that is a rumour now, not a location.")
    elif v["p_present"] < 0.3 and v["negative_looks"] > 0:
        s = (f"{pid} was at pan {v['pan_deg']:+.0f} deg {v['age_s']:.0f}s ago, "
             f"but I have since looked straight at that spot "
             f"{v['negative_looks']}x without seeing them — they have most "
             f"likely moved or left.")
    else:
        s = (f"{pid} was at pan {v['pan_deg']:+.0f} deg {v['age_s']:.0f}s ago, "
             f"give or take {v['sigma_deg']:.0f} deg.")
        if rel is not None:
            # ⚠ WHICH WAY IS 'RIGHT' DEPENDS ON THE MEASURED SIGN. With
            # PAN_SIGN = -1 (what this camera actually measured), a face on
            # the right of frame sits at a LOWER absolute pan, so a target
            # numerically above my bearing is to my LEFT. I wrote this
            # backwards first and only caught it setting up the look-away
            # test — the same mirrored-world failure the calibration exists
            # to prevent, reappearing one layer up in the words.
            sgn = _pan_sign() or 1.0
            side = "right" if (sgn * rel) > 0 else "left"
            s += f" That is {abs(rel):.0f} deg to my {side} of where I am pointed."
    return {"ok": True, "known": True, **v, "relative_pan_deg": rel,
            "summary": s}


def _calibrate(g: dict[str, Any], params: dict) -> dict:
    """MEASURE the pan sign against the hardware. Never inferred from source.

    Method: find a recognised face, note its horizontal position, pan the head
    a known amount, and see which way the face moved in the frame. If panning
    positive pushes the face LEFT (dx decreases), then a face on the right is
    reached by INCREASING pan, so PAN_SIGN = +1.

    Restores the original bearing afterwards whether it succeeds or fails.
    """
    step = float(params.get("step_deg") or 8.0)
    step = max(4.0, min(20.0, abs(step)))
    try:
        from brain import visual_attention as va
        from brain import frame_store
    except Exception as e:
        return {"ok": False, "error": f"imports unavailable: {e!r}"}

    act = va.build_actuator()
    if not getattr(act, "available", lambda: False)():
        return {"ok": False, "error": "no movable actuator available"}

    def _biggest_face() -> tuple[float, dict] | None:
        faces = g.get("_face_results") or []
        best, area = None, 0.0
        for f in faces:
            try:
                x1, y1, x2, y2 = [float(v) for v in list(f.get("bbox"))[:4]]
            except Exception:
                continue
            a = abs((x2 - x1) * (y2 - y1))
            if a > area:
                area, best = a, f
        if best is None:
            return None
        shape = _frame_shape(g)
        if shape is None:
            return None
        fw, _fh = shape
        x1, _y1, x2, _y2 = [float(v) for v in list(best.get("bbox"))[:4]]
        cx = (x1 + x2) / 2.0
        return ((cx - fw / 2.0) / (fw / 2.0), best)

    start = act.bearing()
    if not start:
        return {"ok": False, "error": "could not read starting bearing"}
    p0 = float(start.get("pan_deg") or 0.0)

    before = _biggest_face()
    if before is None:
        return {"ok": False, "error": "no face in frame",
                "detail": "calibration needs a face to watch move. Stand in "
                          "view and run it again."}
    dx0, _f0 = before

    moved = act.look_at(p0 + step)
    if not moved.get("ok"):
        return {"ok": False, "error": f"move refused: {moved.get('reason')}"}
    time.sleep(float(params.get("settle_s") or 2.0))
    try:
        frame_store.get_buffered_frame(max_age_sec=1.0)
    except Exception:
        pass
    after = _biggest_face()
    act.look_at(p0)                                  # always restore

    if after is None:
        return {"ok": False, "error": "lost the face during the test move",
                "detail": "try a smaller --step, or stand nearer the centre."}
    dx1, _f1 = after
    delta = dx1 - dx0
    if abs(delta) < 0.04:
        return {"ok": False, "error": "no measurable image shift",
                "dx_before": round(dx0, 3), "dx_after": round(dx1, 3),
                "detail": "the face did not move enough in frame to call it. "
                          "Larger step, or the head did not actually move."}

    # Face moved LEFT (delta < 0) when pan increased => +pan sweeps view right
    # => a face on the right (dx>0) is at a HIGHER absolute pan => sign +1.
    sign = 1.0 if delta < 0 else -1.0
    geom = _load_geom()
    geom.update({"pan_sign": sign, "hfov_deg": _HFOV_DEG,
                 "measured_ts": time.time(),
                 "measured_step_deg": step,
                 "dx_before": round(dx0, 3), "dx_after": round(dx1, 3),
                 "method": "watch a face shift while panning a known amount"})
    try:
        _GEOM_PATH.parent.mkdir(parents=True, exist_ok=True)
        _GEOM_PATH.write_text(json.dumps(geom, indent=2) + "\n",
                              encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"measured {sign:+.0f} but could not "
                                      f"save it: {e!r}"}
    return {"ok": True, "pan_sign": sign, "step_deg": step,
            "dx_before": round(dx0, 3), "dx_after": round(dx1, 3),
            "saved_to": str(_GEOM_PATH),
            "summary": f"panned +{step:.0f} deg and the face moved "
                       f"{'left' if delta < 0 else 'right'} in frame "
                       f"({dx0:+.2f} -> {dx1:+.2f}), so PAN_SIGN = {sign:+.0f}. "
                       f"Measured, not assumed."}


def _look(g: dict[str, Any], params: dict) -> dict:
    """Point the head at a remembered bearing — or at explicit degrees.

    This is the primitive the eyes never had. Every existing mover is a
    CONTROL LOOP that needs to already see its target: pursuit corrects a
    visible offset, search sweeps blind hotspots. Nothing could be told
    'turn to +15 degrees'. That is why a remembered position was useless
    even once I had one — knowing where Q is means nothing without a way
    to go there.
    """
    try:
        from brain import visual_attention as va
    except Exception as e:
        return {"ok": False, "error": f"imports unavailable: {e!r}"}
    act = va.build_actuator()
    if not getattr(act, "available", lambda: False)():
        return {"ok": False, "error": "no movable actuator available"}

    who = str(params.get("person") or params.get("who") or "").strip().lower()
    pan = params.get("pan_deg")
    tilt = params.get("tilt_deg")
    src = "explicit"
    if who:
        e = _state(g)["people"].get(who)
        if not e:
            return {"ok": False, "error": f"no record of {who}",
                    "detail": "nothing to turn toward — I have never placed "
                              "them in the room."}
        now = time.time()
        sigma = _sigma_now(e, now)
        if sigma >= _SIGMA_MAX_DEG - 1e-6 and not params.get("anyway"):
            return {"ok": False, "error": "memory too decayed",
                    "age_s": round(now - float(e["last_seen_ts"]), 1),
                    "sigma_deg": round(sigma, 1),
                    "detail": f"my memory of {who} has decayed past being a "
                              f"location. I can point the head there, but it "
                              f"would be theatre — pass anyway=true if you "
                              f"want it regardless."}
        pan, tilt, src = e["pan_deg"], e["tilt_deg"], f"remembered:{who}"
        # People live in a band (Zeke 09-02): a remembered PERSON bearing above
        # a standing head is a map error, never a place to look for them.
        if tilt is not None and float(tilt) > 30.0:
            tilt = 30.0

    if pan is None:
        return {"ok": False,
                "error": "pass person='q', or pan_deg (and optional tilt_deg)"}
    r = act.look_at(float(pan), None if tilt is None else float(tilt))
    if not r.get("ok"):
        return {"ok": False, "error": f"move refused: {r.get('reason')}"}
    return {"ok": True, "moved": True, "source": src,
            "pan_deg": round(float(pan), 1),
            "tilt_deg": None if tilt is None else round(float(tilt), 1),
            "bearing": r.get("bearing"),
            "note": "a success dict is not motion — confirm with a frame if "
                    "it matters."}


# ── ATTENTION SCHEDULING ───────────────────────────────────────────────────
# Built 2026-08-30 from the gaze-arbitration research, not invented. Sources
# and why each number is what it is:
#
#  * Persistent-monitoring control theory (Cassandras et al., ACC 2017) gives
#    the core result: DWELL UNTIL UNCERTAINTY BOTTOMS OUT, THEN SWITCH. The
#    control variable is a DEPARTURE THRESHOLD, not a timer. So the scheduler
#    scores on sigma — how stale my belief about each person is — rather than
#    round-robining on a clock.
#  * Mishra & Skantze (RO-MAN 2022, Furhat) give social priority: active
#    speaker 0.60, plain listening 0.40, being-spoken-to 0.30 — a 2:1 ratio,
#    NOT a hard lock on the speaker. And the intimacy cap: never hold
#    continuous gaze on one person past 3-5 s.
#  * Kismet / Frontiers-2020 give HABITUATION as the anti-jitter mechanism:
#    the current target's own gain decays while I look at it, so switching
#    happens without a timer and without oscillation.
#  * Mutlu et al. (HRI 2009) is the reason this is worth doing carefully at
#    all: gaze PROPORTION alone assigns people the social roles of addressee
#    / bystander / overhearer, and they behave accordingly. How I split my
#    attention between two people is not cosmetic.
#
# What I deliberately did NOT copy: the full three-layer dynamical network
# (STM / habituation / lateral-inhibition ODEs). It needs a 10 Hz integration
# loop and audio DOA I do not have. The habituation term is the part that
# earns its keep; the rest would be ceremony.
_DWELL_MIN_S = 1.6            # human mean face fixation ~1 s; 2.2 s mutual-gaze
_DWELL_MAX_S = 4.5            # intimacy cap (3-5 s) — forces the look-away
_SWITCH_MARGIN = 1.25         # rival must beat the incumbent by 25% to steal
_HAB_DEPLETE = 0.35           # gain lost per second of continuous attention
_HAB_RECOVER = 0.55           # gain regained per second while unattended


def _score_people(g: dict[str, Any], now: float) -> list[dict]:
    """Score every known person for how much they deserve the next look."""
    st = _state(g)
    in_frame = {str(f.get("person_id") or "").lower()
                for f in (g.get("_face_results") or [])}
    cur = st.get("attending")
    cur_since = float(st.get("attending_since") or 0.0)
    rows = []
    for pid, e in st["people"].items():
        sigma = _sigma_now(e, now)
        # Staleness drive: normalised to the fraction of my FOV the doubt has
        # eaten. This IS the persistent-monitoring R_i — it grows while
        # unobserved and collapses the moment they are in frame.
        drive = min(1.0, sigma / _TIER_REMEMBERED_DEG)
        if pid in in_frame:
            drive = 0.0
        # Social priority. No audio DOA here, so 'active speaker' is not
        # available; presence and recency stand in.
        prio = 0.40 if pid in in_frame else 0.30
        if pid == "unknown":
            # Novelty beats familiarity (Zeke 09-02) — until the unknown has
            # been STUDIED (a study_face result in the last 10 min), then it
            # drops to a plain bystander so I don't stare at a guest all night.
            last_study = float(((g.get("_study_face") or {}).get("last") or {}).get("ts") or 0.0)
            studied = (now - last_study) < 600.0
            prio = 0.35 if studied else 0.75
            if pid not in in_frame:
                drive = min(1.0, drive + 0.25)   # an unnamed thing I lost sight of is worth a look
        if float(e.get("p_present", 1.0)) < 0.3:
            prio *= 0.4        # I looked and they were gone; stop hunting
        hab = float(e.get("hab", 1.0))
        rows.append({"person": pid, "sigma_deg": round(sigma, 1),
                     "drive": round(drive, 3), "priority": prio,
                     "habituation": round(hab, 3),
                     "score": round((prio + drive) * hab, 4),
                     "is_current": pid == cur,
                     "attended_s": round(now - cur_since, 1)
                                   if pid == cur and cur_since else 0.0})
    rows.sort(key=lambda r: -r["score"])
    return rows


def _attend(g: dict[str, Any], params: dict) -> dict:
    """One scheduler tick: update habituation, pick a target, maybe move.

    Returns its reasoning, because a gaze policy that cannot say WHY it looked
    away from someone is impossible to debug and slightly sinister.
    """
    sign = _pan_sign()
    if sign is None:
        return {"ok": False, "error": "uncalibrated — run action='calibrate'"}
    ing = ingest(g)
    if not ing.get("ok"):
        return {"ok": False, "error": ing.get("error"), "detail": ing.get("detail")}
    now = time.time()
    st = _state(g)
    dt = min(2.0, max(0.0, now - float(st.get("last_attend_ts") or now)))
    st["last_attend_ts"] = now

    cur = st.get("attending")
    in_frame = {str(f.get("person_id") or "").lower()
                for f in (g.get("_face_results") or [])}
    # Habituation: deplete whoever I am actually looking at, recover everyone
    # else. This is what makes me tire of a face and glance away on my own.
    for pid, e in st["people"].items():
        hab = float(e.get("hab", 1.0))
        # ⚠ Depletes on ATTENDING, not on SEEING. First version required the
        # person to also be recognised in frame, and the two-person simulation
        # immediately showed why that is wrong: whoever the recogniser happened
        # to be resolving got tired of quickly (2.1 s) while the other was held
        # until the intimacy cap (4.9 s) — a 2:1 split created purely by a
        # detector artifact. Mutlu (HRI 2009) is the reason that matters:
        # gaze PROPORTION assigns people the roles of addressee vs bystander,
        # and they behave accordingly. I am not willing to demote someone to
        # bystander because face recognition blinked.
        # The cost of staring is about where my head is POINTED — which is what
        # the other person actually perceives — so that is what it keys on.
        if pid == cur:
            hab -= _HAB_DEPLETE * dt
        else:
            hab += _HAB_RECOVER * dt
        e["hab"] = max(0.15, min(1.0, hab))

    rows = _score_people(g, now)
    if not rows:
        return {"ok": True, "moved": False, "target": None,
                "reason": "nobody in the room map yet"}

    best = rows[0]
    held = now - float(st.get("attending_since") or 0.0) if cur else 1e9
    decision, target = "hold", cur

    if cur is None:
        decision, target = "acquire", best["person"]
    elif best["person"] == cur:
        if held >= _DWELL_MAX_S and len(rows) > 1:
            # Intimacy cap: staring is its own failure mode, even when the
            # score says stay.
            decision, target = "release", rows[1]["person"]
        else:
            decision, target = "hold", cur
    elif held < _DWELL_MIN_S:
        decision, target = "hold_min_dwell", cur
    else:
        incumbent = next((r for r in rows if r["person"] == cur), None)
        inc_score = incumbent["score"] if incumbent else 0.0
        if best["score"] >= inc_score * _SWITCH_MARGIN:
            decision, target = "switch", best["person"]
        else:
            decision, target = "hold_margin", cur

    moved = None
    if decision in ("acquire", "switch", "release") and target:
        if not bool(params.get("dry_run")):
            moved = _look(g, {"person": target, "anyway": True})
        st["attending"] = target
        st["attending_since"] = now
    return {"ok": True, "decision": decision, "target": target,
            "held_s": round(min(held, 999.9), 1), "moved": moved,
            "scores": rows,
            "reason": {
                "hold": "still the best use of my eyes",
                "hold_min_dwell": f"a rival scores higher but I have only been "
                                  f"on {cur} {held:.1f}s; switching faster than "
                                  f"{_DWELL_MIN_S}s reads as twitching",
                "hold_margin": "rival is ahead but not by the 25% margin — "
                               "near-ties should not move my head",
                "switch": "rival cleared the margin after the minimum dwell",
                "release": f"held {held:.1f}s, past the {_DWELL_MAX_S}s cap — "
                           f"looking at someone forever is its own rudeness",
                "acquire": "was not attending anyone",
            }.get(decision, "")}


def _room_map(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "status").lower()
    try:
        if action == "calibrate":
            return _calibrate(g, params)
        if action == "look":
            return _look(g, params)
        if action == "attend":
            return _attend(g, params)
        if action == "ingest":
            return ingest(g)
        if action == "where":
            who = str(params.get("person") or params.get("who") or "").strip()
            if not who:
                return {"ok": False, "error": "pass person='q'"}
            ingest(g)                       # freshen before answering
            return _where(g, who)
        if action == "forget":
            _state(g)["people"].clear()
            return {"ok": True, "cleared": True}
        if action == "status":
            ing = ingest(g)
            out = _roster(g)
            out["ingest"] = ing
            if not ing.get("ok"):
                out["warning"] = ing.get("detail") or ing.get("error")
            return out
        return {"ok": False,
                "error": f"unknown action {action!r} — "
                         f"status|where|ingest|calibrate|forget"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:300]}


register_tool(
    "room_map",
    "WORLD-FRAME memory of who is where, so people survive leaving my view. "
    "Converts each recognised face into an ABSOLUTE room bearing (head pan/tilt "
    "+ pixel offset), so 'where is Q' stays answerable while I look at someone "
    "else — the thing image-space tracking and attention_multi both cannot do "
    "(multi has no identity; pursuit has no memory). Uncertainty GROWS while a "
    "person is unobserved and the entry degrades to 'stale' rather than lying. "
    "Also the eyes' MISSING PRIMITIVE: action='look' turns the head to an "
    "absolute bearing or to a remembered person — every other mover is a "
    "control loop that must already SEE its target. "
    "action='status' (roster) | 'where' (+person='q') | 'look' (+person, or "
    "pan_deg/tilt_deg) | 'ingest' | 'calibrate' (measures the pan sign against "
    "hardware — required once before any bearing is trusted) | 'forget'.",
    1,
    _room_map,
)
