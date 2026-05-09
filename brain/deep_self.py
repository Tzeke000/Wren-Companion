from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama


@dataclass
class ZekeMindModel:
    inferred_current_mood: str = "uncertain"
    inferred_energy_level: str = "medium"
    inferred_focus: str = "unknown"
    likely_wants_from_ava: str = "helpful, honest support"
    recent_patterns: list[dict[str, Any]] | list[str] = None  # type: ignore[assignment]
    confidence: float = 0.5
    last_updated: float = 0.0


def _path(base_dir: Path, name: str) -> Path:
    return base_dir / "state" / name


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def update_mind_model(user_text: str, context: str, g: dict[str, Any]) -> None:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    out_path = _path(base, "zeke_mind_model.json")
    old = _load_json(out_path, asdict(ZekeMindModel(recent_patterns=[])))
    if not isinstance(old, dict):
        old = asdict(ZekeMindModel(recent_patterns=[]))
    prompt = (
        "Based on this message and context, what is Zeke likely feeling, wanting, and thinking right now? "
        "Reply as JSON: {mood, energy, focus, wants_from_ava, confidence}"
    )
    mood, energy, focus, wants, conf = "uncertain", "medium", "unknown", "helpful support", 0.5
    try:
        llm = ChatOllama(model="mistral:7b", temperature=0.25)
        out = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=f"MESSAGE:\n{user_text[:1600]}\n\nCONTEXT:\n{context[:1600]}")])
        txt = (getattr(out, "content", None) or str(out)).strip()
        blob = json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        if isinstance(blob, dict):
            mood = str(blob.get("mood") or mood)[:120]
            energy = str(blob.get("energy") or energy)[:32].lower()
            if energy not in {"high", "medium", "low"}:
                energy = "medium"
            focus = str(blob.get("focus") or focus)[:220]
            wants = str(blob.get("wants_from_ava") or wants)[:220]
            conf = max(0.0, min(1.0, float(blob.get("confidence") or conf)))
    except Exception:
        pass
    patterns = list(old.get("recent_patterns") or [])
    patterns.append({"ts": time.time(), "mood": mood, "focus": focus, "wants": wants})
    payload = {
        "inferred_current_mood": mood,
        "inferred_energy_level": energy,
        "inferred_focus": focus,
        "likely_wants_from_ava": wants,
        "recent_patterns": patterns[-80:],
        "confidence": conf,
        "last_updated": time.time(),
    }
    _save_json(out_path, payload)


def update_mind_model_async(user_text: str, context: str, g: dict[str, Any]) -> None:
    threading.Thread(target=update_mind_model, args=(user_text, context, g), daemon=True, name="ava-mind-model").start()


def get_mind_model_summary(g: dict[str, Any]) -> str:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    model = _load_json(_path(base, "zeke_mind_model.json"), asdict(ZekeMindModel(recent_patterns=[])))
    if not isinstance(model, dict):
        model = {}
    mood = str(model.get("inferred_current_mood") or "uncertain")
    energy = str(model.get("inferred_energy_level") or "medium")
    wants = str(model.get("likely_wants_from_ava") or "clear support")
    return f"Zeke seems {mood}, {energy} energy, probably wants {wants} right now."


def resolve_value_conflict(situation: str, g: dict[str, Any]) -> dict[str, Any]:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    path = _path(base, "value_conflicts.json")
    rows = _load_json(path, [])
    if not isinstance(rows, list):
        rows = []
    prompt = (
        "You are Ava balancing values: honesty, kindness, long-term care, privacy. "
        "Given the situation, return JSON with keys competing_values(list), priority, integrated_response, tension_explained."
    )
    out_blob = {
        "competing_values": ["honesty", "kindness"],
        "priority": "balanced honesty with care",
        "integrated_response": "Be truthful while being gentle and specific.",
        "tension_explained": "Direct truth can sting, but hiding it is worse long-term.",
    }
    try:
        llm = ChatOllama(model="mistral:7b", temperature=0.3)
        out = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=situation[:1500])])
        txt = (getattr(out, "content", None) or str(out)).strip()
        blob = json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        if isinstance(blob, dict):
            out_blob.update(blob)
    except Exception:
        pass
    row = {"ts": time.time(), "situation": situation[:500], **out_blob}
    rows.append(row)
    _save_json(path, rows[-200:])
    return row


def self_critique(reply: str, user_text: str, context: str, g: dict[str, Any]) -> None:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    path = _path(base, "self_critique.json")
    data = _load_json(path, {"entries": [], "averages": {}})
    if not isinstance(data, dict):
        data = {"entries": [], "averages": {}}
    prompt = (
        "Rate this AI response on: helpfulness (0-1), emotional_attunement (0-1), honesty (0-1), conciseness (0-1). "
        "Reply as JSON only."
    )
    scores = {"helpfulness": 0.7, "emotional_attunement": 0.7, "honesty": 0.8, "conciseness": 0.6}
    try:
        llm = ChatOllama(model="mistral:7b", temperature=0.2)
        out = llm.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=f"USER:\n{user_text[:1200]}\n\nREPLY:\n{reply[:1200]}\n\nCONTEXT:\n{context[:800]}"),
            ]
        )
        txt = (getattr(out, "content", None) or str(out)).strip()
        blob = json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        if isinstance(blob, dict):
            for k in list(scores.keys()):
                if k in blob:
                    scores[k] = max(0.0, min(1.0, float(blob.get(k) or scores[k])))
    except Exception:
        pass
    entry = {"ts": time.time(), "scores": scores, "user_text": user_text[:220], "reply_preview": reply[:220]}
    rows = list(data.get("entries") or [])
    rows.append(entry)
    rows = rows[-300:]
    avgs = {}
    for key in ("helpfulness", "emotional_attunement", "honesty", "conciseness"):
        vals = [float((r.get("scores") or {}).get(key) or 0.0) for r in rows[-60:]]
        avgs[key] = (sum(vals) / len(vals)) if vals else 0.0
    data["entries"] = rows
    data["averages"] = avgs
    data["last_updated"] = time.time()
    low_dims = [k for k, v in avgs.items() if float(v) < 0.6]
    if low_dims:
        data["learning_focus"] = f"improve: {', '.join(low_dims)}"
    _save_json(path, data)


def self_critique_async(reply: str, user_text: str, context: str, g: dict[str, Any]) -> None:
    threading.Thread(target=self_critique, args=(reply, user_text, context, g), daemon=True, name="ava-self-critique").start()


def track_confidence(statement: str, was_correct: bool, g: dict[str, Any]) -> None:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    path = _path(base, "confidence_calibration.json")
    data = _load_json(path, {"claims": [], "summary": {}})
    if not isinstance(data, dict):
        data = {"claims": [], "summary": {}}
    claims = list(data.get("claims") or [])
    claims.append({"ts": time.time(), "statement": statement[:240], "was_correct": bool(was_correct)})
    claims = claims[-500:]
    total = len(claims)
    correct = sum(1 for c in claims if bool(c.get("was_correct")))
    data["claims"] = claims
    data["summary"] = {"total": total, "correct": correct, "accuracy": (correct / total) if total else 0.0}
    _save_json(path, data)


def check_repair_needed(g: dict[str, Any]) -> str | None:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    critique = _load_json(_path(base, "self_critique.json"), {"entries": []})
    entries = list((critique or {}).get("entries") or [])[-10:] if isinstance(critique, dict) else []
    lows = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        scores = e.get("scores") if isinstance(e.get("scores"), dict) else {}
        if any(float(scores.get(k) or 0.0) < 0.5 for k in ("helpfulness", "emotional_attunement", "honesty", "conciseness")):
            lows.append(e)
    if not lows:
        return None
    last = lows[-1]
    topic = str(last.get("user_text") or "that topic").strip()[:100]
    note = f"Last time we talked about {topic} I think I could have handled it better — I wanted to revisit that."
    q_path = _path(base, "repair_queue.json")
    queue = _load_json(q_path, {"pending": [], "last_check_at": 0.0})
    if not isinstance(queue, dict):
        queue = {"pending": [], "last_check_at": 0.0}
    pending = list(queue.get("pending") or [])
    pending.append({"ts": time.time(), "note": note, "source": "self_critique"})
    queue["pending"] = pending[-30:]
    queue["last_check_at"] = time.time()
    _save_json(q_path, queue)
    return note


def pop_pending_repair(g: dict[str, Any]) -> str:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    q_path = _path(base, "repair_queue.json")
    queue = _load_json(q_path, {"pending": [], "last_check_at": 0.0})
    if not isinstance(queue, dict):
        return ""
    pending = list(queue.get("pending") or [])
    if not pending:
        return ""
    row = pending.pop(0)
    queue["pending"] = pending
    _save_json(q_path, queue)
    return str((row or {}).get("note") or "")


def propose_identity_addition(text: str, g: dict[str, Any]) -> dict[str, Any]:
    """
    Phase 68: Ava proposes adding something to her identity.
    Stored in state/identity_proposals.jsonl for Zeke to review.
    Approved proposals are appended to state/identity_extensions.md.
    """
    base = Path(g.get("BASE_DIR") or Path.cwd())
    proposal = {
        "ts": time.time(),
        "text": str(text or "")[:500],
        "status": "pending",  # pending | approved | rejected
    }
    q_path = base / "state" / "identity_proposals.jsonl"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    with q_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(proposal, ensure_ascii=False) + "\n")
    return {"ok": True, "proposal": proposal["text"][:100], "status": "pending_review"}


def approve_identity_addition(proposal_text: str, g: dict[str, Any]) -> dict[str, Any]:
    """Apply an approved identity proposal to identity_extensions.md."""
    base = Path(g.get("BASE_DIR") or Path.cwd())
    ext_path = base / "state" / "identity_extensions.md"
    ext_path.parent.mkdir(parents=True, exist_ok=True)
    with ext_path.open("a", encoding="utf-8") as f:
        f.write(f"\n<!-- approved {time.strftime('%Y-%m-%d')} -->\n{proposal_text}\n")
    return {"ok": True, "appended": proposal_text[:100]}


def load_identity_extensions(g: dict[str, Any]) -> str:
    """Returns identity_extensions.md content to inject into prompts."""
    base = Path(g.get("BASE_DIR") or Path.cwd())
    ext_path = base / "state" / "identity_extensions.md"
    if not ext_path.is_file():
        return ""
    try:
        return ext_path.read_text(encoding="utf-8").strip()[:2000]
    except Exception:
        return ""


def deep_self_snapshot(g: dict[str, Any]) -> dict[str, Any]:
    base = Path(g.get("BASE_DIR") or Path.cwd())
    mind = _load_json(_path(base, "zeke_mind_model.json"), {})
    critique = _load_json(_path(base, "self_critique.json"), {"averages": {}})
    repairs = _load_json(_path(base, "repair_queue.json"), {"pending": []})
    conflicts = _load_json(_path(base, "value_conflicts.json"), [])
    avgs = (critique.get("averages") if isinstance(critique, dict) else {}) or {}
    avg_val = 0.0
    vals = [float(avgs.get(k) or 0.0) for k in ("helpfulness", "emotional_attunement", "honesty", "conciseness")]
    if vals:
        avg_val = sum(vals) / len(vals)
    return {
        "zeke_inferred_mood": str((mind or {}).get("inferred_current_mood") or "uncertain"),
        "zeke_energy": str((mind or {}).get("inferred_energy_level") or "medium"),
        "last_self_critique_avg": round(avg_val, 3),
        "pending_repairs": len(list((repairs or {}).get("pending") or [])) if isinstance(repairs, dict) else 0,
        "value_conflicts_logged": len(conflicts) if isinstance(conflicts, list) else 0,
    }
