# SELF_ASSESSMENT: I am Iris's PERSISTENT body — one held Vector control session
# with a live camera feed, so I stay SEATED in the robot instead of jumping out
# for every command.
"""Persistent body-session tools — 2026-07-15.

Zeke: "not move then check cam then move then check cam again ... you need to be
streamed the info ... do all the vector body things while seated in the body,
jumping out the body for a tool call wastes time and effort."

These wrap brain/vector_session.py (the held anki_vector control connection +
live camera feed + safety guard). The connection lives in iris_runtime module
state, so `body_open` once, then look/drive/turn/head/lift/dock all reuse the
SAME session — control never drops, the head never spuriously resets, and
`body_look` samples the live feed instantly (no per-grab MJPEG open/close).

Distinct from:
  * vector_body_tool.py  (vector_*)  — wire-pod /api-sdk RAW motion (per-call HTTP)
  * vector_action_tool.py (vector_turn/straight/...) — on-demand SDK session PER call
These body_* tools are the "stay seated" path; use them for piloting. The
observe/inhabit daemon (nerves/ears/battery/nav_map) keeps running alongside.

Hot-loadable via iris_tool_reload so the body layer can grow mid-session.
"""
from __future__ import annotations

from typing import Any

from tools.tool_registry import register_tool


def _sess():
    from brain import vector_session
    return vector_session


def _body_open(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Seat myself: open ONE held control session + live camera feed. Coexists
    with the observe daemon. Idempotent (returns status if already open)."""
    try:
        timeout = float(params.get("timeout") or 20.0)
    except Exception:
        timeout = 20.0
    return _sess().open_session(timeout=timeout)


def _body_close(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Un-seat: stop wheels, release control, disconnect. Stock brain resumes."""
    reason = str(params.get("reason") or "requested")
    return _sess().close_session(reason=reason)


def _body_status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Session + REAL battery (SDK is_charging) + wheels/head/reflex + nerves."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": True, "connected": False, "note": "no body session open"}
    return s.status()


def _body_look(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """MY EYES, instant: sample the live feed -> jpg path to Read. No stream
    open/close. Requires body_open."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    name = str(params.get("name") or "body_view").strip() or "body_view"
    return s.look(name=name, bright=bool(params.get("bright", False)))


def _body_perceive(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """ONE call = full LIVE fused body awareness (camera + depth + heading +
    lean + lift + head), streamed continuously in the background + how it just
    changed. Replaces the look-then-status stitch. Needs body_open."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    return s.perceive(name=str(params.get("name") or "perceive_view"),
                      save_frame=bool(params.get("frame", True)))


def _body_servo(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """CLOSED-LOOP drive-to-target: I set the goal, the body runs the fast
    control loop itself (steer-P + forward-P, edge-guarded) off the live stream
    until it arrives. Target: (x,y) absolute pose; or (x,y, relative=true); or
    (bearing_deg, dist_mm) relative to current heading. Needs body_open."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    return s.servo_to(
        x=params.get("x"), y=params.get("y"),
        bearing_deg=params.get("bearing_deg"), dist_mm=params.get("dist_mm"),
        standoff_mm=float(params.get("standoff_mm") or 25.0),
        max_speed=params.get("max_speed"),
        timeout_s=float(params.get("timeout_s") or 12.0),
        relative=bool(params.get("relative", False)))


def _body_drive(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Smooth velocity setpoint the guard HOLDS between my tool calls (no jerk,
    no head reset), edge-guarded. lw/rw mm/s (max 120); hold secs (default 3,
    guard keeps driving that long, refresh to continue); accel mm/s^2 ramp.
    lw=rw>0 forward; lw=rw<0 back; lw=-rw spins in place."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    return s.drive(lw=params.get("lw") or 0, rw=params.get("rw") or 0,
                   hold=params.get("hold") or 3.0,
                   accel=params.get("accel") if params.get("accel") is not None else 300.0)


def _body_stop(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Stop all motion now."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    return s.stop()


def _body_turn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Gyro-EXACT turn in place. angle_deg +left / -right. Restores head after."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        angle = float(params.get("angle_deg"))
    except Exception:
        return {"ok": False, "error": "angle_deg (float, +left/-right) required"}
    return s.turn(angle, speed_deg_s=float(params.get("speed_deg_s") or 90.0))


def _body_straight(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Encoder-EXACT straight line. dist_mm +fwd / -back. Cliff-safe. Restores head."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        dist = float(params.get("dist_mm"))
    except Exception:
        return {"ok": False, "error": "dist_mm (float, +fwd/-back) required"}
    return s.straight(dist, speed_mm_s=float(params.get("speed_mm_s") or 100.0))


def _body_pose(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Drive to a pose (x,y mm, heading deg), relative to current pose by default."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        x = float(params.get("x")); y = float(params.get("y"))
    except Exception:
        return {"ok": False, "error": "x and y (mm) required"}
    return s.go_to_pose(x, y, angle_deg=float(params.get("angle_deg") or 0.0),
                        relative=bool(params.get("relative", True)))


def _body_head(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Set head pitch (-22 down .. +45 up) and remember it (restored after behaviors)."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        a = float(params.get("angle_deg"))
    except Exception:
        return {"ok": False, "error": "angle_deg (-22..45) required"}
    return s.head(a, speed_deg_s=params.get("speed_deg_s"),
                  accel_deg_s2=params.get("accel_deg_s2"))


def _body_lift(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Set fork lift height 0.0 (down) .. 1.0 (up)."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    try:
        r = float(params.get("ratio"))
    except Exception:
        return {"ok": False, "error": "ratio (0.0..1.0) required"}
    return s.lift(r, speed=params.get("speed"), accel=params.get("accel"))


def _body_dock(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """NATIVE dock seat (drive_on_charger). Reliable last-3cm. ~55s from distance."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    return s.dock()


def _body_undock(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Drive off the charger."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open"}
    return s.undock()


def _body_detect(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """OPEN-VOCAB detection on my Vector eyes (OWL-ViT, morphed from vector-advanced-
    ai, no TensorRT). prompts = list or comma-string of TEXT queries ('an orange
    traffic cone','a cube','a small robot'). Uses the LIVE feed if a body session is
    open, else `path` (default the last body frame). threshold (0.05), bright (lift).
    ~0.7s on the 3060. Best in good light (dim feed limits detection)."""
    from brain import vector_owl
    prompts = params.get("prompts")
    if not prompts:
        return {"ok": False, "error": "prompts required (list or comma-string of text queries)"}
    s = _sess().get_session(create=False)
    path = None
    if s is not None and s.connected:
        r = s.look(name="owl_view")
        if r.get("ok"):
            path = r.get("path")
    if path is None:
        path = str(params.get("path") or r"D:\Wren-Companion\state\vector\body_view.jpg")
    out = vector_owl.detect(path, prompts,
                            threshold=float(params.get("threshold") or 0.05),
                            bright=bool(params.get("bright", False)))
    out["frame"] = path
    out["live"] = bool(s is not None and s.connected)
    return out


def _body_eyes(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Set Vector's eye color — SDK-NATIVE (no wire-pod). hue/sat 0..1 (my blue ~0.58)."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    try:
        hue = float(params.get("hue") if params.get("hue") is not None else 0.58)
    except Exception:
        return {"ok": False, "error": "hue (0..1) required"}
    return s.eyes(hue, sat=float(params.get("sat") if params.get("sat") is not None else 1.0))


def _body_say(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Speak text through Vector's own speaker — SDK-NATIVE (no wire-pod). Stock Vector voice."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    text = params.get("text")
    if not text:
        return {"ok": False, "error": "text required"}
    return s.say(str(text), vector_voice=bool(params.get("vector_voice", True)))


def _body_anim(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Play a built-in animation/trigger by name (chirps, expressions) — SDK-NATIVE.
    Best-effort (may need the anim list). E.g. 'GreetAfterLongTime', 'anim_pounce_success_02'."""
    s = _sess().get_session(create=False)
    if s is None or not s.connected:
        return {"ok": False, "error": "body session not open (call body_open)"}
    name = params.get("name")
    if not name:
        return {"ok": False, "error": "name required (animation trigger or anim name)"}
    return s.anim(str(name), loops=int(params.get("loops") or 1))


register_tool("body_open", "SEAT myself in Vector: open ONE held control session + live camera feed (coexists with observe daemon). Idempotent. Then use body_look/drive/turn/... without jumping out.", 1, _body_open)
register_tool("body_close", "Un-seat: stop, release control, disconnect. Stock brain resumes.", 1, _body_close)
register_tool("body_status", "Body session status + REAL battery (SDK is_charging) + wheels/head/reflex + nerves.", 1, _body_status)
register_tool("body_look", "MY EYES (instant): sample live feed -> jpg path to Read. Reports image_id/age/stale (feed-frozen check). bright=true = mild lift. Needs body_open.", 1, _body_look)
register_tool("body_perceive", "ONE CALL = full LIVE fused body-state, streamed ~15Hz in the background: latest camera frame + depth(prox) + heading(gyro) + lean(pitch/roll) + lift + head + how it all just CHANGED (moved/turned/prox-delta/frames-advanced). Replaces look+status stitch — the body never stops sensing. Needs body_open.", 1, _body_perceive)
register_tool("body_drive", "Smooth velocity setpoint the guard HOLDS (no jerk, no head reset), edge-guarded. lw/rw mm/s (max 220 = true hardware max), hold s (default 3), accel ramp. lw=rw>0 fwd, lw=-rw spin.", 1, _body_drive)
register_tool("body_servo", "CLOSED-LOOP drive-to-target: I set the goal, the BODY runs the fast control loop off the live stream (steer-P + forward-P, ramped, edge-guarded) until it arrives. Target: (x,y) absolute pose | (x,y,relative=true) | (bearing_deg,dist_mm) rel to heading. Opts: standoff_mm, max_speed, timeout_s. This is see-and-move-at-once. Needs body_open.", 1, _body_servo)
register_tool("body_stop", "Stop all Vector motion now.", 1, _body_stop)
register_tool("body_turn", "Gyro-EXACT turn in place. angle_deg +left/-right. Restores head after.", 1, _body_turn)
register_tool("body_straight", "Encoder-EXACT straight. dist_mm +fwd/-back. Cliff-safe. Restores head.", 1, _body_straight)
register_tool("body_pose", "Drive to a pose (x,y mm, angle_deg heading), relative to current by default.", 1, _body_pose)
register_tool("body_head", "Set Vector head pitch (-22 down..45 up); remembered + restored after behaviors. Optional speed_deg_s (SDK-native VARIABLE speed: omit=fast default, ~30=slow) + accel_deg_s2.", 1, _body_head)
register_tool("body_lift", "Set Vector fork lift 0.0 (down)..1.0 (up). Optional speed (rad/s, SDK-native variable speed: omit=fast ~10, ~2=slow) + accel.", 1, _body_lift)
register_tool("body_dock", "NATIVE dock seat (drive_on_charger). Reliable. ~55s from distance.", 1, _body_dock)
register_tool("body_undock", "Drive Vector off the charger.", 1, _body_undock)
register_tool("body_detect", "OPEN-VOCAB detection on my Vector eyes (OWL-ViT, no TensorRT). prompts=list/comma-string of TEXT queries. Live feed if seated else last frame. threshold/bright. ~0.7s on 3060; best in good light.", 1, _body_detect)
register_tool("body_eyes", "Set Vector's EYE COLOR — SDK-native (NO wire-pod). hue/sat 0..1 (my blue ~0.58). Replaces the wire-pod vector_eyes path.", 1, _body_eyes)
register_tool("body_say", "SPEAK text through Vector's own speaker — SDK-native (NO wire-pod), stock Vector voice. params: text. Replaces the wire-pod vector_say path.", 1, _body_say)
register_tool("body_anim", "Play a built-in ANIMATION/chirp/expression by name — SDK-native (NO wire-pod). params: name (trigger or anim), loops. Best-effort (may need anim list).", 1, _body_anim)
