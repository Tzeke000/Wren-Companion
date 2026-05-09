"""brain/telemetry.py — Pipeline-stage telemetry (A6).

Per-turn timing across the pipeline stages so we can answer "what was
slow about that turn?" without log-grepping. Each turn gets a record
with timestamps for each stage; records are kept in an in-memory ring
buffer (last 100 turns) AND persisted to state/telemetry/turns.jsonl
so we can post-hoc analyze across sessions.

Usage:

    from brain.telemetry import telemetry

    turn_id = telemetry.start_turn(input_text=text, source="voice", person_id="zeke")
    ...
    telemetry.mark(turn_id, "stt_done")
    ...
    telemetry.mark(turn_id, "router_entry")
    ...
    telemetry.mark(turn_id, "llm_invoke_start", model="ava-personal:latest")
    ...
    telemetry.mark(turn_id, "llm_invoke_done")
    ...
    telemetry.end_turn(turn_id, reply_chars=len(reply), route="action_tag")

Stage names (recommended; not enforced):
    wake         (clap/wake-word fired)
    stt_start    (Silero VAD detected speech start)
    stt_done     (Whisper produced text)
    router_entry (run_ava entry)
    voice_command_match
    action_tag_classify
    skill_recall_match
    subagent_delegated
    introspection_compose
    fast_path_entered
    deep_path_entered
    llm_invoke_start
    llm_first_chunk
    llm_invoke_done
    tts_enqueue
    tts_synth_start
    tts_synth_done
    tts_playback_start
    tts_playback_done
    run_ava_return

Surfaced on operator HTTP at /api/v1/debug/turn_timings.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any


_BUFFER_SIZE = 100
_PERSIST_FLUSH_EVERY = 5  # flush every N turns
_MAX_PERSIST_LINES = 5000  # cap state/telemetry/turns.jsonl


class _TurnRecord:
    __slots__ = ("turn_id", "started_ts", "ended_ts", "input_chars",
                 "input_text_preview", "source", "person_id", "stages",
                 "reply_chars", "route", "model", "ok", "extra")

    def __init__(
        self,
        turn_id: str,
        *,
        input_text: str,
        source: str,
        person_id: str,
    ) -> None:
        self.turn_id = turn_id
        self.started_ts = time.time()
        self.ended_ts: float | None = None
        self.input_chars = len(input_text or "")
        self.input_text_preview = (input_text or "")[:80]
        self.source = source
        self.person_id = person_id
        # Each stage entry: {"name": str, "ts": float, "ms_from_start": int, "meta": dict}
        self.stages: list[dict[str, Any]] = []
        self.reply_chars: int | None = None
        self.route: str | None = None
        self.model: str | None = None
        self.ok: bool | None = None
        self.extra: dict[str, Any] = {}

    def mark(self, stage: str, **meta: Any) -> None:
        ts = time.time()
        self.stages.append({
            "name": str(stage),
            "ts": ts,
            "ms_from_start": int((ts - self.started_ts) * 1000),
            "meta": dict(meta) if meta else {},
        })

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "turn_id": self.turn_id,
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
            "duration_ms": int((self.ended_ts - self.started_ts) * 1000) if self.ended_ts else None,
            "input_chars": self.input_chars,
            "input_text_preview": self.input_text_preview,
            "source": self.source,
            "person_id": self.person_id,
            "stages": list(self.stages),
            "reply_chars": self.reply_chars,
            "route": self.route,
            "model": self.model,
            "ok": self.ok,
        }
        if self.extra:
            out["extra"] = dict(self.extra)
        return out


class Telemetry:
    def __init__(self, base_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._buffer: deque[_TurnRecord] = deque(maxlen=_BUFFER_SIZE)
        self._active: dict[str, _TurnRecord] = {}
        self._base_dir = base_dir
        self._unflushed: list[dict[str, Any]] = []
        self._flush_counter = 0
        self._enabled = True

    def _persist_path(self) -> Path | None:
        if self._base_dir is None:
            return None
        p = self._base_dir / "state" / "telemetry" / "turns.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def configure(self, base_dir: Path) -> None:
        with self._lock:
            self._base_dir = base_dir

    def start_turn(
        self,
        *,
        input_text: str = "",
        source: str = "unknown",
        person_id: str = "",
    ) -> str:
        if not self._enabled:
            return ""
        turn_id = uuid.uuid4().hex[:12]
        rec = _TurnRecord(
            turn_id,
            input_text=input_text,
            source=source,
            person_id=person_id,
        )
        with self._lock:
            self._active[turn_id] = rec
        return turn_id

    def mark(self, turn_id: str, stage: str, **meta: Any) -> None:
        if not turn_id or not self._enabled:
            return
        with self._lock:
            rec = self._active.get(turn_id)
            if rec is not None:
                rec.mark(stage, **meta)

    def end_turn(
        self,
        turn_id: str,
        *,
        reply_chars: int | None = None,
        route: str | None = None,
        model: str | None = None,
        ok: bool | None = None,
        **extra: Any,
    ) -> None:
        if not turn_id or not self._enabled:
            return
        with self._lock:
            rec = self._active.pop(turn_id, None)
            if rec is None:
                return
            rec.ended_ts = time.time()
            if reply_chars is not None:
                rec.reply_chars = int(reply_chars)
            if route is not None:
                rec.route = str(route)
            if model is not None:
                rec.model = str(model)
            if ok is not None:
                rec.ok = bool(ok)
            if extra:
                rec.extra.update(extra)
            self._buffer.append(rec)
            self._unflushed.append(rec.to_dict())
            self._flush_counter += 1
            if self._flush_counter >= _PERSIST_FLUSH_EVERY:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._unflushed:
            self._flush_counter = 0
            return
        path = self._persist_path()
        if path is None:
            self._unflushed.clear()
            self._flush_counter = 0
            return
        try:
            with path.open("a", encoding="utf-8") as f:
                for d in self._unflushed:
                    f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
            self._unflushed.clear()
            self._flush_counter = 0
            self._truncate_if_needed(path)
        except Exception as e:
            print(f"[telemetry] flush error: {e!r}")

    def _truncate_if_needed(self, path: Path) -> None:
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > _MAX_PERSIST_LINES:
                with path.open("w", encoding="utf-8") as f:
                    f.writelines(lines[-_MAX_PERSIST_LINES:])
        except Exception as e:
            print(f"[telemetry] truncate error: {e!r}")

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            recs = list(self._buffer)[-int(limit):]
            return [r.to_dict() for r in recs]

    def summary(self) -> dict[str, Any]:
        """Aggregate summary across the in-memory buffer.

        Returns mean/median/p90 durations per stage transition, plus
        counts per route. Useful for "is something regressing?" checks.
        """
        with self._lock:
            recs = list(self._buffer)
        if not recs:
            return {"count": 0}
        durations = [r.ended_ts - r.started_ts for r in recs if r.ended_ts]
        if not durations:
            return {"count": len(recs), "completed": 0}
        durations.sort()
        n = len(durations)
        median = durations[n // 2]
        p90 = durations[int(n * 0.9)]
        mean = sum(durations) / n
        routes: dict[str, int] = {}
        for r in recs:
            routes[r.route or "unknown"] = routes.get(r.route or "unknown", 0) + 1
        return {
            "count": len(recs),
            "completed": n,
            "mean_seconds": round(mean, 3),
            "median_seconds": round(median, 3),
            "p90_seconds": round(p90, 3),
            "routes": routes,
        }


# Singleton for the process.
telemetry = Telemetry()


def configure_telemetry(base_dir: Path) -> None:
    """Called once at startup to point telemetry at the project base dir."""
    telemetry.configure(base_dir)
