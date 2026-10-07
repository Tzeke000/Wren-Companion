"""attention_arbiter — wake/attention ownership + traffic control (Iris_fixes #6 + addition A).

Zeke's words (Iris_fixes.docx, 2026-09-30): "Define a primary owner and fallback owner for
every overlapping event type ... redundancy without duplicate wakes or two systems
independently deciding the same event needs cognition."  And addition A: one common
arbitration point that can answer — what wants my attention, how important, why, does it
need cognition now, can it wait, is another event already handling the same underlying
situation, what did I deliberately choose not to act on.  "Traffic control between
faculties, not another brain."

What overlapped before this (open_loops_audit_vale_2026-09-30.md §2):
  * Zeke arriving → the body host's eyes poller (a perception wake), iris_attention_sources'
    camera loop (an ambient <channel> emit into the SAME session) and the wifi watcher (an
    orb-chat wake that coordinates by asking me in English whether I'd already said hello).
  * A stranger → face_appeared|unknown (host wake) AND unknown_capture (host wake) seconds apart.

The model here is a SITUATION LEDGER.  A producer that notices something calls
decide(situation, source).  The primary owner of that situation always proceeds (its own
gates — hysteresis, wake economy, wake_salience — still apply) and its claim is recorded.
A fallback owner proceeds only if no primary claim with wake=True exists inside the
situation's dedupe window and the primary is not known to be alive; otherwise its event is
recorded as a DUPLICATE / STANDING-BY and it must NOT wake cognition.  Unlisted situations
or sources fail OPEN (wake) and are recorded so the table can grow from evidence.

Everything is stdlib and fail-open.  A broken arbiter can only ever cause the old
behaviour (duplicate wakes), never silence.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

# ---------------------------------------------------------------- the ownership table
# situation → who owns it, who may stand in, how long a primary claim "covers" the situation.
OWNERSHIP: dict[str, dict[str, Any]] = {
    "zeke_presence": {
        "primary": "host-eyes", "fallback": ("wifi", "runtime-camera"), "dedupe_s": 600.0,
        "importance": "medium",
        "why": "the camera is the faster, richer signal (it named him ~10 min before wifi on 08-25); "
               "wifi confirms the building, not the room",
    },
    "zeke_absence": {
        "primary": "wifi", "fallback": ("host-eyes",), "dedupe_s": 900.0,
        "importance": "low",
        "why": "the camera loses him constantly (head down, out of frame); the phone leaving the "
               "wifi is the only signal that he left the BUILDING",
    },
    "unknown_person": {
        "primary": "host-eyes", "fallback": ("unknown_capture", "runtime-camera"), "dedupe_s": 180.0,
        "importance": "high",
        "why": "UNKNOWN OUTRANKS KNOWN (Zeke 09-02); the host's face_appeared|unknown wake already "
               "carries the study rule — unknown_capture is a follow-up about photos on disk, not a "
               "second stranger",
    },
    "person_presence": {   # an enrolled non-Zeke face (a friend)
        "primary": "host-eyes", "fallback": ("runtime-camera",), "dedupe_s": 600.0,
        "importance": "medium", "why": "same path as zeke_presence; a known visitor",
    },
    "camera_transition": {   # added 2026-10-02 from evidence: wake_missed listed it unowned ×4/3 h
        # iris_attention_sources tags every runtime-camera transition that is NOT an arrival of
        # zeke / an unknown face with this name — in practice "<who> left frame" (-> no_face).
        "primary": "host-eyes", "fallback": ("runtime-camera",), "dedupe_s": 600.0,
        "importance": "low",
        "why": "a face leaving the FRAME is not news (the camera loses him constantly); the host's "
               "eyes already record departures ledger-only, and leaving the BUILDING is wifi's "
               "(zeke_absence). The runtime camera only speaks if the host's eyes are dead",
    },
    "sibling_letter": {
        "primary": "host-letters", "fallback": ("runtime-sibling",), "dedupe_s": 6 * 3600.0,
        "importance": "medium",
        "why": "the host's post-office poller owns letters while it runs; the runtime's inbox "
               "watcher can only emit into a live session anyway (it is a re-surface, not a rescue)",
    },
    "llm_pending": {
        "primary": "stop-hook", "fallback": ("host-llm-nudge",), "dedupe_s": 0.0,
        "importance": "low",
        "why": "the Stop hook delivers brain/* requests when a turn ends; the host nudge exists "
               "because IDLE = DEAF — it is the designed fallback, not a duplicate",
    },
    "mood_shift": {
        "primary": "runtime-channel", "fallback": (), "dedupe_s": 0.0,
        "importance": "low", "why": "single producer (iris_attention_sources mood loop)",
    },
}
HOST_EYES_ALIVE_S = 10.0     # the host polls /api/v1/signals every 1.5 s; silence this long = gone
TAIL_LINES = 3000
MAX_AGE_S = 2 * 86400.0      # situation rows older than this are history


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ledger_path() -> str:
    p = os.environ.get("ATTENTION_ARBITER_LEDGER", "").strip()
    if p:
        return p
    try:
        from brain.iris_paths import paths  # type: ignore
        return str(paths.state_dir / "attention" / "situations.jsonl")
    except Exception:
        return os.path.join(_repo_root(), "state", "attention", "situations.jsonl")


# ---------------------------------------------------------------- liveness of the primary eyes
def host_eyes_alive(g: Optional[dict[str, Any]] = None, *, now: Optional[float] = None
                    ) -> tuple[Optional[bool], Optional[float]]:
    """(alive, age_s) from the body host's last /api/v1/signals poll, stamped by orb_http into
    the runtime's globals. Only answerable IN the runtime process (g given); elsewhere (None, None)
    = unknown, which fallback logic treats as "not known alive" (fail-open → wake)."""
    if g is None:
        return None, None
    try:
        ts = float(g.get("_host_eyes_poll_ts") or 0.0)
        if ts <= 0.0:
            return None, None
        age = (float(now) if now is not None else time.time()) - ts
        return (age <= HOST_EYES_ALIVE_S), age
    except Exception:
        return None, None


# ---------------------------------------------------------------- ledger
_CACHE: dict[str, Any] = {"path": "", "mtime": -1.0, "rows": []}


def _append(row: dict[str, Any]) -> None:
    p = ledger_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _CACHE["mtime"] = -1.0


def _load(now: Optional[float] = None) -> list[dict[str, Any]]:
    p = ledger_path()
    try:
        st = os.stat(p)
    except OSError:
        return []
    if _CACHE["path"] == p and _CACHE["mtime"] == st.st_mtime:
        rows = _CACHE["rows"]
    else:
        rows = []
        try:
            with open(p, "rb") as f:
                size = st.st_size
                f.seek(max(0, size - TAIL_LINES * 260))
                blob = f.read().decode("utf-8", errors="replace")
            lines = blob.split("\n")
            if size > TAIL_LINES * 260:
                lines = lines[1:]
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("situation") and d.get("source"):
                    rows.append(d)
        except Exception:
            rows = []
        _CACHE.update({"path": p, "mtime": st.st_mtime, "rows": rows})
    t = float(now) if now is not None else time.time()
    return [r for r in rows if (t - float(r.get("ts") or 0.0)) <= MAX_AGE_S]


def _owner_match(source: str, owner: str) -> bool:
    s = (source or "").strip().lower()
    o = (owner or "").strip().lower()
    return bool(o) and (s == o or s.startswith(o + ":"))


def _last_primary_claim(situation: str, primary: str, window_s: float, now: float
                        ) -> Optional[dict[str, Any]]:
    if window_s <= 0:
        return None
    for r in reversed(_load(now)):
        if r.get("situation") != situation:
            continue
        if not _owner_match(str(r.get("source") or ""), primary):
            continue
        if not r.get("wake", False):
            continue          # a primary that saw it but did NOT wake does not cover it
        if (now - float(r.get("ts") or 0.0)) <= window_s:
            return r
        break                 # rows are chronological; older ones are outside the window too
    return None


# ---------------------------------------------------------------- the arbitration point
def decide(situation: str, source: str, *, detail: str = "", primary_alive: Optional[bool] = None,
           now: Optional[float] = None, record: bool = True) -> dict[str, Any]:
    """May `source` wake cognition about `situation` right now?

    Returns {wake, role, reason, situation, source, covered_by?}. Roles: primary | fallback |
    duplicate | standing_by | unowned | unlisted. Fail-open on any error (wake=True)."""
    t = float(now) if now is not None else time.time()
    sit = (situation or "").strip()
    src = (source or "").strip()
    out: dict[str, Any] = {"situation": sit, "source": src, "wake": True, "role": "unowned",
                           "reason": "", "ts": t}
    try:
        entry = OWNERSHIP.get(sit)
        if entry is None:
            out["reason"] = "no ownership entry for this situation (fail-open)"
        elif _owner_match(src, entry["primary"]):
            out["role"] = "primary"
            out["reason"] = "primary owner of %s" % sit
        elif any(_owner_match(src, fb) for fb in entry.get("fallback", ())):
            cov = _last_primary_claim(sit, entry["primary"], float(entry.get("dedupe_s") or 0.0), t)
            if cov is not None:
                out.update(wake=False, role="duplicate",
                           reason="%s already handled by %s %.0fs ago" % (sit, cov.get("source"), t - float(cov["ts"])),
                           covered_by={"source": cov.get("source"), "ts": cov.get("ts"),
                                       "detail": cov.get("detail", "")})
            elif primary_alive is True:
                out.update(wake=False, role="standing_by",
                           reason="primary owner %s is alive and will decide %s itself" % (entry["primary"], sit))
            else:
                out["role"] = "fallback"
                out["reason"] = ("primary owner %s not known alive and no recent claim — fallback wakes"
                                 % entry["primary"])
        else:
            out["role"] = "unlisted"
            out["reason"] = "source not in the ownership table for %s (fail-open)" % sit
    except Exception as e:
        out["role"] = "error"
        out["reason"] = "arbiter error (fail-open): %r" % (e,)
        out["wake"] = True
    if record:
        try:
            _append({"ts": t, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)),
                     "situation": sit, "source": src, "role": out["role"], "wake": bool(out["wake"]),
                     "reason": out["reason"], "detail": (detail or "")[:200],
                     **({"covered_by_ts": out["covered_by"]["ts"]} if "covered_by" in out else {})})
        except Exception:
            pass
    return out


def claim(situation: str, source: str, *, detail: str = "", wake: bool = True,
          now: Optional[float] = None) -> dict[str, Any]:
    """Record that `source` observed `situation` (and whether it woke cognition) without asking
    permission — for primaries whose own gates already decided. Returns the row."""
    t = float(now) if now is not None else time.time()
    entry = OWNERSHIP.get((situation or "").strip())
    role = "primary" if entry and _owner_match(source, entry["primary"]) else (
        "fallback" if entry and any(_owner_match(source, fb) for fb in entry.get("fallback", ())) else "unowned")
    row = {"ts": t, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)),
           "situation": (situation or "").strip(), "source": (source or "").strip(), "role": role,
           "wake": bool(wake), "reason": "claim", "detail": (detail or "")[:200]}
    try:
        _append(row)
    except Exception:
        pass
    return row


def recent(n: int = 30, *, situation: Optional[str] = None, now: Optional[float] = None
           ) -> list[dict[str, Any]]:
    rows = _load(now)
    if situation:
        rows = [r for r in rows if r.get("situation") == situation]
    return rows[-max(1, int(n)):]


def explain(*, g: Optional[dict[str, Any]] = None, now: Optional[float] = None,
            window_s: float = 900.0) -> dict[str, Any]:
    """Zeke's seven questions, answered from the ledgers — not a controller, a readout."""
    t = float(now) if now is not None else time.time()
    rows = [r for r in _load(t) if (t - float(r.get("ts") or 0.0)) <= window_s]
    wants = [{"situation": r["situation"], "source": r["source"], "role": r.get("role"),
              "wake": r.get("wake"), "age_s": round(t - float(r["ts"]), 1),
              "detail": r.get("detail", "")} for r in rows[-20:]]
    now_ish = [w for w in wants if w["wake"] and w["age_s"] <= 60.0]
    waiting = [w for w in wants if w["role"] in ("duplicate", "standing_by")]
    handled = [{"situation": r["situation"], "by": r.get("source"),
                "deferred_to_ts": r.get("covered_by_ts")} for r in rows if r.get("role") == "duplicate"]
    chose_not = []
    try:
        from brain import wake_salience as _ws
        chose_not = [{"sig": r.get("sig"), "verdict": r.get("verdict"), "note": r.get("note", ""),
                      "age_s": round(t - float(r.get("ts") or t), 1)}
                     for r in _ws.recent(10) if r.get("verdict") in ("nothing", "auto_suppressed")]
    except Exception:
        pass
    alive, age = host_eyes_alive(g, now=t)
    return {
        "what_wants_attention": wants,
        "how_important": {k: v["importance"] for k, v in OWNERSHIP.items()},
        "why": {k: v["why"] for k, v in OWNERSHIP.items()},
        "needs_cognition_now": now_ish,
        "can_it_wait": waiting,
        "already_handled_by": handled,
        "chose_not_to_act": chose_not,
        "owners": {k: {"primary": v["primary"], "fallback": list(v["fallback"]), "dedupe_s": v["dedupe_s"]}
                   for k, v in OWNERSHIP.items()},
        "host_eyes_alive": alive, "host_eyes_poll_age_s": (round(age, 1) if age is not None else None),
        "ledger": ledger_path(),
    }
