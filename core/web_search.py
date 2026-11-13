"""
Optional web results for the context assistant.

Privacy rule: only the user's question is ever sent to the search provider,
never document text, file names or matter names.

Configuration (environment):
    WEB_SEARCH_PROVIDER   tavily | brave | serper     (unset = web access unavailable)
    WEB_SEARCH_API_KEY    key for that provider
    ASSISTANT_WEB_ENABLED true | false                 (platform-wide switch, default true)
"""
from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)
TIMEOUT = 12


def web_status() -> str:
    """'ok', 'disabled' (switched off for the platform) or 'not_configured'."""
    if os.getenv("ASSISTANT_WEB_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        return "disabled"
    if not (os.getenv("WEB_SEARCH_PROVIDER") and os.getenv("WEB_SEARCH_API_KEY")):
        return "not_configured"
    return "ok"


def search(question: str, limit: int = 5) -> list[dict]:
    """[{title, url, snippet}] for the question, or [] when unavailable or failing."""
    if web_status() != "ok":
        return []
    provider = os.getenv("WEB_SEARCH_PROVIDER", "").strip().lower()
    key = os.getenv("WEB_SEARCH_API_KEY", "").strip()
    q = (question or "").strip()[:400]
    try:
        if provider == "tavily":
            r = requests.post("https://api.tavily.com/search", timeout=TIMEOUT,
                              json={"api_key": key, "query": q, "max_results": limit, "search_depth": "basic"})
            r.raise_for_status()
            items = r.json().get("results", [])
            return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": (i.get("content") or "")[:600]}
                    for i in items[:limit]]
        if provider == "brave":
            r = requests.get("https://api.search.brave.com/res/v1/web/search", timeout=TIMEOUT,
                             params={"q": q, "count": limit},
                             headers={"X-Subscription-Token": key, "Accept": "application/json"})
            r.raise_for_status()
            items = (r.json().get("web") or {}).get("results", [])
            return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": (i.get("description") or "")[:600]}
                    for i in items[:limit]]
        if provider == "serper":
            r = requests.post("https://google.serper.dev/search", timeout=TIMEOUT,
                              json={"q": q, "num": limit}, headers={"X-API-KEY": key})
            r.raise_for_status()
            items = r.json().get("organic", [])
            return [{"title": i.get("title", ""), "url": i.get("link", ""), "snippet": (i.get("snippet") or "")[:600]}
                    for i in items[:limit]]
        logger.warning("Unknown WEB_SEARCH_PROVIDER %r", provider)
    except Exception as exc:
        logger.warning("Web search failed (%s): %s", provider, exc)
    return []
