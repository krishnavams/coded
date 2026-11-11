"""Reading PDF, Word, Excel and PowerPoint through the `read` tool.

Every fixture is generated here with the real library (or, for PDF, written by
hand as a minimal valid file), so the tests exercise actual parsing rather than
mocks. Tests that need a parser skip cleanly when the `docs` extra is absent;
the missing-library path is tested separately by hiding the import.
"""

from __future__ import annotations

import builtins

import pytest

from coded import documents
from coded.config import Config
from coded.permissions import PermissionManager
from coded.tools.base import ToolContext
from coded.tools.files import ReadTool


def _read(tmp_path, name, **args):
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True),
                      config=Config(), extra={})
    return ReadTool().run({"path": name, **args}, ctx)


# -- fixtures -------------------------------------------------------------------

def _pdf_bytes(pages):
    """A minimal valid PDF with one line of real text per page (no library)."""
    objects = []
    kids = []
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        kids.append(f"{page_id} 0 R")
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append((page_id,
                        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        f"/Contents {content_id} 0 R "
                        f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()))
        objects.append((content_id, b"<< /Length " + str(len(stream)).encode() + b" >>\n"
                        b"stream\n" + stream + b"\nendstream"))
    objects.insert(0, (2, f"<< /Type /Pages /Kids [{' '.join(kids)}] "
                          f"/Count {len(pages)} >>".encode()))
    objects.insert(0, (1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append((font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objects.sort()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for num in range(1, len(objects) + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


@pytest.fixture
def pdf(tmp_path):
    pytest.importorskip("pypdf")
    path = tmp_path / "report.pdf"
    path.write_bytes(_pdf_bytes(["Quarterly revenue grew", "Costs fell sharply",
                                 "Outlook remains strong"]))
    return path


@pytest.fixture
def docx_file(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Design notes", level=1)
    d.add_paragraph("The service keeps a write-ahead log.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Name", "Owner"
    table.cell(1, 0).text, table.cell(1, 1).text = "ledger", "payments"
    d.add_paragraph("Rollbacks replay the log.")
    path = tmp_path / "notes.docx"
    d.save(path)
    return path


@pytest.fixture
def xlsx_file(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sales = book.active
    sales.title = "Sales"
    sales.append(["Region", "Q1", "Q2"])
    sales.append(["North", 120, 135.0])
    sales.append(["South", 90, 101.5])
    staff = book.create_sheet("Staff")
    staff.append(["Name", "Team"])
    staff.append(["Ada", "Platform"])
    path = tmp_path / "numbers.xlsx"
    book.save(path)
    return path


@pytest.fixture
def pptx_file(tmp_path):
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    s1 = deck.slides.add_slide(deck.slide_layouts[1])
    s1.shapes.title.text = "Roadmap"
    s1.placeholders[1].text_frame.text = "Ship the map"
    s1.notes_slide.notes_text_frame.text = "Mention the deadline."
    s2 = deck.slides.add_slide(deck.slide_layouts[5])
    s2.shapes.title.text = "Numbers"
    from pptx.util import Inches
    table = s2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    table.cell(0, 0).text, table.cell(0, 1).text = "Metric", "Value"
    table.cell(1, 0).text, table.cell(1, 1).text = "Users", "1200"
    path = tmp_path / "deck.pptx"
    deck.save(path)
    return path


# -- each format ------------------------------------------------------------------

def test_a_pdf_is_read_page_by_page(tmp_path, pdf):
    out = _read(tmp_path, "report.pdf")
    assert not out.is_error, out.content
    assert "[PDF: 3 pages]" in out.content
    for page, text in ((1, "Quarterly revenue grew"), (2, "Costs fell sharply")):
        assert f"===== page {page} =====" in out.content and text in out.content


def test_pages_selects_part_of_a_pdf(tmp_path, pdf):
    out = _read(tmp_path, "report.pdf", pages="2-3")
    assert "showing 2" in out.content
    assert "Costs fell sharply" in out.content and "Outlook remains strong" in out.content
    assert "Quarterly revenue grew" not in out.content


def test_a_mis_named_pdf_is_recognised_by_its_magic(tmp_path, pdf):
    (tmp_path / "download.bin").write_bytes(pdf.read_bytes())
    out = _read(tmp_path, "download.bin")
    assert not out.is_error and "Quarterly revenue grew" in out.content


def test_a_word_document_keeps_headings_and_tables_in_order(tmp_path, docx_file):
    out = _read(tmp_path, "notes.docx")
    assert not out.is_error, out.content
    body = out.content
    assert "# Design notes" in body
    assert "| Name | Owner |" in body and "| ledger | payments |" in body
    assert body.index("write-ahead log") < body.index("| Name") < body.index("Rollbacks")


def test_an_excel_workbook_is_read_sheet_by_sheet(tmp_path, xlsx_file):
    out = _read(tmp_path, "numbers.xlsx")
    assert not out.is_error, out.content
    assert "[XLSX: 2 sheets]" in out.content
    assert "===== sheet Sales =====" in out.content
    assert "row 2: North | 120 | 135" in out.content       # 135.0 shown as 135
    assert "row 3: South | 90 | 101.5" in out.content
    assert "===== sheet Staff =====" in out.content


def test_a_sheet_can_be_picked_by_name(tmp_path, xlsx_file):
    out = _read(tmp_path, "numbers.xlsx", pages="Staff")
    assert "Ada | Platform" in out.content and "North" not in out.content


def test_a_presentation_is_read_slide_by_slide_with_notes(tmp_path, pptx_file):
    out = _read(tmp_path, "deck.pptx")
    assert not out.is_error, out.content
    assert "===== slide 1: Roadmap =====" in out.content
    assert "- Ship the map" in out.content
    assert "Speaker notes: Mention the deadline." in out.content
    assert "===== slide 2: Numbers =====" in out.content
    assert "| Users | 1200 |" in out.content


def test_offset_and_limit_page_through_a_long_document(tmp_path, pdf):
    first = _read(tmp_path, "report.pdf", limit=2)
    assert "continue with offset=3" in first.content
    rest = _read(tmp_path, "report.pdf", offset=3)
    assert "Costs fell sharply" in rest.content


# -- what it refuses, and how ------------------------------------------------------

def test_without_the_extra_the_error_names_the_install(tmp_path, monkeypatch, pdf):
    real_import = builtins.__import__

    def no_pypdf(name, *a, **kw):
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError("hidden for the test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    out = _read(tmp_path, "report.pdf")
    assert out.is_error
    assert "pip install 'coded[docs]'" in out.content and "pypdf" in out.content


def test_a_pdf_never_comes_back_as_garbage_text(tmp_path, monkeypatch):
    """The old failure: a PDF without NUL bytes up front decoded as mojibake."""
    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda name, *a, **kw: (_ for _ in ()).throw(ImportError())
                        if name == "pypdf" else real_import(name, *a, **kw))
    (tmp_path / "x.pdf").write_bytes(_pdf_bytes(["hello"]))
    out = _read(tmp_path, "x.pdf")
    assert out.is_error and "%PDF" not in out.content and "obj" not in out.content


def test_old_office_formats_are_refused_with_a_conversion_hint(tmp_path):
    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0 not really")
    out = _read(tmp_path, "old.doc")
    assert out.is_error and "soffice --headless --convert-to docx" in out.content


def test_a_scanned_pdf_says_so_instead_of_returning_nothing(tmp_path):
    pytest.importorskip("pypdf")
    (tmp_path / "scan.pdf").write_bytes(_pdf_bytes([""]))
    out = _read(tmp_path, "scan.pdf")
    assert not out.is_error
    assert "scanned" in out.content and "OCR" in out.content


def test_a_damaged_document_is_an_error_not_a_crash(tmp_path):
    pytest.importorskip("openpyxl")
    (tmp_path / "broken.xlsx").write_bytes(b"PK\x03\x04 this is not a workbook")
    out = _read(tmp_path, "broken.xlsx")
    assert out.is_error and "Could not parse broken.xlsx" in out.content


def test_a_bad_page_selection_is_explained(tmp_path, pdf):
    assert "only 3 pages" in _read(tmp_path, "report.pdf", pages="9").content
    assert "not a valid page selection" in _read(tmp_path, "report.pdf", pages="x").content


def test_pages_on_a_text_file_is_explained(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    out = _read(tmp_path, "a.txt", pages="2")
    assert out.is_error and "offset/limit" in out.content


def test_a_binary_file_without_nul_bytes_is_refused(tmp_path):
    (tmp_path / "blob.dat").write_bytes(bytes(range(128, 256)) * 200)
    out = _read(tmp_path, "blob.dat")
    assert out.is_error and "does not decode as text" in out.content


def test_plain_text_is_read_exactly_as_before(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    out = _read(tmp_path, "a.py")
    assert out.content == "     1\tx = 1\n     2\ty = 2"


def test_page_ranges_parse_like_a_person_writes_them():
    assert documents.parse_ranges("3", 5) == [2]
    assert documents.parse_ranges("2-4", 5) == [1, 2, 3]
    assert documents.parse_ranges("1,4,2-3", 5) == [0, 3, 1, 2]
    assert documents.parse_ranges("4-", 5) == [3, 4]
    assert documents.parse_ranges("2-99", 3) == [1, 2]
    with pytest.raises(ValueError):
        documents.parse_ranges("0", 5)


def test_a_formula_without_a_saved_result_is_shown_as_the_formula(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Price", "Qty", "Total"])
    sheet.append([3, 4, "=A2*B2"])  # written by a script: Excel never computed it
    book.save(tmp_path / "calc.xlsx")
    out = _read(tmp_path, "calc.xlsx")
    assert "row 2: 3 | 4 | =A2*B2" in out.content
    assert "1 formula cell(s) have no saved result" in out.content
