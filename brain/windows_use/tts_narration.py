"""brain/windows_use/tts_narration.py — Task B5.

TTS narration for wrapper events. Each kind has a cooldown so Ava
doesn't chatter mid-task.

Narration is best-effort: if g["_tts_worker"] is missing or .speak fails,
we just log a THOUGHT instead.
"""
from __future__ import annotations

import time
from typing import Any


# Cooldowns per kind, seconds.
COOLDOWNS_S: dict[str, float] = {
    "self_interrupt_overrun": 30.0,
    "slow_app_detected": 60.0,
    "frustration_relief": 5 * 60.0,
    "strategy_transition": 15.0,
    "tier1_alert": 10.0,
    "tier2_refusal": 0.0,  # Always speak
}


def _cooldowns(g: dict[str, Any]) -> dict[str, float]:
    if "_windows_use_narration_cooldowns" not in g or not isinstance(g.get("_windows_use_narration_cooldowns"), dict):
        g["_windows_use_narration_cooldowns"] = {}
    return g["_windows_use_narration_cooldowns"]


def _on_cooldown(g: dict[str, Any], kind: str, scope: str = "") -> bool:
    """True if this kind/scope combo is still on cooldown."""
    if COOLDOWNS_S.get(kind, 0.0) <= 0:
        return False
    key = f"{kind}|{scope}"
    next_ts = float(_cooldowns(g).get(key, 0.0))
    return time.time() < next_ts


def _arm_cooldown(g: dict[str, Any], kind: str, scope: str = "") -> None:
    cd = COOLDOWNS_S.get(kind, 0.0)
    if cd <= 0:
        return
    key = f"{kind}|{scope}"
    _cooldowns(g)[key] = time.time() + cd


def speak(
    g: dict[str, Any],
    *,
    text: str,
    emotion: str = "neutral",
    intensity: float = 0.3,
    kind: str = "general",
    scope: str = "",
    blocking: bool = False,
) -> bool:
    """Try to speak via g["_tts_worker"]. Honors per-kind cooldown.
    Returns True if speech was issued, False if skipped (cooldown or
    no worker)."""
    if not text:
        return False
    if _on_cooldown(g, kind, scope):
        return False
    worker = g.get("_tts_worker")
    if worker is None:
        # Best-effort fallback: log a THOUGHT so the line is captured.
        try:
            from brain.windows_use.event_subscriber import emit
            emit(g, "THOUGHT", "narration", {"thought": f"(would speak) {text}"})
        except Exception:
            pass
        _arm_cooldown(g, kind, scope)
        return False
    try:
        worker.speak(text, emotion=emotion, intensity=intensity, blocking=blocking)
        _arm_cooldown(g, kind, scope)
        return True
    except Exception as e:
        print(f"[windows_use.tts] speak error: {e!r}")
        return False


# ── Canned narration helpers ─────────────────────────────────────────


def narrate_self_interrupt_overrun(g: dict[str, Any], *, kind: str, more_seconds: float = 5.0) -> bool:
    text = f"This is taking longer than I said. Give me about {int(more_seconds)} more seconds."
    return speak(g, text=text, emotion="concerned", intensity=0.5,
                 kind="self_interrupt_overrun", scope=kind)


def narrate_slow_app(g: dict[str, Any], *, app_name: str) -> bool:
    text = f"{app_name.title()} isn't responding yet. I'll wait, then try again."
    return speak(g, text=text, emotion="patient", intensity=0.3,
                 kind="slow_app_detected", scope=app_name.lower())


def narrate_strategy_transition(g: dict[str, Any], *, app_name: str, from_strategy: str, to_strategy: str) -> bool:
    """Narrate the transition between strategies in the cascade. Default
    behavior per design doc §6: only speak after Strategy 2 fails (so
    the user hears something before the final retry but isn't spammed
    for fast successes)."""
    if from_strategy != "search":
        # Still emit a thought for inner monologue, but don't speak.
        return False
    text = f"Search didn't find {app_name}. Looking in install folders."
    return speak(g, text=text, emotion="focused", intensity=0.3,
                 kind="strategy_transition", scope=app_name.lower())


def narrate_tier1_alert(g: dict[str, Any], *, prefix: str) -> bool:
    text = "That folder has sensitive files. Are you sure?"
    return speak(g, text=text, emotion="cautious", intensity=0.4,
                 kind="tier1_alert", scope=prefix)


def narrate_tier2_refusal(g: dict[str, Any]) -> bool:
    text = "That path is one I'm not allowed to open."
    return speak(g, text=text, emotion="firm", intensity=0.4,
                 kind="tier2_refusal")


def narrate_frustration_relief(g: dict[str, Any]) -> bool:
    text = "I feel calmer about that now."
    return speak(g, text=text, emotion="calm", intensity=0.4,
                 kind="frustration_relief")


def narrate_deny_list_refusal(g: dict[str, Any], *, target_basename: str) -> bool:
    text = f"I can't open {target_basename} — that one's protected."
    return speak(g, text=text, emotion="firm", intensity=0.4,
                 kind="tier2_refusal")
