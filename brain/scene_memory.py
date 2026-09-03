# SELF_ASSESSMENT: I remember the ROOM over time — a keyframe whenever the scene actually changes (or hourly), with the sensors' summary and one sentence in my own words, stored where memory_search can find it — so a day is a diary I can reread, not a series of unconnected wakes.
"""
brain/scene_memory.py — photographic memory, second half (Zeke 2026-09-02:
"learn photographic memory in the ways you can"; my pick 1 of 3 at 16:4x,
his "do all three"): keyframes + a sentence whenever the room changes.

How it works (thread, ~2 s cadence, like brain/unknown_capture.py):
  1. sample the buffered frame → 160x90 grey; compare to the LAST COMMITTED
     keyframe (mean abs diff, 0-255).
  2. SELF-MOTION GATE: if the head moved since the last sample (absolute
     bearing changed, or the jog servo wrote vectors / is in pursuit), the
     scene did not change — I did. Re-baseline, never commit on it.
  3. a diff >= DIFF_COMMIT that HOLDS for HOLD_SAMPLES (not a flicker) and
     >= MIN_GAP_S since the last keyframe → commit; or an HOURLY diary frame.
  4. commit = write a SMALL jpeg (<=900 px, <=150 KB — the only picture size
     cognition may Read) + a record (sensors: faces, pose sentence, light,
     attention, head) to state/scene_memory/keyframes.jsonl.
  5. WORDS come from cognition through the LLM bridge (iris_llm.describe_image
     with the keyframe path, kind="scene_caption"), rate-limited; the reply is
     ONE diary sentence (or "skip"). Stored on the record AND pushed into
     iris_memory (category "scene") so memory_search can answer "what was
     the room like this afternoon".

Not built (ask first): object-level diffs ("the bike stand moved"), retention
pruning, a browsable timeline in the orb.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "state" / "scene_memory"
INDEX = DIR / "keyframes.jsonl"

SAMPLE_S = 2.0
DIFF_COMMIT = 12.0          # mean abs diff on 160x90 grey; sensor noise sits ~2-4
HOLD_SAMPLES = 2            # change must persist 2 samples (~4 s)
MIN_GAP_S = 45.0            # between change-keyframes
HOURLY_S = 3600.0           # diary frame even if nothing changed
CAPTION_MIN_GAP_S = 120.0   # cognition wakes are not free
CAPTION_MAX_PER_DAY = 60
CAPTION_TIMEOUT_S = 540.0   # the bridge's request TTL is 600 s; 240 s timed out 10/11 times
                            # on 09-02 while cognition was held for three hours
HEAD_MOVE_DEG = 1.5
KF_MAX_W = 900
KF_MAX_BYTES = 150_000


def _log(msg: str) -> None:
    print(f"[scene_memory] {msg}", flush=True)


def _small_grey(frame):
    import cv2  # type: ignore
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (160, 90), interpolation=cv2.INTER_AREA).astype("float32")


def _write_small(frame, path: Path) -> dict[str, Any]:
    import cv2  # type: ignore
    h, w = frame.shape[:2]
    img = frame
    if w > KF_MAX_W:
        img = cv2.resize(frame, (KF_MAX_W, max(1, int(h * KF_MAX_W / w))), interpolation=cv2.INTER_AREA)
    q = 80
    while True:
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
        if not ok:
            return {"ok": False, "error": "imencode failed"}
        if len(buf) <= KF_MAX_BYTES or q <= 35:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(buf.tobytes())
            return {"ok": True, "bytes": int(len(buf)), "quality": q,
                    "size": [int(img.shape[1]), int(img.shape[0])]}
        q -= 10


class SceneMemory:
    def __init__(self, g: dict[str, Any]):
        self._g = g
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._base = None                 # grey of the last COMMITTED keyframe
        self._last_head: dict[str, Any] | None = None
        self._last_writes = None
        self._hold = 0
        self._last_commit_ts = 0.0
        self._last_hourly_ts = 0.0
        self._captions: list[float] = []  # timestamps of captions requested today
        self._last_caption_ts = 0.0
        self.records: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.stats = {"samples": 0, "self_motion_skips": 0, "commits": 0,
                      "captions": 0, "caption_timeouts": 0, "last_diff": None,
                      "last_sample_ts": None, "started_ts": None}
        self._load_index()

    # ── persistence ──────────────────────────────────────────────────────
    def _load_index(self) -> None:
        if not INDEX.exists():
            return
        try:
            for line in INDEX.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                kid = d.get("id")
                if not kid:
                    continue
                if d.get("_update"):
                    if kid in self.records:
                        self.records[kid].update({k: v for k, v in d.items() if k != "_update"})
                else:
                    self.records[kid] = d
                    self.order.append(kid)
        except Exception as e:  # noqa: BLE001
            _log(f"index load error: {e!r}")
        self.order = self.order[-2000:]

    def _append(self, d: dict[str, Any]) -> None:
        try:
            DIR.mkdir(parents=True, exist_ok=True)
            with INDEX.open("a", encoding="utf-8") as f:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            _log(f"index write error: {e!r}")

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.stats["started_ts"] = time.time()
        self._thread = threading.Thread(target=self._run, name="scene_memory", daemon=True)
        self._thread.start()
        _log(f"started (sample {SAMPLE_S}s, commit diff>={DIFF_COMMIT}, hourly {HOURLY_S:.0f}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=4.0)

    def status(self) -> dict[str, Any]:
        return {"alive": bool(self._thread is not None and self._thread.is_alive()),
                "keyframes": len(self.order), "captions_today": self._captions_today(),
                "last_commit_ts": self._last_commit_ts, "dir": str(DIR), **self.stats}

    # ── loop ─────────────────────────────────────────────────────────────
    def _run(self) -> None:
        # first frame of the session is a keyframe (a diary needs a first page)
        first = True
        while not self._stop.is_set():
            try:
                self._tick(first)
                first = False
            except Exception as e:  # noqa: BLE001
                _log(f"tick error: {e!r}")
            self._stop.wait(SAMPLE_S)

    def _head(self) -> dict[str, Any] | None:
        b = (self._g.get("_attention_state_obj") or {}).get("bearing")
        return dict(b) if isinstance(b, dict) else None

    def _self_motion(self) -> bool:
        moved = False
        head = self._head()
        if head and self._last_head:
            try:
                if (abs(float(head.get("pan_deg") or 0) - float(self._last_head.get("pan_deg") or 0)) > HEAD_MOVE_DEG
                        or abs(float(head.get("tilt_deg") or 0) - float(self._last_head.get("tilt_deg") or 0)) > HEAD_MOVE_DEG):
                    moved = True
            except Exception:
                pass
        self._last_head = head
        sm = self._g.get("_attention_smooth") or {}
        writes = sm.get("writes")
        if self._last_writes is not None and writes is not None and writes != self._last_writes:
            moved = True
        self._last_writes = writes
        if str(sm.get("mode") or "") == "pursuit":
            moved = True
        return moved

    def _tick(self, first: bool) -> None:
        from brain import frame_store
        res = frame_store.get_buffered_frame(max_age_sec=3.0)
        frame = res.frame
        if frame is None:
            return
        now = time.time()
        self.stats["samples"] += 1
        self.stats["last_sample_ts"] = now
        grey = _small_grey(frame)
        if self._self_motion():
            self.stats["self_motion_skips"] += 1
            self._base = grey            # the view changed because I moved
            self._hold = 0
            return
        if self._base is None or first:
            self._base = grey
            if first:
                self.commit(frame, reason="start", diff=0.0, caption=True)
            return
        diff = float(abs(grey - self._base).mean())
        self.stats["last_diff"] = round(diff, 2)
        hourly = (now - self._last_hourly_ts) >= HOURLY_S
        if diff >= DIFF_COMMIT:
            self._hold += 1
        else:
            self._hold = 0
        if self._hold >= HOLD_SAMPLES and (now - self._last_commit_ts) >= MIN_GAP_S:
            self.commit(frame, reason="change", diff=diff, caption=True)
            self._hold = 0
        elif hourly:
            self.commit(frame, reason="hourly", diff=diff, caption=True)

    # ── commit + caption ─────────────────────────────────────────────────
    def _sensors(self, frame) -> dict[str, Any]:
        g = self._g
        faces = list(g.get("_face_results") or [])
        out: dict[str, Any] = {
            "faces": [str(f.get("person_id") or "unknown") for f in faces],
            "attention_state": g.get("_attention_state"),
            "light_mean": round(float(frame.mean()), 1),
            "head": self._head(),
        }
        try:
            from brain import body_pose
            live = body_pose.live(g, max_age_s=3.0)
            if live is not None:
                out["pose"] = live.get("sentence")
                out["activity"] = [p.get("activity") for p in (live.get("persons") or [])
                                   if p.get("activity")]
            elif body_pose.status().get("loaded"):
                tracks = []
                try:
                    from brain import person_track
                    tracks, _ = person_track.track_boxes()
                except Exception:
                    pass
                r = body_pose.analyze(frame, faces=faces, tracks=tracks, head=out["head"])
                out["pose"] = r.get("sentence") if r.get("ok") else None
        except Exception:
            out["pose"] = None
        try:
            from brain import unknown_capture
            out["zeke_presence"] = unknown_capture.zeke_presence().get("reason")
        except Exception:
            pass
        return out

    def commit(self, frame, *, reason: str, diff: float, caption: bool) -> dict[str, Any]:
        now = time.time()
        kid = time.strftime("%Y%m%d_%H%M%S") + f"_{reason}"
        day = time.strftime("%Y%m%d")
        path = DIR / "keyframes" / day / f"{kid}.jpg"
        w = _write_small(frame, path)
        rec = {"id": kid, "ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "path": str(path), "reason": reason, "diff": round(float(diff), 2),
               "image": w, "sensors": self._sensors(frame),
               "words": None, "caption_status": "pending" if caption else "none"}
        self.records[kid] = rec
        self.order.append(kid)
        self._append(rec)
        self._base = _small_grey(frame)
        self._last_commit_ts = now
        if reason in ("hourly", "start"):
            self._last_hourly_ts = now
        self.stats["commits"] += 1
        _log(f"keyframe {kid} ({reason}, diff {diff:.1f}, {w.get('bytes')} B)")
        if caption:
            self._maybe_caption(kid)
        return rec

    def _captions_today(self) -> int:
        cutoff = time.time() - 86400.0
        self._captions = [t for t in self._captions if t > cutoff]
        return len(self._captions)

    def _maybe_caption(self, kid: str) -> None:
        now = time.time()
        rec = self.records.get(kid)
        if rec is None:
            return
        if (now - self._last_caption_ts) < CAPTION_MIN_GAP_S or self._captions_today() >= CAPTION_MAX_PER_DAY:
            rec["caption_status"] = "skipped_rate"
            self._append({"id": kid, "_update": True, "caption_status": "skipped_rate"})
            return
        self._last_caption_ts = now
        self._captions.append(now)
        threading.Thread(target=self._caption, args=(kid,), daemon=True,
                         name=f"scene_caption_{kid}").start()

    def _caption(self, kid: str) -> None:
        rec = self.records.get(kid)
        if rec is None:
            return
        s = rec.get("sensors") or {}
        prompt = (
            "[SCENE MEMORY — automated, not Zeke. A keyframe of the room was committed"
            f" ({rec['reason']}, diff {rec['diff']}). Sensors: faces={s.get('faces')},"
            f" pose={s.get('pose')!r}, attention={s.get('attention_state')},"
            f" light_mean={s.get('light_mean')}, head={s.get('head')},"
            f" presence={s.get('zeke_presence')}, time={rec['iso']}.]\n"
            f"The keyframe is SMALL and SAFE to Read: {rec['path']}\n"
            "Look at it. Reply with ONE diary sentence in your own voice about the room /"
            " what changed (who is there, what they seem to be doing, light, anything"
            " notable) — plain words, past tense, no preface. If nothing is worth"
            " remembering, reply exactly: skip"
        )
        try:
            from brain import iris_llm
            words = iris_llm.describe_image(rec["path"], prompt=prompt,
                                            kind="scene_caption", timeout_s=CAPTION_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            words = None
            _log(f"caption error: {e!r}")
        if not words:
            rec["caption_status"] = "timeout"
            self.stats["caption_timeouts"] += 1
            self._append({"id": kid, "_update": True, "caption_status": "timeout"})
            return
        words = words.strip()
        if words.lower().startswith("skip"):
            rec["caption_status"] = "skipped"
            self._append({"id": kid, "_update": True, "caption_status": "skipped"})
            return
        self.set_words(kid, words, source="caption")

    def set_words(self, kid: str, words: str, source: str = "manual") -> dict[str, Any]:
        rec = self.records.get(kid)
        if rec is None:
            return {"ok": False, "error": f"unknown keyframe {kid!r}"}
        rec["words"] = words
        rec["caption_status"] = "done"
        rec["caption_source"] = source
        self._append({"id": kid, "_update": True, "words": words,
                      "caption_status": "done", "caption_source": source})
        self.stats["captions"] += 1
        mem_id = None
        try:
            remember = self._g.get("remember_memory")
            if callable(remember):
                mem_id = remember(f"[scene {rec['iso']}] {words}", person_id="zeke",
                                  category="scene", importance=0.5, source="scene_memory",
                                  tags=["scene", "keyframe", kid, rec.get("reason", "")])
        except Exception as e:  # noqa: BLE001
            _log(f"remember error: {e!r}")
        rec["memory_id"] = mem_id
        _log(f"words for {kid}: {words[:90]}")
        return {"ok": True, "id": kid, "words": words, "memory_id": mem_id}

    def wordless(self, n: int = 20) -> list[dict[str, Any]]:
        """Keyframes whose caption timed out or was rate-skipped — for a
        free-time BACKFILL by cognition (Read the jpeg, action=caption)."""
        out = []
        for kid in reversed(self.order):
            r = self.records.get(kid) or {}
            if r.get("words") or r.get("caption_status") in ("done", "skipped", "none"):
                continue
            s = r.get("sensors") or {}
            out.append({"id": kid, "iso": r.get("iso"), "reason": r.get("reason"),
                        "diff": r.get("diff"), "path": r.get("path"),
                        "caption_status": r.get("caption_status"),
                        "faces": s.get("faces"), "pose": s.get("pose"),
                        "light": s.get("light_mean")})
            if len(out) >= n:
                break
        return out

    def recent(self, n: int = 10, with_words_only: bool = False) -> list[dict[str, Any]]:
        out = []
        for kid in reversed(self.order):
            r = self.records.get(kid) or {}
            if with_words_only and not r.get("words"):
                continue
            out.append({k: r.get(k) for k in ("id", "iso", "reason", "diff", "words",
                                                "caption_status", "path")}
                       | {"faces": (r.get("sensors") or {}).get("faces"),
                          "pose": (r.get("sensors") or {}).get("pose")})
            if len(out) >= n:
                break
        return out

    def snapshot(self, reason: str = "manual", caption: bool = True) -> dict[str, Any]:
        from brain import frame_store
        res = frame_store.get_buffered_frame(max_age_sec=3.0)
        if res.frame is None:
            return {"ok": False, "error": "no live frame"}
        rec = self.commit(res.frame, reason=reason, diff=float(self.stats.get("last_diff") or 0.0),
                          caption=caption)
        return {"ok": True, "id": rec["id"], "path": rec["path"], "caption_status": rec["caption_status"]}


# ── module-level wiring ────────────────────────────────────────────────────
def start(g: dict[str, Any]) -> dict[str, Any]:
    sm = g.get("_scene_memory")
    if not isinstance(sm, SceneMemory):
        sm = SceneMemory(g)
        g["_scene_memory"] = sm
    sm.start()
    return sm.status()


def get(g: dict[str, Any]) -> SceneMemory | None:
    sm = g.get("_scene_memory")
    return sm if isinstance(sm, SceneMemory) else None


def status(g: dict[str, Any]) -> dict[str, Any]:
    sm = get(g)
    return sm.status() if sm else {"alive": False, "note": "not started"}
