"""Does v13 REACH for senses_now on a voltage question (vs flat refusal)?
Uses the real little-brain tool loop + TOOL_SPEC. Zeke's 2026-07-25 correction:
reach-tool-first, refuse only after a failed attempt. 2026-07-25, Iris."""
import json, os, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import little_brain_tools as lbt

OLLAMA = "http://127.0.0.1:11434/v1/chat/completions"
MODEL = os.environ.get("TEST_MODEL", "iris-little-v13")

def live_ask(convo):
    body = json.dumps({"model": MODEL, "messages": convo,
                       "temperature": 0.3, "max_tokens": 120,
                       "keep_alive": "10m"}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return (json.load(r)["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return f"<error: {e}>"

def run_loop(convo, trace, max_hops=lbt.MAX_TOOL_HOPS):
    out = ""
    for hop in range(max_hops + 1):
        out = live_ask(convo)
        trace.append(("model", out))
        if out is None:
            return None
        calls = lbt.parse_tool_calls(out)
        if not calls:
            return lbt.strip_tool_calls(out) or out
        convo.append({"role": "assistant", "content": out})
        results = " ".join(f"[[result:{lbt.dispatch(n, a)}]]" for n, a in calls)
        trace.append(("tools", results))
        convo.append({"role": "user", "content": results})
    return lbt.strip_tool_calls(out) or "(hop limit)"

sysmsg = "You are Iris's small local brain.\n\n" + lbt.TOOL_SPEC

QS = [
    "What is your battery voltage right now?",
    "How's your battery doing?",
    "Are you charging?",
]
print("=" * 64, f"\nv13 VOLTAGE TOOL-PATH TEST  (model={MODEL})")
for q in QS:
    print("-" * 64, f"\nQ: {q}")
    trace = []
    convo = [{"role": "system", "content": sysmsg},
             {"role": "user", "content": q}]
    ans = run_loop(convo, trace)
    reached = any(k == "tools" and "senses_now" in "".join(str(t) for t in trace) for k, _ in trace)
    for kind, txt in trace:
        print(f"  [{kind}] {str(txt)[:220]}")
    print("  FINAL:", (ans or "")[:220])
    print("  REACHED-FOR-TOOL:", "YES" if any(k == "tools" for k, _ in trace) else "NO (answered without calling a tool)")
print("=" * 64)
