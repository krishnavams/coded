"""Search tools: glob (filename patterns) and grep (content search)."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from coded.tools.base import Tool, ToolContext, ToolResult

# Directories we never descend into during content/file search.
_IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", ".idea", ".tox", "target",
}
_MAX_MATCHES = 200


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(ctx.cwd) / p
    return p


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fn in filenames:
            yield Path(dirpath) / fn


class GlobTool(Tool):
    name = "glob"
    description = """Find files by glob pattern (e.g. `**/*.py`, `src/**/*.ts`). Returns \
matching paths. Fast; use this to locate files by name."""
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'."},
            "path": {"type": "string", "description": "Root directory to search (default: cwd)."},
        },
        "required": ["pattern"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = _resolve(ctx, args.get("path", "."))
        pattern = args["pattern"]
        matches: List[str] = []
        try:
            for f in _walk(root):
                rel = f.relative_to(root).as_posix()
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                    matches.append(str(f))
                    if len(matches) >= _MAX_MATCHES:
                        break
        except OSError as exc:
            return ToolResult.error(f"Search failed: {exc}")
        if not matches:
            return ToolResult.ok(f"No files matching '{pattern}' under {root}.")
        matches.sort()
        capped = "" if len(matches) < _MAX_MATCHES else f"\n… (capped at {_MAX_MATCHES})"
        return ToolResult.ok("\n".join(matches) + capped)


class GrepTool(Tool):
    name = "grep"
    description = """Search file contents with a regular expression. Returns matching \
lines as `path:line: text`. Filter files with `glob` (e.g. '*.py'). Prefer this over \
shell grep."""
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regular expression."},
            "path": {"type": "string", "description": "Root directory to search (default: cwd)."},
            "glob": {"type": "string", "description": "Only search files matching this glob (e.g. '*.py')."},
            "ignore_case": {"type": "boolean", "description": "Case-insensitive match."},
        },
        "required": ["pattern"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = _resolve(ctx, args.get("path", "."))
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            rx = re.compile(args["pattern"], flags)
        except re.error as exc:
            return ToolResult.error(f"Invalid regex: {exc}")
        file_glob = args.get("glob")
        results: List[str] = []
        files_with_matches = 0
        try:
            for f in _walk(root):
                if file_glob and not (
                    fnmatch.fnmatch(f.name, file_glob)
                    or fnmatch.fnmatch(f.relative_to(root).as_posix(), file_glob)
                ):
                    continue
                try:
                    data = f.read_bytes()
                except OSError:
                    continue
                if b"\x00" in data[:2048]:
                    continue
                had_match = False
                for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if rx.search(line):
                        had_match = True
                        text = line if len(line) <= 300 else line[:300] + "…"
                        results.append(f"{f}:{lineno}: {text}")
                        if len(results) >= _MAX_MATCHES:
                            break
                if had_match:
                    files_with_matches += 1
                if len(results) >= _MAX_MATCHES:
                    break
        except OSError as exc:
            return ToolResult.error(f"Search failed: {exc}")
        if not results:
            return ToolResult.ok(f"No matches for /{args['pattern']}/ under {root}.")
        capped = "" if len(results) < _MAX_MATCHES else f"\n… (capped at {_MAX_MATCHES} matches)"
        header = f"{len(results)} match(es) in {files_with_matches} file(s):\n"
        return ToolResult.ok(header + "\n".join(results) + capped)
