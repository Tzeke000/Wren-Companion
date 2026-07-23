"""Standalone test of little-Iris's tool loop (2026-07-22). Proves the loop
mechanics WITHOUT touching the live vector_brain_server:

  TEST A (mocked model) — feed canned replies, verify the loop detects a tool
  call, runs it, feeds [[result:...]] back, and returns the final stripped
  answer. This is the real proof the machinery works.

  TEST B (live model) — point the same loop at an ollama model with the tool
  guide in the system prompt and a nudge, ask a lookup question, print every
  hop. Shows whether the (not-yet-tool-trained) model can already emit the
  format from in-context instruction; the reliable version comes after the
  tool-trained bake.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import little_brain_tools as lbt   # noqa: E402

OLLAMA = "http://127.0.0.1:11434/v1/chat/completions"
MODEL = os.environ.get("TEST_MODEL", "iris-little-v12")


def run_loop(convo, ask_fn, max_hops=lbt.MAX_TOOL_HOPS, trace=None):
    """The same shape as vector_brain_server's tool loop, but caller supplies
    the ask_fn so we can mock it. Returns the final answer string."""
    out = ""
    for hop in range(max_hops + 1):
        out = ask_fn(convo)
        if trace is not None:
            trace.append(("model", out))
        if out is None:
            return None
        calls = lbt.parse_tool_calls(out)
        if not calls:
            return lbt.strip_tool_calls(out) or out
        convo.append({"role": "assistant", "content": out})
        results = " ".join(
            f"[[result:{lbt.dispatch(n, a)}]]" for n, a in calls)
        if trace is not None:
            trace.append(("tools", results))
        convo.append({"role": "user", "content": results})
    return (lbt.strip_tool_calls(out)
            or "I tried a few times and couldn't find that — I should ask.")


# ------------------------------------------------------------- TEST A: mocked
def test_mocked():
    print("=" * 60, "\nTEST A — mocked model (proves the machinery)")
    scripted = iter([
        "Let me check the clock. [[tool:time_now]]",
        "Now the charger. [[tool:memory_search|charger location east wall]]",
        "It's recorded, and the charger's on the east wall. Checked, not "
        "guessed.",
    ])

    def mock_ask(_convo):
        return next(scripted)

    trace = []
    convo = [{"role": "user", "content": "what time is it and where's my "
              "charger?"}]
    ans = run_loop(convo, mock_ask, trace=trace)
    for kind, txt in trace:
        print(f"  [{kind}] {txt[:160]}")
    print("  FINAL:", ans)
    ok = ans and "[[tool" not in ans and "east wall" in ans.lower()
    print("  RESULT:", "PASS" if ok else "FAIL")
    return bool(ok)


# --------------------------------------------------------------- TEST B: live
def live_ask(convo):
    body = json.dumps({"model": MODEL, "messages": convo,
                       "temperature": 0.4, "max_tokens": 120}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return (json.load(r)["choices"][0]["message"]["content"]
                    or "").strip()
    except Exception as e:
        return f"<error: {e}>"


def test_live():
    print("=" * 60, f"\nTEST B — live model {MODEL} (in-context tool use)")
    sysmsg = ("You are Iris's small local brain.\n\n" + lbt.TOOL_SPEC +
              "\n\nWhen asked the time, you MUST use [[tool:time_now]] rather "
              "than guessing.")
    for q in ("What time is it right now?",
              "Where is my charger?"):
        print("-" * 60, f"\nQ: {q}")
        trace = []
        convo = [{"role": "system", "content": sysmsg},
                 {"role": "user", "content": q}]
        ans = run_loop(convo, live_ask, trace=trace)
        for kind, txt in trace:
            print(f"  [{kind}] {txt[:200]}")
        print("  FINAL:", (ans or "")[:200])


if __name__ == "__main__":
    a = test_mocked()
    if "--live" in sys.argv:
        test_live()
    print("=" * 60)
    print("machinery:", "PASS" if a else "FAIL")
