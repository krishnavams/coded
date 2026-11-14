"""Build docs/coded-documentation.pdf from the repository's Markdown docs.

    pip install markdown pygments
    python docs/build_pdf.py

Markdown → one HTML file (python-markdown) → PDF printed by Chromium through
Node Playwright (`require('playwright')`), so tables and code blocks render as
on GitHub. The PDF gets a cover, a linked contents page, bookmarks and page
numbers. Re-run it after the docs change.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "coded-documentation.pdf"

# (part title, source file, one-line summary) in reading order.
PARTS = [
    ("User Guide", "README.md",
     "Install, configure, and use coded: models, tools, the REPL and TUI, skills, "
     "plugins, reviews, missions and more."),
    ("Writing Skills", "examples/skills/README.md",
     "How to write and install skill packs."),
    ("Writing Plugins", "examples/plugins/README.md",
     "The plugin layout: skills, commands, hooks and MCP servers in one package."),
    ("Example Configuration", "config.example.json",
     "The annotated sample configuration (config.example.json)."),
    ("Developer Guide", "CLAUDE.md",
     "Architecture, module map and conventions for working on coded itself."),
    ("Changelog", "CHANGELOG.md", "What changed in each release."),
]

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
html { font-family: "DejaVu Sans", "Liberation Sans", sans-serif; font-size: 10pt;
       line-height: 1.5; color: #1f2330; }
body { margin: 0; }
a { color: #6d28d9; text-decoration: none; }
h1, h2, h3, h4, h5, h6 { color: #2e1065; line-height: 1.25; page-break-after: avoid;
                         break-after: avoid; }
h1.part { font-size: 26pt; margin: 0 0 6pt; padding-bottom: 8pt;
          border-bottom: 3px solid #7c3aed; break-before: page; }
p.part-summary { color: #5b6170; font-size: 11pt; margin-bottom: 18pt; }
h2 { font-size: 16pt; margin-top: 22pt; border-bottom: 1px solid #ddd6fe;
     padding-bottom: 3pt; }
h3 { font-size: 13pt; margin-top: 16pt; }
h4 { font-size: 11pt; margin-top: 12pt; }
h5, h6 { font-size: 10pt; }
code { font-family: "DejaVu Sans Mono", "Liberation Mono", monospace; font-size: 8.6pt;
       background: #f3f0ff; padding: 0 2pt; border-radius: 3px; }
pre { background: #f7f7fb; border: 1px solid #e4e1f5; border-left: 3px solid #a78bfa;
      border-radius: 4px; padding: 7pt 9pt; white-space: pre-wrap; word-break: break-word;
      font-size: 8.4pt; line-height: 1.4; break-inside: avoid-page; }
pre code { background: none; padding: 0; font-size: inherit; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 8.8pt; }
th, td { border: 1px solid #e0dcf3; padding: 3pt 6pt; vertical-align: top; text-align: left; }
th { background: #ede9fe; }
tr { break-inside: avoid; }
td code { word-break: break-word; }
blockquote { margin: 8pt 0; padding: 2pt 10pt; border-left: 3px solid #c4b5fd;
             color: #4b5163; background: #faf9ff; }
hr { border: none; border-top: 1px solid #ddd6fe; margin: 14pt 0; }
img { max-width: 100%; }

.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover .word { font-size: 64pt; font-weight: bold; color: #7c3aed; letter-spacing: -1pt;
               margin: 0; }
.cover .sub { font-size: 18pt; color: #2e1065; margin: 4pt 0 26pt; }
.cover .meta { font-size: 10.5pt; color: #5b6170; line-height: 1.8; }
.cover .bar { height: 6px; width: 120px; background: linear-gradient(90deg, #c4b5fd, #7c3aed);
              border-radius: 3px; margin-bottom: 20pt; }

.toc { break-before: page; }
.toc h1 { font-size: 22pt; border-bottom: 3px solid #7c3aed; padding-bottom: 6pt; }
.toc ol { list-style: none; padding-left: 0; }
.toc > ol > li { margin: 10pt 0 2pt; font-weight: bold; font-size: 11.5pt; }
.toc > ol > li > ol { margin-top: 3pt; columns: 2; column-gap: 18pt; }
.toc > ol > li:first-child > ol { columns: 1; }
.toc .kids { font-size: 8.4pt; color: #5b6170; margin: 1pt 0 5pt 10pt; line-height: 1.55; }
.toc .kids a { color: #5b6170; }
.toc > ol > li > ol > li { font-weight: normal; font-size: 9.2pt; margin: 1.5pt 0;
                           break-inside: avoid; }

/* pygments (codehilite) — a light purple-leaning theme */
.codehilite .c, .codehilite .c1, .codehilite .cm, .codehilite .ch { color: #7a8194;
  font-style: italic; }
.codehilite .k, .codehilite .kn, .codehilite .kd, .codehilite .kc { color: #7c3aed;
  font-weight: bold; }
.codehilite .s, .codehilite .s1, .codehilite .s2, .codehilite .sd { color: #0f7b5f; }
.codehilite .nf, .codehilite .nc { color: #1d4ed8; }
.codehilite .mi, .codehilite .mf { color: #b45309; }
.codehilite .nt { color: #6d28d9; }
.codehilite .nb, .codehilite .bp { color: #0e7490; }
"""


def _source(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return f"```json\n{text.rstrip()}\n```\n"
    return text


def _shift_headings(body: str) -> str:
    """Every heading of a part goes one level down: the part title is the h1."""
    def bump(match: re.Match) -> str:
        level = min(int(match.group(2)) + 1, 6)
        return f"<{match.group(1)}h{level}"
    return re.sub(r"<(/?)h([1-6])", bump, body)


def _drop_leading_title(text: str) -> str:
    """The README/CHANGELOG/CLAUDE.md "# Title" line repeats the part title."""
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        return "\n".join(lines[1:])
    return text


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading, so the docs' own #links keep working."""
    text = re.sub(r"<[^>]+>", "", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _toc_entry(token: dict, deep: bool) -> str:
    """A part's top-level section; the User Guide also lists its subsections."""
    kids = ""
    if deep and token["children"]:
        kids = ('<div class="kids">' + " · ".join(
            f'<a href="#{c["id"]}">{c["name"]}</a>' for c in token["children"]) + "</div>")
    return f'<li><a href="#{token["id"]}">{token["name"]}</a>{kids}</li>'


def build_html() -> str:
    import markdown

    version = re.search(r'^version\s*=\s*"([^"]+)"',
                        (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    sections, toc = [], []
    for n, (title, rel, summary) in enumerate(PARTS, 1):
        src = ROOT / rel
        if not src.exists():
            print(f"skipping missing {rel}", file=sys.stderr)
            continue
        md = markdown.Markdown(extensions=[
            "tables", "fenced_code", "sane_lists", "toc",
            "codehilite"], extension_configs={
                "codehilite": {"guess_lang": False, "css_class": "codehilite"},
                "toc": {"slugify": lambda value, sep, n=n: f"p{n}-" + _slug(value)}})
        body = md.convert(_drop_leading_title(_source(src)))
        # The README's own #anchors point at GitHub-style slugs; map them to ours.
        body = re.sub(r'href="#([^"]+)"', lambda m, n=n: f'href="#p{n}-{m.group(1)}"', body)
        part_id = f"part-{n}"
        sections.append(
            f'<h1 class="part" id="{part_id}">{n}. {html.escape(title)}</h1>'
            f'<p class="part-summary">{html.escape(summary)} '
            f'<span style="color:#8a94a7">— from <code>{html.escape(rel)}</code></span></p>'
            + _shift_headings(body))
        subs = "".join(_toc_entry(t, deep=(rel == "README.md")) for t in md.toc_tokens)
        toc.append(f'<li><a href="#{part_id}">{n}. {html.escape(title)}</a>'
                   + (f"<ol>{subs}</ol>" if subs else "") + "</li>")
    cover = f"""
<section class="cover">
  <div class="bar"></div>
  <p class="word">coded</p>
  <p class="sub">Complete Documentation</p>
  <div class="meta">
    A terminal coding agent for any OpenAI-compatible endpoint.<br>
    Version {html.escape(version)}<br>
    User guide · Skills &amp; plugins · Configuration · Developer guide · Changelog
  </div>
</section>"""
    contents = f'<section class="toc"><h1>Contents</h1><ol>{"".join(toc)}</ol></section>'
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>coded documentation"
            f"</title><style>{CSS}</style></head><body>{cover}{contents}"
            f"{''.join(sections)}</body></html>")


PRINT_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const [src, out, version] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + src, { waitUntil: 'load' });
  await page.pdf({
    path: out, format: 'A4', printBackground: true, outline: true, tagged: true,
    displayHeaderFooter: true,
    headerTemplate: '<span></span>',
    footerTemplate: `<div style="font-size:7.5pt;color:#8a94a7;width:100%;padding:0 16mm;
      display:flex;justify-content:space-between;font-family:DejaVu Sans,sans-serif">
      <span>coded ${version} — documentation</span>
      <span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`,
    margin: { top: '18mm', bottom: '20mm', left: '16mm', right: '16mm' },
  });
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def main() -> int:
    version = re.search(r'^version\s*=\s*"([^"]+)"',
                        (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "coded-docs.html"
        page.write_text(build_html(), encoding="utf-8")
        script = Path(tmp) / "print.js"
        script.write_text(PRINT_JS, encoding="utf-8")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["node", str(script), str(page), str(OUT), version], check=True)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
