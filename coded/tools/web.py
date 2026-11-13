"""web_search and web_fetch tools."""

from __future__ import annotations

from typing import Any, Dict

from coded.limits import get_limits
from coded.tools.base import Tool, ToolContext, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    parallel_safe = True
    untrusted_output = True
    description = """Search the web and return titles, URLs, and snippets. Requires a \
configured backend (Tavily/Brave/SerpAPI/SearXNG). Use web_fetch to read a result."""
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "count": {"type": "integer", "description": "Number of results (default 5)."},
        },
        "required": ["query"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from coded.websearch import SearchError, web_search

        try:
            results = web_search(ctx.config, args["query"], int(args.get("count", 5)))
        except SearchError as exc:
            return ToolResult.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"Web search failed: {exc}")
        if not results:
            return ToolResult.ok("No results.")
        out = [f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results]
        return ToolResult.ok("\n\n".join(out))


class WebFetchTool(Tool):
    name = "web_fetch"
    parallel_safe = True
    untrusted_output = True
    description = """Fetch a URL and return its readable text content (HTML tags stripped). \
Use for reading documentation pages or search results."""
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch (http/https)."},
        },
        "required": ["url"],
    }

    def is_parallel_safe(self, args: Dict[str, Any], ctx: ToolContext) -> bool:
        # An internal address asks first, and prompts are not run from workers.
        from coded.netguard import internal_reason

        return internal_reason(str(args.get("url", ""))) is None

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from coded.websearch import fetch_url

        url = args["url"]
        if not url.startswith(("http://", "https://")):
            return ToolResult.error("URL must start with http:// or https://")
        allow = url_permission(ctx, "web_fetch", "Fetch")
        if not allow(url):
            return ToolResult.error(
                f"The user did not allow fetching {url} (it is on this machine or the local "
                "network). Use a public URL, or ask the user.")
        try:
            text = fetch_url(url, max_chars=get_limits(ctx).web_fetch_chars, allow=allow)
        except PermissionError as exc:
            return ToolResult.error(f"Stopped: a redirect led somewhere not allowed ({exc}).")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"Could not fetch {url}: {exc}")
        return ToolResult.ok(text)


def url_permission(ctx: ToolContext, key: str, verb: str):
    """A check for each URL a tool is about to load: public addresses pass;
    a local file or an internal address (localhost, the LAN, cloud metadata)
    asks the user once per host. Non-interactive runs refuse them."""
    from coded.netguard import internal_reason
    from coded.permissions import Decision
    from urllib.parse import urlparse

    approved = set()

    def allow(url: str) -> bool:
        reason = internal_reason(url)
        if reason is None:
            return True
        parsed = urlparse(url)
        scope = (parsed.scheme, parsed.hostname or parsed.path)
        if scope in approved:
            return True
        if ctx.permissions is None:
            return False
        decision = ctx.permissions.request(
            key=key, title=f"{verb} {reason}", detail=url, target=url, always_ask=True)
        if decision is Decision.ALLOW:
            approved.add(scope)
            return True
        return False

    return allow
