from __future__ import annotations

import time
import re
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

from tools.tool_registry import register_tool

_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_TTL_SECONDS = 300.0


def _extract_results(payload: dict[str, Any], max_results: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    abs_text = str(payload.get("AbstractText") or "").strip()
    abs_url = str(payload.get("AbstractURL") or "").strip()
    heading = str(payload.get("Heading") or "").strip() or "DuckDuckGo Result"
    if abs_text or abs_url:
        out.append({"title": heading, "url": abs_url, "snippet": abs_text})
    for row in list(payload.get("RelatedTopics") or []):
        if isinstance(row, dict) and isinstance(row.get("Topics"), list):
            for nested in list(row.get("Topics") or []):
                if not isinstance(nested, dict):
                    continue
                out.append(
                    {
                        "title": str(nested.get("Text") or "").split(" - ")[0][:120],
                        "url": str(nested.get("FirstURL") or ""),
                        "snippet": str(nested.get("Text") or "")[:240],
                    }
                )
        elif isinstance(row, dict):
            out.append(
                {
                    "title": str(row.get("Text") or "").split(" - ")[0][:120],
                    "url": str(row.get("FirstURL") or ""),
                    "snippet": str(row.get("Text") or "")[:240],
                }
            )
        if len(out) >= max_results:
            break
    return [r for r in out if r.get("url")][:max_results]


def _extract_results_from_html(html: str, max_results: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not html:
        return out
    link_re = re.compile(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    snip_re = re.compile(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.I | re.S)
    links = link_re.findall(html)
    snippets = snip_re.findall(html)
    for idx, (url, title_html) in enumerate(links[:max_results]):
        if "uddg=" in url:
            try:
                qs = parse_qs(urlparse(url).query)
                raw = (qs.get("uddg") or [url])[0]
                url = unquote(raw)
            except Exception:
                pass
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        snippet = ""
        if idx < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[idx]).strip()
        out.append({"title": title[:140], "url": url, "snippet": snippet[:240]})
    return out[:max_results]


def search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    q = str(query or "").strip()
    limit = max(1, min(10, int(max_results or 5)))
    if not q:
        return []
    now = time.time()
    cached = _CACHE.get(q.lower())
    if cached and (now - cached[0]) <= _TTL_SECONDS:
        return cached[1][:limit]
    url = f"https://api.duckduckgo.com/?q={quote_plus(q)}&format=json&no_redirect=1&no_html=1"
    r = requests.get(url, timeout=20)
    payload = r.json() if r.headers.get("content-type", "").lower().startswith("application/json") else {}
    rows = _extract_results(payload if isinstance(payload, dict) else {}, max_results=limit)
    if not rows:
        html_url = f"https://duckduckgo.com/html/?q={quote_plus(q)}"
        h = requests.get(html_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        rows = _extract_results_from_html(h.text if h.ok else "", max_results=limit)
    _CACHE[q.lower()] = (now, rows)
    return rows


def _tool_search(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        query = str(params.get("query") or "").strip()
        max_results = int(params.get("max_results") or 5)
        rows = search(query, max_results=max_results)
        return {"ok": True, "query": query, "results": rows}
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}


register_tool("web_search", "Search the web via DuckDuckGo instant answer API.", 1, _tool_search)

