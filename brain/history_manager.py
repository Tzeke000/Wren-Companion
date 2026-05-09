from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fury import HistoryManager


class AvaHistoryManager:
    def __init__(self, base_dir: Path, *, target_context_length: int = 8000) -> None:
        self.base_dir = Path(base_dir)
        self.target_context_length = int(target_context_length or 8000)
        self.chatlog_path = self.base_dir / "chatlog.jsonl"
        self.summaries_path = self.base_dir / "state" / "history_summaries.jsonl"
        self.summaries_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_rows(self, person_id: str | None = None) -> list[dict[str, Any]]:
        if not self.chatlog_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in self.chatlog_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("role") or "") not in ("user", "assistant"):
                    continue
                if person_id:
                    meta = row.get("meta", {}) if isinstance(row.get("meta"), dict) else {}
                    if str(meta.get("person_id") or "") != str(person_id):
                        continue
                rows.append(row)
        except Exception:
            return []
        return rows

    @staticmethod
    def _as_fury_messages(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for r in rows:
            role = str(r.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            content = str(r.get("content") or "").strip()
            if not content:
                continue
            out.append({"role": role, "content": content})
        return out

    def _load_summaries(self, person_id: str) -> list[dict[str, Any]]:
        if not self.summaries_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in self.summaries_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("person_id") or "") != str(person_id):
                    continue
                out.append(row)
        except Exception:
            return []
        return out[-20:]

    def _persist_summaries(self, rows: list[dict[str, Any]]) -> None:
        kept = rows[-20:]
        blob = "\n".join(json.dumps(r, ensure_ascii=False) for r in kept)
        if blob:
            blob += "\n"
        tmp = self.summaries_path.with_suffix(".jsonl.tmp")
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(self.summaries_path)

    def _append_summary(self, person_id: str, summary_text: str) -> None:
        txt = " ".join((summary_text or "").split()).strip()
        if not txt:
            return
        all_rows: list[dict[str, Any]] = []
        if self.summaries_path.is_file():
            try:
                for line in self.summaries_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        all_rows.append(json.loads(line))
                    except Exception:
                        continue
            except Exception:
                all_rows = []
        row = {
            "ts": time.time(),
            "person_id": str(person_id),
            "summary": txt[:4000],
            "source": "fury_auto_compaction",
        }
        all_rows.append(row)
        self._persist_summaries(all_rows)

    def _run_fury_compaction(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            return ""
        mgr = HistoryManager(
            history=[],
            summary_model="mistral:7b",
            auto_compact=True,
            context_window=self.target_context_length + 1200,
            reserve_tokens=1400,
            keep_recent_tokens=max(1800, int(self.target_context_length * 0.35)),
            summary_prefix="CONVERSATION SUMMARY (earlier sessions):",
            summary_system_prompt=(
                "Summarize this conversation between Zeke and Ava concisely, preserving key facts, "
                "decisions, topics discussed, and emotional tone. Write from Ava's perspective."
            ),
            persist_to_disk=False,
        )
        for msg in messages:
            try:
                asyncio.run(mgr.add(msg))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(mgr.add(msg))
                finally:
                    loop.close()
        # Fury compaction leaves summary messages in history; prefer latest summary block.
        summary = ""
        for row in reversed(list(getattr(mgr, "history", []) or [])):
            content = str((row or {}).get("content") or "").strip()
            if "CONVERSATION SUMMARY (earlier sessions):" in content:
                summary = content.split("CONVERSATION SUMMARY (earlier sessions):", 1)[-1].strip()
                break
        return summary.strip()

    def load_history(self, person_id: str, max_turns: int = 20) -> list[dict[str, str]]:
        rows = self._load_rows(person_id=person_id)
        msgs = self._as_fury_messages(rows)
        if len(msgs) > int(max_turns):
            older = msgs[:-int(max_turns)]
            try:
                compacted = self._run_fury_compaction(older)
                if compacted:
                    self._append_summary(person_id, compacted)
            except Exception:
                pass
        return msgs[-int(max_turns) :]

    def get_context_block(self, person_id: str, *, max_turns: int = 20, max_chars: int = 6000) -> str:
        recent = self.load_history(person_id, max_turns=max_turns)
        summaries = self._load_summaries(person_id)
        lines: list[str] = []
        if summaries:
            merged = "\n".join(str(r.get("summary") or "").strip() for r in summaries[-3:] if str(r.get("summary") or "").strip())
            if merged:
                lines.append("CONVERSATION SUMMARY (earlier sessions):")
                lines.append(merged[: max(600, max_chars // 2)])
                lines.append("")
        lines.append("RECENT CONVERSATION (Zeke / Ava):")
        for row in recent:
            role = str(row.get("role") or "").strip().lower()
            who = "Zeke" if role == "user" else "Ava"
            text = str(row.get("content") or "").strip()
            if text:
                lines.append(f"{who}: {text[:320]}")
        block = "\n".join(lines).strip()
        if len(block) > int(max_chars):
            block = block[: int(max_chars) - 1] + "…"
        return block or "(no conversation history available)"

    def summary_count(self, person_id: str | None = None) -> int:
        if not self.summaries_path.is_file():
            return 0
        try:
            rows = self.summaries_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if person_id:
                c = 0
                for line in rows:
                    try:
                        row = json.loads(line)
                        if str(row.get("person_id") or "") == str(person_id):
                            c += 1
                    except Exception:
                        continue
                return c
            return len([ln for ln in rows if ln.strip()])
        except Exception:
            return 0


_MANAGER: AvaHistoryManager | None = None


def get_history_manager(base_dir: Path | None = None) -> AvaHistoryManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = AvaHistoryManager(base_dir or Path(__file__).resolve().parent.parent)
    return _MANAGER


def load_history(person_id: str, max_turns: int = 20) -> list[dict[str, str]]:
    return get_history_manager().load_history(person_id, max_turns=max_turns)


def get_context_block(person_id: str) -> str:
    return get_history_manager().get_context_block(person_id)
