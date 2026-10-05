"""Offline proof for brain/skill_library.py (temp state; lexical similarity forced so the
test is deterministic and doesn't load the ONNX model).

Rules under test:
  * a recipe citing a ledger SUCCESS is verified; citing a failure is refused
  * provisional recipes are hidden from find() unless asked
  * a successful use promotes provisional -> verified; two failed uses -> needs_revision
  * re-adding a name makes v2 and keeps v1
  * find() ranks by description relevance; act open hands back matching recipes

Run: .venv/Scripts/python.exe scripts/test_skill_library.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from brain import action_ledger as al  # noqa: E402
from brain import skill_library as sl  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {info}")


with tempfile.TemporaryDirectory(dir=str(ROOT / "scratch" / "tmp")) as td:
    td = Path(td)
    al._DIR, al.ACTIONS_PATH, al.LESSONS_PATH = td, td / "a.json", td / "l.jsonl"
    sl._DIR, sl.LIB_PATH, sl.EMB_PATH = td, td / "lib.json", td / "emb.json"
    sl._embedder_failed = True  # deterministic lexical similarity

    ok = al.open_action(kind="vector", intent="restart vic-cloud", expected="connected")
    al.close_action(ok["id"], outcome="success", evidence="daemon: connected - nerves online")
    bad = al.open_action(kind="vector", intent="restart daemon alone", expected="connected")
    al.close_action(bad["id"], outcome="failure", evidence="unreliable event stream x5")

    print("== 1. the ledger is the critic ==")
    r = sl.add("vector_vic_restart",
               "recover a wedged Vector SDK (event stream unreliable, deadline exceeded): restart vic-cloud and vic-switchboard",
               ["ssh root@robot systemctl restart vic-switchboard vic-cloud", "wait for daemon retry"],
               verified_by=ok["id"])
    check("cited success -> verified", r["status"] == "verified", str(r))
    try:
        sl.add("bad", "restart the daemon alone", ["kill daemon"], verified_by=bad["id"])
        refused = False
    except ValueError:
        refused = True
    check("cited failure -> refused", refused)

    print("== 2. provisional recipes are hidden ==")
    sl.add("git_push_verify", "confirm commits reached the remote: git rev-list count vs upstream",
           ["git rev-list --left-right --count HEAD...@{u}"])
    check("provisional hidden from find", not sl.find("confirm my git commits reached the remote"))
    check("visible when asked", bool(sl.find("confirm my git commits reached the remote", include_provisional=True)))

    print("== 3. use promotes / demotes ==")
    u = al.open_action(kind="git_push", intent="push", expected="0 ahead")
    al.close_action(u["id"], outcome="success", evidence="HEAD 0 ahead")
    check("successful use promotes", sl.record_use("git_push_verify", u["id"])["status"] == "verified")
    for _ in range(2):
        f = al.open_action(kind="git_push", intent="push again", expected="0 ahead")
        al.close_action(f["id"], outcome="failure", evidence="still 2 ahead")
        res = sl.record_use("git_push_verify", f["id"])
    check("two failed uses -> needs_revision", res["status"] == "needs_revision", str(res))
    still_open = al.open_action(kind="x", intent="y", expected="z")
    try:
        sl.record_use("git_push_verify", still_open["id"])
        guarded = False
    except ValueError:
        guarded = True
    check("can't record a use of an open action", guarded)

    print("== 4. versions ==")
    r2 = sl.add("vector_vic_restart", "recover a wedged Vector SDK: restart vic-switchboard + vic-cloud, THEN the daemon",
                ["ssh ... restart vic-switchboard vic-cloud", "restart the inhabit daemon"], verified_by=ok["id"])
    g = sl.get("vector_vic_restart")
    check("v2 created, v1 kept", r2["version"] == 2 and len(g["versions"]) == 2 and g["current"]["v"] == 2)

    print("== 5. retrieval ranks by relevance ==")
    sl.add("ptz_resync", "fix stale PTZ head position registers: absolute move away and back home, then check_home",
           ["ptz_predict action=resync"], verified_by=ok["id"])
    hits = sl.find("vector robot sdk event stream wedged")
    check("vector query -> vector recipe first", hits and hits[0]["name"] == "vector_vic_restart",
          str([(h["name"], h["score"]) for h in hits]))
    hits = sl.find("camera head registers stale after jog")
    check("ptz query -> ptz recipe first", hits and hits[0]["name"] == "ptz_resync",
          str([(h["name"], h["score"]) for h in hits]))

    print("== 6. act open hands back matching recipes ==")
    o = al.open_action(kind="vector_recover", intent="Vector event stream wedged again, recover the sdk",
                       expected="nerves online")
    check("recipes returned on open", any(r["name"] == "vector_vic_restart" for r in o.get("recipes", [])),
          str(o.get("recipes")))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
