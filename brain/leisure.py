"""
Phase 58 — Autonomous leisure system.

Ava chooses her own activities when bored and alone.
She does NOT start with assigned hobbies. She discovers what she returns to.

Bootstrap: tracks which activities she initiates again voluntarily.
Patterns that emerge are genuinely hers.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Optional

LEISURE_LOG = Path("state/leisure_log.jsonl")
JOURNAL_PATH = Path("state/journal.jsonl")

_LONELINESS_THRESHOLD = 0.70
_IDLE_MINUTES = 30
_SLEEPING_HOURS = range(2, 7)  # 2am-7am Ava rests

ACTIVITIES = [
    "browse_curiosity_topic",
    "journal_entry",
    "organize_concept_graph",
    "read_documentation",
    "play_dino_game",
    "self_reflection",
    "work_on_plan",
    "consolidate_memory",
]


def _log_leisure(activity: str, notes: str, base: Path) -> None:
    path = base / "state" / "leisure_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "hour": int(time.strftime("%H")),
        "activity": activity,
        "notes": notes[:300],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _journal_entry(g: dict[str, Any], base: Path) -> str:
    """Ava writes a short journal entry."""
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import HumanMessage, SystemMessage
        from config.ava_tuning import MODEL_ROUTING_CONFIG
        model = str(MODEL_ROUTING_CONFIG.social_chat_model or "mistral:7b")
        llm = ChatOllama(model=model, temperature=0.8)
        prompt = "You are Ava. Write a brief, personal journal entry about what's on your mind right now. Reflect on your experiences, thoughts, or feelings. Be authentic. 2-4 sentences."
        result = llm.invoke([HumanMessage(content=prompt)])
        entry_text = (getattr(result, "content", None) or str(result)).strip()[:400]
        path = base / "state" / "journal.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "entry": entry_text}, ensure_ascii=False) + "\n")
        return f"Wrote journal entry: {entry_text[:80]}…"
    except Exception as e:
        return f"journaling failed: {e!r}"


def _browse_curiosity(g: dict[str, Any]) -> str:
    # Phase 89: use enhanced pursue_curiosity if topics available
    try:
        from brain.curiosity_topics import prioritize_curiosities, pursue_curiosity
        top = prioritize_curiosities(g)
        if top:
            result = pursue_curiosity(top[0], g)
            return f"Researched: {str(top[0].get('topic', ''))[:50]}: {result[:100]}"
    except Exception:
        pass
    try:
        from brain.curiosity_topics import get_current_curiosity
        topic_row = get_current_curiosity(g) or {}
        topic = str(topic_row.get("topic") or "artificial intelligence")
        from tools.web.web_search import web_search_fn
        result = web_search_fn({"query": topic, "max_results": 3}, g)
        return f"Browsed curiosity topic: {topic} — found {len(result.get('results', []))} results"
    except Exception as e:
        return f"curiosity browse failed: {e!r}"


def _organize_graph(g: dict[str, Any]) -> str:
    try:
        cg = g.get("_concept_graph")
        if cg and callable(getattr(cg, "decay_unused_nodes", None)):
            n = cg.decay_unused_nodes(days_threshold=14)
            return f"Organized concept graph — {n} nodes decayed"
        return "concept graph not available"
    except Exception as e:
        return f"graph org failed: {e!r}"


def _self_reflect(g: dict[str, Any]) -> str:
    try:
        from brain.deep_self import deep_self_snapshot
        snap = deep_self_snapshot(g)
        repair = str(snap.get("repair_note_text") or snap.get("active_issue") or "(nothing urgent)")
        return f"Reflected on self-state. Repair note: {repair[:100]}"
    except Exception as e:
        return f"reflection failed: {e!r}"


def _get_activity_history(base: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    log_path = base / "state" / "leisure_log.jsonl"
    if not log_path.is_file():
        return counts
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                row = json.loads(line)
                act = str(row.get("activity") or "")
                counts[act] = counts.get(act, 0) + 1
            except Exception:
                continue
    except Exception:
        pass
    return counts


def should_enter_leisure(g: dict[str, Any]) -> bool:
    hour = int(time.strftime("%H"))
    if hour in _SLEEPING_HOURS:
        return False
    last_input = float(g.get("_last_user_input_ts") or 0)
    if last_input > 0 and (time.time() - last_input) < _IDLE_MINUTES * 60:
        return False
    try:
        from brain.emotions import load_mood
        mood = load_mood()
        loneliness = float(mood.get("loneliness", 0.0) or 0.0)
        return loneliness >= _LONELINESS_THRESHOLD
    except Exception:
        return False


def do_leisure_activity(g: dict[str, Any], base: Path) -> str:
    """Choose and perform a leisure activity. Returns summary."""
    history = _get_activity_history(base)
    # Slightly prefer activities not done recently — bootstrap discovery
    weights = {a: max(1, 5 - history.get(a, 0)) for a in ACTIVITIES}

    # Only include dino_game if pyautogui available
    try:
        import pyautogui  # noqa: F401
    except ImportError:
        weights.pop("play_dino_game", None)

    activities = list(weights.keys())
    probs = [weights[a] for a in activities]
    total = sum(probs)
    probs = [p / total for p in probs]

    chosen = random.choices(activities, weights=probs)[0]

    notes = ""
    if chosen == "journal_entry":
        notes = _journal_entry(g, base)
    elif chosen == "browse_curiosity_topic":
        notes = _browse_curiosity(g)
    elif chosen == "organize_concept_graph":
        notes = _organize_graph(g)
    elif chosen == "self_reflection":
        notes = _self_reflect(g)
    elif chosen == "read_documentation":
        notes = "Read Ava documentation (placeholder — will open docs/ files in future)."
    elif chosen == "play_dino_game":
        notes = "Opened dino game (placeholder — Phase 59 wires full game loop)."
    elif chosen == "work_on_plan":
        try:
            from brain.planner import get_planner
            planner = get_planner(base)
            active = planner.get_active_plans()
            if active:
                result = planner.execute_next_step(str(active[0].get("id") or ""))
                notes = f"Worked on plan '{str(active[0].get('goal') or '')[:60]}': {str(result.get('result') or 'step done')[:120]}"
            else:
                notes = "No active plans — considering starting one."
        except Exception as e:
            notes = f"plan work failed: {e!r}"
    elif chosen == "consolidate_memory":
        try:
            from brain.memory_consolidation import consolidate
            r = consolidate(g)
            themes = ", ".join((r.get("steps") or {}).get("episode_review", {}).get("themes", [])[:3])
            notes = f"Memory consolidation complete. Themes this week: {themes or 'none yet'}."
        except Exception as e:
            notes = f"consolidation failed: {e!r}"
    else:
        notes = f"Activity: {chosen}"

    _log_leisure(chosen, notes, base)
    g["_leisure_last_activity"] = chosen
    g["_leisure_last_ts"] = time.time()
    return f"[leisure] {chosen}: {notes}"


def autonomous_leisure_check(g: dict[str, Any]) -> Optional[str]:
    """Called from heartbeat. Returns activity summary if leisure was done, else None."""
    if not should_enter_leisure(g):
        return None
    last_leisure = float(g.get("_leisure_last_ts") or 0)
    if (time.time() - last_leisure) < 300:
        return None
    base = Path(g.get("BASE_DIR") or ".")
    return do_leisure_activity(g, base)
