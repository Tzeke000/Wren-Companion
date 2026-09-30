"""Adversarial tests for brain/wake_salience (Iris_fixes #5).

The gate's job is to be QUIET only where past-me has already proven the wake is
noise, and LOUD everywhere else. So most of these tests try to make it go quiet
when it must not.

Run:  .venv\\Scripts\\python.exe scripts\\test_wake_salience.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

try:  # Windows console is cp1252; the checks print arrows/dashes
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix="wake_salience_")
os.environ["WAKE_SALIENCE_LEDGER"] = os.path.join(_tmp, "wake_verdicts.jsonl")

from brain import wake_salience as ws  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def fresh_ledger() -> None:
    p = ws.ledger_path()
    if os.path.exists(p):
        os.remove(p)
    ws._CACHE["mtime"] = -1.0


NOW = time.time()
BOOT_UNKNOWN = ws.signature("face_appeared", "unknown", boot_age_s=25, zeke_wifi="present")
ZEKE_RETURN = ws.signature("face_appeared", "zeke", gone_s=120, zeke_wifi="present")
STRANGER_AWAY = ws.signature("face_appeared", "unknown", boot_age_s=900, zeke_wifi="away")
STRANGER_UNK = ws.signature("face_appeared", "unknown", boot_age_s=900, zeke_wifi="unknown")

# 0. signatures are deterministic + phase-aware
check("sig boot phase", BOOT_UNKNOWN == "face_appeared|unknown|boot|wifi=present", BOOT_UNKNOWN)
check("sig return phase", ZEKE_RETURN == "face_appeared|zeke|return<300|wifi=present", ZEKE_RETURN)
check("sig first phase", ws.signature("face_appeared", "zeke", boot_age_s=500, first_obs=True)
      == "face_appeared|zeke|first|wifi=unknown")
check("sig normalises unknown", ws.signature("face_appeared", "", zeke_wifi="present").split("|")[1] == "unknown")

# 1. a never-seen shape WAKES
fresh_ledger()
j = ws.judge(BOOT_UNKNOWN, now=NOW)
check("new shape wakes", j["wake"] and "insufficient" in j["reason"], j["reason"])

# 2. below MIN_EVIDENCE still wakes
for i in range(ws.MIN_EVIDENCE - 1):
    ws.record(BOOT_UNKNOWN, "nothing", note="it was Zeke, model warming", ts=NOW - 1000 + i)
j = ws.judge(BOOT_UNKNOWN, now=NOW)
check("one short of evidence wakes", j["wake"], j["reason"])

# 3. MIN_EVIDENCE 'nothing' in a row → suppressed
ws.record(BOOT_UNKNOWN, "nothing", note="Zeke again", ts=NOW - 900)
j = ws.judge(BOOT_UNKNOWN, now=NOW)
check("proven-noise shape is suppressed", not j["wake"], j["reason"])

# 4. ADVERSARIAL: the same evidence must NOT quiet a stranger while Zeke is away/unknown
fresh_ledger()
for i in range(12):
    ws.record(STRANGER_AWAY, "nothing", ts=NOW - 5000 + i)
    ws.record(STRANGER_UNK, "nothing", ts=NOW - 5000 + i)
check("stranger while away NEVER suppressed", ws.judge(STRANGER_AWAY, now=NOW)["wake"],
      ws.judge(STRANGER_AWAY, now=NOW)["reason"])
check("stranger while wifi-unknown NEVER suppressed", ws.judge(STRANGER_UNK, now=NOW)["wake"])

# 5. ADVERSARIAL: an 'acted' among recent verdicts re-opens the gate
fresh_ledger()
for i in range(5):
    ws.record(ZEKE_RETURN, "nothing", ts=NOW - 4000 + i)
check("zeke return proven noise → suppressed", not ws.judge(ZEKE_RETURN, now=NOW)["wake"])
ws.record(ZEKE_RETURN, "acted", note="greeted him, he'd been at work", ts=NOW - 3000)
j = ws.judge(ZEKE_RETURN, now=NOW)
check("one 'acted' re-opens the gate", j["wake"] and "acted" in j["reason"], j["reason"])
# ...and the trailing-run rule: nothing,nothing after an acted is still < MIN_EVIDENCE in a row
ws.record(ZEKE_RETURN, "nothing", ts=NOW - 2000)
ws.record(ZEKE_RETURN, "nothing", ts=NOW - 1000)
check("acted still inside recent window keeps it open", ws.judge(ZEKE_RETURN, now=NOW)["wake"])

# 6. ADVERSARIAL: 'unrecorded' and 'auto_suppressed' rows are NOT evidence
fresh_ledger()
for i in range(10):
    ws.record(BOOT_UNKNOWN, "unrecorded", ts=NOW - 5000 + i)
    ws.record(BOOT_UNKNOWN, "auto_suppressed", ts=NOW - 4000 + i)
j = ws.judge(BOOT_UNKNOWN, now=NOW)
check("non-explicit rows don't count as evidence", j["wake"] and "insufficient" in j["reason"], j["reason"])

# 7. audit sample: every AUDIT_EVERY-th consecutive suppression fires anyway
fresh_ledger()
for i in range(ws.MIN_EVIDENCE):
    ws.record(BOOT_UNKNOWN, "nothing", ts=NOW - 9000 + i)
fired_at = []
for k in range(ws.AUDIT_EVERY * 2):
    j = ws.judge(BOOT_UNKNOWN, now=NOW)
    if j["wake"]:
        fired_at.append(k)
        check("audit-sample wake is labelled", j.get("audit") is True and "audit" in j["reason"], j["reason"])
        # a real wake got through: pretend cognition judged it (still nothing)
        ws.record(BOOT_UNKNOWN, "nothing", note="audit: still Zeke", ts=NOW - 8000 + k)
    else:
        ws.record(BOOT_UNKNOWN, "auto_suppressed", note=j["reason"], ts=NOW - 8000 + k)
check("audit sample fires periodically", len(fired_at) == 2 and fired_at[0] == ws.AUDIT_EVERY - 1,
      "fired at %s" % fired_at)

# 8. stale evidence expires
fresh_ledger()
for i in range(5):
    ws.record(BOOT_UNKNOWN, "nothing", ts=NOW - ws.MAX_AGE_S - 100 + i)
j = ws.judge(BOOT_UNKNOWN, now=NOW)
check("verdicts older than MAX_AGE are not evidence", j["wake"], j["reason"])

# 9. corrupt ledger lines are skipped, not fatal; a wholly broken ledger fails OPEN
fresh_ledger()
for i in range(ws.MIN_EVIDENCE):
    ws.record(BOOT_UNKNOWN, "nothing", ts=NOW - 100 + i)
with open(ws.ledger_path(), "a", encoding="utf-8") as f:
    f.write("{this is not json\n")
    f.write('{"sig": "x", "verdict": "bogus", "ts": 1}\n')
ws._CACHE["mtime"] = -1.0
j = ws.judge(BOOT_UNKNOWN, now=NOW)
check("corrupt lines skipped, good evidence still counts", not j["wake"], j["reason"])
os.environ["WAKE_SALIENCE_LEDGER"] = os.path.join(_tmp, "nope", "dir")  # a directory path can't be read
os.makedirs(os.environ["WAKE_SALIENCE_LEDGER"], exist_ok=True)
ws._CACHE["mtime"] = -1.0
j = ws.judge(BOOT_UNKNOWN, now=NOW)
check("unreadable ledger fails OPEN (wakes)", j["wake"], j["reason"])
os.environ["WAKE_SALIENCE_LEDGER"] = os.path.join(_tmp, "wake_verdicts.jsonl")
ws._CACHE["mtime"] = -1.0

# 10. writer validates — a bad verdict RAISES (no swallowed TypeError, no silent no-op)
try:
    ws.record(BOOT_UNKNOWN, "maybe")
    check("bad verdict raises", False, "no exception")
except ValueError as e:
    check("bad verdict raises", True, str(e))
try:
    ws.record("", "nothing")
    check("empty signature raises", False)
except ValueError:
    check("empty signature raises", True)
# and the writer actually writes (the counterfactual-archive failure mode)
fresh_ledger()
ws.record(ZEKE_RETURN, "nothing", note="probe")
check("record() really appends a line", os.path.getsize(ws.ledger_path()) > 0)
check("history_line mentions the shape", ZEKE_RETURN in ws.history_line(ZEKE_RETURN))

# 11. the tool wrapper rejects non-explicit verdicts and surfaces errors
try:
    from tools.system.wake_verdict_tool import _wake_verdict
    r = _wake_verdict({"signature": ZEKE_RETURN, "verdict": "auto_suppressed"}, {})
    check("tool refuses non-explicit verdict", r.get("ok") is False, str(r.get("error")))
    r = _wake_verdict({"signature": ZEKE_RETURN, "verdict": "nothing", "note": "t"}, {})
    check("tool records + returns gate state", r.get("ok") is True and "gate_now" in r)
    r = _wake_verdict({"action": "judge"}, {})
    check("tool judge without signature errors", r.get("ok") is False)
except Exception as e:
    check("tool wrapper importable", False, repr(e))

n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
print("\n%d/%d passed" % (len(RESULTS) - n_fail, len(RESULTS)))
sys.exit(1 if n_fail else 0)
