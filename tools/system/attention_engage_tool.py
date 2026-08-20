"""attention_engage + attention_sentry + attention_lock_status.

Built 2026-08-20 at Zeke's ask: how do the big shops decide WHEN to track, and
make the eyes fast to drive tool-wise (one call, not four).

attention_engage — the CHAIN. What took four round-trips this morning
    (look -> objects -> follow start -> status) is one call: resolve the
    target NOW (object targets acquire a tracking lock via brain/object_lock),
    start the follow loop, and return what happened — acquired or not, where,
    and what the head is doing. One tool call from cold to locked-and-following.

attention_sentry — WHEN-to-track policy, the part big PTZ systems ship that
    mine lacked. Industry auto-tracking cameras sit in a cheap "armed" state
    (motion gate) and only spend detector/tracker cycles when something moves.
    Sentry does the same: ~1.6s frame-differencing samples (downscaled gray,
    CPU-trivial), suppressed while the head itself is moving; on real motion it
    consults a PRIORITY POLICY (zeke > any person > watchlist objects) and
    engages the follow loop on the winner. While the follow loop runs, sentry
    idles. When the follow loop stops, sentry re-arms. So the eyes decide to
    look on their own — and the decision has a policy, not a reflex.

attention_lock_status — introspection for the object lock layer (tracker kind,
    update/reval/reseat counts, current box/score).

Discipline: frames only from frame_store (no second camera handle), honest
misses, and everything here composes the EXISTING follow loop — nothing
rebuilt.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from tools.tool_registry import register_tool

_SENTRY_PERIOD_S = 1.6
_MOTION_PCT = 0.035        # fraction of downscaled pixels that must change
_PIXEL_DELTA = 22          # gray-level delta that counts as "changed"
_ENGAGE_COOLDOWN_S = 20.0  # after an engage, don't re-engage for this long


def _log(msg: str) -> None:
    import sys
    print(f"[attention_engage] {msg}", file=sys.stderr, flush=True)


# ── attention_engage ─────────────────────────────────────────────────────────

def _attention_engage(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """attention_engage(target='zeke'|'object:mug', period_s?, wander?)"""
    target = str(params.get("target") or "").strip()
    if not target:
        return {"ok": False, "error": "pass target='zeke' or 'object:<thing>'"}

    from brain import visual_attention as va
    from tools.system.attention_follow_tool import _attention_follow

    t0 = time.time()
    r_set = va.set_target(g, target)
    if not r_set.get("ok"):
        return {"ok": False, "error": f"set_target failed: {r_set}"}

    # Resolve once NOW so the caller learns immediately whether the thing is
    # actually in view (object targets acquire their tracking lock here).
    tid = (g.get("_attention_state_obj") or {}).get("target") or target
    seen = None
    try:
        obs = va.observe(g)
        st = obs.get("state") or {}
        # observe() has acquire hysteresis, so a fresh target often reports no
        # bbox on the very first cycle even though resolution SUCCEEDED (found
        # live 2026-08-20: engage on 'object:computer monitor' said
        # acquired_now=false while the lock was demonstrably holding the box).
        # Run a second observe if the first came back empty — by then the
        # lock/face record exists and the state carries it.
        if not st.get("last_bbox"):
            obs = va.observe(g)
            st = obs.get("state") or {}
        if st.get("last_bbox"):
            seen = {"bbox": st.get("last_bbox"),
                    "confidence": st.get("confidence"),
                    "resolved_by": st.get("resolved_by")}
    except Exception as e:
        _log(f"engage observe failed (follow loop will keep trying): {e!r}")

    follow_params: dict[str, Any] = {"action": "start"}
    if params.get("period_s"):
        follow_params["period_s"] = params["period_s"]
    if params.get("wander") is not None:
        follow_params["wander"] = params["wander"]
    r_follow = _attention_follow(follow_params, g)

    lock = None
    if str(tid).startswith("object:"):
        try:
            from brain import object_lock
            lock = object_lock.status()
        except Exception:
            pass

    return {"ok": bool(r_follow.get("ok")), "target": tid,
            "acquired_now": seen is not None, "seen": seen,
            "following": bool(r_follow.get("running")), "lock": lock,
            "engage_ms": int((time.time() - t0) * 1000)}


# ── attention_sentry ─────────────────────────────────────────────────────────

def _small_gray(frame):
    import cv2
    return cv2.cvtColor(cv2.resize(frame, (160, 120)), cv2.COLOR_BGR2GRAY)


def _motion_pct(prev, cur) -> float:
    import cv2
    d = cv2.absdiff(prev, cur)
    return float((d > _PIXEL_DELTA).sum()) / d.size


def _bearing_key(g: dict[str, Any]) -> tuple:
    b = (g.get("_attention_state_obj") or {}).get("bearing") or {}
    return (round(float(b.get("pan_deg") or 0.0), 1),
            round(float(b.get("tilt_deg") or 0.0), 1))


def _follow_running(g: dict[str, Any]) -> bool:
    st8 = g.get("_attention_follow") or {}
    t = st8.get("thread")
    return bool(t is not None and t.is_alive())


def _pick_person(g: dict[str, Any]) -> str | None:
    """Priority: zeke > named person > stably-unknown person.

    A face with NO person_id yet is recognition-in-flight (found 2026-08-20
    live: sentry fired the instant Zeke moved, sampled before insightface
    labeled him, and engaged 'person1' — which a RECOGNIZED zeke can never
    match, so the follow loop stared at a ghost). Unlabeled faces return None;
    the next sample (~1.6s) retries with the label present.
    """
    faces = g.get("_face_results") or []
    best = None
    unknown = None
    for f in faces:
        pid = str(f.get("person_id") or "").lower()
        if pid == "zeke":
            return "zeke"
        if pid.startswith("unknown_"):
            unknown = unknown or pid.replace("unknown_", "person")
        elif pid == "unknown":
            pass  # bare 'unknown' = recognition still settling, not a target
        elif pid:
            best = best or pid
    return best or unknown  # None while recognition is still in flight


def _sentry_loop(g: dict[str, Any], stop: threading.Event, st: dict[str, Any]) -> None:
    from brain import frame_store
    prev = None
    prev_bearing = None
    while not stop.is_set():
        stop.wait(st.get("period_s", _SENTRY_PERIOD_S))
        if stop.is_set():
            break
        st["samples"] = st.get("samples", 0) + 1
        try:
            if _follow_running(g):
                st["mode"] = "yielding"   # follow loop owns the eyes
                prev = None               # scene will have changed; re-baseline
                # Priority upgrade: if we engaged a watchlist OBJECT and a
                # person has since been recognized, the person wins — retarget
                # the running loop (people outrank things, always).
                if str(st.get("last_target") or "").startswith("object:"):
                    person = _pick_person(g)
                    # Upgrade only to NAMED people: an unlabeled or personN
                    # face is not better than a solid object lock — wait for
                    # recognition. (Live 2026-08-20: upgraded to bare
                    # 'unknown' mid-warm-up; same race, one branch further in.)
                    if person and not person.startswith("person"):
                        try:
                            from brain import visual_attention as va2
                            va2.set_target(g, person)
                            st["last_target"] = person
                            st["upgrades"] = st.get("upgrades", 0) + 1
                            _log(f"sentry upgraded object -> {person!r}")
                        except Exception as e:
                            _log(f"sentry upgrade failed: {e!r}")
                continue
            # gesture window: a nod/shake RETURNS to its start bearing, so
            # bearing-compare alone cannot see it (found live 2026-08-20 —
            # a shake tripped the motion gate). The gesture tool publishes
            # gesture_until; treat it + a settle margin as self-motion.
            guard = float((g.get("_attention_follow") or {})
                          .get("gesture_until") or 0.0)
            if time.time() < guard + 1.2:
                prev = None
                st["mode"] = "self-motion"
                continue
            bearing = _bearing_key(g)
            res = frame_store.get_buffered_frame(max_age_sec=2.5)
            if res.frame is None:
                st["mode"] = "blind"      # honest: no fresh frame, no opinion
                prev = None
                continue
            cur = _small_gray(res.frame)
            if prev is None or bearing != prev_bearing:
                # first frame, or the head moved between samples: motion would
                # be SELF-motion — re-baseline instead of firing on it.
                prev, prev_bearing = cur, bearing
                st["mode"] = "arming"
                continue
            pct = _motion_pct(prev, cur)
            prev, prev_bearing = cur, bearing
            st["mode"] = "armed"
            st["last_motion_pct"] = round(pct, 4)
            if pct < st.get("motion_pct", _MOTION_PCT):
                continue
            st["motion_events"] = st.get("motion_events", 0) + 1
            st["last_motion_ts"] = time.time()
            if time.time() - st.get("last_engage_ts", 0.0) < _ENGAGE_COOLDOWN_S:
                continue

            # ── policy: who/what deserves the eyes ──
            target = _pick_person(g)
            if target is None and (g.get("_face_results") or []):
                # Faces present but recognition still in flight: do NOT fall
                # through to the watchlist — a person outranks any object, so
                # wait a sample for the label. (Found live 2026-08-20: Zeke
                # walked in carrying his backpack; the sentry engaged
                # 'object:backpack' while his face was still unlabeled.)
                continue
            if target is None and st.get("watchlist"):
                try:
                    from brain import object_lock, vector_owl
                    det = None
                    for want in st["watchlist"]:
                        d = object_lock._detect(want, res.frame, 0.12)
                        if d is not None:
                            det, target = d, f"object:{want}"
                            break
                except Exception as e:
                    _log(f"sentry watchlist detect failed: {e!r}")
            if target is None:
                continue  # motion with nothing engageable: let it pass

            st["last_engage_ts"] = time.time()
            st["engages"] = st.get("engages", 0) + 1
            st["last_target"] = target
            _log(f"sentry engaging {target!r} (motion {pct:.1%})")
            try:
                from tools.system.attention_follow_tool import (
                    _attention_follow, _fire_signal)
                _attention_follow({"action": "start", "target": target}, g)
                # nudge the signal bus so I hear about it (best-effort)
                _fire_signal(g, "sentry_engaged",
                             {"target": target, "motion_pct": round(pct, 3)})
            except Exception as e:
                _log(f"sentry engage failed: {e!r}")
        except Exception as e:
            st["errors"] = st.get("errors", 0) + 1
            st["last_error"] = repr(e)
    st["mode"] = "stopped"


def _attention_sentry(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    """attention_sentry(action='start'|'stop'|'status', watchlist?, motion_pct?)"""
    action = str(params.get("action") or "status").lower()
    st = g.setdefault("_attention_sentry", {})
    t = st.get("thread")
    running = bool(t is not None and t.is_alive())

    if action == "status":
        out = {k: v for k, v in st.items() if k not in ("thread", "stop")}
        out.update({"ok": True, "running": running})
        return out

    if action == "stop":
        if running:
            st["stop"].set()
            st["thread"].join(timeout=4.0)
        return {"ok": True, "running": False, "was_running": running,
                "samples": st.get("samples"), "engages": st.get("engages")}

    if action == "start":
        wl = params.get("watchlist")
        if isinstance(wl, str):
            wl = [w.strip() for w in wl.split(",") if w.strip()]
        if wl:
            st["watchlist"] = wl
        if params.get("motion_pct"):
            st["motion_pct"] = float(params["motion_pct"])
        if params.get("period_s"):
            st["period_s"] = max(0.8, float(params["period_s"]))
        if running:
            return {"ok": True, "running": True, "note": "already on sentry",
                    "watchlist": st.get("watchlist")}
        stop = threading.Event()
        th = threading.Thread(target=_sentry_loop, args=(g, stop, st),
                              daemon=True, name="attention_sentry")
        st.update({"stop": stop, "thread": th, "mode": "arming",
                   "started_ts": time.time(), "samples": 0})
        th.start()
        return {"ok": True, "running": True, "watchlist": st.get("watchlist"),
                "motion_pct": st.get("motion_pct", _MOTION_PCT)}

    return {"ok": False, "error": f"unknown action {action!r} — start|stop|status"}


def _attention_lock_status(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        from brain import object_lock
        return {"ok": True, **object_lock.status()}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


register_tool(
    "attention_engage",
    "ONE-CALL eye chain: set target ('zeke'|'object:mug'), resolve it NOW "
    "(objects acquire a cheap TrackerVit lock — OWL-ViT once, ~12ms/frame "
    "after), start the PTZ follow loop, return acquired/seen/following in a "
    "single round-trip. Replaces look->objects->follow->status.",
    1,
    _attention_engage,
)

register_tool(
    "attention_sentry",
    "WHEN-to-track policy: cheap motion gate (self-motion suppressed) arms the "
    "eyes; on real motion a priority policy (zeke > person > watchlist "
    "objects) auto-engages the follow loop, then re-arms when it stops. "
    "action='start' (+watchlist='mug,helmet', motion_pct, period_s) | 'stop' "
    "| 'status'.",
    2,
    _attention_sentry,
)

register_tool(
    "attention_lock_status",
    "Object-lock introspection: tracker kind (vit/mil), box, score, update/"
    "revalidation/reseat counts, age. The tracking-by-detection layer's gauges.",
    1,
    _attention_lock_status,
)
