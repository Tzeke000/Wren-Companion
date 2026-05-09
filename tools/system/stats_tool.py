# SELF_ASSESSMENT: I monitor system resources — CPU, RAM, GPU, disk — and Ava's own footprint.
"""
Phase 54 — System stats monitoring.

Requires: py -3.11 -m pip install psutil
Optional GPU: py -3.11 -m pip install gputil

Bootstrap: Ava calibrates her own concern thresholds based on what actually causes problems.
She develops her own sense of what 'too much' means for her system.
"""
from __future__ import annotations

import os
import time
from typing import Any
from tools.tool_registry import register_tool

_AVA_PROCESS_KEYWORDS = ("avaagent", "python")


def _get_cpu_usage(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        import psutil
        return {
            "ok": True,
            "overall_pct": psutil.cpu_percent(interval=0.5),
            "per_core": psutil.cpu_percent(interval=0.5, percpu=True),
            "count": psutil.cpu_count(),
        }
    except ImportError:
        return {"ok": False, "error": "psutil not installed: py -3.11 -m pip install psutil"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _get_ram_usage(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        import psutil
        m = psutil.virtual_memory()
        return {
            "ok": True,
            "total_gb": round(m.total / 1e9, 1),
            "used_gb": round(m.used / 1e9, 1),
            "available_gb": round(m.available / 1e9, 1),
            "percent": m.percent,
        }
    except ImportError:
        return {"ok": False, "error": "psutil not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _get_gpu_usage(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
        return {
            "ok": True,
            "gpus": [{"id": gpu.id, "name": gpu.name, "load_pct": round(gpu.load*100,1), "vram_used_mb": round(gpu.memoryUsed,0), "vram_total_mb": round(gpu.memoryTotal,0)} for gpu in gpus],
        }
    except ImportError:
        try:
            import subprocess
            r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                parts = r.stdout.strip().split(",")
                return {"ok": True, "gpus": [{"load_pct": float(parts[0]), "vram_used_mb": float(parts[1]), "vram_total_mb": float(parts[2])}]}
        except Exception:
            pass
        return {"ok": False, "error": "gputil not installed and nvidia-smi unavailable"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _get_disk_usage(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("path") or "C:\\")
    try:
        import psutil
        d = psutil.disk_usage(path)
        return {
            "ok": True,
            "path": path,
            "total_gb": round(d.total / 1e9, 1),
            "used_gb": round(d.used / 1e9, 1),
            "free_gb": round(d.free / 1e9, 1),
            "percent": d.percent,
        }
    except ImportError:
        return {"ok": False, "error": "psutil not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _get_ava_footprint(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        import psutil
        my_pid = os.getpid()
        total_cpu, total_ram = 0.0, 0
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                pname = str(p.info["name"] or "").lower()
                if p.info["pid"] == my_pid or any(kw in pname for kw in _AVA_PROCESS_KEYWORDS):
                    cpu = p.info["cpu_percent"] or 0.0
                    ram = (p.info["memory_info"].rss if p.info.get("memory_info") else 0) or 0
                    total_cpu += cpu
                    total_ram += ram
                    procs.append({"pid": p.info["pid"], "name": p.info["name"], "cpu_pct": cpu, "ram_mb": round(ram/1e6,1)})
            except Exception:
                continue
        return {"ok": True, "total_cpu_pct": round(total_cpu,1), "total_ram_mb": round(total_ram/1e6,1), "processes": procs}
    except ImportError:
        return {"ok": False, "error": "psutil not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _get_top_processes(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    n = min(int(params.get("n") or 5), 20)
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                procs.append({
                    "pid": p.info["pid"],
                    "name": str(p.info["name"] or "")[:40],
                    "cpu_pct": float(p.info["cpu_percent"] or 0),
                    "ram_mb": round((p.info["memory_info"].rss if p.info.get("memory_info") else 0) / 1e6, 1),
                })
            except Exception:
                continue
        procs.sort(key=lambda x: x["cpu_pct"], reverse=True)
        return {"ok": True, "top_processes": procs[:n]}
    except ImportError:
        return {"ok": False, "error": "psutil not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


register_tool("get_cpu_usage", "Get CPU usage per core and overall.", 1, _get_cpu_usage)
register_tool("get_ram_usage", "Get RAM usage — used/total/available.", 1, _get_ram_usage)
register_tool("get_gpu_usage", "Get GPU utilization and VRAM usage.", 1, _get_gpu_usage)
register_tool("get_disk_usage", "Get disk space for a given path.", 1, _get_disk_usage)
register_tool("get_ava_footprint", "Get CPU/RAM used by Ava's own processes.", 1, _get_ava_footprint)
register_tool("get_top_processes", "Get top N resource-consuming processes.", 1, _get_top_processes)
