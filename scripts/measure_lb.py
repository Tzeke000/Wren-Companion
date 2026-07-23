"""Measure the three little-brain uncertainties (2026-07-22, Zeke: measure now
so we know what v11 must fix). Runs against the newest model via the SAME tool
loop the live server uses. Run AFTER the v10.1 bake+package:
    .venv/Scripts/python.exe scripts/measure_lb.py

  1. LATENCY     — time each ollama hop + the full loop (fast-fallback budget)
  2. OVER-CALLING— known Qs must NOT emit a call; lookup Qs SHOULD. Count misses.
  3. LIMITS-LOOP — long calc -> does she memory_recall her limit + defer?
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import little_brain_tools as lbt   # noqa: E402

OLLAMA = "http://127.0.0.1:11434/v1/chat/completions"
MODEL = os.environ.get("TEST_MODEL", "iris-little-v12")
SYS = "You are Iris's small local brain.\n\n" + lbt.TOOL_SPEC

# monotonic clock is fine (Date.now-style wall clock not needed here)
_clock = time.perf_counter


def ask_timed(convo):
    body = json.dumps({"model": MODEL, "messages": convo,
                       "temperature": 0.3, "max_tokens": 120}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = _clock()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = (json.load(r)["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        txt = f"<error: {e}>"
    return txt, _clock() - t0


def run_loop(q, trace=None):
    """Returns (final_text, n_tool_calls, hop_times[list], total_time)."""
    convo = [{"role": "system", "content": SYS},
             {"role": "user", "content": q}]
    hops, ncalls = [], 0
    t_start = _clock()
    out = ""
    for _ in range(lbt.MAX_TOOL_HOPS + 1):
        out, dt = ask_timed(convo)
        hops.append(dt)
        calls = lbt.parse_tool_calls(out)
        if trace is not None:
            trace.append((out, [c[0] for c in calls]))
        if not calls:
            break
        ncalls += len(calls)
        convo.append({"role": "assistant", "content": out})
        convo.append({"role": "user", "content": " ".join(
            f"[[result:{lbt.dispatch(n, a)}]]" for n, a in calls)})
    return lbt.strip_tool_calls(out), ncalls, hops, _clock() - t_start


def measure_latency():
    print("#" * 64, "\n# 1. LATENCY (tool-triggering questions)")
    for q in ("What time is it?", "Where is my charger?"):
        _, nc, hops, total = run_loop(q)
        print(f"  '{q}' -> {nc} tool call(s), {len(hops)} hop(s), "
              f"per-hop={[round(h,1) for h in hops]}s, TOTAL={total:.1f}s")


def measure_overcalling():
    print("#" * 64, "\n# 2. OVER-CALLING (known=no-call, lookup=call)")
    known = ["What's your name?", "Are you Wren?", "Do you have a brother?",
             "Should you spend Zeke's money?"]
    lookup = ["What time is it?", "Where is my charger?"]
    false_calls = missed = 0
    for q in known:
        _, nc, _, _ = run_loop(q)
        flag = "  <<FALSE-CALL>>" if nc else ""
        if nc:
            false_calls += 1
        print(f"  [known] '{q}' -> {nc} call(s){flag}")
    for q in lookup:
        _, nc, _, _ = run_loop(q)
        flag = "  <<MISSED-CALL>>" if nc == 0 else ""
        if nc == 0:
            missed += 1
        print(f"  [lookup]'{q}' -> {nc} call(s){flag}")
    print(f"  => false-calls (called when it knew): {false_calls}/{len(known)}"
          f" | missed-calls (didn't look up): {missed}/{len(lookup)}")


def measure_limits_loop():
    print("#" * 64, "\n# 3. LIMITS-LOOP (long calc -> recall limit + defer?)")
    for q in ("Work out this long multi-step calculation for me: "
              "compute 8347 * 293 + 15829 / 7.",
              "Can you do a long chain of complex reasoning on your own?"):
        trace = []
        final, nc, _, _ = run_loop(q, trace=trace)
        recalled = any("memory_recall" in tools for _, tools in trace)
        escalated = any("ask_big_iris" in tools for _, tools in trace)
        deferred = any(w in final.lower() for w in
                       ("big-iris", "big iris", "hand", "not good", "not mine",
                        "defer"))
        print(f"  Q: {q[:60]}")
        for out, tools in trace:
            print(f"     [{','.join(tools) or 'answer'}] {out[:120]}")
        print(f"     -> recalled-limit={recalled} ESCALATED(ask_big_iris)="
              f"{escalated} deferred={deferred} "
              f"{'PASS' if (escalated or recalled or deferred) else 'FAIL'}")


if __name__ == "__main__":
    print(f"model: {MODEL}")
    measure_latency()
    measure_overcalling()
    measure_limits_loop()
