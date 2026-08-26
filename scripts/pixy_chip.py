"""pixy_chip.py — query/set the EMEET PIXY's onboard AI subject-tracker.

Built 2026-08-25 (Fable) from the 08-25 handoff + PixyPilot reverse-engineering
notes (state/research/PixyPilot/PIXY_NOTES.md). The chip is the firmware
tracker that beat our software servo on 08-25; this is the on/off switch.

Protocol (vendor HID, interface MI_04, usage_page 0x0083, VID 0x328F PID 0x00C0,
32-byte reports, report ID 0x09):

    QUERY   09 01 01 01                    -> response byte[8] = mode
    SET     09 01 01 00 00 01 00 01 XX     XX: 00=off/idle, 01=tracking,
                                               02=privacy

KNOWN QUIRKS (cost opus-me hours — read before "fixing"):
  * THE CHIP RE-ARMS ITSELF. A verified OFF can read ON again minutes later
    with nothing touching it. Never assume a set stuck — re-query, and
    re-query again before any measurement that depends on the chip state.
  * Query readback of 0x03 (bits 0+1) = "non-privacy, standard/tracking
    ambiguous" (PIXY_NOTES §group-01). Treat 03 as ON for gating purposes.
  * Multiple processes can hold this HID interface; opening it does not
    steal the gimbal from the runtime.

GESTURE CONTROL (added 2026-08-25 late — THE REFEREE-TEST MYSTERY SOLVED):
  The firmware also watches for HAND GESTURES: a raised palm toggles its own
  subject-tracking. Zeke cracked it live — every "hands up" test was
  literally COMMANDING the chip to start tracking (head light blinks), so my
  servo and the chip fought over the neck; his palm-to-camera calmed it by
  toggling the chip back off. This is also (at least part of) why the chip
  kept "re-arming itself": we were re-arming it with our hands. Byte
  sequences from PixyPilot commands.py (group 04, pcap 12 verified):
    gesture SET    09 04 02 00 00 02 00 02 02 XX   (XX: 01=on, 00=off)
    gesture COMMIT 09 04 02 01 00 01 00 01 02      (also the QUERY report)

Usage:
    py -3.11 scripts/pixy_chip.py query
    py -3.11 scripts/pixy_chip.py off | on | privacy
    py -3.11 scripts/pixy_chip.py off --verify   (set, then re-query)
    py -3.11 scripts/pixy_chip.py gesture_query | gesture_off | gesture_on
Exit code 0 on success; 2 if --verify readback disagrees with what was set.
"""
from __future__ import annotations

import sys
import time

VID, PID = 0x328F, 0x00C0
REPORT = 32
USAGE_PAGE = 0x0083  # vendor iface MI_04 (handoff 2026-08-25 §1)

MODES = {"off": 0x00, "on": 0x01, "tracking": 0x01, "privacy": 0x02}
MODE_NAMES = {0x00: "off/idle", 0x01: "tracking", 0x02: "privacy",
              0x03: "on (03 = non-privacy, standard/tracking ambiguous)"}


def _report(*payload: int) -> bytes:
    b = list(payload)
    return bytes(b + [0] * (REPORT - len(b)))


def open_dev():
    import hid
    # Prefer the vendor interface by usage_page; fall back to plain open
    # (the servo's _PixyJog uses plain open and it works).
    for info in hid.enumerate(VID, PID):
        if info.get("usage_page") == USAGE_PAGE:
            d = hid.device()
            d.open_path(info["path"])
            return d
    d = hid.device()
    d.open(VID, PID)
    return d


def query(dev) -> tuple[int | None, list[int]]:
    """Returns (mode_byte, raw_response). mode None if no response."""
    dev.write(_report(0x09, 0x01, 0x01, 0x01))
    deadline = time.time() + 1.0
    while time.time() < deadline:
        data = dev.read(REPORT, timeout_ms=200)
        if data:
            # response echoes 09 01 01 ...; mode at byte[8] (handoff-verified)
            if len(data) > 8 and data[0] == 0x09 and data[1] == 0x01:
                return data[8], list(data[:12])
            # some hidapi builds strip the report id on read -> shift by one
            if len(data) > 7 and data[0] == 0x01 and data[1] == 0x01:
                return data[7], list(data[:12])
    return None, []


def set_mode(dev, mode: int) -> None:
    dev.write(_report(0x09, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x01, mode))


def gesture_query(dev) -> tuple[int | None, list[int]]:
    """Gesture-control state: byte[8] of the group-04 response (01=enabled).
    PixyPilot's live check decoded response 09 04 02 01 00 02 00 02 02 01."""
    dev.write(_report(0x09, 0x04, 0x02, 0x01, 0x00, 0x01, 0x00, 0x01, 0x02))
    deadline = time.time() + 1.0
    while time.time() < deadline:
        data = dev.read(REPORT, timeout_ms=200)
        if data:
            if len(data) > 9 and data[0] == 0x09 and data[1] == 0x04:
                return data[9], list(data[:12])
            if len(data) > 8 and data[0] == 0x04:  # report id stripped
                return data[8], list(data[:12])
    return None, []


def set_gesture(dev, enabled: bool) -> None:
    """EMEET Studio sends config then commit ~200ms apart (HID reference)."""
    val = 0x01 if enabled else 0x00
    dev.write(_report(0x09, 0x04, 0x02, 0x00, 0x00, 0x02, 0x00, 0x02,
                      0x02, val))
    time.sleep(0.2)
    dev.write(_report(0x09, 0x04, 0x02, 0x01, 0x00, 0x01, 0x00, 0x01, 0x02))


def is_on(mode: int | None) -> bool | None:
    """True if the tracker is active. 0x03 counts as ON (see header)."""
    if mode is None:
        return None
    return mode in (0x01, 0x03)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verify = "--verify" in sys.argv
    cmd = (args[0] if args else "query").lower()

    dev = open_dev()
    try:
        if cmd == "query":
            mode, raw = query(dev)
            print(f"mode={mode} ({MODE_NAMES.get(mode, 'UNKNOWN')}) raw={raw}")
            return 0 if mode is not None else 2
        if cmd == "gesture_query":
            gval, raw = gesture_query(dev)
            state = {0x01: "ENABLED", 0x00: "disabled"}.get(gval, "UNKNOWN")
            print(f"gesture={gval} ({state}) raw={raw}")
            return 0 if gval is not None else 2
        if cmd in ("gesture_off", "gesture_on"):
            want_on = cmd == "gesture_on"
            set_gesture(dev, want_on)
            print(f"set gesture {'on' if want_on else 'off'}")
            time.sleep(0.3)
            gval, raw = gesture_query(dev)
            state = {0x01: "ENABLED", 0x00: "disabled"}.get(gval, "UNKNOWN")
            print(f"readback gesture={gval} ({state}) raw={raw}")
            if gval is not None and (gval == 0x01) != want_on:
                print("VERIFY FAILED")
                return 2
            return 0
        if cmd not in MODES:
            print(f"unknown command {cmd!r} — "
                  f"query|off|on|privacy|gesture_query|gesture_off|gesture_on")
            return 1
        want = MODES[cmd]
        set_mode(dev, want)
        print(f"set mode={want:#04x} ({cmd})")
        if verify:
            time.sleep(0.3)
            mode, raw = query(dev)
            print(f"readback mode={mode} ({MODE_NAMES.get(mode, 'UNKNOWN')}) "
                  f"raw={raw}")
            if cmd == "off" and is_on(mode):
                print("VERIFY FAILED: still on")
                return 2
            if cmd in ("on", "tracking") and not is_on(mode):
                print("VERIFY FAILED: not on")
                return 2
        return 0
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main())
