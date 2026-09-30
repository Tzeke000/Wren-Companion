"""Adversarial tests for brain/belief_lifecycle (Iris_fixes #8 + addition B).

The lifecycle must: never revise from ONE contradiction; count only INDEPENDENT contradictions;
keep previous/new/reason/evidence on every revision; honour the domain split (Zeke's word on his
own life revises at once, world claims never auto-revise); decay stale confidence on read; and
refuse to write over a corrupt store.

Run:  .venv\\Scripts\\python.exe scripts\\test_belief_lifecycle.py
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

_tmp = tempfile.mkdtemp(prefix="belief_")
os.environ["BELIEF_STORE_BASE"] = _tmp
(Path(_tmp) / "state").mkdir(parents=True, exist_ok=True)

from brain import belief_lifecycle as bl  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
NOW = time.time()


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def fresh() -> None:
    for p in (bl.store_path(), events_path := bl.events_path()):
        if os.path.exists(p):
            os.remove(p)


# 1. hold → created with the source's default confidence; re-hold same statement → confirmed, bounded
fresh()
r = bl.hold("vector docking", "Vector docks himself (firmware reflex works)", kind="world",
            source_kind="user_told", source_ref="zeke 09-08 eyewitness", now=NOW - 3600)
check("hold creates", r["ok"] and r["action"] == "created" and abs(r["belief"]["confidence"] - 0.85) < 1e-6, str(r.get("error")))
r = bl.hold("vector docking", "Vector docks himself (firmware reflex works)", source_kind="observation",
            source_ref="battery.json 09-30", now=NOW - 1800)
check("re-hold confirms and raises confidence (bounded)", r["action"] == "confirmed" and 0.85 < r["belief"]["confidence"] < 1.0,
      str(r["belief"]["confidence"]))

# 2. ONE contradiction → confidence down, still active, NOT revised
r = bl.challenge("vector docking", "he sat off-dock for 3 h", source_kind="observation", source_ref="battery.json 09-30 15:00",
                 note="single observation", now=NOW - 1000)
b = r["belief"]
check("one contradiction never revises", r["action"] == "noted" and b["status"] == "active"
      and b["statement"].startswith("Vector docks himself"), r["action"])
check("one contradiction lowers confidence", b["confidence"] < 0.85, str(b["confidence"]))

# 3. ADVERSARIAL: the SAME source repeated inside the gap counts ONCE
r = bl.challenge("vector docking", "still off-dock", source_kind="observation", source_ref="battery.json 09-30 15:00",
                 now=NOW - 900)
check("same source repeated within the gap is not independent", r["independent_challenges"] == 1 and r["belief"]["status"] == "active",
      str(r["independent_challenges"]))

# 4. a second INDEPENDENT contradiction → under_review, still not revised
r = bl.challenge("vector docking", "Zeke: 'he was on the floor all night'", source_kind="user_told", source_ref="zeke 09-30",
                 now=NOW - 800)
check("two independent contradictions → under_review", r["action"] == "under_review" and r["belief"]["status"] == "under_review")
check("still not revised by evidence alone", r["belief"]["statement"].startswith("Vector docks himself"))
rc = bl.reconsider(now=NOW)
check("reconsider lists it", any(v["subject"] == "vector docking" for v in rc["under_review"]))
rec = bl.recall("vector docking", now=NOW)
check("recall flags under_review + counts", rec["beliefs"][0]["status"] == "under_review" and rec["beliefs"][0]["independent_challenges"] == 2)

# 5. revise → history keeps previous/new/reason/evidence; self_revision + provenance written
r = bl.revise("vector docking", "Vector docks himself ONLY when no SDK session holds him", reason="two independent contradictions + Zeke's account",
              source_kind="derived", now=NOW - 700)
b = r["belief"]
check("revise keeps previous + reason + evidence", b["status"] == "active" and b["history"][-1]["previous"].startswith("Vector docks himself (")
      and b["history"][-1]["reason"].startswith("two independent") and len(b["history"][-1]["evidence"]) == 3, str(b["history"][-1].keys()))
check("revise resets challenges and re-seeds confidence from the source kind", b["evidence_against"] == [] and abs(b["confidence"] - 0.4) < 1e-6,
      str(b["confidence"]))
sr = Path(_tmp) / "state" / "self_revisions.jsonl"
pv = Path(_tmp) / "state" / "provenance.jsonl"
check("self_revision log written (unified, not replaced)", sr.exists() and "vector docking" in sr.read_text(encoding="utf-8"))
check("provenance records written for every source", pv.exists() and pv.read_text(encoding="utf-8").count("\n") >= 5)
check("events ledger records the lifecycle", os.path.exists(bl.events_path()) and "revise" in open(bl.events_path(), encoding="utf-8").read())

# 6. DOMAIN SPLIT: Zeke's word on his own life revises at once; a world claim from him does NOT
fresh()
bl.hold("zeke height", "Zeke is 1.80 m", kind="person", source_kind="derived", source_ref="pose estimate", now=NOW - 5000)
r = bl.challenge("zeke height", "Zeke: 'I'm 1.74'", source_kind="user_told", source_ref="zeke 08-xx", proposed="Zeke is 1.74 m",
                 decisive=True, note="his own body — his word wins", now=NOW - 4000)
check("decisive (his domain) revises immediately", r["action"] == "revised_decisive" and r["belief"]["statement"] == "Zeke is 1.74 m")
check("decisive revision still keeps history", r["belief"]["history"][-1]["previous"] == "Zeke is 1.80 m")
bl.hold("server gpu", "the V100 is dead", kind="world", source_kind="observation", source_ref="burnt cable 09-15", now=NOW - 3000)
r = bl.challenge("server gpu", "Zeke thinks the card is probably fine", source_kind="user_told", source_ref="zeke 09-30",
                 proposed="the V100 is fine", now=NOW - 2000)
check("non-decisive user_told on a technical claim does not auto-revise", r["action"] == "noted" and r["belief"]["statement"] == "the V100 is dead")
r = bl.challenge("server gpu", "decisive but nothing proposed", source_kind="user_told", source_ref="zeke 09-30 b", decisive=True, now=NOW - 1000)
check("decisive without a proposed statement only opens review", r["belief"]["status"] == "under_review" and r["belief"]["statement"] == "the V100 is dead")

# 7. hold() with a DIFFERENT statement on an existing subject is a challenge, not an overwrite
fresh()
bl.hold("wifi watcher", "the watcher is alive", source_kind="observation", source_ref="a", now=NOW - 100)
r = bl.hold("wifi watcher", "the watcher is dead", source_kind="observation", source_ref="b", now=NOW)
check("conflicting hold becomes a challenge, statement unchanged", r["action"] == "noted" and r["belief"]["statement"] == "the watcher is alive")

# 8. retire; recall excludes retired unless asked
r = bl.retire("wifi watcher", reason="replaced by the arbiter", now=NOW)
check("retire works", r["ok"] and r["belief"]["status"] == "retired")
check("recall hides retired", bl.recall("wifi watcher", now=NOW)["beliefs"] == [])
check("recall include_retired shows it", bl.recall("wifi watcher", include_retired=True, now=NOW)["beliefs"][0]["status"] == "retired")

# 9. stale decay on read + reconsider lists stale
fresh()
bl.hold("old thing", "something believed long ago", source_kind="observation", now=NOW - 200 * 86400)
v = bl.recall("old thing", now=NOW)["beliefs"][0]
check("effective confidence decays with age", v["effective_confidence"] < v["confidence"] and v["stale"], "%s < %s" % (v["effective_confidence"], v["confidence"]))
check("reconsider lists stale", any(s["subject"] == "old thing" for s in bl.reconsider(now=NOW)["stale"]))

# 10. token-overlap recall + contradictions_for
fresh()
bl.hold("runtime camera channel", "the runtime camera channel cannot rescue a dead host", source_kind="observation", source_ref="audit 09-30", now=NOW)
r = bl.recall("can the camera channel rescue the host?", now=NOW)
check("recall finds by token overlap", r["beliefs"] and r["beliefs"][0]["subject"] == "runtime camera channel")
check("contradictions_for returns confident beliefs only", bl.contradictions_for("camera channel rescue host", now=NOW))

# 11. ADVERSARIAL: corrupt store → recall reports, writes REFUSE
fresh()
os.makedirs(os.path.dirname(bl.store_path()), exist_ok=True)
with open(bl.store_path(), "w", encoding="utf-8") as f:
    f.write("{corrupt")
r = bl.recall("anything", now=NOW)
check("corrupt store → recall returns error, no crash", r["ok"] is False and "error" in r)
r = bl.hold("x", "y", now=NOW)
check("corrupt store → hold refuses to overwrite", r["ok"] is False and "corrupt" in r["error"])
check("corrupt file untouched", open(bl.store_path(), encoding="utf-8").read() == "{corrupt")

# 12. validation
fresh()
check("hold requires subject+statement", bl.hold("", "x")["ok"] is False and bl.hold("x", "")["ok"] is False)
check("bad kind rejected", bl.hold("a", "b", kind="vibes")["ok"] is False)
check("challenge on unknown subject errors", bl.challenge("nope", "e")["ok"] is False)
check("revise requires a reason", bl.hold("a", "b")["ok"] and bl.revise("a", "c", reason="")["ok"] is False)

# 13. opinions.py: the generic fallback must NOT persist when the LLM fails; topic freq reads the transcript
try:
    from brain import opinions, iris_llm
    base = Path(_tmp)
    (base / "state").mkdir(exist_ok=True)
    with open(base / "state" / "transcript.jsonl", "w", encoding="utf-8") as f:
        for i in range(4):
            f.write(json.dumps({"role": "user", "content": "let's talk about motorcycles again %d" % i}) + "\n")
    orig = iris_llm.ask_iris
    iris_llm.ask_iris = lambda *a, **k: None   # LLM failure
    try:
        r = opinions.form_opinion("motorcycles", "context", {"BASE_DIR": str(base)})
    finally:
        iris_llm.ask_iris = orig
    check("form_opinion: LLM failure persists NO generic fallback", r is None and not (base / "state" / "opinions.json").exists())
    check("form_opinion: topic frequency reads transcript.jsonl", opinions._topic_freq(base, "motorcycles") >= 3)
    iris_llm.ask_iris = lambda *a, **k: json.dumps({"stance": "they are worth the risk if you train", "confidence": 0.7, "reasoning": "r"})
    try:
        r = opinions.form_opinion("motorcycles", "context", {"BASE_DIR": str(base)})
    finally:
        iris_llm.ask_iris = orig
    check("form_opinion: a real opinion persists AND registers as a belief", r is not None
          and bl.recall("motorcycles", kind="opinion", now=NOW)["beliefs"] != [])
except Exception as e:
    check("opinions integration", False, repr(e))

# 14. honest_disagreement Track A reads beliefs (the file shape it read never existed)
try:
    from brain import honest_disagreement as hd
    ok, basis = hd._has_opinion_basis_contradicting({"BASE_DIR": str(Path(_tmp))}, "Nobody wants motorcycles, that idea is bad")
    check("honest_disagreement finds the opinion via beliefs", ok and "motorcycles" in basis.lower(), basis)
except Exception as e:
    check("honest_disagreement integration", False, repr(e))

# 15. tool wrapper
try:
    from tools.system.belief_tool import _belief
    t = _belief({"action": "recall", "query": "motorcycles"}, {})
    check("tool recall", t.get("ok") and t.get("beliefs"))
    t = _belief({"action": "challenge", "subject": "nope", "evidence": "x"}, {})
    check("tool surfaces errors", t.get("ok") is False)
    t = _belief({}, {})
    check("tool explain", t.get("ok") and "lifecycle" in t)
except Exception as e:
    check("tool wrapper importable", False, repr(e))

n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
print("\n%d/%d passed" % (len(RESULTS) - n_fail, len(RESULTS)))
sys.exit(1 if n_fail else 0)
