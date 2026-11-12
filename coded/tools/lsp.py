"""lsp: code intelligence from the project's language servers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coded.limits import cap_text, get_limits
from coded.tools.base import Tool, ToolContext, ToolResult

POSITION_OPS = ("definition", "references", "hover", "implementation", "type_definition")
OPERATIONS = POSITION_OPS + ("symbols", "workspace_symbols", "diagnostics")
_METHODS = {
    "definition": "textDocument/definition",
    "type_definition": "textDocument/typeDefinition",
    "implementation": "textDocument/implementation",
    "references": "textDocument/references",
    "hover": "textDocument/hover",
}
_MAX_ITEMS = 100
_KINDS = {1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
          7: "property", 8: "field", 9: "constructor", 10: "enum", 11: "interface",
          12: "function", 13: "variable", 14: "constant", 15: "string", 16: "number",
          17: "boolean", 18: "array", 19: "object", 20: "key", 21: "null",
          22: "enum member", 23: "struct", 24: "event", 25: "operator", 26: "type parameter"}
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


class LspTool(Tool):
    name = "lsp"
    parallel_safe = True
    brief = ("Code intelligence from language servers: definition, references, hover, "
             "symbols, diagnostics. Name the symbol and its line; no column counting.")
    description = """Precise code intelligence from the project's language servers (the \
same engine as an IDE). Prefer it to grep when you need the real answer for one symbol:
- definition / type_definition / implementation: where a symbol is defined
- references: every place it is used (before renaming or changing a signature)
- hover: its type, signature and docs
- symbols: the outline of one file; workspace_symbols: find a symbol by name (`query`)
- diagnostics: the type errors and warnings in one file (after an edit)
For the position operations give `path`, the 1-based `line` and the `symbol` name as it \
appears on that line (use `column` only if the name appears twice there). References follow \
types: uses through untyped values (Any, dynamic attributes) can be missed, so confirm with \
grep before a rename."""
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": list(OPERATIONS)},
            "path": {"type": "string", "description": "The file (not needed for workspace_symbols)."},
            "line": {"type": "integer", "description": "1-based line of the symbol."},
            "symbol": {"type": "string", "description": "The name as written on that line."},
            "column": {"type": "integer", "description": "1-based column, if the name repeats."},
            "query": {"type": "string", "description": "workspace_symbols: the name to look for."},
        },
        "required": ["operation"],
    }

    def __init__(self, settings: Any = True):
        self.settings = settings

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from coded.lsp import LSPError, get_manager

        op = str(args.get("operation") or "")
        if op not in OPERATIONS:
            return ToolResult.error(f"`operation` must be one of: {', '.join(OPERATIONS)}.")
        manager = get_manager(ctx.cwd, self.settings)
        try:
            if op == "workspace_symbols":
                text = self._workspace_symbols(manager, args, ctx)
            else:
                path = self._path(args, ctx)
                client = manager.client_for(path)
                if op == "symbols":
                    text = self._symbols(client, path, ctx)
                elif op == "diagnostics":
                    text = self._diagnostics(client, path, ctx)
                else:
                    text = self._at_position(client, op, path, args, ctx)
        except LSPError as exc:
            return ToolResult.error(str(exc))
        except _Refused as exc:
            return ToolResult.error(str(exc))
        return ToolResult.ok(cap_text(text, get_limits(ctx).tool_result_chars,
                                      "ask about one symbol or file at a time"))

    # -- arguments -----------------------------------------------------------------
    @staticmethod
    def _path(args: Dict[str, Any], ctx: ToolContext) -> str:
        raw = str(args.get("path") or "").strip()
        if not raw:
            raise _Refused("Give the file `path`.")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(ctx.cwd) / path
        if not path.is_file():
            raise _Refused(f"No such file: {raw}. Use glob to find it.")
        return str(path.resolve())

    @staticmethod
    def _position(path: str, args: Dict[str, Any]) -> Tuple[int, int]:
        """(0-based line, UTF-16 column) of the symbol the model named."""
        from coded.lsp import utf16_col

        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        try:
            line_no = int(args.get("line"))
        except (TypeError, ValueError):
            raise _Refused("Give the 1-based `line` of the symbol.") from None
        if not 1 <= line_no <= max(len(lines), 1):
            raise _Refused(f"Line {line_no} is outside the file ({len(lines)} lines).")
        text = lines[line_no - 1] if lines else ""
        symbol = str(args.get("symbol") or "").strip()
        if args.get("column"):
            col = max(int(args["column"]) - 1, 0)
        elif symbol:
            m = re.search(r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])", text)
            if m is None:
                m = re.search(re.escape(symbol), text)
            if m is None:
                raise _Refused(f"`{symbol}` is not on line {line_no}, which reads: "
                               f"{text.strip()!r}. Check the line number.")
            col = m.start()
        else:
            raise _Refused("Give the `symbol` name as it appears on that line (or a `column`).")
        return line_no - 1, utf16_col(text, min(col, len(text)))

    # -- operations ----------------------------------------------------------------
    def _at_position(self, client, op: str, path: str, args: Dict[str, Any],
                     ctx: ToolContext) -> str:
        line, col = self._position(path, args)
        uri = client.sync(path)
        params: Dict[str, Any] = {"textDocument": {"uri": uri},
                                  "position": {"line": line, "character": col}}
        if op == "references":
            params["context"] = {"includeDeclaration": True}
        result = client.request(_METHODS[op], params)
        name = args.get("symbol") or f"line {line + 1}"
        if op == "hover":
            text = _hover_text(result)
            return text or f"No hover information for {name}."
        locations = _locations(result)
        if not locations:
            what = {"references": "references", "implementation": "implementations"}.get(
                op, "a definition")
            return f"No {what} found for {name}."
        label = {"references": "reference", "implementation": "implementation"}.get(op, "definition")
        head = f"{len(locations)} {label}{'s' if len(locations) != 1 else ''} of {name}:"
        return head + "\n" + _format_locations(locations, ctx.cwd)

    def _symbols(self, client, path: str, ctx: ToolContext) -> str:
        uri = client.sync(path)
        result = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        lines: List[str] = []
        _symbol_lines(result or [], lines, 0)
        if not lines:
            return f"No symbols in {_rel(path, ctx.cwd)}."
        return f"Symbols in {_rel(path, ctx.cwd)}:\n" + _limit(lines)

    def _workspace_symbols(self, manager, args: Dict[str, Any], ctx: ToolContext) -> str:
        from coded.lsp import LSPError

        query = str(args.get("query") or args.get("symbol") or "").strip()
        if not query:
            raise _Refused("Give the `query` (a symbol name) to look for.")
        anchor = args.get("path")
        if anchor:
            clients = [manager.client_for(self._path(args, ctx))]
        else:  # every language server already running; else the project's main language
            clients = list(manager.clients.values())
            if not clients:
                guess = _first_source_file(ctx.cwd, manager)
                if guess is None:
                    raise _Refused("No language server is running yet: also give a `path` "
                                   "in the language to search.")
                clients = [manager.client_for(guess)]
        lines: List[str] = []
        problems: List[str] = []
        for client in clients:
            if not client.capabilities.get("workspaceSymbolProvider", True):
                problems.append(f"the {client.language} server cannot search the project")
                continue
            try:
                result = client.request("workspace/symbol", {"query": query}) or []
            except LSPError as exc:
                problems.append(f"{client.language}: {exc}")
                continue
            for sym in result:
                loc = sym.get("location") or {}
                path = _uri_path(loc.get("uri", ""))
                start = (loc.get("range") or {}).get("start")
                where = _rel(path, ctx.cwd) + (f":{start['line'] + 1}" if start else "")
                container = f" (in {sym['containerName']})" if sym.get("containerName") else ""
                lines.append(f"{_KINDS.get(sym.get('kind'), 'symbol')} {sym.get('name')}"
                             f"{container}  {where}")
        if not lines and problems:
            raise _Refused("; ".join(problems) + ". Use grep or repo_map to find it instead.")
        if not lines:
            return f"No symbols match {query!r}."
        return f"Symbols matching {query!r}:\n" + _limit(lines)

    def _diagnostics(self, client, path: str, ctx: ToolContext) -> str:
        items = client.file_diagnostics(path)
        rel = _rel(path, ctx.cwd)
        if not items:
            return f"No problems reported in {rel}."
        items = sorted(items, key=lambda d: (d.get("severity") or 9,
                                             d["range"]["start"]["line"]))
        lines = []
        for d in items:
            start = d["range"]["start"]
            source = f" [{d['source']}]" if d.get("source") else ""
            # The first line is the finding; servers like pyright add a long
            # explanation of the type mismatch below it.
            message = str(d.get("message", "")).strip().splitlines() or [""]
            lines.append(f"{rel}:{start['line'] + 1}:{start['character'] + 1} "
                         f"{_SEVERITY.get(d.get('severity'), 'note')}: {message[0]}{source}")
        lines = list(dict.fromkeys(lines))
        return f"{len(lines)} problem(s) in {rel}:\n" + _limit(lines)


class _Refused(Exception):
    pass


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _uri_path(uri: str) -> str:
    from coded.lsp import uri_to_path

    return uri_to_path(uri)


def _rel(path: str, cwd: str) -> str:
    try:
        return Path(path).resolve().relative_to(Path(cwd).resolve()).as_posix()
    except ValueError:
        return path


def _limit(lines: List[str]) -> str:
    if len(lines) > _MAX_ITEMS:
        lines = lines[:_MAX_ITEMS] + [f"… {len(lines) - _MAX_ITEMS} more"]
    return "\n".join(lines)


def _locations(result: Any) -> List[Tuple[str, int, int]]:
    """Location | Location[] | LocationLink[] → (path, 0-based line, utf16 col)."""
    if not result:
        return []
    items = result if isinstance(result, list) else [result]
    out = []
    for item in items:
        uri = item.get("uri") or item.get("targetUri")
        rng = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange")
        if uri and rng:
            out.append((_uri_path(uri), rng["start"]["line"], rng["start"]["character"]))
    return list(dict.fromkeys(out))


def _format_locations(locations: List[Tuple[str, int, int]], cwd: str) -> str:
    from coded.lsp import py_col

    cache: Dict[str, Optional[List[str]]] = {}
    lines = []
    for path, line, col16 in locations:
        if path not in cache:
            try:
                cache[path] = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                cache[path] = None
        src = cache[path]
        text = src[line] if src is not None and line < len(src) else ""
        col = py_col(text, col16) + 1
        snippet = f"  {text.strip()[:160]}" if text.strip() else ""
        lines.append(f"{_rel(path, cwd)}:{line + 1}:{col}{snippet}")
    return _limit(lines)


def _hover_text(result: Any) -> str:
    if not result:
        return ""
    contents = result.get("contents") if isinstance(result, dict) else result
    parts = contents if isinstance(contents, list) else [contents]
    out = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            value = str(part.get("value") or "")
            if part.get("language"):
                value = f"```{part['language']}\n{value}\n```"
            out.append(value)
    return "\n\n".join(p.strip() for p in out if p and p.strip())


_CALLABLE_KINDS = {6, 9, 12}  # method, constructor, function
_VARIABLE_KINDS = {13, 14}    # variable, constant


def _symbol_lines(symbols: list, out: List[str], depth: int, in_callable: bool = False) -> None:
    for sym in symbols:
        if in_callable and sym.get("kind") in _VARIABLE_KINDS:
            continue  # parameters and locals: noise in an outline
        rng = sym.get("selectionRange") or sym.get("range") or (sym.get("location") or {}).get("range")
        where = f"  line {rng['start']['line'] + 1}" if rng else ""
        detail = f"  {sym['detail']}" if sym.get("detail") else ""
        out.append(f"{'  ' * depth}{_KINDS.get(sym.get('kind'), 'symbol')} "
                   f"{sym.get('name')}{detail}{where}")
        _symbol_lines(sym.get("children") or [], out, depth + 1,
                      in_callable or sym.get("kind") in _CALLABLE_KINDS)


def _first_source_file(cwd: str, manager) -> Optional[str]:
    """Some file a configured language server handles, to pick a server by."""
    import os

    from coded.ignore import rules_for

    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", "target"}
    ignore = rules_for(cwd)
    seen = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))
        for name in sorted(files):
            seen += 1
            if seen > 5000:
                return None
            path = os.path.join(root, name)
            if manager.spec_for(path) is not None and not ignore.ignored(path):
                return path
    return None
