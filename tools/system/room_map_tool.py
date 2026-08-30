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
_FORGET_S = 900.0             # 15 min unseen -> drop the entry entirely

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
    """Absolute room bearing of a face, in degrees.

    Square pixels + one optical axis => the per-pixel angular scale is the same
    on both axes, so the vertical field falls out of the aspect ratio rather
    than needing its own constant.
    """
    x1, y1, x2, y2 = [float(v) for v in list(face.get("bbox") or [0, 0, 0, 0])[:4]]
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    dx = (cx - frame_w / 2.0) / (frame_w / 2.0)      # -1..1
    dy = (cy - frame_h / 2.0) / (frame_h / 2.0)
    vfov = _HFOV_DEG * (float(frame_h) / float(max(1, frame_w)))
    pan = float(head["pan_deg"]) + pan_sign * dx * (_HFOV_DEG / 2.0)
    # +y pixels point DOWN, so a face below centre is at a LOWER elevation.
    tilt = float(head["tilt_deg"]) - dy * (vfov / 2.0)
    return pan, tilt


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
    elif sigma >= _SIGMA_MAX_DEG - 1e-6:
        state = "stale"
    else:
        state = "remembered"
    return {
        "person": pid,
        "state": state,
        "pan_deg": round(float(e.get("pan_deg") or 0.0), 1),
        "tilt_deg": round(float(e.get("tilt_deg") or 0.0), 1),
        "sigma_deg": round(sigma, 1),
        "age_s": round(age, 1),
        "sightings": int(e.get("sightings") or 0),
        "last_conf": round(float(e.get("last_conf") or 0.0), 3),
    }


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
                         "teleported": bool(jump > gate)})
        else:
            people[pid] = {"pan_deg": pan, "tilt_deg": tilt,
                           "last_seen_ts": now, "last_conf": conf,
                           "first_seen_ts": now, "sightings": 1,
                           "last_jump_deg": 0.0, "teleported": False}
        seen.append(pid)

    for pid in [p for p, e in people.items()
                if now - float(e.get("last_seen_ts") or 0.0) > _FORGET_S]:
        people.pop(pid, None)

    st["last_ingest_ts"] = now
    return {"ok": True, "seen": seen, "unknown_faces": unknown,
            "head": head, "tracked": len(people),
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
    if head is not None:
        rel = round(v["pan_deg"] - float(head["pan_deg"]), 1)
    if v["state"] == "visible":
        s = f"{pid} is in view, at pan {v['pan_deg']:+.0f} deg."
    elif v["state"] == "stale":
        s = (f"I last saw {pid} {v['age_s']:.0f}s ago at pan "
             f"{v['pan_deg']:+.0f}, but that memory has decayed past being a "
             f"location — treat it as 'somewhere', not 'there'.")
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


def _room_map(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "status").lower()
    try:
        if action == "calibrate":
            return _calibrate(g, params)
        if action == "look":
            return _look(g, params)
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
