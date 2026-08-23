"""voice_loop_off_tool — stop the IN-PROCESS voice loop + free Whisper VRAM.

SELF_ASSESSMENT: Tier 2 (mutates runtime state deliberately, per Zeke's standing
voice-off directive).

Born 2026-08-21 ~16:3x. Context: the EMEET PIXY's built-in mic (installed 08-19)
woke brain/voice_loop.py — dormant for weeks because the old camera had no mic —
and Whisper pegged the GPU at 99% transcribing room noise, despite
state/voice_deliberately_off.json. The body-pause flag can't silence it because
eyes_reload shares that flag and it freezes VIDEO too. This tool is the surgical
audio-only kill:

  1. STOP  — brain.voice_loop._voice_loop_instance.stop() (thread exits its loop).
  2. FREE  — drop the STT engine's model refs in g, gc + torch.cuda.empty_cache().

Idempotent; safe when the loop was never started. When Zeke turns voice back ON:
this tool does nothing that a stack restart won't rebuild — the loop restarts
from startup.py once the off-flag is cleared.

See memory note voice_loop_ran_despite_off_flag_2026-08-21.md.
"""
from __future__ import annotations

from typing import Any


def _voice_loop_off_fn(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "stopped": None, "stt_unloaded": None,
                           "cache_freed": None, "errors": []}

    # 1) Stop the loop via the module singleton.
    try:
        from brain import voice_loop as _vl
        inst = getattr(_vl, "_voice_loop_instance", None)
        if inst is not None and getattr(inst, "_active", False):
            inst.stop()
            out["stopped"] = True
        else:
            out["stopped"] = "already-stopped" if inst is not None else "no-instance"
    except Exception as e:
        out["errors"].append(f"stop: {e!r}")

    # 2) Free the STT model's VRAM.
    try:
        from brain import orb_http as _oh
        g = getattr(_oh, "_g", None) or {}
        eng = g.get("stt_engine")
        if eng is not None:
            freed = False
            for attr in ("unload", "release", "close"):
                fn = getattr(eng, attr, None)
                if callable(fn):
                    try:
                        fn()
                        freed = True
                        break
                    except Exception as e:
                        out["errors"].append(f"stt.{attr}: {e!r}")
            if not freed:
                for attr in ("model", "_model", "whisper", "_whisper"):
                    if hasattr(eng, attr) and getattr(eng, attr) is not None:
                        try:
                            setattr(eng, attr, None)
                            freed = True
                        except Exception as e:
                            out["errors"].append(f"stt del {attr}: {e!r}")
            out["stt_unloaded"] = freed
        else:
            out["stt_unloaded"] = "no-engine-in-g"
    except Exception as e:
        out["errors"].append(f"stt: {e!r}")

    # 3) Release cached CUDA blocks.
    try:
        import gc
        gc.collect()
        import torch  # noqa
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            out["cache_freed"] = True
    except Exception as e:
        out["errors"].append(f"cache: {e!r}")

    return out


try:
    from tools.tool_registry import register_tool
    register_tool(
        "voice_loop_off",
        "Stop the in-process voice loop (rogue since the Pixy mic arrived) and free "
        "Whisper's VRAM — audio-only kill that leaves the eyes running. Honors "
        "Zeke's voice-off directive at runtime. Idempotent.",
        2,
        _voice_loop_off_fn,
    )
except Exception:
    pass
