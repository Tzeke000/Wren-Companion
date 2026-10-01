"""'What did NOT wake me' — the reader for the eye logs (Zeke's ask, 2026-10-01).

    .venv\\Scripts\\python.exe tests/test_wake_missed.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import wake_missed as wm  # noqa: E402

T0 = 1_790_870_000.0  # a fixed 'now' (local-time rendering is irrelevant to the assertions)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def _write(p: Path, rows: list[dict]) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p)


def _raw(t: float, person: str = "unknown", event: str = "face_appeared") -> dict:
    return {"ts_iso": _iso(t), "ts_epoch": t, "channel": "eyes_raw", "event": event,
            "detail": {"person": person, "signal_ts": t, "believed_present": True}}


def _commit(t: float, wake: bool, suppressed: str = "") -> dict:
    d = {"present": True, "ts": t, "wake": wake}
    if suppressed:
        d["suppressed"] = suppressed
    return {"ts_iso": _iso(t), "ts_epoch": t, "channel": "eyes", "event": "A face just appeared in view", "detail": d}


def _build(tmp: Path) -> dict:
    now = T0
    body = []
    # episode A (now-7000): 6 raw signals over 8 s, woke
    body += [_raw(now - 7000 + i) for i in range(0, 8, 2)] + [_raw(now - 6994, "zeke")]
    body += [_commit(now - 6995, True)]
    # episode B (now-6000): 3 signals in 2 s, nothing → below confirm
    body += [_raw(now - 6000 + i * 0.7) for i in range(3)]
    # episode C (now-5000): 10 s of zeke, no commit, zeke woke 4 min earlier? no → make a wake at now-5200 for zeke
    body += [_commit(now - 5200, True)] + [_raw(now - 5201, "zeke")]
    body += [_raw(now - 5000 + i * 2, "zeke") for i in range(6)]          # dedupe: zeke woke 200 s ago
    # episode D (now-4000): 12 s unknown, host wrote wake:false with a reason
    body += [_raw(now - 4000 + i * 3) for i in range(5)] + [_commit(now - 3990, False, "departure (ledger-only by default)")]
    # episode E (now-3000): 12 s unknown, salience auto_suppressed
    body += [_raw(now - 3000 + i * 3) for i in range(5)]
    ledger = [{"ts": now - 2995, "ts_iso": _iso(now - 2995), "sig": "face_appeared|unknown|steady|wifi=present",
               "verdict": "auto_suppressed", "source": "perception", "note": "gate"}]
    # episode F (now-2000): 20 s unknown, no commit, nothing nearby → unconfirmed drop
    body += [_raw(now - 2000 + i * 4) for i in range(6)]
    # runtime-camera situations: some while host raw rows exist (near A), many in a window with NO host rows (now-1500..now-1200)
    sits = [{"ts": now - 6998, "ts_iso": _iso(now - 6998), "situation": "zeke_presence", "source": "runtime-camera", "role": "fallback", "wake": True}]
    sits += [{"ts": now - 1500 + i * 30, "ts_iso": _iso(now - 1500 + i * 30), "situation": "camera_transition",
              "source": "runtime-camera", "role": "unowned", "wake": True} for i in range(10)]
    # wifi joins: one seen (commit within 10 min at now-6995), one unseen (now-1400, no commit after)
    pres = [{"ts": _iso(now - 7100), "kind": "join"}, {"ts": _iso(now - 1400), "kind": "join"}]
    return {
        "body_log": _write(tmp / "body.jsonl", body),
        "verdicts": _write(tmp / "verdicts.jsonl", ledger),
        "situations": _write(tmp / "situations.jsonl", sits),
        "presence_log": _write(tmp / "presence.jsonl", pres),
    }


def test_episodes_are_labelled_with_honest_reasons() -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = _build(Path(td))
        res = wm.missed(3.0, now=T0, **paths)
        assert res["ok"], res
        # oldest-first for reasoning; items are newest-first in the output
        items = list(reversed(res["items"]))
        outcomes = [(i["outcome"], i["reason"].split(":")[0]) for i in items]
        assert outcomes[0] == ("woke", ""), outcomes[0]
        assert outcomes[1][0] == "missed" and outcomes[1][1].startswith("below appear-confirm"), outcomes[1]
        assert outcomes[2] == ("woke", ""), outcomes[2]            # the now-5200 zeke wake
        assert outcomes[3] == ("missed", "dedupe"), outcomes[3]
        assert outcomes[4] == ("missed", "host wake"), outcomes[4]
        assert items[4]["reason"].endswith("departure (ledger-only by default)")
        assert outcomes[5] == ("missed", "salience auto_suppressed"), outcomes[5]
        assert outcomes[6] == ("missed", "unconfirmed drop"), outcomes[6]
        assert res["episodes"] == 7 and res["woke"] == 2 and res["missed"] == 5, (res["episodes"], res["woke"], res["missed"])
        assert res["by_reason"]["dedupe"] == 1 and res["by_reason"]["salience auto_suppressed"] == 1


def test_cross_checks_find_dead_host_eyes_unseen_arrivals_and_unowned() -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = _build(Path(td))
        res = wm.missed(3.0, now=T0, host_eyes_poll_age_s=7989.6, **paths)
        hed = res["host_eyes_dead"]
        assert hed and hed["runtime_camera_events_host_missed"] == 10, hed
        assert res["arrivals_unseen"] and len(res["arrivals_unseen"]) == 1, res["arrivals_unseen"]
        assert res["unowned_situations"] == {"camera_transition <- runtime-camera": 10}
        assert res["host_eyes_now"].startswith("DEAD/STALLED")
        line = wm.summary_line(res)
        assert "HOST EYES DEAD" in line and "1 arrival(s) the eyes never saw" in line and "unowned:" in line, line


def test_window_filters_and_missing_files_are_fine() -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = _build(Path(td))
        res = wm.missed(0.25, now=T0, **paths)   # last 15 min: nothing happened
        assert res["ok"] and res["episodes"] == 0 and res["host_eyes_dead"] is None
        res2 = wm.missed(3.0, now=T0, body_log=str(Path(td) / "nope.jsonl"), verdicts=str(Path(td) / "nope2.jsonl"),
                         situations=str(Path(td) / "nope3.jsonl"), presence_log=str(Path(td) / "nope4.jsonl"))
        assert res2["ok"] and res2["episodes"] == 0
        assert wm.summary_line({"ok": False, "error": "x"}).startswith("missed-view error")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)
