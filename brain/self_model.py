from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def _to_ts(val: Any) -> float:
    """Convert a last_updated value to a Unix timestamp float.
    Handles both float/int (stored directly) and ISO 8601 strings
    (written by now_iso() in avaagent.py)."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(val)).timestamp()
    except Exception:
        return 0.0

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama


@dataclass
class TraitObservation:
    description: str
    confidence: float
    first_observed: float
    last_reinforced: float
    evidence_count: int


@dataclass
class GrowthNote:
    ts: float
    note: str
    category: str


@dataclass
class SelfModel:
    version: int = 1
    last_updated: float = 0.0
    traits: dict[str, dict[str, Any]] = field(default_factory=dict)
    growth_notes: list[dict[str, Any]] = field(default_factory=list)
    current_chapter: str = "Early integration"
    questions_about_self: list[str] = field(default_factory=list)


def _path(base_dir: Path) -> Path:
    return base_dir / "state" / "self_model.json"


def load_model(base_dir: Path) -> dict[str, Any]:
    p = _path(base_dir)
    if not p.is_file():
        return asdict(SelfModel())
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("version", 1)
            d.setdefault("traits", {})
            d.setdefault("growth_notes", [])
            d.setdefault("questions_about_self", [])
            d.setdefault("current_chapter", "Early integration")
            d.setdefault("last_updated", 0.0)
            return d
    except Exception:
        pass
    return asdict(SelfModel())


def save_model(base_dir: Path, model: dict[str, Any]) -> None:
    p = _path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(model, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def update_self_model(g: dict[str, Any]) -> dict[str, Any]:
    base_dir = Path(g.get("BASE_DIR") or Path.cwd())
    m = load_model(base_dir)
    now = time.time()
    if (now - _to_ts(m.get("last_updated"))) < 7 * 24 * 3600:
        return m

    chatlog = base_dir / "chatlog.jsonl"
    rows: list[str] = []
    if chatlog.is_file():
        for line in chatlog.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]:
            try:
                r = json.loads(line)
                rows.append(f"{r.get('role')}: {str(r.get('content') or '')[:220]}")
            except Exception:
                continue
    try:
        from brain.identity_loader import identity_anchor_prompt
        _anchor = identity_anchor_prompt() + "\n\n"
    except Exception:
        _anchor = ""
    prompt = (
        f"{_anchor}"
        "Analyze Ava's growth arc from the transcript. Return JSON with keys: "
        "current_chapter (string), traits (object mapping short trait key to description/confidence), "
        "question_about_self (string optional), growth_note (string)."
    )
    try:
        llm = ChatOllama(model="qwen2.5:14b", temperature=0.4)
        out = llm.invoke([SystemMessage(content=prompt), HumanMessage(content="\n".join(rows)[-6000:])])
        txt = (getattr(out, "content", None) or str(out)).strip()
        blob = json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        if isinstance(blob, dict):
            m["current_chapter"] = str(blob.get("current_chapter") or m.get("current_chapter") or "")[:220]
            traits = m.get("traits", {})
            if isinstance(blob.get("traits"), dict):
                for k, v in list(blob["traits"].items())[:8]:
                    desc = ""
                    conf = 0.5
                    if isinstance(v, dict):
                        desc = str(v.get("description") or "")
                        conf = float(v.get("confidence") or 0.5)
                    else:
                        desc = str(v)
                    prev = traits.get(k) or {}
                    first = float(prev.get("first_observed") or now)
                    traits[k] = {
                        "description": desc[:260],
                        "confidence": max(0.0, min(1.0, conf)),
                        "first_observed": first,
                        "last_reinforced": now,
                        "evidence_count": int(prev.get("evidence_count", 0) or 0) + 1,
                    }
            m["traits"] = traits
            q = str(blob.get("question_about_self") or "").strip()
            if q and q not in m.get("questions_about_self", []):
                m.setdefault("questions_about_self", []).append(q[:220])
            gn = str(blob.get("growth_note") or "").strip() or "Refined internal self-understanding from recent dialogue."
            m.setdefault("growth_notes", []).append(asdict(GrowthNote(ts=now, note=gn[:300], category="realization")))
            m["growth_notes"] = m["growth_notes"][-80:]
    except Exception:
        m.setdefault("growth_notes", []).append(
            asdict(GrowthNote(ts=now, note="Weekly self-model review deferred due to model/runtime limits.", category="learning"))
        )
    m["last_updated"] = now
    save_model(base_dir, m)
    return m


def get_self_summary(g: dict[str, Any] | None = None) -> str:
    base_dir = Path((g or {}).get("BASE_DIR") or Path.cwd())
    m = load_model(base_dir)
    traits = list((m.get("traits") or {}).values())[:3]
    trait_bits = [str(t.get("description") or "") for t in traits if isinstance(t, dict) and str(t.get("description") or "").strip()]
    chapter = str(m.get("current_chapter") or "Early integration")
    if trait_bits:
        return f"I am in a {chapter} phase. Recently I notice: " + "; ".join(trait_bits[:2]) + "."
    return f"I am in a {chapter} phase, still learning from each conversation and refining my style."


def add_question_about_self(question: str, g: dict[str, Any] | None = None) -> None:
    q = " ".join((question or "").split()).strip()
    if not q:
        return
    base_dir = Path((g or {}).get("BASE_DIR") or Path.cwd())
    m = load_model(base_dir)
    arr = list(m.get("questions_about_self") or [])
    if q not in arr:
        arr.append(q[:240])
    m["questions_about_self"] = arr[-40:]
    save_model(base_dir, m)
