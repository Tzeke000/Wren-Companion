"""Skill library — verified recipes I can find again (Voyager, borrowed).

Origin: part 5 of the Zeke+Vale research plan (Zeke 10-05: "go for it").
Voyager (MineDojo/Voyager, MIT): a skill is CODE + a one-line DESCRIPTION; the
description is embedded and retrieved top-k (k=5) for a new task; a skill enters the
library ONLY after a critic says the attempt succeeded; re-adding a name makes a new
version (nameV2) instead of overwriting.

The observed failure: my recovery recipes live scattered across memory notes and
cards (the Vector wedge ladder, the :5876 listener recovery, the DHCP ARP sweep, the
PTZ resync). Each time I need one I reconstruct it from prose — and reconstructions
drift. Today (10-05) I restarted the Vector daemon ALONE first, when the card's
ladder said step 2 comes first.

Adaptation:
  * the CRITIC is the action ledger (brain/action_ledger.py): a recipe is `verified`
    only when it cites a ledger action that closed `success` WITH evidence. Without
    one it is `provisional` and `find` hides it unless asked (Voyager-strict);
  * a provisional recipe is PROMOTED by its first successful use (record_use);
  * two failed uses in a row => `needs_revision` (the recipe stopped matching reality);
  * steps are text (tool calls / commands) — I execute them, the library remembers.

Consumer: action_ledger.open_action() calls find(intent) and hands the top verified
recipes back with the past lessons — successes and failures arrive together, at the
moment I start that kind of thing again.

State: state/skill_library/library.json, state/skill_library/emb.json (description
vectors, MiniLM via chromadb's bundled ONNX on CPU; lexical fallback if unavailable).
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
_DIR = _ROOT / "state" / "skill_library"
LIB_PATH = _DIR / "library.json"
EMB_PATH = _DIR / "emb.json"
_LOCK = threading.RLock()
TOP_K = 5
_embedder = None
_embedder_failed = False


# ── storage ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        d = json.loads(LIB_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, LIB_PATH)


def _load_emb() -> dict:
    try:
        return json.loads(EMB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_emb(e: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = EMB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(e), encoding="utf-8")
    os.replace(tmp, EMB_PATH)


# ── similarity ──────────────────────────────────────────────────────────────

def _embed(texts: list[str]) -> Optional[list[list[float]]]:
    """MiniLM sentence vectors on CPU, or None (callers fall back to lexical)."""
    global _embedder, _embedder_failed
    if _embedder_failed:
        return None
    try:
        if _embedder is None:
            from chromadb.utils import embedding_functions as ef
            try:
                _embedder = ef.ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])
            except TypeError:
                _embedder = ef.DefaultEmbeddingFunction()
        return [list(map(float, v)) for v in _embedder(texts)]
    except Exception:
        _embedder_failed = True
        return None


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)


_WORD = re.compile(r"[a-z0-9]+")


def _lexical(q: str, d: str) -> float:
    qa, da = set(_WORD.findall(q.lower())), set(_WORD.findall(d.lower()))
    if not qa or not da:
        return 0.0
    return len(qa & da) / math.sqrt(len(qa) * len(da))


# ── public API ──────────────────────────────────────────────────────────────

def _ledger_success(action_id: str) -> Optional[dict]:
    try:
        from brain import action_ledger as al
        row = al._load().get(str(action_id))
        if row and row.get("outcome") == "success" and row.get("evidence"):
            return row
    except Exception:
        pass
    return None


def add(name: str, description: str, steps: list[str] | str, *,
        verified_by: Optional[str] = None, source: str = "iris",
        notes: str = "") -> dict:
    """Add a recipe (or a new version of one). `description` = ONE line saying what it
    achieves and when to use it (that is what gets embedded). `verified_by` = a ledger
    action id that closed success with evidence; without it the recipe is provisional."""
    name = re.sub(r"[^a-z0-9_]+", "_", str(name).strip().lower()).strip("_")
    if not name:
        raise ValueError("name required")
    description = " ".join(str(description or "").split())
    if not description:
        raise ValueError("a one-line description is required (it is what retrieval matches)")
    if isinstance(steps, str):
        steps = [s for s in steps.splitlines() if s.strip()]
    if not steps:
        raise ValueError("steps required")
    proof = _ledger_success(verified_by) if verified_by else None
    if verified_by and proof is None:
        raise ValueError(f"ledger action {verified_by} did not close success with evidence — "
                         f"add without verified_by (provisional) or cite a real success")
    with _LOCK:
        lib = _load()
        sk = lib.get(name) or {"name": name, "versions": [], "uses": []}
        v = len(sk["versions"]) + 1
        sk["versions"].append({"v": v, "description": description, "steps": list(steps),
                               "notes": notes, "created_ts": time.time(), "source": source,
                               "verified_by": [verified_by] if proof else []})
        sk["current_v"] = v
        sk["status"] = "verified" if proof else "provisional"
        lib[name] = sk
        _save(lib)
        vec = _embed([description])
        if vec:
            e = _load_emb()
            e[f"{name}@{v}"] = vec[0]
            _save_emb(e)
    return {"ok": True, "name": name, "version": v, "status": sk["status"]}


def _current(sk: dict) -> dict:
    v = int(sk.get("current_v") or len(sk["versions"]))
    return sk["versions"][v - 1]


def find(query: str, k: int = TOP_K, include_provisional: bool = False,
         min_score: float = 0.25) -> list[dict]:
    """Top-k recipes for a task, by description similarity (Voyager: k=5)."""
    lib = _load()
    cands = [sk for sk in lib.values()
             if include_provisional or sk.get("status") == "verified"]
    if not cands or not str(query or "").strip():
        return []
    qv = _embed([query])
    emb = _load_emb() if qv else {}
    scored = []
    for sk in cands:
        cur = _current(sk)
        key = f"{sk['name']}@{cur['v']}"
        if qv and key in emb:
            s = _cos(qv[0], emb[key])
        else:
            s = _lexical(query, cur["description"] + " " + sk["name"].replace("_", " "))
        scored.append((s, sk, cur))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for s, sk, cur in scored[:k]:
        if s < min_score:
            continue
        out.append({"name": sk["name"], "score": round(s, 3), "status": sk.get("status"),
                    "version": cur["v"], "description": cur["description"],
                    "steps": cur["steps"], "uses": len(sk.get("uses", []))})
    return out


def get(name: str) -> Optional[dict]:
    sk = _load().get(name)
    if not sk:
        return None
    out = dict(sk)
    out["current"] = _current(sk)
    return out


def record_use(name: str, action_id: str) -> dict:
    """Link a use of this recipe to a ledger action and let its OUTCOME judge the recipe:
    success promotes a provisional recipe; two failures in a row flag needs_revision."""
    try:
        from brain import action_ledger as al
        row = al._load().get(str(action_id))
    except Exception:
        row = None
    if row is None:
        raise KeyError(f"no ledger action {action_id}")
    if row.get("status") == "open":
        raise ValueError("that action is still open — record the use after it closes")
    with _LOCK:
        lib = _load()
        sk = lib.get(name)
        if sk is None:
            raise KeyError(f"no recipe {name}")
        sk.setdefault("uses", []).append({"ts": time.time(), "action_id": action_id,
                                          "outcome": row.get("outcome"), "v": sk.get("current_v")})
        cur = _current(sk)
        if row.get("outcome") == "success" and row.get("evidence"):
            if action_id not in cur["verified_by"]:
                cur["verified_by"].append(action_id)
            sk["status"] = "verified"
        recent = [u["outcome"] for u in sk["uses"][-2:]]
        if len(recent) == 2 and all(o in ("failure", "impossible") for o in recent):
            sk["status"] = "needs_revision"
        lib[name] = sk
        _save(lib)
        return {"ok": True, "name": name, "status": sk["status"], "uses": len(sk["uses"])}


def list_all() -> list[dict]:
    out = []
    for sk in _load().values():
        cur = _current(sk)
        out.append({"name": sk["name"], "status": sk.get("status"), "version": cur["v"],
                    "description": cur["description"], "uses": len(sk.get("uses", [])),
                    "verified_by": cur.get("verified_by")})
    return sorted(out, key=lambda r: r["name"])
