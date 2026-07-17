# SELF_ASSESSMENT: I am Iris's PILOT layer — background missions for my robot
# body, so my cognition stays FREE (hearing, answering) while the body moves.
"""Pilot layer — 2026-07-16 (Zeke: "you need to be able to hear and respond
while you're in your body").

THE ARCHITECTURE (classic mobile robotics, mapped onto my harness):

  L0  firmware reflexes       (cliff hard-stop)             — always on, ~ms
  L1  session reflex/guard    (vector_session: edge-guard,  — autonomous, 5-15Hz
      startle, prox-brake, fused sensor stream)
  L2  THIS FILE: pilot        (missions run in a WORKER     — continuous, 20Hz
      THREAD: servo/route/scan/dock; interruptible;
      reports EVENTS, nudges cognition on completion)
  L3  me (cognition)          (issue GOALS, receive EVENTS) — slow, turn-based

The whole point: L3 (my Claude turn) must NEVER block on motion. A mission
starts and returns instantly; my turn ends; the Stop hook delivers queued ear
transcripts / senses; I can answer Zeke MID-DRIVE and issue body_abort or a
new mission that preempts. This is the standard sense-plan-act rate split —
the plan layer runs orders of magnitude slower than the act layer, and only
exchanges goals & events with it (Brooks-style layering; the fast loops don't
wait for the slow one).

Obstacle handling stays where it belongs (L1/L2, not L3): servo_to gates every
tick on the ToF depth sensor (prox-brake + speed scaling — depth beats vision
here) and the edge-guard. The pilot adds mission-level recovery: on 'blocked'
it stops, backs off, reports — cognition decides what's next, it doesn't steer.
"""
from __future__ import annotations

import collections
import contextlib
import math
import threading
import time

_LOCK = threading.Lock()
_PILOT = None


def _session():
    from brain import vector_session
    s = vector_session.get_session(create=False)
    if s is None or not getattr(s, "connected", False) or s.robot is None:
        raise RuntimeError("no body session (body_open first)")
    return s


class Pilot:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.mission: dict | None = None
        self.abort_evt = threading.Event()
        self.events: collections.deque = collections.deque(maxlen=48)
        self.state = "idle"                      # idle | running
        self.started_ts = 0.0

    # ------------------------------------------------------------- events
    def _event(self, kind: str, detail, nudge: bool = False) -> dict:
        ev = {"t": round(time.time(), 2), "stamp": time.strftime("%H:%M:%S"),
              "kind": kind, "detail": detail}
        self.events.append(ev)
        if nudge:
            # Completion nudge -> cognition wakes with the outcome. Rides the
            # same iris_chat path as the inhabit daemon; the Stop hook stamps
            # DELIVERY AGE on it, so staleness is self-evident.
            with contextlib.suppress(Exception):
                from brain import iris_chat
                iris_chat.submit(
                    f"[VECTOR PILOT @ {ev['stamp']} — mission event from my "
                    f"body's pilot layer, not Zeke typing] {kind}: "
                    f"{str(detail)[:300]} Decide: next mission / body_abort / "
                    f"just note it. Reply with chat_reply (one short line ok)."
                )
        return ev

    # ------------------------------------------------------------ control
    def start(self, mission: dict) -> dict:
        with _LOCK:
            if self.state == "running":
                # preempt: newest goal wins (cognition changed its mind)
                self.abort_evt.set()
                if self.thread is not None:
                    self.thread.join(timeout=2.5)
                self._event("preempted", {"old": (self.mission or {}).get("kind")})
            self.abort_evt.clear()
            self.mission = dict(mission)
            self.state = "running"
            self.started_ts = time.time()
            self.thread = threading.Thread(
                target=self._run, args=(dict(mission),),
                name="body-pilot", daemon=True)
            self.thread.start()
        return {"ok": True, "started": mission.get("kind"),
                "note": "mission running in background — my turn stays free; "
                        "outcome arrives as a [VECTOR PILOT] nudge, or poll "
                        "body_pilot"}

    def abort(self) -> dict:
        self.abort_evt.set()
        with contextlib.suppress(Exception):
            s = _session()
            s._raw_wheels(0.0, 0.0)
            s._wheels = (0.0, 0.0)
        return {"ok": True, "aborted": self.state == "running"}

    def status(self) -> dict:
        return {"ok": True, "state": self.state, "mission": self.mission,
                "running_s": round(time.time() - self.started_ts, 1)
                if self.state == "running" else 0.0,
                "events": list(self.events)[-10:]}

    # ------------------------------------------------------------ missions
    def _run(self, m: dict) -> None:
        kind = str(m.get("kind") or "")
        try:
            fn = {"servo": self._m_servo, "route": self._m_route,
                  "scan": self._m_scan, "dock": self._m_dock,
                  "undock": self._m_undock}.get(kind)
            if fn is None:
                self._event("error", f"unknown mission kind {kind!r}")
                return
            fn(m)
        except Exception as e:
            with contextlib.suppress(Exception):
                s = _session()
                s._raw_wheels(0.0, 0.0)
                s._wheels = (0.0, 0.0)
            self._event("error", repr(e)[:220], nudge=True)
        finally:
            self.state = "idle"
            self.mission = None

    def _m_servo(self, m: dict) -> None:
        s = _session()
        r = s.servo_to(
            x=m.get("x"), y=m.get("y"),
            bearing_deg=m.get("bearing_deg"), dist_mm=m.get("dist_mm"),
            standoff_mm=float(m.get("standoff_mm") or 25.0),
            max_speed=m.get("max_speed"),
            timeout_s=float(m.get("timeout_s") or 20.0),
            relative=bool(m.get("relative")),
            abort_event=self.abort_evt)
        if r.get("aborted"):
            self._event("aborted", r)
        elif r.get("ok"):
            self._event("arrived", r, nudge=True)
        else:
            # blocked/refused: back off a touch so I'm not nose-on-obstacle
            with contextlib.suppress(Exception):
                s._raw_wheels(-60.0, -60.0)
                time.sleep(0.5)
                s._raw_wheels(0.0, 0.0)
            self._event("blocked", r, nudge=True)

    def _m_route(self, m: dict) -> None:
        s = _session()
        pts = list(m.get("points") or [])
        done = []
        for i, pt in enumerate(pts):
            if self.abort_evt.is_set():
                self._event("aborted", {"leg": i, "done": done})
                return
            r = s.servo_to(x=float(pt[0]), y=float(pt[1]),
                           standoff_mm=float(m.get("standoff_mm") or 30.0),
                           max_speed=m.get("max_speed"),
                           timeout_s=float(m.get("timeout_s") or 20.0),
                           abort_event=self.abort_evt)
            if r.get("aborted"):
                self._event("aborted", {"leg": i, "done": done})
                return
            if not r.get("ok"):
                self._event("blocked", {"leg": i, "at": pt, "res": r,
                                        "done": done}, nudge=True)
                return
            done.append(pt)
            self._event("waypoint", {"leg": i, "at": pt})
        self._event("route_done", {"legs": len(done)}, nudge=True)

    def _m_scan(self, m: dict) -> None:
        """Rotate-survey: N steps around 360°, sample fused state (esp. ToF
        depth + heading) at each, optional frame. Output = a polar room sketch
        from where I stand — the fast 'where am I / what's around' primitive."""
        s = _session()
        steps = max(4, min(16, int(m.get("steps") or 8)))
        save_frames = bool(m.get("frames"))
        step_deg = 360.0 / steps
        sweep = []
        for i in range(steps):
            if self.abort_evt.is_set():
                self._event("aborted", {"at_step": i})
                return
            with contextlib.suppress(Exception):
                s.turn(step_deg, speed_deg_s=120.0)
            time.sleep(0.35)                     # let stream/prox settle
            st = dict(s._latest or {})
            entry = {"heading": st.get("heading"),
                     "prox_mm": st.get("prox_mm"),
                     "prox_found": st.get("prox_found"),
                     "prox_q": st.get("prox_q")}
            if save_frames:
                with contextlib.suppress(Exception):
                    r = s.look(name=f"scan_{i}")
                    entry["frame"] = r.get("path")
                    entry["brightness"] = r.get("brightness")
            sweep.append(entry)
        self._event("scan_done", {"steps": steps, "sweep": sweep}, nudge=True)

    def _guarded_sdk(self, fn, label: str, timeout_s: float) -> dict:
        """Run a hang-capable SDK behavior call in a sub-thread with a hard
        join deadline (the 07-16 dock/undock-wedge scars: drive_on/off_charger
        can block FOREVER when the connection hiccups mid-call). On timeout the
        call is left detached in a daemon thread and we report 'hung' — the
        pilot, the tool bridge, and cognition all stay alive."""
        box: dict = {}

        def run():
            try:
                box["r"] = fn()
            except Exception as e:
                box["r"] = {"ok": False, "error": repr(e)[:200]}

        t = threading.Thread(target=run, daemon=True, name=f"pilot-{label}")
        t.start()
        t.join(timeout=timeout_s)
        if t.is_alive():
            return {"ok": False, "hung": True,
                    "note": f"{label} SDK call still blocking after "
                            f"{timeout_s:.0f}s — detached. Do NOT issue more "
                            "SDK behaviors; body_close + body_open to recover."}
        return box.get("r", {"ok": False, "error": "no result"})

    def _m_dock(self, m: dict) -> None:
        """MISSION-AWARE REFLEXES (the 23:41 double-hang etiology): docking is
        an EXPECTED close approach — but my L1 startle reflex reads the looming
        charger as 'something appeared <220mm' and backs away mid-maneuver, so
        drive_on_charger never completes and the SDK call blocks forever.
        Suspend expressive/startle reflexes for the maneuver; L0 firmware cliff
        stop and the session guard stay armed. Restore is in finally — a hang
        or exception can never leave me reflex-dead."""
        s = _session()
        self._event("docking",
                    "drive_on_charger started (reflexes suspended + control yielded)")
        prev = getattr(s, "_reflex_on", True)
        s._reflex_on = False
        s._yield_control_until = time.time() + 95.0   # guard won't reclaim control
        try:
            r = self._guarded_sdk(s.dock, "dock", 90.0)
        finally:
            s._reflex_on = prev
            s._yield_control_until = 0.0
        self._event("dock_result", r, nudge=True)

    def _m_undock(self, m: dict) -> None:
        s = _session()
        prev = getattr(s, "_reflex_on", True)
        s._reflex_on = False               # same close-quarters logic as dock
        s._yield_control_until = time.time() + 35.0
        try:
            r = self._guarded_sdk(s.undock, "undock", 30.0)
        finally:
            s._reflex_on = prev
            s._yield_control_until = 0.0
        self._event("undock_result", r, nudge=True)


def get_pilot() -> Pilot:
    global _PILOT
    with _LOCK:
        if _PILOT is None:
            _PILOT = Pilot()
    return _PILOT


def start_mission(mission: dict) -> dict:
    return get_pilot().start(mission)


def abort() -> dict:
    return get_pilot().abort()


def status() -> dict:
    return get_pilot().status()
