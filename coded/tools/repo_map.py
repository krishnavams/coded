"""The `repo_map` tool: a ranked outline of the codebase."""

from __future__ import annotations

from typing import Any, Dict

from coded.limits import get_limits
from coded.tools.base import Tool, ToolContext, ToolResult


class RepoMapTool(Tool):
    name = "repo_map"
    brief = "Ranked outline of the repository's definitions, most-used files first. Use it in an unfamiliar codebase or to find where something lives; narrow with path or focus, then read the files."
    description = """A ranked outline of the repository: each file's definitions \
(classes, functions, types), with the files the rest of the code depends on most listed \
first. Use it at the start of a task in an unfamiliar codebase, or to find where a concept \
lives when you don't know the filename — it answers "what is this made of and what matters", \
which grep cannot. Narrow it with `path` (a subdirectory) or `focus` (a symbol or word to \
rank files around). It lists definitions only, never code: read the files it points at."""
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Limit the map to this subdirectory (repo-relative)."},
            "focus": {"type": "string",
                      "description": "A symbol, file or concept to rank files around, "
                                     "e.g. 'PermissionManager' or 'compaction'."},
            "detail": {"type": "string", "enum": ["names", "kinds"],
                       "description": "'kinds' (default) shows 'class Foo'; 'names' is "
                                      "terser and fits more files in the same budget."},
        },
    }
    parallel_safe = True

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from coded.repomap import build, render
        from coded.repo_overview import list_files

        limits = get_limits(ctx)
        budget = max(2_000, min(limits.tool_result_chars, 40_000))
        path = (args.get("path") or "").strip().strip("/")
        focus = (args.get("focus") or "").strip()
        names_only = (args.get("detail") == "names")

        try:
            files, _source = list_files(ctx.cwd)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"Could not list the repository: {exc}")
        if path:
            files = [f for f in files if f == path or f.startswith(path + "/")]
            if not files:
                return ToolResult.error(
                    f"No files under '{path}'. Use ls or glob to find the right directory.")
        repo_map = build(ctx.cwd, files)
        if not repo_map.files:
            return ToolResult.error(
                "No definitions found to map here — the files may be data, or in a language "
                "the outline does not cover. Use glob and grep instead.")
        if focus:
            from coded.repomap import _refocus

            repo_map = _refocus(repo_map, focus)
        text = render(repo_map, budget, per_file=20 if names_only else 12,
                      names_only=names_only)
        if repo_map.truncated:
            text += ("\n(The scan was capped, so some files are missing; narrow it with "
                     "`path` for the full picture of a subtree.)\n")
        return ToolResult.ok(text)
