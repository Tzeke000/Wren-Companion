"""
Phase image-generation — ComfyUI (local FLUX) + Pollinations.ai (cloud fallback).
SELF_ASSESSMENT: Tier 1 — Ava generates images freely. Prefers local ComfyUI when running.

generate(prompt) → path or None
generate_local(prompt) → path or None   (ComfyUI :8188)
generate_cloud(prompt) → path or None   (pollinations.ai)
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


_COMFYUI_BASE = "http://127.0.0.1:8188"
_POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
_IMAGE_DIR = "state/generated_images"


def _image_dir(g: dict[str, Any]) -> Path:
    d = Path(g.get("BASE_DIR") or ".") / _IMAGE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(prompt: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (prompt or "")[:40].lower()).strip("_") or "image"


def _ts_name(prompt: str, ext: str = "png") -> str:
    return f"{int(time.time())}_{_slug(prompt)}.{ext}"


class ImageGenerator:
    def __init__(self, g: dict[str, Any]):
        self._g = g

    def is_comfyui_running(self) -> bool:
        try:
            req = urllib.request.Request(f"{_COMFYUI_BASE}/system_stats", method="GET")
            with urllib.request.urlopen(req, timeout=1.0):
                return True
        except Exception:
            return False

    def generate_local(
        self, prompt: str, width: int = 1024, height: int = 1024, model: str = "flux"
    ) -> Optional[str]:
        """Generate via ComfyUI FLUX.1-schnell workflow. Returns saved path or None."""
        if not self.is_comfyui_running():
            return None
        try:
            workflow = self._flux_workflow(prompt, width, height)
            payload = json.dumps({"prompt": workflow}).encode("utf-8")
            req = urllib.request.Request(
                f"{_COMFYUI_BASE}/prompt",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
            prompt_id = str(resp_data.get("prompt_id") or "")
            if not prompt_id:
                return None
            # Poll for completion (max 120s)
            for _ in range(120):
                time.sleep(1.0)
                try:
                    hreq = urllib.request.Request(
                        f"{_COMFYUI_BASE}/history/{prompt_id}", method="GET"
                    )
                    with urllib.request.urlopen(hreq, timeout=5.0) as hr:
                        h = json.loads(hr.read().decode("utf-8"))
                    if prompt_id in h:
                        outputs = h[prompt_id].get("outputs") or {}
                        for node_out in outputs.values():
                            imgs = node_out.get("images") or []
                            if imgs:
                                fname = imgs[0].get("filename")
                                subdir = imgs[0].get("subfolder", "")
                                if fname:
                                    return self._download_comfy_image(
                                        fname, subdir, prompt
                                    )
                except Exception:
                    pass
            return None
        except Exception as e:
            print(f"[image_generator] local error: {e}")
            return None

    def generate_cloud(
        self, prompt: str, width: int = 1024, height: int = 1024
    ) -> Optional[str]:
        """Generate via Pollinations.ai. Requires internet."""
        if not self._g.get("_is_online", False):
            return None
        try:
            encoded = urllib.parse.quote(prompt, safe="")
            url = (
                f"{_POLLINATIONS_BASE}/{encoded}"
                f"?width={width}&height={height}&nologo=true&model=flux"
            )
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                data = resp.read()
            if not data or len(data) < 1000:
                return None
            path = _image_dir(self._g) / _ts_name(prompt, "png")
            path.write_bytes(data)
            print(f"[image_generator] cloud saved {path.name}")
            return str(path)
        except Exception as e:
            print(f"[image_generator] cloud error: {e}")
            return None

    def generate(
        self,
        prompt: str,
        prefer_local: bool = True,
        style: str = "",
        width: int = 1024,
        height: int = 1024,
    ) -> Optional[str]:
        """Try local then cloud (or cloud first if prefer_local=False)."""
        full_prompt = f"{prompt}, {style}" if style else prompt
        result: Optional[str] = None
        if prefer_local:
            result = self.generate_local(full_prompt, width, height)
        if result is None and self._g.get("_is_online", False):
            result = self.generate_cloud(full_prompt, width, height)
        if result:
            self._g["_latest_image"] = result
            self._log_generation(prompt, style, result, "local" if prefer_local and result else "cloud")
        return result

    # ── internals ────────────────────────────────────────────────────────────

    def _flux_workflow(self, prompt: str, width: int, height: int) -> dict[str, Any]:
        """Minimal FLUX.1-schnell ComfyUI API workflow."""
        return {
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["11", 1], "text": prompt},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["13", 0], "vae": ["10", 0]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "ava_gen", "images": ["8", 0]},
            },
            "10": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "ae.safetensors"},
            },
            "11": {
                "class_type": "DualCLIPLoader",
                "inputs": {
                    "clip_name1": "clip_l.safetensors",
                    "clip_name2": "t5xxl_fp8_e4m3fn.safetensors",
                    "type": "flux",
                },
            },
            "12": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": "flux1-schnell.safetensors",
                    "weight_dtype": "fp8_e4m3fn",
                },
            },
            "13": {
                "class_type": "KSampler",
                "inputs": {
                    "cfg": 1,
                    "denoise": 1,
                    "latent_image": ["14", 0],
                    "model": ["12", 0],
                    "negative": ["7", 0],
                    "positive": ["6", 0],
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "seed": int(time.time()) % (2**31),
                    "steps": 4,
                },
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["11", 1], "text": ""},
            },
            "14": {
                "class_type": "EmptyLatentImage",
                "inputs": {"batch_size": 1, "height": height, "width": width},
            },
        }

    def _download_comfy_image(
        self, fname: str, subdir: str, prompt: str
    ) -> Optional[str]:
        try:
            params = urllib.parse.urlencode(
                {"filename": fname, "subfolder": subdir, "type": "output"}
            )
            url = f"{_COMFYUI_BASE}/view?{params}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                data = resp.read()
            if not data:
                return None
            path = _image_dir(self._g) / _ts_name(prompt, "png")
            path.write_bytes(data)
            print(f"[image_generator] local saved {path.name}")
            return str(path)
        except Exception as e:
            print(f"[image_generator] download error: {e}")
            return None

    def _log_generation(
        self, prompt: str, style: str, path: str, backend: str
    ) -> None:
        try:
            log_p = _image_dir(self._g).parent / "image_generation_log.jsonl"
            entry = {
                "ts": time.time(),
                "prompt": str(prompt)[:200],
                "style": str(style)[:100],
                "path": path,
                "backend": backend,
            }
            with log_p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ── tool registration shim ────────────────────────────────────────────────────

def _get_gen(g: dict[str, Any]) -> ImageGenerator:
    gen = g.get("_image_generator")
    if gen is None:
        gen = ImageGenerator(g)
        g["_image_generator"] = gen
    return gen


def generate_image_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    gen = _get_gen(g)
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt required"}
    style = str(params.get("style") or "")
    width = int(params.get("width") or 1024)
    height = int(params.get("height") or 1024)
    path = gen.generate(prompt, style=style, width=width, height=height)
    return {"ok": path is not None, "path": path, "prompt": prompt}


def show_image_fn(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("image_path") or g.get("_latest_image") or "")
    caption = str(params.get("caption") or "")
    g["_latest_image"] = path
    g["_latest_image_caption"] = caption
    return {"ok": bool(path), "path": path, "caption": caption}


try:
    from tools.tool_registry import register_tool
    register_tool("generate_image", "Generate an image from a text prompt (local FLUX or cloud).", 1, generate_image_fn)
    register_tool("show_image", "Set the latest image for display in the operator panel.", 1, show_image_fn)
except Exception:
    pass
