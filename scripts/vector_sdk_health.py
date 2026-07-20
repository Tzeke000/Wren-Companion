"""scripts/vector_sdk_health.py

VECTOR SDK HEALTH PROBE + AUTO-HEAL (built 2026-07-19).

The wedge it targets: after the tower cold-boot on deployment eve, every
tower-side SDK client hung on the ListAnimations RPC. Restarting vic-cloud +
vic-switchboard did NOT heal it; a FULL ROBOT REBOOT did (proven twice).
This script makes that diagnosis + heal automatic, so a power-cut boot on the
OPUS brain self-repairs without cognition (or Zeke) having to notice.

Probe (observe-mode — does NOT take behavior control, so the inhabit daemon's
possession hold is untouched):
  1. connect to the robot with NO control (behavior_control_level=None)
  2. fetch the animation list with a hard thread-deadline
  3. wedged = connect ok but anim-list times out (the 07-19 signature)

Heal (--heal): if wedged -> reboot the robot over root ssh (WireOS dev-unlock,
reuses vector_root_tool's proven WIN-SSH invocation), wait for it to come
back, re-probe, DM Zeke the outcome. Skips the reboot if ssh is unreachable.

Exit codes: 0 healthy · 1 wedged (not healed) · 2 probe couldn't run.

Called from tower_boot_sentinel after the stack launches (--heal --dm), and
runnable by hand / by Iris any time:
    .venv\\Scripts\\python.exe scripts\\vector_sdk_health.py [--heal] [--dm]
"""
from __future__ import annotations

import datetime as _dt
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(r"D:\Wren-Companion")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

LOG = REPO / "state" / "vector_sdk_health.log"
SERIAL = "0dd1cdaf"
CONNECT_TIMEOUT_S = 20
ANIM_LIST_TIMEOUT_S = 25
REBOOT_WAIT_S = 150       # robot cold boot ~60-90s; generous
REPROBE_TRIES = 3


def log(msg: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _dm(text: str, enabled: bool) -> None:
    if not enabled:
        return
    try:
        from iris_runtime_watchdog import dm_zeke
        dm_zeke(text)
    except Exception as e:
        log(f"DM failed: {e!r}")


def _with_deadline(label: str, fn, timeout_s: float):
    """Run fn in a disposable thread with a hard deadline (the 07-19 pattern).
    Returns (ok, result_or_error, timed_out)."""
    box: dict = {}
    done = threading.Event()

    def _run():
        try:
            box["res"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
        finally:
            done.set()

    threading.Thread(target=_run, name=f"probe:{label}", daemon=True).start()
    if not done.wait(timeout=timeout_s):
        return False, f"{label} exceeded {timeout_s}s deadline", True
    if "err" in box:
        return False, repr(box["err"])[:300], False
    return True, box.get("res"), False


def probe() -> dict:
    """One observe-mode probe. Returns {healthy, wedged, detail}."""
    try:
        import anki_vector
    except Exception as e:
        return {"healthy": False, "wedged": False,
                "detail": f"anki_vector import failed: {e!r}"}

    robot = anki_vector.Robot(SERIAL, behavior_control_level=None,
                              cache_animation_lists=False, default_logging=False)
    ok, res, t_out = _with_deadline(
        "connect", lambda: robot.connect(timeout=CONNECT_TIMEOUT_S),
        CONNECT_TIMEOUT_S + 10)
    if not ok:
        with_suppress_disconnect(robot)
        # can't even connect: robot off / wifi down — NOT the ListAnimations wedge
        return {"healthy": False, "wedged": False, "detail": f"connect failed: {res}"}

    ok, res, t_out = _with_deadline(
        "anim_list", lambda: robot.anim.load_animation_list(),
        ANIM_LIST_TIMEOUT_S)
    with_suppress_disconnect(robot)
    if ok:
        n = len(getattr(robot.anim, "anim_list", []) or [])
        return {"healthy": True, "wedged": False,
                "detail": f"anim list loaded ({n} anims cached)"}
    if t_out:
        # THE 07-19 signature: connection fine, ListAnimations never returns
        return {"healthy": False, "wedged": True,
                "detail": f"ListAnimations wedged: {res}"}
    return {"healthy": False, "wedged": False, "detail": f"anim list error: {res}"}


def with_suppress_disconnect(robot) -> None:
    ok, _, _ = _with_deadline("disconnect", robot.disconnect, 10)
    if not ok:
        log("disconnect itself hung (abandoned in daemon thread)")


def reboot_robot() -> dict:
    """Full robot reboot over root ssh — the PROVEN heal for the wedge."""
    from tools.system.vector_root_tool import _ssh
    r = _ssh("(sleep 2; /sbin/reboot) >/dev/null 2>&1 &", timeout=15)
    # command backgrounds the reboot so ssh returns cleanly first
    return r


def main() -> int:
    heal = "--heal" in sys.argv
    dm = "--dm" in sys.argv
    log(f"probe start (heal={heal} dm={dm})")

    r = probe()
    log(f"probe: {r}")
    if r["healthy"]:
        return 0
    if not r["wedged"]:
        # unreachable/off — not the wedge this script owns; report only
        _dm(f"\N{WARNING SIGN} Vector SDK probe couldn't reach the robot "
            f"({r['detail']}) — not the ListAnimations wedge, no auto-action.", dm)
        return 2

    log("WEDGE detected (the 07-19 ListAnimations signature)")
    if not heal:
        _dm("\N{WARNING SIGN} Vector SDK is WEDGED on ListAnimations (the "
            "07-19 signature). Run vector_sdk_health.py --heal, or reboot the "
            "robot — that's the proven fix.", dm)
        return 1

    _dm("\N{WRENCH} Vector SDK wedged on ListAnimations after boot (the 07-19 "
        "signature). Auto-healing with a full robot reboot — the proven fix. "
        "Re-probe result follows in ~3 min.", dm)
    rb = reboot_robot()
    log(f"reboot over ssh: {rb}")
    if not rb.get("ok"):
        _dm(f"\N{POLICE CARS REVOLVING LIGHT} Auto-heal FAILED: ssh reboot "
            f"didn't go through ({str(rb)[:120]}). The SDK stays wedged — "
            f"needs a manual robot reboot.", dm)
        return 1

    time.sleep(REBOOT_WAIT_S)
    for i in range(REPROBE_TRIES):
        r2 = probe()
        log(f"re-probe {i + 1}/{REPROBE_TRIES}: {r2}")
        if r2["healthy"]:
            _dm("\N{WHITE HEAVY CHECK MARK} Auto-heal WORKED: robot rebooted, "
                "SDK answers cleanly (anim list loads). Body control is "
                "available again.", dm)
            return 0
        time.sleep(30)
    _dm("\N{POLICE CARS REVOLVING LIGHT} Robot rebooted but the SDK still "
        "isn't clean after re-probes — needs eyes on it.", dm)
    return 1


if __name__ == "__main__":
    sys.exit(main())
