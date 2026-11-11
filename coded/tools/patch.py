"""Batch editing tools: multi_edit (several replacements in one file) and
apply_patch (a multi-file patch in the Codex "*** Begin Patch" format)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coded.textio import NOT_UTF8, newline_of, read_text, write_text
from coded.tools.base import Tool, ToolContext, ToolResult
from coded.tools.files import _checkpoint, _resolve, _unified_diff


class PatchError(Exception):
    pass


# ---------------------------------------------------------------------------
# multi_edit
# ---------------------------------------------------------------------------
def apply_edits(text: str, edits: List[Dict[str, Any]]) -> Tuple[str, int]:
    """Apply edits in order; all-or-nothing. Returns (new_text, replacements)."""
    total = 0
    for i, e in enumerate(edits):
        old, new = e.get("old_string"), e.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise PatchError(f"edits[{i}] needs string old_string and new_string.")
        if old == new:
            raise PatchError(f"edits[{i}]: old_string and new_string are identical.")
        if old == "":
            raise PatchError(f"edits[{i}]: old_string is empty; use write to create files.")
        count = text.count(old)
        if count == 0:
            raise PatchError(
                f"edits[{i}]: old_string not found (after applying the earlier edits). "
                "Read the file and match its exact text.")
        if count > 1 and not e.get("replace_all"):
            raise PatchError(
                f"edits[{i}]: old_string matches {count} times; add context or set replace_all.")
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
        total += count if e.get("replace_all") else 1
    return text, total


class MultiEditTool(Tool):
    name = "multi_edit"
    brief = 'Several exact-string replacements in one file, applied in order; all or nothing. Same matching rules as edit.'
    description = """Make several exact-string replacements in ONE file in a single, atomic \
call. Edits apply in order (each sees the previous one's result); if any edit fails, none \
are applied. Same matching rules as `edit`. Prefer this over repeated `edit` calls on the \
same file."""
    requires_permission = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to edit."},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        return f"Edit {args.get('path')} ({len(args.get('edits') or [])} change(s))"

    def permission_target(self, args: Dict[str, Any]) -> str:
        return args.get("path", "")

    def preview(self, args: Dict[str, Any], ctx: ToolContext):
        p = _resolve(ctx, args["path"])
        try:
            text = read_text(p)[0]
            new, _ = apply_edits(text, args.get("edits") or [])
        except (OSError, PatchError, UnicodeDecodeError):
            return None
        return _unified_diff(text, new, args["path"])

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args["path"])
        if not p.is_file():
            return ToolResult.error(f"File not found: {p}")
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            return ToolResult.error("`edits` must be a non-empty array.")
        try:
            text, newline = read_text(p)
            new, n = apply_edits(text, edits)
        except PatchError as exc:
            return ToolResult.error(f"{exc} No changes were made.")
        except UnicodeDecodeError:
            return ToolResult.error(f"{p} {NOT_UTF8}")
        except OSError as exc:
            return ToolResult.error(f"Could not read {p}: {exc}")
        try:
            _checkpoint(ctx, p)
            write_text(p, new, newline)
        except OSError as exc:
            return ToolResult.error(f"Could not write {p}: {exc}")
        return ToolResult.ok(f"Edited {p} ({n} replacement(s) from {len(edits)} edit(s)).")


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------
@dataclass
class FileOp:
    kind: str  # add | delete | update
    path: str
    move_to: Optional[str] = None
    lines: List[str] = field(default_factory=list)  # add: content; update: hunk body


def parse_patch(patch: str) -> List[FileOp]:
    lines = patch.strip("\n").splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise PatchError("Patch must start with '*** Begin Patch'.")
    if lines[-1].strip() != "*** End Patch":
        raise PatchError("Patch must end with '*** End Patch'.")
    ops: List[FileOp] = []
    cur: Optional[FileOp] = None
    for line in lines[1:-1]:
        if line.startswith("*** Add File: "):
            cur = FileOp("add", line[len("*** Add File: "):].strip())
            ops.append(cur)
        elif line.startswith("*** Delete File: "):
            cur = FileOp("delete", line[len("*** Delete File: "):].strip())
            ops.append(cur)
        elif line.startswith("*** Update File: "):
            cur = FileOp("update", line[len("*** Update File: "):].strip())
            ops.append(cur)
        elif line.startswith("*** Move to: "):
            if cur is None or cur.kind != "update":
                raise PatchError("'*** Move to:' must follow '*** Update File:'.")
            cur.move_to = line[len("*** Move to: "):].strip()
        elif line.strip() == "*** End of File":
            continue
        elif cur is None:
            if line.strip():
                raise PatchError(f"Unexpected line before any file header: {line!r}")
        elif cur.kind == "add":
            if not line.startswith("+"):
                raise PatchError(f"Lines in an added file must start with '+': {line!r}")
            cur.lines.append(line[1:])
        elif cur.kind == "delete":
            if line.strip():
                raise PatchError(f"Unexpected content after '*** Delete File': {line!r}")
        else:
            cur.lines.append(line)
    if not ops:
        raise PatchError("Patch contains no file operations.")
    return ops


def _hunks(body: List[str]) -> List[Tuple[Optional[str], List[str]]]:
    hunks: List[Tuple[Optional[str], List[str]]] = []
    header: Optional[str] = None
    cur: List[str] = []
    for line in body:
        if line.startswith("@@"):
            if cur:
                hunks.append((header, cur))
            header = line[2:].strip() or None
            cur = []
        else:
            cur.append(line)
    if cur or header:
        hunks.append((header, cur))
    return hunks


def _find(haystack: List[str], needle: List[str], start: int) -> int:
    """Index of `needle` in `haystack` at/after `start`; exact, then whitespace-tolerant."""
    if not needle:
        return start
    for norm in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
        target = [norm(x) for x in needle]
        for i in range(start, len(haystack) - len(needle) + 1):
            if [norm(x) for x in haystack[i:i + len(needle)]] == target:
                return i
    return -1


def apply_update(text: str, body: List[str], path: str) -> str:
    trailing_nl = text.endswith("\n")
    lines = text.splitlines()
    pos = 0
    for header, hunk in _hunks(body):
        old: List[str] = []
        new: List[str] = []
        for raw in hunk:
            tag, content = (raw[:1], raw[1:]) if raw else (" ", "")
            if tag == "-":
                old.append(content)
            elif tag == "+":
                new.append(content)
            elif tag == " ":
                old.append(content)
                new.append(content)
            else:
                raise PatchError(f"{path}: hunk lines must start with ' ', '-' or '+': {raw!r}")
        if header:
            h = _find(lines, [header], pos)
            if h < 0:
                raise PatchError(f"{path}: context line not found: {header!r}")
            pos = h + 1
        if not old:
            at = pos if header else len(lines)
            lines[at:at] = new
            pos = at + len(new)
            continue
        idx = _find(lines, old, pos)
        if idx < 0:
            idx = _find(lines, old, 0)
        if idx < 0:
            preview = "\n".join(old[:6])
            raise PatchError(f"{path}: could not find the lines to replace:\n{preview}")
        lines[idx:idx + len(old)] = new
        pos = idx + len(new)
    out = "\n".join(lines)
    return out + "\n" if (trailing_nl or not text) and out else out


def plan_patch(patch: str, ctx: ToolContext) -> List[Tuple[FileOp, Path, Optional[str], Optional[str]]]:
    """Validate the whole patch; returns (op, path, old_text, new_text) per file."""
    planned = []
    for op in parse_patch(patch):
        p = _resolve(ctx, op.path)
        if op.kind == "add":
            if p.exists():
                raise PatchError(f"{op.path} already exists; use '*** Update File:' instead.")
            content = "\n".join(op.lines) + ("\n" if op.lines else "")
            planned.append((op, p, None, content))
        elif op.kind == "delete":
            if not p.is_file():
                raise PatchError(f"Cannot delete {op.path}: file not found.")
            planned.append((op, p, _text(p), None))
        else:
            if not p.is_file():
                raise PatchError(f"Cannot update {op.path}: file not found.")
            old = _text(p)
            planned.append((op, p, old, apply_update(old, op.lines, op.path)))
    return planned


def _text(p) -> str:
    try:
        return read_text(p)[0]
    except UnicodeDecodeError:
        raise PatchError(f"{p} {NOT_UTF8}") from None


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = """Apply a patch that adds, updates, deletes or renames one or more files \
atomically (nothing is written unless every hunk applies). Format:

*** Begin Patch
*** Add File: path/new.py
+line of the new file
*** Update File: path/existing.py
*** Move to: path/renamed.py        (optional)
@@ def some_function():              (optional line to anchor the hunk)
 unchanged context line
-line to remove
+line to add
*** Delete File: path/old.py
*** End Patch

Include ~3 lines of unchanged context around each change so the location is unique."""
    requires_permission = True
    parameters = {
        "type": "object",
        "properties": {"patch": {"type": "string", "description": "The full patch text."}},
        "required": ["patch"],
    }

    @staticmethod
    def _paths(args: Dict[str, Any]) -> List[str]:
        try:
            ops = parse_patch(args.get("patch", ""))
        except PatchError:
            return []
        return [op.path for op in ops] + [op.move_to for op in ops if op.move_to]

    def permission_detail(self, args: Dict[str, Any]) -> str:
        paths = self._paths(args)
        return "Apply patch to " + (", ".join(paths) if paths else "(unparseable patch)")

    def permission_target(self, args: Dict[str, Any]) -> str:
        return " ".join(self._paths(args))

    def preview(self, args: Dict[str, Any], ctx: ToolContext):
        try:
            planned = plan_patch(args.get("patch", ""), ctx)
        except (PatchError, OSError):
            return None
        return "\n".join(
            _unified_diff(old or "", new or "", op.move_to or op.path) for op, _, old, new in planned)

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            planned = plan_patch(args.get("patch", ""), ctx)
        except PatchError as exc:
            return ToolResult.error(f"Patch not applied: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Patch not applied, could not read a file: {exc}")
        summary = []
        try:
            for op, p, _old, new in planned:
                dest = _resolve(ctx, op.move_to) if op.move_to else p
                _checkpoint(ctx, p, *([dest] if dest != p else []))
                if op.kind == "delete":
                    p.unlink()
                    summary.append(f"D {op.path}")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                write_text(dest, new or "", newline_of(p if p.exists() else dest))
                if dest != p:
                    p.unlink()
                    summary.append(f"R {op.path} -> {op.move_to}")
                else:
                    summary.append(("A " if op.kind == "add" else "M ") + op.path)
        except OSError as exc:
            return ToolResult.error(f"Patch partially applied ({'; '.join(summary)}); failed: {exc}. "
                                    "Use /undo to roll back.")
        return ToolResult.ok("Applied patch:\n" + "\n".join(summary))
