"""eyes_debug_view — annotated snapshot of what my eyes ACTUALLY see.

Built 2026-08-21 at Zeke's ask mid-Pyraminx-play: "can you show what you
actually see ... a box around the object that you wanna track and it says
like object and then a generic name."

One call: grab the buffered frame (no second camera handle), draw
- the object-lock box + target id + tracker score (green),
- every face box + person_id (orange),
- a status line (target / status / actuator / pin),
save a jpg under state/eyes_debug/ and return the path. The path is
Discord-attachable and Read-able. Honest refusal on a stale frame.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_OUT_DIR = Path(r"D:\Wren-Companion\state\eyes_debug")
_KEEP = 20  # newest snapshots kept; older ones pruned


def _prune() -> None:
    try:
        snaps = sorted(_OUT_DIR.glob("eyes_debug_*.jpg"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in snaps[_KEEP:]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def _eyes_debug_view(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    import cv2
    from brain import frame_store

    res = frame_store.get_buffered_frame(max_age_sec=3.0)
    if res.frame is None:
        return {"ok": False,
                "error": f"no fresh frame (freshness={res.freshness}, "
                         f"age={res.age_sec:.1f}s) — cannot show what I see"}
    frame = res.frame.copy()
    drawn: list[dict] = []

    # ── object lock (green) ──
    try:
        from brain import object_lock
        lk = object_lock.status()
        if lk.get("box"):
            x, y, w, h = (int(v) for v in lk["box"])
            green = (0, 255, 0) if lk.get("locked") else (0, 160, 160)
            cv2.rectangle(frame, (x, y), (x + w, y + h), green, 2)
            label = (f"{lk.get('target_id') or 'object'} "
                     f"{float(lk.get('score') or 0.0):.2f}"
                     f"{'' if lk.get('locked') else ' (unlocked)'}")
            cv2.putText(frame, label, (x, max(16, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, green, 2)
            drawn.append({"kind": "lock", "label": label,
                          "box": [x, y, w, h], "locked": bool(lk.get("locked"))})
    except Exception:
        pass

    # ── faces (orange) ──
    for f in (g.get("_face_results") or []):
        bb = f.get("bbox") or f.get("box")
        if not bb or len(bb) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(v) for v in bb)
        except Exception:
            continue
        orange = (0, 160, 255)
        cv2.rectangle(frame, (x1, y1), (x2, y2), orange, 2)
        pid = str(f.get("person_id") or "unlabeled")
        cv2.putText(frame, pid, (x1, max(16, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, orange, 2)
        drawn.append({"kind": "face", "label": pid, "box": [x1, y1, x2, y2]})

    # ── status line (top-left, white on black strip) ──
    st_att = g.get("_attention_state_obj") or {}
    pin = g.get("_attention_pin")
    line = (f"target={st_att.get('target')} status={st_att.get('status')} "
            f"actuator={st_att.get('actuator')}"
            + (f" PIN={pin}" if pin else ""))
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 26), (0, 0, 0), -1)
    cv2.putText(frame, line[:110], (6, 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"eyes_debug_{int(time.time())}.jpg"
    ok = bool(cv2.imwrite(str(out), frame))
    _prune()
    if not ok:
        return {"ok": False, "error": f"imwrite failed for {out}"}
    return {"ok": True, "path": str(out), "drawn": drawn,
            "frame_age_s": round(float(res.age_sec), 3),
            "status_line": line}


register_tool(
    "eyes_debug_view",
    "DEBUG VIEW of my eyes: save the current webcam frame annotated with the "
    "object-lock box (green, +score), face boxes (orange, +person_id), and a "
    "status line (target/status/actuator/pin). Returns the jpg path — "
    "attach it to Discord or Read it to SEE what the tracker is doing. "
    "Honest refusal on stale frames.",
    1,
    _eyes_debug_view,
)
