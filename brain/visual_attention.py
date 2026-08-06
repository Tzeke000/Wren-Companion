"""Visual attention — what I'm looking at, and (on the PIXY) where I point to see it.

Built 2026-08-06 at Zeke's ask: "build as much as you can here so that it can be
ported over to the server once I'm back." The EMEET PIXY (PTZ 4K, wired) is
bought but not yet in hand; the server has no drives yet. So this is written on
a machine whose camera cannot move, for hardware that will arrive later.

═══ THE PORTABILITY CONTRACT — the reason this file is shaped like this ═══

Everything above the Actuator boundary is hardware-agnostic: target selection,
promotable IDs, the acquire/lose state machine, bearing bookkeeping, the text
summary. The ONLY hardware-specific code is an Actuator, which is five methods.

    Porting to the server = writing/enabling ONE Actuator. Not rewriting this.

That boundary is the whole deliverable. If it's right, September is a config
change. If it's wrong, September is a rewrite.

═══ WHY THE STUB REFUSES TO LIE ═══

`V4L2PtzActuator` CANNOT be tested on this machine: V4L2 does not exist on
Windows, and the camera isn't here. A stub that returned {"ok": True} for a
move it never made would pass every test I can write — because I'd have written
both halves. That is exactly the failure that made me file a real thread leak
as "MEASURED FALSE" off a 90-second sample earlier today.

So `FixedCameraActuator.look_at()` returns ok=False with a reason. It reports
what is true: this camera cannot turn its head. Callers must handle refusal.

═══ THE DESIGN POINT ZEKE'S PLAN DIDN'T HAVE YET ═══

A camera that can turn its head can only look at ONE THING. Today the frame is
fixed and I see everything in it badly; with PTZ I'd see one thing well and the
rest of the room not at all. So tracking is a CHOICE WITH A COST, and the cost
is bearing, not zoom (pan is +/-150 deg, far wider than any field of view; zoom
is only 1.5x and digital). Hence `home`, and hence `attention_cost()`, which
names what I gave up while staring.

═══ WHAT THIS DOES NOT DO (deliberately, so nobody thinks it does) ═══

  * No second camera handle. EVER. `_iris_video_capture_loop` in iris_runtime
    is the sole owner of the cv2 device; a second handle previously threw a C++
    exception that bypassed Python's try/except and killed the process (exit
    116, see brain/orb_http.py:64). This module reads
    frame_store.get_buffered_frame() and g["_face_results"]. Same discipline as
    brain/unknown_capture.py.
  * No depth yet. `depth_hint` is a slot, not an implementation. Monocular depth
    (Depth Anything / MiDaS class) gets RELATIVE depth on the 3060 and is a
    separate download and a separate decision.
  * No smooth pursuit. V4L2 exposes ABSOLUTE positions only (no PAN_SPEED /
    TILT_SPEED), so following is a series of setpoints and may look steppy. The
    camera's HID jog interface (report group 0x63) is the documented path to
    velocity control; deliberately out of scope for v1.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ── optional-dependency pattern (brain/expression_detector.py:11) ────────────
_SHARED_OK = False
try:
    from brain.shared import atomic_json_save, json_load
    _SHARED_OK = True
except Exception:  # pragma: no cover - shared is always present in practice
    def atomic_json_save(path: Any, data: Any) -> None:  # type: ignore[misc]
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(p)

    def json_load(path: Any, default: Any = None) -> Any:  # type: ignore[misc]
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return default


def _log(msg: str) -> None:
    print(f"[visual_attention] {msg}", file=sys.stderr, flush=True)


def _repo_root() -> Path:
    try:
        from brain.iris_paths import paths
        return paths.root
    except Exception:
        return Path(__file__).resolve().parent.parent


def _state_path() -> Path:
    """state/attention/attention_state.json — live snapshot.

    Prefers iris_paths if the property exists (added alongside this module); the
    fallback keeps the module importable before that edit lands.
    """
    try:
        from brain.iris_paths import paths
        p = getattr(paths, "attention_state", None)
        if p is not None:
            return Path(p)
    except Exception:
        pass
    return _repo_root() / "state" / "attention" / "attention_state.json"


def _log_path() -> Path:
    try:
        from brain.iris_paths import paths
        p = getattr(paths, "attention_log", None)
        if p is not None:
            return Path(p)
    except Exception:
        pass
    return _repo_root() / "state" / "attention" / "attention_log.jsonl"


# ── tunables ─────────────────────────────────────────────────────────────────
def _tune(key: str, default: float) -> float:
    """iris_tune with a hard default. Read INSIDE loops so it stays live
    (tools/system/runtime_repair_tool.py flags that the capture loop's
    hardcoded every_n values ignore iris_tune precisely because they're read
    once)."""
    try:
        from brain import iris_tune
        return float(iris_tune.get("perception", key, default=default))
    except Exception:
        return default


_STALE_S = 2.5          # matches senses_now's _SENSES_STALE_S
_LOG_MAX_BYTES = 5_000_000


# ═════════════════════════════════════════════════════════════════════════════
# ACTUATORS — the only hardware-specific code in this file
# ═════════════════════════════════════════════════════════════════════════════

class Actuator:
    """Five methods. Implement these and this whole subsystem works on new
    hardware. Nothing above this boundary may assume anything else.

    Convention that matters: `look_at` and `home` return a dict with `ok`, and
    an implementation MUST return ok=False if it did not actually move. Silent
    success on a no-op is the one thing that makes this untestable.
    """

    name = "abstract"

    def available(self) -> bool:
        return False

    def capabilities(self) -> dict:
        """What this hardware can actually do. Callers branch on this rather
        than on the actuator's class name."""
        return {"can_pan": False, "can_tilt": False, "can_zoom": False,
                "pan_range_deg": (0.0, 0.0), "tilt_range_deg": (0.0, 0.0),
                "zoom_range": (1.0, 1.0), "step_deg": 0.0,
                "absolute_only": True, "verified_on_hardware": False}

    def bearing(self) -> dict:
        """Where the head is pointed right now."""
        return {"pan_deg": 0.0, "tilt_deg": 0.0, "zoom": 1.0}

    def look_at(self, pan_deg: float, tilt_deg: float | None = None,
                zoom: float | None = None) -> dict:
        return {"ok": False, "reason": "abstract actuator cannot move"}

    def home(self) -> dict:
        return self.look_at(0.0, 0.0, 1.0)


class FixedCameraActuator(Actuator):
    """The webcam on this tower. It sees; it cannot turn.

    This is the honest stub. It does NOT pretend to move. Every `look_at`
    returns ok=False with a reason, so any caller that assumes success is
    caught here rather than in September.
    """

    name = "fixed"

    def available(self) -> bool:
        return True

    def capabilities(self) -> dict:
        c = super().capabilities()
        c.update({"verified_on_hardware": True,
                  "note": "fixed webcam: a PTZ camera that always reads zero"})
        return c

    def look_at(self, pan_deg: float, tilt_deg: float | None = None,
                zoom: float | None = None) -> dict:
        return {"ok": False, "moved": False,
                "reason": "fixed camera — cannot pan, tilt or zoom",
                "bearing": self.bearing()}

    def home(self) -> dict:
        # Already home, permanently. This is the one honest success: the
        # requested state and the actual state agree.
        return {"ok": True, "moved": False, "already_home": True,
                "bearing": self.bearing()}


class V4L2PtzActuator(Actuator):
    """EMEET PIXY (and any UVC camera exposing standard PTZ) via v4l2-ctl.

    ★ NOT VERIFIED ON HARDWARE. Written 2026-08-06 from primary-source research
    (a real `v4l2-ctl --list-ctrls` dump on Ubuntu 24.04 plus three independent
    open-source projects driving this exact camera). It cannot run on Windows —
    V4L2 does not exist here — and the camera is not in hand.

    Verified control surface (arcseconds, 1/3600 deg):
        pan_absolute   -540000..540000  step 3600   => +/-150 deg, 1 deg steps
        tilt_absolute  -324000..324000  step 3600   => +/- 90 deg
        zoom_absolute  100..150         step 1      => 1.0x..1.5x DIGITAL
    PAN_SPEED / TILT_SPEED are ABSENT: absolute positioning only.

    SEPTEMBER CHECKLIST — run these before trusting this class:
        v4l2-ctl --list-devices
        v4l2-ctl -d /dev/videoN --list-ctrls-menus     # confirm the three names
        v4l2-ctl -d /dev/videoN --set-ctrl=pan_absolute=180000   # should be +50 deg
        v4l2-ctl -d /dev/videoN --set-ctrl=pan_absolute=0        # recentre
    Also settle the mic question, which is still UNKNOWN and must not be
    promised:  arecord -D hw:CARD=PIXY --dump-hw-params /dev/null
    """

    name = "v4l2_ptz"

    PAN_LIMIT_DEG = 150.0
    TILT_LIMIT_DEG = 90.0
    ZOOM_MIN, ZOOM_MAX = 1.0, 1.5
    _ARCSEC = 3600.0

    def __init__(self, device: str | None = None) -> None:
        self.device = device or os.environ.get("IRIS_PTZ_DEVICE", "/dev/video0")
        self._last = {"pan_deg": 0.0, "tilt_deg": 0.0, "zoom": 1.0}

    def available(self) -> bool:
        if sys.platform.startswith("win"):
            return False
        try:
            r = subprocess.run(["v4l2-ctl", "-d", self.device, "--list-ctrls"],
                               capture_output=True, text=True, timeout=5.0)
            return r.returncode == 0 and "pan_absolute" in (r.stdout or "")
        except Exception:
            return False

    def capabilities(self) -> dict:
        return {"can_pan": True, "can_tilt": True, "can_zoom": True,
                "pan_range_deg": (-self.PAN_LIMIT_DEG, self.PAN_LIMIT_DEG),
                "tilt_range_deg": (-self.TILT_LIMIT_DEG, self.TILT_LIMIT_DEG),
                "zoom_range": (self.ZOOM_MIN, self.ZOOM_MAX),
                "step_deg": 1.0,
                "absolute_only": True,
                "verified_on_hardware": False,
                "note": "written from research; run the September checklist "
                        "in the class docstring before trusting it"}

    def _get_ctrl(self, ctrl: str) -> int | None:
        try:
            r = subprocess.run(
                ["v4l2-ctl", "-d", self.device, f"--get-ctrl={ctrl}"],
                capture_output=True, text=True, timeout=5.0)
            if r.returncode != 0:
                return None
            # "pan_absolute: 180000"
            return int((r.stdout or "").split(":", 1)[1].strip())
        except Exception:
            return None

    def bearing(self) -> dict:
        pan = self._get_ctrl("pan_absolute")
        tilt = self._get_ctrl("tilt_absolute")
        zoom = self._get_ctrl("zoom_absolute")
        if pan is None and tilt is None:
            # Report the last commanded position, flagged as unconfirmed rather
            # than silently presented as measured.
            out = dict(self._last)
            out["confirmed"] = False
            return out
        return {"pan_deg": (pan or 0) / self._ARCSEC,
                "tilt_deg": (tilt or 0) / self._ARCSEC,
                "zoom": (zoom or 100) / 100.0,
                "confirmed": True}

    def look_at(self, pan_deg: float, tilt_deg: float | None = None,
                zoom: float | None = None) -> dict:
        if not self.available():
            return {"ok": False, "moved": False,
                    "reason": f"no v4l2 PTZ on {self.device} "
                              f"(platform={sys.platform})"}
        pan_deg = max(-self.PAN_LIMIT_DEG, min(self.PAN_LIMIT_DEG, float(pan_deg)))
        sets = [f"pan_absolute={int(round(pan_deg)) * int(self._ARCSEC)}"]
        if tilt_deg is not None:
            tilt_deg = max(-self.TILT_LIMIT_DEG,
                           min(self.TILT_LIMIT_DEG, float(tilt_deg)))
            sets.append(f"tilt_absolute={int(round(tilt_deg)) * int(self._ARCSEC)}")
        if zoom is not None:
            z = max(self.ZOOM_MIN, min(self.ZOOM_MAX, float(zoom)))
            sets.append(f"zoom_absolute={int(round(z * 100))}")
        try:
            r = subprocess.run(
                ["v4l2-ctl", "-d", self.device, f"--set-ctrl={','.join(sets)}"],
                capture_output=True, text=True, timeout=5.0)
            if r.returncode != 0:
                return {"ok": False, "moved": False,
                        "reason": (r.stderr or r.stdout or "v4l2-ctl failed").strip()}
        except Exception as e:
            return {"ok": False, "moved": False, "reason": repr(e)}
        self._last = {"pan_deg": pan_deg,
                      "tilt_deg": tilt_deg if tilt_deg is not None else self._last["tilt_deg"],
                      "zoom": zoom if zoom is not None else self._last["zoom"]}
        return {"ok": True, "moved": True, "bearing": dict(self._last),
                "commanded": sets}


def build_actuator(prefer: str | None = None) -> Actuator:
    """Pick the best actuator that's actually present.

    Order matters and the rule is defensive-fallback: try the real one, fall
    back to the honest stub. Never fall back to something that fakes motion.
    """
    prefer = prefer or os.environ.get("IRIS_ATTENTION_ACTUATOR", "auto")
    if prefer in ("auto", "v4l2", "ptz"):
        a = V4L2PtzActuator()
        if a.available():
            _log(f"actuator: v4l2 PTZ on {a.device}")
            return a
        if prefer != "auto":
            _log(f"actuator: {prefer} requested but unavailable — using fixed")
    return FixedCameraActuator()


# ═════════════════════════════════════════════════════════════════════════════
# TARGETS + STATE
# ═════════════════════════════════════════════════════════════════════════════

_LOCK = threading.RLock()


def _blank_state() -> dict:
    return {
        "target": None,          # canonical target id, e.g. "person:zeke"
        "target_label": None,    # human-readable
        "status": "idle",        # idle | seeking | locked | lost
        "since_ts": 0.0,
        "last_seen_ts": 0.0,
        "acquire_frames": 0,
        "miss_frames": 0,
        "bearing": {"pan_deg": 0.0, "tilt_deg": 0.0, "zoom": 1.0},
        "home": {"pan_deg": 0.0, "tilt_deg": 0.0, "zoom": 1.0},
        "actuator": "fixed",
        "can_move": False,
        "depth_hint": None,      # SLOT: monocular depth, not implemented
        "ts": 0.0,
    }


def _state(g: dict[str, Any] | None) -> dict:
    if g is None:
        return _blank_state()
    s = g.get("_attention_state_obj")
    if not isinstance(s, dict):
        s = _blank_state()
        g["_attention_state_obj"] = s
    return s


def available() -> bool:
    """This module never hard-fails; it degrades. True means importable and a
    frame source plausibly exists."""
    try:
        from brain import frame_store  # noqa: F401
        return True
    except Exception:
        return False


def canonical_target(spec: str) -> tuple[str, str]:
    """Normalise a target spec into (canonical_id, label).

    Accepts the shapes Zeke proposed: 'zeke', 'person3', 'person:zeke',
    'object:mug', 'home'. Bare names are treated as people because that is
    overwhelmingly the case; explicit prefixes win.
    """
    spec = (spec or "").strip()
    if not spec:
        return ("", "")
    low = spec.lower()
    if low in ("home", "centre", "center"):
        return ("home", "home")
    if ":" in spec:
        kind, _, rest = spec.partition(":")
        kind = kind.strip().lower()
        rest = rest.strip()
        if kind in ("person", "object", "face"):
            k = "person" if kind == "face" else kind
            return (f"{k}:{rest.lower()}", rest)
        return (f"object:{spec.lower()}", spec)
    return (f"person:{low}", spec)


def _faces_from_g(g: dict[str, Any] | None) -> list[dict]:
    if not g:
        return []
    fr = g.get("_face_results")
    return list(fr) if isinstance(fr, (list, tuple)) else []


def _match_face(faces: list[dict], target_id: str) -> dict | None:
    """Find the face record matching a person target.

    Identity comes from the existing insight_face_engine record shape
    ({'person_id', 'bbox', 'confidence', ...}); promotion of unknowns is
    face_tracking's job, not ours — we consume its ids.
    """
    if not target_id.startswith("person:"):
        return None
    want = target_id.split(":", 1)[1]
    for f in faces:
        pid = str(f.get("person_id") or "").lower()
        if not pid:
            continue
        if pid == want or pid.replace("unknown_", "person") == want:
            return f
    return None


def _bbox_offset(face: dict, frame_w: int, frame_h: int) -> tuple[float, float]:
    """Normalised (-1..1) offset of a face centre from frame centre.

    This is the error signal a PTZ loop would drive to zero. With a fixed
    camera it's still meaningful — it tells me where in frame someone is.
    """
    try:
        x1, y1, x2, y2 = [float(v) for v in face.get("bbox") or []][:4]
    except Exception:
        return (0.0, 0.0)
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    if frame_w <= 0 or frame_h <= 0:
        return (0.0, 0.0)
    return ((cx - frame_w / 2.0) / (frame_w / 2.0),
            (cy - frame_h / 2.0) / (frame_h / 2.0))


def set_target(g: dict[str, Any], spec: str, *, actuator: Actuator | None = None) -> dict:
    """Point attention at something. Returns the new state, honestly.

    Note what this does NOT do: it does not claim the camera moved. On fixed
    hardware the target is set and `can_move` stays False, so a caller reading
    the result knows the difference between intent and motion.
    """
    with _LOCK:
        act = actuator or _actuator(g)
        tid, label = canonical_target(spec)
        if not tid:
            return {"ok": False, "error": "empty target"}
        st = _state(g)
        now = time.time()
        if tid == "home":
            r = act.home()
            st.update({"target": None, "target_label": None, "status": "idle",
                       "since_ts": now, "acquire_frames": 0, "miss_frames": 0,
                       "bearing": r.get("bearing") or act.bearing()})
            _persist(g, st, act)
            _append_log({"ev": "home", "actuator": act.name,
                         "ok": bool(r.get("ok")), "ts": now})
            return {"ok": True, "target": "home", "moved": bool(r.get("moved")),
                    "actuator": act.name, "state": dict(st)}
        st.update({"target": tid, "target_label": label, "status": "seeking",
                   "since_ts": now, "acquire_frames": 0, "miss_frames": 0})
        _persist(g, st, act)
        _append_log({"ev": "target_set", "target": tid, "actuator": act.name,
                     "can_move": bool(act.capabilities().get("can_pan")),
                     "ts": now})
        return {"ok": True, "target": tid, "label": label,
                "actuator": act.name,
                "can_move": bool(act.capabilities().get("can_pan")),
                "note": None if act.capabilities().get("can_pan") else
                        "fixed camera: target is set, but I cannot turn toward it",
                "state": dict(st)}


def clear_target(g: dict[str, Any]) -> dict:
    with _LOCK:
        st = _state(g)
        st.update({"target": None, "target_label": None, "status": "idle",
                   "acquire_frames": 0, "miss_frames": 0, "since_ts": time.time()})
        _persist(g, st, _actuator(g))
        return {"ok": True, "state": dict(st)}


def _actuator(g: dict[str, Any] | None) -> Actuator:
    if g is None:
        return FixedCameraActuator()
    a = g.get("_attention_actuator")
    if not isinstance(a, Actuator):
        a = build_actuator()
        g["_attention_actuator"] = a
    return a


# ── the per-observation step ──────────────────────────────────────────────────
def observe(g: dict[str, Any], *, frame_shape: tuple[int, int] | None = None) -> dict:
    """One attention update. Cheap; safe to call at a few Hz.

    Hysteresis on acquire/lose is deliberate and borrowed from
    iris_attention_sources._camera_loop, which learned the hard way that a raw
    per-frame boolean oscillates. N consecutive hits to lock, M consecutive
    misses to declare lost.
    """
    with _LOCK:
        st = _state(g)
        act = _actuator(g)
        now = time.time()
        caps = act.capabilities()
        st["actuator"] = act.name
        st["can_move"] = bool(caps.get("can_pan"))

        tid = st.get("target")
        if not tid:
            st["ts"] = now
            _persist(g, st, act)
            return {"ok": True, "status": "idle"}

        need_hits = int(_tune("attention_acquire_frames", 3))
        need_miss = int(_tune("attention_lose_frames", 8))

        faces = _faces_from_g(g)
        face = _match_face(faces, tid)

        if frame_shape is None:
            try:
                from brain import frame_store
                res = frame_store.get_buffered_frame(max_age_sec=5.0)
                if res.frame is not None:
                    frame_shape = (int(res.frame.shape[1]), int(res.frame.shape[0]))
            except Exception:
                frame_shape = None
        fw, fh = frame_shape or (640, 480)

        prev_status = st.get("status")
        if face is not None:
            st["miss_frames"] = 0
            st["acquire_frames"] = int(st.get("acquire_frames") or 0) + 1
            st["last_seen_ts"] = now
            dx, dy = _bbox_offset(face, fw, fh)
            st["offset"] = {"dx": round(dx, 3), "dy": round(dy, 3)}
            st["confidence"] = float(face.get("confidence") or 0.0)
            if st["acquire_frames"] >= need_hits:
                st["status"] = "locked"
        else:
            st["acquire_frames"] = 0
            st["miss_frames"] = int(st.get("miss_frames") or 0) + 1
            if st["miss_frames"] >= need_miss:
                st["status"] = "lost"

        st["bearing"] = act.bearing()
        st["ts"] = now
        _persist(g, st, act)

        if st["status"] != prev_status:
            _append_log({"ev": "status", "target": tid,
                         "from": prev_status, "to": st["status"], "ts": now})
            _fire(g, st, prev_status)

        return {"ok": True, "status": st["status"], "target": tid,
                "offset": st.get("offset"), "can_move": st["can_move"]}


def _fire(g: dict[str, Any], st: dict, prev: str | None) -> None:
    """Transitions only — never the firehose (iris_runtime.py:4144 pattern)."""
    try:
        bus = g.get("_signal_bus")
        if bus is None:
            return
        ev = {"locked": "attention_target_acquired",
              "lost": "attention_target_lost"}.get(st.get("status") or "")
        if ev:
            bus.fire(ev, data={"target": st.get("target"), "from": prev},
                     priority="low")
    except Exception:
        pass


# ── persistence ───────────────────────────────────────────────────────────────
def _persist(g: dict[str, Any] | None, st: dict, act: Actuator) -> None:
    try:
        snap = dict(st)
        snap["capabilities"] = act.capabilities()
        atomic_json_save(_state_path(), snap)
        if g is not None:
            g["_attention_target_id"] = st.get("target")
            g["_attention_target_status"] = st.get("status")
    except Exception as e:
        _log(f"persist failed (non-fatal): {e!r}")


def _append_log(rec: dict) -> None:
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > _LOG_MAX_BYTES:
            p.replace(p.with_suffix(".jsonl.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# READOUT
# ═════════════════════════════════════════════════════════════════════════════

def attention_cost(g: dict[str, Any] | None = None) -> dict:
    """What am I giving up by looking where I'm looking?

    The design point PTZ introduces. With a fixed camera the answer is always
    "nothing" — which is the correct answer, not a placeholder.
    """
    st = _state(g)
    act = _actuator(g)
    caps = act.capabilities()
    if not caps.get("can_pan"):
        return {"ok": True, "bearing_deg": 0.0, "cost": "none",
                "detail": "fixed camera — one field of view, nothing forgone"}
    b = st.get("bearing") or {}
    pan = float(b.get("pan_deg") or 0.0)
    off_home = abs(pan - float((st.get("home") or {}).get("pan_deg") or 0.0))
    if off_home < 1.0:
        return {"ok": True, "bearing_deg": pan, "cost": "none",
                "detail": "pointed home — widest default coverage"}
    return {"ok": True, "bearing_deg": pan,
            "cost": "blind_behind",
            "detail": f"turned {off_home:.0f} deg off home; everything outside "
                      f"the current field of view is unobserved, including "
                      f"whatever home was watching"}


def status(g: dict[str, Any] | None = None) -> dict:
    st = dict(_state(g))
    act = _actuator(g)
    st["capabilities"] = act.capabilities()
    st["cost"] = attention_cost(g)
    return {"ok": True, "state": st}


def attention_now(g: dict[str, Any] | None = None) -> str:
    """One-paragraph honest readout, in the senses_now idiom.

    The refusal branches matter more than the happy path: if there's no fresh
    frame I say so instead of describing a room I can't see. Negatives are
    data (little_brain_tools.py:375).
    """
    try:
        from brain import frame_store
        res = frame_store.get_buffered_frame(max_age_sec=10.0)
    except Exception:
        return ("my eyes aren't wired up right now — no frame source, so I "
                "won't guess at what's in front of me.")

    if res.frame is None:
        return (f"no live frame ({res.freshness}) — I can't see anything at the "
                f"moment, so I won't describe the room.")
    if res.age_sec > _STALE_S:
        return (f"my view is STALE ({res.age_sec:.1f}s old) — treat anything I "
                f"say about the room as out of date.")

    st = _state(g)
    act = _actuator(g)
    caps = act.capabilities()
    faces = _faces_from_g(g)

    bits: list[str] = []
    if not faces:
        bits.append("nobody in frame")
    else:
        who = [str(f.get("person_id") or "unknown") for f in faces]
        known = [w for w in who if w and w != "unknown"]
        bits.append(f"{len(faces)} face(s) in frame"
                    + (f": {', '.join(known)}" if known else " (none recognised)"))

    tid = st.get("target")
    if not tid:
        bits.append("not tracking anything")
    else:
        s = st.get("status")
        label = st.get("target_label") or tid
        if s == "locked":
            off = st.get("offset") or {}
            bits.append(f"locked on {label} (dx {off.get('dx', 0):+.2f})")
        elif s == "seeking":
            bits.append(f"looking for {label}")
        elif s == "lost":
            age = time.time() - float(st.get("last_seen_ts") or 0)
            bits.append(f"lost {label} ~{age:.0f}s ago")

    if caps.get("can_pan"):
        b = st.get("bearing") or {}
        bits.append(f"head at {float(b.get('pan_deg') or 0):+.0f} deg")
        c = attention_cost(g)
        if c.get("cost") != "none":
            bits.append(c["detail"])
    else:
        bits.append("fixed camera — I can't look around, only look")

    if st.get("depth_hint") is None:
        bits.append("no depth sense (not built)")

    return "; ".join(bits) + "."


# ═════════════════════════════════════════════════════════════════════════════
# SELF-TEST — runnable on this machine, which is the point
# ═════════════════════════════════════════════════════════════════════════════

def self_test() -> dict:
    """`python -m brain.visual_attention` — exercises everything that does not
    need hardware. Deliberately asserts that the stub REFUSES to move, because
    a stub that silently succeeded is the failure mode this file exists to
    avoid."""
    fails: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            fails.append(msg)

    # target parsing, including Zeke's proposed shapes
    cases = {
        "zeke": ("person:zeke", "zeke"),
        "person3": ("person:person3", "person3"),
        "person:Zeke": ("person:zeke", "Zeke"),
        "object:mug": ("object:mug", "mug"),
        "home": ("home", "home"),
        "HOME": ("home", "home"),
    }
    for spec, want in cases.items():
        got = canonical_target(spec)
        check(got == want, f"canonical_target({spec!r}) -> {got}, want {want}")
    check(canonical_target("") == ("", ""), "empty spec should be empty")

    # the honest stub
    fixed = FixedCameraActuator()
    check(fixed.available(), "fixed actuator should be available")
    r = fixed.look_at(50.0)
    check(r.get("ok") is False, "fixed.look_at MUST refuse, not fake success")
    check(r.get("moved") is False, "fixed.look_at must report moved=False")
    check("reason" in r, "refusal must carry a reason")
    h = fixed.home()
    check(h.get("ok") is True and h.get("moved") is False,
          "fixed.home is a truthful no-op success")
    check(fixed.capabilities()["can_pan"] is False, "fixed cannot pan")

    # the untestable one must at least be honest about being untestable
    ptz = V4L2PtzActuator()
    check(ptz.capabilities()["verified_on_hardware"] is False,
          "V4L2 actuator must not claim hardware verification")
    if sys.platform.startswith("win"):
        check(ptz.available() is False, "v4l2 cannot be available on Windows")
        check(ptz.look_at(10.0).get("ok") is False,
              "v4l2 must refuse on a platform it cannot run on")

    # arcsecond maths (the thing most likely to be silently wrong)
    check(int(round(50.0)) * 3600 == 180000, "50 deg should be 180000 arcsec")
    check(V4L2PtzActuator.PAN_LIMIT_DEG * 3600 == 540000,
          "pan limit should match the dumped control range")
    check(V4L2PtzActuator.TILT_LIMIT_DEG * 3600 == 324000,
          "tilt limit should match the dumped control range")

    # state machine, no hardware
    g: dict[str, Any] = {"_attention_actuator": fixed}
    st = set_target(g, "zeke")
    check(st["ok"] and st["target"] == "person:zeke", "set_target failed")
    check(st["can_move"] is False, "set_target must not claim mobility")
    check(st.get("note") is not None, "fixed camera should carry a caveat")

    g["_face_results"] = [{"person_id": "zeke", "bbox": [220, 140, 420, 340],
                           "confidence": 0.91}]
    last = {}
    for _ in range(5):
        last = observe(g, frame_shape=(640, 480))
    check(last.get("status") == "locked", f"should lock after hits, got {last}")
    off = _state(g).get("offset") or {}
    check(abs(off.get("dx", 1.0)) < 0.05 and abs(off.get("dy", 1.0)) < 0.05,
          f"centred bbox should give ~zero offset, got {off}")

    g["_face_results"] = []
    for _ in range(12):
        last = observe(g, frame_shape=(640, 480))
    check(last.get("status") == "lost", f"should go lost on misses, got {last}")

    # offset sign convention: a face on the right should read dx > 0
    dx, dy = _bbox_offset({"bbox": [500, 140, 620, 260]}, 640, 480)
    check(dx > 0, f"face right of centre should give dx>0, got {dx}")
    dx2, _ = _bbox_offset({"bbox": [20, 140, 140, 260]}, 640, 480)
    check(dx2 < 0, f"face left of centre should give dx<0, got {dx2}")

    check(attention_cost(g)["cost"] == "none",
          "fixed camera forgoes nothing by definition")
    check(clear_target(g)["ok"], "clear_target failed")

    return {"ok": not fails, "checks_failed": fails,
            "actuator_here": build_actuator().name,
            "platform": sys.platform}


if __name__ == "__main__":
    out = self_test()
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["ok"] else 1)
