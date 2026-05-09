"""
VideoMemory — rolling 1-minute video clip buffer.
Ava decides which clips to keep based on what happened in them.
Auto-saves meaningful moments; discards quiet/empty ones.
Bootstrap: Ava develops her own criteria for what's worth remembering on video.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional


_CLIPS_DIR = "state/video_clips"
_SUMMARIES_PATH = "state/video_summaries.jsonl"
_MAX_FPS = 15
_MAX_FRAMES = _MAX_FPS * 60       # 1 minute
_MAX_STORAGE_GB = 2.0
_IMPORTANT_KEEP_DAYS = 7
_NORMAL_KEEP_DAYS = 1


class VideoMemory:
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self._clips_dir = base_dir / _CLIPS_DIR
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._buffer: deque[Any] = deque(maxlen=_MAX_FRAMES)
        self._clip_start_ts: float = time.time()
        self._lock = threading.Lock()
        self._clip_count_today = 0
        self._last_expression: str = "neutral"
        self._last_gaze: str = "center"

    # ── Frame intake ──────────────────────────────────────────────────────────

    def add_frame(self, frame: Any, expression: str = "", gaze: str = "") -> None:
        """Add a frame to the rolling buffer."""
        with self._lock:
            self._buffer.append((time.time(), frame))
            if expression:
                self._last_expression = expression
            if gaze:
                self._last_gaze = gaze

    # ── Clip management ───────────────────────────────────────────────────────

    def start_clip(self) -> None:
        """Reset buffer and start accumulating a new clip."""
        with self._lock:
            self._buffer.clear()
            self._clip_start_ts = time.time()

    def end_clip(self, keep_reason: Optional[str] = None) -> Optional[str]:
        """
        Finalize current buffer.
        If keep_reason is set, save to disk as MP4 and return path.
        Otherwise discard.
        """
        with self._lock:
            frames = list(self._buffer)
            self._buffer.clear()
            self._clip_start_ts = time.time()

        if not keep_reason or not frames:
            return None

        return self._save_clip(frames, keep_reason)

    def _save_clip(self, frames: list, reason: str) -> Optional[str]:
        try:
            import cv2
            import numpy as np
            ts = int(time.time())
            safe_reason = reason.replace(" ", "_").replace("/", "_")[:40]
            out_path = self._clips_dir / f"{ts}_{safe_reason}.mp4"
            if not frames:
                return None
            # Get frame dimensions from first valid frame
            sample_frame = None
            for _, f in frames:
                if f is not None and hasattr(f, "shape"):
                    sample_frame = f
                    break
            if sample_frame is None:
                return None
            h, w = sample_frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(str(out_path), fourcc, _MAX_FPS, (w, h))
            for _, f in frames:
                if f is not None and hasattr(f, "shape"):
                    if len(f.shape) == 2:
                        f = cv2.cvtColor(f, cv2.COLOR_GRAY2BGR)
                    elif f.shape[2] == 4:
                        f = cv2.cvtColor(f, cv2.COLOR_RGBA2BGR)
                    writer.write(f)
            writer.release()
            self._clip_count_today += 1
            print(f"[video_memory] saved clip {out_path.name} frames={len(frames)} reason={reason}")
            return str(out_path)
        except Exception as e:
            print(f"[video_memory] save_clip error: {e}")
            return None

    def should_keep_clip(self, g: dict[str, Any]) -> tuple[bool, str]:
        """
        Ava decides whether to keep the current clip.
        Bootstrap: she develops her own criteria over time.
        Returns (keep: bool, reason: str).
        """
        with self._lock:
            frames = list(self._buffer)

        if not frames:
            return False, "no_frames"

        # Heuristics Ava starts with — she can override or extend these
        last_expression = self._last_expression
        last_gaze = self._last_gaze
        last_user_interact = float(g.get("_last_user_interaction_ts") or 0)
        was_talking = (time.time() - last_user_interact) < 120.0

        # Expression changed significantly (would be detected by comparing first/last)
        if last_expression not in ("neutral", ""):
            return True, f"expression_{last_expression}"

        # Active conversation
        if was_talking:
            return True, "active_conversation"

        # Unusual gaze region (not looking at center)
        if last_gaze not in ("center", "unknown", ""):
            return True, f"gaze_{last_gaze}"

        # New person appeared
        if g.get("_person_transition_note"):
            return True, "person_transition"

        return False, "no_significant_events"

    def auto_clip_tick(self, g: dict[str, Any]) -> None:
        """Call every 60 seconds from heartbeat. Evaluates and rotates clip."""
        keep, reason = self.should_keep_clip(g)
        if keep:
            saved = self.end_clip(keep_reason=reason)
            if saved:
                self._summarize_clip_async(saved, g)
        else:
            self.end_clip(keep_reason=None)

    # ── Summarization ─────────────────────────────────────────────────────────

    def _summarize_clip_async(self, clip_path: str, g: dict[str, Any]) -> None:
        t = threading.Thread(
            target=self._summarize_clip,
            args=(clip_path, g),
            daemon=True,
            name="ava-clip-summarize",
        )
        t.start()

    def _summarize_clip(self, clip_path: str, g: dict[str, Any]) -> None:
        try:
            summary = self.summarize_clip(clip_path, g)
            if not summary:
                return
            entry = {
                "ts": time.time(),
                "clip_path": clip_path,
                "summary": summary,
                "important": False,
            }
            log_path = self._base_dir / _SUMMARIES_PATH
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[video_memory] summarize error: {e}")

    def summarize_clip(self, clip_path: str, g: dict[str, Any]) -> Optional[str]:
        """
        Sample 5 frames, send to LLaVA, combine into text summary.
        """
        try:
            import cv2
            cap = cv2.VideoCapture(clip_path)
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total < 5:
                cap.release()
                return None
            frame_indices = [int(total * i / 5) for i in range(5)]
            descriptions: list[str] = []
            for idx in frame_indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if not ok:
                    continue
                desc = self._describe_frame(frame, g)
                if desc:
                    descriptions.append(desc)
            cap.release()
            if not descriptions:
                return None
            ts_str = time.strftime("%H:%M", time.localtime())
            combined = "; ".join(descriptions[:3])
            return f"At {ts_str}: {combined}"
        except Exception as e:
            print(f"[video_memory] summarize_clip error: {e}")
            return None

    def _describe_frame(self, frame: Any, g: dict[str, Any]) -> Optional[str]:
        """Use LLaVA to describe a single frame."""
        try:
            llava_model = g.get("_llava_model_name")
            if not llava_model:
                return None
            import base64
            import cv2
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            b64 = base64.b64encode(buf.tobytes()).decode("ascii")
            from langchain_ollama import ChatOllama
            from langchain_core.messages import HumanMessage
            llm = ChatOllama(model=llava_model, temperature=0.2)
            msg = HumanMessage(content=[
                {"type": "text", "text": "Describe what Zeke (the user) is doing and their expression in one sentence."},
                {"type": "image_url", "image_url": f"data:image/jpeg;base64,{b64}"},
            ])
            result = llm.invoke([msg])
            return str(getattr(result, "content", str(result))).strip()[:200]
        except Exception:
            return None

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self) -> dict[str, Any]:
        """Delete old clips. Keep important clips 7 days, others 1 day."""
        deleted = 0
        kept = 0
        total_size = 0
        now = time.time()
        try:
            summaries: dict[str, bool] = {}
            log_path = self._base_dir / _SUMMARIES_PATH
            if log_path.is_file():
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    try:
                        e = json.loads(line)
                        summaries[str(e.get("clip_path") or "")] = bool(e.get("important"))
                    except Exception:
                        pass

            for clip in sorted(self._clips_dir.glob("*.mp4")):
                age_days = (now - clip.stat().st_mtime) / 86400
                is_important = summaries.get(str(clip), False)
                max_age = _IMPORTANT_KEEP_DAYS if is_important else _NORMAL_KEEP_DAYS
                size = clip.stat().st_size
                total_size += size
                if age_days > max_age:
                    clip.unlink()
                    deleted += 1
                else:
                    kept += 1

            # Also enforce total size limit
            if total_size > _MAX_STORAGE_GB * 1e9:
                clips_by_age = sorted(self._clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
                for clip in clips_by_age:
                    if total_size <= _MAX_STORAGE_GB * 1e9 * 0.8:
                        break
                    is_important = summaries.get(str(clip), False)
                    if not is_important:
                        total_size -= clip.stat().st_size
                        clip.unlink()
                        deleted += 1
        except Exception as e:
            print(f"[video_memory] cleanup error: {e}")
        return {"deleted": deleted, "kept": kept, "total_mb": round(total_size / 1e6, 1)}


# ── Module singleton ──────────────────────────────────────────────────────────

_SINGLETON: Optional[VideoMemory] = None


def get_video_memory(base_dir: Optional[Path] = None) -> Optional[VideoMemory]:
    global _SINGLETON
    if _SINGLETON is None and base_dir is not None:
        _SINGLETON = VideoMemory(base_dir)
    return _SINGLETON


def bootstrap_video_memory(g: dict[str, Any]) -> VideoMemory:
    global _SINGLETON
    base = Path(g.get("BASE_DIR") or ".")
    vm = VideoMemory(base)
    _SINGLETON = vm
    g["_video_memory"] = vm
    vm.start_clip()
    print("[video_memory] initialized, clip recording started")
    return vm
