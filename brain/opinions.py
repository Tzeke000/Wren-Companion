from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama


@dataclass
class Opinion:
    topic: str
    stance: str
    confidence: float
    reasoning: str
    formed_from: str
    ts_formed: float
    times_expressed: int


def _path(base_dir: Path) -> Path:
    return base_dir / "state" / "opinions.json"


def _load(base_dir: Path) -> dict[str, Any]:
    p = _path(base_dir)
    if not p.is_file():
        return {"opinions": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("opinions", [])
            return d
    except Exception:
        pass
    return {"opinions": []}


def _save(base_dir: Path, st: dict[str, Any]) -> None:
    p = _path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _topic_freq(base_dir: Path, topic: str) -> int:
    p = base_dir / "chatlog.jsonl"
    if not p.is_file():
        return 0
    t = (topic or "").lower().strip()
    c = 0
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
        try:
            row = json.loads(line)
            content = str(row.get("content") or "").lower()
            if t and t in content:
                c += 1
        except Exception:
            continue
    return c


def form_opinion(topic: str, context: str, g: dict[str, Any]) -> dict[str, Any] | None:
    base_dir = Path(g.get("BASE_DIR") or Path.cwd())
    if _topic_freq(base_dir, topic) < 3:
        return None
    st = _load(base_dir)
    existing = list(st.get("opinions") or [])
    for row in existing:
        if str(row.get("topic") or "").lower() == topic.lower():
            return row
    prompt = (
        "Form Ava's concise opinion. Return JSON with keys stance, confidence(0-1), reasoning. "
        "Base it on Ava values: grounded, kind, direct, thoughtful."
    )
    try:
        llm = ChatOllama(model="mistral:7b", temperature=0.5)
        out = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=f"Topic: {topic}\nContext: {context[:1200]}")])
        txt = (getattr(out, "content", None) or str(out)).strip()
        blob = json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        op = asdict(
            Opinion(
                topic=topic[:120],
                stance=str(blob.get("stance") or "")[:260],
                confidence=max(0.0, min(1.0, float(blob.get("confidence") or 0.55))),
                reasoning=str(blob.get("reasoning") or "")[:420],
                formed_from=context[:240],
                ts_formed=time.time(),
                times_expressed=0,
            )
        )
    except Exception:
        op = asdict(
            Opinion(
                topic=topic[:120],
                stance="I think balance and clarity usually lead to better outcomes.",
                confidence=0.56,
                reasoning="This is a provisional stance derived from repeated conversation context.",
                formed_from=context[:240],
                ts_formed=time.time(),
                times_expressed=0,
            )
        )
    existing.append(op)
    st["opinions"] = existing[-80:]
    _save(base_dir, st)
    return op


def get_opinion(topic: str, g: dict[str, Any] | None = None) -> dict[str, Any] | None:
    base_dir = Path((g or {}).get("BASE_DIR") or Path.cwd())
    st = _load(base_dir)
    q = (topic or "").lower()
    best = None
    best_score = 0.0
    for row in st.get("opinions", []):
        t = str(row.get("topic") or "").lower()
        if not t:
            continue
        score = 1.0 if q == t else (0.75 if q in t or t in q else 0.0)
        if score > best_score:
            best = row
            best_score = score
    if not best:
        return None
    if float(best.get("confidence") or 0.0) <= 0.5:
        return None
    best["times_expressed"] = int(best.get("times_expressed", 0) or 0) + 1
    _save(base_dir, st)
    return best


def list_top_opinions(g: dict[str, Any] | None = None, limit: int = 3) -> list[dict[str, Any]]:
    base_dir = Path((g or {}).get("BASE_DIR") or Path.cwd())
    st = _load(base_dir)
    rows = list(st.get("opinions") or [])
    rows.sort(key=lambda r: float(r.get("confidence") or 0.0), reverse=True)
    return rows[:limit]


def opinion_count(g: dict[str, Any] | None = None) -> int:
    base_dir = Path((g or {}).get("BASE_DIR") or Path.cwd())
    return len(list(_load(base_dir).get("opinions") or []))
