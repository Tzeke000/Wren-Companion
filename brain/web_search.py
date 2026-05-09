"""brain/web_search.py — Web search + connectivity-gated knowledge lookup (A7).

Per Zeke 2026-05-06: "allow her to search the web for whatever she needs.
When offline, she should (1) realize she doesn't know, (2) realize she
can't look it up, (3) verbally say so."

Three states for any factual question:
- KNOW — she has it from training/memory; no search needed.
- CAN_LOOK_UP — online, search succeeds, fold result into reply.
- CANNOT_KNOW — offline AND don't know AND can't look up. Must say so.

Backends (bootstrap-friendly — no API keys required):
- Wikipedia REST API (en.wikipedia.org/api/rest_v1) for factual lookups
- DuckDuckGo Instant Answer (api.duckduckgo.com) as secondary

Brave Search API would be better for general web queries but requires
key + paid tier. Defer until Phase 1+ when we have budget for it.

Storage: state/web_search_cache.json (lightweight cache to avoid re-fetching)

API:

    from brain.web_search import (
        is_online_for_search, search, SearchResult,
        knows_or_can_look_up_hint,
    )

    if is_online_for_search():
        result = search("polar bears habitat")
        if result.ok:
            # fold result.summary into reply
        else:
            # search failed; explain in reply
    else:
        # offline; the cannot_know branch
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SearchResult:
    ok: bool
    query: str
    summary: str = ""
    source: str = ""  # "wikipedia" | "duckduckgo" | "cache"
    url: str = ""
    error: str = ""
    ts: float = 0.0


_lock = threading.RLock()
_base_dir: Path | None = None
_CACHE_TTL_S = 24 * 3600  # facts don't change much in a day
_cache: dict[str, dict[str, Any]] = {}
_USER_AGENT = "AvaAgent/1.0 (personal AI companion; bootstrap-friendly)"


def configure(base_dir: Path) -> None:
    global _base_dir
    with _lock:
        _base_dir = base_dir
        _load_cache_locked()


def _path() -> Path | None:
    if _base_dir is None:
        return None
    p = _base_dir / "state" / "web_search_cache.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_cache_locked() -> None:
    global _cache
    p = _path()
    if p is None or not p.exists():
        _cache = {}
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            _cache = d
    except Exception as e:
        print(f"[web_search] cache load error: {e!r}")
        _cache = {}


def _persist_cache_locked() -> None:
    p = _path()
    if p is None:
        return
    try:
        p.write_text(json.dumps(_cache, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[web_search] cache save error: {e!r}")


def _normalize_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())[:200]


def is_online_for_search() -> bool:
    """Check connectivity. Uses existing brain.connectivity infrastructure."""
    try:
        from brain.connectivity import get_monitor
        return get_monitor().is_online()
    except Exception:
        # No connectivity monitor available — assume offline (fail-closed)
        return False


def _lookup_cache(query: str) -> SearchResult | None:
    norm = _normalize_query(query)
    with _lock:
        entry = _cache.get(norm)
        if not entry:
            return None
        ts = float(entry.get("ts") or 0.0)
        if (time.time() - ts) > _CACHE_TTL_S:
            return None
        return SearchResult(
            ok=True,
            query=query,
            summary=str(entry.get("summary") or ""),
            source=str(entry.get("source") or "cache"),
            url=str(entry.get("url") or ""),
            ts=ts,
        )


def _store_cache(result: SearchResult) -> None:
    if not result.ok:
        return
    norm = _normalize_query(result.query)
    with _lock:
        _cache[norm] = {
            "ts": result.ts,
            "summary": result.summary,
            "source": result.source,
            "url": result.url,
        }
        _persist_cache_locked()


def _wikipedia_summary(query: str) -> SearchResult:
    """Wikipedia REST API summary endpoint. Free, no API key."""
    try:
        # Use the search endpoint first to find the right page
        search_url = (
            "https://en.wikipedia.org/w/api.php?"
            "action=query&list=search&format=json&srlimit=1&srsearch="
            + urllib.parse.quote(query)
        )
        req = urllib.request.Request(search_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        hits = data.get("query", {}).get("search", [])
        if not hits:
            return SearchResult(
                ok=False, query=query, source="wikipedia",
                error="no results",
            )
        title = hits[0].get("title") or ""
        if not title:
            return SearchResult(
                ok=False, query=query, source="wikipedia",
                error="no title in result",
            )

        # Fetch summary for the matched title
        summary_url = (
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            + urllib.parse.quote(title.replace(" ", "_"))
        )
        req2 = urllib.request.Request(summary_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req2, timeout=5.0) as resp:
            sd = json.loads(resp.read().decode("utf-8"))

        extract = str(sd.get("extract") or "").strip()
        if not extract:
            return SearchResult(
                ok=False, query=query, source="wikipedia",
                error="no extract in summary",
            )
        page_url = str(sd.get("content_urls", {}).get("desktop", {}).get("page") or "")
        return SearchResult(
            ok=True,
            query=query,
            summary=extract[:1500],
            source="wikipedia",
            url=page_url,
            ts=time.time(),
        )
    except Exception as e:
        return SearchResult(
            ok=False, query=query, source="wikipedia", error=f"{type(e).__name__}: {e}",
        )


def _duckduckgo_instant_answer(query: str) -> SearchResult:
    """DuckDuckGo Instant Answer API. Free, no key, limited coverage but
    good for entity-style queries. Note: DDG IA only returns answers for
    a fraction of queries — it's a complement to Wikipedia, not replacement."""
    try:
        url = (
            "https://api.duckduckgo.com/?format=json&no_html=1&skip_disambig=1&q="
            + urllib.parse.quote(query)
        )
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        abstract = str(data.get("Abstract") or "").strip()
        answer = str(data.get("Answer") or "").strip()
        if abstract:
            return SearchResult(
                ok=True,
                query=query,
                summary=abstract[:1500],
                source="duckduckgo",
                url=str(data.get("AbstractURL") or ""),
                ts=time.time(),
            )
        if answer:
            return SearchResult(
                ok=True,
                query=query,
                summary=answer[:1500],
                source="duckduckgo",
                ts=time.time(),
            )
        return SearchResult(
            ok=False, query=query, source="duckduckgo",
            error="no abstract or answer",
        )
    except Exception as e:
        return SearchResult(
            ok=False, query=query, source="duckduckgo", error=f"{type(e).__name__}: {e}",
        )


def search(query: str, *, allow_cache: bool = True) -> SearchResult:
    """Search for `query` using the available backends.

    Returns SearchResult with ok=True on success, ok=False otherwise.
    Tries cache first, then Wikipedia, then DuckDuckGo. Connectivity
    is checked at the wrapper level (not here) so callers see explicit
    "cannot look up" when offline.
    """
    if not query or not query.strip():
        return SearchResult(ok=False, query=query, error="empty query")

    if allow_cache:
        cached = _lookup_cache(query)
        if cached:
            return cached

    if not is_online_for_search():
        return SearchResult(
            ok=False, query=query, source="offline",
            error="not online — cannot look this up",
        )

    # Try Wikipedia first (best for factual queries)
    result = _wikipedia_summary(query)
    if result.ok:
        _store_cache(result)
        return result

    # Fall through to DuckDuckGo Instant Answer
    result = _duckduckgo_instant_answer(query)
    if result.ok:
        _store_cache(result)
        return result

    return SearchResult(
        ok=False, query=query, source="all_backends_failed",
        error=f"wikipedia + duckduckgo both failed for {query!r}",
    )


# Heuristic: question phrasings that are usually factual lookups Ava
# benefits from searching for rather than answering from training.
_FACTUAL_QUESTION_PATTERNS = [
    re.compile(r"\bwho is\b", re.IGNORECASE),
    re.compile(r"\bwhat is\b", re.IGNORECASE),
    re.compile(r"\bwhat (was|were)\b", re.IGNORECASE),
    re.compile(r"\bwhen (did|was|will)\b", re.IGNORECASE),
    re.compile(r"\bwhere is\b", re.IGNORECASE),
    re.compile(r"\bhow (does|do|did)\b", re.IGNORECASE),
    re.compile(r"\btell me about\b", re.IGNORECASE),
    re.compile(r"\blook up\b", re.IGNORECASE),
    re.compile(r"\bsearch (for|the web)\b", re.IGNORECASE),
    re.compile(r"\bcan you find\b", re.IGNORECASE),
]


def looks_like_factual_lookup(text: str) -> bool:
    """Heuristic: is this user input a factual question Ava would benefit
    from looking up rather than answering from training?

    Bootstrap-friendly: errs on the conservative side. The action_tag_router
    LLM classifier catches the long tail.
    """
    if not text:
        return False
    return any(p.search(text) for p in _FACTUAL_QUESTION_PATTERNS)


def cannot_know_response(query: str = "") -> str:
    """Honest response when Ava can't look something up.

    Per Zeke's spec: she should verbally acknowledge that she (1) doesn't
    know, (2) realizes she can't look it up, (3) says so.
    """
    if query:
        return (
            f"I don't know about that, and I can't look it up right now — "
            f"I'm offline. Once we're back online, ask me again and I'll "
            f"try."
        )
    return (
        "I don't know that, and I can't look it up right now — I'm offline. "
        "Ask me again when we're back online."
    )


def knows_or_can_look_up_hint(g: dict[str, Any]) -> str:
    """System-prompt fragment giving Ava awareness of her current
    information-access state."""
    online = is_online_for_search()
    if online:
        return (
            "WEB SEARCH AVAILABLE: you're currently online. If asked something "
            "you don't know, you can search Wikipedia / DuckDuckGo via "
            "brain.web_search. Don't fabricate; look it up or say you don't "
            "know."
        )
    return (
        "WEB SEARCH UNAVAILABLE: you're currently offline. If asked something "
        "you don't know from training/memory, say so honestly — explain that "
        "you'd need internet to look it up. Don't fabricate."
    )


def search_summary() -> dict[str, Any]:
    """Operator-debug summary of the search subsystem."""
    online = is_online_for_search()
    with _lock:
        cache_size = len(_cache)
    return {
        "online": online,
        "cache_size": cache_size,
        "cache_ttl_s": _CACHE_TTL_S,
        "backends": ["wikipedia", "duckduckgo"],
    }
