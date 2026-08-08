"""scripts/verify_face_tracking_wiring.py

Proves the brain/face_tracking.py wiring done 2026-08-07 behaves, WITHOUT
waiting for a stack restart or for a real stranger to stand in front of the
camera for 12 seconds.

Why it exists: the wiring inserted a call into the live video-capture loop
(iris_runtime._iris_video_capture_loop). That loop can't be hot-swapped — a
running `while True` keeps its old code object — so the hook is inert until the
next restart, which means the usual "run the smallest test that exercises the
fixed path" isn't available. This is that test, driven synthetically: it fakes
frames at the loop's real ~5Hz face cadence and asserts on promotion behaviour.

The two defects it pins down (both found by reading the code before trusting it):
  1. EMPTY ROOM PROMOTED A PHANTOM. update() documented "Unknown face (or no
     face at all)" as one case, and the capture loop sets person_id="unknown"
     for an empty frame — so 12s of nobody there would have minted a person,
     written to my inner monologue, and logged an audit row.
  2. THE PROMOTION SIGNAL NEVER FIRED. _on_promotion imported
     `publish, SIGNAL_PERSON_ONBOARDED` from brain.signal_bus; neither name has
     ever existed, so the import raised and the fire was swallowed.

Run: .venv\Scripts\python.exe scripts\verify_face_tracking_wiring.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import face_tracking as ft   # noqa: E402

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r} want {want!r}")


class FakeBus:
    """Stands in for the real SignalBus — same fire() signature."""

    def __init__(self) -> None:
        self.fired: list[tuple] = []

    def fire(self, signal_type, data=None, priority="low") -> None:
        self.fired.append((signal_type, dict(data or {}), priority))


def fresh_g(tmp: Path) -> dict:
    """A g dict shaped like the runtime's, pointed at a scratch BASE_DIR so the
    audit log and any monologue write land in the temp dir, not in real state."""
    return {"BASE_DIR": str(tmp), "_signal_bus": FakeBus()}


def run(g: dict, seconds: float, *, faces: int, pid: str | None,
        t0: float, hz: float = 5.0) -> list[dict]:
    """Drive tick_from_capture at the capture loop's real face cadence."""
    outs = []
    n = max(1, int(seconds * hz))
    for i in range(n):
        ts = t0 + (i / hz)
        outs.append(ft.tick_from_capture(
            g, face_results=[{"person_id": pid or "unknown"}] * faces,
            recognized_person_id=pid, similarity=0.9 if pid else 0.0,
            frame_ts=ts))
    return outs


def main() -> int:
    import tempfile
    persistence = float(ft._cfg("temporal_filter", "unknown_persistence_seconds",
                                default=12.0))
    print(f"config: unknown_persistence_seconds={persistence}")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        t0 = time.time()

        # ---- DEFECT 1: an empty room must NEVER promote -------------------
        g = fresh_g(tmp)
        outs = run(g, persistence * 3, faces=0, pid="unknown", t0=t0)
        check("empty room: no promotion in 3x the persistence window",
              any(o.get("promoted_new_person") for o in outs), False)
        check("empty room: reports no_face, not unknown_jitter",
              outs[-1].get("status"), "no_face")
        check("empty room: fired no signals", len(g["_signal_bus"].fired), 0)
        check("empty room: wrote no audit log",
              (tmp / "state" / "face_tracking_log.jsonl").exists(), False)

        # ---- a REAL unknown face still promotes (didn't break the feature) -
        g = fresh_g(tmp)
        outs = run(g, persistence + 2, faces=1, pid="unknown", t0=t0)
        promos = [o for o in outs if o.get("promoted_new_person")]
        check("real unknown face: promotes exactly once", len(promos), 1)
        check("real unknown face: got a temp id",
              bool(promos and str(promos[0].get("temp_id", "")).startswith("unknown_")),
              True)

        # ---- DEFECT 2: the promotion signal must reach the bus ------------
        fired = g["_signal_bus"].fired
        check("promotion fires exactly one signal", len(fired), 1)
        check("signal type is new_person_detected",
              fired[0][0] if fired else None, "new_person_detected")
        check("signal carries the temp id",
              bool(fired and fired[0][1].get("temp_id")), True)
        check("promotion wrote the audit row",
              (tmp / "state" / "face_tracking_log.jsonl").exists(), True)

        # ---- a KNOWN face must never promote ------------------------------
        g = fresh_g(tmp)
        outs = run(g, persistence * 2, faces=1, pid="zeke", t0=t0)
        check("known face: never promotes",
              any(o.get("promoted_new_person") for o in outs), False)
        check("known face: status known", outs[-1].get("status"), "known")

        # ---- the merge bug the no-face reset prevents ---------------------
        # Someone unknown stands there 8s (under the 12s bar), LEAVES, and later
        # someone else arrives. Without the reset their candidacies would add up
        # and the second person would promote almost instantly, attributed to a
        # window that started before they existed.
        g = fresh_g(tmp)
        run(g, 8, faces=1, pid="unknown", t0=t0)
        run(g, 5, faces=0, pid="unknown", t0=t0 + 8)        # room empties
        outs = run(g, 3, faces=1, pid="unknown", t0=t0 + 13)  # someone new, 3s
        check("candidacy does not carry across an empty room",
              any(o.get("promoted_new_person") for o in outs), False)

        # ---- cooldown holds ----------------------------------------------
        g = fresh_g(tmp)
        run(g, persistence + 1, faces=1, pid="unknown", t0=t0)
        outs = run(g, persistence + 1, faces=1, pid="unknown", t0=t0 + persistence + 2)
        check("cooldown blocks a second promotion",
              any(o.get("promoted_new_person") for o in outs), False)

        # ---- the hook itself must never raise ----------------------------
        check("hook survives garbage input",
              ft.tick_from_capture({}, face_results="not a list",
                                   recognized_person_id=None).get("status")
              in ("no_face", "unknown_jitter_start", "error"), True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
