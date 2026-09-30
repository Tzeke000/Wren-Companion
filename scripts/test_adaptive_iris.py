"""Adversarial tests for brain/adaptive_iris (Iris_fixes #7).

The loop must: move weights in the RIGHT direction from evidence, stay bounded, refuse to
learn from thin or corrupt evidence, never touch anything outside state/learning, and every
consumer must be a no-op (unchanged behaviour) when there is no evidence.

Run:  .venv\\Scripts\\python.exe scripts\\test_adaptive_iris.py
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

# Sandbox: a fake repo root with its own state/ so the real ledgers + prefs are untouched.
_tmp = tempfile.mkdtemp(prefix="adaptive_iris_")
STATE = Path(_tmp) / "state"
(STATE / "learning").mkdir(parents=True)

from brain import adaptive_learning as al  # noqa: E402
from brain import adaptive_iris as ai  # noqa: E402

# redirect the prefs file into the sandbox (module constants, patched for the test)
al.LEARNING_DIR = STATE / "learning"
al.PREFERENCES_PATH = al.LEARNING_DIR / "adaptive_preferences.json"

RESULTS: list[tuple[str, bool, str]] = []
NOW = time.time()


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def wipe() -> None:
    for p in STATE.glob("*.jsonl"):
        p.unlink()
    for p in STATE.glob("*.json"):
        p.unlink()
    if al.PREFERENCES_PATH.exists():
        al.PREFERENCES_PATH.unlink()


def jsonl(name: str, rows: list[dict]) -> None:
    with open(STATE / name, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# 1. no evidence at all → nothing learned, weights stay neutral, nothing written
wipe()
r = ai.learn(_tmp, now=NOW)
check("no evidence → no update", r["updated"] == {} and not al.PREFERENCES_PATH.exists(), str(r["skipped"].get(ai.FOCUS["proactive"])))
check("no evidence → hints silent", ai.hints(al.load_preferences()) == [])
check("no evidence → cooldown scale 1.0", ai.cooldown_scale(al.load_preferences()) == 1.0)

# 2. proactive: 8 'nothing' + 2 'acted' → usefulness falls toward 0.2, silence rises toward 0.8
wipe()
jsonl("wake_verdicts.jsonl", [{"ts": NOW - 100 - i, "sig": "s", "verdict": "nothing"} for i in range(8)]
      + [{"ts": NOW - 50, "sig": "s", "verdict": "acted"}, {"ts": NOW - 40, "sig": "s", "verdict": "acted"}]
      + [{"ts": NOW - 30, "sig": "s", "verdict": "auto_suppressed"}] * 5   # not evidence
      + [{"ts": NOW - 20, "sig": "s", "verdict": "unrecorded"}] * 5)      # not evidence
r = ai.learn(_tmp, now=NOW)
u = r["updated"][ai.FOCUS["proactive"]]
check("proactive target = acted/(acted+nothing)", abs(u["target"] - 0.2) < 1e-6, str(u))
check("proactive moved DOWN from neutral, bounded by alpha", 0.5 - 0.2 * 0.3 - 1e-6 <= u["new"] < 0.5, str(u))
check("non-explicit rows ignored as evidence", u["n"] == 10)
s = r["updated"][ai.FOCUS["silence"]]
check("silence is the inverse", abs(s["target"] - 0.8) < 1e-6 and s["new"] > 0.5, str(s))
check("prefs file written under state/learning only", al.PREFERENCES_PATH.exists()
      and not (STATE / "adaptive_preferences.json").exists())
# ...and repeated runs converge monotonically toward the target without overshoot
last = u["new"]
for _ in range(20):
    r = ai.learn(_tmp, now=NOW)
    cur = r["updated"][ai.FOCUS["proactive"]]["new"]
    if cur > last + 1e-9 or cur < 0.2 - 1e-9:
        check("EWMA converges monotonically without overshoot", False, "%s -> %s" % (last, cur))
        break
    last = cur
else:
    check("EWMA converges monotonically without overshoot", abs(last - 0.2) < 0.01, "final %.3f" % last)

# 3. consumers react to the learned proactive weight
p = al.load_preferences()
check("cooldown scale grows when proactive usefulness is low", 1.5 < ai.cooldown_scale(p) <= 3.0, "%.2f" % ai.cooldown_scale(p))
h = ai.hints(p)
check("hint says don't volunteer", any("don't volunteer" in x for x in h), str(h))
# ADVERSARIAL: the scale is bounded even for absurd weights
check("cooldown scale bounded at 3.0", ai.cooldown_scale({"weights": {ai.FOCUS["proactive"]: 0.0}, "evidence_counts": {ai.FOCUS["proactive"]: 99}}) == 3.0)
check("cooldown scale bounded at 0.5", ai.cooldown_scale({"weights": {ai.FOCUS["proactive"]: 1.0}, "evidence_counts": {ai.FOCUS["proactive"]: 99}}) == 0.5)

# 4. pacing: 6 of 10 voice replies cut by a barge-in → pacing target 0 → weight falls; hint says 1–2 sentences
wipe()
rows = []
for i in range(10):
    rows.append({"ts": NOW - 1000 + i * 10, "role": "assistant", "modality": "voice", "content": "blah " * 30})
    if i < 6:
        rows.append({"ts": NOW - 995 + i * 10, "role": "user", "modality": "voice",
                     "content": "[barge-in] [cut mid-sentence at: \"Right.\" | 2 sentence(s) fully heard, 3 never played] hey"})
    else:
        rows.append({"ts": NOW - 995 + i * 10, "role": "user", "modality": "voice",
                     "content": "[barge-in] [reply had already finished playing — all 4 sentence(s) heard] ok"})
jsonl("transcript.jsonl", rows)
r = ai.learn(_tmp, now=NOW)
u = r["updated"][ai.FOCUS["pacing"]]
check("pacing counts only CUT barge-ins", r["evidence"]["pacing"]["cut"] == 6 and r["evidence"]["pacing"]["replies"] == 10)
check("pacing target 0 when >=50% cut", u["target"] == 0.0 and u["new"] < 0.5, str(u))
for _ in range(10):
    ai.learn(_tmp, now=NOW)
h = ai.hints(al.load_preferences())
check("pacing hint → 1–2 sentences", any("1–2 sentences" in x for x in h), str(h))
# ADVERSARIAL: 'all N heard' and 'nothing spoken' barge-ins must NOT count as cuts
wipe()
jsonl("transcript.jsonl", [{"ts": NOW, "role": "assistant", "modality": "voice", "content": "x"}] * 6
      + [{"ts": NOW, "role": "user", "modality": "voice", "content": "[barge-in] [barge landed before the reply started playing — nothing had been spoken] hi"}] * 6
      + [{"ts": NOW, "role": "user", "modality": "voice", "content": "[barge-in] [reply had already finished playing — all 3 sentence(s) heard] hi"}] * 6)
r = ai.learn(_tmp, now=NOW)
check("non-cut barge-ins are not pacing evidence", r["evidence"]["pacing"]["cut"] == 0 and r["updated"][ai.FOCUS["pacing"]]["target"] == 1.0)
# ADVERSARIAL: chat rows never count as voice pacing
wipe()
jsonl("transcript.jsonl", [{"ts": NOW, "role": "assistant", "modality": "chat", "content": "x"}] * 20)
r = ai.learn(_tmp, now=NOW)
check("chat replies are not voice pacing evidence", ai.FOCUS["pacing"] in r["skipped"])

# 5. memory: 6 accessed, 3 retrieved again → target 0.5; rerank only acts with evidence
wipe()
meta = {("m%d" % i): {"access_count": (3 if i < 3 else 1), "importance_boost": 0.3 if i < 3 else 0.0} for i in range(6)}
(STATE / "iris_memory_meta.json").write_text(json.dumps(meta), encoding="utf-8")
r = ai.learn(_tmp, now=NOW)
u = r["updated"][ai.FOCUS["memory"]]
check("memory target = retrieved-again share", abs(u["target"] - 0.5) < 1e-6, str(u))
rows = [{"id": "a", "distance": 0.10, "importance": 0.1, "ts": NOW}, {"id": "b", "distance": 0.30, "importance": 0.9, "ts": NOW}]
imp = lambda mid, base, ts: 0.05 if mid == "a" else 0.95  # b is a much-used memory
check("rerank untouched without evidence", ai.rerank_memories(rows, {"weights": {}, "evidence_counts": {}}, importance_fn=imp)[0]["id"] == "a")
strong = {"weights": {ai.FOCUS["memory"]: 1.0}, "evidence_counts": {ai.FOCUS["memory"]: 50}}
check("rerank with strong evidence lifts the well-used memory", ai.rerank_memories(rows, strong, importance_fn=imp)[0]["id"] == "b")
weak = {"weights": {ai.FOCUS["memory"]: 0.1}, "evidence_counts": {ai.FOCUS["memory"]: 50}}
check("rerank with weak usefulness keeps distance order", ai.rerank_memories(rows, weak, importance_fn=imp)[0]["id"] == "a")
def boom(*a): raise RuntimeError("meta unreadable")
check("rerank fails open on a broken importance fn", [r["id"] for r in ai.rerank_memories(rows, strong, importance_fn=boom)] == ["a", "b"])
check("rerank never drops or duplicates rows", sorted(r["id"] for r in ai.rerank_memories(rows, strong, importance_fn=imp)) == ["a", "b"])

# 6. curiosity: 6 browses, 2 real learnings + 4 placeholders → target 2/6
wipe()
jsonl("leisure_log.jsonl", [{"ts": NOW - 10 - i, "activity": "browse_curiosity_topic"} for i in range(6)])
jsonl("learning_log.jsonl", [{"ts": NOW - 5, "knowledge": "Steam is a game store"}, {"ts": NOW - 4, "knowledge": "Real thing"}]
      + [{"ts": NOW - 3, "knowledge": "I couldn't research 'x' deeply this time"}] * 4)
r = ai.learn(_tmp, now=NOW)
u = r["updated"][ai.FOCUS["curiosity"]]
check("curiosity excludes placeholder learnings", abs(u["target"] - (2 / 6)) < 1e-3 and r["evidence"]["curiosity"]["real_learnings"] == 2, str(u))
# ADVERSARIAL: evidence older than the lookback is ignored
wipe()
jsonl("wake_verdicts.jsonl", [{"ts": NOW - ai.LOOKBACK_S - 10, "sig": "s", "verdict": "nothing"}] * 20)
r = ai.learn(_tmp, now=NOW)
check("stale evidence ignored", ai.FOCUS["proactive"] in r["skipped"])

# 7. ADVERSARIAL: corrupt evidence files → no crash, no update
wipe()
(STATE / "wake_verdicts.jsonl").write_text("{{{not json\n" * 10, encoding="utf-8")
(STATE / "iris_memory_meta.json").write_text("[]", encoding="utf-8")
(STATE / "transcript.jsonl").write_text("\x00\x00garbage\n", encoding="utf-8")
r = ai.learn(_tmp, now=NOW)
check("corrupt evidence → no update, no crash", r["ok"] and r["updated"] == {})

# 8. bounds + schema: weights always in [0,1]; the file keeps the never_writes_ava_core flag; dry_run writes nothing
wipe()
jsonl("wake_verdicts.jsonl", [{"ts": NOW - i, "sig": "s", "verdict": "acted"} for i in range(50)])
r = ai.learn(_tmp, now=NOW, dry_run=True)
check("dry_run learns but does not write", r["updated"] and not al.PREFERENCES_PATH.exists())
for _ in range(40):
    r = ai.learn(_tmp, now=NOW)
w = r["prefs"]["weights"][ai.FOCUS["proactive"]]
check("weight bounded in [0,1] after many all-acted runs", 0.0 <= w <= 1.0 and w > 0.95, "%.3f" % w)
saved = json.loads(al.PREFERENCES_PATH.read_text(encoding="utf-8"))
check("saved schema keeps the safety flags", saved.get("never_writes_ava_core") is True and saved.get("schema") == "phase31_adaptive_v1")
check("cooldown scale shrinks when wakes are useful", ai.cooldown_scale(r["prefs"]) < 1.0)

# 9. the tool wrapper
try:
    from tools.system.adaptive_learning_tool import _adaptive_learning
    t = _adaptive_learning({"action": "learn", "dry_run": True}, {})
    check("tool learn dry_run ok", t.get("ok") and t.get("dry_run") is True and "prefs" not in t)
    t = _adaptive_learning({}, {})
    check("tool status lists consumers", t.get("ok") and "consumers" in t and "hints" in t)
except Exception as e:
    check("tool wrapper importable", False, repr(e))

n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
print("\n%d/%d passed" % (len(RESULTS) - n_fail, len(RESULTS)))
sys.exit(1 if n_fail else 0)
