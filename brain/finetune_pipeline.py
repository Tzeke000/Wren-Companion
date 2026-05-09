from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


def _safe_read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_identity_bundle(base_dir: Path) -> str:
    identity_path = base_dir / "ava_core" / "IDENTITY.md"
    soul_path = base_dir / "ava_core" / "SOUL.md"
    identity = identity_path.read_text(encoding="utf-8", errors="replace") if identity_path.is_file() else ""
    soul = soul_path.read_text(encoding="utf-8", errors="replace") if soul_path.is_file() else ""
    self_model = _safe_read_json(base_dir / "state" / "self_model.json")
    self_summary = ""
    if isinstance(self_model, dict):
        summary_parts: list[str] = []
        identity_statement = str(self_model.get("identity_statement") or "").strip()
        if identity_statement:
            summary_parts.append(identity_statement)
        strengths = self_model.get("perceived_strengths")
        if isinstance(strengths, list) and strengths:
            summary_parts.append(f"strengths: {', '.join(str(x) for x in strengths[:6])}")
        goals = self_model.get("current_goals")
        if isinstance(goals, list) and goals:
            summary_parts.append(f"goals: {', '.join(str(x) for x in goals[:6])}")
        self_summary = "\n".join(summary_parts)
    return f"{identity}\n\n{soul}\n\nSELF SUMMARY:\n{self_summary}".strip()


class TrainingDataBuilder:
    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.chatlog_path = self.base_dir / "chatlog.jsonl"
        self.reflection_path = self.base_dir / "memory" / "self reflection" / "reflection_log.jsonl"
        self.out_dir = self.base_dir / "state" / "finetune"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.dataset_path = self.out_dir / "training_data.jsonl"
        self.meta_path = self.out_dir / "dataset_meta.json"

    def _load_reflection_keywords(self) -> set[str]:
        words: set[str] = set()
        if not self.reflection_path.is_file():
            return words
        for line in self.reflection_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            importance = float(row.get("importance") or row.get("confidence") or 0.0)
            if importance < 0.7 and not bool(row.get("memory_worthy", False)):
                continue
            summary = str(row.get("summary") or row.get("note") or row.get("observation") or "").lower()
            for token in summary.replace("\n", " ").split():
                t = token.strip(".,:;!?()[]{}\"'")
                if len(t) >= 4:
                    words.add(t)
        return words

    @staticmethod
    def _is_substantive(text: str) -> bool:
        low = (text or "").strip().lower()
        if not low:
            return False
        if len(low.split()) <= 3:
            return False
        skip = {"hi", "hello", "hey", "ok", "okay", "yes", "no", "thanks", "thank you"}
        if low in skip:
            return False
        return True

    def build_dataset(self, person_id: str = "zeke", min_turns: int = 50) -> int:
        if not self.chatlog_path.is_file():
            self.dataset_path.write_text("", encoding="utf-8")
            return 0
        reflection_keywords = self._load_reflection_keywords()
        system_prompt = _read_identity_bundle(self.base_dir)
        rows = self.chatlog_path.read_text(encoding="utf-8", errors="replace").splitlines()
        examples: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()

        for i in range(len(rows) - 1):
            try:
                user_row = json.loads(rows[i])
                asst_row = json.loads(rows[i + 1])
            except Exception:
                continue
            if not (isinstance(user_row, dict) and isinstance(asst_row, dict)):
                continue
            if str(user_row.get("role") or "") != "user" or str(asst_row.get("role") or "") != "assistant":
                continue
            user_meta = user_row.get("meta") if isinstance(user_row.get("meta"), dict) else {}
            pid = str(user_meta.get("person_id") or "").lower()
            if person_id and pid != person_id.lower():
                continue
            user_text = " ".join(str(user_row.get("content") or "").split()).strip()
            asst_text = " ".join(str(asst_row.get("content") or "").split()).strip()
            if len(asst_text.split()) <= 20:
                continue
            if not (self._is_substantive(user_text) and self._is_substantive(asst_text)):
                continue
            if reflection_keywords:
                merged = f"{user_text.lower()} {asst_text.lower()}"
                if not any(k in merged for k in reflection_keywords):
                    continue
            pair_key = (user_text.lower(), asst_text.lower())
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            examples.append(
                {
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": asst_text},
                    ]
                }
            )

        if len(examples) < min_turns:
            # fallback to best substantive pairs without reflection restriction
            for i in range(len(rows) - 1):
                try:
                    user_row = json.loads(rows[i])
                    asst_row = json.loads(rows[i + 1])
                except Exception:
                    continue
                if str(user_row.get("role") or "") != "user" or str(asst_row.get("role") or "") != "assistant":
                    continue
                user_meta = user_row.get("meta") if isinstance(user_row.get("meta"), dict) else {}
                pid = str(user_meta.get("person_id") or "").lower()
                if person_id and pid != person_id.lower():
                    continue
                user_text = " ".join(str(user_row.get("content") or "").split()).strip()
                asst_text = " ".join(str(asst_row.get("content") or "").split()).strip()
                if len(asst_text.split()) <= 20:
                    continue
                if not (self._is_substantive(user_text) and self._is_substantive(asst_text)):
                    continue
                pair_key = (user_text.lower(), asst_text.lower())
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                examples.append(
                    {
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_text},
                            {"role": "assistant", "content": asst_text},
                        ]
                    }
                )
                if len(examples) >= min_turns:
                    break

        with self.dataset_path.open("w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
        self.meta_path.write_text(
            json.dumps(
                {
                    "last_built_at": time.time(),
                    "count": len(examples),
                    "person_id": person_id,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return len(examples)

    def validate_dataset(self) -> dict[str, Any]:
        issues: list[str] = []
        if not self.dataset_path.is_file():
            return {"valid": False, "count": 0, "issues": ["training_data.jsonl missing"]}
        lines = self.dataset_path.read_text(encoding="utf-8", errors="replace").splitlines()
        dedupe: set[str] = set()
        count = 0
        for idx, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                issues.append(f"line {idx} invalid JSON")
                continue
            msgs = row.get("messages") if isinstance(row, dict) else None
            if not isinstance(msgs, list) or len(msgs) != 3:
                issues.append(f"line {idx} invalid message format")
                continue
            sig = json.dumps(msgs, ensure_ascii=False, sort_keys=True)
            if sig in dedupe:
                issues.append(f"line {idx} duplicate example")
                continue
            dedupe.add(sig)
            count += 1
        if count < 50:
            issues.append("fewer than 50 examples")
        return {"valid": len(issues) == 0, "count": count, "issues": issues}


class FineTuneManager:
    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.out_dir = self.base_dir / "state" / "finetune"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.dataset_builder = TrainingDataBuilder(self.base_dir)
        self.status_path = self.out_dir / "finetune_status.json"
        self.log_path = self.out_dir / "finetune_log.txt"
        self.modelfile_path = self.out_dir / "Modelfile"
        self._lock = threading.Lock()
        if not self.status_path.is_file():
            self._write_status({"status": "idle"})

    def _read_status(self) -> dict[str, Any]:
        data = _safe_read_json(self.status_path)
        return data if isinstance(data, dict) else {"status": "idle"}

    def _write_status(self, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self._read_status()
        cur.update(patch)
        if self.log_path.is_file():
            tail = self.read_log_tail(10)
            cur["log_tail"] = "\n".join(tail)
        self.status_path.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        return cur

    def read_log_tail(self, n: int = 50) -> list[str]:
        if not self.log_path.is_file():
            return []
        return self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, n) :]

    def check_prerequisites(self) -> dict[str, Any]:
        issues: list[str] = []
        checks: dict[str, bool] = {
            "ollama_running": False,
            "base_model_available": False,
            "dataset_valid": False,
            "disk_ok": False,
        }
        try:
            p = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=15)
            checks["ollama_running"] = p.returncode == 0
            if p.returncode == 0 and "llama3.1:8b" in (p.stdout or ""):
                checks["base_model_available"] = True
        except Exception:
            pass
        if not checks["ollama_running"]:
            issues.append("Ollama is not running / not accessible")
        if not checks["base_model_available"]:
            issues.append("Base model llama3.1:8b not available")

        vr = self.dataset_builder.validate_dataset()
        checks["dataset_valid"] = bool(vr.get("valid", False))
        if not checks["dataset_valid"]:
            issues.append(f"Dataset invalid: {', '.join(vr.get('issues') or [])}")

        usage = shutil.disk_usage(self.base_dir)
        free_gb = usage.free / (1024**3)
        checks["disk_ok"] = free_gb >= 10.0
        if not checks["disk_ok"]:
            issues.append(f"Insufficient disk space ({free_gb:.2f} GB free)")

        return {"ready": len(issues) == 0, "issues": issues, "checks": checks, "free_gb": round(free_gb, 2)}

    def create_modelfile(self, base_model: str = "llama3.1:8b") -> str:
        system_prompt = _read_identity_bundle(self.base_dir).replace('"', '\\"')
        text = (
            f"FROM {base_model}\n"
            f'SYSTEM "{system_prompt}"\n'
            "# Training parameters\n"
            "PARAMETER temperature 0.7\n"
            "PARAMETER top_p 0.9\n"
        )
        self.modelfile_path.write_text(text, encoding="utf-8")
        return str(self.modelfile_path)

    def run_finetune(self) -> bool:
        with self._lock:
            start_ts = time.time()
            self._write_status(
                {
                    "status": "preparing",
                    "started_at": start_ts,
                    "completed_at": None,
                    "base_model": "llama3.1:8b",
                    "output_model": "ava-personal:latest",
                }
            )
            examples = self.dataset_builder.validate_dataset().get("count", 0)
            self.create_modelfile("llama3.1:8b")
            self._write_status({"status": "running", "examples_used": int(examples)})
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as logf:
                logf.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting finetune\n")
                try:
                    proc = subprocess.Popen(
                        ["ollama", "create", "ava-personal", "-f", str(self.modelfile_path)],
                        cwd=str(self.base_dir),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        logf.write(line)
                        logf.flush()
                    code = proc.wait()
                    ok = code == 0
                except Exception as e:
                    logf.write(f"finetune error: {e}\n")
                    ok = False
            end_ts = time.time()
            self._write_status(
                {
                    "status": "complete" if ok else "failed",
                    "completed_at": end_ts,
                    "examples_used": int(examples),
                    "base_model": "llama3.1:8b",
                    "output_model": "ava-personal:latest",
                    "last_finetune_at": end_ts if ok else None,
                }
            )
            return ok

    def schedule_finetune(self, interval_days: int = 7) -> bool:
        st = self._read_status()
        now = time.time()
        last_ts = float(st.get("last_finetune_at") or 0.0)
        enough_time = last_ts <= 0 or ((now - last_ts) >= max(1, int(interval_days)) * 24 * 3600)
        if not enough_time:
            return False

        chatlog = self.base_dir / "chatlog.jsonl"
        lines = chatlog.read_text(encoding="utf-8", errors="replace").splitlines() if chatlog.is_file() else []
        user_count = 0
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and str(row.get("role") or "") == "user":
                    user_count += 1
            except Exception:
                continue
        last_count = int(st.get("last_chatlog_user_count") or 0)
        if user_count - last_count < 50:
            self._write_status({"last_chatlog_user_count": user_count})
            return False
        self._write_status({"last_chatlog_user_count": user_count})

        def _run_bg() -> None:
            pre = self.check_prerequisites()
            if not pre.get("ready", False):
                self._write_status({"status": "failed", "completed_at": time.time(), "issues": pre.get("issues", [])})
                return
            self.run_finetune()

        threading.Thread(target=_run_bg, daemon=True, name="ava-finetune-scheduler").start()
        return True

