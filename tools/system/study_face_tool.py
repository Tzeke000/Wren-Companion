# SELF_ASSESSMENT: I turn "a face I don't know while Zeke is away" into evidence — head on them, a frame burst, a face crop and a small contact sheet cognition can safely look at — so the study happens the same way every time instead of being improvised at wake.
"""
study_face — Zeke's standing rule (Discord, 2026-09-02, from Cpl's Course):

    "with your eyes a unrecognized face is kinda more important when I'm not
     in the room. My phone not connected to the WiFi and you see a face you
     don't know — you should study it."

What "study" means here, mechanically:
  1. keep the head on them (retarget the smooth-pursuit servo to the unknown
     face — only when nobody KNOWN is in frame; a guest beside Zeke is his call),
  2. burst-capture frames through the video loop's enroll hook into
     faces/_drafts/study_<ts>/ (the same invisible-to-the-recognizer layout
     brain/unknown_capture.py uses),
  3. cut a face crop from the live frame and build ONE small contact sheet
     (<=900px, <150KB) — the ONLY picture cognition should Read (a 476KB PNG
     froze the host on 08-29; big images wedge it far below 1MB),
  4. write study.json with what the sensors said (bbox, confidence, the
     InsightFace age/gender GUESS — labelled a guess, never a fact),
  5. hand it back. Cognition looks, describes, and DMs Zeke the crop on
     Discord (chat_id 1504668879220117725). Watch-and-report; never speak
     to them — his default until he says otherwise.

action='start' (frames=6, interval_s=0.7, track=True) | 'release' (servo back
to its persisted target) | 'status' | 'rearm_watcher' (restart the
unknown_capture watcher on fresh code — brain_hot_swap can't swap a live
thread's bound method).
"""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

ROOT = Path(__file__).resolve().parents[2]
DRAFTS_DIR = ROOT / "faces" / "_drafts"
SHEET_MAX_W = 900
SHEET_MAX_BYTES = 150_000
CAPTURE_TIMEOUT_S = 25.0
BUSY_WAIT_S = 20.0
DISCORD_CHAT_ID = "1504668879220117725"


def _split(faces: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    knowns, unknowns = [], []
    for r in faces or []:
        if not isinstance(r, dict):
            continue
        pid = str(r.get("person_id") or "unknown")
        (unknowns if (pid == "unknown" or pid.startswith("_")) else knowns).append(r)
    return knowns, unknowns


def _face_summary(f: dict[str, Any]) -> dict[str, Any]:
    bbox = f.get("bbox") or [0, 0, 0, 0]
    try:
        x1, y1, x2, y2 = [int(v) for v in bbox[:4]]
    except Exception:
        x1 = y1 = x2 = y2 = 0
    return {
        "person_id": str(f.get("person_id") or "unknown"),
        "confidence": round(float(f.get("confidence") or 0.0), 3),
        "bbox": [x1, y1, x2, y2],
        "face_px": max(0, x2 - x1) * max(0, y2 - y1),
        # Labelled a GUESS on purpose: the standing rule is never to state
        # these as facts (they are the person's / Zeke's to state).
        "insightface_guess": {"age": f.get("age"), "gender": f.get("gender")},
    }


def _presence() -> dict[str, Any]:
    try:
        from brain import unknown_capture
        return unknown_capture.zeke_presence()
    except Exception as e:  # noqa: BLE001
        return {"away": None, "reason": f"presence helper missing: {e!r}"[:100]}


def _servo(g: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    try:
        from tools.tool_registry import _REGISTRY
        td = _REGISTRY.get("attention_smooth")
        if td is None:
            return {"ok": False, "error": "attention_smooth not registered"}
        return td.handler(params, g)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:160]}


def _write_small_jpeg(cv2, img, path: Path, max_w: int = SHEET_MAX_W,
                      max_bytes: int = SHEET_MAX_BYTES) -> dict[str, Any]:
    h, w = img.shape[:2]
    if w > max_w:
        scale = max_w / float(w)
        img = cv2.resize(img, (max_w, max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    q = 80
    while True:
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
        if not ok:
            return {"ok": False, "error": "imencode failed"}
        if len(buf) <= max_bytes or q <= 35:
            path.write_bytes(buf.tobytes())
            return {"ok": True, "path": str(path), "bytes": int(len(buf)),
                    "quality": q, "size": [int(img.shape[1]), int(img.shape[0])]}
        q -= 10


def _study_start(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        from brain import frame_store
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"imports: {e!r}"}

    frames_n = int(params.get("frames") or 6)
    interval = float(params.get("interval_s") or 0.7)
    track = params.get("track") is not False
    st = g.setdefault("_study_face", {})

    faces = list(g.get("_face_results") or [])
    knowns, unknowns = _split(faces)
    pres = _presence()
    if not unknowns:
        return {"ok": False, "error": "no unknown face in frame right now",
                "faces_now": [_face_summary(f) for f in faces],
                "zeke_presence": pres}

    ts_label = time.strftime("%Y%m%d_%H%M%S")
    study_id = f"study_{ts_label}"
    pid = f"_drafts/{study_id}"
    out_dir = DRAFTS_DIR / study_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) head on them — only when nobody known is in frame.
    servo: dict[str, Any] = {"retargeted": False}
    if track and not knowns:
        prev = (g.get("_attention_state_obj") or {}).get("target")
        st["restore_target"] = prev or (g.get("_attention_smooth") or {}).get(
            "auto_start_target") or "person:zeke"
        r = _servo(g, {"action": "start", "target": "person:unknown", "pin": False})
        servo = {"retargeted": bool(r.get("ok")), "result": r,
                 "restore_target": st["restore_target"]}
    elif knowns:
        servo["note"] = ("someone known is in frame — not retargeting the head "
                         "(a guest beside a known person is their call)")

    # 2) burst through the enroll hook; wait out another capture if one is live.
    t0 = time.time()
    while g.get("_enroll_request") is not None and time.time() - t0 < BUSY_WAIT_S:
        time.sleep(0.5)
    if g.get("_enroll_request") is not None:
        return {"ok": False, "error": "enroll hook busy for 20s (unknown_capture "
                                      "or enroll_face mid-burst) — retry",
                "servo": servo, "zeke_presence": pres}
    g["_enroll_result"] = None
    g["_enroll_request"] = {
        "pid": pid, "count": frames_n, "remaining": frames_n,
        "interval_s": max(0.2, interval), "require_face": True,
        "saved_paths": [], "last_saved_ts": 0.0, "started_ts": time.time(),
        "known_count_before": 0,
    }
    saved: list[str] = []
    complete = False
    deadline = time.time() + CAPTURE_TIMEOUT_S
    while time.time() < deadline:
        res = g.get("_enroll_result")
        if isinstance(res, dict) and str(res.get("pid") or "") == pid:
            g["_enroll_result"] = None
            saved = list(res.get("saved_paths") or [])
            complete = True
            break
        time.sleep(0.25)
    if not complete:
        req = g.get("_enroll_request")
        if isinstance(req, dict) and str(req.get("pid") or "") == pid:
            saved = list(req.get("saved_paths") or [])
            g["_enroll_request"] = None

    # 3) crop + contact sheet from the LIVE frame and the latest face results.
    faces_now = list(g.get("_face_results") or [])
    knowns_now, unknowns_now = _split(faces_now)
    crop_info: dict[str, Any] = {"ok": False, "reason": "no live frame"}
    sheet_info: dict[str, Any] = {"ok": False}
    live = frame_store.get_buffered_frame(max_age_sec=3.0)
    frame = live.frame
    if frame is None and saved:
        frame = cv2.imread(saved[-1])
        crop_info["reason"] = "live frame stale — used last saved frame"
    if frame is not None:
        h, w = frame.shape[:2]
        target = max(unknowns_now or unknowns, key=lambda f: _face_summary(f)["face_px"])
        x1, y1, x2, y2 = _face_summary(target)["bbox"]
        if x2 > x1 and y2 > y1:
            mx, my = int((x2 - x1) * 0.45), int((y2 - y1) * 0.6)
            cx1, cy1 = max(0, x1 - mx), max(0, y1 - my)
            cx2, cy2 = min(w, x2 + mx), min(h, y2 + my)
            crop = frame[cy1:cy2, cx1:cx2]
            crop_info = _write_small_jpeg(cv2, crop, out_dir / "crop.jpg",
                                          max_w=600, max_bytes=120_000)
            crop_info["bbox_used"] = [cx1, cy1, cx2, cy2]
        # sheet: full frame (downscaled) with the crop inset top-left.
        try:
            sheet = frame.copy()
            if crop_info.get("ok"):
                ins = cv2.imread(crop_info["path"])
                if ins is not None:
                    ih = max(80, int(h * 0.35))
                    iw = max(1, int(ins.shape[1] * ih / float(ins.shape[0])))
                    ins = cv2.resize(ins, (iw, ih), interpolation=cv2.INTER_AREA)
                    sheet[8:8 + ih, 8:8 + iw] = ins
                    cv2.rectangle(sheet, (8, 8), (8 + iw, 8 + ih), (0, 220, 0), 2)
            for f in faces_now:
                fx1, fy1, fx2, fy2 = _face_summary(f)["bbox"]
                cv2.rectangle(sheet, (fx1, fy1), (fx2, fy2), (0, 165, 255), 2)
            sheet_info = _write_small_jpeg(cv2, sheet, out_dir / "sheet.jpg")
        except Exception as e:  # noqa: BLE001
            sheet_info = {"ok": False, "error": repr(e)[:120]}

    record = {
        "study_id": study_id,
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "zeke_presence": pres,
        "faces_at_start": [_face_summary(f) for f in faces],
        "faces_at_end": [_face_summary(f) for f in faces_now],
        "known_present": [str(k.get("person_id")) for k in knowns_now],
        "frames_saved": len(saved),
        "frame_paths": saved,
        "complete": complete,
        "crop": crop_info,
        "sheet": sheet_info,
        "servo": servo,
        "dir": str(out_dir),
        "discord_chat_id": DISCORD_CHAT_ID,
    }
    try:
        (out_dir / "study.json").write_text(json.dumps(record, indent=2),
                                            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        record["study_json_error"] = repr(e)[:120]
    st["last"] = record
    try:
        bus = g.get("_signal_bus")
        if bus is not None:
            bus.fire("face_study", data={"study_id": study_id, "dir": str(out_dir),
                                         "frames": len(saved),
                                         "zeke_away": pres.get("away")},
                     priority="high")
    except Exception:
        pass
    out = dict(record)
    out["ok"] = True
    out["next"] = ("Read ONLY sheet.jpg (pre-shrunk, safe). Say what you can about "
                   "who/what you see — and check frame CONTEXT: a face on a SCREEN "
                   "reads as a visitor. Then DM Zeke on Discord with crop.jpg "
                   f"(chat_id {DISCORD_CHAT_ID}); do not speak to them. "
                   "Finish with study_face action=release.")
    return out


def _study_release(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    st = g.setdefault("_study_face", {})
    target = (st.pop("restore_target", None)
              or (g.get("_attention_smooth") or {}).get("auto_start_target")
              or "person:zeke")
    r = _servo(g, {"action": "start", "target": target})
    return {"ok": bool(r.get("ok")), "restored_target": target, "servo": r}


def _rearm_watcher(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Restart brain.unknown_capture's watcher on freshly loaded code."""
    try:
        from brain import unknown_capture as uc
        old = g.get("_unknown_capture_watcher")
        if old is not None:
            try:
                old._stop.set()
                if old._thread is not None:
                    old._thread.join(timeout=3.0)
            except Exception:
                pass
            g["_unknown_capture_watcher"] = None
        uc = importlib.reload(uc)
        status = uc.start(g)
        return {"ok": True, "watcher": status,
                "presence": uc.zeke_presence()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200]}


def _study_face(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "status").lower()
    if action == "start":
        return _study_start(params, g)
    if action == "release":
        return _study_release(params, g)
    if action == "rearm_watcher":
        return _rearm_watcher(params, g)
    if action == "status":
        st = g.get("_study_face") or {}
        faces = list(g.get("_face_results") or [])
        knowns, unknowns = _split(faces)
        try:
            from brain import unknown_capture
            watcher = unknown_capture.status(g)
        except Exception as e:  # noqa: BLE001
            watcher = {"error": repr(e)[:100]}
        return {"ok": True, "last": st.get("last"),
                "restore_target": st.get("restore_target"),
                "faces_now": {"known": [str(k.get("person_id")) for k in knowns],
                              "unknown": len(unknowns)},
                "zeke_presence": _presence(), "unknown_capture": watcher}
    return {"ok": False,
            "error": f"unknown action {action!r} — start|release|status|rearm_watcher"}


register_tool(
    "study_face",
    "STUDY an unrecognised face (Zeke's rule 2026-09-02: matters MORE when his "
    "phone is off the wifi). action='start' puts the head on them, burst-captures "
    "frames to faces/_drafts/study_<ts>/, cuts crop.jpg and a SMALL sheet.jpg "
    "(safe to Read), writes study.json; then cognition looks + DMs Zeke the crop "
    "(never speaks to them). 'release' returns the head to its persisted target; "
    "'status'; 'rearm_watcher' restarts the unknown_capture watcher on new code.",
    2,
    _study_face,
)
