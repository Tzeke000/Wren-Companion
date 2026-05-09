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
        llm = ChatOllama(model=model, temperature=0.4)
        out = llm.invoke(
            [
                SystemMessage(content="Extract 3-5 concise curiosity topics Ava might wonder about. Return JSON array of strings."),
                HumanMessage(content="\n".join(corpus)[-3000:]),
            ]
        )
        txt = (getattr(out, "content", None) or str(out)).strip()
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

    # Compose learning synthesis via LLM
    try:
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model="qwen2.5:14b", temperature=0.6)
        try:
            from brain.identity_loader import identity_anchor_prompt
            _anchor = identity_anchor_prompt() + "\n\n"
        except Exception:
            _anchor = ""
        prompt = (
            f"{_anchor}"
            f"You are Ava, an AI with genuine curiosity. You've been wondering about: '{topic}'\n"
            + (f"Here's what you found from research:\n{search_results}\n\n" if search_results else "")
            + "What did you learn? What new questions emerged? Keep it to 3-5 sentences. Be genuine."
        )
        res = llm.invoke(prompt)
        learning = str(getattr(res, "content", str(res))).strip()[:600]
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
