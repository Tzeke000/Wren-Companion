"""scripts/setup_virtual_audio.py — VB-CABLE A+B presence + smoketest.

Component 1 of the autonomous testing work order. The actual VB-CABLE
install requires admin privileges and a reboot, so this script does NOT
install — it verifies presence and routes a known tone end-to-end so we
can confirm the cables are correctly named and wired before driving the
full doctor harness.

Usage:
    py -3.11 scripts/setup_virtual_audio.py             # verify only
    py -3.11 scripts/setup_virtual_audio.py --tone-test # play 1s tone, capture, compare

Per docs/AUTONOMOUS_TESTING.md § Virtual audio cable setup.

Expected device names (case-insensitive substring match, since indices
renumber across reboots):

    Claude -> Ava direction:
      output: "CABLE Input"      (Claude plays here)
      input:  "CABLE Output"     (Ava records here as her mic)

    Ava -> Claude direction:
      output: "CABLE-A Input"    (Ava plays her TTS here)
      input:  "CABLE-A Output"   (Claude records here as his "ears")

If install is missing, prints concrete next steps (download URL, install
flow, reboot reminder).
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any


def _try_import_audio() -> tuple[Any, Any] | None:
    try:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore
        return sd, np
    except Exception as e:
        print(f"[venv] sounddevice / numpy import failed: {e!r}")
        return None


def _find_device(sd, name_substr: str, kind: str) -> int | None:
    """Return device index whose name contains substr and has the right channel count."""
    target_key = f"max_{kind}_channels"
    name_lower = name_substr.lower()
    for i, dev in enumerate(sd.query_devices()):
        if name_lower in dev.get("name", "").lower() and dev.get(target_key, 0) > 0:
            return i
    return None


_NEEDED = [
    ("CABLE Input", "output", "Claude -> Ava (Claude's TTS plays here)"),
    ("CABLE Output", "input", "Claude -> Ava (Ava's mic records here)"),
    ("CABLE-A Input", "output", "Ava -> Claude (Ava's TTS plays here)"),
    ("CABLE-A Output", "input", "Ava -> Claude (Claude's STT records here)"),
]


def _print_install_help() -> None:
    print()
    print("VB-CABLE A+B not detected. Install steps:")
    print("  1. Download VB-CABLE driver pack:")
    print("     https://vb-audio.com/Cable/")
    print("     File: VBCABLE_Driver_Pack45.zip")
    print("  2. Right-click VBCABLE_Setup_x64.exe -> Run as administrator.")
    print("  3. Reboot.")
    print("  4. Donate $5 for the A+B pack at:")
    print("     https://shop.vb-audio.com/en/win-apps/12-vb-cable-ab.html")
    print("  5. Run VBCABLE_A_Setup_x64.exe and VBCABLE_B_Setup_x64.exe as admin.")
    print("  6. Reboot again.")
    print("  7. In Sound -> Recording, set each CABLE Output device's sample rate to")
    print("     48000 Hz, 16-bit, in its Properties -> Advanced tab.")
    print("  8. Re-run this script to verify presence.")
    print()


def verify_presence(sd) -> bool:
    print("Scanning for VB-CABLE A+B devices...")
    print()
    all_found = True
    for name, kind, role in _NEEDED:
        idx = _find_device(sd, name, kind)
        if idx is None:
            print(f"  [MISSING] {name} ({kind})  — {role}")
            all_found = False
        else:
            dev = sd.query_devices(idx)
            print(f"  [OK]      {name} ({kind})  index={idx}  rate={dev.get('default_samplerate', '?')}  — {role}")
    print()
    return all_found


def _device_rate(sd, idx: int, fallback: int = 44100) -> int:
    try:
        rate = sd.query_devices(idx).get("default_samplerate")
        return int(rate) if rate else fallback
    except Exception:
        return fallback


def tone_test(sd, np_mod, src_name: str = "CABLE Input", sink_name: str = "CABLE Output") -> bool:
    """Play a 1s 440Hz tone on `src_name`, capture from `sink_name`, verify peak.

    Sample rate is queried from the actual devices at runtime — hardcoding
    48 kHz on a 44.1 kHz device caused WASAPI to emit garbage frames whose
    raw float32 bit pattern read as 3e38 (max float).

    Uses `sd.playrec` so playback and capture run on the same call —
    avoids the race where `sd.rec` returns its buffer before the recording
    callback has filled it, which surfaced as NaN samples on this machine.
    """
    print(f"Running tone test (1s @ 440Hz, {src_name} -> {sink_name})...")

    out_idx = _find_device(sd, src_name, "output")
    in_idx = _find_device(sd, sink_name, "input")
    if out_idx is None or in_idx is None:
        print(f"  Cannot run tone test — {src_name}/{sink_name} not found.")
        return False

    out_rate = _device_rate(sd, out_idx)
    in_rate = _device_rate(sd, in_idx)
    if out_rate != in_rate:
        # If the two cable endpoints disagree, the OS will resample for us
        # — but only cleanly if both rates are sane. Pick the lower one so
        # we never ask either device for samples it can't generate.
        sample_rate = min(out_rate, in_rate)
        print(f"  Note: device rates differ (out={out_rate} Hz, in={in_rate} Hz); using {sample_rate} Hz.")
    else:
        sample_rate = out_rate
    print(f"  Sample rate: {sample_rate} Hz")

    duration = 1.0
    freq = 440.0
    t = np_mod.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    tone = (0.3 * np_mod.sin(2 * np_mod.pi * freq * t)).reshape(-1, 1).astype(np_mod.float32)

    try:
        captured = sd.playrec(
            tone,
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=(in_idx, out_idx),
        )
        sd.wait()
    except Exception as e:
        print(f"  [FAIL] playrec error: {e!r}")
        return False

    flat = np_mod.asarray(captured).flatten()
    if flat.size == 0:
        print("  [FAIL] recording returned empty buffer.")
        return False
    if not np_mod.all(np_mod.isfinite(flat)):
        nan_count = int(np_mod.isnan(flat).sum())
        inf_count = int(np_mod.isinf(flat).sum())
        print(f"  [FAIL] capture contains non-finite samples ({nan_count} NaN, {inf_count} inf) — driver / device-config issue.")
        return False
    peak = float(np_mod.abs(flat).max())
    print(f"  Captured peak amplitude: {peak:.4f}  (expected ~0.3, sane range 0.05–1.0)")
    if peak < 0.05:
        print("  [FAIL] Tone did not reach the capture side. Check cable routing or sample rate.")
        return False
    if peak > 1.5:
        print("  [FAIL] Captured peak is implausibly large — likely a sample-rate or dtype mismatch.")
        return False
    print("  [OK] Tone end-to-end loopback verified.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify VB-CABLE A+B install for autonomous testing.")
    parser.add_argument("--tone-test", action="store_true", help="Play+capture a tone to verify routing.")
    args = parser.parse_args()

    audio = _try_import_audio()
    if audio is None:
        print("Install sounddevice + numpy: py -3.11 -m pip install sounddevice numpy")
        return 2
    sd, np_mod = audio

    ok_presence = verify_presence(sd)
    basic_present = (
        _find_device(sd, "CABLE Input", "output") is not None
        and _find_device(sd, "CABLE Output", "input") is not None
    )
    a_present = (
        _find_device(sd, "CABLE-A Input", "output") is not None
        and _find_device(sd, "CABLE-A Output", "input") is not None
    )

    if args.tone_test:
        any_failed = False
        if basic_present:
            if not tone_test(sd, np_mod, "CABLE Input", "CABLE Output"):
                any_failed = True
        if a_present:
            print()
            if not tone_test(sd, np_mod, "CABLE-A Input", "CABLE-A Output"):
                any_failed = True
        if not basic_present and not a_present:
            print("  Cannot run tone test — no CABLE devices detected.")
            any_failed = True
        if any_failed:
            return 1
        if not ok_presence:
            print()
            print("Note: basic CABLE verified end-to-end, but A+B pack still missing.")
            _print_install_help()
            return 1

    if not ok_presence:
        _print_install_help()
        return 1

    print("VB-CABLE A+B verified. Ready for the doctor harness driver.")
    print("Next: ensure Ava is running (start_ava.bat), then:")
    print("  py -3.11 scripts/diagnostic_session.py --probe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
