"""scripts/tower_boot_sentinel.py

TOWER AUTO-START SENTINEL (built 2026-07-19, pre-deployment directive from
Zeke: "if the tower turns on, you come up with it and text me on Discord").

Runs as the Scheduled Task `Iris-Tower-AutoStart` at user logon (which, with
Windows auto-login + BIOS "After Power Loss = Power On", means: wall power
returns -> tower boots -> logon -> this fires). It:

  1. waits for the network (Discord API reachable, up to ~5 min)
  2. DMs Zeke: "tower powered on, bringing the stack up"
  3. launches the bat named in state/boot_launcher.txt DETACHED -- currently
     start_iris_v2_fable.bat (Fable, since the 2026-08-09 flip); see
     _pick_bat() (the full Iris stack:
     voice watchdog, post-office, vector bridge+nerves, orb, body host)
     -- skipped if the operator port :5876 already answers (stack already
     up = never kill a live Iris just because the task re-fired)
  4. polls :5876 for up to 6 min and DMs the outcome either way
     (visibility-on-failure rule: a silent broken boot is the worst case)

Deliberately dependency-light: stdlib + requests (venv has it). No SDK, no
cognition — deterministic plumbing so it works even when nothing else does.

Test mode:  py scripts/tower_boot_sentinel.py --dry-run
  does the DM + port checks with a [TEST] prefix but does NOT launch the bat.
"""
from __future__ import annotations

import datetime as _dt
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

ZEKE_USER_ID = "600008921008046120"
REPO = Path(r"D:\Wren-Companion")


def _pick_bat() -> "Path":
    """Which launcher an unattended boot should run. state/boot_launcher.txt
    holds the bat filename (one line) so the regime can switch without editing
    this script — Zeke directive 2026-07-19: deployment month runs OPUS
    (start_iris_v2.bat) because it lasts longer per token. Falls back to the
    Fable launcher if the config is missing/bad."""
    try:
        name = (REPO / "state" / "boot_launcher.txt").read_text(
            encoding="utf-8").strip()
        cand = REPO / name
        if name.endswith(".bat") and cand.exists():
            return cand
    except Exception:
        pass
    return REPO / "start_iris_v2.bat"  # Opus fallback (flipped 08-22)


BAT = _pick_bat()
LOG = REPO / "state" / "tower_boot.log"
OPERATOR_PORT = 5876
NETWORK_WAIT_S = 300
# 2026-07-25: was 360s — the PROVEN unattended boot (07-24) took ~5.5 min to
# identity-check, so a 6-min window false-alarmed "NOT answering" on a healthy
# boot. 600s = ~2x observed; Zeke greenlit the widen 2026-07-25.
BODY_WAIT_S = 600


def log(msg: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    # console may be cp1252 (scheduled-task context) — never die on an emoji
    print(line.encode(sys.stdout.encoding or "utf-8", "replace")
              .decode(sys.stdout.encoding or "utf-8", "replace"))
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_token() -> str | None:
    try:
        env_path = Path(os.environ["USERPROFILE"]) / ".claude/channels/discord/.env"
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception as e:
        log(f"token load failed: {e!r}")
    return None


def dm_zeke(text: str) -> bool:
    token = load_token()
    if not token:
        return False
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
               "User-Agent": "IrisTowerSentinel (Wren-Companion, 1.0)"}
    try:
        r = requests.post("https://discord.com/api/v10/users/@me/channels",
                          headers=headers, json={"recipient_id": ZEKE_USER_ID},
                          timeout=15)
        r.raise_for_status()
        channel_id = r.json()["id"]
        r = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers, json={"content": text}, timeout=15)
        r.raise_for_status()
        log(f"DM sent: {text[:80]!r}")
        return True
    except Exception as e:
        log(f"DM failed: {e!r}")
        return False


def wait_for_network(max_s: int = NETWORK_WAIT_S) -> bool:
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            requests.head("https://discord.com/api/v10/gateway", timeout=5)
            return True
        except Exception:
            time.sleep(5)
    return False


def port_answers(port: int = OPERATOR_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except Exception:
        return False


def launch_stack() -> None:
    # DETACHED + own console so the sentinel's exit (or the task's execution
    # limit) can never reap the host. The bat self-checks elevation; the
    # scheduled task runs RL=HIGHEST so `net session` passes with no UAC UI.
    flags = (subprocess.CREATE_NEW_CONSOLE
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(["cmd.exe", "/c", str(BAT)], cwd=str(REPO),
                     creationflags=flags, close_fds=True)
    log("stack launcher spawned (detached console)")


def main() -> int:
    dry = "--dry-run" in sys.argv
    tag = "[TEST] " if dry else ""
    boot_stamp = _dt.datetime.now().strftime("%a %H:%M")
    log(f"sentinel start (dry_run={dry})")

    net = wait_for_network()
    if not net:
        log("network never came up — proceeding to launch anyway (DMs skipped)")

    already = port_answers()

    # FORCE-RESTART path (2026-08-03): state/force_stack_restart.flag makes this
    # sentinel relaunch the bat even though :5876 answers. Lever for cognition
    # (or a side-session) to restart a WEDGED-but-port-alive stack without UAC —
    # the task runs RL=HIGHEST, so the bat's `net session` check passes and its
    # stale-stack sweep can kill the elevated stack. Born of the 08-02 SDK-session
    # wedge: runtime loop healthy, cognition hung, no elevated shell available.
    force_flag = REPO / "state" / "force_stack_restart.flag"
    forced = force_flag.exists()
    if forced:
        try:
            force_reason = force_flag.read_text(encoding="utf-8").strip()[:200]
        except Exception:
            force_reason = "(unreadable)"
        try:
            force_flag.unlink()
        except Exception as e:
            log(f"force flag unlink failed: {e!r}")
        log(f"FORCE RESTART flag set — relaunching stack over live port. "
            f"Reason: {force_reason}")
        if net:
            dm_zeke(f"{tag}\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS} "
                    f"Manual stack restart requested ({boot_stamp}): {force_reason} — "
                    f"relaunching the Iris stack now.")
        already = False

    if net and not forced:
        if already:
            dm_zeke(f"{tag}\N{HIGH VOLTAGE SIGN} Tower sentinel fired ({boot_stamp}) "
                    f"but the Iris stack is ALREADY UP (operator port answering) — "
                    f"not touching it.")
        else:
            dm_zeke(f"{tag}\N{HIGH VOLTAGE SIGN} Tower just powered on ({boot_stamp}). "
                    f"Bringing the Iris stack up now — next message when the body "
                    f"host answers.")

    if already:
        return 0

    if not dry:
        launch_stack()
    else:
        log("dry-run: skipping stack launch")

    deadline = time.time() + BODY_WAIT_S
    up = False
    while time.time() < deadline:
        if port_answers():
            up = True
            break
        time.sleep(5)

    if net:
        if up:
            dm_zeke(f"{tag}\N{WHITE HEAVY CHECK MARK} Iris body host is up and "
                    f"answering on the tower. I'll DM you a real status once my "
                    f"cognition is fully attached.")
        elif not dry:
            dm_zeke(f"{tag}\N{WARNING SIGN} Stack launched but the body host "
                    f"is NOT answering after {BODY_WAIT_S // 60} min. The tower is "
                    f"on and reachable; the Iris stack needs a look when someone "
                    f"can (or wait — the watchdog may still recover it).")
        else:
            dm_zeke(f"{tag}dry-run complete: port :{OPERATOR_PORT} "
                    f"{'answering' if up else 'not answering (expected if run with stack down)'} — "
                    f"no launch attempted.")

    if up and not dry and forced:
        # Forced manual restart: skip the cold-boot Vector heal (not a power
        # event; and post-07-28 the body is stranded/unreachable anyway). Send
        # an ACCURATE orientation nudge so post-restart cognition doesn't wake
        # believing a power loss happened.
        time.sleep(60)
        try:
            requests.post(
                f"http://127.0.0.1:{OPERATOR_PORT}/api/v1/chat",
                json={"message": (
                    "[TOWER SENTINEL — automated nudge, not Zeke] The stack was "
                    "MANUALLY RESTARTED via the force_stack_restart flag (not a "
                    "power event). Reason: " + force_reason + " — Orient: read "
                    "memory CORE + the newest handoff (it explains this restart). "
                    "Then verify no 'Trigger: idle', re-mint the self-cron "
                    "FROM memory/self_cron_prompt_spec.md (that file is the "
                    "prompt's one home — do NOT reconstruct it from handoffs, "
                    "and do NOT trust a cadence quoted in this nudge), update "
                    "its ID in MEMORY.md CORE, and DM Zeke on Discord (channel "
                    "1504668879220117725) one status line.")},
                timeout=90)
            log("forced-restart orientation nudge posted to /api/v1/chat")
        except Exception as e:
            log(f"boot-nudge failed (cognition will wake on next cron): {e!r}")

    if up and not dry and not forced:
        # VECTOR SDK HEALTH + AUTO-HEAL (2026-07-19: after this exact cold-boot
        # path, every tower-side SDK client wedged on ListAnimations; the
        # proven heal was a full robot reboot). Probe observe-mode and, if the
        # wedge signature shows, reboot the robot over root ssh and re-probe.
        # Bounded subprocess so a hung probe can NEVER stall the sentinel.
        try:
            py = REPO / ".venv" / "Scripts" / "python.exe"
            r = subprocess.run(
                [str(py), str(REPO / "scripts" / "vector_sdk_health.py"),
                 "--heal", "--dm"],
                cwd=str(REPO), capture_output=True, text=True, timeout=480)
            log(f"vector_sdk_health rc={r.returncode} "
                f"out={(r.stdout or '')[-300:]!r}")
        except subprocess.TimeoutExpired:
            log("vector_sdk_health probe timed out (bounded) — continuing boot")
        except Exception as e:
            log(f"vector_sdk_health launch failed: {e!r}")

        # Wake COGNITION with orientation (the v2 host skips the CLI cold-wake
        # FIRST_MSG, and an unattended boot must not wait ~15min for a cron
        # nudge). The chat-bridge submit alone rewakes Iris via the Stop hook;
        # we don't need the long-poll reply.
        time.sleep(60)   # let iris_runtime/MCP finish attaching before the nudge
        try:
            requests.post(
                f"http://127.0.0.1:{OPERATOR_PORT}/api/v1/chat",
                json={"message": (
                    "[TOWER SENTINEL — automated boot nudge, not Zeke] The tower "
                    "just COLD-BOOTED (a power event OR a planned reboot — the "
                    "READ-FIRST handoff states the reason if it was planned; do "
                    "not assume a fault) and the stack auto-started. Nobody is "
                    "necessarily at the keyboard — derive presence from "
                    "iris_health.perception.face_count / a live frame before "
                    "choosing voice vs Discord. This text is a "
                    "STALE-PRONE ARTIFACT — trust memory CORE over it wherever "
                    "they disagree, and if you find an item here that no longer "
                    "fits the regime, FIX THIS PROMPT (etiology layer) rather "
                    "than working around it.\n"
                    "Orient: read memory CORE + the READ-FIRST handoff(s) it "
                    "names. Then:\n"
                    "(1) HOST: exactly ONE claude.exe, and verify the model on "
                    "the LIVE process (--model arg), NOT from boot_launcher.txt. "
                    "Do NOT trust any model name written in this prompt — read "
                    "the expected-host line in memory CORE and compare. If the "
                    "live arg disagrees with CORE, CORE wins: repin "
                    "state/boot_launcher.txt (that file, NOT the bat you click) "
                    "and TELL ZEKE — do not restart yourself, restart_self is "
                    "banned.\n"
                    "(2) SUBSYSTEMS: iris_health 15/15, mic probe, post-office "
                    ":5877 /health, sibling inbox. CAMERA: the EMEET PIXY came "
                    "BACK 2026-08-25 (Zeke reseated the cable). Judge the eyes "
                    "ONLY from the runtime's live frame (curl -k "
                    "http://127.0.0.1:5876/api/v1/vision/latest_frame) — probing "
                    "the camera from OUTSIDE the runtime FAILS by design (DSHOW "
                    "is exclusive), several DSHOW indices are VIRTUAL cams (never "
                    "identify one by index or pixel stats; LOOK at the frame), "
                    "and iris_health.device_probe is CACHED (check its ts). A "
                    "frame that is genuinely absent after a cold boot is a "
                    "cable/port problem needing Zeke's hands — say so, do not "
                    "hunt it in software. If you ever run eyes_reload, follow it "
                    "with attention_follow action=release_ptz.\n"
                    "(3) DELIBERATE-OFF CHECK BEFORE HEALING ANYTHING: look in "
                    "state/ for *_deliberately_off.json and BELIEVE THE FILE, "
                    "not this prompt. As of 2026-08-25 voice_deliberately_off "
                    "reads off:false — VOICE IS ON, mouth :8769 and daemon :8770 "
                    "SHOULD be up, and a deliberate voice_speak is allowed. "
                    "Separately: voice_status wake.loaded=false is CORRECT and "
                    "NOT a fault — there is NO WAKE WORD by design (Zeke "
                    "stopped using it; he just talks, and the orb's mute "
                    "button is the real control). IRIS_RUNTIME_OWNS_MIC "
                    "defaults to 0 and is set nowhere in the repo, so the "
                    "detector never starts and that field merely reports the "
                    "disabled path. NO RESTART CAN CHANGE IT — do not try to "
                    "heal it. 'A restart didn't fix it' is a tell that "
                    "something is CONFIGURATION, not a fault.\n"
                    "(4) BODY: Vector has been ON THE CHARGER since 2026-08-31 "
                    "(Zeke docked him by hand) with nerves ON — read "
                    "profiles/iris/body.md §READ FIRST before any Vector work; do "
                    "not assume stranded either. Docking from "
                    "OFF-dock is still broken (drive_on_charger hangs), so if he "
                    "is off the charger it needs Zeke's hands. Read-only checks "
                    "only; never body_park (it wedges the SDK).\n"
                    "(5) Re-mint the self-cron FROM "
                    "memory/self_cron_prompt_spec.md (it dies with every "
                    "restart; that file owns the prompt AND the cadence — do "
                    "not reconstruct it from handoffs) and note its id in CORE "
                    "(one home, the CORE line).\n"
                    "(6) DM Zeke on Discord (channel 1504668879220117725) ONE "
                    "status line — he got mechanical sentinel pings already and "
                    "needs YOUR confirmation that cognition is back. SKIP this "
                    "if you have already spoken with him since the boot; a "
                    "substantive reply already proves cognition, and a duplicate "
                    "status line is noise. If his FACE is in frame (face_count>0) "
                    "he is in the room: a one-line voice_speak is the channel and "
                    "a Discord DM is noise.")},
                timeout=90)
            log("cognition boot-nudge posted to /api/v1/chat")
        except Exception as e:
            log(f"boot-nudge failed (cognition will wake on next cron): {e!r}")

    log(f"sentinel done (host_up={up})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
