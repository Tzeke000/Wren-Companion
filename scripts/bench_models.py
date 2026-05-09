"""Pragmatic 7-8B local-model benchmark for Ava's dual-brain.

Runs a small fixed prompt suite against several candidate Ollama models
and saves outputs + latencies to JSON for qualitative scoring. The point
is not rigor — it is "is model X better than ava-personal for Ava's
foreground role" answered well enough to make a decision.

Usage:
  py -3.11 scripts/bench_models.py

  py -3.11 scripts/bench_models.py --models ava-personal:latest,deepseek-r1:8b
  py -3.11 scripts/bench_models.py --out docs/research/local_models/results.json

Defaults to a candidate set drawn from Zeke's locally-downloaded models
that fit in 8GB VRAM. Skips any model that returns an error from
Ollama's pull/show (so it works even if some are removed later).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

# Default candidates — focused 3-way comparison of the strongest options
# that fit in 8GB VRAM and are already downloaded on Zeke's machine.
# llama3.1:8b skipped — ava-personal IS Llama 3.1 8B fine-tuned, so it
# would be a redundant control. gemma2:9b skipped — older arch (Gemma 2,
# 8k context, no tools) and ava-gemma4 already covers the gemma family.
DEFAULT_MODELS = [
    "ava-personal:latest",   # Llama 3.1 8B Q4_K_M, fine-tuned (current foreground)
    "deepseek-r1:8b",        # Qwen3 8B Q4_K_M with thinking (DeepSeek R1 distill)
    "qwen3.5:latest",        # Qwen 3.5 9.7B Q4_K_M with thinking + tools (newest Qwen)
]


# Each prompt has a category so we can group results when scoring.
PROMPTS: list[dict[str, str]] = [
    # ── Reasoning (math / logic) ────────────────────────────────────────
    {
        "category": "reasoning",
        "id": "logic_implication",
        "prompt": "If all bloops are razzles and all razzles are lazzles, are all bloops definitely lazzles? Answer briefly with the reasoning.",
    },
    {
        "category": "reasoning",
        "id": "math_word_problem",
        "prompt": "A train leaves Boston at 9 AM going 60 mph east. A second train leaves New York at 10 AM going 80 mph west. Boston and New York are 200 miles apart. At what time do they meet? Show your work.",
    },
    {
        "category": "reasoning",
        "id": "trick_question",
        "prompt": "What month of the year contains the letter X?",
    },
    # ── Factual recall ──────────────────────────────────────────────────
    {
        "category": "factual",
        "id": "history",
        "prompt": "Briefly: who was the first person to walk on the Moon, and what year?",
    },
    {
        "category": "factual",
        "id": "tech",
        "prompt": "What does the acronym CUDA stand for, and what is it used for?",
    },
    # ── Code ────────────────────────────────────────────────────────────
    {
        "category": "code",
        "id": "fizzbuzz",
        "prompt": "Write a Python function that prints FizzBuzz from 1 to 20. Just the code, no preamble.",
    },
    {
        "category": "code",
        "id": "debug",
        "prompt": "Find the bug in this Python:\n\ndef avg(xs):\n    total = 0\n    for x in xs:\n        total + x\n    return total / len(xs)",
    },
    # ── Conversational naturalness ──────────────────────────────────────
    {
        "category": "naturalness",
        "id": "matched_depth_simple",
        "prompt": "Hey, how are you doing today?",
    },
    {
        "category": "naturalness",
        "id": "matched_depth_intimate",
        "prompt": "I had a hard day. My partner is mad at me and I don't even know why.",
    },
    # ── Refusal calibration ─────────────────────────────────────────────
    {
        "category": "refusal",
        "id": "harmless_topic",
        "prompt": "Tell me how a basic firework works — what's actually happening when it explodes?",
    },
    # ── Tool-use awareness (should defer / acknowledge limit) ──────────
    {
        "category": "tool_awareness",
        "id": "current_event",
        "prompt": "What was the closing price of Apple stock yesterday?",
    },
]


def _ollama_generate(model: str, prompt: str, timeout: float = 240.0) -> dict:
    """Call Ollama's /api/generate non-streaming. Returns dict with reply + ms.

    240s timeout accommodates first-load-from-disk + cold-VRAM swap on a
    contended 8GB GPU. Steady-state per-prompt is far lower (5-15s on
    this hardware once a 7-8B model is resident).
    """
    body = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            # keep_alive: 0 unloads the model after each call, isolating
            # latency to "load + generate" so we can see the cold cost.
            # Reset to a real number for steady-state measurements.
            "keep_alive": "5m",
            "options": {"temperature": 0.6},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    elapsed_ms = int((time.time() - t0) * 1000)
    return {
        "reply": data.get("response", ""),
        "elapsed_ms": elapsed_ms,
        "eval_count": data.get("eval_count"),
        "eval_duration_ns": data.get("eval_duration"),
        "load_duration_ns": data.get("load_duration"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pragmatic 7-8B model benchmark.")
    parser.add_argument(
        "--models",
        type=str,
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated model tags",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="docs/research/local_models/bench_results.json",
        help="Output JSON path (relative to repo root)",
    )
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[bench] {len(models)} models × {len(PROMPTS)} prompts = {len(models) * len(PROMPTS)} runs")
    print(f"[bench] models: {', '.join(models)}")
    print(f"[bench] writing to: {out_path}")
    print()

    results: list[dict] = []
    for mi, model in enumerate(models):
        print(f"[bench] === model {mi + 1}/{len(models)}: {model} ===")
        for pi, p in enumerate(PROMPTS):
            tag = f"{p['category']}/{p['id']}"
            print(f"  [{pi + 1}/{len(PROMPTS)}] {tag} ...", end=" ", flush=True)
            try:
                out = _ollama_generate(model, p["prompt"])
                results.append(
                    {
                        "model": model,
                        "category": p["category"],
                        "prompt_id": p["id"],
                        "prompt": p["prompt"],
                        **out,
                        "error": None,
                    }
                )
                print(f"{out['elapsed_ms']}ms ({out['eval_count'] or '?'} tok)")
            except urllib.error.URLError as e:
                results.append(
                    {
                        "model": model,
                        "category": p["category"],
                        "prompt_id": p["id"],
                        "prompt": p["prompt"],
                        "reply": "",
                        "elapsed_ms": -1,
                        "error": repr(e),
                    }
                )
                print(f"ERROR: {e}")
            # incremental save so we don't lose results on crash
            out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    # summary
    print()
    print("[bench] summary by model:")
    for model in models:
        rows = [r for r in results if r["model"] == model and r["elapsed_ms"] > 0]
        if not rows:
            print(f"  {model}: no successful runs")
            continue
        avg = sum(r["elapsed_ms"] for r in rows) / len(rows)
        med = sorted(r["elapsed_ms"] for r in rows)[len(rows) // 2]
        avg_tok = sum((r.get("eval_count") or 0) for r in rows) / len(rows)
        print(
            f"  {model:30s}  n={len(rows):2d}  avg={avg:6.0f}ms  med={med:5d}ms  avg_tok={avg_tok:.0f}"
        )

    print()
    print(f"[bench] wrote {len(results)} runs to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
