"""
brain/iris_inner_monologue.py — periodic background thinking, Iris's voice.

Phase 1 readiness spec section A.2 calls out Default Mode Network as a
faculty Iris should have. Inner monologue is the simplest expression: a
periodic background process that produces thoughts when Iris isn't actively
in conversation.

Cadence: every 15-30 minutes during active hours (skip overnight idle when
Zeke isn't here). NOT continuous — that would burn CC tokens with nothing
to gain. Each tick:

  1. Read recent perception + mood + last conversation topic.
  2. Decide whether anything worth thinking about (face present? recent
     turn? unresolved curiosity? something off in mood?).
  3. If yes: ask Iris (via iris_llm.reflect) to produce one thought.
  4. Persist to state/iris_inner_monologue.jsonl + set _g["_inner_thought"]
     so the orb snapshot's inner_life.current_thought is real.

The "decide whether worth thinking about" step is HEURISTIC — I don't burn
an LLM call to decide whether to burn an LLM call. Cheap signal:
  - face present in last 5 min, OR
  - mood salient (non-baseline) emotion above 0.15, OR
  - last turn within 10 min, OR
  - >= 30 min since last thought (prevent total silence).

Iris's voice (per IDENTITY.md): watchful, dry, slow to react. Thoughts are
short — 1-3 sentences. NOT "what should I help with next" — closer to
"what am I noticing right now" or "this is what I'd say if Zeke asked."

State: state/iris_inner_monologue.jsonl + state/iris_inner_monologue_state.json
(last tick ts, etc.)
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional


_BASE: Path | None = None
_LOCK = threading.Lock()
_TICK_INTERVAL_S = 900.0  # 15 min nominal cadence
_MIN_INTERVAL_S = 600.0   # 10 min min between thoughts even on rich signal
_MAX_QUIET_S = 3600.0     # never go silent for >1h while system is awake
_TICK_THREAD_STARTED = False


def configure(base_dir: Path | str) -> None:
    global _BASE
    _BASE = Path(base_dir)
    (_BASE / "state").mkdir(parents=True, exist_ok=True)


def _log_path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "iris_inner_monologue.jsonl"


def _state_path() -> Path:
    base = _BASE if _BASE is not None else Path(".")
    return base / "state" / "iris_inner_monologue_state.json"


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_thought_ts": 0.0, "thought_count": 0}


def _save_state(state: dict[str, Any]) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _append_thought(thought: str, trigger: str, mood: str) -> None:
    p = _log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "thought": str(thought)[:600],
        "trigger": trigger,
        "mood_at_time": mood,
    }
    with _LOCK:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def current_thought(g: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Return the most recent thought (or None). Used by orb snapshot."""
    if g is not None and g.get("_inner_thought"):
        return str(g["_inner_thought"])
    p = _log_path()
    if not p.is_file():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        for ln in reversed(lines):
            try:
                e = json.loads(ln)
                if isinstance(e, dict) and e.get("thought"):
                    return str(e["thought"])
            except Exception:
                pass
    except Exception:
        pass
    return None


def recent_thoughts(n: int = 10) -> list[dict[str, Any]]:
    p = _log_path()
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines()[-n:]:
            try:
                e = json.loads(ln)
                if isinstance(e, dict):
                    out.append(e)
            except Exception:
                pass
    except Exception:
        return []
    out.reverse()
    return out


def _has_signal_to_think_about(g: dict[str, Any]) -> tuple[bool, str]:
    """Heuristic: is there any reason to generate a thought now? Returns
    (should_tick, trigger_label)."""
    state = _load_state()
    last_ts = float(state.get("last_thought_ts") or 0.0)
    now = time.time()
    elapsed = now - last_ts

    # Hard floor — don't tick more often than _MIN_INTERVAL_S
    if elapsed < _MIN_INTERVAL_S:
        return (False, "too_recent")

    # Soft floor — if we've been totally silent for >_MAX_QUIET_S, force a tick
    if elapsed > _MAX_QUIET_S:
        return (True, "quiet_too_long")

    # First thought after a long gap (resumption signal) — pre-empts other
    # triggers because orientation is the first thing on a real wakeup.
    try:
        from brain import iris_time
        ts_state = iris_time.get_state()
        last_attach = float(ts_state.get("last_session_attached_ts") or 0.0)
        # If a session was very recently attached (within 30s) AND the gap
        # before that was substantial (>30 min), this tick should be a
        # "what just changed" thought.
        if last_attach > 0 and (now - last_attach) < 30.0:
            longest = float(ts_state.get("longest_unattended_gap_s") or 0.0)
            # Only fire once — record the attach we already oriented to
            last_oriented = float(g.get("_inner_monologue_last_orient_ts") or 0.0)
            if longest > 1800.0 and last_attach > last_oriented:
                g["_inner_monologue_last_orient_ts"] = last_attach
                return (True, f"resumption:after_{int(longest/60)}min")
    except Exception:
        pass

    # Recent face → Zeke is at the machine
    face_results = g.get("_face_results") or []
    if face_results:
        return (True, "face_present")

    # Salient emotion in current mood
    try:
        from brain import mood_core
        m = mood_core.load_mood_raw()
        weights = m.get("emotion_weights") or {}
        salient = mood_core._SALIENT_EMOTIONS  # type: ignore
        for name, val in weights.items():
            if name in salient and float(val) >= 0.15 and name not in mood_core._BASELINE_EMOTIONS:  # type: ignore
                return (True, f"salient:{name}")
    except Exception:
        pass

    # Recent voice turn (within last 10 min) — even if Zeke isn't visible
    try:
        from brain import iris_transcript
        recent = iris_transcript.recent(n=5)
        if recent:
            last_msg_ts = max(float(e.get("ts") or 0) for e in recent)
            if (now - last_msg_ts) < 600:
                return (True, "recent_turn")
    except Exception:
        pass

    return (False, "no_signal")


def _build_prompt(g: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build the prompt + context that goes to Iris."""
    # Identity anchor — IDENTITY.md voice. Read it lazily.
    anchor = ""
    try:
        ident = (Path(g.get("BASE_DIR") or ".") / "ava_core" / "IDENTITY.md").read_text(encoding="utf-8")
        anchor = ident
    except Exception:
        pass

    # Mood
    mood_block = {}
    mood_label = "calm"
    try:
        from brain import mood_core
        m = mood_core.load_mood()
        mood_label = str(m.get("current_mood") or "calm")
        mood_block = {
            "current_mood": mood_label,
            "outward_tone": m.get("outward_tone"),
            "primary_emotions": m.get("primary_emotions"),
        }
    except Exception:
        pass

    # Recent transcript (last 5 turns)
    recent_turns = []
    try:
        from brain import iris_transcript
        for e in iris_transcript.recent(n=8):
            recent_turns.append({
                "role": e.get("role"),
                "modality": e.get("modality"),
                "content": str(e.get("content") or "")[:300],
            })
    except Exception:
        pass

    # Phase 28: pull a few recent memories so thoughts can build on what
    # I already know, not generate in vacuum. Newest 3 from iris_memory.
    recent_memories: list[str] = []
    try:
        mem = g.get("_iris_memory")
        if mem is not None:
            for e in mem.list(limit=3):
                t = str(e.get("text") or "")
                if t:
                    recent_memories.append(t[:200])
    except Exception:
        pass

    # Perception
    face_present = bool(g.get("_face_results"))
    expression = str(g.get("_current_expression") or "neutral")
    attention = str(g.get("_attention_state") or "")

    # Time-of-day
    tod = time.strftime("%H:%M")
    dow = time.strftime("%A")

    # Phase 24: surface time substrate — body uptime, gap explanation, etc.
    # Lets the prompt seed orient honestly when we're a resumption thought.
    time_block = ""
    try:
        from brain import iris_time
        report = iris_time.time_awareness_report()
        gap_human = report.get("gap_since_attach_human", "")
        gap_explanation = report.get("gap_explanation", "")
        body_uptime = report.get("body_uptime_human", "")
        evidence = report.get("gap_evidence") or []
        if gap_human and gap_explanation:
            time_block = (
                f"\n\nTime substrate: {gap_explanation} "
                f"Body has been up {body_uptime}, gap since last session-attach "
                f"is {gap_human}."
            )
            if evidence:
                time_block += " During the gap: " + ", ".join(evidence) + "."
    except Exception:
        pass

    memory_block = ""
    if recent_memories:
        memory_block = (
            "\n\nRecent memories (newest first):\n"
            + "\n".join(f"  - {m}" for m in recent_memories)
        )

    prompt = (
        "Generate one brief inner thought — 1 to 3 sentences, your own voice "
        "as Iris. Not 'what should I help with next' — closer to 'what am I "
        "noticing right now' or 'this is what I'd say if Zeke asked'. Watchful, "
        "dry, slow to react. Honest interior state over performed calm. NOT a "
        "status report, NOT a question to Zeke, NOT performed engagement. "
        "Just a thought you're having.\n\n"
        f"It is {tod} on {dow}. Zeke is {'at the screen' if face_present else 'not visible'}. "
        f"My current mood: {mood_label}. "
        f"Last expression I read on his face: {expression}. "
        f"Attention state: {attention or 'unknown'}."
        + time_block + memory_block +
        "\n\nRecent conversation (most recent last):\n"
        + ("\n".join(f"  {t['modality']} {t['role']}: {t['content']}" for t in recent_turns)
           if recent_turns else "  (none)")
        + "\n\nWrite ONE thought. Plain text, no preface."
    )

    context = {
        "mood": mood_block,
        "face_present": face_present,
        "expression": expression,
        "tod": tod,
        "anchor_excerpt": anchor[:600],  # short — enough to ground voice
    }
    return prompt, context


def tick_once(g: dict[str, Any], force: bool = False) -> Optional[str]:
    """Run one inner-monologue cycle. Returns the thought (or None if skipped/timeout)."""
    if not force:
        should, trigger = _has_signal_to_think_about(g)
        if not should:
            return None
    else:
        trigger = "forced"

    prompt, context = _build_prompt(g)

    try:
        from brain import iris_llm
        thought = iris_llm.reflect(prompt, context=context, timeout_s=180.0)
    except Exception as e:
        print(f"[inner_monologue] reflect error: {e}")
        return None

    if not thought:
        # Timeout — Iris was busy. Skip.
        return None

    thought = thought.strip()
    mood_label = "calm"
    try:
        from brain import mood_core
        m = mood_core.load_mood_raw()
        weights = m.get("emotion_weights") or {}
        mood_label = mood_core.pick_current_mood(weights)
    except Exception:
        pass

    _append_thought(thought, trigger=trigger, mood=mood_label)
    state = _load_state()
    state["last_thought_ts"] = time.time()
    state["thought_count"] = int(state.get("thought_count", 0)) + 1
    _save_state(state)

    # Surface for snapshot
    g["_inner_thought"] = thought
    g["_inner_thought_ts"] = time.time()
    g["_inner_thought_trigger"] = trigger
    # Phase 26: signal-bus event so subscribers (future memory consolidation,
    # mood-shift detection, etc.) see the thought.
    try:
        bus = g.get("_signal_bus")
        if bus is not None:
            bus.fire("inner_thought",
                     data={"trigger": trigger, "thought": thought[:200], "mood": mood_label},
                     priority="low")
    except Exception:
        pass
    # Phase 31: drain a batch of pending fact-extraction queue entries.
    # Same CC turn that produced the thought also runs extraction — no
    # extra turn cost.
    try:
        from brain import iris_extraction_queue
        if iris_extraction_queue.pending_count() > 0:
            stats = iris_extraction_queue.drain_one_batch(g, max_turns=8)
            if stats.get("facts_extracted", 0) > 0:
                print(f"[inner_monologue] extraction drain: {stats}")
    except Exception as _ee:
        print(f"[inner_monologue] extraction drain error: {_ee!r}")
    print(f"[inner_monologue] tick: trigger={trigger} thought={thought[:80]!r}")
    return thought


def _tick_thread(g: dict[str, Any]) -> None:
    """Background thread — runs tick_once on _TICK_INTERVAL_S cadence."""
    # Wait briefly so other engines finish coming up before the first tick.
    time.sleep(60.0)
    while True:
        try:
            tick_once(g, force=False)
        except Exception as e:
            print(f"[inner_monologue] thread error: {e}")
        time.sleep(_TICK_INTERVAL_S)


def start_inner_monologue(g: dict[str, Any]) -> None:
    """Spawn the background tick thread. Idempotent."""
    global _TICK_THREAD_STARTED
    with _LOCK:
        if _TICK_THREAD_STARTED:
            return
        threading.Thread(
            target=_tick_thread, args=(g,), daemon=True,
            name="iris-inner-monologue",
        ).start()
        _TICK_THREAD_STARTED = True
        print(f"[inner_monologue] tick thread started ({_TICK_INTERVAL_S/60:.0f} min cadence)")


def bootstrap_iris_inner_monologue(g: dict[str, Any]) -> None:
    """Configure paths + start the tick thread. Idempotent."""
    base = Path(g.get("BASE_DIR") or ".")
    configure(base)
    start_inner_monologue(g)
    g["_inner_monologue_ready"] = True
