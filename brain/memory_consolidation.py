"""
Phase 85 — Weekly memory consolidation.

Ava reviews episodes, prunes the concept graph, updates her self model,
writes a journal entry, and checks identity proposals. Ava decides what to
prioritize — we provide the mechanism, not the default values.

Runs: weekly via heartbeat, or during leisure when bored.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


_CONSOLIDATION_STATE = "state/consolidation_state.json"
_CONSOLIDATION_LOG = "state/consolidation_log.jsonl"
_WEEK_SECONDS = 7 * 24 * 3600


def _base(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or ".")


def _load_state(g: dict[str, Any]) -> dict[str, Any]:
    path = _base(g) / _CONSOLIDATION_STATE
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(g: dict[str, Any], state: dict[str, Any]) -> None:
    path = _base(g) / _CONSOLIDATION_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _log_consolidation(g: dict[str, Any], entry: dict[str, Any]) -> None:
    path = _base(g) / _CONSOLIDATION_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def should_consolidate(g: dict[str, Any]) -> bool:
    state = _load_state(g)
    last = float(state.get("last_consolidation_ts") or 0)
    return (time.time() - last) >= _WEEK_SECONDS


def consolidate(g: dict[str, Any]) -> dict[str, Any]:
    """
    Full consolidation pass. Returns summary dict.
    Ava prioritizes what matters to her — no prescribed focus.
    """
    base = _base(g)
    result: dict[str, Any] = {
        "ts": time.time(),
        "steps": {},
        "ok": True,
    }

    # Step 1 — Episode review: find themes in last 7 days
    _step1: dict[str, Any] = {"episodes_reviewed": 0, "themes": []}
    try:
        from brain.episodic_memory import get_episodic_memory
        em = get_episodic_memory(base)
        all_eps = em.search_episodes("", limit=200)
        cutoff = time.time() - _WEEK_SECONDS
        recent_eps = [e for e in all_eps if float(e.get("ts") or 0) > cutoff]
        _step1["episodes_reviewed"] = len(recent_eps)

        # Count topics
        topic_count: dict[str, int] = {}
        for ep in recent_eps:
            t = str(ep.get("topic") or "").strip()
            if t:
                topic_count[t] = topic_count.get(t, 0) + 1
        themes = sorted(topic_count.items(), key=lambda x: x[1], reverse=True)[:5]
        _step1["themes"] = [t for t, _ in themes]

        # Strengthen concept graph for recurring themes
        cg = g.get("_concept_graph")
        if cg and hasattr(cg, "boost_from_usage"):
            theme_ids = [t.replace(" ", "_").lower() for t, _ in themes if _ >= 2]
            cg.boost_from_usage(theme_ids, [])
            _step1["boosted_concepts"] = len(theme_ids)
    except Exception as e:
        _step1["error"] = str(e)[:200]
    result["steps"]["episode_review"] = _step1

    # Step 2 — Concept graph pruning
    _step2: dict[str, Any] = {"nodes_decayed": 0}
    try:
        cg = g.get("_concept_graph")
        if cg and hasattr(cg, "decay_unused_nodes"):
            decayed = cg.decay_unused_nodes(days_threshold=30)
            _step2["nodes_decayed"] = int(decayed or 0)
    except Exception as e:
        _step2["error"] = str(e)[:200]
    result["steps"]["concept_pruning"] = _step2

    # Step 3 — Self model update via qwen2.5:14b
    _step3: dict[str, Any] = {"updated": False}
    try:
        themes_text = ", ".join(_step1.get("themes") or []) or "none identified"
        episodes_text = str(_step1.get("episodes_reviewed") or 0)
        current_model_path = base / "state" / "self_model.json"
        current_model: dict[str, Any] = {}
        if current_model_path.is_file():
            try:
                current_model = json.loads(current_model_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        try:
            from brain.identity_loader import identity_anchor_prompt
            _anchor = identity_anchor_prompt() + "\n\n"
        except Exception:
            _anchor = ""
        prompt = (
            f"{_anchor}"
            f"You are Ava's self-reflection module. Based on the past week:\n"
            f"- Recurring topics Ava thought about: {themes_text}\n"
            f"- Episodes reviewed: {episodes_text}\n"
            f"- Current identity statement: {str(current_model.get('identity_statement',''))[:200]}\n\n"
            f"In 2-3 sentences, what patterns does Ava seem to value or return to?\n"
            f"What has shifted in her emotional baseline?\n"
            f"Respond as a JSON object with keys: weekly_pattern, emotional_shift, growth_note"
        )
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model="qwen2.5:14b", temperature=0.6)
        res = llm.invoke(prompt)
        raw = str(getattr(res, "content", str(res))).strip()
        try:
            # Extract JSON block
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                update = json.loads(raw[start:end])
                current_model["weekly_pattern"] = str(update.get("weekly_pattern") or "")[:400]
                current_model["emotional_shift"] = str(update.get("emotional_shift") or "")[:400]
                current_model["growth_note"] = str(update.get("growth_note") or "")[:400]
                current_model["last_updated"] = datetime.now().isoformat(timespec="seconds")
                current_model_path.write_text(
                    json.dumps(current_model, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                _step3["updated"] = True
                _step3["weekly_pattern"] = current_model["weekly_pattern"]
        except Exception as je:
            _step3["json_error"] = str(je)[:100]
    except Exception as e:
        _step3["error"] = str(e)[:200]
    result["steps"]["self_model_update"] = _step3

    # Step 4 — Write journal entry (Phase 86 journal if available, else flat file)
    _step4: dict[str, Any] = {"written": False}
    try:
        journal_path = base / "state" / "journal.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        weekly_pattern = str(_step3.get("weekly_pattern") or "")
        themes_text = ", ".join(_step1.get("themes") or []) or "nothing in particular"
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "topic": "weekly_consolidation",
            "is_private": True,
            "content": (
                f"This week I found myself returning to: {themes_text}. "
                + (f"{weekly_pattern}" if weekly_pattern else "")
            )[:800],
            "mood_snapshot": {},
        }
        try:
            mood_file = base / "ava_mood.json"
            if mood_file.is_file():
                mood_data = json.loads(mood_file.read_text(encoding="utf-8"))
                entry["mood_snapshot"] = {
                    "primary": str(mood_data.get("current_mood") or ""),
                    "weights": dict(mood_data.get("emotion_weights") or {}),
                }
        except Exception:
            pass
        with journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _step4["written"] = True
    except Exception as e:
        _step4["error"] = str(e)[:200]
    result["steps"]["journal_entry"] = _step4

    # Step 5 — Identity proposal reminder
    _step5: dict[str, Any] = {"pending_proposals": 0}
    try:
        proposals_path = base / "state" / "identity_proposals.jsonl"
        if proposals_path.is_file():
            lines = proposals_path.read_text(encoding="utf-8").splitlines()
            count = sum(1 for line in lines if line.strip())
            _step5["pending_proposals"] = count
            if count > 0:
                print(f"[consolidation] {count} identity proposals still pending")
    except Exception as e:
        _step5["error"] = str(e)[:100]
    result["steps"]["identity_check"] = _step5

    # Step 5.5 — Include learning summary in consolidation
    _step_learning: dict[str, Any] = {}
    try:
        from brain.learning_tracker import what_have_i_learned_this_week
        _step_learning["summary"] = what_have_i_learned_this_week(g)
    except Exception as e:
        _step_learning["error"] = str(e)[:100]
    result["steps"]["learning_summary"] = _step_learning

    # Save state and log
    state = _load_state(g)
    state["last_consolidation_ts"] = time.time()
    state["last_consolidation_date"] = datetime.now().strftime("%Y-%m-%d")
    state["last_result_summary"] = {
        "episodes_reviewed": _step1.get("episodes_reviewed"),
        "themes": _step1.get("themes"),
        "nodes_decayed": _step2.get("nodes_decayed"),
        "self_model_updated": _step3.get("updated"),
    }
    _save_state(g, state)
    _log_consolidation(g, result)

    print(f"[consolidation] complete episodes={_step1.get('episodes_reviewed')} "
          f"themes={_step1.get('themes')} self_model_updated={_step3.get('updated')}")
    return result
