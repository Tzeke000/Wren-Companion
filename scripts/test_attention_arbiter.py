"""Adversarial tests for brain/attention_arbiter (Iris_fixes #6).

The arbiter's job: a fallback owner must stay quiet when the primary already handled the
situation, and must NEVER stay quiet when nobody did. So the tests mostly try to make it
swallow a wake that has no other owner.

Run:  .venv\\Scripts\\python.exe scripts\\test_attention_arbiter.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix="attention_arbiter_")
os.environ["ATTENTION_ARBITER_LEDGER"] = os.path.join(_tmp, "situations.jsonl")
os.environ["WAKE_SALIENCE_LEDGER"] = os.path.join(_tmp, "wake_verdicts.jsonl")

from brain import attention_arbiter as arb  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def fresh() -> None:
    p = arb.ledger_path()
    if os.path.exists(p):
        os.remove(p)
    arb._CACHE["mtime"] = -1.0


NOW = time.time()

# 1. primary always proceeds (its own gates apply elsewhere)
fresh()
d = arb.decide("zeke_presence", "host-eyes", now=NOW)
check("primary wakes", d["wake"] and d["role"] == "primary", d["reason"])

# 2. wifi arrival 2 min after the camera named him → DUPLICATE, no wake
fresh()
arb.claim("zeke_presence", "host-eyes", detail="face_appeared zeke 0.74", wake=True, now=NOW - 120)
d = arb.decide("zeke_presence", "wifi", now=NOW)
check("wifi after camera = duplicate", (not d["wake"]) and d["role"] == "duplicate", d["reason"])
check("duplicate names who covered it", d.get("covered_by", {}).get("source") == "host-eyes")

# 3. ADVERSARIAL: the camera claim is OUTSIDE the window → wifi must wake
fresh()
arb.claim("zeke_presence", "host-eyes", wake=True, now=NOW - 1200)
d = arb.decide("zeke_presence", "wifi", now=NOW)
check("stale camera claim does not cover", d["wake"] and d["role"] == "fallback", d["reason"])

# 4. ADVERSARIAL: the camera SAW him but did NOT wake (suppressed) → does not cover; wifi wakes
fresh()
arb.claim("zeke_presence", "host-eyes", detail="seen, no wake: salience", wake=False, now=NOW - 60)
d = arb.decide("zeke_presence", "wifi", now=NOW)
check("a non-waking primary claim does not cover", d["wake"], d["reason"])

# 5. ADVERSARIAL: empty ledger, nobody home → wifi wakes (fail-open)
fresh()
d = arb.decide("zeke_presence", "wifi", now=NOW)
check("no claims at all → fallback wakes", d["wake"] and d["role"] == "fallback", d["reason"])

# 6. runtime camera with the host eyes ALIVE → standing by (host will decide)
fresh()
g = {"_host_eyes_poll_ts": NOW - 3.0}
alive, age = arb.host_eyes_alive(g, now=NOW)
check("host_eyes_alive reads the stamp", alive is True and 2.9 < age < 3.1, "age=%s" % age)
d = arb.decide("zeke_presence", "runtime-camera", primary_alive=alive, now=NOW)
check("runtime camera stands by while host eyes alive", (not d["wake"]) and d["role"] == "standing_by", d["reason"])

# 7. ADVERSARIAL: host eyes silent for 60 s → runtime camera is the fallback and WAKES
g = {"_host_eyes_poll_ts": NOW - 60.0}
alive, age = arb.host_eyes_alive(g, now=NOW)
d = arb.decide("zeke_presence", "runtime-camera", primary_alive=alive, now=NOW)
check("host eyes dead → runtime camera wakes", d["wake"] and d["role"] == "fallback", d["reason"])
# ...and with NO stamp at all (unknown) it also wakes
alive, age = arb.host_eyes_alive({}, now=NOW)
d = arb.decide("zeke_presence", "runtime-camera", primary_alive=alive, now=NOW)
check("unknown host liveness → wakes", d["wake"] and alive is None)
check("host_eyes_alive outside the runtime = unknown", arb.host_eyes_alive(None) == (None, None))

# 8. unknown_capture 30 s after face_appeared|unknown woke me → duplicate; 10 min later → wakes
fresh()
arb.claim("unknown_person", "host-eyes", detail="face_appeared unknown", wake=True, now=NOW - 30)
d = arb.decide("unknown_person", "unknown_capture", now=NOW)
check("unknown_capture right after the stranger wake = duplicate", not d["wake"], d["reason"])
fresh()
arb.claim("unknown_person", "host-eyes", wake=True, now=NOW - 600)
d = arb.decide("unknown_person", "unknown_capture", now=NOW)
check("unknown_capture 10 min later wakes", d["wake"], d["reason"])

# 9. ADVERSARIAL: situations must not cover each other (an arrival claim must not mute a departure)
fresh()
arb.claim("zeke_presence", "host-eyes", wake=True, now=NOW - 30)
d = arb.decide("zeke_absence", "host-eyes", now=NOW)
check("camera 'lost him' is only a fallback for absence (wakes when wifi hasn't spoken)",
      d["role"] == "fallback" and d["wake"], d["reason"])
d = arb.decide("zeke_absence", "wifi", now=NOW)
check("departure is owned by wifi, never muted by an arrival", d["wake"] and d["role"] == "primary", d["reason"])
d = arb.decide("zeke_absence", "host-eyes", now=NOW + 1)
check("camera 'lost him' after wifi said he left = duplicate", (not d["wake"]) and d["role"] == "duplicate", d["reason"])

# 10. ADVERSARIAL: unlisted situation / unlisted source → fail-open, and RECORDED so the table can grow
fresh()
d = arb.decide("vector_low_battery", "body-daemon", now=NOW)
check("unknown situation fails open", d["wake"] and d["role"] == "unowned", d["reason"])
d = arb.decide("zeke_presence", "some-new-sensor", now=NOW)
check("unlisted source fails open", d["wake"] and d["role"] == "unlisted", d["reason"])
check("fail-open decisions are still ledgered", len(arb.recent(10, now=NOW)) == 2)

# 11. ADVERSARIAL: corrupt ledger lines are skipped; a wholly unreadable ledger fails OPEN
fresh()
arb.claim("zeke_presence", "host-eyes", wake=True, now=NOW - 10)
with open(arb.ledger_path(), "a", encoding="utf-8") as f:
    f.write("garbage{\n")
arb._CACHE["mtime"] = -1.0
d = arb.decide("zeke_presence", "wifi", now=NOW)
check("corrupt line skipped, good claim still covers", not d["wake"], d["reason"])
os.environ["ATTENTION_ARBITER_LEDGER"] = os.path.join(_tmp, "adir")
os.makedirs(os.environ["ATTENTION_ARBITER_LEDGER"], exist_ok=True)
arb._CACHE["mtime"] = -1.0
d = arb.decide("zeke_presence", "wifi", now=NOW)
check("unreadable ledger fails OPEN", d["wake"], d["reason"])
os.environ["ATTENTION_ARBITER_LEDGER"] = os.path.join(_tmp, "situations.jsonl")
arb._CACHE["mtime"] = -1.0

# 12. dry-run decide does not write
fresh()
arb.decide("zeke_presence", "wifi", now=NOW, record=False)
check("record=False leaves no row", not os.path.exists(arb.ledger_path()) or os.path.getsize(arb.ledger_path()) == 0)

# 13. explain() answers all seven questions
fresh()
arb.claim("zeke_presence", "host-eyes", wake=True, now=NOW - 20)
arb.decide("zeke_presence", "wifi", now=NOW - 5)
from brain import wake_salience as ws  # noqa: E402
ws.record("face_appeared|zeke|steady|wifi=present", "nothing", note="sat back down", ts=NOW - 8)
ex = arb.explain(g={"_host_eyes_poll_ts": NOW - 2}, now=NOW)
keys = ["what_wants_attention", "how_important", "why", "needs_cognition_now", "can_it_wait",
        "already_handled_by", "chose_not_to_act", "owners"]
check("explain has the seven questions", all(k in ex for k in keys), str([k for k in keys if k not in ex]))
check("explain: duplicate shows as can_it_wait", any(w["role"] == "duplicate" for w in ex["can_it_wait"]))
check("explain: already_handled_by names the primary", ex["already_handled_by"] and ex["already_handled_by"][0]["by"] == "wifi")
check("explain: chose_not_to_act reads the salience ledger", ex["chose_not_to_act"] and ex["chose_not_to_act"][0]["verdict"] == "nothing")
check("explain: host eyes alive from the stamp", ex["host_eyes_alive"] is True)

# 14. the tool wrapper
try:
    from tools.system.attention_arbiter_tool import _attention_arbiter
    r = _attention_arbiter({"action": "decide", "situation": "zeke_presence", "source": "wifi"}, {})
    check("tool decide is a dry run", r.get("ok") and r.get("dry_run") and r.get("role") == "duplicate", str(r.get("reason")))
    r = _attention_arbiter({"action": "decide"}, {})
    check("tool decide without args errors", r.get("ok") is False)
    r = _attention_arbiter({}, {"_host_eyes_poll_ts": NOW - 1})
    check("tool explain works", r.get("ok") and "owners" in r)
except Exception as e:
    check("tool wrapper importable", False, repr(e))

n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
print("\n%d/%d passed" % (len(RESULTS) - n_fail, len(RESULTS)))
sys.exit(1 if n_fail else 0)
