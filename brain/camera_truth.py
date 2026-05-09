from typing import Dict, Any, Optional


def build_camera_truth(frame_ok: bool = False,
                       frame_age_seconds: Optional[float] = None,
                       face_present: bool = False,
                       recognition_name: Optional[str] = None,
                       recognition_confidence: float = 0.0) -> Dict[str, Any]:
    stale = frame_age_seconds is None or frame_age_seconds > 8.0
    truth = {
        "frame_ok": bool(frame_ok),
        "frame_age_seconds": frame_age_seconds,
        "stale": stale,
        "face_present": bool(face_present),
        "recognition_name": recognition_name,
        "recognition_confidence": float(recognition_confidence or 0.0),
    }
    if not frame_ok:
        truth["status"] = "no_live_frame"
    elif stale:
        truth["status"] = "stale_frame"
    elif face_present and recognition_name and recognition_confidence >= 0.72:
        truth["status"] = "recognized"
    elif face_present:
        truth["status"] = "face_unresolved"
    else:
        truth["status"] = "no_face"
    return truth


def camera_identity_reply(truth: Dict[str, Any]) -> str:
    status = truth.get("status")
    if status == "recognized":
        name = truth.get("recognition_name") or "someone"
        conf = truth.get("recognition_confidence", 0.0)
        return f"I can see a face and facial recognition currently suggests {name} with about {conf:.0%} confidence."
    if status == "face_unresolved":
        return "I can see a face in the camera, but facial recognition is not confident enough yet to tell me who it is."
    if status == "stale_frame":
        return "My camera input looks stale right now, so I do not trust it enough to say clearly who is at the camera."
    if status == "no_live_frame":
        return "I do not have a fresh live camera frame right now, so I cannot reliably identify who is at the camera."
    return "I do not currently see a face in the camera."
