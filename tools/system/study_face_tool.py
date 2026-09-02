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
     Discord (chat_id 1504668879220117725). Speaking to them is ALLOWED
     (Zeke 09-02 13:3x: "you can try and speak to a stranger, they might not hear
     you because you're in headphones") — ask who they are; never announce he's out.
  6. PHOTOGRAPHIC MEMORY (Zeke 09-02: "learn photographic memory in the ways you
     can"): every studied face is embedded (ArcFace, via the live InsightFace
     engine) into faces/_seen/<seen_id>/ and matched against every stranger seen
     before, so the result can say "this is the one from Tuesday" (seen_before).
     action='note' stores my words about them next to the face.

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


SEEN_DIR = ROOT / "faces" / "_seen"        # nested => invisible to the recognizer's loader
SEEN_THRESHOLD = 0.45                       # same cosine bar the engine uses for a positive ID
SEEN_MAX_EMB = 12                           # per stranger; keeps the gallery cheap


def _embed_frames(g: dict[str, Any], paths: list[str]) -> list[Any]:
    """ArcFace embeddings of the LARGEST face in each saved frame, via the live
    InsightFace engine (its own lock). Empty list if the engine is down."""
    engine = g.get("_insight_face")
    app = getattr(engine, "_app", None)
    lock = getattr(engine, "_lock", None)
    if app is None:
        return []
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return []
    out = []
    for pth in paths:
        try:
            img = cv2.imread(pth)
            if img is None:
                continue
            if lock is not None:
                with lock:
                    faces = app.get(img)
            else:
                faces = app.get(img)
            if not faces:
                continue
            # Largest UNKNOWN face: a burst often has Zeke in it too, and the
            # gallery must never learn a known person as a "stranger".
            match = getattr(engine, "_match", None)
            cands = []
            for z in faces:
                emb = getattr(z, "embedding", None)
                if emb is None:
                    continue
                pid = "unknown"
                if callable(match):
                    try:
                        pid, _sc = match(emb)
                    except Exception:
                        pid = "unknown"
                if pid != "unknown":
                    continue
                area = float((z.bbox[2] - z.bbox[0]) * (z.bbox[3] - z.bbox[1]))
                cands.append((area, emb))
            if not cands:
                continue
            _area, emb = max(cands, key=lambda t: t[0])
            out.append(np.asarray(emb, dtype="float32"))
        except Exception:
            continue
    return out


def _gallery_load() -> list[dict[str, Any]]:
    import numpy as np  # type: ignore
    entries = []
    if not SEEN_DIR.exists():
        return entries
    for d in sorted(SEEN_DIR.iterdir()):
        if not d.is_dir():
            continue
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {"seen_id": d.name}
        embs = None
        try:
            embs = np.load(d / "embeddings.npy")
        except Exception:
            pass
        entries.append({"seen_id": d.name, "dir": d, "meta": meta, "embs": embs})
    return entries


def _gallery_match(embs: list[Any]) -> tuple[dict[str, Any] | None, float]:
    """Best (entry, cosine) across strangers seen before; entry None if under the bar."""
    import numpy as np  # type: ignore
    best, best_s = None, 0.0
    for e in _gallery_load():
        if e["embs"] is None or not len(e["embs"]):
            continue
        for q in embs:
            qn = float(np.linalg.norm(q)) or 1.0
            for kv in e["embs"]:
                kn = float(np.linalg.norm(kv)) or 1.0
                sc = float(np.dot(q, kv) / (qn * kn))
                if sc > best_s:
                    best, best_s = e, sc
    if best is not None and best_s >= SEEN_THRESHOLD:
        return best, best_s
    return None, best_s


def _gallery_record(embs: list[Any], crop_path: str | None, study_id: str,
                    pres: dict[str, Any]) -> dict[str, Any]:
    """Match the studied face against strangers seen before; append the sighting
    (new embeddings capped) or create a new seen_id. Returns the seen record."""
    import numpy as np  # type: ignore
    import shutil
    if not embs:
        return {"ok": False, "reason": "no embeddings (engine down or no face in saved frames)"}
    now = time.time()
    hit, sim = _gallery_match(embs)
    if hit is not None:
        d = hit["dir"]
        meta = hit["meta"]
        old = hit["embs"] if hit["embs"] is not None else np.zeros((0, embs[0].shape[0]), dtype="float32")
        merged = np.concatenate([old, np.stack(embs)], axis=0)[-SEEN_MAX_EMB:]
        seen_before = True
    else:
        seen_id = "seen_" + time.strftime("%Y%m%d_%H%M%S")
        d = SEEN_DIR / seen_id
        d.mkdir(parents=True, exist_ok=True)
        meta = {"seen_id": seen_id, "first_seen": now,
                "first_seen_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "note": None, "sightings": []}
        merged = np.stack(embs)[-SEEN_MAX_EMB:]
        seen_before = False
    meta["last_seen"] = now
    meta["last_seen_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta.setdefault("sightings", []).append(
        {"ts": now, "iso": meta["last_seen_iso"], "study_id": study_id,
         "similarity": round(sim, 3) if seen_before else None,
         "zeke_away": pres.get("away")})
    try:
        np.save(d / "embeddings.npy", merged.astype("float32"))
        if crop_path and Path(crop_path).exists():
            shutil.copyfile(crop_path, d / f"crop_{study_id}.jpg")
            if not (d / "crop.jpg").exists():
                shutil.copyfile(crop_path, d / "crop.jpg")
        (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"gallery write: {e!r}"[:120]}
    return {"ok": True, "seen_before": seen_before, "seen_id": meta["seen_id"],
            "similarity": round(sim, 3), "first_seen_iso": meta.get("first_seen_iso"),
            "sightings": len(meta["sightings"]), "note": meta.get("note"),
            "embeddings_kept": int(merged.shape[0]), "dir": str(d)}


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

    # 4) photographic memory: embed the burst, match against strangers seen before.
    seen = _gallery_record(_embed_frames(g, saved), crop_info.get("path"), study_id, pres)

    record = {
        "study_id": study_id,
        "seen": seen,
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
                   f"(chat_id {DISCORD_CHAT_ID}). You MAY speak to them (Zeke 09-02) - "
                   "but my voice lands in his HEADPHONES so they may not hear; ask who "
                   "they are / what they need, never announce that he is out. Then "
                   "study_face action=note seen_id=... text='what I saw' and action=release.")
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
    if action == "note":
        seen_id = str(params.get("seen_id") or "").strip()
        text = str(params.get("text") or "").strip()
        d = SEEN_DIR / seen_id
        if not seen_id or not d.is_dir():
            return {"ok": False, "error": f"unknown seen_id {seen_id!r}",
                    "known": [e["seen_id"] for e in _gallery_load()]}
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            meta["note"] = text
            meta.setdefault("notes", []).append(
                {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": text})
            (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return {"ok": True, "seen_id": seen_id, "note": text}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)[:120]}
    if action == "selftest":
        # Exercise the embedding + matching path on frames already on disk,
        # WITHOUT writing to the gallery (no stranger needed to prove the plumbing).
        d = Path(str(params.get("dir") or ""))
        paths = sorted(str(p) for p in d.glob("*.jpg")) if d.is_dir() else []
        t0 = time.time()
        embs = _embed_frames(g, paths[: int(params.get("limit") or 4)])
        hit, sim = _gallery_match(embs) if embs else (None, 0.0)
        recorded = None
        if embs and params.get("record"):
            # Opt-in: write these as a gallery entry (tests the record path).
            recorded = _gallery_record(embs, None, "selftest", {"away": None})
        return {"ok": bool(embs), "frames": len(paths), "embedded": len(embs),
                "recorded": recorded,
                "dim": int(embs[0].shape[0]) if embs else None,
                "secs": round(time.time() - t0, 2),
                "best_match": (hit or {}).get("seen_id"), "similarity": round(sim, 3),
                "engine_present": g.get("_insight_face") is not None}
    if action == "gallery":
        out = []
        for e in _gallery_load():
            m = e["meta"]
            out.append({"seen_id": e["seen_id"], "first_seen": m.get("first_seen_iso"),
                        "last_seen": m.get("last_seen_iso"),
                        "sightings": len(m.get("sightings") or []),
                        "embeddings": int(len(e["embs"])) if e["embs"] is not None else 0,
                        "note": m.get("note"), "crop": str(e["dir"] / "crop.jpg")})
        return {"ok": True, "strangers": out, "count": len(out), "dir": str(SEEN_DIR)}
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
            "error": f"unknown action {action!r} — start|release|status|note|gallery|rearm_watcher"}


register_tool(
    "study_face",
    "STUDY an unrecognised face (Zeke's rule 2026-09-02: matters MORE when his "
    "phone is off the wifi). action='start' puts the head on them, burst-captures "
    "frames to faces/_drafts/study_<ts>/, cuts crop.jpg and a SMALL sheet.jpg "
    "(safe to Read), writes study.json; then cognition looks + DMs Zeke the crop "
    "(may speak: voice is in his headphones; never say he's out). Every studied "
    "face is embedded into faces/_seen/ and matched against strangers seen before "
    "(seen_before in the result). 'note' (seen_id=, text=) stores my words about "
    "them; 'gallery' lists strangers I've seen; 'release' returns the head to its "
    "persisted target; 'status'; 'rearm_watcher' restarts the unknown_capture "
    "watcher on new code.",
    2,
    _study_face,
)
