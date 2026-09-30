"""Adversarial tests for brain/self_hypotheses (Iris_fixes #9 + addition C).

The point of the model: a self-observation must NOT become a trait by being stated; only
discriminating evidence, repeated over weeks, stabilises a hypothesis — and it can be undone.

Run:  .venv\\Scripts\\python.exe scripts\\test_self_hypotheses.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix="selfhyp_")
os.environ["SELF_HYPOTHESES_BASE"] = _tmp
(Path(_tmp) / "state").mkdir(parents=True, exist_ok=True)

from brain import self_hypotheses as sh  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
NOW = time.time()
DAY = 86400.0


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def fresh() -> None:
    for p in (sh.store_path(), sh.events_path()):
        if os.path.exists(p):
            os.remove(p)


# 1. a single hypothesis is REFUSED — a self-observation is not a trait until it has a rival
fresh()
r = sh.propose("reserved", "Why do I speak so little some days?", {"reserved": "I am naturally reserved"})
check("single hypothesis refused", r["ok"] is False and "rival" in r["error"])
r = sh.propose("reserved", "Why do I speak so little some days?",
               {"reserved": "I am naturally reserved", "busy": "I speak less when Zeke is occupied"}, now=NOW - 40 * DAY)
check("two rivals accepted", r["ok"] and r["action"] == "created")
check("advisory says NOT identity", "NOT identity" in sh.advisory(now=NOW) and "IDENTITY.md" in sh.advisory(now=NOW))
check("undistinguished with no evidence", "undistinguished" in sh.status("reserved", now=NOW)["traits"][0]["verdict"])

# 2. an observation supporting ALL rivals is kept but NON-discriminating: moves nothing
r = sh.observe("reserved", "said 3 words in 2 h while he gamed", supports=["reserved", "busy"], now=NOW - 39 * DAY)
check("non-discriminating observation kept", r["ok"] and r["discriminates"] is False and r["status"]["observations"] == 1)
check("still undistinguished after it", "undistinguished" in r["status"]["verdict"])

# 3. discriminating observations lean but do NOT stabilise early (needs N, ratio and span)
for i in range(4):
    r = sh.observe("reserved", "spoke freely the moment he addressed me (%d)" % i, supports=["busy"], now=NOW - (38 - i) * DAY)
check("4 discriminating → leaning, not stabilised", r["status"]["stabilised"] is None and "leaning busy" in r["status"]["verdict"],
      r["status"]["verdict"])
# ADVERSARIAL: 5 discriminating within ONE day must not stabilise (span rule)
fresh()
sh.propose("reserved", "q", {"reserved": "a", "busy": "b"}, now=NOW - 2 * DAY)
for i in range(5):
    r = sh.observe("reserved", "x%d" % i, supports=["busy"], now=NOW - 1 * DAY + i * 600)
check("5 discriminating in one day does NOT stabilise (span)", r["status"]["stabilised"] is None, r["status"]["verdict"])
# ADVERSARIAL: 5 vs 4 must not stabilise (ratio rule)
fresh()
sh.propose("reserved", "q", {"reserved": "a", "busy": "b"}, now=NOW - 40 * DAY)
for i in range(5):
    sh.observe("reserved", "b%d" % i, supports=["busy"], now=NOW - (35 - i * 5) * DAY)
for i in range(4):
    r = sh.observe("reserved", "r%d" % i, supports=["reserved"], now=NOW - (34 - i * 5) * DAY)
check("5 vs 4 discriminating does NOT stabilise (ratio)", r["status"]["stabilised"] is None, r["status"]["verdict"])

# 4. proper evidence stabilises; the advisory wording changes; still advisory
fresh()
sh.propose("reserved", "Why do I speak so little some days?",
           {"reserved": "I am naturally reserved", "busy": "I speak less when Zeke is occupied"}, now=NOW - 60 * DAY)
for i in range(6):
    r = sh.observe("reserved", "spoke freely when addressed (%d)" % i, supports=["busy"], now=NOW - (50 - i * 6) * DAY)
sh.observe("reserved", "quiet while he gamed", supports=["reserved", "busy"], now=NOW - 10 * DAY)   # non-discriminating noise
st = sh.status("reserved", now=NOW)["traits"][0]
check("repeated discriminating evidence over weeks stabilises", st["stabilised"] == "busy", st["verdict"])
adv = sh.advisory(now=NOW)
check("advisory shows stabilised as still advisory", "stabilised over repeated evidence" in adv and "still advisory" in adv)

# 5. counter-evidence DESTABILISES: rival gathers 3 discriminating within the window
for i in range(3):
    r = sh.observe("reserved", "stayed silent even when he asked me directly (%d)" % i, supports=["reserved"], now=NOW - (3 - i) * DAY)
check("rival counter-evidence un-stabilises", r["status"]["stabilised"] is None, r["status"]["verdict"])
ev = open(sh.events_path(), encoding="utf-8").read()
check("events ledger records stabilise + destabilise", "\"stabilise\"" in ev and "\"destabilise\"" in ev)

# 6. observe validation: unknown trait / unknown hypothesis / empty text
fresh()
sh.propose("t", "q", {"a": "A", "b": "B"})
check("observe unknown trait errors", sh.observe("nope", "x", supports=["a"])["ok"] is False)
check("observe with no valid supports errors", sh.observe("t", "x", supports=["zzz"])["ok"] is False)
check("observe empty text errors", sh.observe("t", "", supports=["a"])["ok"] is False)
# extending a trait with a third rival keeps existing observations
sh.observe("t", "o1", supports=["a"])
r = sh.propose("t", "q", {"c": "C"})
check("extend adds a rival without losing observations", r["action"] == "extended" and len(r["trait"]["hypotheses"]) == 3
      and len(r["trait"]["observations"]) == 1)

# 7. the last self-review line comes from self_model.json as CONTEXT, never as a trait
fresh()
(Path(_tmp) / "state" / "self_model.json").write_text(json.dumps({"growth_note": "getting better at treating my instruments as fallible",
                                                                  "last_updated": "2026-09-24T18:54:33"}), encoding="utf-8")
sh.propose("t", "q", {"a": "A", "b": "B"})
adv = sh.advisory(now=NOW)
check("advisory includes last self-review as context", "last self-review (2026-09-24)" in adv)
check("self-review is not a trait", len(sh.status(now=NOW)["traits"]) == 1)

# 8. ADVERSARIAL: corrupt store → advisory '' (fail-open), writes refuse
fresh()
os.makedirs(os.path.dirname(sh.store_path()), exist_ok=True)
with open(sh.store_path(), "w", encoding="utf-8") as f:
    f.write("{nope")
check("corrupt store → advisory empty, no crash", sh.advisory(now=NOW) == "")
check("corrupt store → propose refuses", sh.propose("t", "q", {"a": "A", "b": "B"})["ok"] is False)
check("corrupt store → status reports", sh.status(now=NOW)["ok"] is False)
check("corrupt file untouched", open(sh.store_path(), encoding="utf-8").read() == "{nope")

# 9. no traits at all → advisory '' (the prompt simply lacks the block)
fresh()
check("empty store → advisory empty", sh.advisory(now=NOW) == "")

# 10. tool wrapper
try:
    from tools.system.self_model_tool import _self_model
    r = _self_model({"action": "propose", "trait": "x", "question": "q", "hypotheses": {"a": "A", "b": "B"}}, {})
    check("tool propose", r.get("ok"))
    r = _self_model({"action": "observe", "trait": "x", "text": "o", "supports": "a"}, {})
    check("tool observe accepts comma string", r.get("ok") and r.get("discriminates") is True)
    r = _self_model({"action": "propose", "trait": "y", "question": "q", "hypotheses": ["not", "a", "dict"]}, {})
    check("tool rejects non-dict hypotheses", r.get("ok") is False)
    r = _self_model({"action": "advisory"}, {})
    check("tool advisory", r.get("ok") and "NOT identity" in r.get("advisory", ""))
except Exception as e:
    check("tool wrapper importable", False, repr(e))

n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
print("\n%d/%d passed" % (len(RESULTS) - n_fail, len(RESULTS)))
sys.exit(1 if n_fail else 0)
