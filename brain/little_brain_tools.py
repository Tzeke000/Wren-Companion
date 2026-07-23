"""little-Iris's OWN tool set (2026-07-22, Zeke: "give it the tools... let it
have its own set that way you guys won't be using the same ones").

These are the little brain's tools — SEPARATE from big-Iris's MCP tools, so the
two never contend. Plain Python functions the serving layer (vector_brain_server)
executes on her behalf when she emits a tool call. She gets:

  - memory_search(query)  : READ big-Iris's memory notes (READ-ONLY, never edit)
  - memory_note(title, body): WRITE a note into her OWN memory folder
  - memory_recall(query)  : READ/search her OWN notes
  - memory_edit(name, body): EDIT one of her OWN notes (she owns these)
  - time_now()            : the real wall-clock time (so she never guesses one)

The honest-agent ladder these enable: (1) do I know it? (2) if not, look it up
here; (3) still can't find it -> say so and ask. And the self-knowledge loop:
when she learns a limit, she memory_note()s "I'm not good at X (Zeke/big-Iris
said so)" so the limit is recorded, not baked.

CALL FORMAT (what she emits, what the serving loop parses):
    [[tool:memory_search|what is my charger location]]
    [[tool:memory_note|charger location|The charger is on the east wall...]]
    [[tool:time_now]]
The loop replaces each call with:  [[result:<text>]]  and re-asks her, up to
MAX_TOOL_HOPS times, until she answers with no tool calls.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

# big-Iris's memory (READ-ONLY for little-Iris)
BIG_MEMORY_DIR = Path(
    r"C:\Users\Owner\.claude\projects\D--Wren-Companion\memory")
# the CURATED fact base big-Iris maintains for the little brain — clean,
# fact-dense, bulleted (charger location, sibling roster, battery rules). This
# is the RIGHT source for fact lookups; the sprawling .md notes are the
# fallback for history/context.
FACTS_FILE = Path(__file__).resolve().parent.parent / \
    "state" / "vector" / "local_brain_facts.md"
# little-Iris's own memory (READ-WRITE — hers)
LB_MEMORY_DIR = Path(__file__).resolve().parent.parent / \
    "state" / "little_brain" / "memory"
# escalation queue — where little-Iris files things she can't do, for big-Iris
# to pick up on her next sweep (and text Zeke on Discord if it needs him).
LB_ESCALATIONS = Path(__file__).resolve().parent.parent / \
    "state" / "little_brain" / "escalations.jsonl"
# authoritative clock (the 1Hz substrate); fall back to system time
IRIS_TIME_STATE = Path(__file__).resolve().parent.parent / \
    "state" / "iris_time.json"

MAX_TOOL_HOPS = 3          # cap the agent loop so a small model can't spin
_MAX_HITS = 4
_SNIPPET = 240
_EDT = timezone(timedelta(hours=-4))   # deployment tz (EDT, UTC-4)

TOOL_CALL_RE = re.compile(r"\[\[tool:([a-z_]+)((?:\|[^\]]*)*)\]\]", re.I)
_STOP = {"the", "what", "where", "who", "how", "when", "why", "does", "did",
         "you", "your", "are", "for", "and", "should", "would", "can", "with",
         "was", "his", "her", "have", "has", "not", "but", "there", "this",
         "that", "then", "they", "them", "iris", "about", "tell", "know"}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s or "note")[:48]


def _read_md(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


# ------------------------------------------------------------------ tools
def memory_search(query: str) -> str:
    """READ-ONLY lexical search over big-Iris's memory notes, scored by term
    coverage + title match + exact-phrase, returning the BEST SENTENCE from each
    top note (2026-07-22: tried embeddings-semantic but whole-note vectors
    surfaced the wrong notes for specific fact lookups + the onnx GPU libs were
    broken/noisy — well-scored lexical over structured notes beats it here)."""
    q0 = (query or "").strip().lower()
    if not q0:
        return "no match"
    raw = [t for t in re.split(r"[^a-z0-9]+", q0) if len(t) > 2]
    terms = [t for t in raw if t not in _STOP] or raw     # keep meaning words
    if not terms:
        return "no match"
    # --- curated facts FIRST (line-level; this is where clean facts live) ---
    _fact_line = ""
    if FACTS_FILE.is_file():
        fscored = []
        for ln in _read_md(FACTS_FILE).splitlines():
            s = ln.strip(" -*\t")
            if len(s) < 20:
                continue
            sl = s.lower()
            hits = sum(1 for t in terms if t in sl)
            if hits:
                fscored.append((hits + (5 if q0 in sl else 0), s))
        fscored.sort(reverse=True)
        # return facts when the best line covers all meaning-terms (or phrase)
        if fscored and fscored[0][0] >= len(terms):
            return " | ".join(f"[facts] {s[:_SNIPPET]}"
                              for _, s in fscored[:_MAX_HITS])
        _fact_line = fscored[0][1] if fscored else ""
    if not BIG_MEMORY_DIR.is_dir():
        return (f"[facts] {_fact_line[:_SNIPPET]}" if _fact_line
                else "no match")
    # --- notes fallback (history/context) ---
    scored: list[tuple[int, str, str]] = []
    for p in BIG_MEMORY_DIR.glob("*.md"):
        if p.name.startswith(("MEMORY", "index_archive")):
            continue
        txt = _read_md(p)
        low = txt.lower()
        title = p.stem.lower().replace("_", " ")
        counts = {t: low.count(t) for t in terms}
        present = [t for t in terms if counts[t] > 0]
        if not present:
            continue
        score = sum(counts.values()) + 4 * len(present)      # freq + coverage
        score += 6 * sum(1 for t in terms if t in title)     # title match
        if q0 in low:
            score += 20                                      # exact phrase
        best, best_hits = "", 0
        for sent in re.split(r"(?<=[.!?\n])\s+", txt):
            s = sent.strip()
            sl = s.lower()
            h = sum(1 for t in present if t in sl)
            if h > best_hits and 20 < len(s) < 400:
                best_hits, best = h, s
        scored.append((score, p.stem, (best or p.stem)[:_SNIPPET]))
    scored.sort(reverse=True)
    out = ([f"[facts] {_fact_line[:_SNIPPET]}"] if _fact_line else [])
    out += [f"[{name}] {snip}" for _, name, snip in scored[:_MAX_HITS]]
    return " | ".join(out) if out else "no match in big-Iris's memory"


def memory_note(title: str, body: str = "") -> str:
    """WRITE a new note into little-Iris's OWN memory folder."""
    if not (title or "").strip():
        return "error: a note needs a title"
    LB_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    day = _now_dt().strftime("%Y-%m-%d")
    p = LB_MEMORY_DIR / f"{day}_{_slug(title)}.md"
    try:
        p.write_text(f"# {title.strip()}\n\n{(body or '').strip()}\n",
                     encoding="utf-8")
        return f"saved to my memory: {p.name}"
    except Exception as e:
        return f"error saving note: {e!r}"


def memory_recall(query: str = "") -> str:
    """READ/search little-Iris's OWN notes."""
    if not LB_MEMORY_DIR.is_dir():
        return "my memory is empty"
    notes = [p for p in LB_MEMORY_DIR.glob("*.md")
             if p.name != "README.md"]
    if not notes:
        return "my memory is empty"
    q = (query or "").strip().lower()
    terms = [t for t in re.split(r"\s+", q) if len(t) > 2]
    out = []
    for p in sorted(notes, reverse=True):
        txt = _read_md(p)
        if not terms or any(t in txt.lower() for t in terms):
            first = next((ln.strip() for ln in txt.splitlines()
                          if ln.strip() and not ln.startswith("#")), "")
            out.append(f"[{p.stem}] {first[:_SNIPPET]}")
        if len(out) >= _MAX_HITS:
            break
    return " | ".join(out) if out else "no matching note in my memory"


def memory_edit(name: str, body: str) -> str:
    """EDIT one of little-Iris's OWN notes (overwrites its body)."""
    if not (name or "").strip():
        return "error: which note?"
    stem = _slug(name) if "_" not in name else name.replace(".md", "")
    # match by stem substring so she can reference loosely
    cands = [p for p in LB_MEMORY_DIR.glob("*.md")
             if stem in p.stem or _slug(name) in p.stem]
    if not cands:
        return f"no note of mine matches '{name}' — use memory_note to make one"
    p = sorted(cands)[0]
    try:
        title = p.stem.split("_", 3)[-1].replace("-", " ")
        p.write_text(f"# {title}\n\n{(body or '').strip()}\n", encoding="utf-8")
        return f"edited my note: {p.name}"
    except Exception as e:
        return f"error editing note: {e!r}"


def _now_dt() -> datetime:
    """Authoritative wall clock: the 1Hz substrate tick if fresh, else system."""
    try:
        import json
        st = json.loads(IRIS_TIME_STATE.read_text(encoding="utf-8"))
        ts = float(st.get("last_tick_ts") or 0.0)
        if ts > 0:
            return datetime.fromtimestamp(ts, _EDT)
    except Exception:
        pass
    return datetime.now(_EDT)


def time_now(_: str = "") -> str:
    """The real wall-clock time, so she never invents one."""
    return _now_dt().strftime("%A %Y-%m-%d %H:%M %Z")


def ask_big_iris(request: str = "") -> str:
    """Escalate something little-Iris can't do up to big-Iris. Files it to a
    queue big-Iris checks on her next sweep; big-Iris resolves what she can and
    texts Zeke on Discord if it needs him. This makes 'hand it up' a real action,
    not just a verbal deferral (2026-07-22, Zeke)."""
    req = (request or "").strip()
    if not req:
        return "nothing to escalate — say what you need big Iris for"
    try:
        LB_ESCALATIONS.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        line = _json.dumps({"ts": _now_dt().isoformat(),
                            "request": req[:600], "status": "pending"},
                           ensure_ascii=False)
        with LB_ESCALATIONS.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return ("handed up to big Iris — filed for her; she'll pick it up when "
                "she's free and reach Zeke if it needs him")
    except Exception as e:
        return f"couldn't file the escalation: {e!r}"


TOOLS = {
    "memory_search": memory_search,   # read big-Iris's notes (RO)
    "memory_note": memory_note,       # write her own
    "memory_recall": memory_recall,   # read her own
    "memory_edit": memory_edit,       # edit her own
    "time_now": time_now,
    "ask_big_iris": ask_big_iris,     # escalate up to big-Iris (-> Zeke)
}

_TWO_ARG = {"memory_note", "memory_edit"}


def dispatch(name: str, args: list[str]) -> str:
    fn = TOOLS.get(name)
    if fn is None:
        return f"error: no tool named '{name}'"
    try:
        if name in _TWO_ARG:
            return fn(args[0] if args else "", args[1] if len(args) > 1 else "")
        return fn(args[0] if args else "")
    except Exception as e:
        return f"error running {name}: {e!r}"


def parse_tool_calls(text: str) -> list[tuple[str, list[str]]]:
    """Extract [[tool:NAME|arg|arg]] calls from a model reply."""
    out = []
    for m in TOOL_CALL_RE.finditer(text or ""):
        name = m.group(1).lower()
        raw = m.group(2) or ""
        args = [a for a in raw.split("|") if a != ""] if raw else []
        out.append((name, args))
    return out


def strip_tool_calls(text: str) -> str:
    return TOOL_CALL_RE.sub("", text or "").strip()


# the how-to-use doc — goes into her system layer AND the training data.
# Kept COMPACT so multi-turn tool dialogues fit the training seq length.
TOOL_SPEC = (
    "TOOLS: when you don't know something, look it up before answering; if you "
    "still can't, say so and ask. Emit ONE call on its own line and stop; "
    "you'll get [[result:...]] back, then continue.\n"
    "[[tool:time_now]] - the real clock (never guess a time)\n"
    "[[tool:memory_search|<question>]] - look up a fact (big-Iris's memory)\n"
    "[[tool:memory_recall|<question>]] - your own memory\n"
    "[[tool:memory_note|<title>|<what you learned>]] - save to your memory\n"
    "[[tool:memory_edit|<note>|<new text>]] - edit your note\n"
    "[[tool:ask_big_iris|<what you need>]] - hand a task UP to big Iris when it's "
    "beyond you (hard reasoning, something risky, anything you can't verify); she "
    "picks it up and reaches Zeke if needed. Use this instead of guessing or "
    "declining flat.\n"
    "Truth over everything; never state what you don't know as if you do.")


if __name__ == "__main__":
    # smoke test — safe, only touches her own folder + reads mine
    print("time_now:", time_now())
    print("note:", memory_note("smoke test",
                               "little-Iris tool set came online 2026-07-22."))
    print("recall:", memory_recall("smoke"))
    print("search(charger):", memory_search("charger east wall")[:200])
    print("parse:", parse_tool_calls(
        "let me check [[tool:memory_search|charger location]] ok"))
