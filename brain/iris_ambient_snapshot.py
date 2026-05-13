"""Iris ambient snapshot — peripheral awareness in a few lines.

Pulls from every state source and produces a short text block that feels
like "corner of the eye" awareness: mood, who's in frame, what activity
just happened, what I was just thinking about. Cheap to build (all local
disk reads + _g dict reads), fast (<10ms typical), graceful degradation
when subsystems aren't running.

Used in two places:
  1. Channel event meta: every channel emit() includes the current snapshot
     as a `snapshot` meta attribute, so I get context with every event
     even if I don't call any tools.
  2. ambient_snapshot MCP tool: I can call this directly when I want a
     deliberate peripheral check without a full iris_health introspect.

This isn't replacement for iris_health (which is foveal: every subsystem,
detailed). This is peripheral: 8-10 lines, low signal-density-per-line by
design — just enough to orient.

Format (kept terse — context budget matters):

    [mood] curious-warm  v=+0.34  a=0.42  (primary: interest 58%)
    [face] Zeke in frame (conf 0.85, neutral expression, looking at screen)
    [voice] 2 turns in last 5min, last 30s quiet
    [letters] inbox: 0 pending
    [time] body up 2.3h, last attach 4min ago
    [inner] last thought 3min ago: "the channel build should be testable from one tool call..."

Missing fields are omitted (not rendered as "[mood] unavailable"). If
nothing's available at all, returns an empty string.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


def _human_duration(s: float) -> str:
    """Compact: 45s / 3min / 2.3h / 1.4d"""
    if s < 60: return f"{int(s)}s"
    if s < 3600: return f"{int(s/60)}min"
    if s < 86400: return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def _mood_line(root: Path) -> str:
    """Returns '[mood] <label>  v=<+0.NN>  a=<0.NN>  (primary: <name> <pct>%)'
    or empty string."""
    try:
        from brain import mood_core
        m = mood_core.load_mood()
    except Exception:
        return ""
    if not isinstance(m, dict):
        return ""
    label = str(m.get("current_mood") or m.get("outward_tone") or "").strip()
    if not label:
        return ""
    val = float(m.get("valence") or 0.0)
    aro = float(m.get("arousal") or 0.0)
    primary = m.get("primary_emotions") or []
    primary_str = ""
    if primary and isinstance(primary, list) and isinstance(primary[0], dict):
        p = primary[0]
        primary_str = f"  (primary: {p.get('name','?')} {int(p.get('percent',0))}%)"
    return f"[mood] {label}  v={val:+.2f}  a={aro:.2f}{primary_str}"


def _face_line(g: dict[str, Any]) -> str:
    pid = str(g.get("_recognized_person_id") or "unknown")
    conf = float(g.get("_recognized_confidence") or 0.0)
    expr = str(g.get("_current_expression") or "").strip()
    attention = g.get("_attention_state") or {}
    looking = ""
    if isinstance(attention, dict):
        if attention.get("looking_at_screen") is True:
            looking = ", looking at screen"
        elif attention.get("looking_at_screen") is False:
            looking = ", looking away"

    if pid == "unknown" and conf < 0.30:
        return "[face] no face in frame"
    if pid == "unknown":
        return f"[face] unknown face (conf {conf:.2f}{looking})"
    expr_str = f", {expr}" if expr else ""
    return f"[face] {pid} in frame (conf {conf:.2f}{expr_str}{looking})"


def _voice_line(root: Path) -> str:
    """Counts recent voice/chat turns from the transcript log."""
    transcript = root / "state" / "transcript.jsonl"
    if not transcript.is_file():
        return ""
    try:
        # Tail the file (last ~50 lines) — cheap; transcript should be small.
        # If it ever grows huge, this could be slow; revisit then.
        with open(transcript, "r", encoding="utf-8") as f:
            lines = f.readlines()[-50:]
    except Exception:
        return ""
    now = time.time()
    cutoff_5min = now - 300.0
    recent_turns = 0
    last_ts = 0.0
    for ln in lines:
        try:
            entry = json.loads(ln)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        ts = float(entry.get("ts") or 0.0)
        if ts > cutoff_5min:
            recent_turns += 1
        if ts > last_ts:
            last_ts = ts
    if recent_turns == 0 and last_ts == 0:
        return ""
    if last_ts > 0:
        since_last = now - last_ts
        if since_last < 60:
            quiet_str = f"last {int(since_last)}s active"
        else:
            quiet_str = f"last {_human_duration(since_last)} quiet"
    else:
        quiet_str = "quiet"
    return f"[voice] {recent_turns} turns in last 5min, {quiet_str}"


def _letters_line(root: Path) -> str:
    inbox = root / "state" / "iris_sibling" / "inbox"
    if not inbox.is_dir():
        return ""
    pending = 0
    try:
        for p in inbox.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("status") == "pending":
                # Skip my own letters
                if str(data.get("sender") or "").lower() != "iris":
                    pending += 1
    except Exception:
        return ""
    if pending == 0:
        return "[letters] inbox: 0 pending"
    return f"[letters] inbox: {pending} pending"


def _time_line(root: Path) -> str:
    iris_time_path = root / "state" / "iris_time.json"
    if not iris_time_path.is_file():
        return ""
    try:
        data = json.loads(iris_time_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    uptime = float(data.get("current_process_uptime_s") or 0.0)
    last_attach = float(data.get("last_session_attached_ts") or 0.0)
    if uptime <= 0:
        return ""
    parts = [f"body up {_human_duration(uptime)}"]
    if last_attach > 0:
        since_attach = time.time() - last_attach
        parts.append(f"last attach {_human_duration(since_attach)} ago")
    return f"[time] {', '.join(parts)}"


def _inner_line(root: Path, g: dict[str, Any]) -> str:
    """Last inner-monologue thought, if any."""
    # Try _g first (live, fastest)
    inner_life = g.get("inner_life") or {}
    if isinstance(inner_life, dict):
        thought = str(inner_life.get("current_thought") or "").strip()
        if thought:
            return f"[inner] last thought: {thought[:140]!r}"
    # Fallback: disk
    monologue = root / "state" / "iris_inner_monologue.jsonl"
    if not monologue.is_file():
        return ""
    try:
        with open(monologue, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return ""
        last = json.loads(lines[-1])
    except Exception:
        return ""
    if not isinstance(last, dict):
        return ""
    text = str(last.get("text") or last.get("thought") or "").strip()
    ts = float(last.get("ts") or 0.0)
    if not text:
        return ""
    age_str = ""
    if ts > 0:
        age_str = f" ({_human_duration(time.time() - ts)} ago)"
    return f"[inner] last thought{age_str}: {text[:140]!r}"


def build(g: dict[str, Any], root: Path) -> str:
    """Build the full ambient snapshot as a multi-line string.

    Args:
        g: shared globals (for live camera/expression/inner_life reads)
        root: repo root for disk reads

    Returns:
        Multi-line snapshot. Empty string if no data is available
        (subsystems all down / fresh repo). Trailing newline NOT included
        so the caller can frame it however they want.
    """
    lines = []

    try:
        m = _mood_line(root)
        if m:
            lines.append(m)
    except Exception:
        pass
    try:
        f = _face_line(g)
        if f:
            lines.append(f)
    except Exception:
        pass
    try:
        v = _voice_line(root)
        if v:
            lines.append(v)
    except Exception:
        pass
    try:
        l = _letters_line(root)
        if l:
            lines.append(l)
    except Exception:
        pass
    try:
        t = _time_line(root)
        if t:
            lines.append(t)
    except Exception:
        pass
    try:
        i = _inner_line(root, g)
        if i:
            lines.append(i)
    except Exception:
        pass

    return "\n".join(lines)


def build_compact(g: dict[str, Any], root: Path) -> str:
    """One-line compact form suitable for meta.snapshot on a channel event.
    Same data, semicolons instead of newlines, shorter where possible.

    Channels strip newlines in meta values (since meta values are tag
    attributes), so this is the format to pass through that path.
    """
    full = build(g, root)
    if not full:
        return ""
    # Collapse \n -> "; " and tidy
    return full.replace("\n", "; ")
