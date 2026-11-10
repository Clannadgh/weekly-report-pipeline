"""生成 Pandoc 用的 Word 样式模板 tools/templates/report-reference.docx。

只生成一次；以后改版式改这里再重新运行：
    python tools/make_reference_docx.py

Pandoc 只读取模板的样式与页面设置（字体、行距、标题、图注、页眉页脚），不读正文。
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

OUT = Path(__file__).resolve().parent / "templates" / "report-reference.docx"


def _east_asia(style, name: str, size: float, bold: bool = False) -> None:
    style.font.name = name
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor(0, 0, 0)
    rpr = style.element.get_or_add_rPr()
    rpr.rFonts.set(qn("w:eastAsia"), name)


def _field(paragraph, instr: str, placeholder: str = "") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    run._r.append(begin)
    run2 = paragraph.add_run()
    text = OxmlElement("w:instrText")
    text.set(qn("xml:space"), "preserve")
    text.text = instr
    run2._r.append(text)
    run3 = paragraph.add_run()
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    run3._r.append(sep)
    paragraph.add_run(placeholder)
    end_run = paragraph.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run._r.append(end)


def _add_style(doc, name: str, size: float, *, bold: bool = False,
               align=WD_ALIGN_PARAGRAPH.CENTER, before: float = 0,
               after: float = 0, spacing: float = 1.0, indent: float = 0,
               color=(0, 0, 0), font: str = "宋体"):
    try:
        style = doc.styles[name]
    except KeyError:
        style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    _east_asia(style, font, size, bold)
    style.font.color.rgb = RGBColor(*color)
    fmt = style.paragraph_format
    fmt.alignment = align
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = spacing
    fmt.first_line_indent = Pt(indent)
    return style


def _bottom_border(style, color: str = "4F5BD6", size: int = 12) -> None:
    ppr = style.element.get_or_add_pPr()
    border = ppr.find(qn("w:pBdr"))
    if border is None:
        border = OxmlElement("w:pBdr")
        ppr.append(border)
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "6")
    bottom.set(qn("w:color"), color)
    border.append(bottom)


def _table_style(doc) -> None:
    try:
        style = doc.styles["Table"]
    except KeyError:
        style = doc.styles.add_style("Table", WD_STYLE_TYPE.TABLE)
    element = style.element
    tbl_pr = element.find(qn("w:tblPr"))
    if tbl_pr is None:
        tbl_pr = OxmlElement("w:tblPr")
        element.append(tbl_pr)
    borders = OxmlElement("w:tblBorders")
    for edge, size in (("top", "12"), ("bottom", "12"), ("insideH", "4")):
        node = OxmlElement("w:" + edge)
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), size)
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), "1E1C19")
        borders.append(node)
    for edge in ("left", "right", "insideV"):
        node = OxmlElement("w:" + edge)
        node.set(qn("w:val"), "nil")
        borders.append(node)
    tbl_pr.append(borders)
    # 表头加粗、居中；数据行左对齐
    first_row = OxmlElement("w:tblStylePr")
    first_row.set(qn("w:type"), "firstRow")
    rpr = OxmlElement("w:rPr")
    bold = OxmlElement("w:b")
    rpr.append(bold)
    first_row.append(rpr)
    element.append(first_row)


def build() -> Path:
    doc = Document()

    normal = doc.styles["Normal"]
    _east_asia(normal, "宋体", 12)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.first_line_indent = Pt(24)

    heading = doc.styles["Heading 1"]
    _east_asia(heading, "黑体", 15, True)
    heading.paragraph_format.space_before = Pt(14)
    heading.paragraph_format.space_after = Pt(8)
    heading.paragraph_format.line_spacing = 1.3
    heading.paragraph_format.first_line_indent = Pt(0)
    heading.paragraph_format.keep_with_next = True

    heading2 = doc.styles["Heading 2"]
    _east_asia(heading2, "黑体", 13, True)
    heading2.paragraph_format.space_before = Pt(10)
    heading2.paragraph_format.space_after = Pt(4)
    heading2.paragraph_format.line_spacing = 1.3
    heading2.paragraph_format.first_line_indent = Pt(0)
    heading2.paragraph_format.keep_with_next = True

    # 图注：Pandoc 的 implicit figure 用 Image Caption 样式
    _add_style(doc, "Image Caption", 9.5, after=14, spacing=1.0,
               color=(0x49, 0x44, 0x3D))
    _add_style(doc, "Cover Kicker", 11, after=18, spacing=1.2,
               color=(0x7B, 0x74, 0x6A), font="黑体")
    _add_style(doc, "Cover Spacer", 12, after=0, spacing=1.0)
    _add_style(doc, "Cover Title", 26, bold=True, before=0, after=10,
               spacing=1.1, font="黑体")
    _add_style(doc, "Cover Subtitle", 12.5, after=4, spacing=1.2,
               color=(0x49, 0x44, 0x3D))
    _add_style(doc, "Cover Rule", 6, before=8, after=14, spacing=1.0)
    _add_style(doc, "Cover Meta", 11.5, after=3, spacing=1.35,
               color=(0x49, 0x44, 0x3D))
    _add_style(doc, "Cover Foot", 9.5, after=0, spacing=1.2,
               color=(0x7B, 0x74, 0x6A))
    _add_style(doc, "Table Caption", 10.5, bold=True, before=8, after=4,
               spacing=1.2, font="黑体")
    _add_style(doc, "Table Note", 9, after=8, spacing=1.1,
               color=(0x7B, 0x74, 0x6A))
    _add_style(doc, "Compact", 10, after=0, spacing=1.0, indent=0,
               align=WD_ALIGN_PARAGRAPH.LEFT)
    _bottom_border(doc.styles["Cover Rule"])
    _table_style(doc)

    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.54)
    section.left_margin = section.right_margin = Cm(3.17)
    section.different_first_page_header_footer = True

    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = header.add_run("经营周报")
    run.font.size = Pt(9)
    run.font.name = "宋体"
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
    run.font.color.rgb = RGBColor(0x66, 0x6E, 0x77)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _field(footer, "PAGE", "1")
    for item in footer.runs:
        item.font.size = Pt(9)
        item.font.name = "宋体"

    # 封面算第 0 页，正文从 1 开始
    pg = OxmlElement("w:pgNumType")
    pg.set(qn("w:start"), "0")
    section._sectPr.append(pg)

    doc.add_paragraph("模板说明：本文件只提供样式，Pandoc 不读取正文。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    return OUT


if __name__ == "__main__":
    path = build()
    print("wrote", path)
