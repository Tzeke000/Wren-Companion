"""heap_census_tool — autopsy the live runtime heap: WHO holds the RAM.

SELF_ASSESSMENT: Tier 1 (read-only introspection; walks gc objects, mutates
nothing).

Born 2026-08-27 ~23:3x. Context: the tower froze and rebooted at 21:16 with RAM
at 98% (Zeke eyewitness). Post-reboot the runtime child was back at 18.3GB
private and climbing ~2GB/h — a real leak somewhere in the 30fps-era vision
stack or the whisper reload path. No eval tool exists in the registry, and a
stack bounce destroys the heap evidence, so this tool answers "what is the
memory?" from INSIDE the process before we bounce.

Reports:
- top object types by count (gc census)
- numpy arrays: count, total GB, top-10 by size with shape/dtype
- STTEngine / WhisperModel / TranscriptionEngine instance counts (the
  suspected stackers)
- torch CUDA allocator stats if available
- process private bytes for scale

Census of a multi-GB heap takes a few seconds; runs in the tool worker thread
so it cannot wedge the runtime loop.
"""
from __future__ import annotations

from typing import Any


def _heap_census_fn(*_args: Any, **kwargs: Any) -> dict[str, Any]:
    import gc
    import sys
    from collections import Counter

    out: dict[str, Any] = {"ok": True, "errors": []}

    # Process scale first.
    try:
        import psutil
        p = psutil.Process()
        out["private_gb"] = round(p.memory_info().private / 1e9, 2)
        out["rss_gb"] = round(p.memory_info().rss / 1e9, 2)
    except Exception as e:
        out["errors"].append(f"psutil: {e!r}")

    objs = gc.get_objects()
    out["gc_object_count"] = len(objs)

    # Type census (counts only — cheap).
    try:
        counts = Counter(type(o).__name__ for o in objs)
        out["top_types_by_count"] = counts.most_common(20)
    except Exception as e:
        out["errors"].append(f"type census: {e!r}")

    # numpy arrays by bytes — frames/audio live here.
    try:
        import numpy as np
        arrays = [o for o in objs if isinstance(o, np.ndarray)]
        total = sum(int(a.nbytes) for a in arrays)
        out["numpy"] = {
            "count": len(arrays),
            "total_gb": round(total / 1e9, 2),
            "top10": [
                {"shape": list(a.shape), "dtype": str(a.dtype),
                 "mb": round(a.nbytes / 1e6, 1)}
                for a in sorted(arrays, key=lambda a: -int(a.nbytes))[:10]
            ],
        }
    except Exception as e:
        out["errors"].append(f"numpy: {e!r}")

    # bytes/bytearray blobs (encoded frames, sockets buffers).
    try:
        blobs = [o for o in objs if isinstance(o, (bytes, bytearray))]
        btotal = sum(len(o) for o in blobs)
        out["bytes_blobs"] = {"count": len(blobs),
                              "total_gb": round(btotal / 1e9, 2)}
    except Exception as e:
        out["errors"].append(f"bytes: {e!r}")

    # The suspected stackers.
    try:
        suspects = Counter()
        for o in objs:
            tn = type(o).__name__
            if tn in ("STTEngine", "WhisperModel", "TranscriptionEngine",
                      "TTSWorker", "VideoMemory", "InsightFaceEngine"):
                suspects[tn] += 1
        out["suspect_instances"] = dict(suspects)
    except Exception as e:
        out["errors"].append(f"suspects: {e!r}")

    # Biggest dicts/lists by len (queues/caches gone feral show up here).
    try:
        big = []
        for o in objs:
            if isinstance(o, (list, dict)) and len(o) > 50_000:
                big.append({"type": type(o).__name__, "len": len(o),
                            "sample_key_or_item": repr(
                                next(iter(o), None))[:80]})
        out["big_containers"] = sorted(big, key=lambda d: -d["len"])[:10]
    except Exception as e:
        out["errors"].append(f"containers: {e!r}")

    # torch CUDA allocator view.
    try:
        import torch
        if torch.cuda.is_available():
            out["torch_cuda"] = {
                "allocated_gb": round(torch.cuda.memory_allocated() / 1e9, 2),
                "reserved_gb": round(torch.cuda.memory_reserved() / 1e9, 2),
            }
    except Exception as e:
        out["errors"].append(f"torch: {e!r}")

    del objs
    return out


def _shadow_import_probe_fn(*_args: Any, **kwargs: Any) -> dict[str, Any]:
    """Is the avaagent shadow stack cached, half-imported, or loop-importing?
    Read-only. Also lists live thread names (the shadow stack's threads have
    ava-* names) and whether brain.startup ran in this process."""
    import sys
    import threading
    out: dict[str, Any] = {"ok": True}
    out["avaagent_in_sys_modules"] = "avaagent" in sys.modules
    out["main_is"] = getattr(sys.modules.get("__main__"), "__file__", "?")
    out["brain_startup_imported"] = "brain.startup" in sys.modules
    names = sorted(t.name for t in threading.enumerate())
    out["thread_count"] = len(names)
    out["ava_threads"] = [n for n in names if "ava" in n.lower()]
    out["threads"] = names[:60]
    return out


try:
    from tools.tool_registry import register_tool
    register_tool(
        "shadow_import_probe",
        "Read-only: is avaagent cached in sys.modules (one shadow boot), absent "
        "(fail-evict import loop possible), plus live thread names. Built "
        "2026-08-27 chasing the 5xSTTEngine/9xWhisperModel stack.",
        1,
        _shadow_import_probe_fn,
    )
except Exception:
    pass


try:
    from tools.tool_registry import register_tool
    register_tool(
        "heap_census",
        "Read-only autopsy of the runtime's own heap: top object types, numpy "
        "array totals + top-10, whisper/STT instance counts, oversized "
        "containers, torch CUDA stats. Built 2026-08-27 to name the RAM leak "
        "that froze the tower. Takes a few seconds on a big heap.",
        1,
        _heap_census_fn,
    )
except Exception:
    pass
