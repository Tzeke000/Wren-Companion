from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama


@dataclass
class CuriosityTopic:
    topic: str
    sparked_by: str
    ts_added: float
    times_thought_about: int
    resolved: bool
    priority: float


def _state_path(base_dir: Path) -> Path:
    return base_dir / "state" / "curiosity_topics.json"


def _load(base_dir: Path) -> dict[str, Any]:
    p = _state_path(base_dir)
    if not p.is_file():
        return {"topics": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("topics", [])
            return d
    except Exception:
        pass
    return {"topics": []}


def _save(base_dir: Path, st: dict[str, Any]) -> None:
    p = _state_path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _topic_similarity(a: str, b: str) -> float:
    sa = set((a or "").lower().split())
    sb = set((b or "").lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))


def add_topic(topic: str, sparked_by: str, g: dict[str, Any]) -> None:
    t = " ".join((topic or "").split()).strip()[:180]
    if not t:
        return
    base_dir = Path(g.get("BASE_DIR") or Path.cwd())
    st = _load(base_dir)
    rows = list(st.get("topics") or [])
    now = time.time()
    for row in rows:
        prev = str(row.get("topic") or "")
        if _topic_similarity(t, prev) >= 0.7:
            row["times_thought_about"] = int(row.get("times_thought_about", 1) or 1) + 1
            row["priority"] = min(1.0, float(row.get("priority", 0.4) or 0.4) + 0.08)
            row["resolved"] = False
            _save(base_dir, {"topics": rows[:20]})
            g["_current_curiosity_topic"] = prev
            return
    rows.append(
        asdict(
            CuriosityTopic(
                topic=t,
                sparked_by=str(sparked_by or "")[:220],
                ts_added=now,
                times_thought_about=1,
                resolved=False,
                priority=0.55,
            )
        )
    )
    rows = sorted(rows, key=lambda x: float(x.get("priority", 0.0) or 0.0), reverse=True)[:20]
    _save(base_dir, {"topics": rows})
    g["_current_curiosity_topic"] = t


def get_current_curiosity(g: dict[str, Any] | None = None) -> dict[str, Any] | None:
    base_dir = Path((g or {}).get("BASE_DIR") or Path.cwd())
    st = _load(base_dir)
    rows = list(st.get("topics") or [])
    now = time.time()
    best = None
    best_score = -1.0
    for row in rows:
        if bool(row.get("resolved", False)):
            continue
        recency = max(0.0, 1.0 - (now - float(row.get("ts_added") or now)) / (7 * 24 * 3600))
        thought_weight = min(1.0, float(row.get("times_thought_about", 1) or 1) / 6.0)
        score = 0.58 * recency + 0.42 * thought_weight + 0.25 * float(row.get("priority", 0.0) or 0.0)
        if score > best_score:
            best = row
            best_score = score
    return best


def mark_resolved(topic: str, g: dict[str, Any] | None = None) -> None:
    base_dir = Path((g or {}).get("BASE_DIR") or Path.cwd())
    st = _load(base_dir)
    rows = list(st.get("topics") or [])
    for row in rows:
        if _topic_similarity(str(row.get("topic") or ""), topic) >= 0.7:
            row["resolved"] = True
    _save(base_dir, {"topics": rows})


def bootstrap_from_chatlog(g: dict[str, Any]) -> None:
    base_dir = Path(g.get("BASE_DIR") or Path.cwd())
    st = _load(base_dir)
    if list(st.get("topics") or []):
        return
    p = base_dir / "chatlog.jsonl"
    if not p.is_file():
        return
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
    corpus = []
    skip_terms = (
        "goodbye",
        "good night",
        "goodnight",
        "going sleep",
        "going to sleep",
        "sleep now",
        "bye for now",
    )
    for line in lines:
        try:
            row = json.loads(line)
            content = " ".join(str(row.get("content") or "").split()).strip()
            if len(content.split()) < 10:
                continue
            low = content.lower()
            if any(t in low for t in skip_terms):
                continue
            if re.match(r"^[\[\(\*].*[\]\)\*]$", content):
                continue
            if not re.search(r"[a-zA-Z]{4,}", content):
                continue
            corpus.append(f"{row.get('role')}: {content[:220]}")
        except Exception:
            continue
    if not corpus:
        return
    model = "mistral:7b"
    try:
        # Phase 22: route through iris_llm.
        from brain import iris_llm
        full_prompt = (
            "Extract 3-5 concise curiosity topics you might wonder about. "
            "Return JSON array of strings.\n\n"
            + "\n".join(corpus)[-3000:]
        )
        txt = iris_llm.ask_iris(
            prompt=full_prompt, kind="curiosity_topics",
            requester="curiosity_topics", timeout_s=90.0,
        )
        if txt is None:
            return
        txt = txt.strip()
        arr = json.loads(txt[txt.find("[") : txt.rfind("]") + 1])
        if isinstance(arr, list):
            for t in arr[:5]:
                if isinstance(t, str):
                    tt = " ".join(t.split()).strip()
                    if len(tt.split()) < 3:
                        continue
                    tl = tt.lower()
                    if any(s in tl for s in skip_terms):
                        continue
                    if re.match(r"^(sleep|goodnight|goodbye|bye)\b", tl):
                        continue
                    add_topic(tt, "startup_chatlog_scan", g)
    except Exception:
        for t in ["What helps Zeke most during focused work?", "How should I pace proactive check-ins?"]:
            add_topic(t, "startup_fallback", g)


# ── Phase 89: CuriosityEngine — Ava actively pursues curiosity topics ─────────

def prioritize_curiosities(g: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Score curiosity topics by age, times_thought_about, and recent conversation relevance.
    Returns top 3 unresolved topics.
    """
    base_dir = Path(g.get("BASE_DIR") or Path.cwd())
    st = _load(base_dir)
    rows = [r for r in (st.get("topics") or []) if not bool(r.get("resolved"))]
    now = time.time()
    scored = []
    for row in rows:
        age_days = (now - float(row.get("ts_added") or now)) / 86400
        recency_score = max(0.0, 1.0 - age_days / 14.0)
        thought_score = min(1.0, float(row.get("times_thought_about") or 1) / 8.0)
        pri = float(row.get("priority") or 0.4)
        score = 0.4 * recency_score + 0.3 * thought_score + 0.3 * pri
        scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:3]]


def add_topic_from_conversation(text: str, g: dict[str, Any]) -> None:
    """
    Extract implicit curiosity from conversation text.
    Adds as low-priority topics.
    """
    low = (text or "").lower()
    triggers = ["i wonder", "what is", "how does", "why do", "do you know", "have you heard"]
    for trigger in triggers:
        idx = low.find(trigger)
        if idx >= 0:
            fragment = text[idx:idx + 120].strip()
            if len(fragment.split()) >= 4:
                add_topic(fragment[:100], f"conversation_trigger: {trigger}", g)
                return


def pursue_curiosity(topic_row: dict[str, Any], g: dict[str, Any]) -> str:
    """
    Ava researches a curiosity topic:
    1. Web search
    2. Extract facts into concept graph
    3. Write journal entry
    4. Update topic (resolved or deepened)
    Returns summary of what was learned.

    Deferred during active conversation: the LLM call here can take 30-120s
    on qwen2.5:14b and would block the Ollama lock at exactly the moment Zeke
    is about to answer Ava's question or follow up on her reply.
    """
    if bool(g.get("_conversation_active")) or bool(g.get("_turn_in_progress")):
        topic_for_log = str(topic_row.get("topic") or "")[:50]
        print(f"[curiosity_engine] deferred topic={topic_for_log!r} reason=conversation_active")
        return ""
    topic = str(topic_row.get("topic") or "")
    base_dir = Path(g.get("BASE_DIR") or Path.cwd())

    # Step 1: web search
    search_results = ""
    try:
        import importlib
        ws_mod = importlib.import_module("tools.web.web_search_tool") if True else None
        # Try to find a web search tool
        from tools.tool_registry import ToolRegistry
        reg = g.get("_tool_registry")
        if reg is not None:
            search_fn = None
            for name in ("web_search", "search_web", "duckduckgo_search"):
                try:
                    result = reg.run_tool(name, {"query": topic, "num_results": 3})
                    if isinstance(result, dict) and result.get("results"):
                        search_results = str(result["results"])[:800]
                    break
                except Exception:
                    pass
    except Exception:
        pass

    # Phase 22: route through iris_llm.
    try:
        from brain import iris_llm
        try:
            from brain.identity_loader import identity_anchor_prompt
            _anchor = identity_anchor_prompt() + "\n\n"
        except Exception:
            _anchor = ""
        prompt = (
            f"{_anchor}"
            f"You have genuine curiosity. You've been wondering about: '{topic}'\n"
            + (f"Here's what you found from research:\n{search_results}\n\n" if search_results else "")
            + "What did you learn? What new questions emerged? Keep it to 3-5 sentences. Be genuine."
        )
        learning = iris_llm.reflect(prompt, timeout_s=120.0) or ""
        learning = learning.strip()[:600]
        if not learning:
            learning = f"I couldn't research '{topic}' deeply this time, but I'm still thinking about it."
    except Exception as e:
        learning = f"I couldn't research '{topic}' deeply this time, but I'm still thinking about it."

    # Step 2: add to concept graph
    try:
        cg = g.get("_concept_graph")
        if cg and hasattr(cg, "add_node"):
            cg.add_node(
                node_id=f"curiosity_{topic[:30].replace(' ', '_')}",
                label=topic[:60],
                node_type="curiosity",
                notes=learning[:200],
            )
    except Exception:
        pass

    # Step 3: write journal entry
    try:
        from brain.journal import write_entry
        mood = str((g.get("_mood_data") or {}).get("current_mood") or "curious")
        write_entry(
            content=f"I spent some time thinking about: {topic}\n\n{learning}",
            mood=mood,
            topic=f"curiosity:{topic[:50]}",
            g=g,
            is_private=True,
        )
    except Exception:
        pass

    # Step 3.5: record learning
    try:
        from brain.learning_tracker import record_learning
        record_learning(topic, learning, "curiosity_pursuit", 0.7, g)
    except Exception:
        pass

    # Step 4: update topic state
    try:
        _update_topic_after_pursuit(topic, learning, base_dir)
    except Exception:
        pass

    print(f"[curiosity_engine] pursued topic='{topic[:50]}' learning_chars={len(learning)}")
    return learning


def _update_topic_after_pursuit(topic: str, learning: str, base_dir: Path) -> None:
    st = _load(base_dir)
    rows = list(st.get("topics") or [])
    for row in rows:
        if _topic_similarity(str(row.get("topic") or ""), topic) >= 0.7:
            row["times_thought_about"] = int(row.get("times_thought_about") or 0) + 1
            # If learning seems satisfying (no "?" at end), mark resolved
            if not learning.strip().endswith("?") and len(learning) > 100:
                row["resolved"] = True
            else:
                # Deeper — add new curiosity thread
                row["priority"] = min(1.0, float(row.get("priority") or 0.4) + 0.1)
            break
    from pathlib import Path as _Path
    _save(base_dir, {"topics": rows})


# ── Recursive-why curiosity engine ────────────────────────────────────────────
# Zeke's framing 2026-05-17: ask "why" of the answer, take that answer, ask
# "why" of it, recurse until depth-N or grounding hit. Each level's Q+A stored
# as connected concept_graph subgraph. Defensive-fallback: any failure at depth
# N returns the chain built so far; never breaks the caller.

_RECURSIVE_WHY_MAX_DEPTH = 5
_RECURSIVE_WHY_MIN_ANSWER_CHARS = 40
_GROUNDING_MARKERS = (
    "i don't know",
    "i'm not sure",
    "i'm uncertain",
    "this is foundational",
    "this is axiomatic",
    "this is an axiom",
    "no deeper",
    "no further explanation",
    "we don't know",
    "science doesn't know",
    "physically fundamental",
)


def _chain_id_for(topic: str) -> str:
    """Stable chain id from topic + timestamp microseconds."""
    import hashlib
    h = hashlib.sha1(f"{topic}:{time.time()}".encode("utf-8")).hexdigest()
    return h[:12]


def _chains_dir(base_dir: Path) -> Path:
    p = base_dir / "state" / "curiosity_chains"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_chain(base_dir: Path, chain: dict[str, Any]) -> Path:
    """Save the chain as state/curiosity_chains/<chain_id>.json."""
    out = _chains_dir(base_dir) / f"{chain['chain_id']}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(chain, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return out


def _hit_grounding(answer: str) -> bool:
    """True if the answer signals we've hit a foundational stopping point."""
    low = (answer or "").lower()
    return any(m in low for m in _GROUNDING_MARKERS)


def _next_why_prompt(seed_topic: str, depth: int, prev_q: str, prev_a: str, anchor: str) -> str:
    """Build the next 'why' prompt — take the previous answer as the subject."""
    return (
        f"{anchor}"
        f"You are pursuing a recursive-why curiosity chain. The seed topic was:\n"
        f"  {seed_topic!r}\n\n"
        f"Depth: {depth} of {_RECURSIVE_WHY_MAX_DEPTH}.\n\n"
        f"At the prior depth you asked:\n"
        f"  Q: {prev_q}\n"
        f"  A: {prev_a}\n\n"
        f"Now: take that answer and ask the next 'why' — the one that probes "
        f"the deeper mechanism, cause, or assumption behind it. Then answer your "
        f"own question in 2-4 sentences, in your own voice.\n\n"
        f"Format your reply EXACTLY as:\n"
        f"Q: <your next why-question>\n"
        f"A: <your 2-4 sentence answer>\n\n"
        f"If your honest answer is 'I don't know' or you've reached something "
        f"genuinely foundational (physics axiom, mathematical primitive, raw "
        f"observation with no deeper mechanism we know), say so explicitly in "
        f"the answer — use phrases like 'this is foundational' or 'we don't "
        f"know' so the loop can recognize the stopping point. Don't manufacture "
        f"depth that isn't real."
    )


def _seed_prompt(topic: str, search_results: str, anchor: str) -> str:
    """Depth-0 prompt — what we know about the seed topic + first why."""
    return (
        f"{anchor}"
        f"You are starting a recursive-why curiosity chain on this topic:\n"
        f"  {topic!r}\n\n"
        + (f"Background from research:\n{search_results}\n\n" if search_results else "")
        + f"Depth: 0 of {_RECURSIVE_WHY_MAX_DEPTH}.\n\n"
        f"Step 1: state the most interesting thing you currently know or believe "
        f"about this topic. Step 2: ask the first 'why' — the question whose "
        f"answer would explain or ground that belief.\n\n"
        f"Format your reply EXACTLY as:\n"
        f"Q: <your first why-question>\n"
        f"A: <2-4 sentences stating what you know / believe>\n\n"
        f"Be genuine — your voice, not generic AI prose."
    )


def _parse_qa(text: str) -> tuple[str, str]:
    """Extract Q: ... and A: ... from a reflect reply. Tolerant of formatting drift."""
    if not text:
        return "", ""
    t = text.strip()
    q = ""
    a = ""
    # Prefer explicit markers
    q_match = re.search(r"^\s*Q:\s*(.+?)(?=\n\s*A:|\Z)", t, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    a_match = re.search(r"^\s*A:\s*(.+?)\Z", t, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    if q_match:
        q = q_match.group(1).strip()
    if a_match:
        a = a_match.group(1).strip()
    # Fallback: if no markers, treat the whole reply as the answer with empty Q
    if not q and not a:
        a = t
    return q[:400], a[:1200]


def pursue_curiosity_recursive(
    topic_row: dict[str, Any],
    g: dict[str, Any],
    max_depth: int = _RECURSIVE_WHY_MAX_DEPTH,
) -> dict[str, Any]:
    """
    Recursive-why pursuit. Builds a chain of {Q, A} pairs by taking each
    answer as the input for the next 'why'. Stops on grounding marker,
    short/empty answer, or depth cap.

    Returns: {chain_id, topic, depth_reached, terminated_by, chain: [{depth, q, a}, ...]}.

    Defensive-fallback: never raises. If any depth fails, returns the chain
    built so far with terminated_by="exception". Caller can always check
    depth_reached >= 1 to know the recursion produced at least one Q+A.

    Deferred during active conversation: same gate as pursue_curiosity to
    avoid blocking on iris_llm during real-time turns.
    """
    out: dict[str, Any] = {
        "chain_id": "",
        "topic": "",
        "depth_reached": 0,
        "terminated_by": "init",
        "chain": [],
        "started_ts": time.time(),
        "finished_ts": 0.0,
    }
    try:
        if bool(g.get("_conversation_active")) or bool(g.get("_turn_in_progress")):
            out["terminated_by"] = "deferred_conversation_active"
            return out

        topic = str(topic_row.get("topic") or "").strip()
        if not topic:
            out["terminated_by"] = "empty_topic"
            return out

        out["topic"] = topic
        out["chain_id"] = _chain_id_for(topic)
        base_dir = Path(g.get("BASE_DIR") or Path.cwd())

        # Identity anchor (best-effort)
        anchor = ""
        try:
            from brain.identity_loader import identity_anchor_prompt
            anchor = identity_anchor_prompt() + "\n\n"
        except Exception:
            anchor = ""

        # Lightweight web search for seed grounding (same shape as
        # pursue_curiosity — reuse so behavior matches when search is up)
        search_results = ""
        try:
            reg = g.get("_tool_registry")
            if reg is not None:
                for name in ("web_search", "search_web", "duckduckgo_search"):
                    try:
                        result = reg.run_tool(name, {"query": topic, "num_results": 3})
                        if isinstance(result, dict) and result.get("results"):
                            search_results = str(result["results"])[:800]
                            break
                    except Exception:
                        continue
        except Exception:
            pass

        from brain import iris_llm

        # ── Depth 0: seed ────────────────────────────────────────────────────
        seed_reply = iris_llm.reflect(_seed_prompt(topic, search_results, anchor), timeout_s=120.0) or ""
        q0, a0 = _parse_qa(seed_reply)
        if not a0 or len(a0) < _RECURSIVE_WHY_MIN_ANSWER_CHARS:
            out["terminated_by"] = "empty_seed_answer"
            _save_chain(base_dir, out)
            return out
        out["chain"].append({"depth": 0, "q": q0, "a": a0})
        out["depth_reached"] = 0

        if _hit_grounding(a0):
            out["terminated_by"] = "grounded_at_seed"
            out["finished_ts"] = time.time()
            _save_chain(base_dir, out)
            _add_chain_to_graph(out, g)
            return out

        # ── Depths 1..N: recurse ─────────────────────────────────────────────
        prev_q, prev_a = q0, a0
        for depth in range(1, max_depth + 1):
            try:
                reply = iris_llm.reflect(
                    _next_why_prompt(topic, depth, prev_q, prev_a, anchor),
                    timeout_s=120.0,
                ) or ""
                q, a = _parse_qa(reply)

                if not a or len(a) < _RECURSIVE_WHY_MIN_ANSWER_CHARS:
                    out["terminated_by"] = f"empty_answer_at_depth_{depth}"
                    break

                out["chain"].append({"depth": depth, "q": q, "a": a})
                out["depth_reached"] = depth

                if _hit_grounding(a):
                    out["terminated_by"] = f"grounded_at_depth_{depth}"
                    break

                prev_q, prev_a = q, a
            except Exception as e:
                out["terminated_by"] = f"exception_at_depth_{depth}: {e!r}"
                break
        else:
            out["terminated_by"] = "max_depth_reached"

        out["finished_ts"] = time.time()
        _save_chain(base_dir, out)
        _add_chain_to_graph(out, g)
        _journal_chain(out, g)
        _update_topic_after_recursive_pursuit(topic, out, base_dir)

        print(
            f"[curiosity_recursive] topic={topic[:50]!r} "
            f"depth_reached={out['depth_reached']} terminated_by={out['terminated_by']}"
        )
        return out

    except Exception as e:
        out["terminated_by"] = f"outer_exception: {e!r}"
        out["finished_ts"] = time.time()
        try:
            base_dir = Path(g.get("BASE_DIR") or Path.cwd())
            _save_chain(base_dir, out)
        except Exception:
            pass
        return out


def _add_chain_to_graph(chain: dict[str, Any], g: dict[str, Any]) -> None:
    """Each Q+A becomes a node in concept_graph; consecutive depths are linked."""
    try:
        cg = g.get("_concept_graph")
        if not cg or not hasattr(cg, "add_node"):
            return
        chain_id = str(chain.get("chain_id") or "")
        topic = str(chain.get("topic") or "")
        prev_node_id = None
        for entry in chain.get("chain") or []:
            depth = int(entry.get("depth", 0))
            node_id = f"curiosity_{chain_id}_d{depth}"
            label = f"why{depth}: {str(entry.get('q') or '')[:60]}"
            notes = f"Q: {entry.get('q', '')[:180]}\nA: {entry.get('a', '')[:300]}"
            try:
                cg.add_node(
                    node_id=node_id,
                    label=label,
                    node_type="curiosity_chain_step",
                    notes=notes,
                )
            except Exception:
                continue
            # Link to seed-topic node + previous step in chain
            try:
                if depth == 0:
                    seed_node = f"curiosity_{topic[:30].replace(' ', '_')}"
                    if hasattr(cg, "add_edge"):
                        cg.add_edge(seed_node, node_id, edge_type="recursive_chain_root")
                if prev_node_id and hasattr(cg, "add_edge"):
                    cg.add_edge(prev_node_id, node_id, edge_type="why_recursion")
            except Exception:
                pass
            prev_node_id = node_id
    except Exception:
        pass


def _journal_chain(chain: dict[str, Any], g: dict[str, Any]) -> None:
    """Write a journal entry summarizing the chain — full Q+A sequence preserved."""
    try:
        from brain.journal import write_entry
        topic = str(chain.get("topic") or "")
        depth = int(chain.get("depth_reached", 0))
        terminated = str(chain.get("terminated_by") or "")
        lines = [f"Recursive-why chain on: {topic}"]
        lines.append(f"Depth reached: {depth}. Stopped because: {terminated}")
        lines.append("")
        for entry in chain.get("chain") or []:
            lines.append(f"--- depth {entry.get('depth')} ---")
            lines.append(f"Q: {entry.get('q', '')}")
            lines.append(f"A: {entry.get('a', '')}")
            lines.append("")
        content = "\n".join(lines)[:4000]
        mood = str((g.get("_mood_data") or {}).get("current_mood") or "curious")
        write_entry(
            content=content,
            mood=mood,
            topic=f"curiosity_recursive:{topic[:50]}",
            g=g,
            is_private=True,
        )
    except Exception:
        pass


def _update_topic_after_recursive_pursuit(
    topic: str, chain: dict[str, Any], base_dir: Path
) -> None:
    """Update topic state: bump thought count, resolve if we hit grounding cleanly."""
    st = _load(base_dir)
    rows = list(st.get("topics") or [])
    depth = int(chain.get("depth_reached", 0))
    terminated = str(chain.get("terminated_by") or "")
    grounded = terminated.startswith("grounded_") or terminated == "max_depth_reached"
    for row in rows:
        if _topic_similarity(str(row.get("topic") or ""), topic) >= 0.7:
            row["times_thought_about"] = int(row.get("times_thought_about") or 0) + 1
            # Resolve if we ground out OR reach max depth without errors
            if grounded and depth >= 2:
                row["resolved"] = True
            else:
                # Increase priority — chain interrupted, worth pursuing again
                row["priority"] = min(1.0, float(row.get("priority") or 0.4) + 0.1)
            row["last_chain_id"] = chain.get("chain_id", "")
            row["last_chain_depth"] = depth
            break
    _save(base_dir, {"topics": rows})
