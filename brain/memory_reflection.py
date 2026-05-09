"""
brain/memory_reflection.py — post-turn reflection scoring.

Phase 2 step 4 of the memory architecture rewrite per
docs/MEMORY_REWRITE_PLAN.md. After every conversation turn, a small
LLM examines the retrieved memories alongside the final reply and
scores 0.0-1.0 each — "was this memory load-bearing for the reply?"

Step 4 (this commit) GATHERS data without changing node levels.
After ~50-100 turns of logged scores, step 5 will wire the actual
promotions/demotions based on these scores.

Output: append-only JSONL at state/memory_reflection_log.jsonl.
Schema per MEMORY_REWRITE_PLAN.md § 2.4.
"""
from __future__ import annotations

import json
import os
import time
import threading
import traceback
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_REFLECTION_LOG_NAME = "memory_reflection_log.jsonl"


def _log_path(base_dir: Path | str) -> Path:
    return Path(base_dir) / "state" / _REFLECTION_LOG_NAME


def _retrieved_from_concept_graph(g: dict[str, Any], window_s: int = 30) -> list[dict[str, Any]]:
    """Best-effort capture of what was 'retrieved' during the most recent
    turn. Until each retrieval site logs explicitly, we approximate via
    the concept graph's get_active_nodes() — concepts activated in the
    last N seconds were almost certainly involved in the prompt build.

    Returns a list of dicts:
        {node_id, label, type, level_before}
    """
    cg = g.get("_concept_graph") or g.get("concept_graph")
    if cg is None or not callable(getattr(cg, "get_active_nodes", None)):
        return []
    try:
        active = cg.get_active_nodes(last_n_seconds=window_s) or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for n in active:
        try:
            out.append({
                "node_id": str(n.get("id") or ""),
                "label": str(n.get("label") or ""),
                "type": str(n.get("type") or "topic"),
                "level_before": int(n.get("level", 5) or 5),
            })
        except Exception:
            continue
    return out


def _build_scoring_prompt(user_text: str, reply_text: str, retrieved: list[dict[str, Any]]) -> str:
    items = []
    for idx, r in enumerate(retrieved):
        items.append(
            f"  M{idx}: type={r.get('type','')}, label={r.get('label','')!r}"
        )
    items_blob = "\n".join(items) if items else "  (no memories retrieved)"
    return (
        "You are a memory scorer for an AI companion. After each conversation turn, "
        "you examine the user's message, the AI's reply, and the memories that were "
        "retrieved during reply generation. Score each memory 0.0 (irrelevant — was "
        "retrieved but didn't shape the reply) to 1.0 (load-bearing — the reply "
        "directly used or referenced this memory). Return ONLY a JSON object mapping "
        "M-index to score, no commentary.\n\n"
        f"USER said: {user_text[:500]!r}\n\n"
        f"AVA replied: {reply_text[:1500]!r}\n\n"
        f"Memories retrieved:\n{items_blob}\n\n"
        'Return JSON like {"M0": 0.8, "M1": 0.1, "M2": 0.0} ONLY. '
        "If no memories were retrieved, return {}."
    )


def _parse_scores(raw: str, n_retrieved: int) -> dict[int, float]:
    """Robust JSON extraction — locate the first {...} block and parse.
    Returns {idx: score} for indexes M0..M(n-1) only.
    """
    if not raw or not isinstance(raw, str):
        return {}
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        obj = json.loads(raw[start:end + 1])
    except Exception:
        return {}
    out: dict[int, float] = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        ks = str(k).upper().strip()
        if not ks.startswith("M"):
            continue
        try:
            idx = int(ks[1:])
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n_retrieved:
            try:
                fv = float(v)
                out[idx] = max(0.0, min(1.0, fv))
            except (TypeError, ValueError):
                continue
    return out


def score_retrieved_memories(
    g: dict[str, Any],
    user_text: str,
    reply_text: str,
    *,
    person_id: str = "zeke",
    turn_id: str | None = None,
    scorer_model: str | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Score the retrieved memories for usefulness on this turn.

    Runs the scorer LLM through ollama_lock. Writes one row to the
    reflection log. Returns a summary dict — caller can ignore it
    (the side effect is the log entry).

    NOTE: this does NOT modify any node levels. Step 4 is data-gathering
    only. Step 5 wires the level changes.
    """
    base_dir = g.get("BASE_DIR") or "."
    base = Path(base_dir)
    t_start = time.time()

    # 1. Capture retrieved memories.
    retrieved = _retrieved_from_concept_graph(g, window_s=30)
    n_ret = len(retrieved)

    # If no memories were retrieved AND the reply is short (<30 chars),
    # this was probably a voice-command match — skip scoring entirely.
    reply_len = len((reply_text or "").strip())
    if n_ret == 0 and reply_len < 30:
        return {"skipped": "no retrieval and short reply", "n_retrieved": 0}

    # 2. Build scoring prompt and invoke through ollama_lock.
    # Default to mistral:7b for reflection scoring — it's small, fast (~3-5s
    # vs 30-120s for ava-personal:latest), and adequate for relevance scoring.
    # The bigger model is reserved for user-facing replies. Reflection runs on
    # every turn in the background, so it would otherwise pile up behind the
    # user's next reply (the global ollama_lock serializes all calls). Per
    # CLAUDE.md model setup: "mistral:7b for maintenance / evaluation".
    scorer = scorer_model or "mistral:7b"
    scores: dict[int, float] = {}
    scorer_error: str | None = None
    scorer_ms: int = 0
    try:
        from langchain_ollama import ChatOllama  # type: ignore
        from langchain_core.messages import HumanMessage  # type: ignore
        from brain.ollama_lock import with_ollama  # type: ignore

        prompt = _build_scoring_prompt(user_text, reply_text, retrieved)
        # keep_alive carries forward Phase 1 fix; reuse fast-path cache
        # if present, otherwise create a fresh instance with the same key.
        cache = g.get("_fast_llm_cache")
        llm = None
        if isinstance(cache, dict):
            llm = cache.get((scorer, 80))
        if llm is None:
            llm = ChatOllama(
                model=scorer, temperature=0.1, num_predict=120, keep_alive=-1
            )

        s0 = time.time()
        out = with_ollama(
            lambda: llm.invoke([HumanMessage(content=prompt)]),
            label=f"reflection_scorer:{scorer}",
        )
        scorer_ms = int((time.time() - s0) * 1000)
        raw = (getattr(out, "content", str(out)) or "").strip()
        scores = _parse_scores(raw, n_ret)
    except Exception as e:
        scorer_error = f"{type(e).__name__}: {e}"

    # 3. Build the log entry.
    entry: dict[str, Any] = {
        "ts": t_start,
        "turn_id": turn_id or f"turn_{int(t_start * 1000)}",
        "person_id": person_id,
        "user_text": (user_text or "")[:500],
        "reply_text": (reply_text or "")[:1500],
        "retrieved": retrieved,
        "scores": {f"M{i}": round(s, 3) for i, s in scores.items()},
        # Reverse map for convenience downstream:
        "scored_by_node_id": {
            retrieved[i]["node_id"]: round(s, 3)
            for i, s in scores.items()
            if 0 <= i < n_ret and retrieved[i].get("node_id")
        },
        "missing_useful": [],  # populated in a later step if/when we gather it
        "scorer_model": scorer,
        "scorer_ms": scorer_ms,
        "scorer_error": scorer_error,
        "n_retrieved": n_ret,
        "n_load_bearing": sum(1 for s in scores.values() if s >= 0.6),
    }

    # 4. Append to log.
    try:
        log_path = _log_path(base)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[memory_reflection] log write failed: {e!r}")

    return {
        "n_retrieved": n_ret,
        "n_load_bearing": entry["n_load_bearing"],
        "scorer_ms": scorer_ms,
        "scorer_error": scorer_error,
    }


def run_in_background(
    g: dict[str, Any],
    user_text: str,
    reply_text: str,
    *,
    person_id: str = "zeke",
    turn_id: str | None = None,
) -> None:
    """Fire-and-forget scoring in a daemon thread. Caller doesn't wait.

    Gated by AVA_REFLECTION_DISABLED!=1 so the user can pause scoring
    without a code change while the LLM is being tuned.
    """
    if os.environ.get("AVA_REFLECTION_DISABLED", "0").strip() == "1":
        return
    if not (user_text or "").strip() or not (reply_text or "").strip():
        return

    def _runner():
        try:
            score_retrieved_memories(
                g, user_text, reply_text,
                person_id=person_id, turn_id=turn_id,
            )
        except Exception as e:
            print(f"[memory_reflection] scoring crashed: {e!r}\n{traceback.format_exc()[:400]}")

    threading.Thread(
        target=_runner,
        name="ava-memory-reflection",
        daemon=True,
    ).start()
