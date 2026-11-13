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
rank files around). It lists definitions only, never code: read the files it points at.

Other operations answer dependency questions (Python and JS/TS imports): `imports` (what \
`path` imports), `dependents` (what imports `path`), `impact` (everything a change to \
`path` — comma-separate several — can affect, and the tests to run) and `cycles` (import \
cycles). Check `impact` before changing a widely used module. `health` lists refactoring hot \
spots (long, branchy or deeply nested functions, copy-pasted blocks) under `path`."""
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string",
                          "enum": ["outline", "imports", "dependents", "impact", "cycles",
                                   "health"],
                          "description": "'outline' (default) ranks definitions; the others "
                                         "follow imports."},
            "path": {"type": "string",
                     "description": "outline: limit to this subdirectory. imports/dependents/"
                                    "impact: the file or folder (impact: comma-separated)."},
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
        op = (args.get("operation") or "outline").strip()
        if op != "outline":
            return self._deps(op, (args.get("path") or "").strip(), ctx, budget)
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

    def _deps(self, op: str, path: str, ctx: ToolContext, budget: int) -> ToolResult:
        from coded import depgraph

        if op == "health":
            return self._health(path, ctx, budget)
        if op not in ("imports", "dependents", "impact", "cycles"):
            return ToolResult.error(f"Unknown operation {op!r}; use outline, imports, "
                                    "dependents, impact, cycles or health.")
        if op != "cycles" and not path:
            return ToolResult.error(f"'{op}' needs `path`: a repo-relative file or folder.")
        graph = depgraph.build_for(ctx.cwd)
        if not graph.imports:
            return ToolResult.error(
                "No Python or JavaScript/TypeScript imports found here. Use grep to follow "
                "references in other languages.")
        if op == "imports":
            text = depgraph.render_imports(graph, path, budget)
        elif op == "dependents":
            text = depgraph.render_dependents(graph, path, budget)
        elif op == "impact":
            paths = [p.strip() for p in path.split(",") if p.strip()]
            text = depgraph.render_impact(graph, paths, budget)
        else:
            text = depgraph.render_cycles(graph, budget)
        if not text:
            return ToolResult.error(
                f"'{path}' is not a Python or JS/TS file (or folder of them) in the import "
                "graph. Use glob to find the exact repo-relative path.")
        if graph.truncated:
            text += "(The scan was capped, so the graph may be incomplete.)\n"
        return ToolResult.ok(text)

    def _health(self, path: str, ctx: ToolContext, budget: int) -> ToolResult:
        from coded import codehealth
        from coded.repo_overview import list_files

        try:
            files, _ = list_files(ctx.cwd)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"Could not list the repository: {exc}")
        path = path.strip().strip("/")
        if path:
            files = [f for f in files if f == path or f.startswith(path + "/")]
            if not files:
                return ToolResult.error(f"No files under '{path}'. Use glob to find it.")
        try:
            from coded.ignore import rules_for

            rules = rules_for(ctx.cwd)
            ignored = rules.ignored if rules else None
        except Exception:  # noqa: BLE001
            ignored = None
        findings, truncated = codehealth.scan(ctx.cwd, files, ignored=ignored)
        text = codehealth.render(findings, budget)
        if truncated:
            text += "(The scan was capped; narrow it with `path` for a complete picture.)\n"
        return ToolResult.ok(text)
