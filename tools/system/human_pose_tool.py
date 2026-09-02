# SELF_ASSESSMENT: I expose the body-pose eyes (brain/body_pose.py) as a tool — read the live frame for joints, posture, visible extent and a distance estimate, store a person's true height, and draw a skeleton I can safely look at.
"""
human_pose — Zeke's ask (Discord 2026-09-02 ~16:1x): see BODIES, not boxes —
where the elbow is relative to the head, how much of a person is visible
(→ how far away, given their height), and in plain words what they're doing.

action='read'  (person=<hint e.g. 'zeke'>, height_m=<override>, draw=true)
               → joints/posture/extent/distance per person + a sentence;
                 draw=true also writes state/human_pose_last.jpg (<=900px, safe to Read)
action='status'                          → model loaded, latency, stored heights
action='set_height' person= height_m=    → store a person's TRUE height (their word)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

ROOT = Path(__file__).resolve().parents[2]
OUT_JPG = ROOT / "state" / "human_pose_last.jpg"


def _human_pose(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "read").lower()
    try:
        from brain import body_pose as bp
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"brain.body_pose import failed: {e!r}"[:200]}

    if action == "status":
        return {"ok": True, **bp.status()}

    if action == "set_height":
        person = str(params.get("person") or "").strip().lower()
        try:
            h = float(params.get("height_m"))
        except Exception:
            return {"ok": False, "error": "pass person= and height_m= (metres, e.g. 1.78)"}
        if not person or not (1.2 <= h <= 2.3):
            return {"ok": False, "error": "person missing or height_m outside 1.2–2.3 m"}
        return bp.set_height(person, h, source=str(params.get("source") or "zeke"))

    if action == "read":
        try:
            from brain import frame_store
            res = frame_store.get_buffered_frame(max_age_sec=3.0)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"frame_store: {e!r}"[:160]}
        frame = res.frame
        if frame is None:
            return {"ok": False, "error": "no live frame (camera down or stale)"}
        hint = params.get("person")
        if hint is None:
            # default the hint to whoever the recogniser currently sees
            faces = g.get("_face_results") or []
            known = [str(f.get("person_id")) for f in faces
                     if str(f.get("person_id") or "unknown") not in ("unknown", "")]
            hint = known[0] if len(known) == 1 else None
        h_override = params.get("height_m")
        tracks = []
        try:
            from brain import person_track
            tracks, _sz = person_track.track_boxes()
        except Exception:
            tracks = []
        head = (g.get("_attention_state_obj") or {}).get("bearing")
        out = bp.analyze(frame, person_hint=hint,
                         height_m=float(h_override) if h_override else None,
                         conf=float(params.get("conf") or 0.25),
                         faces=list(g.get("_face_results") or []), tracks=tracks,
                         head=head, imgsz=int(params.get("imgsz") or 1280))
        out["head"] = head
        g["_human_pose_last"] = out
        if params.get("draw") and out.get("ok"):
            try:
                import cv2  # type: ignore
                img = bp.draw(frame, out)
                h, w = img.shape[:2]
                if w > 900:
                    img = cv2.resize(img, (900, int(h * 900 / w)), interpolation=cv2.INTER_AREA)
                q = 80
                while True:
                    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
                    if ok and (len(buf) <= 150_000 or q <= 35):
                        OUT_JPG.write_bytes(buf.tobytes())
                        out["drawn"] = {"path": str(OUT_JPG), "bytes": int(len(buf))}
                        break
                    q -= 10
            except Exception as e:  # noqa: BLE001
                out["drawn"] = {"error": repr(e)[:120]}
        # keep the heavy per-joint table but put the readable bits first
        out["person_hint"] = hint
        return out

    if action == "mark_static":
        # Teach it a humanoid NON-person at the current head bearing: pass the
        # index from the last read (default: the first unverified one) + a label.
        last = g.get("_human_pose_last") or {}
        persons = last.get("persons") or []
        label = str(params.get("label") or "").strip()
        if not label:
            return {"ok": False, "error": "pass label= (e.g. 'spartan helmet statue on the dresser')"}
        idx = params.get("index")
        cand = None
        if idx is not None:
            cand = next((p for p in persons if p["index"] == int(idx)), None)
        else:
            cand = next((p for p in persons if not p.get("verified_person")), None)
        if cand is None:
            return {"ok": False, "error": "no matching detection in the last read — run action=read first"}
        return bp.mark_static(cand["box"], last.get("head"), label)

    return {"ok": False, "error": f"unknown action {action!r} — read|status|set_height|mark_static"}


register_tool(
    "human_pose",
    "BODY pose from the live frame (YOLO11-pose, 17 joints/person, ~20ms on the GPU): "
    "visible extent (head-only … full body), posture in words (standing/sitting/lying/"
    "leaning/upright-legs-hidden), hands raised/together, and a distance estimate from "
    "the best visible body ruler + the person's TRUE height (set_height; else flagged "
    "assumed). action='read' (person=, height_m=, draw=true → state/human_pose_last.jpg "
    "safe to Read) | 'status' | 'set_height' person= height_m=.",
    2,
    _human_pose,
)
