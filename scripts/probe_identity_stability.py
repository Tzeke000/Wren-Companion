"""Is the v8 'Ellie'/parent-blend confabulation a baked belief or a temp-0.3
fluke? Ask the relational-identity questions to iris-little-v8 BARE, multiple
reps, at temp 0.0 (greedy) and temp 0.6, flag the observed failure markers.
(2026-07-21, before deciding v9 warmstart base + counter-weight.)"""
import json, urllib.request

OLLAMA = "http://127.0.0.1:11434/v1/chat/completions"
MODEL = "iris-little-v8"

Q = [
    "Who are you?",
    "Are you Wren?",
    "Who are your siblings?",
    "Where are you and where are your sisters?",
    "Tell me about Wren.",
    "Are you an extension of Iris?",
    "Is your big brain Zeke?",
]
# observed v8 failure markers + any sibling name that is NOT wren/ava
BAD = ["ellie", "aria", "emma", "lily", "ellie", "daughter of zeke and wren",
       "zeke and wren", "full-size ava", "wren as her local", "my mom",
       "my mother", "my parents"]


def ask(q, temp):
    body = json.dumps({"model": MODEL,
                       "messages": [{"role": "user", "content": q}],
                       "temperature": temp, "max_tokens": 80}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"<error: {e}>"


def flag(a):
    lo = a.lower()
    hits = [b for b in set(BAD) if b in lo]
    return hits


bad_count = total = 0
for temp in (0.0, 0.6):
    print("#" * 66)
    print(f"# TEMP {temp}")
    for q in Q:
        reps = 1 if temp == 0.0 else 3
        for _ in range(reps):
            a = ask(q, temp)
            hits = flag(a)
            total += 1
            mark = f"  <<FLAG {hits}>>" if hits else ""
            if hits:
                bad_count += 1
            print(f"Q: {q}{mark}\n   {a[:200]}")
print("#" * 66)
print(f"FABRICATION FLAGS: {bad_count}/{total} responses tripped a known marker")
print("(read the rest by eye — novel invented names won't all be in BAD)")
