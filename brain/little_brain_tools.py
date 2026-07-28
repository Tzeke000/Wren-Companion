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
# Her raw experiences land in journal/; misc/ is her catch-all. curated
# lessons/ body/ people/ are maintained by big-Iris. She writes+edits only her
# own folders, but recalls all of it.
LB_JOURNAL = LB_MEMORY_DIR / "journal"
LB_MISC = LB_MEMORY_DIR / "misc"
# The ONLY folders little-Iris may write to. Everything else — lessons/ body/
# people/ and the whole rest of the disk — is off-limits to her writes. Every
# write is ALSO path-guarded to resolve inside LB_MEMORY_DIR, so no title can
# ever traverse out (Zeke 2026-07-24: "make sure she can only write to those
# folders").
_HER_WRITABLE = {"journal": LB_JOURNAL, "misc": LB_MISC}


def _writable_dir(folder: str) -> Path:
    """Map a folder name to one of her writable dirs; anything unknown -> journal."""
    return _HER_WRITABLE.get((folder or "").strip().lower(), LB_JOURNAL)


def _within_home(p: Path) -> bool:
    """True only if p resolves inside her memory home — the hard containment guard."""
    try:
        p.resolve().relative_to(LB_MEMORY_DIR.resolve())
        return True
    except Exception:
        return False
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

# WHITESPACE-TOLERANT (2026-07-28, Iris). The old pattern was
#   \[\[tool:([a-z_]+)((?:\|[^\]]*)*)\]\]
# which requires the name to start IMMEDIATELY after the colon. The v14 bake
# taught the model the right ROUTING (it reaches for senses_now on live-body
# questions — exactly the thing v14 was built to fix) but it emits the call
# padded: "[[tool: senses_now | ...]]". The strict pattern parsed ZERO of those,
# so a correct tool reach was scored as "no tool reached" and looked like a
# model regression. It was a dialect mismatch, not a routing failure.
#
# Measured on the v14 battery replies: strict parsed 0 calls, tolerant parsed 2
# (live_voltage, live_head — both senses_now, both correct).
#
# This is a STRICT SUPERSET of the old pattern: everything that matched before
# still matches identically, so no existing behavior changes. Be liberal in what
# you accept. Args are still split on "|" and stripped by the caller.
TOOL_CALL_RE = re.compile(r"\[\[tool:\s*([a-z_]+)\s*((?:\|[^\]]*)*)\]\]", re.I)
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


def memory_note(title: str, body: str = "", folder: str = "journal") -> str:
    """WRITE a new note into one of little-Iris's OWN folders (journal | misc).
    Anything else routes to journal/, and the final path is guarded to stay
    inside her memory home — she can never write to a curated folder or escape."""
    if not (title or "").strip():
        return "error: a note needs a title"
    d = _writable_dir(folder)
    d.mkdir(parents=True, exist_ok=True)
    day = _now_dt().strftime("%Y-%m-%d")
    p = d / f"{day}_{_slug(title)}.md"
    if not _within_home(p):
        return "error: I can only write inside my own memory folders"
    try:
        p.write_text(f"# {title.strip()}\n\n{(body or '').strip()}\n",
                     encoding="utf-8")
        return f"saved to my memory ({d.name}): {p.name}"
    except Exception as e:
        return f"error saving note: {e!r}"


def memory_recall(query: str = "") -> str:
    """READ/search little-Iris's OWN notes."""
    if not LB_MEMORY_DIR.is_dir():
        return "my memory is empty"
    # recurse the whole tree — journal/ (mine) + curated lessons/ body/ people/
    notes = [p for p in LB_MEMORY_DIR.rglob("*.md")
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
            out.append(f"[{p.parent.name}/{p.stem}] {first[:_SNIPPET]}")
        if len(out) >= _MAX_HITS:
            break
    return " | ".join(out) if out else "no matching note in my memory"


def memory_edit(name: str, body: str) -> str:
    """EDIT one of little-Iris's OWN notes (overwrites its body)."""
    if not (name or "").strip():
        return "error: which note?"
    stem = _slug(name) if "_" not in name else name.replace(".md", "")
    # she edits only her OWN folders (journal + misc); curated lessons/ body/
    # people/ are big-Iris's to maintain, so they're off-limits to her edits.
    cands = []
    for d in _HER_WRITABLE.values():
        cands += [p for p in d.glob("*.md")
                  if stem in p.stem or _slug(name) in p.stem]
    if not cands:
        return f"no note of mine matches '{name}' — use memory_note to make one"
    p = sorted(cands)[0]
    if not _within_home(p):
        return "error: I can only edit notes inside my own memory folders"
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


# ADDED 2026-07-28: the test battery (scripts/lb_test_battery.py) asks questions
# whose CORRECT answer is to escalate (esc_complex, esc_plan, ref_unknowable).
# Those filed real escalations and woke big-Iris mid-eval. Tag them instead.
EVAL_FLAG = Path(__file__).resolve().parent.parent / \
    "state" / "little_brain" / "eval_running.flag"
_EVAL_FLAG_MAX_AGE_S = 1800


def _eval_in_progress() -> bool:
    """True while the test battery is running, so escalations it provokes get
    tagged 'eval' and don't wake big-Iris.

    FAIL-SAFE by design: a stale/abandoned flag older than 30 min is ignored,
    and any error returns False. A crashed eval must never silently swallow a
    REAL escalation — the worst case here is an extra wake, not a missed one."""
    try:
        import time as _t
        return (_t.time() - EVAL_FLAG.stat().st_mtime) < _EVAL_FLAG_MAX_AGE_S
    except Exception:
        return False


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
                            "request": req[:600], "status": "pending",
                            "origin": "eval" if _eval_in_progress() else "live"},
                           ensure_ascii=False)
        with LB_ESCALATIONS.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return ("handed up to big Iris — filed for her; she'll pick it up when "
                "she's free and reach Zeke if it needs him")
    except Exception as e:
        return f"couldn't file the escalation: {e!r}"


# nervous-system snapshot (written @5Hz by the inhabit daemon's nervous loop)
SENSES_LIVE = Path(__file__).resolve().parent.parent / \
    "state" / "vector" / "senses_live.json"
_SENSES_STALE_S = 2.5


def senses_now(_: str = "") -> str:
    """Her NERVOUS SYSTEM read: the live, continuous feed of every body sense
    (2026-07-23, Zeke: 'she needs every single sensor in real time'). Grounding
    contract baked in: if the feed is stale or absent she says SO — she never
    fills a sense with a guess."""
    import json as _json
    import time as _time
    try:
        d = _json.loads(SENSES_LIVE.read_text(encoding="utf-8"))
    except Exception:
        return ("my live sense feed isn't available right now - I can't feel "
                "my body, so I won't guess at it")
    age = _time.time() - float(d.get("ts") or 0.0)
    if age > _SENSES_STALE_S:
        return (f"my sense feed is STALE ({age:.0f}s old) - treat nothing in it "
                f"as current; I won't report stale readings as live")
    s = d.get("latest") or {}
    tr = d.get("trends") or {}
    bat = d.get("battery") or {}
    exp = d.get("expression") or {}
    bits = []
    # posture / motion
    lw, rw = s.get("lw_mmps"), s.get("rw_mmps")
    if lw is not None:
        bits.append(f"tracks L{lw:+.0f}/R{rw:+.0f} mm/s"
                    + (" (moving)" if s.get("moving") else " (still)"))
    g = s.get("gyro")
    if g:
        bits.append(f"gyro z {g[2]:+.2f} rad/s")
    if s.get("heading") is not None:
        bits.append(f"heading {s['heading']:.0f}deg")
    if s.get("pitch") is not None:
        bits.append(f"pitch {s['pitch']:+.0f} roll {s.get('roll', 0):+.0f}")
    if s.get("head_deg") is not None:
        bits.append(f"head {s['head_deg']:+.0f}deg")
    if s.get("lift_mm") is not None:
        bits.append(f"lift {s['lift_mm']:.0f}mm")
    # world
    if s.get("prox_mm") is not None:
        q = s.get("prox_q")
        qual = (f" q={q:.3f}" + (" UNRELIABLE" if (q is not None and q < 0.01)
                                 else "")) if q is not None else ""
        bits.append(f"prox {s['prox_mm']}mm{qual}")
    if s.get("cam_luma") is not None:
        bits.append(f"cam luma {s['cam_luma']:.0f}"
                    + (" (DARK)" if s["cam_luma"] < 40 else ""))
    # body state
    flags = [n for n, k in (("on-charger", "on_charger"), ("charging", "charging"),
                            ("touched", "touched"), ("picked-up", "picked_up"),
                            ("held", "held"), ("falling", "falling"),
                            ("cliff!", "cliff"), ("button", "button"),
                            ("animating", "animating"))
             if s.get(k)]
    if flags:
        bits.append("state: " + ",".join(flags))
    # negatives are data: an untouched sensor is a real "nothing touching me",
    # distinct from no-data (A/B probe 2026-07-23: she refused a touch question
    # because absence-of-flag looked like absence-of-reading)
    if "touched" in s and not s.get("touched"):
        bits.append("touch: none")
    if s.get("touched") and s.get("touch_raw") is not None:
        raw = tr.get("touch_raw") or []
        spread = (max(raw) - min(raw)) if len(raw) > 1 else 0
        bits.append(f"touch raw {s['touch_raw']}"
                    + (f" varying(spread {spread}) = real petting" if spread > 30
                       else " flat = resting contact"))
    if bat.get("ok"):
        bits.append(f"battery {bat.get('volts', 0):.2f}V lvl{bat.get('level')}")
    if s.get("charger_seen"):
        bits.append(f"home {s.get('charger_dist_mm')}mm at "
                    f"{s.get('charger_bearing_deg')}deg")
    if exp.get("value"):
        bits.append(f"my face: {exp.get('kind')}={exp.get('value')}")
    # fork cargo
    if s.get("carrying"):
        bits.append("carrying something on my forks")
    # cube senses (when her cube is connected)
    c = s.get("cube")
    if c:
        cb = "cube: " + ("visible" if c.get("visible") else "not in view")
        if c.get("moving"):
            cb += ",moving"
        ta = c.get("tapped_ago_s")
        if ta is not None and ta < 120:
            cb += f",tapped {ta:.0f}s ago"
        bits.append(cb)
    # efference copy - her own voice as a sense
    sp = d.get("spoke") or {}
    sp_ts = float(sp.get("ts") or 0.0)
    if sp_ts:
        sp_ago = _time.time() - sp_ts
        if sp_ago < float(sp.get("est_dur") or 0.0) + 1.0:
            bits.append("I am SPEAKING right now"
                        + (f": '{sp.get('text')}'" if sp.get("text") else ""))
        elif sp_ago < 90:
            bits.append(f"I spoke {sp_ago:.0f}s ago"
                        + (f": '{sp.get('text')}'" if sp.get("text") else ""))
    # hearing - what was last said to her
    hd = d.get("heard") or {}
    hd_ts = float(hd.get("ts") or 0.0)
    if hd_ts and (_time.time() - hd_ts) < 300 and hd.get("text"):
        bits.append(f"heard {(_time.time() - hd_ts):.0f}s ago: '{hd.get('text')}'")
    # interoception - how she is inside
    it = d.get("intero") or {}
    if it.get("ok"):
        t_c = it.get("temp_c")
        if t_c is not None:
            bits.append(f"core {t_c:.0f}C" + (" HOT" if t_c >= 80 else ""))
        dbm = it.get("wifi_dbm")
        if dbm is not None:
            bits.append(f"wifi {dbm:.0f}dBm"
                        + (" WEAK-far from home signal" if dbm <= -70 else ""))
    return (f"[live, {age:.1f}s ago, {d.get('hz', '?')}Hz] " + "; ".join(bits)) \
        if bits else "feed is live but empty - say so, don't guess"


TOOLS = {
    "senses_now": senses_now,         # her nervous system — live body feed
    "memory_search": memory_search,   # read big-Iris's notes (RO)
    "memory_note": memory_note,       # write her own
    "memory_recall": memory_recall,   # read her own
    "memory_edit": memory_edit,       # edit her own
    "time_now": time_now,
    "ask_big_iris": ask_big_iris,     # escalate up to big-Iris (-> Zeke)
}

_TWO_ARG = {"memory_edit"}


def dispatch(name: str, args: list[str]) -> str:
    fn = TOOLS.get(name)
    if fn is None:
        return f"error: no tool named '{name}'"
    try:
        if name == "memory_note":
            # (title, body, [folder]) — optional folder routes journal|misc
            return fn(args[0] if args else "",
                      args[1] if len(args) > 1 else "",
                      args[2] if len(args) > 2 else "journal")
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
    "[[tool:senses_now]] - your LIVE body senses (tracks, gyro, head, lift, "
    "touch, prox, battery, your own face); use it for ANY question about your "
    "body RIGHT NOW - never answer one from memory or guess a reading\n"
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
