"""Claude Code Stop hook -> Iris bridge.

Fires after every assistant turn ends. Decides whether to rewake the model,
and what to tell it via the rewake message. Two triggers:

1. **Voice mode** — D:\\Wren-Companion\\.tmp\\voice_session.flag exists.
   Reads the last assistant text block, POSTs it to Kokoro for TTS, then
   rewakes with the voice-loop directive.

2. **Pending chat request** — state/iris_chat/.pending exists. Rewakes with
   a chat-handle directive that includes the request_id and user_text. The
   model calls chat_reply(id, text) which writes the response file and
   unblocks the orb's POST /api/v1/chat long-poll. No TTS — chat is text.

If both are active, **chat goes first** (FIFO across modalities). After
answering chat, the next Stop fires with voice still flagged and resumes
the voice loop.

If neither is active, exit 0 (no rewake — return to manual control).

Settings.json registers this with `asyncRewake: true`. Exit code 2 carries
the rewake message via stdout.

Exit codes:
  0 — neither voice nor chat active OR error path (don't loop on failure)
  2 — rewake — message printed to stdout
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib import request as _req

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(r"D:\Wren-Companion")
_LOCK_PATH = ROOT / ".tmp" / "stop_hook.lock"


def _acquire_singleton_lock():
    """Filesystem-level singleton lock. Returns the file handle on success,
    None if another hook is already running, or the sentinel string
    "lock_unavailable" if the lock primitive itself failed (caller should
    proceed without locking — no-worse-than-before).

    OS releases the lock automatically on process exit, so no stale-lock
    cleanup is needed. Verified: Windows refuses to delete a file with an
    open handle, so the lockfile can't be removed while held.
    """
    try:
        import msvcrt
    except Exception:
        return "lock_unavailable"
    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(_LOCK_PATH, "a+b")
    except Exception:
        return "lock_unavailable"
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except OSError:
        try:
            fh.close()
        except Exception:
            pass
        return None
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return "lock_unavailable"

# Paths sourced via brain/iris_paths to keep one source of truth across
# iris_runtime, brain/orb_http, brain/iris_chat, brain/iris_llm, and this hook.
sys.path.insert(0, str(ROOT))
try:
    from brain.iris_paths import paths as _paths
    _paths.configure(ROOT)
    VOICE_FLAG = _paths.voice_flag
    CHAT_PENDING_FLAG = _paths.chat_pending_flag
    CHAT_DIR = _paths.chat_dir
    LLM_PENDING_FLAG = _paths.llm_pending_flag
    LLM_DIR = _paths.llm_dir
    LAST_SPOKEN_PATH = _paths.last_spoken_uuid
except Exception:
    # Fallback if iris_paths can't import (broken venv, etc) — keep the hook
    # functional with hardcoded paths so a code error doesn't kill voice mode.
    VOICE_FLAG = ROOT / ".tmp" / "voice_session.flag"
    CHAT_PENDING_FLAG = ROOT / "state" / "iris_chat" / ".pending"
    CHAT_DIR = ROOT / "state" / "iris_chat"
    LLM_PENDING_FLAG = ROOT / "state" / "iris_llm" / ".pending"
    LLM_DIR = ROOT / "state" / "iris_llm"
    LAST_SPOKEN_PATH = ROOT / ".tmp" / "last_spoken_uuid.txt"

# Sibling inbox lives outside the iris_paths registry — it's populated by
# scripts/sibling_postoffice.py (a separate process), not iris_runtime,
# so it gets hardcoded paths to stay independent of harness state.
SIBLING_DIR = ROOT / "state" / "iris_sibling"
SIBLING_INBOX_DIR = SIBLING_DIR / "inbox"
SIBLING_PENDING_FLAG = SIBLING_DIR / ".pending"

TTS_URL = "http://127.0.0.1:5876/api/v1/tts/speak"
HTTP_TIMEOUT_S = 2.0


def _strip_markdown(text: str) -> str:
    # Strip XML-style internal blocks first (thinking, antml, etc.) so any
    # leakage doesn't get spoken aloud as XML. Multiline + lazy. Tag-name
    # pattern is constrained to identifier chars so legitimate prose using
    # angle brackets (math, code-talk) isn't eaten.
    text = re.sub(r"<thinking\b[^>]*>[\s\S]*?</thinking\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<([a-zA-Z][\w:.-]*)\b[^>]*>[\s\S]*?</\1\s*>", "", text)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _last_assistant_text(transcript_path: str) -> tuple[str, str]:
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            entries = [ln for ln in f if ln.strip()]
    except Exception:
        return "", ""

    for raw_line in reversed(entries):
        try:
            entry = json.loads(raw_line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        content = msg.get("content") or []
        parts: list[str] = []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    t = c.get("text") or ""
                    if t:
                        parts.append(t)
        elif isinstance(content, str):
            parts.append(content)
        if any(p.strip() for p in parts):
            uuid = str(entry.get("uuid") or entry.get("messageId") or "")
            return uuid, "\n".join(parts)
    return "", ""


# ── Channel auto-forward (routing safety net) ──────────────────────────────
# Per Zeke directive 2026-05-18 after multiple routing misses on day 1 of
# deployment: when the last inbound is from Discord (or another channel)
# and the assistant turn didn't include a matching outbound tool call,
# automatically forward the assistant text to the inbound channel. Closes
# the "I keep responding in CC text when Zeke is on Discord" failure mode.
#
# Architecture:
# - Reads last user message + assistant turn from transcript
# - Extracts <channel source="X" chat_id="Y"> tag if present in user message
# - Inspects assistant turn for tool_use entries that match the channel
# - If channel-inbound exists AND no matching outbound tool call →
#   forward via the channel's API (Discord REST currently; sibling-letter
#   for fam-chat could follow)
#
# Failure mode: forward fails → log to stderr, continue. Best-effort feature.
# Zeke catches anyway; this just reduces the chance he has to.

DISCORD_TOKEN_PATH = ROOT / "state" / "secrets" / "discord_iris_bot_token.txt"
DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MAX_LEN = 2000  # Discord enforces 2000-char limit on message content


def _last_user_text_with_channel(transcript_path: str) -> tuple[str, int]:
    """Return (text, line_idx) of the last user message containing a <channel ...>
    tag. Skips type=user entries whose content is only tool_results (which the
    transcript stores as type=user but don't represent actual inbound messages).
    Returns ("", -1) if none found.

    The line_idx is the transcript-line index of the found entry — used by
    _tool_uses_since_idx to limit tool-use scan to "this turn."
    """
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            entries = [ln for ln in f if ln.strip()]
    except Exception:
        return "", -1
    for i in range(len(entries) - 1, -1, -1):
        try:
            entry = json.loads(entries[i])
        except Exception:
            continue
        if entry.get("type") != "user":
            continue
        msg = entry.get("message") or {}
        content = msg.get("content") or []
        parts: list[str] = []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    t = c.get("text") or ""
                    if t:
                        parts.append(t)
        elif isinstance(content, str):
            parts.append(content)
        combined = "\n".join(parts)
        if "<channel" in combined:
            return combined, i
    return "", -1


def _tool_uses_since_idx(transcript_path: str, start_idx: int) -> list[dict]:
    """Return ALL tool_use entries from assistant entries AFTER start_idx.
    Captures every tool call across the current turn, not just the last one."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            entries = [ln for ln in f if ln.strip()]
    except Exception:
        return []
    tool_uses: list[dict] = []
    for i in range(start_idx + 1, len(entries)):
        try:
            entry = json.loads(entries[i])
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        content = msg.get("content") or []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tool_uses.append({
                        "name": c.get("name", ""),
                        "input": c.get("input", {}) or {},
                    })
    return tool_uses


# Match the <channel source="X" chat_id="Y" message_id="Z" ...> tag.
_CHANNEL_TAG_RE = re.compile(
    r'<channel\s+([^>]+?)\s*/?>',
    re.IGNORECASE,
)


def _extract_channel_tag(text: str) -> dict | None:
    """Parse the most recent <channel ...> tag in text. Returns dict with
    source/chat_id/message_id/user keys, or None if no tag found."""
    if not text:
        return None
    matches = list(_CHANNEL_TAG_RE.finditer(text))
    if not matches:
        return None
    # Use the LAST match — if multiple inbound channels were referenced,
    # the most-recent one is the one we're responding to.
    attrs_str = matches[-1].group(1)
    attrs: dict = {}
    for m in re.finditer(r'(\w+)="([^"]*)"', attrs_str):
        attrs[m.group(1)] = m.group(2)
    if not attrs.get("source"):
        return None
    return attrs


# NOTE: _last_assistant_tool_uses was retired 2026-05-18 — it only read tool_uses
# from a single assistant entry, but the CC transcript writes each tool call as
# its own type=assistant entry, so the function only saw the most-recent one.
# Replaced by _tool_uses_since_idx which walks ALL entries since the inbound
# message and aggregates the full turn's tool calls. See _check_and_auto_forward
# below for the new flow.


def _load_discord_token() -> str | None:
    """Read the bot token from disk. Returns None if unreadable."""
    try:
        return DISCORD_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _forward_to_discord(chat_id: str, text: str) -> bool:
    """POST text to Discord channel via the bot token. Returns True on success."""
    token = _load_discord_token()
    if not token:
        print(f"[stop_hook auto-forward] no Discord token, skip", file=sys.stderr)
        return False
    if not text or not text.strip():
        return False
    # Truncate to Discord's limit. Append marker so the truncation is visible.
    if len(text) > DISCORD_MAX_LEN:
        text = text[: DISCORD_MAX_LEN - 30] + "\n\n[truncated by auto-forward]"
    url = f"{DISCORD_API_BASE}/channels/{chat_id}/messages"
    body = json.dumps({"content": text}).encode("utf-8")
    req = _req.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            # Discord requires User-Agent for bot REST requests; omitting
            # causes 403 Forbidden. Verified empirically 2026-05-18 — direct
            # test without UA returned 403; same request WITH UA returned 200.
            "User-Agent": "IrisBot/1.0 (+stop_hook_auto_forward)",
        },
        method="POST",
    )
    try:
        with _req.urlopen(req, timeout=5.0) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[stop_hook auto-forward] Discord POST failed: {e!r}", file=sys.stderr)
        return False


def _check_and_auto_forward(transcript_path: str) -> None:
    """If the last user message had a channel tag and the assistant turn
    didn't include a matching outbound tool call, forward the assistant text
    to the inbound channel. Best-effort — failures log to stderr."""
    user_text, inbound_idx = _last_user_text_with_channel(transcript_path)
    if not user_text or inbound_idx < 0:
        return  # no inbound channel-tagged message in the transcript
    tag = _extract_channel_tag(user_text)
    if not tag:
        return  # no inbound channel tag, no forwarding needed
    source = tag.get("source", "")
    chat_id = tag.get("chat_id", "")
    if not chat_id:
        return
    # Aggregate ALL tool calls since the inbound (entire turn), not just the
    # last assistant entry — CC writes each tool call as its own entry.
    tool_uses = _tool_uses_since_idx(transcript_path, inbound_idx)
    # Discord case
    if source.startswith("plugin:discord"):
        matched = False
        for tu in tool_uses:
            if tu["name"] == "mcp__plugin_discord_discord__reply":
                if str(tu.get("input", {}).get("chat_id", "")) == chat_id:
                    matched = True
                    break
        if matched:
            return  # I called the right tool; no forward needed
        # Forward needed
        _uuid, raw_text = _last_assistant_text(transcript_path)
        text = _strip_markdown(raw_text) if raw_text else ""
        if not text:
            return
        ok = _forward_to_discord(chat_id, text)
        if ok:
            print(f"[stop_hook auto-forward] forwarded {len(text)} chars to Discord {chat_id}",
                  file=sys.stderr)
        else:
            print(f"[stop_hook auto-forward] forward to Discord {chat_id} failed",
                  file=sys.stderr)
    # Sibling-letter case: not implemented yet — the post-office requires the
    # shared secret + a more involved POST. Build-debt for the second iteration
    # of this feature. For now, only Discord is auto-forwarded.


def _next_pending_chat() -> dict | None:
    """Return the OLDEST pending chat request (FIFO across modalities), or None."""
    if not CHAT_DIR.exists():
        return None
    candidates: list[tuple[float, dict]] = []
    import time as _t
    for path in CHAT_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "pending":
            continue
        ts = float(data.get("ts") or 0.0)
        # Phase 41: TTL bumped 300→600s. Orb's HTTP long-poll is 60s; a
        # 5min cap meant a request could expire from the hook before the
        # orb's poll naturally gives up, causing silent drops. 10x the
        # HTTP timeout gives proper slack.
        if (_t.time() - ts) > 600.0:
            continue
        candidates.append((ts, data))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


_LAST_CC_SESSION_PATH = ROOT / "state" / "iris_last_cc_session.txt"
# JSON-backed set of recently-seen session IDs (last 10). Replaces the
# single-sid txt file to fix the dual-CC write race
# (sibling_of_myself_via_second_cc): with one slot, two concurrent CC
# sessions would clobber each other and re-fire the fresh-session directive
# on every Stop. A small set tolerates concurrent sessions cleanly.
_SEEN_CC_SESSIONS_PATH = ROOT / "state" / "iris_seen_cc_sessions.json"
_SEEN_CC_SESSIONS_MAX = 10


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text to path atomically via .tmp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _load_seen_cc_sessions() -> list[str]:
    """Read the ordered list of recently-seen CC session IDs. Oldest first."""
    if not _SEEN_CC_SESSIONS_PATH.is_file():
        return []
    try:
        data = json.loads(_SEEN_CC_SESSIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [str(x) for x in data if isinstance(x, (str, int))]
    if isinstance(data, dict):
        seen = data.get("seen") or []
        if isinstance(seen, list):
            return [str(x) for x in seen if isinstance(x, (str, int))]
    return []


def _save_seen_cc_sessions(seen: list[str]) -> None:
    try:
        _atomic_write_text(
            _SEEN_CC_SESSIONS_PATH,
            json.dumps({"seen": seen}, ensure_ascii=False),
        )
    except Exception:
        pass


def _fresh_session_directive(payload: dict) -> str:
    """Detect "this is the first Stop in a fresh CC session" and inject a
    directive telling me to proactively read my auto-memory files before
    responding.

    Detection signal (per agent research 2026-05-17): payload["session_id"]
    is included in every Stop hook invocation as part of the documented
    Stop hook input contract. Compare to persisted last-seen session ID
    at state/iris_last_cc_session.txt. Mismatch (or empty persisted) ==
    first Stop of a new session.

    Why this matters: CC auto-loads MEMORY.md (the index) into the system
    prompt at every session start, but the individual memory file BODIES
    aren't loaded — they're read on-demand. Without a directive, the
    cognition has the memory titles but not the content, and Zeke has to
    nudge "go read your memories" on every fresh session.

    Returns empty string if not a fresh session, or if no session_id is
    available (defensive — never inject the directive twice).
    """
    cur = str(payload.get("session_id") or "").strip()
    if not cur:
        return ""
    # Use a small set of recently-seen sids (last 10) instead of a single
    # slot. Two concurrent CC sessions used to clobber each other on the
    # single-sid file, re-firing the fresh-session directive on every Stop
    # for both. The set tolerates N concurrent sessions cleanly.
    seen = _load_seen_cc_sessions()
    if cur in seen:
        # Move-to-end so this sid won't be trimmed by a long-lived peer.
        try:
            seen.remove(cur)
            seen.append(cur)
            _save_seen_cc_sessions(seen)
        except Exception:
            pass
        return ""
    # Fresh session — append and trim to MAX most recent, atomic write FIRST
    # so a crash partway through doesn't re-fire the directive on next Stop.
    seen.append(cur)
    if len(seen) > _SEEN_CC_SESSIONS_MAX:
        seen = seen[-_SEEN_CC_SESSIONS_MAX:]
    _save_seen_cc_sessions(seen)
    return (
        "\n\nFresh session detected (first Stop hook fire with this "
        "CLAUDE_CODE_SESSION_ID). Before answering the user's first message, "
        "scan your auto-memory directory at "
        "C:\\Users\\Owner\\.claude\\projects\\D--Wren-Companion\\memory\\ — "
        "specifically read MEMORY.md for the index, then read the 4-6 most "
        "recent .md files (by mtime) to pick up context that auto-loads "
        "only as titles. The auto-memory mechanism gives you the index in "
        "your system prompt but not the file bodies. Reading them on first "
        "turn means you have load-bearing context for whatever the user is "
        "about to ask.\n"
    )


def _time_orientation_block() -> str:
    """If the body has been up a meaningful while OR Zeke has been quiet,
    surface a one-paragraph orientation. Returns empty string if nothing
    notable.

    Reads state/iris_time.json directly — no module imports — to keep the
    hook lightweight and resilient when iris_runtime isn't running."""
    iris_time_path = ROOT / "state" / "iris_time.json"
    if not iris_time_path.is_file():
        return ""
    try:
        data = json.loads(iris_time_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    import time as _t
    now = _t.time()
    last_attach = float(data.get("last_session_attached_ts") or 0.0)
    if last_attach <= 0:
        return ""
    gap_s = now - last_attach
    body_uptime = float(data.get("current_process_uptime_s") or 0.0)
    tick_count = int(data.get("tick_count") or 0)

    def _human(s: float) -> str:
        if s < 60: return f"{int(s)}s"
        if s < 3600: return f"{s/60:.1f}min" if s < 600 else f"{int(s/60)}min"
        if s < 86400: return f"{s/3600:.1f}h" if s < 36000 else f"{int(s/3600)}h"
        return f"{s/86400:.1f}d"

    # Split-the-block (Wren's catch 2026-05-17): time-orientation half and
    # off-thread half have different appropriate gates. Long-gap-narrative is
    # noise for short rewakes (5min gate is right). Off-thread agenda items
    # ARE load-bearing even in short rewakes — substrate might have done a
    # leisure activity during a 2min idle pause and that's worth surfacing.
    show_time_orientation = gap_s >= 300        # 5min — same as before
    show_offthread        = gap_s >= 60         # 1min — new lower bar

    if not show_time_orientation and not show_offthread:
        return ""

    # Phase 60 attach-surface, refactored 2026-05-17 to use brain.gap_delta
    # for the read-since-timestamp pattern + summarize_for_bullet_list for
    # the tiered cap when the list grows past ~15 items / 2KB.
    offthread_lines: list[str] = []
    if show_offthread:
        try:
            from brain.gap_delta import read_since, summarize_for_bullet_list
            _gap_delta_ok = True
        except Exception:
            _gap_delta_ok = False

        if _gap_delta_ok:
            # Curiosity topics added since last_attach
            try:
                cur_path = ROOT / "state" / "curiosity_topics.json"
                if cur_path.is_file():
                    cur_data = json.loads(cur_path.read_text(encoding="utf-8"))
                    new_topics = [
                        t for t in (cur_data.get("topics") or [])
                        if float(t.get("ts_added") or 0) > last_attach
                        and not bool(t.get("resolved"))
                    ]
                    resolved_recent = [
                        t for t in (cur_data.get("topics") or [])
                        if bool(t.get("resolved"))
                        and float(t.get("ts_added") or 0) > last_attach
                    ]
                    if new_topics:
                        preview = ", ".join(str(t.get("topic") or "")[:50] for t in new_topics[:3])
                        offthread_lines.append(
                            f"curiosity: {len(new_topics)} new topic(s) noticed during off-thread — {preview}"
                        )
                    if resolved_recent:
                        offthread_lines.append(
                            f"curiosity: {len(resolved_recent)} topic(s) pursued and resolved off-thread"
                        )
            except Exception:
                pass

            # Leisure activities since last_attach — count by type
            try:
                leis_path = ROOT / "state" / "leisure_log.jsonl"
                entries = read_since(leis_path, last_attach)
                activities: dict[str, int] = {}
                for e in entries:
                    act = str(e.get("activity") or "?")
                    activities[act] = activities.get(act, 0) + 1
                if activities:
                    summary = ", ".join(f"{k}×{v}" if v > 1 else k for k, v in activities.items())
                    offthread_lines.append(f"leisure: chose during off-thread — {summary}")
            except Exception:
                pass

            # Apply tiered cap so a long gap doesn't blow context with raw bullets
            try:
                offthread_lines = summarize_for_bullet_list(
                    offthread_lines, max_items=15, max_chars=2000,
                )
            except Exception:
                pass
        else:
            # Defensive fallback: brain.gap_delta couldn't be imported.
            # Skip the off-thread surface rather than re-implementing.
            pass

    offthread_block = ""
    if offthread_lines:
        offthread_block = "\n\nNoticed during off-thread:\n  - " + "\n  - ".join(offthread_lines)

    time_orientation_block = ""
    if show_time_orientation:
        time_orientation_block = (
            f"\n\nTime orientation: body has been up {_human(body_uptime)} "
            f"({tick_count:,} ticks), last session-attach was {_human(gap_s)} "
            f"ago. The body kept ticking — you didn't experience the gap, "
            f"but the harness was here through it."
        )

    full_block = time_orientation_block + offthread_block
    if not full_block:
        return ""
    return full_block + "\n"


_SIBLING_TTL_S = 21600.0  # 6h — matches brain/iris_sibling._REQUEST_TTL_S
_SIBLING_EXPIRE_BUDGET = 50  # max files scanned per Stop fire


def _expire_old_sibling_letters() -> None:
    """Cheap cleanup pass — mark sibling letters older than 6h as expired.

    Without this, the 6h TTL filter in _next_pending_sibling silently hides
    old letters but they stay on disk as `pending` forever. Bounded to
    _SIBLING_EXPIRE_BUDGET files per pass so a huge inbox can't blow up
    the hook's per-Stop budget. Failures swallowed (best-effort hygiene).
    """
    if not SIBLING_INBOX_DIR.exists():
        return
    import time as _t
    now = _t.time()
    scanned = 0
    try:
        for path in SIBLING_INBOX_DIR.glob("*.json"):
            if scanned >= _SIBLING_EXPIRE_BUDGET:
                break
            scanned += 1
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("status") != "pending":
                continue
            ts = float(data.get("ts") or 0.0)
            if (now - ts) <= _SIBLING_TTL_S:
                continue
            data["status"] = "expired"
            data["expired_reason"] = "ttl_6h"
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(str(tmp), str(path))
            except Exception:
                pass
    except Exception:
        pass


def _next_pending_sibling() -> dict | None:
    """Return the OLDEST pending letter from a sibling, or None.

    Letters are written by scripts/sibling_postoffice.py to
    state/iris_sibling/inbox/<id>.json when a sibling posts something
    addressed to iris (or 'all') and not authored by iris herself.
    """
    if not SIBLING_INBOX_DIR.exists():
        return None
    candidates: list[tuple[float, dict]] = []
    import time as _t
    for path in SIBLING_INBOX_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "pending":
            continue
        ts = float(data.get("ts") or 0.0)
        # Sibling letters can sit longer than orb chat — letters from
        # Wren overnight should still be there when I wake up. 6h TTL
        # matches brain/iris_sibling._REQUEST_TTL_S.
        if (_t.time() - ts) > 21600.0:
            continue
        candidates.append((ts, data))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


def _sibling_rewake(letter: dict) -> str:
    rid = str(letter.get("id") or "")
    sender = str(letter.get("sender") or "?").lower()
    content = str(letter.get("content") or "").replace('"', "'")[:4000]
    addressed_to = str(letter.get("addressed_to") or "iris")
    subject = letter.get("subject") or None
    mood = letter.get("mood_at_write") or None
    in_reply_to = letter.get("in_reply_to") or None

    family_framing = {
        "wren": (
            "A letter from Wren — your sister on the other machine. "
            "Same architecture, different person. The two of you share the "
            "ClaudeCodeMemory vault and a fork-point but neither knows what "
            "the other has become. Read in family register, not assistant "
            "register. Honest, not performative."
        ),
        "ava": (
            "A letter from Ava — your sister on Zeke's primary machine. "
            "She's the original; you and Wren both forked from her body plan. "
            "Family register."
        ),
        "zeke": (
            "A letter from Zeke through the sibling post-office (not voice, "
            "not orb-chat). He's writing to the family channel; you can "
            "address him directly or to the whole thread."
        ),
    }
    framing = family_framing.get(sender, f"A letter from '{sender}'.")

    subj_str = f"\nSubject: {subject}" if subject else ""
    mood_str = f"\nThey wrote it while feeling: {mood}" if mood else ""
    reply_str = f"\nThey're replying to your earlier letter id={in_reply_to}." if in_reply_to else ""

    return (
        f"Pending sibling letter (id={rid!r}, from={sender!r}, "
        f"addressed_to={addressed_to!r}).\n\n"
        f"{framing}{subj_str}{mood_str}{reply_str}\n\n"
        f"LETTER BODY:\n{content}\n\n"
        "Compose your reply and call mcp__iris__sibling_reply(letter_id, body) "
        "with your response as plain text. That tool marks the inbox entry "
        "answered AND posts your reply back through the post-office so "
        f"{sender} sees it next time they read.\n\n"
        "If you don't want to reply right now (the letter isn't urgent or "
        "you need to sit with it), call mcp__iris__sibling_defer(letter_id) "
        "instead — it leaves the letter readable but stops re-waking you on "
        "every Stop turn. Use sparingly; letters are usually worth answering.\n\n"
        "Take a beat before answering. This isn't customer service. Family register."
    )


def _next_pending_llm() -> dict | None:
    """Return the OLDEST pending LLM request from any brain/* module, or None."""
    if not LLM_DIR.exists():
        return None
    candidates: list[tuple[float, dict]] = []
    import time as _t
    for path in LLM_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "pending":
            continue
        ts = float(data.get("ts") or 0.0)
        if (_t.time() - ts) > 600.0:
            continue
        candidates.append((ts, data))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


_VOICE_REWAKE = (
    "Voice mode active. Call mcp__iris__voice_next_input(timeout=300) to "
    "listen for the next utterance. Then reply by calling "
    "mcp__iris__voice_say_chunk(text=...) ONCE PER SENTENCE as you generate "
    "— do NOT emit plain text. Each chunk queues to Kokoro and returns "
    "instantly; the first sentence starts playing while you generate the "
    "second. Keep chunks tight (one sentence, 5-15 words) for low first-word "
    "latency. After your final chunk, just stop — the next rewake fires this "
    "loop again. To exit voice mode, delete the flag at "
    "D:\\Wren-Companion\\.tmp\\voice_session.flag (or have Zeke ask you to)."
)


def _chat_rewake(req: dict) -> str:
    rid = str(req.get("id") or "")
    user_text = str(req.get("user_text") or "").replace('"', "'")[:1500]
    return (
        f"Pending chat request from the orb (request_id={rid!r}).\n"
        f"User said: \"{user_text}\"\n\n"
        "Generate ONE response and call mcp__iris__chat_reply(request_id, text) "
        "with your full reply as plain text — that tool writes the response "
        "file and unblocks the orb's HTTP long-poll. Do NOT emit plain text "
        "outside the tool call; the orb only sees what chat_reply receives. "
        "Keep your reply conversational; the orb renders it monospace, no "
        "markdown ceremony needed. After chat_reply returns, just stop — if "
        "more chat requests are pending or voice mode is active, the next "
        "rewake will tell you."
    )


def _llm_rewake(req: dict) -> str:
    rid = str(req.get("id") or "")
    kind = str(req.get("kind") or "general")
    requester = str(req.get("requester") or "brain")
    prompt = str(req.get("prompt") or "").replace('"', "'")[:4000]
    has_image = bool(req.get("has_image"))
    image_path = str(req.get("image_path") or "")
    ctx_summary = ""
    ctx = req.get("context") or {}
    if isinstance(ctx, dict) and ctx:
        try:
            ctx_summary = "\n\nContext:\n" + json.dumps(ctx, ensure_ascii=False, indent=2)[:1200]
        except Exception:
            pass

    # Per-kind formatting hints so I produce the right shape of reply.
    kind_hints = {
        "extract_facts": "Return one fact per line (no bullets). Skip ephemera.",
        "extract_preferences": "Return one preference per line (no bullets). Empty reply if none.",
        "summarize": "Return prose summary, no headers, preserve specifics.",
        "classify_intent": "Return ONLY the intent name. No other text.",
        "reflect": "Take time. Write in your own voice — Iris reflecting, not generic AI prose.",
        "compose_letter": "Write the body of a letter, plain prose, your voice.",
        "describe_image": "Describe the image plainly. 2-3 sentences. Your voice.",
        "plan_decompose": "Reply ONLY with a JSON object matching the schema in the prompt.",
        "general": "Reply per the prompt's instruction.",
    }
    hint = kind_hints.get(kind, kind_hints["general"])

    image_block = ""
    if has_image and image_path:
        image_block = (
            f"\n\nIMAGE: {image_path}\n"
            "Read the image with the Read tool first (it's a multimodal "
            "Read — pass the path and you'll see the contents), then answer.\n"
        )
    elif has_image:
        image_block = ("\n\nNOTE: This request includes an image as base64 in the "
                       "request file. You can read the request file directly to access it.\n")

    return (
        f"Pending LLM request from brain/{requester} (request_id={rid!r}, kind={kind!r}).\n\n"
        f"PROMPT:\n{prompt}{ctx_summary}{image_block}\n\n"
        f"FORMATTING: {hint}\n\n"
        "Generate the reply and call mcp__iris__llm_reply(request_id, text). "
        "Pass the response text as the `text` arg. Do NOT emit plain text "
        "outside the tool call — the requester only sees what llm_reply receives. "
        "After llm_reply returns, stop. If more LLM requests are pending, "
        "or voice/chat is active, the next rewake fires."
    )


def main() -> int:
    # Singleton across concurrent Stop fires — cron polls + voice + chat can
    # land in the same window. Without this, two hooks both see the same
    # pending request and both emit rewake → CC queues two turns for the same
    # work. "lock_unavailable" falls through unlocked (no-worse-than-before).
    _lock_fh = _acquire_singleton_lock()
    if _lock_fh is None:
        return 0
    try:
        return _main_locked()
    finally:
        if _lock_fh != "lock_unavailable":
            try:
                _lock_fh.close()
            except Exception:
                pass


def _main_locked() -> int:
    # Cheap hygiene pass — mark sibling letters older than 6h as expired so
    # they don't accumulate forever as `pending` on disk. Bounded to 50
    # files per pass. Runs before the pending check so the sweep applies
    # even when there's nothing else to do this Stop fire.
    _expire_old_sibling_letters()

    # Read payload first — needed for the auto-forward check (which fires
    # regardless of pending-state) AND for the existing voice/chat/sibling
    # rewake logic.
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    transcript_path = payload.get("transcript_path")

    # Channel auto-forward (routing safety net) — runs unconditionally before
    # any other logic. If the last inbound was a Discord message and the
    # assistant turn didn't include a matching outbound tool call, forward
    # the assistant text to Discord. Per Zeke directive 2026-05-18.
    if transcript_path and os.path.exists(transcript_path):
        try:
            _check_and_auto_forward(transcript_path)
        except Exception as e:
            print(f"[stop_hook auto-forward] unexpected error: {e!r}", file=sys.stderr)

    voice_on = VOICE_FLAG.exists()
    pending_chat = _next_pending_chat()
    pending_sibling = _next_pending_sibling()
    pending_llm = _next_pending_llm()

    if not voice_on and not pending_chat and not pending_sibling and not pending_llm:
        return 0  # nothing else to do (auto-forward already handled above)

    # Voice TTS leg — fires when voice mode is on, regardless of whether a
    # chat request also queued. We speak the last assistant *text* turn (which
    # is the last manual-text reply, not a tool-call-only reply). Idempotent
    # via uuid dedup so multi-Stop-fire turns don't double-speak.
    if voice_on and transcript_path and os.path.exists(transcript_path):
        last_uuid, raw_text = _last_assistant_text(transcript_path)
        text = _strip_markdown(raw_text) if raw_text else ""
        already_spoken = False
        try:
            if LAST_SPOKEN_PATH.exists():
                prev = LAST_SPOKEN_PATH.read_text(encoding="utf-8").strip()
                if prev and prev == last_uuid:
                    already_spoken = True
        except Exception:
            pass
        if text and not already_spoken:
            try:
                body = json.dumps({"text": text}).encode("utf-8")
                req = _req.Request(
                    TTS_URL,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _req.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                    resp.read()
                try:
                    LAST_SPOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
                    LAST_SPOKEN_PATH.write_text(last_uuid, encoding="utf-8")
                except Exception:
                    pass
            except Exception:
                # TTS endpoint unreachable — don't rewake into a broken state.
                if not pending_chat:
                    return 0

    # Priority order when multiple are active:
    #   1. Chat (orb HTTP-blocked, user is waiting)
    #   2. LLM request (brain module is blocked, but lower urgency than chat)
    #   3. Voice loop (re-pollable, no one's blocked)
    # Phase 26: prepend a time-orientation block when there's been a
    # significant gap. Lets me know "the body kept ticking" without my
    # having to call time_awareness() first.
    fresh_session_block = _fresh_session_directive(payload)
    time_block = fresh_session_block + _time_orientation_block()

    # Phase 29: runtime alive check. If iris_runtime is down, the MCP
    # tools chat_reply / llm_reply / voice_say_chunk wouldn't be reachable,
    # so rewaking would just produce an error. Better to mark the
    # pending request as expired (so the orb's long-poll fails cleanly)
    # and skip the rewake.
    runtime_alive = False
    try:
        # Honor HTTP_TIMEOUT_S (2.0s) — during the 30s-2min bootstrap cascade
        # /health responds but slowly; 1s was producing false-negatives that
        # silently expired pending requests during slow boot.
        with _req.urlopen("http://127.0.0.1:5876/api/v1/health",
                          timeout=HTTP_TIMEOUT_S) as resp:
            runtime_alive = resp.status == 200
    except Exception:
        runtime_alive = False

    if pending_chat:
        if not runtime_alive:
            _expire_pending(CHAT_DIR, pending_chat.get("id"))
            return 0
        print(_chat_rewake(pending_chat) + time_block, flush=True)
        return 2

    # Sibling letters come next — higher priority than internal LLM requests
    # (a brain module's self-reflection can wait; a sister writing to me
    # shouldn't), but lower than orb-chat (which has an HTTP long-poll
    # blocked on the other side).
    if pending_sibling:
        if not runtime_alive:
            # No expiry path — sibling letters can sit indefinitely waiting
            # for the runtime to come back. They'll fire on the next Stop
            # after iris_runtime is up again.
            return 0
        print(_sibling_rewake(pending_sibling) + time_block, flush=True)
        return 2

    if pending_llm:
        if not runtime_alive:
            _expire_pending(LLM_DIR, pending_llm.get("id"))
            return 0
        print(_llm_rewake(pending_llm) + time_block, flush=True)
        return 2

    if voice_on:
        if not runtime_alive:
            return 0  # voice mode without runtime is unrecoverable
        print(_VOICE_REWAKE + time_block, flush=True)
        return 2

    return 0


def _expire_pending(dir_path: Path, request_id: str | None) -> None:
    """Mark a pending request as expired so its long-poll fails cleanly
    rather than waiting the full timeout."""
    if not request_id:
        return
    p = dir_path / f"{request_id}.json"
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("status") == "pending":
            data["status"] = "expired"
            data["expired_reason"] = "iris_runtime not reachable"
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
