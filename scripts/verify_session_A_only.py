"""Session A only retry — verify close-app fix landed.

Imports the Session A prompt list and helpers from verify_phase_b_with_side_effects
but only runs Session A. Used after close-app fix to confirm the 4 close-failures
from the prior run now pass with actual termination."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.verify_phase_b_with_side_effects import (
    SESSION_A,
    run_session,
    write_transcript,
)


def main() -> int:
    records, stats = run_session("A", SESSION_A)
    path = write_transcript("A", records, stats)
    print(f"\n[Session A] saved to {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
