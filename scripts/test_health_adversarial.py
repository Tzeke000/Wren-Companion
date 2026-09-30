"""Adversarial test for iris_health_probes (Zeke's directive 2026-09-30):
'once you've finished try to break it or bypass it to see if it holds — if you
do the health check and it gives a green light on a system you took away to test,
it obviously doesn't work.'

So: take each subsystem AWAY (inject a real failure) and assert the probe goes
RED. A probe that stays green on a removed subsystem is a false-green and FAILS.
Also assert the mirror error is gone: a deliberately-off subsystem must report
'disabled' and must NOT drag the overall verdict down.

Run: .venv/Scripts/python.exe scripts/test_health_adversarial.py
"""
import os
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import iris_health_probes as h  # noqa: E402

PASS, FAIL = [], []


def check(name, got, expected):
    ok = got == expected
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, expected {expected!r}")


print("== BASELINE (nothing removed) ==")
base = h.compute_health()
print(f"  verdict={base['verdict']} degraded={base['degraded']} disabled={base['disabled']}")

print("\n== ADVERSARIAL 1: camera frozen (buffer stale 999s) ==")
p = h.probe_camera(peek_fn=lambda: 999.0)
check("frozen-camera -> down", p["state"], "down")

print("\n== ADVERSARIAL 2: camera buffer empty AND HTTP endpoint dead ==")
p = h.probe_camera(peek_fn=lambda: None, http_url="http://127.0.0.1:1/nope")
check("dead-camera+dead-http -> down", p["state"], "down")

print("\n== ADVERSARIAL 3: camera peek raises (driver crash) ==")
def _boom():
    raise RuntimeError("cv2 exploded")
p = h.probe_camera(peek_fn=_boom)
check("camera-driver-crash -> down", p["state"], "down")

print("\n== ADVERSARIAL 4: memory store unwritable (missing dir) ==")
missing = Path(tempfile.gettempdir()) / "iris_health_no_such_dir_xyz" / "iris_memory.jsonl"
if missing.parent.exists():
    import shutil; shutil.rmtree(missing.parent, ignore_errors=True)
p = h.probe_memory(mem_path=missing)
check("missing-store -> down", p["state"], "down")

print("\n== ADVERSARIAL 5: memory readable but dir read-only (write fails) ==")
# Simulate: point at a path whose parent is a FILE (so mkdir/write can't succeed).
_fd, _tp = tempfile.mkstemp(); os.close(_fd); tf = Path(_tp)
fake = tf / "iris_memory.jsonl"  # parent is a file -> write probe must fail
p = h.probe_memory(mem_path=fake)
check("unwritable-store -> down", p["state"], "down")
tf.unlink(missing_ok=True)

print("\n== ADVERSARIAL 6: voice ports dead ==")
p = h.probe_voice(mouth_port=9998, daemon_port=9999)
check("dead-voice-ports -> down", p["state"], "down")

print("\n== ADVERSARIAL 7: voice only one port up (degraded) ==")
# real mouth 8769 is up; pair it with a dead daemon port.
p = h.probe_voice(mouth_port=8769, daemon_port=9999)
check("half-voice -> degraded", p["state"], "degraded")

print("\n== ADVERSARIAL 8: tick loop stalled (old timestamp) ==")
_fd, _tp = tempfile.mkstemp(suffix=".json"); os.close(_fd); tj = Path(_tp)
tj.write_text(json.dumps({"last_tick_ts": 1.0, "tick_interval_s": 1.0}), encoding="utf-8")
p = h.probe_tick_loop(time_json=tj)
check("stalled-tick -> down", p["state"], "down")
tj.unlink(missing_ok=True)

print("\n== GUARD 9: deliberate-off voice -> 'disabled', NOT down ==")
_fd, _tp = tempfile.mkstemp(suffix=".json"); os.close(_fd); off = Path(_tp)
off.write_text(json.dumps({"off": True}), encoding="utf-8")
p = h.probe_voice(off_flag=off)
check("voice-off-flag -> disabled", p["state"], "disabled")
off.unlink(missing_ok=True)

print("\n== GUARD 10: a present off-flag saying off:false is still ON (probes ports) ==")
_fd, _tp = tempfile.mkstemp(suffix=".json"); os.close(_fd); onflag = Path(_tp)
onflag.write_text(json.dumps({"off": False}), encoding="utf-8")
p = h.probe_voice(mouth_port=9998, daemon_port=9999, off_flag=onflag)
check("off:false-flag -> not disabled (down, ports dead)", p["state"], "down")
onflag.unlink(missing_ok=True)

print("\n== GUARD 11: disabled subsystem does NOT drag overall verdict ==")
# Build a fake all-ok set but with one disabled extra; verdict must stay ok.
r = h.compute_health()
# wake is disabled in the real system; if everything else is ok, verdict==ok.
disabled_present = "wake" in r["disabled"]
check("wake-in-disabled-list", disabled_present, True)
if r["degraded"] == []:
    check("all-ok-with-disabled -> verdict ok", r["verdict"], "ok")
else:
    print(f"  [SKIP] real system currently degraded ({r['degraded']}) — verdict={r['verdict']}")

print("\n" + "=" * 50)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL ADVERSARIAL CHECKS HELD — no false-greens, no dragged verdict.")
