"""Offline proof for brain/memory_hygiene.py on a synthetic memory dir.
Run: .venv/Scripts/python.exe scripts/test_memory_hygiene.py"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from brain import memory_hygiene as mh  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {info}")


with tempfile.TemporaryDirectory(dir=str(ROOT / "scratch" / "tmp")) as td:
    td = Path(td)
    mem = td / "mem"
    mem.mkdir()
    mh.MEM_DIR, mh.CLAUDE_MD = mem, td / "CLAUDE.md"
    mh._STATE = td / "st"
    mh.CURSOR_PATH, mh.LOG_PATH = mh._STATE / "c.json", mh._STATE / "l.jsonl"
    mh.TRANSCRIPT = td / "t.jsonl"
    now = time.mktime((2026, 10, 5, 12, 0, 0, 0, 0, -1))
    (mem / "MEMORY.md").write_text("\n".join([
        "# CORE",
        "- **Voice is currently ON** (verified 08-01) see [[voice_note]]",
        "- **✅ RESOLVED 09-01: the old thing — do NOT re-investigate** " + "x" * 200,
        "- rule (corrected 09-01, corrected 09-03, corrected 09-05, was wrong 09-07) [[missing_note]]",
        "- see [hub](hub_a.md)",
    ]), encoding="utf-8")
    (mem / "hub_a.md").write_text("routes to [[child]] — today this changed", encoding="utf-8")
    (mem / "child.md").write_text("links on to grandchild.md", encoding="utf-8")
    (mem / "grandchild.md").write_text("deep note", encoding="utf-8")
    (mem / "voice_note.md").write_text("v", encoding="utf-8")
    (mem / "lonely.md").write_text("nobody links me", encoding="utf-8")
    (mem / "index_archive.md").write_text("index", encoding="utf-8")
    (td / "CLAUDE.md").write_text("- *(Until 2026-08-01 this read 'voice is ON')* history\n", encoding="utf-8")

    r = mh.audit(now=now)
    by = {}
    for f in r["findings"]:
        by.setdefault(f["check"], []).append(f)
    check("stale state claim found (65 d old 'currently ON')",
          any("MEMORY.md:2" == f.get("where") for f in by.get("stale_state_claim", [])))
    check("history lines in CLAUDE.md are NOT flagged",
          not any(f.get("where", "").startswith("CLAUDE.md") for f in by.get("stale_state_claim", [])))
    check("broken wikilink found", any("missing_note" in f["detail"] for f in by.get("broken_link", [])))
    check("resolved history in CORE flagged", bool(by.get("resolved_in_core")))
    check("stacked corrections flagged", bool(by.get("stacked_corrections")))
    check("relative date in a hub flagged", any("hub_a.md" in f.get("where", "") for f in by.get("relative_date", [])))
    orphans = by.get("orphans", [{}])[0].get("examples", [])
    check("reachability is transitive (grandchild reachable)", "grandchild.md" not in orphans, str(orphans))
    check("a truly unlinked note is an orphan", "lonely.md" in orphans, str(orphans))

    print("== dream cursor ==")
    rows = [{"ts": 100.0 + i, "iso": "x", "role": "user", "source": "zeke", "modality": "chat",
             "content": f"msg {i}"} for i in range(10)]
    mh.TRANSCRIPT.write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    d = mh.dream_next(max_items=4)
    check("first slice = oldest 4", [i["text"] for i in d["items"]] == ["msg 0", "msg 1", "msg 2", "msg 3"])
    check("remaining counted", d["remaining_after_this"] == 6, str(d["remaining_after_this"]))
    try:
        mh.dream_commit(d["until_ts"], "")
        no_summary_ok = True
    except ValueError:
        no_summary_ok = False
    check("commit without a summary refused", not no_summary_ok)
    mh.dream_commit(d["until_ts"], "nothing survived the filter")
    d2 = mh.dream_next(max_items=4)
    check("next slice starts after the cursor", d2["items"][0]["text"] == "msg 4")
    try:
        mh.dream_commit(50.0, "x")
        back_ok = True
    except ValueError:
        back_ok = False
    check("cursor can't move backwards", not back_ok)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
