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
import json
import math
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_PILOT = None

REPO = Path(__file__).resolve().parent.parent
CRUMBS = REPO / "state" / "vector" / "breadcrumbs.jsonl"
BATTERY_JSON = REPO / "state" / "vector" / "battery.json"
CRUMB_SPACING_MM = 25.0     # min distance between logged breadcrumbs
CRUMB_KEEP = 400            # file trimmed to this many recent crumbs


def _battery_low_off_charger() -> bool:
    """True when the last battery poll says LOW while off the charger — the
    drain-that-killed-him-once condition. Missions must not start then."""
    try:
        d = json.loads(BATTERY_JSON.read_text(encoding="utf-8"))
        return (bool(d.get("ok")) and isinstance(d.get("level"), int)
                and d["level"] <= 1 and not d.get("on_charger"))
    except Exception:
        return False


# --- HAZARD MEMORY (2026-07-17 round 2, Zeke: "all things an AI needs for a
# mobile body"). Every blocked/stuck/detour location is journaled with pose +
# origin_id so I stop re-learning the same obstacles. The planner treats recent
# same-frame hazards as obstacles; body_pilot exposes them.
HAZARDS = REPO / "state" / "vector" / "hazards.jsonl"
HAZARD_KEEP = 300


def log_hazard(kind: str, x, y, origin, note: str = "") -> None:
    if x is None or y is None:
        return
    try:
        HAZARDS.parent.mkdir(parents=True, exist_ok=True)
        with HAZARDS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 2), "kind": kind,
                                "x": round(float(x), 1), "y": round(float(y), 1),
                                "origin": origin, "note": str(note)[:120]}) + "\n")
    except Exception:
        pass


def read_hazards(origin=None, max_n: int = HAZARD_KEEP) -> list:
    try:
        lines = HAZARDS.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-max_n:]:
        with contextlib.suppress(Exception):
            d = json.loads(ln)
            if origin is None or d.get("origin") == origin:
                out.append(d)
    return out


# --- RETURN-HOME MARGIN (2026-07-17 round 2): don't accept a mission I might
# not have the battery to come home from. Volts from the daemon's battery poll;
# conservative fixed thresholds (no measured mm-per-volt yet). Missing data =
# allow + note (hedge, don't false-block).
def _return_margin(target_xy=None) -> dict:
    try:
        d = json.loads(BATTERY_JSON.read_text(encoding="utf-8"))
        volts = float(d.get("volts") or 0.0)
        on_charger = bool(d.get("on_charger"))
    except Exception:
        return {"ok": True, "note": "battery unreadable — allowing (hedged)"}
    if on_charger or volts <= 0.0:
        return {"ok": True}
    if volts < 3.60:
        return {"ok": False,
                "refused": f"battery {volts:.2f}V off-charger — below return "
                           f"margin; dock (body_park) is the only sane mission"}
    if volts < 3.72:
        return {"ok": True, "warn": f"battery {volts:.2f}V — thin margin, keep "
                                    f"this mission short and end near home"}
    return {"ok": True}


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
        # BATTERY GATE (2026-07-17): low battery off-charger = dock or nothing.
        if str(mission.get("kind")) != "dock" and _battery_low_off_charger():
            return {"ok": False,
                    "refused": "battery LOW and off charger — only a dock "
                               "mission (body_park) is allowed right now"}
        margin_warn = None
        if str(mission.get("kind")) not in ("dock", "smart_park"):
            mg = _return_margin()
            if not mg.get("ok"):
                return {"ok": False, "refused": mg.get("refused")}
            margin_warn = mg.get("warn")
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
        out = {"ok": True, "started": mission.get("kind"),
               "note": "mission running in background — my turn stays free; "
                       "outcome arrives as a [VECTOR PILOT] nudge, or poll "
                       "body_pilot"}
        if margin_warn:
            out["battery_warn"] = margin_warn
        return out

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
                "events": list(self.events)[-10:],
                "hazards_known": len(read_hazards())}

    # --------------------------------------------------------- breadcrumbs
    # (2026-07-17, mobility layer): every mission drops a pose trail to disk in
    # the current odometry frame. The trail is the known-clear path — 'retrace'
    # walks it backwards to escape a dead end the way I came in. Crumbs key on
    # the pose origin_id: a frame reset (pickup/sleep) makes old crumbs alien.
    def _crumb_loop(self, stop_evt: threading.Event) -> None:
        last_xy = None
        while not stop_evt.is_set():
            with contextlib.suppress(Exception):
                s = _session()
                st = dict(s._latest or {})
                x, y = st.get("x"), st.get("y")
                if x is not None and y is not None:
                    if (last_xy is None or
                            math.hypot(x - last_xy[0], y - last_xy[1]) >= CRUMB_SPACING_MM):
                        last_xy = (x, y)
                        rec = {"t": round(time.time(), 2), "x": x, "y": y,
                               "h": st.get("heading"), "origin": st.get("origin")}
                        CRUMBS.parent.mkdir(parents=True, exist_ok=True)
                        with CRUMBS.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(rec) + "\n")
            stop_evt.wait(0.4)

    def _read_crumbs(self, origin) -> list:
        """Recent crumbs in the CURRENT pose frame only, oldest->newest."""
        try:
            lines = CRUMBS.read_text(encoding="utf-8").strip().splitlines()
        except Exception:
            return []
        out = []
        for ln in lines[-CRUMB_KEEP:]:
            with contextlib.suppress(Exception):
                d = json.loads(ln)
                if d.get("origin") == origin:
                    out.append(d)
        return out

    # ------------------------------------------------------------ missions
    def _run(self, m: dict) -> None:
        kind = str(m.get("kind") or "")
        crumb_stop = threading.Event()
        try:
            fn = {"servo": self._m_servo, "route": self._m_route,
                  "scan": self._m_scan, "dock": self._m_dock,
                  "undock": self._m_undock, "retrace": self._m_retrace,
                  "smart_park": self._m_smart_park,
                  "goto": self._m_goto,
                  "explore": self._m_explore}.get(kind)
            if fn is None:
                self._event("error", f"unknown mission kind {kind!r}")
                return
            if kind in ("servo", "route", "retrace", "goto", "explore"):
                threading.Thread(target=self._crumb_loop, args=(crumb_stop,),
                                 name="pilot-crumbs", daemon=True).start()
            fn(m)
        except Exception as e:
            with contextlib.suppress(Exception):
                s = _session()
                s._raw_wheels(0.0, 0.0)
                s._wheels = (0.0, 0.0)
            self._event("error", repr(e)[:220], nudge=True)
        finally:
            crumb_stop.set()
            self.state = "idle"
            self.mission = None

    # ------------------------------------------------- obstacle avoidance
    def _tof_read(self, s) -> float:
        """Settled MEDIAN ToF distance in the current facing (research playbook
        2026-07-17: settle after rotation, median of several reads, range-gate
        35..1100mm; out-of-range/no-target = free-to-max, NOT obstacle).
        1200 = clear/unknown."""
        time.sleep(0.30)
        vals = []
        for _ in range(4):
            st = dict(s._latest or {})
            pm = st.get("prox_mm")
            if (st.get("prox_found") and pm is not None
                    and st.get("prox_q", 0) > 0.02 and 35.0 <= pm <= 1100.0):
                vals.append(float(pm))
            time.sleep(0.08)
        if not vals:
            return 1200.0
        vals.sort()
        return vals[len(vals) // 2]

    def polar_scan(self, s, arc_deg: float = 150.0, steps: int = 7) -> list:
        """Whole-robot rotate-scan (the servo-pan substitute — research: same
        math, rotation instead of pan): sweep `arc_deg` centered on the current
        heading in `steps` stops, settled-median ToF at each. Returns
        [(rel_bearing_deg, dist_mm)] and restores the original heading."""
        steps = max(3, min(int(steps), 11))
        half = float(arc_deg) / 2.0
        offsets = [(-half + i * (float(arc_deg) / (steps - 1)))
                   for i in range(steps)]
        out = []
        cur = 0.0
        for off in offsets:
            if self.abort_evt.is_set():
                break
            with contextlib.suppress(Exception):
                s.turn(off - cur, speed_deg_s=140.0)
            cur = off
            out.append((round(off, 1), self._tof_read(s)))
        with contextlib.suppress(Exception):        # restore heading
            s.turn(-cur, speed_deg_s=140.0)
        return out

    @staticmethod
    def vfh_pick(scan: list, target_rel_deg: float = 0.0,
                 clear_mm: float = 300.0) -> dict:
        """VFH-lite valley pick over a sparse polar scan: sectors with
        dist >= clear_mm are free; contiguous free runs are valleys; choose
        the valley center nearest the target bearing (research: direct-polar
        simplification of Borenstein's VFH for sparse single-beam sweeps)."""
        if not scan:
            return {"ok": False, "error": "empty scan"}
        free = [(b, d) for (b, d) in scan if d >= clear_mm]
        if not free:
            widest = max(scan, key=lambda t: t[1])
            return {"ok": False, "blocked": True, "least_bad_deg": widest[0],
                    "least_bad_mm": widest[1]}
        valleys = []
        run = [free[0]]
        for (b, d) in free[1:]:
            if b - run[-1][0] <= (scan[1][0] - scan[0][0]) + 1.0:
                run.append((b, d))
            else:
                valleys.append(run)
                run = [(b, d)]
        valleys.append(run)
        best = min(valleys, key=lambda v: abs(
            (v[0][0] + v[-1][0]) / 2.0 - target_rel_deg))
        center = (best[0][0] + best[-1][0]) / 2.0
        return {"ok": True, "steer_deg": round(center, 1),
                "valley_width_deg": round(best[-1][0] - best[0][0], 1),
                "valley_min_mm": round(min(d for _, d in best), 0),
                "n_valleys": len(valleys)}

    def _detour(self, s, why: str, target_rel_deg: float = 0.0) -> bool:
        """One bounded escape maneuver (upgraded 2026-07-17 from 2-point probe
        to a 7-stop polar scan + VFH-lite valley pick — research playbook):
        back off, sweep the forward arc, steer into the valley nearest the
        goal bearing, sidestep. Returns False if aborted."""
        self._event("detour", {"why": str(why)[:140]})
        st = dict(s._latest or {})
        log_hazard("detour", st.get("x"), st.get("y"), st.get("origin"), why)
        with contextlib.suppress(Exception):        # back off nose-on-obstacle
            s._raw_wheels(-70.0, -70.0)
            time.sleep(0.9)
            s._raw_wheels(0.0, 0.0)
            s._wheels = (0.0, 0.0)
        if self.abort_evt.is_set():
            return False
        scan = self.polar_scan(s, arc_deg=150.0, steps=7)
        if self.abort_evt.is_set():
            return False
        pick = self.vfh_pick(scan, target_rel_deg=target_rel_deg)
        self._event("detour_scan", {"scan": scan, "pick": pick})
        steer = pick.get("steer_deg") if pick.get("ok") else pick.get("least_bad_deg", 60.0)
        with contextlib.suppress(Exception):
            s.turn(float(steer), speed_deg_s=140.0)
        with contextlib.suppress(Exception):        # sidestep past the obstacle
            s.straight(150.0, speed_mm_s=110.0)
        return not self.abort_evt.is_set()

    @staticmethod
    def _target_rel(s, kw: dict) -> float:
        """Goal bearing relative to current heading (deg, for the VFH pick).
        0.0 when the target is unknowable (relative/bearing modes)."""
        try:
            st = dict(s._latest or {})
            tx, ty = kw.get("x"), kw.get("y")
            if tx is None or ty is None or kw.get("relative"):
                return 0.0
            dx = float(tx) - float(st["x"])
            dy = float(ty) - float(st["y"])
            b = math.degrees(math.atan2(dy, dx))
            return ((b - float(st.get("heading", 0.0)) + 180.0) % 360.0) - 180.0
        except Exception:
            return 0.0

    def _servo_avoid(self, s, m: dict, kw: dict) -> dict:
        """servo_to + bounded detour retries. Blocked (prox-brake / stuck) →
        escape maneuver → re-servo, up to max_detours times. avoid=False = old
        one-shot behavior."""
        avoid = m.get("avoid", True)
        detours = int(m.get("max_detours") if m.get("max_detours") is not None else 2)
        r = s.servo_to(**kw, abort_event=self.abort_evt)
        while (avoid and detours > 0 and not r.get("ok")
               and not r.get("aborted")
               and (r.get("refused") or r.get("stuck") or r.get("timed_out"))
               and not self.abort_evt.is_set()):
            detours -= 1
            if not self._detour(s, r.get("refused") or "timed out short of target",
                                target_rel_deg=self._target_rel(s, kw)):
                r["aborted"] = True
                break
            r = s.servo_to(**kw, abort_event=self.abort_evt)
        return r

    def _m_servo(self, m: dict) -> None:
        s = _session()
        kw = dict(x=m.get("x"), y=m.get("y"),
                  bearing_deg=m.get("bearing_deg"), dist_mm=m.get("dist_mm"),
                  standoff_mm=float(m.get("standoff_mm") or 25.0),
                  max_speed=m.get("max_speed"),
                  timeout_s=float(m.get("timeout_s") or 20.0),
                  relative=bool(m.get("relative")))
        r = self._servo_avoid(s, m, kw)
        if r.get("aborted"):
            self._event("aborted", r)
        elif r.get("ok"):
            self._event("arrived", r, nudge=True)
        else:
            st = dict(s._latest or {})
            log_hazard("blocked", st.get("x"), st.get("y"), st.get("origin"),
                       str(r.get("refused") or "")[:100])
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
            kw = dict(x=float(pt[0]), y=float(pt[1]),
                      standoff_mm=float(m.get("standoff_mm") or 30.0),
                      max_speed=m.get("max_speed"),
                      timeout_s=float(m.get("timeout_s") or 20.0))
            r = self._servo_avoid(s, m, kw)
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

    def _m_goto(self, m: dict) -> None:
        """MAP-AWARE GOTO (2026-07-17 round 2): A* through the room blueprint
        + hazard memory → waypoint route AROUND known obstacles; graceful
        fallback to direct servo-with-detours when there's no usable map
        (defensive fallback: never worse than body_go)."""
        s = _session()
        st = dict(s._latest or {})
        if st.get("x") is None or m.get("x") is None or m.get("y") is None:
            self._event("error", "goto needs a live pose and target x,y",
                        nudge=True)
            return
        from brain import vector_planner
        hz = read_hazards(origin=st.get("origin"))
        pl = vector_planner.plan((st["x"], st["y"]),
                                 (float(m["x"]), float(m["y"])), hz)
        if pl.get("ok"):
            self._event("planned", {"legs": len(pl["points"]),
                                    "length_mm": pl["length_mm"],
                                    "plan_ms": pl["plan_ms"],
                                    "hazards_used": pl["hazards_used"]})
            m2 = dict(m)
            m2["points"] = pl["points"]
            self._m_route(m2)
        else:
            self._event("plan_fallback", str(pl.get("error"))[:160])
            self._m_servo(m)

    def _m_explore(self, m: dict) -> None:
        """FRONTIER EXPLORATION (2026-07-17 — the gap the research found):
        drive to the nearest known-clear/unknown boundary, scan, let the
        nav-map daemon absorb what the sensors saw, re-read frontiers, repeat.
        Bounded by targets + overall deadline; every leg rides goto (planned,
        detoured, hazard-aware). The map literally grows as I move."""
        s = _session()
        from brain import vector_planner
        n_targets = max(1, min(int(m.get("targets") or 3), 8))
        deadline = time.time() + float(m.get("timeout_s") or 180.0)
        visited = 0
        while visited < n_targets and time.time() < deadline:
            if self.abort_evt.is_set():
                self._event("aborted", {"explored": visited})
                return
            st = dict(s._latest or {})
            if st.get("x") is None:
                self._event("error", "no pose in stream", nudge=True)
                return
            hz = read_hazards(origin=st.get("origin"))
            fr = vector_planner.frontiers((st["x"], st["y"]), hz)
            if not fr.get("ok") or not fr.get("targets"):
                self._event("explore_done",
                            {"explored": visited,
                             "note": fr.get("error") or "no frontiers left — "
                                     "known space is closed"}, nudge=True)
                return
            tx, ty = fr["targets"][0]
            self._event("frontier", {"target": [tx, ty],
                                     "remaining": len(fr["targets"])})
            m2 = dict(m)
            m2.update(x=tx, y=ty, standoff_mm=80.0,
                      timeout_s=float(m.get("timeout_s_leg") or 25.0))
            self._m_goto(m2)
            if self.abort_evt.is_set():
                self._event("aborted", {"explored": visited})
                return
            with contextlib.suppress(Exception):   # survey so the map grows
                for _ in range(4):
                    s.turn(90.0, speed_deg_s=120.0)
                    time.sleep(0.4)
            visited += 1
        self._event("explore_done", {"explored": visited}, nudge=True)

    def _m_retrace(self, m: dict) -> None:
        """ESCAPE THE WAY I CAME (2026-07-17): walk my own breadcrumb trail
        backwards — the known-clear path — instead of improvising through the
        obstacle that just stopped me. Detours OFF (the trail was driveable);
        crumbs from other pose frames (origin changed) are ignored."""
        s = _session()
        st = dict(s._latest or {})
        origin = st.get("origin")
        cx, cy = st.get("x"), st.get("y")
        crumbs = self._read_crumbs(origin)
        if not crumbs or cx is None:
            self._event("error", "no breadcrumbs in the current pose frame "
                                 "(fresh frame or no prior mission)", nudge=True)
            return
        n = max(1, min(int(m.get("steps") or 12), 40))
        pts = []
        for d in reversed(crumbs[-n:]):             # newest first = backwards
            if math.hypot(d["x"] - cx, d["y"] - cy) < 40.0:
                continue                            # skip where I already stand
            pts.append([d["x"], d["y"]])
        if not pts:
            self._event("retrace_done", {"note": "already at trail start"}, nudge=True)
            return
        self._event("retracing", {"legs": len(pts)})
        done = 0
        for pt in pts:
            if self.abort_evt.is_set():
                self._event("aborted", {"retraced": done})
                return
            r = s.servo_to(x=pt[0], y=pt[1], standoff_mm=35.0,
                           timeout_s=float(m.get("timeout_s") or 15.0),
                           abort_event=self.abort_evt)
            if r.get("aborted"):
                self._event("aborted", {"retraced": done})
                return
            if not r.get("ok"):
                self._event("blocked", {"retrace_leg": done, "res": r}, nudge=True)
                return
            done += 1
        self._event("retrace_done", {"legs": done}, nudge=True)

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

    def _m_smart_park(self, m: dict) -> None:
        """SMART-PARK (2026-07-17 round 2 — Zeke's lesson automated: 'get NEAR
        the dock, then hand the last ~2m to the stock dock behavior; it lines
        up + checks + turns in <2min — don't micromanage the parking').
        Sequence: optional approach servo (x,y = a staging point near home) →
        release POSSESSION → close MY session → stock brain parks itself →
        watch the nerves for on_charger → re-possess. Ends with the session
        CLOSED on purpose — cognition reopens with body_open when it wants
        the body back."""
        if m.get("x") is not None and m.get("y") is not None:
            s = _session()
            r = self._servo_avoid(s, m, dict(
                x=m.get("x"), y=m.get("y"),
                standoff_mm=float(m.get("standoff_mm") or 60.0),
                timeout_s=float(m.get("timeout_s") or 25.0)))
            if not r.get("ok"):
                self._event("blocked", {"phase": "smart_park approach",
                                        "res": r}, nudge=True)
                return
        self._event("smart_park", "handing the last stretch to the stock brain "
                                  "(possession released, session closed)")
        ctl = REPO / "state" / "vector" / "possession.json"
        with contextlib.suppress(Exception):
            ctl.write_text(json.dumps({"hold": False, "set_ts": time.time(),
                                       "by": "smart_park"}), encoding="utf-8")
        with contextlib.suppress(Exception):
            from brain import vector_session as vs
            vs.close_session(reason="smart-park handoff to stock brain")
        nerves_p = REPO / "state" / "vector" / "nerves.json"
        deadline = time.time() + float(m.get("wait_s") or 240.0)
        docked = False
        while time.time() < deadline and not self.abort_evt.is_set():
            with contextlib.suppress(Exception):
                n = json.loads(nerves_p.read_text(encoding="utf-8"))
                if (time.time() - float(n.get("ts", 0)) < 5.0
                        and n.get("on_charger")):
                    docked = True
                    break
            time.sleep(2.0)
        # DESIGN FLAW FIXED (2026-07-17 live: timeout re-possess STRANGLED the
        # stock brain mid-dock-search — it had the charger sighted at 1050mm
        # and the RESERVE hold killed its behavior). Only re-possess when
        # DOCKED; on timeout leave possession RELEASED so the search can
        # finish, and tell cognition exactly how to re-hold.
        if docked:
            with contextlib.suppress(Exception):
                ctl.write_text(json.dumps({"hold": True, "set_ts": time.time(),
                                           "by": "smart_park"}), encoding="utf-8")
        self._event("smart_park_result",
                    {"docked": docked,
                     "note": ("stock brain parked me; possession re-held; "
                              "session left CLOSED — body_open to re-seat"
                              if docked else
                              "NOT docked within the wait window — possession "
                              "left RELEASED so the stock brain can finish "
                              "its search (re-possess strangled it once). "
                              "Watch nerves for on_charger, then body_possess "
                              "hold=true; if it never docks it may need light")},
                    nudge=True)

    def _m_dock(self, m: dict) -> None:
        """MISSION-AWARE REFLEXES (the 23:41 double-hang etiology): docking is
        an EXPECTED close approach — but my L1 startle reflex reads the looming
        charger as 'something appeared <220mm' and backs away mid-maneuver, so
        drive_on_charger never completes and the SDK call blocks forever.
        Suspend expressive/startle reflexes for the maneuver; L0 firmware cliff
        stop and the session guard stay armed. Restore is in finally — a hang
        or exception can never leave me reflex-dead."""
        s = _session()
        # PRE-DOCK GATE (2026-07-17 live-test): a dock started with the charger
        # UNKNOWN to the engine hangs drive_on_charger forever (etiology c).
        # Enforce THE DOCK RECIPE for every caller — policy rules included:
        # refuse fast with instructions instead of blocking 90s. Stale-but-known
        # sightings proceed (engine homes on remembered pose, vision-locks
        # close in) but the event notes the staleness.
        ch = None
        seen_ago = None
        try:
            ch = s.robot.world.charger
            if ch is not None:
                seen_ago = float(getattr(ch, "time_since_last_seen", -1.0))
        except Exception:
            ch = None
        if ch is None:
            self._event("dock_result",
                        {"ok": False,
                         "refused": ("charger NOT known this connection — "
                                     "drive_on_charger would hang forever. "
                                     "Face the dock with marker vision on "
                                     "(body_charger until known+fresh), then "
                                     "re-dock")},
                        nudge=True)
            return
        note = ("drive_on_charger started (reflexes suspended + control yielded)"
                + (f" — charger sighting {seen_ago:.0f}s old; stall-at-alignment "
                   f"risk rises with stale sightings/dim light"
                   if seen_ago is not None and seen_ago > 10.0 else ""))
        self._event("docking", note)
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
