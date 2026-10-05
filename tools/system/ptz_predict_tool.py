"""ptz_predict — predicted-vs-observed checks for my own PTZ head (brain/ptz_predict.py).

Borrowed from the Zeke+Vale research handoff (10-04, item 3: "record the expected
next observation and compare it with reality; PTZ movement is a good first
candidate"). Answers the question the position registers can't: is the head
ACTUALLY where it says it is? (2026-10-02: registers said home for 6 h while the
view sat ~15 deg off.)

Actions:
  status        — register_trust, counters, home-ref meta, self-calibration estimate
  capture_home  — absolute snap to home, settle, store the reference home view
                  (writes a small preview jpg so I can LOOK at it — never trust it blind)
  check_home    — compare the live view to the home reference (no motion)
  test_move     — bounded move (|d|<=20), predict, observe, judge, restore + judge the
                  restore. MOVES THE HEAD.
  calibrate     — a ladder of test_moves (pan +-6/+-10, tilt +-6) -> HFOV + tilt sign
                  estimate (advisory; not auto-applied). MOVES THE HEAD.
  resync        — the proven cure for stale registers: absolute move away (+8 pan)
                  and back home, then check_home. MOVES THE HEAD.

Every move action refuses while a follow/servo loop is driving the gimbal.
"""
from __future__ import annotations

import time
from typing import Any

from tools.tool_registry import register_tool

_HOME = (0.0, 10.0)   # same measured home as attention_smooth_tool._HOME
_MAX_D = 20.0


def _follow_active(g: dict) -> bool:
    for key in ("_attention_follow_state", "_attention_smooth_state"):
        try:
            th = ((g or {}).get(key) or {}).get("thread")
            if th is not None and th.is_alive():
                return True
        except Exception:
            pass
    return False


def _actuator():
    from brain import visual_attention as va
    act = va.build_actuator()
    if not getattr(act, "available", lambda: False)():
        return None
    return act


def _ledger(kind: str, intent: str, expected: str):
    """Open an action-ledger row if the ledger exists (it is optional)."""
    try:
        from brain import action_ledger as al
        return al.open_action(kind=kind, intent=intent, expected=expected,
                              source="ptz_predict", deadline_s=60)
    except Exception:
        return None


def _ledger_close(row, verdict: str, result: dict) -> None:
    if not row:
        return
    try:
        from brain import action_ledger as al
        outcome = {"match": "success", "at_home": "success",
                   "mismatch": "failure", "off_home": "failure",
                   "refused": "failure"}.get(verdict, "uncertain")
        al.close_action(row["id"], outcome=outcome,
                        evidence={"verdict": verdict,
                                  "offset_deg": result.get("offset_deg"),
                                  "error_deg": result.get("error_deg"),
                                  "detail": result.get("detail")},
                        verifier="ptz_predict")
    except Exception:
        pass


def check_home_now(source: str = "check_home", settle_s: float = 0.0) -> dict:
    """Shared by this tool and the servo's home paths. No motion."""
    from brain import ptz_predict as pp
    if settle_s:
        time.sleep(settle_s)
    ref = pp.home_ref_load()
    if ref is None:
        return {"ok": False, "verdict": "uncertain", "detail": "no home reference yet — run capture_home"}
    frame = pp.grab_frame(max_age_sec=2.5)
    if frame is None:
        return {"ok": False, "verdict": "uncertain", "detail": "no live frame"}
    bearing = None
    try:
        act = _actuator()
        bearing = act.bearing() if act else None
    except Exception:
        pass
    res = pp.judge_home(ref, frame)
    res = pp.record(res, source=source, bearing=bearing)
    res["ok"] = True
    res["bearing_registers"] = bearing
    return res


def resync_home(source: str = "resync") -> dict:
    """Absolute move away and back (the measured cure, 10-02), then check."""
    act = _actuator()
    if act is None:
        return {"ok": False, "error": "no live PTZ actuator"}
    row = _ledger("ptz_resync", "re-sync the head registers by an absolute move away and back home",
                  "check_home reads at_home afterwards")
    act.look_at(_HOME[0] + 8.0, _HOME[1])
    time.sleep(1.2)
    act.look_at(*_HOME)
    res = check_home_now(source=source, settle_s=2.0)
    _ledger_close(row, res.get("verdict", "uncertain"), res)
    return res


def _test_move(act, d_pan: float, d_tilt: float, settle_s: float, source: str) -> dict:
    from brain import ptz_predict as pp
    start = act.bearing() or {}
    sp = float(start.get("pan_deg") or 0.0)
    stilt = float(start.get("tilt_deg") or _HOME[1])
    before = pp.grab_frame(max_age_sec=1.5)
    row = _ledger("ptz_move", f"move head by pan {d_pan:+.1f} / tilt {d_tilt:+.1f} deg",
                  "image shifts by the pinhole-predicted amount")
    r = act.look_at(sp + d_pan, stilt + d_tilt) or {}
    if not r.get("ok"):
        _ledger_close(row, "refused", {"detail": r.get("reason")})
        return {"verdict": "refused", "move_result": r}
    time.sleep(settle_s)
    after = pp.grab_frame(max_age_sec=1.0)
    if before is None or after is None:
        res = {"kind": "move", "verdict": "uncertain", "detail": "missing before/after frame",
               "cmd": {"d_pan": d_pan, "d_tilt": d_tilt}}
    else:
        res = pp.judge_move(d_pan, d_tilt, pp.observe_shift(before, after), before=before, after=after)
    res = pp.record(res, source=source, bearing=start)
    _ledger_close(row, res.get("verdict", "uncertain"), res)
    return res


def _ptz_predict(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    from brain import ptz_predict as pp
    action = str(params.get("action") or "status").lower()

    if action == "status":
        m = pp.load_model()
        meta = None
        try:
            import json
            meta = json.loads(pp.HOME_REF_META.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"ok": True, "register_trust": pp.register_trust(),
                "counts": m.get("counts"), "last_check": m.get("last_check"),
                "tilt_sign": m.get("tilt_sign"), "hfov_deg": m.get("hfov_deg"),
                "home_ref": meta, "calibration_estimate": pp.calibrate_from_samples(m),
                "n_samples": len(m.get("calib_samples", []))}

    if action == "check_home":
        return check_home_now(source=str(params.get("source") or "check_home"))

    if _follow_active(g):
        return {"ok": False, "error": "a follow/servo loop is driving the gimbal — stop it first"}
    act = _actuator()
    if act is None:
        return {"ok": False, "error": "no live PTZ actuator"}

    if action == "capture_home":
        r = act.look_at(*_HOME) or {}
        if not r.get("ok"):
            return {"ok": False, "error": f"home move refused: {r}"}
        time.sleep(float(params.get("settle_s", 2.5)))
        frame = pp.grab_frame(max_age_sec=1.0)
        if frame is None:
            return {"ok": False, "error": "no live frame"}
        meta = pp.home_ref_save(frame, {"bearing_registers": act.bearing(),
                                        "note": str(params.get("note") or "")})
        prev = str(pp.HOME_REF_PNG.with_name("home_ref_preview.jpg"))
        try:
            import cv2
            h, w = frame.shape[:2]
            cv2.imwrite(prev, cv2.resize(frame, (640, int(h * 640 / w))),
                        [cv2.IMWRITE_JPEG_QUALITY, 70])
        except Exception:
            prev = None
        return {"ok": True, "home_ref": meta, "preview_jpg": prev,
                "note": "LOOK at the preview before trusting it as home"}

    if action == "resync":
        return resync_home()

    settle = float(params.get("settle_s", 1.2))
    if action == "test_move":
        d_pan = max(-_MAX_D, min(_MAX_D, float(params.get("d_pan", 8.0))))
        d_tilt = max(-_MAX_D, min(_MAX_D, float(params.get("d_tilt", 0.0))))
        out = _test_move(act, d_pan, d_tilt, settle, "test_move")
        back = None
        if bool(params.get("restore", True)) and out.get("verdict") != "refused":
            back = _test_move(act, -d_pan, -d_tilt, settle, "test_move_restore")
        return {"ok": True, "move": out, "restore": back,
                "register_trust": pp.register_trust()}

    if action == "calibrate":
        ladder = [(6, 0), (-6, 0), (10, 0), (-10, 0), (0, 6), (0, -6)]
        rows = []
        for dp, dt in ladder:
            a = _test_move(act, dp, dt, settle, "calibrate")
            b = _test_move(act, -dp, -dt, settle, "calibrate")
            rows.append({"cmd": [dp, dt], "go": a.get("verdict"), "go_obs": a.get("observed_deg"),
                         "back": b.get("verdict"), "back_obs": b.get("observed_deg"),
                         "resp": (a.get("observed") or {}).get("response")})
        act.look_at(*_HOME)
        return {"ok": True, "ladder": rows, "estimate": pp.calibrate_from_samples(),
                "note": "estimate is ADVISORY; hfov/tilt_sign are applied only by an explicit step"}

    return {"ok": False, "error": f"unknown action {action!r}",
            "actions": ["status", "capture_home", "check_home", "test_move", "calibrate", "resync"]}


register_tool(
    "ptz_predict",
    "PREDICTED-vs-OBSERVED for my PTZ head: is the head really where its registers say? "
    "status | capture_home | check_home (no motion) | test_move d_pan d_tilt | calibrate | resync. "
    "Verdicts match/mismatch/uncertain, at_home/off_home/uncertain; writes register_trust "
    "(read by room_map + the servo home paths).",
    1,
    _ptz_predict,
)
