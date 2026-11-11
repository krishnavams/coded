"""File tools: read, write, edit, ls."""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any, Dict

from coded.limits import get_limits
from coded.textio import NOT_UTF8, newline_of, read_text, write_text
from coded.tools.base import Tool, ToolContext, ToolResult


def _unified_diff(old: str, new: str, path: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    )
    return "\n".join(diff)

_MAX_READ_LINES = 2000
_MAX_LINE_LEN = 2000


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(ctx.cwd) / p
    return p


def _checkpoint(ctx: ToolContext, *paths: Path) -> None:
    if ctx.checkpoints is not None:
        for p in paths:
            ctx.checkpoints.record(p)


class ReadTool(Tool):
    name = "read"
    brief = 'Read a file with line numbers; use offset/limit for large files. Also reads PDF, .docx, .xlsx and .pptx; `pages` picks pages, slides or sheets ("3", "2-5", a sheet name).'
    parallel_safe = True
    description = """Read a file from the filesystem. Returns the file contents with \
1-indexed line numbers in `cat -n` format. Use `offset` and `limit` for large files. \
Prefer this over `cat`/`head` via the shell.

Also reads documents as text: PDF (per page), Word .docx (headings, paragraphs and \
tables in order), Excel .xlsx (each sheet as numbered rows of cell values) and \
PowerPoint .pptx (per slide, with speaker notes). For those, `pages` picks pages, \
slides or sheets — "3", "2-5", "1,4" or a sheet name — before `offset`/`limit` apply."""
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file (absolute or relative to cwd)."},
            "offset": {"type": "integer", "description": "1-indexed line to start at."},
            "limit": {"type": "integer", "description": "Max number of lines to read."},
            "pages": {"type": "string",
                      "description": "Documents only: pages/slides/sheets to read, e.g. "
                                     "'3', '2-5', '1,4,9' or a sheet name."},
        },
        "required": ["path"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args["path"])
        if not p.exists():
            return ToolResult.error(f"File not found: {p}")
        if p.is_dir():
            return ToolResult.error(f"{p} is a directory; use ls or glob.")
        try:
            data = p.read_bytes()
        except OSError as exc:
            return ToolResult.error(f"Could not read {p}: {exc}")
        from coded import documents

        header = ""
        kind = documents.kind_of(p, data[:8])
        if kind is not None:
            try:
                doc = documents.load(p, kind)
                lines, header = documents.render(doc, args.get("pages"))
            except documents.DocumentError as exc:
                return ToolResult.error(str(exc))
        else:
            reason = documents.unsupported_reason(p)
            if reason:
                return ToolResult.error(f"{p.name} is {reason}.")
            if b"\x00" in data[:4096]:
                return ToolResult.error(f"{p} appears to be a binary file.")
            text = data.decode("utf-8", errors="replace")
            # Not text after all (a binary format without NUL bytes up front):
            # refuse rather than hand the model a page of replacement characters.
            sample = text[:20_000]
            if sample and sample.count("\ufffd") > max(8, len(sample) // 50):
                return ToolResult.error(
                    f"{p} does not decode as text — it is a binary format this tool "
                    "cannot read.")
            if args.get("pages"):
                return ToolResult.error("`pages` applies to PDF, Word, Excel and "
                                        "PowerPoint files; use offset/limit for text.")
            lines = text.splitlines()
        offset = max(1, int(args.get("offset", 1)))
        cap = get_limits(ctx).read_lines
        limit = max(1, min(int(args.get("limit", cap)), cap))
        chunk = lines[offset - 1 : offset - 1 + limit]
        if not chunk:
            return ToolResult.ok((header + "\n" if header else "")
                                 + f"(file has {len(lines)} lines; offset {offset} is past the end)")
        out = []
        budget = get_limits(ctx).tool_result_chars - 200
        used = 0
        last = offset - 1
        for i, line in enumerate(chunk, start=offset):
            if len(line) > _MAX_LINE_LEN:
                line = line[:_MAX_LINE_LEN] + "… (truncated)"
            row = f"{i:6d}\t{line}"
            if out and used + len(row) + 1 > budget:
                break  # stop at a whole line within this model's result budget
            out.append(row)
            used += len(row) + 1
            last = i
        footer = ""
        if last < len(lines):
            footer = f"\n… ({len(lines) - last} more lines; continue with offset={last + 1})"
        return ToolResult.ok((header + "\n" if header else "") + "\n".join(out) + footer)


class WriteTool(Tool):
    name = "write"
    description = """Write content to a file, creating it (and parent directories) or \
overwriting it entirely. For small edits to an existing file, prefer `edit`."""
    requires_permission = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to write (absolute or relative to cwd)."},
            "content": {"type": "string", "description": "Full file contents to write."},
        },
        "required": ["path", "content"],
    }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        n = len(args.get("content", ""))
        return f"Write {n} bytes to {args.get('path')}"

    def permission_target(self, args: Dict[str, Any]) -> str:
        return args.get("path", "")

    def preview(self, args: Dict[str, Any], ctx: ToolContext):
        p = _resolve(ctx, args["path"])
        old = ""
        if p.is_file():
            try:
                old = read_text(p)[0]
            except (OSError, UnicodeDecodeError):
                return None
        return _unified_diff(old, args.get("content", ""), args["path"])

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args["path"])
        try:
            _checkpoint(ctx, p)
            p.parent.mkdir(parents=True, exist_ok=True)
            existed = p.exists()
            write_text(p, args["content"], newline_of(p))
        except OSError as exc:
            return ToolResult.error(f"Could not write {p}: {exc}")
        verb = "Updated" if existed else "Created"
        return ToolResult.ok(f"{verb} {p} ({len(args['content'])} bytes).")


class EditTool(Tool):
    name = "edit"
    description = """Replace an exact string in a file. `old_string` must match the file \
exactly (including whitespace/indentation) and be unique, unless `replace_all` is true. \
Read the file first so your match is accurate."""
    requires_permission = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit."},
            "old_string": {"type": "string", "description": "Exact text to find."},
            "new_string": {"type": "string", "description": "Text to replace it with."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence instead of requiring a unique match.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        return f"Edit {args.get('path')}"

    def permission_target(self, args: Dict[str, Any]) -> str:
        return args.get("path", "")

    def preview(self, args: Dict[str, Any], ctx: ToolContext):
        p = _resolve(ctx, args["path"])
        if not p.is_file():
            return None
        try:
            text = read_text(p)[0]
        except (OSError, UnicodeDecodeError):
            return None
        old, new = args.get("old_string", ""), args.get("new_string", "")
        if old not in text:
            return None
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        return _unified_diff(text, updated, args["path"])

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args["path"])
        if not p.exists():
            return ToolResult.error(f"File not found: {p}")
        old = args["old_string"]
        new = args["new_string"]
        if old == new:
            return ToolResult.error("old_string and new_string are identical.")
        try:
            text, newline = read_text(p)
        except UnicodeDecodeError:
            return ToolResult.error(f"{p} {NOT_UTF8}")
        except OSError as exc:
            return ToolResult.error(f"Could not read {p}: {exc}")
        count = text.count(old)
        if count == 0:
            return ToolResult.error(
                "old_string not found in the file. Read the file and match its exact text."
            )
        if count > 1 and not args.get("replace_all"):
            return ToolResult.error(
                f"old_string is not unique ({count} matches). Add surrounding context "
                f"to make it unique, or set replace_all=true."
            )
        new_text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        try:
            _checkpoint(ctx, p)
            write_text(p, new_text, newline)
        except OSError as exc:
            return ToolResult.error(f"Could not write {p}: {exc}")
        return ToolResult.ok(f"Edited {p} ({count if args.get('replace_all') else 1} replacement(s)).")


class LsTool(Tool):
    name = "ls"
    parallel_safe = True
    description = "List the entries of a directory (non-recursive). Defaults to the working directory."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (default: cwd)."},
        },
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args.get("path", "."))
        if not p.exists():
            return ToolResult.error(f"Not found: {p}")
        if not p.is_dir():
            return ToolResult.error(f"{p} is not a directory.")
        entries = []
        try:
            for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
                if child.name.startswith(".") and child.name not in (".env", ".coded"):
                    pass  # still show, but dotfiles are common; keep them
                suffix = "/" if child.is_dir() else ""
                entries.append(f"{child.name}{suffix}")
        except OSError as exc:
            return ToolResult.error(f"Could not list {p}: {exc}")
        if not entries:
            return ToolResult.ok(f"{p} is empty.")
        return ToolResult.ok(f"{p}:\n" + "\n".join(entries))


class MoveTool(Tool):
    name = "move"
    description = "Move or rename a file or directory. Parent directories are created as needed."
    requires_permission = True
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Existing path."},
            "destination": {"type": "string", "description": "New path."},
        },
        "required": ["source", "destination"],
    }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        return f"Move {args.get('source')} -> {args.get('destination')}"

    def permission_target(self, args: Dict[str, Any]) -> str:
        return args.get("source", "")

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        src = _resolve(ctx, args["source"])
        dst = _resolve(ctx, args["destination"])
        if not src.exists():
            return ToolResult.error(f"Source not found: {src}")
        if dst.exists():
            return ToolResult.error(f"Destination already exists: {dst}")
        try:
            _checkpoint(ctx, src, dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except OSError as exc:
            return ToolResult.error(f"Could not move: {exc}")
        return ToolResult.ok(f"Moved {src} -> {dst}.")


class DeleteTool(Tool):
    name = "delete"
    description = "Delete a file (or an empty directory). Use with care; recoverable via /undo for files."
    requires_permission = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to delete."},
        },
        "required": ["path"],
    }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        return f"Delete {args.get('path')}"

    def permission_target(self, args: Dict[str, Any]) -> str:
        return args.get("path", "")

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args["path"])
        if not p.exists():
            return ToolResult.error(f"Not found: {p}")
        try:
            if p.is_dir():
                p.rmdir()  # only succeeds if empty
            else:
                _checkpoint(ctx, p)
                p.unlink()
        except OSError as exc:
            return ToolResult.error(f"Could not delete {p}: {exc}")
        return ToolResult.ok(f"Deleted {p}.")
