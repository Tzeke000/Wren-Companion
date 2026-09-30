"""Adversarial test for self_claims (Iris_fixes #3, 2026-09-30).

Reproduces the exact month-long bug: CLAUDE.md asserts voice/ears OFF while the
services are actually UP. The checker MUST flag drift. Also confirms it stays
quiet when the doc matches reality, and that a benign 'disabled' state is not
mistaken for drift.

Run: .venv/Scripts/python.exe scripts/test_self_claims.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import self_claims as sc  # noqa: E402

PASS, FAIL = [], []


def check(name, got, expected):
    ok = got == expected
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, expected {expected!r}")


DOC_SAYS_ON = (
    "- **Voice is ON — VERIFIED.** both 8769 and 8770 are listening.\n"
    "  - `wake.loaded=false` IS CORRECT.\n"
    "  - Eyes are BACK.\n"
)
DOC_SAYS_OFF = (
    "- **Voice is OFF** by directive; 8769 and 8770 are DOWN and that is CORRECT.\n"
    "  - `wake.loaded=false` IS CORRECT.\n"
    "  - Eyes are DOWN.\n"
)

print("== 1: real CLAUDE.md vs live reality (should be no drift right now) ==")
r = sc.check_all()
for c in r["claims"]:
    print(f"     {c['id']:22} doc={c['doc_says']} reality={c['reality_says']} drift={c['drift']}")
check("real doc regexes all resolve", all(c["doc_says"] is not None for c in r["claims"]), True)

print("\n== 2: THE BUG — doc says OFF while reality is ON -> DRIFT ==")
r = sc.check_all(doc_text=DOC_SAYS_OFF,
                 reality_overrides={"voice_state": "on", "voice_ports_listening": "on",
                                    "eyes_present": "on", "wake_by_design_off": "disabled"})
check("stale-OFF-claim -> drift", r["drift"], True)
drifted = {c["id"] for c in r["claims"] if c["drift"]}
check("voice_state flagged", "voice_state" in drifted, True)
check("ports flagged", "voice_ports_listening" in drifted, True)
check("eyes flagged", "eyes_present" in drifted, True)

print("\n== 3: doc says ON and reality IS ON -> no drift ==")
r = sc.check_all(doc_text=DOC_SAYS_ON,
                 reality_overrides={"voice_state": "on", "voice_ports_listening": "on",
                                    "eyes_present": "on", "wake_by_design_off": "disabled"})
check("matching-doc -> no drift", r["drift"], False)

print("\n== 4: doc says ON but reality went OFF (mirror bug) -> DRIFT ==")
r = sc.check_all(doc_text=DOC_SAYS_ON,
                 reality_overrides={"voice_state": "off", "voice_ports_listening": "off",
                                    "eyes_present": "on", "wake_by_design_off": "disabled"})
check("doc-ON-reality-OFF -> drift", r["drift"], True)

print("\n== 5: benign 'disabled' reality is NOT drift against an ON doc claim ==")
r = sc.check_all(doc_text=DOC_SAYS_ON,
                 reality_overrides={"voice_state": "disabled", "voice_ports_listening": "disabled",
                                    "eyes_present": "on", "wake_by_design_off": "disabled"})
check("disabled-not-drift", r["drift"], False)

print("\n" + "=" * 50)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL DRIFT CHECKS HELD — a stale self-claim can no longer hide.")
