"""Offline proof for brain/resilience_supervisor.py — no robot, no port, no Discord.
Run: .venv/Scripts/python.exe scripts/test_resilience.py"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from brain import resilience_supervisor as rs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {info}")


now = 1_000_000.0
base = {"nerves_off": False, "daemon_pids": [1], "daemon_started_ts": now - 3600,
        "senses_mtime": now - 5, "robot_pings": True}

print("== A. vector ==")
check("fresh senses -> none", rs.decide_vector(dict(base), {}, now)["action"] == "none")
check("nerves deliberately off -> none, even if stale",
      rs.decide_vector(dict(base, nerves_off=True, senses_mtime=0), {}, now)["action"] == "none")
check("young daemon (connect grace) -> none",
      rs.decide_vector(dict(base, daemon_started_ts=now - 60, senses_mtime=0), {}, now)["action"] == "none")
d = rs.decide_vector(dict(base, senses_mtime=now - 900), {}, now)
check("stale senses + pings -> vic_restart (the 10-05 wedge)", d["action"] == "vic_restart", d["why"])
d = rs.decide_vector(dict(base, senses_mtime=now - 900, robot_pings=False), {}, now)
check("stale + NO ping -> none (DHCP/power case, other recipe)", d["action"] == "none", d["why"])
st = {"vector_heals": [now - 100, now - 50]}
check("budget spent -> stand_down",
      rs.decide_vector(dict(base, senses_mtime=now - 900), st, now)["action"] == "stand_down")
st = {"vector_heals": [now - 7 * 3600, now - 6.5 * 3600]}
check("old heals fall out of the window",
      rs.decide_vector(dict(base, senses_mtime=now - 900), st, now)["action"] == "vic_restart")
st = {}
check("daemon just vanished -> grace", rs.decide_vector(dict(base, daemon_pids=[]), st, now)["action"] == "none")
check("daemon gone > 5 min -> start_daemon",
      rs.decide_vector(dict(base, daemon_pids=[]), st, now + 400)["action"] == "start_daemon")

print("== B. orb listener ==")
st = {}
check("listener up -> none", rs.decide_orb(True, st, now)["action"] == "none")
check("first miss only confirms", rs.decide_orb(False, st, now)["action"] == "none")
check("second miss -> orb_restart", rs.decide_orb(False, st, now + 30)["action"] == "orb_restart")
check("recovery resets the count", rs.decide_orb(True, st, now + 60)["action"] == "none" and st["orb_fails"] == 0)
st = {"orb_heals": [now - 10, now - 20, now - 30], "orb_fails": 5}
check("budget spent -> stand_down", rs.decide_orb(False, st, now)["action"] == "stand_down")

print("== E. wire-pod ==")
st = {}
check("chipper running -> none", rs.decide_wirepod({"running": True}, st, now)["action"] == "none")
check("first absence only starts the grace",
      rs.decide_wirepod({"running": False}, st, now)["action"] == "none")
check("absent past grace -> start_chipper",
      rs.decide_wirepod({"running": False}, st, now + 200)["action"] == "start_chipper")
check("back up clears the clock",
      rs.decide_wirepod({"running": True}, st, now + 260)["action"] == "none" and "wirepod_gone_since" not in st)
check("off flag -> none even when absent",
      rs.decide_wirepod({"running": False, "off": True}, {"wirepod_gone_since": now - 999}, now)["action"] == "none")
st = {"wirepod_heals": [now - 10, now - 20, now - 30], "wirepod_gone_since": now - 999}
check("budget spent -> stand_down", rs.decide_wirepod({"running": False}, st, now)["action"] == "stand_down")
check("live observe sees chipper", rs.observe_wirepod().get("running") is True)

print("== C. pairing ==")
pend = {"abc123": {"senderId": "42", "expiresAt": (now + 3000) * 1000},
        "old999": {"senderId": "43", "expiresAt": (now - 10) * 1000}}
st = {}
new = rs.decide_pairing(pend, st, now)
check("new unexpired code alerted", [n["code"] for n in new] == ["abc123"], str(new))
check("minutes left ~50", new and 48 <= new[0]["minutes_left"] <= 50, str(new))
st["pairing_alerted"]["abc123"] = now
check("same code not alerted twice", rs.decide_pairing(pend, st, now + 60) == [])

print("== C. pairing end-to-end (Discord mocked, access.json READ-ONLY) ==")
with tempfile.TemporaryDirectory(dir=str(ROOT / "scratch" / "tmp")) as td:
    td = Path(td)
    rs._DIR, rs.STATE_PATH, rs.LOG_PATH = td, td / "s.json", td / "l.jsonl"
    rs.ACCESS_JSON = td / "access.json"
    body = {"dmPolicy": "pairing", "allowFrom": ["1"], "pending": {
        "c0ffee": {"senderId": "77", "chatId": "9", "createdAt": time.time() * 1000,
                   "expiresAt": (time.time() + 3600) * 1000, "replies": 1}}}
    rs.ACCESS_JSON.write_text(json.dumps(body), encoding="utf-8")
    before = rs.ACCESS_JSON.read_text(encoding="utf-8")
    sent = []
    rs.dm_zeke = lambda text: sent.append(text) or True
    rs.discord_username = lambda uid: "TestSender"
    rs.port_ok = lambda port=5876: True
    sup = rs.Supervisor({})
    sup._next_vec = float("inf")   # skip the vector watch in this test
    sup.tick()
    sup.tick()
    check("exactly one DM for one new code", len(sent) == 1, str(len(sent)))
    check("DM names the sender + code", sent and "TestSender" in sent[0] and "c0ffee" in sent[0])
    check("access.json untouched", rs.ACCESS_JSON.read_text(encoding="utf-8") == before)

print("== D. relay other approved DMs (pure) ==")


def snow(epoch_s):  # a Discord snowflake id for a given epoch second
    return str(int((epoch_s - 1420070400.0) * 1000) << 22)


t = 1_800_000_000.0
msgs = [
    {"id": snow(t - 3600), "author": {"id": "42"}, "content": "old history"},
    {"id": snow(t - 60), "author": {"id": "42"}, "content": "hi Iris, it's me"},
    {"id": snow(t - 30), "author": {"id": "999", "bot": True}, "content": "pairing code abc"},
    {"id": snow(t - 10), "author": {"id": "77"}, "content": "someone else"},
]
todo, cur = rs.decide_relay(msgs, "42", None, t)
check("first sight: relays only her recent message, not old history",
      [m["text"] for m in todo] == ["hi Iris, it's me"], str(todo))
check("cursor advances to the newest seen id", cur == snow(t - 10))
todo2, cur2 = rs.decide_relay(msgs + [{"id": snow(t - 1), "author": {"id": "42"}, "content": "you there?"}],
                              "42", cur, t)
check("with a cursor: only newer messages", [m["text"] for m in todo2] == ["you there?"], str(todo2))
todo3, _ = rs.decide_relay([{"id": snow(t), "author": {"id": "42"}, "content": "",
                             "attachments": [{"id": 1}]}], "42", cur2, t)
check("attachment-only message still relayed", len(todo3) == 1 and todo3[0]["attachments"] == 1)

print("== D. relay end-to-end (Discord + chat bridge mocked) ==")
with tempfile.TemporaryDirectory(dir=str(ROOT / "scratch" / "tmp")) as td:
    td = Path(td)
    rs._DIR, rs.STATE_PATH, rs.LOG_PATH = td, td / "s.json", td / "l.jsonl"
    rs.ACCESS_JSON = td / "access.json"
    rs.ACCESS_JSON.write_text(json.dumps({"allowFrom": [rs.ZEKE_USER_ID, "42"], "pending": {}}),
                              encoding="utf-8")
    now_ts = time.time()
    calls = []

    def fake_discord(method, path, body=None):
        calls.append((method, path))
        if method == "POST":
            return {"id": "CH42"}
        return [{"id": snow(now_ts - 5), "author": {"id": "42"}, "content": "hello from mom"}]
    rs._discord = fake_discord
    rs.discord_username = lambda uid: "MomUser"
    submitted = []
    import types
    fake_chat = types.SimpleNamespace(submit=lambda text: submitted.append(text) or "rid1")
    sys.modules["brain.iris_chat"] = fake_chat
    import brain
    brain.iris_chat = fake_chat
    sup = rs.Supervisor({})
    st = {}
    out = sup._relay_other_dms(st, now_ts)
    check("Zeke is never relayed (the host polls him)", all("CH" + rs.ZEKE_USER_ID not in p for _, p in calls))
    check("her message reached the chat bridge", len(submitted) == 1 and "hello from mom" in submitted[0])
    check("header says NOT Zeke + gives her chat_id", submitted and "NOT Zeke" in submitted[0] and "CH42" in submitted[0])
    out2 = sup._relay_other_dms(st, now_ts + 5)
    check("same message not relayed twice", len(submitted) == 1, str(len(submitted)))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
