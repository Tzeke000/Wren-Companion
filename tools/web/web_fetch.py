from __future__ import annotations

from typing import Any

import requests

from tools.tool_registry import register_tool


def fetch_url(url: str) -> dict[str, Any]:
    r = requests.get(url, timeout=20)
    return {"status_code": r.status_code, "content": r.text[:9000], "url": url}


def _tool_fetch(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        url = str(params.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "invalid_url"}
        return {"ok": True, **fetch_url(url)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


register_tool("web_fetch", "Fetch a URL and return text content.", 1, _tool_fetch)

