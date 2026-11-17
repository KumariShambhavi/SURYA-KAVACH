"""
build_docx.py — render a Markdown doc into a styled .docx.

Single source of truth: the Markdown. The previous generator kept its own COPY of the prose,
which silently drifted out of sync and ended up asserting numbers that were no longer true.
This one reads the .md, so the .docx can never disagree with it again.

Usage:
    python scripts/build_docx.py                       # architecture doc -> .docx next to it
    python scripts/build_docx.py path/to/file.md       # any markdown file
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MD = ROOT / "docs" / "SURYA-KAVACH_Architecture_and_Design.md"

ORANGE = RGBColor(0xFF, 0x6A, 0x1A)   # ISRO accent
BLUE = RGBColor(0x1E, 0x6F, 0xD9)
GREY = RGBColor(0x44, 0x4A, 0x55)
DARK = RGBColor(0x11, 0x14, 0x1A)
CODEBG = "F2F4F7"
QUOTEBG = "FFF4E8"

INLINE = re.compile(r"(\*\*.+?\*\*|\*[^*]+?\*|`[^`]+?`)")


def _shade(el, hex_fill: str) -> None:
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:fill"), hex_fill)
    el.append(sh)


def _runs(p, text: str, *, size=10.5, color=DARK, bold=False, mono=False) -> None:
    """Write text into paragraph p, honouring **bold**, *italic* and `code`."""
    for part in INLINE.split(text):
        if not part:
            continue
        r = p.add_run()
        b, i, m = bold, False, mono
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, b = part[2:-2], True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            part, m = part[1:-1], True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, i = part[1:-1], True
        r.text = part.replace("→", "->").replace("≥", ">=").replace("≈", "~")
        r.bold, r.italic = b, i
        r.font.size = Pt(size)
        r.font.color.rgb = color
        r.font.name = "Consolas" if m else "Segoe UI"


def _table(doc: Document, rows: list[list[str]]) -> None:
    header, body = rows[0], rows[1:]
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(header):
        cell = t.rows[0].cells[i]
        cell.text = ""
        _shade(cell._tc.get_or_add_tcPr(), "E8EDF3")
        _runs(cell.paragraphs[0], h, size=9.5, bold=True, color=BLUE)
    for row in body:
        cells = t.add_row().cells
        for i, v in enumerate(row[: len(header)]):
            cells[i].text = ""
            _runs(cells[i].paragraphs[0], v, size=9.5, color=GREY)
    doc.add_paragraph()


def render(md_path: Path, out_path: Path) -> None:
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Inches(0.8)
        s.top_margin = s.bottom_margin = Inches(0.7)
    doc.styles["Normal"].font.name = "Segoe UI"
    doc.styles["Normal"].font.size = Pt(10.5)

    lines = md_path.read_text(encoding="utf-8").splitlines()
    i, n = 0, len(lines)
    in_code, code_buf = False, []

    while i < n:
        raw = lines[i]
        line = raw.rstrip()

        # fenced code / ASCII diagrams
        if line.startswith("```"):
            if in_code:
                p = doc.add_paragraph()
                _shade(p._p.get_or_add_pPr(), CODEBG)
                _runs(p, "\n".join(code_buf), size=8.5, color=GREY, mono=True)
                p.paragraph_format.space_after = Pt(8)
                code_buf, in_code = [], False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(raw)
            i += 1
            continue

        if not line.strip():
            i += 1
            continue

        # tables
        if line.lstrip().startswith("|"):
            block = []
            while i < n and lines[i].lstrip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            rows = []
            for r in block:
                if re.fullmatch(r"\|[\s:|-]+\|", r):
                    continue
                rows.append([c.strip() for c in r.strip("|").split("|")])
            if rows:
                _table(doc, rows)
            continue

        # headings
        if line.startswith("#"):
            lvl = len(line) - len(line.lstrip("#"))
            text = line.lstrip("# ").strip()
            p = doc.add_paragraph()
            if lvl == 1:
                _runs(p, text, size=20, bold=True, color=DARK)
                p.paragraph_format.space_before = Pt(14)
                p.paragraph_format.space_after = Pt(6)
            elif lvl == 2:
                _runs(p, text, size=14.5, bold=True, color=ORANGE)
                p.paragraph_format.space_before = Pt(12)
            else:
                _runs(p, text, size=11.5, bold=True, color=BLUE)
                p.paragraph_format.space_before = Pt(8)
            i += 1
            continue

        # horizontal rule
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line.strip()):
            i += 1
            continue

        # blockquote (callouts — the honesty notes)
        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip().lstrip(">").strip())
                i += 1
            p = doc.add_paragraph()
            _shade(p._p.get_or_add_pPr(), QUOTEBG)
            _runs(p, " ".join(x for x in buf if x), size=10, color=GREY)
            p.paragraph_format.space_after = Pt(8)
            continue

        # lists
        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
        if m:
            indent, marker, text = m.group(1), m.group(2), m.group(3)
            style = "List Number" if marker[0].isdigit() else "List Bullet"
            p = doc.add_paragraph(style=style)
            if len(indent) >= 2:
                p.paragraph_format.left_indent = Inches(0.55)
            _runs(p, text, size=10.5, color=GREY)
            p.paragraph_format.space_after = Pt(2)
            i += 1
            continue

        # plain paragraph
        p = doc.add_paragraph()
        _runs(p, line.strip(), size=10.5, color=GREY)
        p.paragraph_format.space_after = Pt(6)
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        i += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    print(f"SAVED: {out_path}")


if __name__ == "__main__":
    md = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_MD
    render(md, md.with_suffix(".docx"))
