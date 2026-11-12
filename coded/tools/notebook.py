"""notebook_edit: change one cell of a Jupyter notebook.

A notebook is JSON; editing it as text breaks it easily (escaped newlines,
source stored as a list of lines, outputs and execution counts that no longer
match). This tool replaces, inserts or deletes a whole cell and writes valid
JSON back, keeping the notebook's own indentation. Cell numbers are the ones
the `read` tool shows (1-based); a cell `id` works too.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from coded import textio
from coded.tools.base import Tool, ToolContext, ToolResult


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path(ctx.cwd) / p


def _source_lines(text: str) -> List[str]:
    """Notebook source format: a list of lines, each but the last ending in \\n."""
    return text.splitlines(keepends=True)


def _find(cells: List[dict], args: Dict[str, Any]) -> Optional[int]:
    cid = args.get("cell_id")
    if cid:
        for i, cell in enumerate(cells):
            if cell.get("id") == cid:
                return i
        return None
    n = args.get("cell_number")
    if isinstance(n, int) and 1 <= n <= len(cells) + (1 if args.get("edit_mode") == "insert" else 0):
        return n - 1
    return None


def _indent_of(text: str) -> int:
    for line in text.splitlines()[1:3]:
        stripped = line.lstrip(" ")
        if stripped and len(stripped) != len(line):
            return len(line) - len(stripped)
    return 1


class NotebookEditTool(Tool):
    name = "notebook_edit"
    requires_permission = True
    brief = "Replace, insert or delete one cell of a Jupyter notebook (.ipynb)."
    description = """Edit a Jupyter notebook (.ipynb) one cell at a time: replace a \
cell's source, insert a new cell, or delete one. Always use this for notebooks — \
never edit/write, which would corrupt the notebook's JSON. Read the notebook first: \
the read tool shows cells as "cell N [code|markdown] id=…". Replacing a code cell \
clears its outputs and execution count (they belonged to the old code)."""
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The .ipynb file."},
            "cell_number": {"type": "integer",
                            "description": "1-based cell number as the read tool shows it. "
                                           "For insert: the new cell's position "
                                           "(cells from there move down)."},
            "cell_id": {"type": "string", "description": "The cell's id, instead of a number."},
            "new_source": {"type": "string",
                           "description": "The cell's full new source (not used for delete)."},
            "cell_type": {"type": "string", "enum": ["code", "markdown"],
                          "description": "For insert (default code), or to change a cell's type."},
            "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"],
                          "description": "Default replace."},
        },
        "required": ["path"],
    }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        where = args.get("cell_id") or args.get("cell_number")
        return f"{args.get('edit_mode', 'replace')} cell {where} in {args.get('path')}"

    def permission_target(self, args: Dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, str(args.get("path", "")))
        if p.suffix.lower() != ".ipynb":
            return ToolResult.error(f"{p.name} is not a notebook (.ipynb); use edit for other files.")
        if not p.is_file():
            return ToolResult.error(f"Notebook not found: {p}")
        try:
            raw, newline = textio.read_text(p)
        except UnicodeDecodeError:
            return ToolResult.error(f"{p.name} {textio.NOT_UTF8}")
        except OSError as exc:
            return ToolResult.error(f"Could not read {p.name}: {exc}")
        try:
            nb = json.loads(raw)
        except ValueError as exc:
            return ToolResult.error(f"Could not read {p.name} as a notebook: {exc}")
        cells = nb.get("cells")
        if not isinstance(cells, list):
            return ToolResult.error(f"{p.name} has no cell list; is it a notebook?")
        mode = args.get("edit_mode") or "replace"
        if mode not in ("replace", "insert", "delete"):
            return ToolResult.error("edit_mode must be replace, insert or delete.")
        index = _find(cells, args)
        if index is None:
            return ToolResult.error(
                f"No such cell (the notebook has {len(cells)}). Give cell_number "
                f"1-{len(cells) + (1 if mode == 'insert' else 0)} or an existing cell_id.")
        if mode != "delete" and "new_source" not in args:
            return ToolResult.error("new_source is required to replace or insert a cell.")

        if ctx.checkpoints is not None:
            ctx.checkpoints.record(p)
        if mode == "delete":
            gone = cells.pop(index)
            note = f"Deleted cell {index + 1} ({gone.get('cell_type', 'code')})."
        elif mode == "insert":
            kind = args.get("cell_type") or "code"
            cell: Dict[str, Any] = {"cell_type": kind, "metadata": {},
                                    "source": _source_lines(args["new_source"])}
            if nb.get("nbformat", 4) >= 4 and nb.get("nbformat_minor", 0) >= 5:
                cell["id"] = uuid.uuid4().hex[:8]
            if kind == "code":
                cell.update(execution_count=None, outputs=[])
            cells.insert(index, cell)
            note = f"Inserted a {kind} cell at position {index + 1}."
        else:
            cell = cells[index]
            cell["source"] = _source_lines(args["new_source"])
            kind = args.get("cell_type")
            if kind and kind != cell.get("cell_type"):
                cell["cell_type"] = kind
                if kind == "markdown":
                    cell.pop("outputs", None)
                    cell.pop("execution_count", None)
            if cell.get("cell_type") == "code":
                cell["outputs"] = []
                cell["execution_count"] = None
            note = f"Replaced cell {index + 1} ({cell.get('cell_type', 'code')})."
        text = json.dumps(nb, indent=_indent_of(raw), ensure_ascii=False) + "\n"
        try:
            textio.write_text(p, text, newline)
        except OSError as exc:
            return ToolResult.error(f"Could not write {p}: {exc}")
        return ToolResult.ok(f"{note} The notebook now has {len(cells)} cell(s).")
