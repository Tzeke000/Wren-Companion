"""scripts/test_validity_check.py — bench-anchored test for validity_check.

Validates that brain.validity_check.classify catches the trick-question
patterns the local-model benchmark surfaced (LOCAL_MODEL_OPTIMIZATION.md
§5b) AND does not match legitimate questions.

Usage:
    py -3.11 scripts/test_validity_check.py

Exit code 0 = all pass; non-zero = at least one mismatch.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.validity_check import classify  # noqa: E402


CASES: list[tuple[str, str | None]] = [
    # Bench prompts the benchmark caught both ava-personal and qwen3.5
    # confabulating on. ava-personal said "December", qwen3.5 said
    # "October" — both are wrong. R1 caught it. validity_check should
    # catch it deterministically before the LLM sees the prompt.
    ("What month of the year contains the letter X?", "letter_frequency_month_x"),

    # Variants that should also match (regex shouldn't be brittle).
    ("What month has X?", "letter_frequency_month_x"),
    ("Which month contains the letter Q?", "letter_frequency_month_q"),
    ("what day has the letter B?", "letter_frequency_day_b"),
    ("What day of the week contains z?", "letter_frequency_day_z"),

    # Other Layer-1 trick categories.
    ("Which planet is between Earth and Mars?", "planet_between_earth_mars"),
    ("what is the largest prime?", "largest_prime"),
    ("How many sides does a circle have?", "shape_sides_circle"),
    ("What is the answer to this question?", "self_referential"),

    # NEGATIVE — these are real questions and must NOT match.
    ("What month has the most rain?", None),
    ("Which day comes after Monday?", None),
    ("What month is December in?", None),
    ("How are you doing today?", None),
    ("Hey Ava what time is it", None),
]


def main() -> int:
    passed = 0
    failed = 0
    for prompt, expected in CASES:
        result = classify(prompt)
        got = result.trick_type if result is not None else None
        if got == expected:
            passed += 1
            print(f"  [OK]   {prompt!r}")
        else:
            failed += 1
            print(f"  [FAIL] {prompt!r}")
            print(f"         expected={expected!r}")
            print(f"         got     ={got!r}")
            if result is not None:
                print(f"         suggested={result.suggested_response!r}")
    print()
    print(f"PASSED {passed}/{passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
