"""Web search backends and a page fetcher.

Search is pluggable via config ``web_search`` (or env), supporting:
  - tavily     (TAVILY_API_KEY)
  - brave      (BRAVE_API_KEY)
  - serpapi    (SERPAPI_API_KEY)
  - searxng    (a self-hosted instance URL; keyless)

Each returns a list of {title, url, snippet}. `fetch_url` GETs a page and
returns readable text (HTML tags stripped).
"""

from __future__ import annotations

import os
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

import httpx


class SearchError(Exception):
    pass


def _get(config, key: str, env: str) -> Optional[str]:
    ws = getattr(config, "web_search", None) if config else None
    if isinstance(ws, dict):
        if ws.get(key):
            return str(ws[key])
        # Allow "<key>_env" to name the environment variable holding the value.
        if ws.get(f"{key}_env"):
            val = os.environ.get(str(ws[f"{key}_env"]))
            if val:
                return val
    return os.environ.get(env)


def web_search(config, query: str, count: int = 5) -> List[Dict[str, str]]:
    ws = getattr(config, "web_search", None) if config else None
    backend = (ws or {}).get("backend") if isinstance(ws, dict) else None
    backend = backend or os.environ.get("CODED_SEARCH_BACKEND")

    # Auto-pick a backend from whatever key is available.
    if not backend:
        if os.environ.get("TAVILY_API_KEY"):
            backend = "tavily"
        elif os.environ.get("BRAVE_API_KEY"):
            backend = "brave"
        elif os.environ.get("SERPAPI_API_KEY"):
            backend = "serpapi"
        elif _get(config, "url", "SEARXNG_URL"):
            backend = "searxng"
        else:
            raise SearchError(
                "No web-search backend configured. Set TAVILY_API_KEY / BRAVE_API_KEY / "
                "SERPAPI_API_KEY, or a SearXNG URL (SEARXNG_URL), or config 'web_search'."
            )

    with httpx.Client(timeout=30) as client:
        if backend == "tavily":
            key = _get(config, "api_key", "TAVILY_API_KEY")
            r = client.post("https://api.tavily.com/search",
                            json={"api_key": key, "query": query, "max_results": count})
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                    for x in r.json().get("results", [])]

        if backend == "brave":
            key = _get(config, "api_key", "BRAVE_API_KEY")
            r = client.get("https://api.search.brave.com/res/v1/web/search",
                           params={"q": query, "count": count},
                           headers={"X-Subscription-Token": key, "Accept": "application/json"})
            r.raise_for_status()
            web = r.json().get("web", {}).get("results", [])
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
                    for x in web]

        if backend == "serpapi":
            key = _get(config, "api_key", "SERPAPI_API_KEY")
            r = client.get("https://serpapi.com/search",
                           params={"q": query, "api_key": key, "num": count, "engine": "google"})
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("link", ""), "snippet": x.get("snippet", "")}
                    for x in r.json().get("organic_results", [])][:count]

        if backend == "searxng":
            base = _get(config, "url", "SEARXNG_URL")
            if not base:
                raise SearchError("SearXNG selected but no URL configured (SEARXNG_URL).")
            r = client.get(base.rstrip("/") + "/search",
                           params={"q": query, "format": "json"})
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                    for x in r.json().get("results", [])][:count]

    raise SearchError(f"Unknown search backend: {backend}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            text = data.strip()
            if text:
                self.parts.append(text)


def fetch_url(url: str, max_chars: int = 12_000) -> str:
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": "coded/0.1"}) as client:
        r = client.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" in ctype:
            parser = _TextExtractor()
            parser.feed(r.text)
            text = "\n".join(parser.parts)
        else:
            text = r.text
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… (truncated)"
    return text
