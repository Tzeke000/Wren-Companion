# SELF_ASSESSMENT: I process files dropped into my window — images, code, documents — and engage with their content.
"""
Phase 55 — Drag-and-drop file processing tool.

Routes dropped files to appropriate handlers based on extension.
Bootstrap: Ava notices which file types lead to engaging conversations.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".cpp", ".c", ".h", ".cs", ".rb", ".sh", ".bat"}
DOC_EXTS = {".txt", ".md", ".rst", ".csv", ".json", ".xml", ".html", ".yaml", ".yml"}
PDF_EXTS = {".pdf"}


def _process_dropped_file(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    path_str = str(params.get("path") or "").strip()
    if not path_str:
        return {"ok": False, "error": "path required"}

    path = Path(path_str)
    if not path.exists():
        return {"ok": False, "error": f"file not found: {path_str}"}

    ext = path.suffix.lower()
    size = path.stat().st_size

    if ext in IMAGE_EXTS:
        # Describe with LLaVA
        try:
            from langchain_ollama import ChatOllama
            from langchain_core.messages import HumanMessage
            import base64
            b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            llm = ChatOllama(model="llava:latest", temperature=0.2)
            result = llm.invoke([HumanMessage(content=[
                {"type": "text", "text": "Describe this image clearly and helpfully."},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ])])
            description = (getattr(result, "content", None) or str(result)).strip()[:800]
            return {"ok": True, "type": "image", "path": path_str, "description": description}
        except Exception as e:
            return {"ok": False, "type": "image", "error": str(e)[:200]}

    if ext in CODE_EXTS:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:4000]
            return {
                "ok": True, "type": "code", "path": path_str,
                "language": ext.lstrip("."), "content": content,
                "lines": content.count("\n") + 1,
                "note": f"Code file ready for review — {ext.lstrip('.')} ({size} bytes)",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    if ext in DOC_EXTS:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:3000]
            return {
                "ok": True, "type": "document", "path": path_str,
                "content": content, "size_bytes": size,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    if ext in PDF_EXTS:
        try:
            import subprocess, sys
            result = subprocess.run(
                [sys.executable, "-c",
                 f"import pypdf; r=pypdf.PdfReader('{path_str}'); print('\\n'.join(p.extract_text() or '' for p in r.pages[:5]))"],
                capture_output=True, text=True, timeout=15,
            )
            text = result.stdout[:3000] if result.returncode == 0 else "(PDF text extraction failed)"
            return {"ok": True, "type": "pdf", "path": path_str, "content": text, "size_bytes": size}
        except Exception:
            return {"ok": True, "type": "pdf", "path": path_str, "content": "(could not extract PDF text)", "size_bytes": size}

    # Unknown — metadata only
    return {
        "ok": True, "type": "unknown", "path": path_str,
        "extension": ext, "size_bytes": size,
        "note": f"Unrecognized file type {ext}. Size: {size} bytes.",
    }


register_tool(
    name="process_dropped_file",
    description="Process a file dropped into Ava's window — images, code, documents, PDFs.",
    tier=1,
    handler=_process_dropped_file,
)
