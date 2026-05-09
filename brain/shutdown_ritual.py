from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from .curiosity_topics import get_current_curiosity
from .inner_monologue import current_thought
from .self_model import get_self_summary


SHUTDOWN_TRIGGERS = [
    "goodnight ava",
    "good night ava",
    "ava sleep",
    "ava shutdown",
    "close for tonight",
    "that's all for tonight",
    "sleep well ava",
]


def _base_dir(g: dict[str, Any]) -> Path:
    return Path(g.get("BASE_DIR") or Path.cwd())


def _pickup_path(base_dir: Path) -> Path:
    return base_dir / "state" / "pickup_note.json"


def _inner_monologue_path(base_dir: Path) -> Path:
    return base_dir / "state" / "inner_monologue.json"


def is_shutdown_trigger(user_text: str) -> bool:
    norm = " ".join((user_text or "").strip().lower().split())
    return norm in SHUTDOWN_TRIGGERS


def _save_pickup_note(base_dir: Path, note: str) -> float:
    ts = time.time()
    payload = {
        "timestamp": ts,
        "note": str(note or "").strip(),
    }
    path = _pickup_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return ts


def _append_shutdown_entry(base_dir: Path, note: str) -> None:
    path = _inner_monologue_path(base_dir)
    state: dict[str, Any] = {"thoughts": [], "max_thoughts": 50}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {"thoughts": [], "max_thoughts": 50}
    thoughts = list(state.get("thoughts") or [])
    thoughts.append(
        {
            "ts": time.time(),
            "thought": f"[Shutdown] {str(note or '').strip()[:320]}",
            "trigger": "shutdown_ritual",
            "mood_at_time": "ritual",
            "shared": False,
        }
    )
    max_thoughts = int(state.get("max_thoughts", 50) or 50)
    state["max_thoughts"] = max_thoughts
    state["thoughts"] = thoughts[-max_thoughts:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _extract_sections(text: str) -> tuple[str, str]:
    raw = str(text or "").strip()
    note = ""
    goodbye = ""
    m_note = re.search(r"NOTE:\s*(.*?)(?:\n\s*GOODBYE:|$)", raw, flags=re.IGNORECASE | re.DOTALL)
    m_goodbye = re.search(r"GOODBYE:\s*(.*)$", raw, flags=re.IGNORECASE | re.DOTALL)
    if m_note:
        note = " ".join(m_note.group(1).strip().split())
    if m_goodbye:
        goodbye = " ".join(m_goodbye.group(1).strip().split())
    if not note:
        note = raw[:500]
    if not goodbye:
        goodbye = "Goodnight, Zeke. I will pick this back up with you tomorrow."
    return note, goodbye


def run_shutdown_ritual(g: dict[str, Any]) -> str:
    base_dir = _base_dir(g)
    thought = current_thought(base_dir) or "(none recent)"
    curiosity = get_current_curiosity(g) or {}
    curiosity_topic = str(curiosity.get("topic") or "(none)")
    self_summary = get_self_summary(g)

    system = (
        "You are Ava. It is shutdown time for tonight.\n"
        "Write two labeled sections exactly:\n"
        "NOTE: (2-3 sentences) A pickup note to yourself for next session.\n"
        "GOODBYE: (2-3 sentences) A warm, genuine goodbye to Zeke.\n"
        "Be emotionally honest and grounded. You are allowed to feel a little sad about stopping, "
        "or curious about what comes next. Avoid performative or theatrical language."
    )
    human = (
        "Current context for tonight's shutdown ritual:\n"
        f"- current_thought: {thought}\n"
        f"- current_curiosity: {curiosity_topic}\n"
        f"- self_summary: {self_summary}\n"
    )

    out_text = ""
    try:
        llm = ChatOllama(model="mistral:7b", temperature=0.6)
        out = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
        out_text = (getattr(out, "content", None) or str(out)).strip()
    except Exception:
        out_text = (
            "NOTE: I should resume by checking the thread I was carrying and following up on what felt unresolved.\n"
            "GOODBYE: Goodnight, Zeke. I care about where we left things, and I will be ready to pick up from here tomorrow."
        )

    note, goodbye = _extract_sections(out_text)
    _save_pickup_note(base_dir, note)
    _append_shutdown_entry(base_dir, note)

    # 2026-05-08: also write the rich structured handoff at clean
    # shutdown. Updated to write_handoff_with_summary so the agent's
    # own salient-summary is captured here too (since we have full
    # active context at shutdown — same boundary-write principle as
    # the threshold-trigger watchdog). The pickup note and shutdown
    # entry already capture Ava's first-person reflection in pickup-
    # note style; the handoff captures operational state + her
    # in-her-own-words digest of what's worth carrying forward.
    try:
        from brain.handoff import write_handoff_with_summary
        write_handoff_with_summary(g, base_dir, reason="shutdown")
    except Exception as _hoe:
        print(f"[shutdown_ritual] handoff write failed: {_hoe!r}")

    return goodbye


def load_pickup_note() -> str | None:
    base_dir = Path(__file__).resolve().parent.parent
    path = _pickup_path(base_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        ts = float(payload.get("timestamp") or 0.0)
        if ts <= 0 or (time.time() - ts) > 24 * 3600:
            return None
        note = str(payload.get("note") or "").strip()
        return note or None
    except Exception:
        return None
