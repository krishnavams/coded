"""Reading documents that are not plain text: PDF, Word, Excel, PowerPoint.

Each format is turned into *sections* — PDF pages, slides, sheets, or (for Word,
which has no pages in the file) one section for the body — and each section into
lines of text, so the `read` tool can page through a document exactly as it
pages through a source file, within the same per-model budgets.

The parsers are an optional extra (`pip install 'coded[docs]'`). Without them
the tool says precisely what to install; it never falls back to decoding a
binary file as text, which is how a PDF used to come back as garbage.

What this deliberately does not do, and says so instead of pretending:
* scanned PDFs (pages that are images need OCR);
* charts, pictures and SmartArt inside Office files (their text, if any, only);
* the pre-2007 binary formats (.doc, .xls, .ppt);
* password-protected files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

INSTALL_HINT = "pip install 'coded[docs]'"

# extension -> (kind, the import that has to work, the PyPI package)
FORMATS: Dict[str, Tuple[str, str, str]] = {
    ".pdf": ("pdf", "pypdf", "pypdf"),
    ".docx": ("docx", "docx", "python-docx"),
    ".docm": ("docx", "docx", "python-docx"),
    ".xlsx": ("xlsx", "openpyxl", "openpyxl"),
    ".xlsm": ("xlsx", "openpyxl", "openpyxl"),
    ".pptx": ("pptx", "pptx", "python-pptx"),
    ".pptm": ("pptx", "pptx", "python-pptx"),
    ".ipynb": ("ipynb", "json", "json"),   # Jupyter notebooks: JSON, no extra needed
}

# Formats we recognise but cannot read, with what to do instead.
UNSUPPORTED = {
    ".doc": "the old binary Word format. Save it as .docx, or convert it: "
            "`soffice --headless --convert-to docx file.doc`",
    ".xls": "the old binary Excel format. Save it as .xlsx, or convert it: "
            "`soffice --headless --convert-to xlsx file.xls`",
    ".ppt": "the old binary PowerPoint format. Save it as .pptx, or convert it: "
            "`soffice --headless --convert-to pptx file.ppt`",
    ".odt": "an OpenDocument text file. Convert it to .docx "
            "(`soffice --headless --convert-to docx`) to read it",
    ".ods": "an OpenDocument spreadsheet. Convert it to .xlsx to read it",
    ".odp": "an OpenDocument presentation. Convert it to .pptx to read it",
}

MAX_ROWS_PER_SHEET = 5_000   # a sheet can be a million rows; the read budget still applies
MAX_CELL_CHARS = 300


class DocumentError(Exception):
    """A document that cannot be read, with a message telling the model why."""


@dataclass
class Section:
    title: str          # "page 3", "slide 2: Roadmap", "sheet Sales"
    lines: List[str]


@dataclass
class Document:
    kind: str
    sections: List[Section]
    notes: List[str] = field(default_factory=list)  # caveats worth telling the model

    def unit(self) -> str:
        return {"pdf": "page", "pptx": "slide", "xlsx": "sheet",
                "ipynb": "cell"}.get(self.kind, "section")


def kind_of(path: Path, head: bytes = b"") -> Optional[str]:
    """The document kind for this file, by extension, or by magic for a
    mis-named PDF. None for anything that should be read as text."""
    fmt = FORMATS.get(path.suffix.lower())
    if fmt:
        return fmt[0]
    if head.startswith(b"%PDF-"):
        return "pdf"
    return None


def unsupported_reason(path: Path) -> Optional[str]:
    return UNSUPPORTED.get(path.suffix.lower())


_LIBRARIES = {"ipynb": ("json", "json"),
              "pdf": ("pypdf", "pypdf"), "docx": ("docx", "python-docx"),
              "xlsx": ("openpyxl", "openpyxl"), "pptx": ("pptx", "python-pptx")}


def _require(kind: str):
    module, package = _LIBRARIES[kind]
    try:
        return __import__(module)
    except ImportError:
        raise DocumentError(
            f"Reading {kind.upper()} files needs the `{package}` package, which is not "
            f"installed. The user can add document support with: {INSTALL_HINT}. "
            "Until then, try a command-line converter through bash if one is "
            "available (e.g. `pdftotext`), or ask the user for a text export.") from None


def load(path: Path, kind: str) -> Document:
    """Parse a document into sections. Raises DocumentError, never anything else."""
    reader: Dict[str, Callable[[Path], Document]] = {
        "pdf": _pdf, "docx": _docx, "xlsx": _xlsx, "pptx": _pptx, "ipynb": _ipynb}
    _require(kind)
    try:
        return reader[kind](path)
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - parser libraries raise anything
        raise DocumentError(
            f"Could not parse {path.name} as {kind.upper()}: {type(exc).__name__}: {exc}. "
            "The file may be damaged, or not really that format.") from None


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------
MAX_OUTPUT_LINES = 30


def notebook_source(value) -> str:
    return "".join(value) if isinstance(value, list) else str(value or "")


def _ipynb(path: Path) -> Document:
    """One section per cell: its source, then its text outputs (cut short).
    Images and other rich outputs are named, not shown."""
    import json

    nb = json.loads(path.read_text(encoding="utf-8"))
    sections: List[Section] = []
    for i, cell in enumerate(nb.get("cells") or [], 1):
        kind = cell.get("cell_type", "code")
        cid = cell.get("id")
        title = f"cell {i} [{kind}]" + (f" id={cid}" if cid else "")
        lines = notebook_source(cell.get("source")).splitlines()
        out: List[str] = []
        for o in cell.get("outputs") or []:
            if o.get("output_type") == "stream":
                out += notebook_source(o.get("text")).splitlines()
            elif o.get("output_type") in ("execute_result", "display_data"):
                data = o.get("data") or {}
                if "text/plain" in data:
                    out += notebook_source(data["text/plain"]).splitlines()
                others = [k for k in data if k != "text/plain"]
                if others:
                    out.append(f"<{', '.join(others)} output>")
            elif o.get("output_type") == "error":
                out.append(f"{o.get('ename', 'Error')}: {o.get('evalue', '')}")
        if out:
            if len(out) > MAX_OUTPUT_LINES:
                out = out[:MAX_OUTPUT_LINES] + [f"… ({len(out) - MAX_OUTPUT_LINES} more lines)"]
            lines += ["----- output -----"] + out
        sections.append(Section(title, lines))
    notes = [] if sections else ["the notebook has no cells"]
    if sections:
        notes.append("edit cells with notebook_edit (cell numbers as shown), not edit/write")
    return Document("ipynb", sections, notes)


def _pdf(path: Path) -> Document:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise DocumentError(f"{path.name} is password-protected; ask the user "
                                    "for an unlocked copy.")
        except DocumentError:
            raise
        except Exception:  # noqa: BLE001 - some encryptions need extra packages
            raise DocumentError(f"{path.name} is encrypted and could not be opened; ask "
                                "the user for an unlocked copy.") from None
    sections: List[Section] = []
    empty = 0
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not lose the rest
            text = ""
        lines = [ln.rstrip() for ln in text.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        if not any(ln.strip() for ln in lines):
            empty += 1
            lines = ["(no extractable text on this page — it may be a scanned image)"]
        sections.append(Section(f"page {i}", lines))
    notes: List[str] = []
    if sections and empty == len(sections):
        notes.append("No page has extractable text: this looks like a scanned PDF, "
                     "which needs OCR. Tell the user rather than guessing at its content.")
    elif empty:
        notes.append(f"{empty} of {len(sections)} pages have no extractable text "
                     "(probably images or scans).")
    return Document("pdf", sections, notes)


def _docx(path: Path) -> Document:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    lines: List[str] = []
    # Walk the body in order, so tables appear where they are in the document.
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            if style.lower().startswith("heading"):
                level = "".join(c for c in style if c.isdigit()) or "1"
                lines.append("#" * min(int(level), 6) + " " + text)
            elif style.lower().startswith("list"):
                lines.append("- " + text)
            else:
                lines.append(text)
        elif tag == "tbl":
            lines.extend(_table_lines(
                [[cell.text for cell in row.cells] for row in Table(child, document).rows]))
            lines.append("")
    notes = []
    if not any(ln.strip() for ln in lines):
        notes.append("The document has no text — it may contain only images.")
    return Document("docx", [Section("document", lines)], notes)


def _xlsx(path: Path) -> Document:
    import openpyxl

    # Two views of the same file: the values Excel last calculated, and the
    # formulas. A workbook written by a script and never opened in Excel has no
    # saved results, so a formula cell would read as empty — show the formula
    # itself instead, which is what the model needs to reason about it.
    values = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    formulas = openpyxl.load_workbook(str(path), read_only=True, data_only=False)
    sections: List[Section] = []
    unresolved = 0
    try:
        for vsheet, fsheet in zip(values.worksheets, formulas.worksheets):
            rows: List[List[str]] = []
            truncated = False
            pairs = zip(vsheet.iter_rows(values_only=True), fsheet.iter_rows(values_only=True))
            for i, (vrow, frow) in enumerate(pairs):
                if i >= MAX_ROWS_PER_SHEET:
                    truncated = True
                    break
                cells = []
                for value, formula in zip(vrow, frow):
                    if value is None and isinstance(formula, str) and formula.startswith("="):
                        unresolved += 1
                        cells.append(formula)
                    else:
                        cells.append("" if value is None else _cell(value))
                rows.append(cells)
            while rows and not any(rows[-1]):
                rows.pop()
            width = max((max((j for j, v in enumerate(r) if v), default=-1) + 1
                         for r in rows), default=0)
            rows = [r[:width] for r in rows]
            lines = _table_lines(rows, numbered=True) if rows else ["(empty sheet)"]
            if truncated:
                lines.append(f"… (sheet has more than {MAX_ROWS_PER_SHEET} rows; only the "
                             "first are shown)")
            sections.append(Section(f"sheet {vsheet.title}", lines))
    finally:
        values.close()
        formulas.close()
    notes = []
    if unresolved:
        notes.append(f"{unresolved} formula cell(s) have no saved result (the file was not "
                     "recalculated in Excel); they are shown as the formula, e.g. =B2*2.")
    return Document("xlsx", sections, notes)


def _pptx(path: Path) -> Document:
    from pptx import Presentation

    deck = Presentation(str(path))
    sections: List[Section] = []
    for i, slide in enumerate(deck.slides, 1):
        lines: List[str] = []
        title = ""
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip()
        for shape in _shapes(slide.shapes):
            if shape == slide.shapes.title:
                continue
            if getattr(shape, "has_table", False) and shape.has_table:
                lines.extend(_table_lines(
                    [[cell.text for cell in row.cells] for row in shape.table.rows]))
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        lines.append("  " * para.level + "- " + text)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append("Speaker notes: " + " ".join(notes.split()))
        if not lines:
            lines = ["(no text on this slide)"]
        sections.append(Section(f"slide {i}" + (f": {title}" if title else ""), lines))
    return Document("pptx", sections, [])


def _shapes(shapes):
    """Shapes in reading order, descending into groups."""
    for shape in shapes:
        if getattr(shape, "shape_type", None) == 6:  # MSO_SHAPE_TYPE.GROUP
            yield from _shapes(shape.shapes)
        else:
            yield shape


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cell(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "…"


def _table_lines(rows: Sequence[Sequence[str]], numbered: bool = False) -> List[str]:
    """A table as pipe-separated rows (Markdown-ish, but robust to ragged rows)."""
    out = []
    for i, row in enumerate(rows, 1):
        cells = [" ".join(str(c).split()) for c in row]
        line = " | ".join(cells)
        out.append(f"row {i}: {line}" if numbered else f"| {line} |")
    return out


def parse_ranges(spec: str, count: int) -> List[int]:
    """'3', '2-5', '1,4,7-9' → 0-based indexes within range. Raises ValueError."""
    picked: List[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo = int(lo_s) if lo_s else 1
            hi = int(hi_s) if hi_s else count
        else:
            lo = hi = int(part)
        if lo < 1 or hi < lo:
            raise ValueError(part)
        picked.extend(i - 1 for i in range(lo, min(hi, count) + 1))
    return list(dict.fromkeys(picked))


def render(document: Document, select: Optional[str] = None) -> Tuple[List[str], str]:
    """The document as numbered-ready lines, plus a one-line header.

    `select` picks pages/slides/sheets (1-based, e.g. "3-5"); for a sheet it may
    also be the sheet's name.
    """
    sections = document.sections
    unit = document.unit()
    if select:
        named = [s for s in sections if s.title.lower() == f"{unit} {select}".lower()]
        if named:
            sections = named
        else:
            try:
                idx = parse_ranges(select, len(sections))
            except ValueError:
                raise DocumentError(
                    f"'{select}' is not a valid {unit} selection; use a number, a range "
                    f"like 2-5, or a list like 1,3,7 (there are {len(sections)}).") from None
            if not idx:
                raise DocumentError(f"There are only {len(sections)} {unit}s.")
            sections = [sections[i] for i in idx]
    lines: List[str] = []
    for section in sections:
        if len(document.sections) > 1 or document.kind != "docx":
            lines.append(f"===== {section.title} =====")
        lines.extend(section.lines)
    total = len(document.sections)
    header = (f"[{document.kind.upper()}: {total} {unit}{'s' if total != 1 else ''}"
              + (f", showing {len(sections)}" if select else "") + "]")
    for note in document.notes:
        header += f"\n[note: {note}]"
    return lines, header
