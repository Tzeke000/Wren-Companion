"""eyes_reload_tool — reload the live-eyes capture pipeline WITHOUT a stack restart.

SELF_ASSESSMENT: Tier 1 (device cycling is Tier-1-adjacent but scoped to the camera).

Born 2026-08-21 ~00:2x, Zeke's directive after the second Pixy-poisoning of the day:
"let's make something for you so you can just fully reload your eyes if you needed
to so you won't need a full stack restart."

How the eyes actually break (mapped tonight from iris_runtime's video loop):
  - The loop owns cv2.VideoCapture(0, CAP_DSHOW) and already honors the body-pause
    flag: pause -> cap.release(), resume -> reopen. That heals a POISONED STREAM.
  - But when the DRIVER/filter-graph wedges (open succeeds, read() fails forever, or
    another process probed the device), release+reopen isn't enough — the DEVICE
    needs a restart. That's what previously forced full stack restarts (08-20 02:3x,
    08-21 00:0x).

So this tool runs the full ladder in one call:
  1. PAUSE   — set the body-pause flag; the video loop releases the camera (~0.5s poll).
  2. RESET   — (deep=true) pnputil /restart-device on the camera PnP device. Needs
               admin; if pnputil refuses, we say so honestly and continue — step 3
               alone still covers stream-level poisoning.
  3. RESUME  — clear the flag; the loop reopens DSHOW and streams again.
  4. VERIFY  — poll brain.frame_store's push buffer (we run INSIDE iris_runtime, so
               this is the ground truth the orb and attention stack actually eat)
               until a frame younger than 2s appears, or timeout.

Params: deep (bool, default True) — include the device restart step.
        verify_timeout_s (float, default 20).
Returns {ok, healed, steps: [...], frame_age_s, note}.
"""
from __future__ import annotations

import subprocess
import time
from typing import Any


def _pause_flag_path():
    from brain.iris_paths import paths as _p
    return _p.body_pause_flag


def _frame_age_s() -> float:
    """Age of the newest pushed frame, or -1 if none. Ground truth, in-process."""
    try:
        from brain import frame_store as fs
        ts = getattr(fs, "_buffer_ts", 0.0) or 0.0
        if ts <= 0:
            return -1.0
        return max(0.0, time.time() - float(ts))
    except Exception:
        return -1.0


def _restart_camera_device() -> tuple[bool, str]:
    """pnputil /restart-device on every OK camera-class device (there's exactly one
    on this tower: the EMEET PIXY). Returns (ok, detail)."""
    ps = (
        "$d = Get-PnpDevice -Class Camera -Status OK; "
        "if (-not $d) { $d = Get-PnpDevice -Class Image -Status OK }; "
        "if (-not $d) { 'NO-DEVICE'; exit 1 }; "
        "$out = @(); foreach ($x in @($d)) { "
        "$r = pnputil /restart-device \"$($x.InstanceId)\" 2>&1 | Out-String; "
        "$out += \"$($x.FriendlyName): $($r.Trim())\" }; $out -join ' | '"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=45,
        )
        detail = (r.stdout or "").strip() or (r.stderr or "").strip()
        ok = r.returncode == 0 and "restarted" in detail.lower()
        return ok, detail[:400]
    except Exception as e:
        return False, f"device restart failed: {e!r}"


def _eyes_reload_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    deep = params.get("deep", True)
    verify_timeout = float(params.get("verify_timeout_s", 20.0))
    steps: list[str] = []
    flag = _pause_flag_path()

    age_before = _frame_age_s()
    steps.append(f"frame age before: {age_before:.1f}s" if age_before >= 0 else
                 "frame age before: no frame ever pushed")

    # 1. PAUSE — video loop releases the capture handle.
    try:
        flag.write_text("eyes_reload", encoding="utf-8")
        steps.append("paused (flag set) — waiting 2.5s for the loop to release the camera")
        time.sleep(2.5)
    except Exception as e:
        return {"ok": False, "error": f"could not set pause flag: {e!r}", "steps": steps}

    # 2. RESET — cycle the device driver while nobody holds it.
    if deep:
        ok_dev, detail = _restart_camera_device()
        steps.append(f"device restart {'OK' if ok_dev else 'NOT CONFIRMED'}: {detail}")
        time.sleep(2.0)  # let the driver re-enumerate before we grab it back
    else:
        steps.append("device restart skipped (deep=false)")

    # 3. RESUME — loop reopens DSHOW.
    try:
        if flag.exists():
            flag.unlink()
        steps.append("resumed (flag cleared)")
    except Exception as e:
        return {"ok": False, "error": f"could not clear pause flag: {e!r}", "steps": steps}

    # 4. VERIFY — wait for a genuinely fresh frame in the shared buffer.
    deadline = time.time() + verify_timeout
    healed = False
    age = -1.0
    while time.time() < deadline:
        time.sleep(1.0)
        age = _frame_age_s()
        if 0.0 <= age < 2.0:
            healed = True
            break
    steps.append(f"frame age after: {age:.1f}s" if age >= 0 else "frame age after: still no frame")

    return {
        "ok": True,
        "healed": healed,
        "frame_age_s": round(age, 2),
        "steps": steps,
        "note": ("eyes are LIVE — fresh frames flowing" if healed else
                 "still stale after the full ladder — next rung is a stack restart"),
    }


try:
    from tools.tool_registry import register_tool
    register_tool(
        "eyes_reload",
        "Fully reload the live-eyes pipeline without a stack restart: pause -> "
        "device driver restart (deep=true) -> resume -> verify fresh frames. "
        "Params: deep (bool, default true), verify_timeout_s (float, default 20).",
        1,
        _eyes_reload_fn,
    )
except Exception:
    pass
