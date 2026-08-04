"""web_search and web_fetch tools."""

from __future__ import annotations

from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
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
    description = """Fetch a URL and return its readable text content (HTML tags stripped). \
Use for reading documentation pages or search results."""
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch (http/https)."},
        },
        "required": ["url"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from coded.websearch import fetch_url

        url = args["url"]
        if not url.startswith(("http://", "https://")):
            return ToolResult.error("URL must start with http:// or https://")
        try:
            text = fetch_url(url)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"Could not fetch {url}: {exc}")
        return ToolResult.ok(text)
