"""One-shot in-process surgery (2026-08-21): stop the rogue VoiceLoop + free Whisper VRAM.

Loaded via brain_hot_swap (import executes run()). Safe to re-run: every step is
idempotent and guarded. Context: the PIXY mic woke the dormant voice loop despite
state/voice_deliberately_off.json; .tmp/body_pause.flag can't be used to silence it
because eyes_reload shares that flag and it also freezes VIDEO. See memory note
voice_loop_ran_despite_off_flag_2026-08-21.md.
"""
from __future__ import annotations

import sys


def run() -> dict:
    out: dict = {"stopped": None, "stt_unloaded": None, "cache_freed": None, "errors": []}

    # 1) Stop the voice loop thread via the module singleton.
    try:
        from brain import voice_loop as _vl
        inst = getattr(_vl, "_voice_loop_instance", None)
        if inst is not None and getattr(inst, "_active", False):
            inst.stop()
            out["stopped"] = True
        else:
            out["stopped"] = False if inst is not None else "no-instance"
    except Exception as e:
        out["errors"].append(f"stop: {e!r}")

    # 2) Drop the STT engine's model to free VRAM (whisper large-v3-turbo).
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
                    if hasattr(eng, attr):
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

    # 3) Release cached CUDA blocks (torch models keep their own weights).
    try:
        import gc
        gc.collect()
        import torch  # noqa
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            out["cache_freed"] = True
    except Exception as e:
        out["errors"].append(f"cache: {e!r}")

    print(f"[voice_stop_patch] {out}", file=sys.stderr, flush=True)
    return out


# Execute on import so brain_hot_swap's load performs the surgery.
RESULT = run()
