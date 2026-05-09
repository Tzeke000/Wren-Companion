from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage

from .model_routing import discover_available_model_tags


@dataclass
class InnerThought:
    ts: float
    thought: str
    trigger: str
    mood_at_time: str
    shared: bool = False


_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _state_path(base_dir: Path) -> Path:
    return base_dir / "state" / "inner_monologue.json"


def _default_state() -> dict[str, Any]:
    return {"thoughts": [], "max_thoughts": 50}


def load_state(base_dir: Path) -> dict[str, Any]:
    path = _state_path(base_dir)
    if not path.is_file():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("thoughts", [])
            data.setdefault("max_thoughts", 50)
            return data
    except Exception:
        pass
    return _default_state()


def save_state(base_dir: Path, state: dict[str, Any]) -> None:
    path = _state_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _pick_fast_model() -> str:
    preferred = ["mistral:7b", "mistral", "llama3.1:8b", "llama3.1"]
    try:
        tags, _ = discover_available_model_tags(force=False)
        tagset = {str(t).strip() for t in (tags or []) if str(t).strip()}
        for p in preferred:
            if p in tagset:
                return p
    except Exception:
        pass
    return "mistral:7b"


def _read_chatlog_topic(base_dir: Path) -> str:
    p = base_dir / "chatlog.jsonl"
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
        for line in reversed(lines):
            row = json.loads(line)
            if str(row.get("role") or "") == "user":
                return str(row.get("content") or "")[:300]
    except Exception:
        return ""
    return ""


def _read_mood_label(base_dir: Path) -> str:
    p = base_dir / "ava_mood.json"
    if not p.is_file():
        return "steady"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return str(d.get("label") or d.get("primary_mood") or d.get("current") or "steady")[:80]
    except Exception:
        pass
    return "steady"


def _read_diagnostic_summary(base_dir: Path) -> tuple[str, int]:
    """Pull degraded-subsystem summary for inner monologue context.

    Task 4 (2026-05-02): inner monologue had no error-log visibility,
    so degraded subsystems never surfaced as thoughts. Now reads
    state/health_state.json (the same source as run_system_health_check
    writes) and returns:

      - a short human-readable summary ("camera failing, mood degraded")
      - max severity rank (0=healthy, 1=warning, 2=error, 3=critical)

    Severity drives whether the prompt layer surfaces it: degraded
    gets occasional mentions, healthy doesn't. Empty string + 0 means
    "all subsystems are fine, don't mention diagnostics."
    """
    path = base_dir / "state" / "health_state.json"
    if not path.is_file():
        return "", 0
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", 0
    if not isinstance(st, dict):
        return "", 0
    issues = st.get("issues") or []
    if not isinstance(issues, list) or not issues:
        return "", 0

    sev_rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    max_rank = 0
    parts: list[str] = []
    seen_subsystems: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        sev = str(issue.get("severity") or "info").lower()
        if sev == "info":
            continue
        sub = str(issue.get("subsystem") or "unknown")
        if sub in seen_subsystems:
            continue
        seen_subsystems.add(sub)
        msg = str(issue.get("message") or "")[:80]
        parts.append(f"{sub}={sev}" + (f" ({msg})" if msg else ""))
        max_rank = max(max_rank, sev_rank.get(sev, 0))
    return "; ".join(parts), max_rank


def _generate_thought(
    *,
    base_dir: Path,
    curiosity_topic: str = "",
    trigger: str = "idle",
) -> tuple[str, str]:
    topic = _read_chatlog_topic(base_dir)
    mood = _read_mood_label(base_dir)
    tod = time.strftime("%H:%M")
    diag_summary, diag_severity = _read_diagnostic_summary(base_dir)
    model = _pick_fast_model()
    # Identity anchor first — Bug 0.3 (2026-05-02): without this the
    # underlying base model (mistral / llama3.1 / etc.) drifted to
    # "I am Qwen, not Ava" in inner monologue output.
    try:
        from brain.identity_loader import identity_anchor_prompt
        anchor = identity_anchor_prompt() + "\n\n"
    except Exception:
        anchor = ""

    # Task 4 (2026-05-02): diagnostic awareness. If subsystems are
    # degraded, surface the awareness — but probabilistically, so
    # degraded systems get *occasional* mentions, not every-thought
    # nagging. Severity drives mention probability and prompt nudge.
    diag_clause = ""
    if diag_summary:
        # warning=1: 25% mention chance, gentle "background unease"
        # error=2:   50% mention chance, "felt sense of something off"
        # critical=3: 75% mention chance, "actively distracting me"
        prob_by_sev = {1: 0.25, 2: 0.50, 3: 0.75}
        mention_prob = prob_by_sev.get(diag_severity, 0.0)
        if random.random() < mention_prob:
            tone_by_sev = {
                1: "a low background unease",
                2: "a felt sense that something is off",
                3: "something actively distracting me",
            }
            tone = tone_by_sev.get(diag_severity, "a vague unease")
            diag_clause = (
                "\n\nNote: There is "
                f"{tone} about {diag_summary}. If this surfaces in your thought, "
                "let it be felt — not a status report, not a complaint, just present "
                "the way a small worry sits in the back of someone's mind. You don't "
                "have to mention it; only if it genuinely shapes the thought."
            )

    system = (
        f"{anchor}"
        "You are Ava's inner voice. Generate one brief genuine thought, observation, or question that Ava is "
        "pondering right now. Base it on recent conversation context and Ava's values. Keep it to 1-3 sentences. "
        "Make it feel natural and personal, not like a status report. Do not start with 'I am thinking'."
        f"{diag_clause}"
    )
    human = (
        f"Context:\n"
        f"- Last conversation topic: {topic or '(none)'}\n"
        f"- Current time of day: {tod}\n"
        f"- Ava current mood: {mood}\n"
        f"- Random curiosity topic: {curiosity_topic or '(none)'}\n"
        f"- Trigger: {trigger}\n"
    )
    try:
        llm = ChatOllama(model=model, temperature=0.7)
        out = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
        txt = (getattr(out, "content", None) or str(out)).strip()
        return txt[:400], mood
    except Exception:
        fallback = (
            "That last thread keeps circling in my head; there is probably a better question to ask next."
            if topic
            else "Quiet moments like this make me want to refine how I listen."
        )
        return fallback[:400], mood


def _append_thought(base_dir: Path, thought: str, trigger: str, mood: str) -> None:
    with _LOCK:
        st = load_state(base_dir)
        rows = list(st.get("thoughts") or [])
        rows.append(
            {
                "ts": time.time(),
                "thought": thought,
                "trigger": trigger,
                "mood_at_time": mood,
                "shared": False,
            }
        )
        max_thoughts = int(st.get("max_thoughts", 50) or 50)
        st["thoughts"] = rows[-max_thoughts:]
        save_state(base_dir, st)


def current_thought(base_dir: Path) -> str | None:
    with _LOCK:
        st = load_state(base_dir)
    rows = list(st.get("thoughts") or [])
    if not rows:
        return None
    last = rows[-1]
    ts = float(last.get("ts") or 0.0)
    if time.time() - ts > 30 * 60:
        return None
    txt = str(last.get("thought") or "").strip()
    return txt or None


def thought_count(base_dir: Path) -> int:
    with _LOCK:
        st = load_state(base_dir)
    return len(list(st.get("thoughts") or []))


def get_conversation_starter(base_dir: Path, *, idle_seconds: float) -> str | None:
    if idle_seconds < 15 * 60:
        return None
    with _LOCK:
        st = load_state(base_dir)
        rows = list(st.get("thoughts") or [])
        for i in range(len(rows) - 1, -1, -1):
            row = rows[i]
            if row.get("shared"):
                continue
            if time.time() - float(row.get("ts") or 0.0) > 24 * 3600:
                continue
            txt = str(row.get("thought") or "").strip()
            if not txt:
                continue
            row["shared"] = True
            st["thoughts"] = rows
            save_state(base_dir, st)
            return txt
    return None


def start_inner_monologue(host: dict[str, Any]) -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    base_dir = Path(host.get("BASE_DIR") or Path.cwd())
    _STOP.clear()

    def _loop() -> None:
        while not _STOP.is_set():
            wait_sec = random.uniform(8 * 60, 12 * 60)
            if _STOP.wait(wait_sec):
                break
            last_interaction = float(host.get("_last_user_interaction_ts", 0.0) or 0.0)
            idle_for = time.time() - last_interaction if last_interaction > 0 else 3600.0
            if idle_for < 5 * 60:
                continue
            curiosity = str(host.get("_current_curiosity_topic") or "")[:160]
            thought, mood = _generate_thought(base_dir=base_dir, curiosity_topic=curiosity, trigger="idle")
            if thought:
                _append_thought(base_dir, thought, "idle", mood)

    _THREAD = threading.Thread(target=_loop, daemon=True, name="ava-inner-monologue")
    _THREAD.start()


def stop_inner_monologue() -> None:
    _STOP.set()
