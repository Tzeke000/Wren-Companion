"""Extra read-only routes the Iris app needs (2026-10-06, Zeke: "your tool tab says 0 tools … add a vector one
for your physical body … whatever else needs to show in the tabs").

One source of truth for both activation paths:
  * brain.orb_http.start() calls install() at boot (lands at the next stack start);
  * the `app_routes` tool calls install() on the RUNNING server now — add_api_route on the served app, never a
    reload of brain.orb_http (a reload wipes its bound globals: the 2026-07-08 scar).
install() is idempotent: routes already present are skipped.

Routes
  GET /api/v1/tools          the live tool registry (name, description, tier) + last-used stats
  GET /api/v1/vector/status  my Vector body: battery, nerves, possession, senses, room-map summary, file ages
  GET /api/v1/vector/frame   the latest Vector camera frame (jpeg, no-cache)
  GET /api/v1/app/scene      newest scene caption from state/scene_memory/keyframes.jsonl ({text, ts})
  GET /api/v1/app/learning   last 100 learning_log rows (newest first) + unresolved curiosity topics
  GET /api/v1/app/people     one entry per person card in profiles/ (name, relation, summary, face samples)
  GET /api/v1/app/proposals  pending identity proposals (state/identity_proposals.jsonl; [] when absent)
  GET /api/v1/app/brains     the host model (Claude) + the little brain's parked state

The /api/v1/app/* paths are NEW on purpose: the old Ava-era routes in orb_http are registered first and
would shadow anything re-added at the same path. Every handler is read-only, tolerates missing files and
reads big jsonl files from the END (keyframes.jsonl is MBs and growing).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any


def _read_json(p: Path) -> tuple[Any, float | None]:
    try:
        return json.loads(p.read_text(encoding="utf-8")), round(time.time() - p.stat().st_mtime, 1)
    except Exception:
        return None, None


def _tail_lines(p: Path, n: int, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """The last `n` complete lines of a text file, read backwards in blocks — never the whole file.
    Stops at `max_bytes` read so a pathological file can't stall a request."""
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf = b""
            read = 0
            while pos > 0 and buf.count(b"\n") <= n and read < max_bytes:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                read += step
        lines = buf.decode("utf-8", errors="replace").splitlines()
        if pos > 0 and lines:
            lines = lines[1:]  # first line is partial when we stopped mid-file
        return [ln for ln in lines if ln.strip()][-n:]
    except Exception:
        return []


def _jsonl_tail(p: Path, n: int, max_bytes: int = 4 * 1024 * 1024) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ln in _tail_lines(p, n, max_bytes):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict):
            rows.append(d)
    return rows


# ── scene ─────────────────────────────────────────────────────────────────────────────────────────────
def scene_latest(root: Path) -> dict[str, Any]:
    """Newest keyframe caption. keyframes.jsonl holds a base row per keyframe (id, ts, sensors, words=null)
    and later `_update` rows (id, words, caption_status) that carry the caption — so the caption row often
    has no ts; take it from the base row with the same id, else from the id (YYYYMMDD_HHMMSS_reason)."""
    p = root / "state" / "scene_memory" / "keyframes.jsonl"
    for n in (300, 3000):  # widen once if the recent tail has no caption at all
        rows = _jsonl_tail(p, n)
        for i in range(len(rows) - 1, -1, -1):
            words = rows[i].get("words")
            if not (isinstance(words, str) and words.strip()):
                continue
            kid = rows[i].get("id")
            ts = rows[i].get("ts")
            if not isinstance(ts, (int, float)):
                ts = next((r.get("ts") for r in rows if r.get("id") == kid and isinstance(r.get("ts"), (int, float))),
                          None)
            if ts is None and isinstance(kid, str):
                try:
                    ts = _dt.datetime.strptime(kid[:15], "%Y%m%d_%H%M%S").timestamp()
                except Exception:
                    ts = None
            return {"ok": True, "text": words.strip(), "ts": ts, "id": kid}
    return {"ok": True, "text": "", "ts": None, "id": None}


# ── learning ──────────────────────────────────────────────────────────────────────────────────────────
def learning(root: Path) -> dict[str, Any]:
    entries = _jsonl_tail(root / "state" / "learning_log.jsonl", 100)
    entries.reverse()  # newest first
    gaps: list[dict[str, Any]] = []
    topics, _ = _read_json(root / "state" / "curiosity_topics.json")
    items = topics.get("topics") if isinstance(topics, dict) else topics
    if isinstance(items, list):
        for t in items:
            if not isinstance(t, dict) or t.get("resolved"):
                continue
            topic = str(t.get("topic") or "").strip()
            if not topic:
                continue
            gap: dict[str, Any] = {"topic": topic}
            note = t.get("note")
            if not (isinstance(note, str) and note.strip()):
                sb = t.get("sparked_by")
                note = sb if isinstance(sb, str) and " " in sb.strip() else None  # skip tokens like manual_add
            if isinstance(note, str) and note.strip():
                gap["note"] = note.strip()
            gaps.append(gap)
    return {"ok": True, "entries": entries, "gaps": gaps}


# ── people ────────────────────────────────────────────────────────────────────────────────────────────
_PERSON_TYPES = {"person", "people", "ai_peer"}
_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _strip_md(s: str) -> str:
    s = re.sub(r"\*+|__|`", "", s)
    s = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), s)  # [[wikilink|alias]]
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)  # [text](url)
    return s.strip()


def _parse_card(text: str) -> tuple[dict[str, str], list[str]]:
    """(front-matter fields, body lines). Front matter = a leading '---' block of `key: value` lines;
    trailing '   # comment' on a value is stripped (cards annotate fields that way)."""
    lines = text.splitlines()
    fm: dict[str, str] = {}
    body = lines
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                for ln in lines[1:j]:
                    m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", ln)
                    if m:
                        fm[m.group(1).lower()] = re.sub(r"\s+#\s.*$", "", m.group(2)).strip()
                body = lines[j + 1:]
                break
    return fm, body


def _face_count(root: Path, *ids: str) -> int | None:
    for fid in ids:
        if not fid or not re.fullmatch(r"[\w.-]+", fid):
            continue
        d = root / "faces" / fid
        if d.is_dir():
            try:
                return sum(1 for f in d.iterdir() if f.is_file() and f.suffix.lower() in _IMG_EXT)
            except Exception:
                return None
    return None


def people(root: Path) -> dict[str, Any]:
    """One entry per profiles/<id>/ card that is a person. Cards declare `type:` in front matter; things
    (hardware, systems, projects, games…) are skipped. An untyped card counts as a person only if it has
    an enrolled face dir — otherwise it's indistinguishable from the untyped THING cards (blender, games)."""
    out: list[dict[str, Any]] = []
    pdir = root / "profiles"
    try:
        dirs = sorted(d for d in pdir.iterdir() if d.is_dir() and not d.name.startswith(("_", ".")))
    except Exception:
        dirs = []
    for d in dirs:
        pid = d.name
        card = d / f"{pid}.md"
        if not card.is_file():
            try:
                mds = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".md")
            except Exception:
                mds = []
            if not mds:
                continue
            card = mds[0]
        try:
            with open(card, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(16384)  # the header is all we need; some cards run to 100s of KB
        except Exception:
            continue
        fm, body = _parse_card(text)
        ctype = fm.get("type", "").lower()
        face_id = fm.get("face_id", "")
        faces = _face_count(root, pid, face_id)
        if ctype:
            if ctype not in _PERSON_TYPES:
                continue
        elif faces is None:
            continue
        heading = next((ln[2:].strip() for ln in body if ln.startswith("# ")), "")
        name = _strip_md(fm.get("name") or heading or pid)
        name = re.split(r"\s+[—–-]\s+", name, maxsplit=1)[0].strip()[:80] or pid  # "Name — how Zeke knows them"
        rel = _strip_md(fm.get("relationship") or fm.get("relation") or "") or None
        # First prose paragraph (cards are hard-wrapped, so join its continuation lines; a new list
        # item, heading or blank line ends it).
        parts: list[str] = []
        for ln in body:
            s = ln.strip()
            skip = (not s or s.startswith("#") or s.startswith("---") or s.startswith("|")
                    or s.startswith("<!--"))
            if parts and (skip or re.match(r"^[-*>]\s+", s)):
                break
            if skip:
                continue
            s = _strip_md(re.sub(r"^[-*>]\s+", "", s)).strip("*_ ")
            if s:
                parts.append(s)
            if sum(len(x) for x in parts) > 200:
                break
        joined = " ".join(parts)
        summary = joined[:200] + ("…" if len(joined) > 200 else "")
        out.append({"id": pid, "name": name, "relationship": rel, "summary": summary or None,
                    "face_samples": faces, "type": ctype or None})
    return {"ok": True, "people": out}


# ── identity proposals ────────────────────────────────────────────────────────────────────────────────
def proposals(root: Path) -> dict[str, Any]:
    p = root / "state" / "identity_proposals.jsonl"
    rows = [r for r in _jsonl_tail(p, 200) if not r.get("approved")] if p.is_file() else []
    rows.reverse()  # newest first
    ext = root / "state" / "identity_extensions.md"
    extensions = None
    if ext.is_file():
        try:
            with open(ext, "r", encoding="utf-8", errors="replace") as f:
                extensions = f.read(20000)
        except Exception:
            extensions = None
    return {"ok": True, "proposals": rows, "extensions": extensions}


# ── brains ────────────────────────────────────────────────────────────────────────────────────────────
def _host_model() -> str | None:
    """Same lookup the snapshot uses (orb_http._live_claude_model: IRIS_MODEL env → live claude.exe
    --model, cached 60s). Read from the ALREADY-LOADED module — never import/reload orb_http here."""
    m = sys.modules.get("brain.orb_http")
    fn = getattr(m, "_live_claude_model", None) if m else None
    if callable(fn):
        try:
            v = str(fn() or "").strip()
            return v if v and v != "claude" else None
        except Exception:
            pass
    return (os.environ.get("IRIS_MODEL") or "").strip() or None


def _port_open(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def brains(root: Path) -> dict[str, Any]:
    flag = root / "state" / "little_brain" / "pilot_deliberately_off.json"
    flag_data, _ = _read_json(flag)
    parked = flag.is_file() and not (isinstance(flag_data, dict) and flag_data.get("off") is False)
    reason = flag_data.get("reason") if isinstance(flag_data, dict) else None
    model = _host_model()
    return {
        "ok": True,
        "host": {
            "model": model,
            "provider": "Claude (Anthropic)",
            "runs_on": "the tower (Claude Code host)",
            "note": "My cognition. brain/* modules that need an LLM route through brain.iris_llm to me.",
        },
        "little_brain": {
            "parked": parked,
            "reason": reason if isinstance(reason, str) else None,
            "endpoint": "http://127.0.0.1:8772",
            "listening": _port_open(8772),
            "note": "A small local model, parked while the flag file exists.",
        },
        "notes": [
            "The host model is pinned by the launcher; it can't be switched from this app.",
            "The Ollama 'ava-personal' models belong to the retired local-model setup.",
        ],
    }


def tools_listing(g: dict[str, Any]) -> dict[str, Any]:
    from tools import tool_registry as tr
    with tr._REGISTRY_LOCK:
        tools = [{"name": d.name, "description": d.description, "tier": d.tier}
                 for d in sorted(tr._REGISTRY.values(), key=lambda x: x.name)]
    return {
        "ok": True,
        "count": len(tools),
        "tools": tools,
        "last_tool_used": g.get("_desktop_last_tool_used") or "",
        "last_tool_result": str(g.get("_desktop_last_tool_result") or "")[:300],
        "execution_count": int(g.get("_desktop_tool_execution_count") or 0),
    }


def vector_status(root: Path) -> dict[str, Any]:
    vd = root / "state" / "vector"
    battery, battery_age = _read_json(vd / "battery.json")
    nerves, nerves_age = _read_json(vd / "nerves.json")
    possession, _ = _read_json(vd / "possession_status.json")
    intero, _ = _read_json(vd / "interoception.json")
    senses, senses_age = _read_json(vd / "senses_live.json")
    room, room_age = _read_json(vd / "room_map.json")
    frame = vd / "latest_frame.jpg"
    frame_age = round(time.time() - frame.stat().st_mtime, 1) if frame.is_file() else None
    b = battery if isinstance(battery, dict) else {}
    n = nerves if isinstance(nerves, dict) else {}
    s = senses if isinstance(senses, dict) else {}
    i = intero if isinstance(intero, dict) else {}
    r = room if isinstance(room, dict) else {}
    temp = i.get("temp_c")
    return {
        "ok": True,
        "battery": {"level": b.get("level"), "on_charger": b.get("on_charger"), "ok": b.get("ok"), "age_s": battery_age},
        "nerves": {k: n.get(k) for k in ("cliff", "picked_up", "touched", "falling", "on_charger", "prox_mm",
                                         "prox_clear", "charger_seen", "heading_deg")} | {"age_s": nerves_age},
        "held_by_me": bool((possession or {}).get("held")) if isinstance(possession, dict) else None,
        "wifi_dbm": i.get("wifi_dbm"),
        # 125 °C is the sensor's saturated/bogus value, not a real reading — don't show it as fact.
        "temp_c": temp if isinstance(temp, (int, float)) and temp < 100 else None,
        "senses": {"hz": s.get("hz"), "active": s.get("active"), "expression": s.get("expression"),
                   "heard": s.get("heard"), "spoke": s.get("spoke"), "age_s": senses_age},
        "room_map": {"cells": r.get("n_cells"), "pose": r.get("pose"), "charger": r.get("charger"), "age_s": room_age},
        "frame_age_s": frame_age,
    }


def install(app: Any, g: dict[str, Any], root: Path) -> list[str]:
    from fastapi.responses import FileResponse, JSONResponse

    existing = {getattr(r, "path", None) for r in app.routes}
    added: list[str] = []

    def _tools() -> dict[str, Any]:
        return tools_listing(g)

    def _vector_status() -> dict[str, Any]:
        return vector_status(root)

    def _vector_frame():
        p = root / "state" / "vector" / "latest_frame.jpg"
        if not p.is_file():
            return JSONResponse({"ok": False, "error": "no Vector frame yet"}, status_code=404)
        return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def _scene() -> dict[str, Any]:
        return scene_latest(root)

    def _learning() -> dict[str, Any]:
        return learning(root)

    def _people() -> dict[str, Any]:
        return people(root)

    def _proposals() -> dict[str, Any]:
        return proposals(root)

    def _brains() -> dict[str, Any]:
        return brains(root)

    for path, fn in (("/api/v1/tools", _tools), ("/api/v1/vector/status", _vector_status),
                     ("/api/v1/vector/frame", _vector_frame),
                     ("/api/v1/app/scene", _scene), ("/api/v1/app/learning", _learning),
                     ("/api/v1/app/people", _people), ("/api/v1/app/proposals", _proposals),
                     ("/api/v1/app/brains", _brains)):
        if path in existing:
            continue
        app.add_api_route(path, fn, methods=["GET"])
        added.append(path)
    # 2026-10-07 Jarvis tools (weather/maps/reminders/pc/bridge) — own module, same idempotent install.
    try:
        import importlib
        from brain import app_jarvis_routes as _jr
        _jr = importlib.reload(_jr)  # stateless module: reload is safe and lets live installs pick up edits
        have = {f"{m} {getattr(r, 'path', '')}" for r in app.routes for m in (getattr(r, "methods", None) or [])}
        added += _jr.install(app, g, root, have)
    except Exception as e:  # noqa: BLE001
        added.append(f"jarvis routes FAILED: {e!r}"[:200])
    return added
