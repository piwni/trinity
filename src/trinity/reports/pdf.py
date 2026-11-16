"""Render a markdown note or an HTML report to a print-ready PDF.

Markdown is for documents that get handed to someone outside the project — a
dietitian, a coach, a doctor — where the reader has no context and may well
print the thing. So: A4, generous margins, tables that do not split mid-row, and
no chrome from the tool that made it. HTML reports carry their own print styles
and are printed as they are.

    trinity render-pdf data/reports/note.md
    trinity render-pdf data/reports/goal-2026-malbork.html

Markdown goes through markdown-it-py; the PDF comes from headless Chrome, which
gets Polish diacritics and table layout right without extra system packages.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "chromium",
    "google-chrome",
)

CSS = """
@page { size: A4; margin: 18mm 17mm 20mm 17mm; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font: 10.5pt/1.5 -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
  color: #1a1a1a; margin: 0; hyphens: none;
}

h1 { font-size: 19pt; margin: 0 0 2mm; letter-spacing: -0.2pt; }
h2 {
  font-size: 13pt; margin: 9mm 0 3mm; padding-bottom: 1.5mm;
  border-bottom: 1.5px solid #1a1a1a; letter-spacing: -0.1pt;
}
h3 { font-size: 11pt; margin: 6mm 0 2mm; }
/* A heading stranded at the foot of a page is the classic print failure. */
h1, h2, h3 { break-after: avoid; break-inside: avoid; }

p { margin: 0 0 2.6mm; orphans: 3; widows: 3; }
strong { font-weight: 650; }
hr { border: 0; border-top: 1px solid #d8d8d8; margin: 7mm 0; }

ol, ul { margin: 0 0 3mm; padding-left: 5.5mm; }
li { margin-bottom: 2.2mm; break-inside: avoid; }
li > p { margin-bottom: 1.2mm; }

table {
  border-collapse: collapse; width: 100%;
  margin: 3mm 0 5mm; font-size: 9.5pt; break-inside: auto;
}
/* Repeat the header when a long table does have to span pages. */
thead { display: table-header-group; }
tr { break-inside: avoid; }
th, td { padding: 1.7mm 2.5mm; text-align: left; vertical-align: top; }
th {
  border-bottom: 1.2px solid #1a1a1a; font-weight: 650;
  font-size: 8.8pt; text-transform: uppercase; letter-spacing: 0.3pt;
}
td { border-bottom: 0.6px solid #e2e2e2; }
tbody tr:nth-child(even) { background: #fafafa; }

blockquote {
  margin: 4mm 0; padding: 3mm 4mm; background: #f4f6f8;
  border-left: 2.5px solid #4a5a6a; break-inside: avoid;
}
blockquote p:last-child { margin-bottom: 0; }

code {
  font: 9pt/1.4 "SF Mono", Menlo, monospace;
  background: #f2f2f2; padding: 0.3mm 1mm; border-radius: 2px;
}
"""


class ChromeNotFoundError(RuntimeError):
    pass


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    raise ChromeNotFoundError(
        "No Chrome or Chromium found — install one, or add it to CHROME_CANDIDATES.")


def html_to_pdf(html_path: Path, pdf_path: Path) -> Path:
    """Print an HTML file to PDF with headless Chrome."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [find_chrome(), "--headless", "--disable-gpu", "--no-sandbox",
         # Chrome's own header prints the file:// URL and today's date across
         # the top of every page, which looks like a misprint on a handout.
         "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path.resolve()}", html_path.resolve().as_uri()],
        check=True, capture_output=True, timeout=120,
    )
    return pdf_path


def markdown_to_html(md_path: Path) -> str:
    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    body = md.render(md_path.read_text(encoding="utf-8"))
    return (f"<!doctype html><html lang='pl'><head><meta charset='utf-8'>"
            f"<title>{md_path.stem}</title><style>{CSS}</style></head>"
            f"<body>{body}</body></html>")


def render(source: Path, pdf_path: Path | None = None) -> Path:
    """Render a ``.md`` note or an ``.html`` report to PDF, next to the source
    unless ``pdf_path`` says otherwise."""
    pdf_path = pdf_path or source.with_suffix(".pdf")
    if source.suffix.lower() in {".html", ".htm"}:
        return html_to_pdf(source, pdf_path)
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "doc.html"
        html_path.write_text(markdown_to_html(source), encoding="utf-8")
        return html_to_pdf(html_path, pdf_path)
