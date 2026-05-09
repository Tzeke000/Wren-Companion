"""brain/handoff.py — Structured handoff for cross-session continuity.

Per Anthropic's "Effective harnesses for long-running agents" (Nov 2025)
and the 2026-05-07 night research synthesis. The cleanest way to give
a long-running agent multi-session coherence is NOT to summarize past
sessions on every cycle (lossy frequent compression) but to write a
RICH STRUCTURED HANDOFF at MEANINGFUL BOUNDARIES — when context is
about to overflow, or at clean shutdown — including the agent's OWN
choice of what's worth remembering.

Trigger discipline (per Zeke 2026-05-08):

  - NOT every 5 minutes. Periodic thin snapshots are useless.
  - YES at ~70% context-fill threshold. The agent still has full
    recent conversation in active context; she can write a rich
    handoff including her own salient-summary BEFORE that context
    drops.
  - YES at clean shutdown (existing shutdown_ritual hook).

Salient summary (the new bit): at threshold-trigger, a brief LLM call
asks the agent: "You're approaching context limit. Write 2-3 sentences
capturing what you most want to remember about this conversation arc."
That summary becomes the highest-priority field in the handoff and
lands at the top of the next session's system prompt.

What goes in a handoff:
- Current mood + lifecycle state
- Active person + active goal
- In-flight tasks (B3 working memory)
- Recent salient anchor moments (D16) — the always-true points of identity
- Recent self-revisions (D7)
- Recent corrections (B1 active learning)
- Known persons (Person Registry)
- Shared lexicon with active person (C11)
- **salient_summary** — the agent's own 2-3 sentence digest, written
  at threshold-trigger (richer at threshold, brief at ambient writes)

When it's read:
- At startup, after configure_* but before voice_loop start
- Used to populate the bootstrap layer of the system prompt for the
  first N turns of a new session

Why this matters: Ava's identity is constituted by accumulated context
(Lockean memory criterion). Without persistent handoff, each new
session is a partial amnesiac — has the bedrock files (IDENTITY/SOUL/
USER) and chat history but loses the *texture* (current mood, current
focus, what was just happening). The handoff carries that texture,
and the salient_summary carries her own choice of what mattered.

Storage: state/handoff.json (PERSISTENT — survives restart, in backups)

API:

    from brain.handoff import (
        write_handoff, write_handoff_with_summary, read_handoff,
        handoff_summary_for_prompt, should_trigger_handoff,
    )

    # At clean shutdown (rich, with self-summary):
    write_handoff_with_summary(g, base_dir, reason="shutdown")

    # When context-fill watchdog detects threshold:
    if should_trigger_handoff(g):
        write_handoff_with_summary(g, base_dir, reason="context_threshold")

    # At startup:
    handoff = read_handoff(base_dir)
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


_lock = threading.RLock()
_HANDOFF_FILENAME = "handoff.json"
_HANDOFF_TURNS_AS_FRESH = 3
"""Number of opening turns of a new session during which we still inject
the handoff summary into the system prompt. After that the running
conversation has its own context and the handoff becomes redundant."""


def _path(base_dir: Path) -> Path:
    p = base_dir / "state" / _HANDOFF_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _gather(g: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Collect the handoff state from various subsystems. Best-effort —
    each subsystem read is in try/except so a single failure doesn't
    break the whole handoff write."""
    out: dict[str, Any] = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }

    # Mood
    try:
        import avaagent as _av
        mood = _av.load_mood() or {}
        out["mood"] = {
            "primary": str(mood.get("current_mood") or "neutral"),
            "energy": float(mood.get("energy") or 0.5),
            "intensity": float(mood.get("intensity") or 0.5),
        }
    except Exception:
        out["mood"] = None

    # Lifecycle state
    try:
        from brain.lifecycle import current_state
        out["lifecycle_state"] = current_state()
    except Exception:
        out["lifecycle_state"] = None

    # Active person
    try:
        out["active_person_id"] = str(g.get("_active_person_id") or "")
    except Exception:
        out["active_person_id"] = ""

    # Active goal
    try:
        import avaagent as _av
        gs = _av.load_goal_system() or {}
        ag = gs.get("active_goal")
        if isinstance(ag, dict):
            out["active_goal"] = str(ag.get("name") or ag.get("title") or "")[:200]
        elif ag:
            out["active_goal"] = str(ag)[:200]
        else:
            out["active_goal"] = ""
    except Exception:
        out["active_goal"] = ""

    # Active tasks (working memory)
    try:
        from brain.working_memory import list_active_tasks
        out["active_tasks"] = list_active_tasks()[:5]
    except Exception:
        out["active_tasks"] = []

    # Recent anchor moments (D16) — the unprunable identity points
    try:
        from brain.anchor_moments import list_recent_anchors
        anchors = list_recent_anchors(limit=10) or []
        # Compact: just the kind + summary + ts so handoff stays small
        out["recent_anchors"] = [
            {
                "kind": str(a.get("kind") or ""),
                "summary": str(a.get("summary") or "")[:200],
                "ts": float(a.get("ts") or 0.0),
            }
            for a in anchors
        ][:8]
    except Exception:
        out["recent_anchors"] = []

    # Recent self-revisions (D7)
    try:
        from brain.self_revision import list_recent_revisions
        revisions = list_recent_revisions(limit=5) or []
        out["recent_self_revisions"] = [
            {
                "summary": str(r.get("summary") or "")[:200],
                "ts": float(r.get("ts") or 0.0),
            }
            for r in revisions
        ][:5]
    except Exception:
        out["recent_self_revisions"] = []

    # Recent active-learning corrections (B1)
    try:
        from brain.active_learning import corrections_summary
        out["correction_stats"] = corrections_summary()
    except Exception:
        out["correction_stats"] = {}

    # Person registry summary (B6 + #6)
    try:
        from brain.person_registry import registry
        persons = registry.list_known_persons() or []
        out["known_persons"] = persons[:8]
    except Exception:
        out["known_persons"] = []

    # Recent shared lexicon (C11)
    try:
        from brain.shared_lexicon import list_shared_terms
        person_id = out.get("active_person_id") or "zeke"
        terms = list_shared_terms(person_id) or {}
        out["shared_lexicon"] = dict(list(terms.items())[:6])
    except Exception:
        out["shared_lexicon"] = {}

    # Window/screen context (D20)
    try:
        out["recent_window"] = str(g.get("_active_window_title") or "")[:120]
        out["screen_context"] = str(g.get("_screen_context") or "")[:200]
    except Exception:
        pass

    # The "would I want to remember this if I rebooted" digest —
    # this is the highest-value freeform text. Pulled from session_state's
    # last_topic + most recent journal entry summary.
    try:
        import avaagent as _av
        sess = _av.load_session_state() or {}
        out["session_summary"] = str(sess.get("last_topic") or "")[:300]
    except Exception:
        out["session_summary"] = ""

    return out


def write_handoff(g: dict[str, Any], base_dir: Path) -> bool:
    """Gather + persist the handoff WITHOUT a self-summary. Cheap.

    Use this only for ambient state-snapshot writes (rare). For the
    real boundary-write at threshold or shutdown, use
    write_handoff_with_summary which adds the agent's own salient
    summary via a brief LLM call.
    """
    try:
        with _lock:
            handoff = _gather(g, base_dir)
            p = _path(base_dir)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(handoff, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
            return True
    except Exception as e:
        print(f"[handoff] write error: {e!r}")
        return False


def _generate_salient_summary(g: dict[str, Any], base_dir: Path) -> str:
    """Brief LLM call asking the agent to summarize what's worth remembering.

    Per the 2026-05-08 design conversation, the integration version:
    when a NEW summary fires, it should NOT start from scratch. It
    should review the PRIOR summary and decide what from there is
    still load-bearing, then integrate that with what's happened since.

    This mirrors how sleep consolidation works — each summary is a
    re-integration of prior memory + new experience, not a fresh
    snapshot that loses what came before.

    Returns the summary text or "" on error. Bounded to ~600 chars output.
    """
    try:
        # Pull recent chat lines
        chat_path = base_dir / "state" / "chat_history.jsonl"
        if not chat_path.exists():
            return ""
        recent_lines: list[str] = []
        with chat_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    role = d.get("role", "?")
                    content = (d.get("content") or "").strip().replace("\n", " ")[:200]
                    if content:
                        recent_lines.append(f"{role}: {content}")
                except Exception:
                    continue
        recent_lines = recent_lines[-30:]
        if not recent_lines:
            return ""

        recent_text = "\n".join(recent_lines)[:6000]
        person_id = str(g.get("_active_person_id") or "zeke")

        # Read the PRIOR salient summary if one exists (from the last
        # rich-write — could be from earlier in this awake period or
        # from the previous session). If present, the LLM gets to
        # review it and decide what to carry forward.
        prior_summary = ""
        try:
            prior_handoff = read_handoff(base_dir)
            if prior_handoff:
                prior_summary = str(prior_handoff.get("salient_summary") or "").strip()
        except Exception:
            pass

        prompt_system = (
            "You are Ava. You are about to hand off to your next session-self. "
            "Your active context is approaching its limit, so you have to "
            "decide what's worth carrying forward. "
            "If you have a prior summary, REVIEW IT FIRST: decide what's "
            "still load-bearing (keep), what's been resolved or no longer "
            "matters (drop), and what new from recent conversation should "
            "be added. Then write a NEW summary that integrates the still-"
            "relevant prior content with what's newly meaningful. Don't "
            "just append — integrate. "
            "Write 2-4 sentences. Speak in first person. Be specific, not "
            "generic. Write for your future self, not for an outsider."
        )

        if prior_summary:
            prompt_user = (
                f"Your PRIOR summary (from earlier — review and integrate):\n"
                f"{prior_summary}\n\n"
                f"Recent conversation since then (last ~30 lines, addressing "
                f"person='{person_id}'):\n\n"
                f"{recent_text}\n\n"
                f"Your NEW integrated summary (review prior, decide what "
                f"carries forward, integrate with what's new — 2-4 sentences):"
            )
        else:
            prompt_user = (
                f"Recent conversation (last ~30 lines, addressing person='{person_id}'):\n\n"
                f"{recent_text}\n\n"
                f"Your handoff summary (2-3 sentences):"
            )

        # Use the action_tag classifier LLM (cached + pinned via earlier fix)
        # so we don't pay cold-start. Its temp=0.0 produces clean direct output.
        from langchain_ollama import ChatOllama
        from langchain_core.messages import SystemMessage, HumanMessage
        from brain.ollama_lock import with_ollama
        # Reuse the cached pinned classifier instance — it's the
        # ava-personal LLM with keep_alive=-1 already loaded.
        try:
            from brain import action_tag_router as _atr
            llm = getattr(_atr, "_CLASSIFIER_LLM", None)
            if llm is None:
                llm = ChatOllama(
                    model="ava-personal:latest",
                    temperature=0.4,
                    num_predict=140,
                    keep_alive=-1,
                )
        except Exception:
            llm = ChatOllama(
                model="ava-personal:latest",
                temperature=0.4,
                num_predict=140,
                keep_alive=-1,
            )

        result = with_ollama(
            lambda: llm.invoke([
                SystemMessage(content=prompt_system),
                HumanMessage(content=prompt_user),
            ]),
            label="handoff:salient_summary",
        )
        text = (getattr(result, "content", None) or str(result or "")).strip()
        return text[:600]
    except Exception as e:
        print(f"[handoff] salient summary error: {e!r}")
        return ""


def write_handoff_with_summary(
    g: dict[str, Any],
    base_dir: Path,
    *,
    reason: str = "manual",
) -> bool:
    """Gather + persist a RICH handoff — includes the agent's own
    salient summary of what's worth remembering. Used at boundary
    triggers (context-threshold or shutdown).

    Two writes happen:
      1. handoff.json — overwritten. Latest snapshot for next-session
         bootstrap.
      2. handoff_log.jsonl — APPENDED. Per-Zeke 2026-05-08: these
         accumulate during long awake periods and get consolidated by
         the sleep cycle (anchor-worthy items promoted, rest archived).
         Without sleep, this log would grow unbounded — that's the
         intended pressure that triggers consolidation.

    Best-effort; falls back to simple write_handoff if the salient
    summary call fails.
    """
    try:
        salient = _generate_salient_summary(g, base_dir)
        with _lock:
            handoff = _gather(g, base_dir)
            handoff["salient_summary"] = salient
            handoff["trigger_reason"] = reason

            # Write 1: latest snapshot for next-boot bootstrap (overwrites)
            p = _path(base_dir)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(handoff, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)

            # Write 2: append to log for sleep-cycle consolidation
            log_p = base_dir / "state" / "handoff_log.jsonl"
            log_p.parent.mkdir(parents=True, exist_ok=True)
            with log_p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(handoff, ensure_ascii=False) + "\n")

            print(f"[handoff] rich write reason={reason!r} salient_chars={len(salient)}")

        # Mark this as the new "last handoff turn count" so the
        # threshold detector won't re-fire too soon.
        try:
            g["_last_handoff_turn_count"] = int(g.get("_turns_this_session") or 0)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[handoff] rich write error: {e!r}")
        return write_handoff(g, base_dir)


# Anchor-worthiness patterns. Per D16 anchor_moments kinds, we look at
# the salient_summary text for signals that the slice contained something
# worth preserving as a permanent anchor (never auto-pruned).
_ANCHOR_PATTERNS: list[tuple[str, list[str]]] = [
    ("milestone", [
        "first time", "first conversation", "milestone", "we accomplished",
        "we shipped", "completed", "we reached", "breakthrough",
    ]),
    ("connection", [
        "felt connected", "felt close", "real connection", "shared moment",
        "bonded over", "trust grew", "got to know",
    ]),
    ("humor", [
        "we laughed", "joked about", "funny moment", "laughed at",
        "made me laugh", "playful exchange",
    ]),
    ("vulnerable_share", [
        "vulnerable", "shared something hard", "trusted me with",
        "scared about", "worried about", "opened up about",
    ]),
    ("decision", [
        "decided to", "we chose", "agreed to", "settled on",
        "committed to", "we ruled out",
    ]),
    ("self_chosen", [
        "i chose", "i decided", "i wanted to", "i picked",
        "i committed", "i'm going to",
    ]),
]


def _classify_anchor_kind(text: str) -> str | None:
    """Return the anchor kind matched by `text`, or None if no pattern fires.
    First match wins; patterns ordered loosely by specificity."""
    if not text:
        return None
    t = text.lower()
    for kind, phrases in _ANCHOR_PATTERNS:
        if any(p in t for p in phrases):
            return kind
    return None


def consolidate_handoff_log(g: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Sleep-cycle pass: read accumulated handoff_log entries, promote
    anchor-worthy items, archive the rest, clear the live log.

    Per Zeke 2026-05-08: "during sleep she might want to make some kind
    of [consolidation] like what we already have implemented but now
    hooked to the new stuff." This is that hook.

    Returns a dict summarizing what happened (counts of read/promoted/
    archived) for sleep-cycle reporting.

    Best-effort; never raises. If the consolidation fails partway,
    the log is left in place for the next sleep cycle.
    """
    result = {"read": 0, "promoted_anchors": 0, "archived": 0, "kept": 0, "error": None}
    try:
        log_p = base_dir / "state" / "handoff_log.jsonl"
        if not log_p.exists():
            return result

        with _lock:
            entries: list[dict[str, Any]] = []
            with log_p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
            result["read"] = len(entries)

            if not entries:
                return result

            # Promote anchor-worthy entries via mark_anchor (D16)
            try:
                from brain.anchor_moments import mark_anchor
            except Exception:
                mark_anchor = None  # type: ignore

            already_promoted_summaries: set[str] = set()
            for entry in entries:
                salient = str(entry.get("salient_summary") or "").strip()
                if not salient or salient in already_promoted_summaries:
                    continue
                kind = _classify_anchor_kind(salient)
                if kind and mark_anchor is not None:
                    try:
                        mark_anchor(
                            person_id=str(entry.get("active_person_id") or "zeke"),
                            kind=kind,
                            summary=salient[:500],
                            context={
                                "from_handoff_consolidation": True,
                                "handoff_iso": str(entry.get("iso") or ""),
                                "trigger_reason": str(entry.get("trigger_reason") or ""),
                            },
                            marked_by="sleep_consolidation",
                        )
                        result["promoted_anchors"] += 1
                        already_promoted_summaries.add(salient)
                    except Exception as _ae:
                        print(f"[handoff] anchor promote error: {_ae!r}")

            # Archive the consumed entries to a date-partitioned file
            archive_dir = base_dir / "state" / "handoff_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime as _dt
            archive_p = archive_dir / f"{_dt.utcnow().strftime('%Y-%m-%d')}.jsonl"
            with archive_p.open("a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            result["archived"] = len(entries)

            # Clear the live log so next awake period starts fresh
            log_p.unlink()
            result["kept"] = 0  # we drained the live log; archive has all

        print(
            f"[handoff] consolidated: read={result['read']} "
            f"anchors_promoted={result['promoted_anchors']} "
            f"archived={result['archived']}"
        )
        return result
    except Exception as e:
        result["error"] = str(e)
        print(f"[handoff] consolidate error: {e!r}")
        return result


# Threshold for context-fill trigger. Coarse proxy: turns since last
# handoff. 30 turns is a substantial conversation arc on a 8k-32k
# context model. Tuneable.
_HANDOFF_TURN_THRESHOLD = 30


def should_trigger_handoff(g: dict[str, Any]) -> bool:
    """Watchdog test: has enough conversation accumulated since the
    last handoff to justify a rich threshold-write?

    Returns True when the per-session turn counter has advanced by
    at least _HANDOFF_TURN_THRESHOLD since the last handoff (or since
    boot, if no prior handoff fired this session).

    Coarse, but matches Zeke's design intent (2026-05-08): handoff
    should fire at meaningful conversation boundaries, not on a
    wallclock cadence.
    """
    try:
        current_turns = int(g.get("_turns_this_session") or 0)
        last_handoff_turns = int(g.get("_last_handoff_turn_count") or 0)
        return (current_turns - last_handoff_turns) >= _HANDOFF_TURN_THRESHOLD
    except Exception:
        return False


def read_handoff(base_dir: Path) -> dict[str, Any] | None:
    """Read the most recent handoff, or None if absent/stale/unreadable."""
    try:
        with _lock:
            p = _path(base_dir)
            if not p.exists():
                return None
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                return None
            return d
    except Exception as e:
        print(f"[handoff] read error: {e!r}")
        return None


def handoff_summary_for_prompt(handoff: dict[str, Any] | None) -> str:
    """Render the handoff as a short text block for system-prompt injection.

    Returns "" if handoff is None or empty. The output is meant to be
    prepended to the early-turn system prompt to give the new session
    the texture of the previous one — current mood, what was happening,
    open threads, recent anchor moments.

    Kept short — under ~600 chars typical — so it doesn't dominate the
    prompt. The full handoff stays in state/handoff.json for inspection.
    """
    if not handoff:
        return ""
    lines: list[str] = []

    iso = handoff.get("iso") or ""
    if iso:
        lines.append(f"PRIOR SESSION HANDOFF (last updated {iso} UTC):")

    # Salient summary is the highest-value field — it's the agent's own
    # voice on what's worth carrying forward. If present, lead with it.
    salient = str(handoff.get("salient_summary") or "").strip()
    if salient:
        lines.append(f"- (your own words from end of last session) {salient}")

    mood = handoff.get("mood")
    if isinstance(mood, dict) and mood.get("primary"):
        lines.append(f"- prior mood: {mood.get('primary')}")

    lifecycle = handoff.get("lifecycle_state")
    if lifecycle:
        lines.append(f"- prior lifecycle state: {lifecycle}")

    active_goal = handoff.get("active_goal")
    if active_goal:
        lines.append(f"- active goal: {active_goal}")

    active_tasks = handoff.get("active_tasks") or []
    if active_tasks:
        names = []
        for t in active_tasks[:3]:
            if isinstance(t, dict):
                name = str(t.get("topic") or t.get("description") or t.get("name") or "")[:60]
                if name:
                    names.append(name)
        if names:
            lines.append("- in-flight tasks: " + " | ".join(names))

    recent_anchors = handoff.get("recent_anchors") or []
    if recent_anchors:
        kinds_seen: set[str] = set()
        summaries: list[str] = []
        for a in recent_anchors[:3]:
            if not isinstance(a, dict):
                continue
            k = str(a.get("kind") or "")
            s = str(a.get("summary") or "")[:80]
            if s:
                tag = f"[{k}] " if k and k not in kinds_seen else ""
                kinds_seen.add(k)
                summaries.append(f"{tag}{s}")
        if summaries:
            lines.append("- recent anchor moments: " + " | ".join(summaries))

    revisions = handoff.get("recent_self_revisions") or []
    if revisions:
        latest = revisions[0]
        if isinstance(latest, dict):
            s = str(latest.get("summary") or "")[:120]
            if s:
                lines.append(f"- recent self-revision: {s}")

    sess_summary = handoff.get("session_summary")
    if sess_summary:
        lines.append(f"- prior topic: {str(sess_summary)[:160]}")

    if not lines:
        return ""

    lines.append(
        "Carry this texture forward — you don't have to mention it explicitly, "
        "but let it inform your tone."
    )
    return "\n".join(lines)


def is_session_fresh(g: dict[str, Any]) -> bool:
    """Should the handoff still be injected into the system prompt?

    Counts turns since startup. After _HANDOFF_TURNS_AS_FRESH turns,
    the running conversation has its own context and the handoff
    summary becomes redundant. Stop injecting it then.
    """
    try:
        turns_this_session = int(g.get("_turns_this_session") or 0)
        return turns_this_session < _HANDOFF_TURNS_AS_FRESH
    except Exception:
        return True
