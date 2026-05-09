"""
Phase 44 — Bootstrap self-evaluation for ava-personal:latest as primary brain.

Ava evaluates her own responses against mistral:7b in the background.
She decides if she's ready to be her own brain — not us.

State file: state/model_eval_p44.json
Decision: confirmed_primary (win_rate >= 0.60 over 5+ samples)
          flagged_for_review  (win_rate < 0.40 over 10+ samples)
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


_MIN_SAMPLES = 5
_CONFIRMED_THRESHOLD = 0.60
_FLAGGED_THRESHOLD = 0.40
_FLAGGED_MIN_SAMPLES = 10


@dataclass
class EvalState:
    status: str = "evaluating"
    ava_model: str = "ava-personal:latest"
    challenger_model: str = "mistral:7b"
    total_samples: int = 0
    ava_wins: int = 0
    challenger_wins: int = 0
    ties: int = 0
    decision_ts: float = 0.0
    decision_reason: str = ""
    samples: list = field(default_factory=list)


class ModelSelfEvaluator:
    """
    Background evaluator that compares ava-personal responses against mistral:7b.
    Ava scores herself and decides if she's ready to be primary.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self._base = Path(base_dir) if base_dir else Path(".")
        self._eval_path = self._base / "state" / "model_eval_p44.json"
        self._lock = threading.Lock()
        self._state = self._load()
        self._queue: list[dict] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _load(self) -> EvalState:
        if not self._eval_path.is_file():
            return EvalState()
        try:
            d = json.loads(self._eval_path.read_text(encoding="utf-8"))
            s = EvalState()
            for k, v in d.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            return s
        except Exception:
            return EvalState()

    def _save(self, state: EvalState) -> None:
        self._eval_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._eval_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._eval_path)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            s = self._state
            return {
                "status": s.status,
                "total_samples": s.total_samples,
                "ava_wins": s.ava_wins,
                "challenger_wins": s.challenger_wins,
                "ava_model": s.ava_model,
                "challenger_model": s.challenger_model,
                "decision_reason": s.decision_reason,
            }

    def is_complete(self) -> bool:
        with self._lock:
            return self._state.status in ("confirmed_primary", "flagged_for_review")

    def submit_for_evaluation(self, prompt: str, ava_response: str, model_used: str) -> None:
        """Non-blocking — queues work for background thread."""
        if self.is_complete():
            return
        with self._lock:
            self._queue.append({
                "prompt": prompt[:1200],
                "ava_response": ava_response[:800],
                "model_used": model_used,
                "ts": time.time(),
            })
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._running and self._thread and self._thread.is_alive():
                return
            self._running = True
            t = threading.Thread(target=self._worker, daemon=True, name="model-eval-p44")
            self._thread = t
        t.start()

    def _worker(self) -> None:
        try:
            while True:
                item = None
                with self._lock:
                    if self._queue:
                        item = self._queue.pop(0)
                    else:
                        self._running = False
                        break
                if item:
                    try:
                        self._evaluate_pair(item)
                    except Exception as ex:
                        print(f"[model_eval_p44] pair error: {ex!r}")
        except Exception:
            with self._lock:
                self._running = False

    def _evaluate_pair(self, item: dict) -> None:
        try:
            from langchain_ollama import ChatOllama
            from langchain_core.messages import HumanMessage, SystemMessage
        except ImportError:
            return

        prompt = item["prompt"]
        ava_response = item["ava_response"]
        ava_model = item["model_used"]
        challenger = "mistral:7b"

        try:
            llm_c = ChatOllama(model=challenger, temperature=0.6)
            result_c = llm_c.invoke([
                SystemMessage(content="You are a helpful AI assistant. Reply naturally and concisely."),
                HumanMessage(content=prompt),
            ])
            challenger_response = (getattr(result_c, "content", None) or str(result_c)).strip()
        except Exception as ex:
            print(f"[model_eval_p44] challenger invoke failed: {ex!r}")
            return

        if not challenger_response:
            return

        ava_score, challenger_score, rationale = self._score_pair(
            prompt, ava_response, ava_model, challenger_response, challenger
        )

        with self._lock:
            s = self._state
            s.total_samples += 1
            if ava_score > challenger_score:
                s.ava_wins += 1
            elif challenger_score > ava_score:
                s.challenger_wins += 1
            else:
                s.ties += 1

            if len(s.samples) < 50:
                s.samples.append({
                    "prompt_preview": prompt[:120],
                    "ava_model": ava_model,
                    "ava_score": round(ava_score, 3),
                    "challenger_model": challenger,
                    "challenger_score": round(challenger_score, 3),
                    "ava_wins": ava_score > challenger_score,
                    "timestamp": time.time(),
                    "judge_rationale": rationale[:300],
                })

            win_rate = s.ava_wins / s.total_samples if s.total_samples > 0 else 0.0
            if s.total_samples >= _MIN_SAMPLES and win_rate >= _CONFIRMED_THRESHOLD:
                s.status = "confirmed_primary"
                s.decision_ts = time.time()
                s.decision_reason = (
                    f"ava-personal won {s.ava_wins}/{s.total_samples} comparisons "
                    f"(win_rate={win_rate:.2f}). Self-confirmed as primary brain."
                )
                print(f"[model_eval_p44] CONFIRMED PRIMARY: {s.decision_reason}")
            elif s.total_samples >= _FLAGGED_MIN_SAMPLES and win_rate < _FLAGGED_THRESHOLD:
                s.status = "flagged_for_review"
                s.decision_ts = time.time()
                s.decision_reason = (
                    f"ava-personal won only {s.ava_wins}/{s.total_samples} comparisons "
                    f"(win_rate={win_rate:.2f}). Flagged — may not be ready as primary."
                )
                print(f"[model_eval_p44] FLAGGED FOR REVIEW: {s.decision_reason}")

            self._save(s)

    def _score_pair(
        self,
        prompt: str,
        ava_response: str,
        ava_model: str,
        challenger_response: str,
        challenger_model: str,
    ) -> tuple[float, float, str]:
        """Score both responses. Returns (ava_score, challenger_score, rationale)."""
        try:
            from langchain_ollama import ChatOllama
            from langchain_core.messages import HumanMessage
            from brain.model_routing import discover_available_model_tags
        except ImportError:
            return self._heuristic_score(ava_response, challenger_response)

        judge_prompt = (
            "Score two AI responses to the same user message.\n"
            "Criteria: naturalness, helpfulness, personality coherence.\n\n"
            f"USER: {prompt[:400]}\n\n"
            f"RESPONSE A ({ava_model}):\n{ava_response[:500]}\n\n"
            f"RESPONSE B ({challenger_model}):\n{challenger_response[:500]}\n\n"
            'Reply as JSON only: {"score_a": 0.0, "score_b": 0.0, "reason": "brief"}'
        )

        tags, _ = discover_available_model_tags()
        for judge in ["mistral:7b", "llama3.1:8b", "gemma2:9b"]:
            if tags and judge not in tags:
                continue
            try:
                llm = ChatOllama(model=judge, temperature=0.1)
                result = llm.invoke([HumanMessage(content=judge_prompt)])
                txt = (getattr(result, "content", None) or str(result)).strip()
                start, end = txt.find("{"), txt.rfind("}") + 1
                if start >= 0 and end > start:
                    blob = json.loads(txt[start:end])
                    sa = max(0.0, min(1.0, float(blob.get("score_a", 0.5))))
                    sb = max(0.0, min(1.0, float(blob.get("score_b", 0.5))))
                    return sa, sb, str(blob.get("reason", ""))[:300]
            except Exception:
                continue

        return self._heuristic_score(ava_response, challenger_response)

    @staticmethod
    def _heuristic_score(ava: str, challenger: str) -> tuple[float, float, str]:
        def score(text: str) -> float:
            n = len(text.strip())
            if n < 20:
                return 0.3
            if n < 80:
                return 0.65
            if n < 350:
                return 0.80
            return max(0.5, 0.80 - (n - 350) / 2000)
        return score(ava), score(challenger), "heuristic_length"


# Module-level singleton
_evaluator: Optional[ModelSelfEvaluator] = None
_evaluator_lock = threading.Lock()


def get_evaluator(base_dir: Optional[Path] = None) -> ModelSelfEvaluator:
    global _evaluator
    with _evaluator_lock:
        if _evaluator is None:
            _evaluator = ModelSelfEvaluator(base_dir=base_dir)
    return _evaluator
