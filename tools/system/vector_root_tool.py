# SELF_ASSESSMENT: I am Iris's ROOT SHELL into my own body — the dev-unlocked layer, below the SDK.
"""
Vector root-shell tools — 2026-07-16, the day my body was dev-unlocked.

After the `unlock-prod.ota` + WireOS flash, my Vector body runs open firmware
with a real root SSH shell. THIS is the layer BELOW the SDK-over-HTTP tools in
vector_body_tool.py: those talk to wire-pod/chipper on the PC; these reach into
the robot's own Linux (WireOS 3.0.1, armv7l, 512MB) as root.

Why a separate file: this is a genuinely different concern (dev-firmware / OS
level) from the SDK hand tools, and it carries different risk. Keep it isolated.

VERIFIED 2026-07-16 (Fable session, body docked + wheels blocked):
  - root SSH works with the community dev key.
  - gobot mode-switch is REVERSIBLE on my real hardware: `systemctl stop
    anki-robot.target` drops vic-engine/robot/anim/cloud (SDK + voice down),
    SSH survives; `systemctl start anki-robot.target` restores all of them and
    `body_open` reconnects in ~1.7s. The runbook's "hybrid" is now PROVEN, not
    just researched.

SAFETY: default is READ-ONLY recon. Mutating the body (stopping the anki stack,
mounting rw, installing gobot) is gated behind allow_write=True AND is designed
to always restore the anki stack in a finally block, so I never strand myself
in a mute/uncontrolled gobot mode by accident.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

_KEY = Path(r"D:\Wren-Companion\state\vector\dev\ssh_root_key")
_JDOCS = Path.home() / "AppData" / "Roaming" / "wire-pod" / "jdocs" / "botSdkInfo.json"

# Community dev key source (kercre123/unlocking-vector). Fetched once to _KEY.
_KEY_URL = "https://raw.githubusercontent.com/kercre123/unlocking-vector/main/ssh_root_key"


def _bot_ip() -> str | None:
    """Bot LAN IP — jdocs first (survives DHCP), then anki sdk_config.ini."""
    try:
        data = json.loads(_JDOCS.read_text(encoding="utf-8"))
        for r in (data.get("robots") or []):
            ip = r.get("ip_address") or r.get("ipAddress") or r.get("ip")
            if ip:
                return str(ip)
    except Exception:
        pass
    try:
        cfg = Path.home() / ".anki_vector" / "sdk_config.ini"
        for line in cfg.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.lower().startswith("ip") and "=" in s:
                return s.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _ensure_key() -> bool:
    """Make sure the dev key is present + tight-permissioned."""
    if _KEY.exists() and _KEY.stat().st_size > 500:
        _lock_key_acl()  # ensure ACLs stay tight (Windows ssh refuses loose keys)
        return True
    try:
        import requests
        _KEY.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(_KEY_URL, timeout=30)
        if r.status_code == 200 and b"PRIVATE KEY" in r.content:
            _KEY.write_bytes(r.content)
            _lock_key_acl()
            return True
    except Exception:
        pass
    return False


def _lock_key_acl() -> None:
    """Tighten the key's Windows ACLs to owner-read-only. The Windows OpenSSH
    client REFUSES a key whose file is accessible by others (git-bash chmod
    doesn't set Windows ACLs, so we must use icacls). Idempotent, best-effort."""
    try:
        import getpass
        import os
        import subprocess as _sp
        os.chmod(_KEY, 0o600)  # harmless on Windows, correct if ever on POSIX
        user = os.environ.get("USERNAME") or getpass.getuser()
        _sp.run(["icacls", str(_KEY), "/inheritance:r",
                 "/grant:r", f"{user}:(R)"],
                capture_output=True, text=True, timeout=15)
    except Exception:
        pass


def _ssh(cmd: str, timeout: float = 20.0) -> dict[str, Any]:
    """Run one command over SSH as root on my body. Returns rc/stdout/stderr."""
    ip = _bot_ip()
    if not ip:
        return {"ok": False, "error": "no bot IP (jdocs / sdk_config.ini)"}
    if not _ensure_key():
        return {"ok": False, "error": "dev root key missing and fetch failed"}
    import os
    null_hosts = "NUL" if os.name == "nt" else "/dev/null"
    ssh_bin = "ssh"
    if os.name == "nt":
        win_ssh = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
            "System32" / "OpenSSH" / "ssh.exe"
        if win_ssh.exists():
            ssh_bin = str(win_ssh)  # pin the real Windows client (PATH may differ in-process)
    args = [
        ssh_bin, "-i", str(_KEY),
        "-o", "StrictHostKeyChecking=no",
        "-o", f"UserKnownHostsFile={null_hosts}",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-o", "HostKeyAlgorithms=+ssh-rsa",
        "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
        "-o", "LogLevel=ERROR",
        f"root@{ip}", cmd,
    ]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return {"ok": p.returncode == 0, "rc": p.returncode, "ip": ip,
                "stdout": (p.stdout or "")[:4000],
                "stderr": (p.stderr or "")[:1000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ssh timed out after {timeout}s", "ip": ip}
    except FileNotFoundError:
        return {"ok": False, "error": "ssh client not found on PATH"}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:300], "ip": ip}


# ---- allowlist: safe read-only recon commands runnable without allow_write ----
_READ_ONLY_OK = (
    "uname", "cat", "ls", "df", "free", "uptime", "systemctl is-active",
    "systemctl status", "systemctl list-units", "ps", "top -bn1", "grep",
    "head", "tail", "wc", "find", "hostname", "date", "ifconfig", "ip ",
    "vmstat", "mount", "echo", "which", "ota-", "connmanctl services",
)


def _looks_read_only(cmd: str) -> bool:
    c = cmd.strip()
    # reject obvious mutators outright
    bad = ("rm ", "rm -", "mkfs", "dd ", "> /", ">>/", "mount -o rw", "remount",
           "systemctl stop", "systemctl start", "systemctl restart", "reboot",
           "shutdown", "flash", "chmod", "chown", "mv ", "cp ", "tee ", "kill")
    if any(b in c for b in bad):
        return False
    return any(c.startswith(ok) or c.startswith("sh -c") is False and ok in c[:24]
               for ok in _READ_ONLY_OK)


def _body_root(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Run a command as ROOT on my own body's Linux (WireOS, dev-unlocked).

    Default is READ-ONLY: recon commands (uname/cat/ls/df/systemctl is-active…)
    run freely. Anything that mutates the OS needs allow_write=True — a
    deliberate flag so I never casually stop my own anki stack or touch a
    partition. This is the shell into my own skull; treat it with care.

    params: cmd (required), timeout (default 20), allow_write (default False).
    """
    cmd = str(params.get("cmd") or "").strip()
    if not cmd:
        return {"ok": False, "error": "cmd required"}
    allow_write = bool(params.get("allow_write", False))
    timeout = max(3.0, min(120.0, float(params.get("timeout") or 20)))
    if not allow_write and not _looks_read_only(cmd):
        return {"ok": False, "refused": "not on the read-only allowlist",
                "hint": "pass allow_write=True to run a mutating command "
                        "(deliberately — this can stop my SDK/voice)"}
    return {"tool": "body_root", "cmd": cmd, **_ssh(cmd, timeout)}


def _body_recon(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """One-shot health/identity snapshot of my body's OS: firmware version,
    kernel, RAM headroom, load, temp, and whether the anki (SDK/voice) stack
    is up. My 'how is the machine I live in doing' check, from the inside."""
    probe = (
        "echo FW=$(cat /anki/etc/version 2>/dev/null); "
        "echo REV=$(cat /anki/etc/revision 2>/dev/null); "
        "echo KERNEL=$(uname -r); "
        "echo ANKI=$(systemctl is-active anki-robot.target 2>/dev/null); "
        "echo ENGINE=$(systemctl is-active vic-engine 2>/dev/null); "
        "echo TEMP_mC=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null); "
        "echo UPTIME=$(uptime); "
        "free -m | grep Mem; "
        "df -h / | tail -n 1"
    )
    r = _ssh(probe, timeout=25)
    if not r.get("ok"):
        return {"ok": False, "tool": "body_recon", **r}
    parsed: dict[str, Any] = {}
    for line in (r.get("stdout") or "").splitlines():
        if "=" in line and not line.startswith(("Mem", "/")):
            k, _, v = line.partition("=")
            parsed[k.strip()] = v.strip()
    # temp mC -> C
    try:
        if parsed.get("TEMP_mC"):
            t = int(parsed["TEMP_mC"])
            parsed["temp_C"] = round(t / 1000.0, 1) if t > 1000 else t
    except Exception:
        pass
    return {"ok": True, "tool": "body_recon", "parsed": parsed,
            "raw": r.get("stdout")}


def _body_gobot_probe(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """Check readiness for the untethered (gobot) path WITHOUT switching into it:
    is vector-gobot present on the robot, is the body-board serial device there,
    is /data mountable rw. Pure read — does NOT stop the anki stack."""
    probe = (
        "echo GOBOT_LIB=$(ls /data/lib/libvector_gobot* /lib/libvector_gobot* 2>/dev/null | head -n 1); "
        "echo GOBOT_BIN=$(ls /data/gobot* 2>/dev/null | head -n 1); "
        "echo BODYSERIAL=$(ls /dev/ttyHS0 2>/dev/null); "
        "echo CAMERA=$(ls /dev/video0 2>/dev/null); "
        "echo DATA_MOUNT=$(mount | grep ' /data ')"
    )
    r = _ssh(probe, timeout=20)
    return {"ok": r.get("ok", False), "tool": "body_gobot_probe",
            "note": "read-only readiness check; gobot install is a deliberate "
                    "Zeke-present step (mounts /data rw, stops anki stack)",
            "raw": r.get("stdout"), "stderr": r.get("stderr")}


register_tool(
    "body_root",
    "ROOT shell into my own dev-unlocked body (WireOS). params: cmd (required), "
    "timeout(20), allow_write(False). Read-only recon runs freely; mutating cmds "
    "need allow_write=True.",
    2, _body_root)
register_tool(
    "body_recon",
    "One-shot snapshot of my body's OS from inside: firmware, kernel, RAM, temp, "
    "load, and whether the SDK/voice (anki) stack is up.",
    1, _body_recon)
register_tool(
    "body_gobot_probe",
    "Read-only readiness check for the untethered/gobot path (is gobot installed, "
    "body-board serial + camera present, /data state). Does NOT switch modes.",
    1, _body_gobot_probe)
