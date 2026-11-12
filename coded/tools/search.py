"""Search tools: glob (filename patterns) and grep (content search)."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from coded.limits import get_limits
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


def _walk(root: Path, cwd: str = ""):
    """Files under root, skipping dependency/VCS folders and whatever the
    project's .codedignore excludes."""
    from coded.ignore import rules_for

    rules = rules_for(cwd) if cwd else None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS
                       and not (rules and rules.ignored(Path(dirpath) / d))]
        for fn in filenames:
            f = Path(dirpath) / fn
            if rules and rules.ignored(f):
                continue
            yield f


class GlobTool(Tool):
    name = "glob"
    parallel_safe = True
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
        max_matches = get_limits(ctx).grep_matches
        root = _resolve(ctx, args.get("path", "."))
        pattern = args["pattern"]
        matches: List[str] = []
        try:
            for f in _walk(root, ctx.cwd):
                rel = f.relative_to(root).as_posix()
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                    matches.append(str(f))
                    if len(matches) >= max_matches:
                        break
        except OSError as exc:
            return ToolResult.error(f"Search failed: {exc}")
        if not matches:
            return ToolResult.ok(f"No files matching '{pattern}' under {root}.")
        matches.sort()
        capped = "" if len(matches) < max_matches else f"\n… (capped at {max_matches})"
        return ToolResult.ok("\n".join(matches) + capped)


# A file this big is data or a log, not source; reading it whole (grep holds
# the bytes, the decoded text and the split lines at once) could exhaust memory.
GREP_MAX_FILE_BYTES = 20 * 1024 * 1024


class GrepTool(Tool):
    name = "grep"
    parallel_safe = True
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
        max_matches = get_limits(ctx).grep_matches
        root = _resolve(ctx, args.get("path", "."))
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            rx = re.compile(args["pattern"], flags)
        except re.error as exc:
            return ToolResult.error(f"Invalid regex: {exc}")
        file_glob = args.get("glob")
        results: List[str] = []
        skipped_big: List[Path] = []
        files_with_matches = 0
        try:
            for f in _walk(root, ctx.cwd):
                if file_glob and not (
                    fnmatch.fnmatch(f.name, file_glob)
                    or fnmatch.fnmatch(f.relative_to(root).as_posix(), file_glob)
                ):
                    continue
                try:
                    if f.stat().st_size > GREP_MAX_FILE_BYTES:
                        skipped_big.append(f)
                        continue
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
                        if len(results) >= max_matches:
                            break
                if had_match:
                    files_with_matches += 1
                if len(results) >= max_matches:
                    break
        except OSError as exc:
            return ToolResult.error(f"Search failed: {exc}")
        skipped = ""
        if skipped_big:
            names = ", ".join(str(p) for p in skipped_big[:5])
            more = f" and {len(skipped_big) - 5} more" if len(skipped_big) > 5 else ""
            skipped = (f"\n(skipped {len(skipped_big)} file(s) over "
                       f"{GREP_MAX_FILE_BYTES // (1024 * 1024)} MB: {names}{more}; "
                       f"read them with `read` and offset/limit if needed)")
        if not results:
            return ToolResult.ok(f"No matches for /{args['pattern']}/ under {root}.{skipped}")
        capped = "" if len(results) < max_matches else f"\n… (capped at {max_matches} matches)"
        header = f"{len(results)} match(es) in {files_with_matches} file(s):\n"
        return ToolResult.ok(header + "\n".join(results) + capped + skipped)
