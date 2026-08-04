"""semantic_search tool: query the embedded code index by meaning."""

from __future__ import annotations

from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class SemanticSearchTool(Tool):
    name = "semantic_search"
    description = """Search the codebase by meaning (not just literal text) using the \
embedded index. Returns the most relevant code chunks as path:line ranges. Requires a \
built index — tell the user to run `coded index` if it's missing. Use `grep` for exact \
string/regex matches."""
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language description of what to find."},
            "k": {"type": "integer", "description": "How many results (default 8)."},
        },
        "required": ["query"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from coded.embeddings import EmbeddingClient, resolve_embedding_model
        from coded.index import CodeIndex

        index = CodeIndex(ctx.cwd)
        if not index.load():
            return ToolResult.error(
                "No semantic index found. Run `coded index` in this project first, "
                "then retry (or use the grep tool for literal search)."
            )
        try:
            active = ctx.config.get_model(ctx.config.default_model) if ctx.config else None
            embed_model = resolve_embedding_model(ctx.config, active)
            client = EmbeddingClient(embed_model)
            results = index.search(client, args["query"], int(args.get("k", 8)))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"Semantic search failed: {exc}")

        if not results:
            return ToolResult.ok("No results (index may be empty).")
        out = []
        for r in results:
            snippet = r["text"]
            if len(snippet) > 500:
                snippet = snippet[:500] + "…"
            out.append(f"{r['path']}:{r['start']}-{r['end']}  (score {r['score']})\n{snippet}")
        return ToolResult.ok("\n\n".join(out))
