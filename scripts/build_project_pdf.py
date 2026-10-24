"""Render the project-book .docx into a print-ready PDF.

No LibreOffice / LaTeX needed: we re-flow the generated .docx (paragraphs,
headings, tables, embedded images) into a styled HTML that follows the template
spec — 宋体小四 body / 黑体 headings / centered page numbers / numbered figure
captions — and let headless Chromium (via Playwright) print it to PDF. Driven by
the same .docx that build_project_docx.py produces, so prose stays in one place.
"""
from __future__ import annotations

import base64
from html import escape
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "competition" / "project_book"

DOCS = [
    ("双创实训_GlobalPool AI数字孪生_完整版.docx", "双创实训_GlobalPool AI数字孪生_完整版.pdf"),
    ("双创实训_GlobalPool AI数字孪生_匿名版.docx", "双创实训_GlobalPool AI数字孪生_匿名版.pdf"),
]

CSS = """
@page { size: A4; margin: 2.5cm 2.5cm 2.2cm 2.5cm;
  @bottom-center { content: counter(page); font-family: "Songti SC","SimSun",serif; font-size:10pt; color:#444; } }
* { box-sizing: border-box; }
body { font-family: "Songti SC","SimSun",serif; font-size: 12pt; line-height: 1.5; color: #111; margin: 0; }
.cover { display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; min-height: 24cm; }
.page-break { break-after: page; page-break-after: always; height: 0; }
.cover h1 { font-family: "Heiti SC","SimHei",sans-serif; font-weight: 700; font-size: 22pt; line-height: 1.4; margin: 0 0 2.4cm; padding: 0 1cm; }
.cover .meta { font-size: 14pt; margin: 6px 0; }
h1.sec { font-family: "Heiti SC","SimHei",sans-serif; font-weight: 700; font-size: 16pt; margin: 18px 0 8px; page-break-after: avoid; }
h2.sub { font-family: "Heiti SC","SimHei",sans-serif; font-weight: 700; font-size: 14pt; margin: 12px 0 6px; page-break-after: avoid; }
p { margin: 0 0 8px; text-indent: 2em; text-align: justify; }
.fig { text-align: center; text-indent: 0; margin: 12px 0 4px; page-break-inside: avoid; }
.fig img { max-width: 100%; max-height: 11cm; }
.cap { text-align: center; text-indent: 0; font-size: 9pt; color: #505050; margin: 0 0 12px; page-break-before: avoid; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 14px; font-size: 11pt; page-break-inside: avoid; }
.tabcap { text-align: center; text-indent: 0; font-weight: 700; margin: 6px 0 4px; }
th, td { border: 1px solid #888; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #d9eaf7; font-family: "Heiti SC","SimHei",sans-serif; }
"""


def iter_block_items(parent: DocxDocument):
    body = parent.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def image_data_uri(doc: DocxDocument, rid: str) -> str | None:
    part = doc.part.related_parts.get(rid)
    if part is None:
        return None
    b64 = base64.b64encode(part.blob).decode("ascii")
    ext = (part.content_type.split("/")[-1] or "png").replace("jpeg", "jpg")
    return f"data:image/{ext};base64,{b64}"


def paragraph_image_uri(doc: DocxDocument, para: Paragraph) -> str | None:
    blips = para._p.findall(".//" + qn("a:blip"))
    for blip in blips:
        rid = blip.get(qn("r:embed"))
        if rid:
            return image_data_uri(doc, rid)
    return None


def render_html(doc: DocxDocument) -> str:
    parts: list[str] = []
    cover_lines: list[str] = []
    blocks = list(iter_block_items(doc))

    # Cover = the centered paragraphs before the first Heading 1.
    idx = 0
    for idx, block in enumerate(blocks):
        if isinstance(block, Paragraph) and block.style.name == "Heading 1":
            break
        if isinstance(block, Paragraph) and block.text.strip():
            cover_lines.append(block.text.strip())

    if cover_lines:
        title = cover_lines[0]
        metas = cover_lines[1:]
        parts.append('<section class="cover">')
        parts.append(f"<h1>{escape(title)}</h1>")
        for m in metas:
            parts.append(f'<div class="meta">{escape(m)}</div>')
        parts.append("</section>")
        parts.append('<div class="page-break"></div>')

    fig_no = 0
    for block in blocks[idx:]:
        if isinstance(block, Table):
            rows = block.rows
            thead = rows[0]
            body_rows = rows[1:]
            parts.append("<table>")
            parts.append("<tr>" + "".join(f"<th>{escape(c.text)}</th>" for c in thead.cells) + "</tr>")
            for r in body_rows:
                parts.append("<tr>" + "".join(f"<td>{escape(c.text)}</td>" for c in r.cells) + "</tr>")
            parts.append("</table>")
            continue

        style = block.style.name
        text = block.text.strip()
        img = paragraph_image_uri(doc, block)
        if img:
            parts.append(f'<div class="fig"><img src="{img}"></div>')
            continue
        if not text:
            continue
        if style == "Heading 1":
            parts.append(f'<h1 class="sec">{escape(text)}</h1>')
        elif style == "Heading 2":
            parts.append(f'<h2 class="sub">{escape(text)}</h2>')
        elif text[:1] in {"图", "表"} and len(text) <= 40:
            # Figure/table captions: short standalone paragraphs like "图10 ...".
            cls = "tabcap" if text.startswith("表") else "cap"
            parts.append(f'<p class="{cls}">{escape(text)}</p>')
        else:
            parts.append(f"<p>{escape(text)}</p>")

    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<style>{CSS}</style></head><body>" + "".join(parts) + "</body></html>"
    )


def html_to_pdf(html: str, out_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            display_header_footer=False,
        )
        browser.close()


def main() -> None:
    for docx_name, pdf_name in DOCS:
        docx_path = REPORT_DIR / docx_name
        if not docx_path.exists():
            print(f"SKIP (missing): {docx_path}")
            continue
        doc = Document(str(docx_path))
        html = render_html(doc)
        out = REPORT_DIR / pdf_name
        html_to_pdf(html, out)
        print(f"{out}  ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
