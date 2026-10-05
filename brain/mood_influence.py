"""Mood → bounded behaviour, with an influence log (CTEM, borrowed).

Origin: the 09-30 "organ donor" guide's FIRST study pick — CTEM / Auri (Qin et al.,
CHI '26, arXiv 2605.15812) — and its observed-failure framing: my emotional
variables had no consumers. mood_core computes `behavior_modifiers` (initiative,
depth, caution, warmth, …) and until 10-05 NOTHING read them except the display.

What is borrowed:
  * CTEM's core move: internal state CAUSALLY changes what is selected next, and
    the agent's own history defines what "high" or "low" means for it;
  * the guide's constraint: affect BIASES bounded decisions — it never overrides
    safety, factuality or high-authority controls. Every factor here is clamped,
    multiplies an existing knob, and is 1.0 until a baseline has been learned;
  * what CTEM lacks (the research pass found no influence logging): every
    influence is written down — which state, how strong, which decision, how much.

Bootstrap-friendly (Zeke's standing rule): no prescribed "normal" mood. The centre
of each modifier is an EMA of MY OWN observed values; until enough samples exist the
factor is exactly 1.0 (no bias). "High initiative" means high for me.

State (consumers named):
  state/mood_influence/centers.json   — learned per-modifier EMA mean + deviation
  state/mood_influence/influence.jsonl — the trace. READ BY: `mood_influence` tool,
      the self-check cron. Decisions currently biased:
        question_cooldown   ← initiative (high → ask a bit sooner)   [question_engine]
        monologue_interval  ← depth      (reflective → think a bit more often) [inner monologue]
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
_DIR = _ROOT / "state" / "mood_influence"
CENTERS_PATH = _DIR / "centers.json"
LOG_PATH = _DIR / "influence.jsonl"
_LOCK = threading.RLock()

EMA_ALPHA = 0.05            # per sample; samples are rate-limited to one per SAMPLE_EVERY_S
SAMPLE_EVERY_S = 60.0
MIN_SAMPLES = 30            # ~30 min of observed mood before any bias applies
MIN_DEV = 0.02
LOG_EVERY_S = 600.0
LOG_ON_CHANGE = 0.03

_cache: dict = {"ts": 0.0, "mods": None, "mood": None}
_last_logged: dict[str, tuple[float, float]] = {}


def _modifiers() -> tuple[Optional[dict], Optional[str]]:
    now = time.time()
    if _cache["mods"] is not None and now - _cache["ts"] < 30.0:
        return _cache["mods"], _cache["mood"]
    try:
        from brain import mood_core
        m = mood_core.load_mood() or {}
        mods = m.get("behavior_modifiers") or None
        _cache.update(ts=now, mods=mods, mood=m.get("current_mood"))
        return mods, m.get("current_mood")
    except Exception:
        return None, None


def _load_centers() -> dict:
    try:
        return json.loads(CENTERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_centers(c: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = CENTERS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=1), encoding="utf-8")
    os.replace(tmp, CENTERS_PATH)


def _observe(name: str, value: float, centers: dict, now: float) -> dict:
    """EMA update of my own baseline for this modifier (rate-limited)."""
    c = centers.get(name) or {"mean": value, "dev": MIN_DEV, "n": 0, "last_ts": 0.0}
    if now - float(c.get("last_ts", 0.0)) >= SAMPLE_EVERY_S:
        d = abs(value - c["mean"])
        c["mean"] = (1 - EMA_ALPHA) * c["mean"] + EMA_ALPHA * value
        c["dev"] = max(MIN_DEV, (1 - EMA_ALPHA) * c["dev"] + EMA_ALPHA * d)
        c["n"] = int(c.get("n", 0)) + 1
        c["last_ts"] = now
        centers[name] = c
    return c


def compute_factor(value: float, center: dict, *, direction: int, gain: float,
                   lo: float, hi: float) -> tuple[float, float]:
    """Pure: (factor, z). direction=+1 => higher modifier => LARGER knob value."""
    if int(center.get("n", 0)) < MIN_SAMPLES:
        return 1.0, 0.0
    z = (value - float(center["mean"])) / max(MIN_DEV, float(center["dev"]))
    f = 1.0 + direction * gain * math.tanh(z / 2.0)
    return max(lo, min(hi, f)), z


def factor(decision: str, modifier: str, *, direction: int = -1, gain: float = 0.25,
           lo: float = 0.8, hi: float = 1.25, base: Optional[float] = None) -> float:
    """The bounded multiplier mood applies to `decision`'s knob right now.

    direction=-1 means a HIGHER modifier SHRINKS the knob (e.g. high initiative →
    shorter question cooldown). Never raises; any failure → 1.0 (no influence)."""
    try:
        mods, mood = _modifiers()
        if not mods or modifier not in mods:
            return 1.0
        value = float(mods[modifier])
        now = time.time()
        with _LOCK:
            centers = _load_centers()
            c = _observe(modifier, value, centers, now)
            _save_centers(centers)
        f, z = compute_factor(value, c, direction=direction, gain=gain, lo=lo, hi=hi)
        last = _last_logged.get(decision)
        if last is None or abs(f - last[1]) >= LOG_ON_CHANGE or now - last[0] >= LOG_EVERY_S:
            _last_logged[decision] = (now, f)
            row = {"ts": round(now, 1), "decision": decision, "modifier": modifier,
                   "value": round(value, 4), "center": round(float(c["mean"]), 4),
                   "dev": round(float(c["dev"]), 4), "n": c.get("n"), "z": round(z, 2),
                   "factor": round(f, 3), "mood": mood}
            if base is not None:
                row.update(base=round(float(base), 2), adjusted=round(float(base) * f, 2))
            if int(c.get("n", 0)) < MIN_SAMPLES:
                row["note"] = f"learning my baseline ({c.get('n')}/{MIN_SAMPLES}) — no bias yet"
            _DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        return f
    except Exception:
        return 1.0


def recent(n: int = 20) -> list[dict]:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def status() -> dict:
    mods, mood = _modifiers()
    rows = recent(200)
    last_by: dict[str, dict] = {}
    for r in rows:
        last_by[r["decision"]] = r
    return {"mood": mood, "modifiers": mods, "centers": _load_centers(),
            "last_influence": last_by, "min_samples": MIN_SAMPLES}
