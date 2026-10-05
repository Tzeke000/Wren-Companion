"""Offline proof for brain/action_ledger.py (temp state dir; no live files touched).

The rules under test are the point of the module:
  * no expected result -> no action
  * success without evidence is REFUSED (intent is not completion)
  * a failure's lesson comes back verbatim the next time that kind of action opens
  * the same intent failing twice raises a loop warning (retry budget)
  * an open action past its deadline shows up as overdue
  * verifiers are external probes: a passing port closes success, a dead one stays
    open until the deadline, then closes failure

Run: .venv/Scripts/python.exe scripts/test_action_ledger.py
"""
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import action_ledger as al  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {info}")


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc as e:
        return str(e)
    return None


with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent.parent / "scratch" / "tmp")) as td:
    al._DIR = Path(td)
    al.ACTIONS_PATH = Path(td) / "a.json"
    al.LESSONS_PATH = Path(td) / "l.jsonl"

    print("== 1. no expected result, no action ==")
    check("open without expected refused",
          raises(lambda: al.open_action(kind="x", intent="do x", expected="")) is not None)

    print("== 2. success needs evidence ==")
    r = al.open_action(kind="fix", intent="fix the orb listener", expected=":5876 answers 200")
    msg = raises(lambda: al.close_action(r["id"], outcome="success"))
    check("success w/o evidence refused", msg is not None, msg or "")
    check("still open after refusal", al.list_actions(status="open")[0]["id"] == r["id"])
    out = al.close_action(r["id"], outcome="success", evidence="curl -> 200")
    check("success with evidence closes", out["ok"] and out["row"]["outcome"] == "success")

    print("== 3. abandoned needs a reason ==")
    r = al.open_action(kind="fix", intent="other", expected="y")
    check("abandoned w/o reason refused", raises(lambda: al.close_action(r["id"], outcome="abandoned")) is not None)
    al.close_action(r["id"], outcome="abandoned", reason="Zeke changed plans")

    print("== 4. a failure's lesson comes back next time (Reflexion injection) ==")
    r = al.open_action(kind="vector_reconnect", intent="reconnect Vector", expected="daemon logs connected")
    al.close_action(r["id"], outcome="failure", evidence="VectorNotFoundException",
                    lesson={"diagnosis": "DHCP moved him; the IP in sdk_config was stale",
                            "next_time": "ARP-sweep for 00-0a-f5 before calling him dark"})
    r2 = al.open_action(kind="vector_reconnect", intent="reconnect Vector again", expected="connected")
    check("lesson returned on open", len(r2["lessons"]) == 1 and "ARP" in r2["lessons"][0]["next_time"],
          str(r2["lessons"]))
    other = al.open_action(kind="unrelated", intent="z", expected="z")
    check("lessons are per kind", other["lessons"] == [])
    for i in range(4):
        x = al.open_action(kind="k3", intent=f"t{i}", expected="e")
        al.close_action(x["id"], outcome="failure", evidence="no",
                        lesson={"diagnosis": f"d{i}", "next_time": f"n{i}"})
    check("only the last 3 lessons (omega=3)",
          [l["diagnosis"] for l in al.lessons_for("k3")] == ["d1", "d2", "d3"])

    print("== 5. loop-breaker: same intent failing twice ==")
    for _ in range(2):
        x = al.open_action(kind="loop", intent="Restart the THING", expected="up")
        al.close_action(x["id"], outcome="failure", evidence="still down")
    x = al.open_action(kind="loop", intent="restart the thing", expected="up")
    check("loop warning raised", bool(x.get("loop_warning")), x.get("loop_warning", ""))

    print("== 6. unreflected queue + reflect + ack ==")
    by = al.unreflected_by_kind()
    check("loop failures listed as unreflected", by.get("loop", {}).get("count") == 2, str(by.get("loop")))
    rows = al.unreflected()
    lid = al.reflect(rows[0]["id"], diagnosis="restarting a configured-off service", next_time="read the flag")["lesson_id"]
    al.ack_covered("loop", lid)
    check("ack clears the kind", "loop" not in al.unreflected_by_kind())

    print("== 7. overdue = intent that never became a result ==")
    o = al.open_action(kind="promise", intent="tell Zeke X", expected="message sent", deadline_s=0.01)
    time.sleep(0.05)
    check("past-deadline open action is overdue", any(r["id"] == o["id"] for r in al.overdue()))

    print("== 8. external verifiers ==")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    v = al.open_action(kind="svc", intent="bring up test port", expected="port listens",
                       verify={"type": "port", "port": port})
    res = al.verify(v["id"])
    check("live port verifies -> success", res.get("row", {}).get("outcome") == "success", str(res.get("verify")))
    srv.close()
    v = al.open_action(kind="svc", intent="dead port", expected="listens",
                       verify={"type": "port", "port": port}, deadline_s=3600)
    res = al.verify(v["id"])
    check("dead port before deadline stays open", res.get("still_open") is True, str(res.get("verify")))
    v = al.open_action(kind="svc", intent="dead port late", expected="listens",
                       verify={"type": "port", "port": port}, deadline_s=0.01)
    time.sleep(0.05)
    res = al.verify(v["id"])
    check("dead port after deadline -> failure", res.get("row", {}).get("outcome") == "failure")
    f = Path(td) / "made.txt"
    v = al.open_action(kind="file", intent="write file", expected="file has OK",
                       verify={"type": "file", "path": str(f), "contains": "OK"}, deadline_s=0.01)
    f.write_text("all OK here")
    res = al.verify(v["id"])
    check("file verifier passes", res.get("row", {}).get("outcome") == "success", str(res.get("verify")))
    v = al.open_action(kind="x", intent="no verifier", expected="?")
    res = al.verify(v["id"])
    check("no verifier = can't judge, stays open", res.get("still_open") is True)

    print("== 9. stats ==")
    s = al.stats()
    check("stats count kinds", s["fix"]["success"] == 1 and s["fix"]["abandoned"] == 1, str(s.get("fix")))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
