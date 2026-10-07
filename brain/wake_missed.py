"""brain/wake_missed.py — "what did NOT wake me?" (Zeke, 2026-10-01 Discord: *"you can't perceive what didn't
wake you … write something so that you can at least look in the logs at what didn't wake you"*).

Vale's blind spot, made a READER. Every source already writes; nothing read them side by side:
  * state/iris_body_log.jsonl   — `eyes_raw` (every signal the host's poller saw, person + believed_present) and
                                   `eyes` commits (`wake` true/false + `suppressed`/`salience` reason)
  * state/wake_verdicts.jsonl   — the salience gate's `auto_suppressed` rows (+ my explicit verdicts)
  * state/attention/situations.jsonl — the arbiter ledger: who claimed each situation (host-eyes / wifi /
                                   runtime-camera fallback / unowned) and whether it was allowed to wake
  * state/zeke_presence_log.jsonl — the wifi watcher's join/left events

`missed(hours)` groups the raw eye signals into EPISODES (gap > 30 s starts a new one) and labels each:
  woke · salience_auto_suppressed · eyes_wake_false (host reason) · dedupe (same person already woke within
  the owner's window) · below_confirm (episode shorter than the appear-confirm window) · unconfirmed_drop.
Then the cross-checks that no single log can show:
  * host_eyes_dead: runtime-camera situations (the runtime saw a face) while the host logged NO eyes_raw row
    within ±60 s — the host's poller is dead or stalled (found 2026-10-01: dead 2.2 h while Zeke was home).
  * arrivals_unseen: a wifi JOIN with no host eyes commit within ±10 min (symmetric: the camera usually sees him first).
  * unowned: situations the arbiter has no ownership entry for (fail-open wakes nobody reads).
Pure functions over files; paths injectable for tests. Never raises — returns {"ok": False, "error"} instead.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BODY_LOG = os.path.join(ROOT, "state", "iris_body_log.jsonl")
VERDICTS = os.path.join(ROOT, "state", "wake_verdicts.jsonl")
SITUATIONS = os.path.join(ROOT, "state", "attention", "situations.jsonl")
PRESENCE_LOG = os.path.join(ROOT, "state", "zeke_presence_log.jsonl")

EPISODE_GAP_S = 30.0
APPEAR_CONFIRM_S = 5.0        # mirrors IRIS_PERCEPTION_CONFIRM_APPEAR_S (host default)
DEDUPE_S = 600.0              # mirrors the arbiter's zeke_presence/person_presence window
NEAR_S = 30.0                 # a commit within this of a raw signal belongs to that episode
HOST_DEAD_NEAR_S = 60.0
ARRIVAL_SEEN_WITHIN_S = 600.0
MAX_ITEMS = 40


def _read_jsonl(path: str, *, max_bytes: int = 6_000_000) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial line
            for raw in f:
                raw = raw.strip()
                if not raw or raw[:1] != b"{":
                    continue
                try:
                    out.append(json.loads(raw.decode("utf-8", "replace")))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return out
    return out


def _ts(row: dict[str, Any]) -> float:
    for k in ("ts_epoch", "ts"):
        v = row.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v:
            try:
                return time.mktime(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                pass
    return 0.0


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts else ""


def _episodes(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group raw face_appeared signals into episodes (gap > EPISODE_GAP_S starts a new one)."""
    eps: list[dict[str, Any]] = []
    for r in sorted(raw_rows, key=_ts):
        t = _ts(r)
        d = r.get("detail") or {}
        person = str(d.get("person") or "unknown")
        if eps and t - eps[-1]["end"] <= EPISODE_GAP_S:
            e = eps[-1]
            e["end"] = t
            e["signals"] += 1
            e["persons"][person] = e["persons"].get(person, 0) + 1
        else:
            eps.append({"start": t, "end": t, "signals": 1, "persons": {person: 1}})
    return eps


def missed(hours: float = 3.0, *, now: float | None = None, body_log: str = BODY_LOG,
           verdicts: str = VERDICTS, situations: str = SITUATIONS, presence_log: str = PRESENCE_LOG,
           host_eyes_poll_age_s: float | None = None) -> dict[str, Any]:
    try:
        now = float(now if now is not None else time.time())
        since = now - float(hours) * 3600.0
        body = [r for r in _read_jsonl(body_log) if _ts(r) >= since]
        raw_appear = [r for r in body if r.get("channel") == "eyes_raw" and r.get("event") == "face_appeared"]
        raw_any = [r for r in body if r.get("channel") == "eyes_raw"]
        commits = [r for r in body if r.get("channel") == "eyes"]
        wakes = [c for c in commits if (c.get("detail") or {}).get("wake")]
        nowakes = [c for c in commits if not (c.get("detail") or {}).get("wake")]
        ledger = [r for r in _read_jsonl(verdicts) if _ts(r) >= since]
        auto_sup = [r for r in ledger if r.get("verdict") == "auto_suppressed"]
        sits = [r for r in _read_jsonl(situations) if _ts(r) >= since]
        pres = [r for r in _read_jsonl(presence_log) if _ts(r) >= since]

        # ── episodes of raw sightings → outcome ───────────────────────────────
        items: list[dict[str, Any]] = []
        by_reason: dict[str, int] = {}
        last_wake_by_person: dict[str, float] = {}
        for e in _episodes(raw_appear):
            s, t_end = e["start"], e["end"]
            near_wake = [w for w in wakes if s - NEAR_S <= _ts(w) <= t_end + NEAR_S]
            near_nowake = [w for w in nowakes if s - NEAR_S <= _ts(w) <= t_end + NEAR_S]
            near_sup = [w for w in auto_sup if s - NEAR_S <= _ts(w) <= t_end + NEAR_S]
            persons = sorted(e["persons"], key=lambda k: -e["persons"][k])
            lead = persons[0] if persons else "unknown"
            if near_wake:
                outcome, reason = "woke", ""
                for p in e["persons"]:
                    last_wake_by_person[p] = max(last_wake_by_person.get(p, 0.0), _ts(near_wake[-1]))
            elif near_sup:
                outcome, reason = "missed", "salience auto_suppressed: " + str(near_sup[-1].get("sig") or "")
            elif near_nowake:
                d = near_nowake[-1].get("detail") or {}
                outcome, reason = "missed", "host wake:false — " + str(d.get("suppressed") or d.get("salience") or "unspecified")
            elif any(0 < s - last_wake_by_person.get(p, 0.0) <= DEDUPE_S for p in e["persons"]):
                outcome, reason = "missed", "dedupe: same person already woke me within %d s" % int(DEDUPE_S)
            elif (t_end - s) < APPEAR_CONFIRM_S:
                outcome, reason = "missed", "below appear-confirm (%.0f s): flicker shorter than %.0f s" % (t_end - s, APPEAR_CONFIRM_S)
            else:
                outcome, reason = "missed", "unconfirmed drop: held %.0f s but no commit (host mid-turn, pending reset, or poller stalled)" % (t_end - s)
            if outcome == "missed":
                by_reason[reason.split(":")[0]] = by_reason.get(reason.split(":")[0], 0) + 1
            items.append({"start": _iso(s), "end": _iso(t_end), "signals": e["signals"], "persons": e["persons"],
                          "lead": lead, "outcome": outcome, "reason": reason})

        # ── cross-checks ──────────────────────────────────────────────────────
        raw_ts = sorted(_ts(r) for r in raw_any)

        def _host_saw_near(t: float) -> bool:
            # binary-ish scan; lists are small
            return any(abs(x - t) <= HOST_DEAD_NEAR_S for x in raw_ts)

        runtime_cam = [r for r in sits if str(r.get("source") or "") == "runtime-camera"]
        dead_hits = [r for r in runtime_cam if not _host_saw_near(_ts(r))]
        host_eyes_dead = None
        if dead_hits:
            host_eyes_dead = {"runtime_camera_events_host_missed": len(dead_hits),
                              "first": _iso(_ts(dead_hits[0])), "last": _iso(_ts(dead_hits[-1])),
                              "verdict": "the runtime saw faces the host's eye poller never logged — the host's eyes were dead or stalled in this window"}
        joins = [p for p in pres if str(p.get("kind") or "") in ("join", "joined", "arrived", "present")]
        commit_ts = sorted(_ts(c) for c in commits)
        # 2026-10-07: the window is SYMMETRIC. The camera usually names him
        # BEFORE the phone rejoins the wifi (10-07 15:32: eyes 15:32:11, wifi
        # join 15:32:47) — a forward-only window reported that arrival as
        # "never seen" by the very eyes that saw it first.
        arrivals_unseen = [{"join": _iso(_ts(j)), "note": "no host eyes commit within %d min" % int(ARRIVAL_SEEN_WITHIN_S // 60)}
                           for j in joins if not any(abs(c - _ts(j)) <= ARRIVAL_SEEN_WITHIN_S for c in commit_ts)]
        unowned: dict[str, int] = {}
        for r in sits:
            if str(r.get("role") or "") == "unowned":
                k = str(r.get("situation") or "?") + " <- " + str(r.get("source") or "?")
                unowned[k] = unowned.get(k, 0) + 1

        missed_items = [i for i in items if i["outcome"] == "missed"]
        out = {
            "ok": True, "window_hours": hours, "since": _iso(since), "now": _iso(now),
            "episodes": len(items), "woke": len(items) - len(missed_items), "missed": len(missed_items),
            "by_reason": by_reason,
            "raw_signals": {"face_appeared": len(raw_appear), "all_eyes_raw": len(raw_any)},
            "host_commits": {"wake": len(wakes), "no_wake": len(nowakes)},
            "salience_auto_suppressed": len(auto_sup),
            "host_eyes_dead": host_eyes_dead,
            "arrivals_unseen": arrivals_unseen,
            "unowned_situations": unowned,
            "items": list(reversed(items))[:MAX_ITEMS],
        }
        if host_eyes_poll_age_s is not None:
            out["host_eyes_poll_age_s"] = round(float(host_eyes_poll_age_s), 1)
            if host_eyes_poll_age_s > 120:
                out["host_eyes_now"] = "DEAD/STALLED: last host eye poll %.0f s ago" % host_eyes_poll_age_s
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}


def summary_line(res: dict[str, Any]) -> str:
    """One line for a run note / Discord."""
    if not res.get("ok"):
        return "missed-view error: " + str(res.get("error"))
    bits = ["%dh: %d episodes, %d woke, %d missed" % (res.get("window_hours", 0), res.get("episodes", 0),
                                                     res.get("woke", 0), res.get("missed", 0))]
    if res.get("by_reason"):
        bits.append("reasons " + ", ".join("%s=%d" % kv for kv in sorted(res["by_reason"].items(), key=lambda kv: -kv[1])))
    if res.get("host_eyes_dead"):
        bits.append("HOST EYES DEAD %s→%s (%d runtime events unseen)" % (
            res["host_eyes_dead"]["first"][11:16], res["host_eyes_dead"]["last"][11:16],
            res["host_eyes_dead"]["runtime_camera_events_host_missed"]))
    if res.get("arrivals_unseen"):
        bits.append("%d arrival(s) the eyes never saw" % len(res["arrivals_unseen"]))
    if res.get("unowned_situations"):
        bits.append("unowned: " + ", ".join("%s×%d" % kv for kv in res["unowned_situations"].items()))
    return " · ".join(bits)
