"""经营周报的 Word 排版（论文式）。

与 tools/docx_report.py 的区别：这里是周报专用，负责把 S5 的 draft.md 整理成
「封面 → 摘要 → 目录 → 正文（图随文走）→ 附录」的交付稿，不改变台账数字。

规则：
- 一级标题按「一、二、…」编号；
- 正文里的「（见图 N）」保留，对应图就近插在该段之后，图题居中置下；
- 「图表」小节不直接排版，图从 figures_manifest.json 取，避免重复图题；
- 「数据发现明细」移到附录，用 9.5pt 小字，保留原文供核对；
- 「未来清单」若正文只写了「见 s5_facts.json」，改用 facts.future 的实际条目；
- 封面、页眉页脚、目录域、页码由本模块生成，改版式改这里，不改正文。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

CN_NUM = ["一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、", "九、", "十、",
          "十一、", "十二、", "十三、", "十四、", "十五、"]
FIG_REF_RE = re.compile(r"（见图\s*(\d+)）")
FIG_GROUP_RE = re.compile(r"（见图[^）]*）")
NUM_RE = re.compile(r"[0-9]+")


def _fig_numbers(text: str) -> list[int]:
    """从「（见图 1）」「（见图 4、图 7）」里取出所有图号，保持出现顺序。"""
    numbers: list[int] = []
    for match in FIG_GROUP_RE.finditer(text or ""):
        for raw in NUM_RE.findall(match.group(0)):
            value = int(raw)
            if value not in numbers:
                numbers.append(value)
    return numbers
FIG_CAP_RE = re.compile(r"^图\s*(\d+)[\s\u3000]+(.+)$")
BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
TABLE_SEP_RE = re.compile(r"^[\s\-:|]+$")


def _set_run(run, name: str, size: float, bold: bool = False,
             color: tuple[int, int, int] = (0, 0, 0)) -> None:
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    run.font.color.rgb = RGBColor(*color)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)


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
    run4 = paragraph.add_run(placeholder)
    end_run = paragraph.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run._r.append(end)


def _para_border(paragraph, edge: str = "bottom", size: int = 8) -> None:
    pr = paragraph._p.get_or_add_pPr()
    borders = pr.find(qn("w:pBdr"))
    if borders is None:
        borders = OxmlElement("w:pBdr")
        pr.append(borders)
    el = OxmlElement("w:" + edge)
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), "1")
    el.set(qn("w:color"), "808080")
    borders.append(el)


def _three_line(table) -> None:
    pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge, size in (("top", 12), ("bottom", 12)):
        el = OxmlElement("w:" + edge)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:color"), "000000")
        borders.append(el)
    for edge in ("left", "right", "insideV", "insideH"):
        el = OxmlElement("w:" + edge)
        el.set(qn("w:val"), "none")
        borders.append(el)
    pr.append(borders)


def _figure_map(figure_dir: Path, manifest: Path | None) -> dict[int, dict]:
    mapping: dict[int, dict] = {}
    data: dict[str, Any] = {}
    if manifest and manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    for index, fig in enumerate(data.get("figures") or [], 1):
        if not isinstance(fig, dict):
            continue
        try:
            number = int(fig.get("number") or fig.get("no") or index)
        except (TypeError, ValueError):
            number = index
        files = fig.get("files") or {}
        png = ""
        if isinstance(files, dict):
            png = str(files.get("png") or "")
        elif isinstance(files, list):
            for item in files:
                raw = str(item.get("path") or "") if isinstance(item, dict) else str(item)
                if raw.lower().endswith(".png"):
                    png = raw
                    break
        if not png:
            name = str(fig.get("name") or "")
            if name:
                png = "figures/" + name + ".png"
        path = Path(png)
        if not path.is_absolute():
            path = figure_dir.parent / path if png.startswith("figures/") else figure_dir / path.name
        if not path.is_file():
            fallback = figure_dir / ("fig" + str(number).zfill(2) + ".png")
            path = fallback
        mapping[number] = {
            "number": number,
            "path": path,
            "caption": str(fig.get("caption") or fig.get("title") or ("图 " + str(number))),
        }
    return mapping


def _parse(md_text: str) -> tuple[str, list[dict]]:
    """返回 (标题, 小节列表)；每个小节 {heading, blocks:[{type,text}]}。"""
    title = ""
    sections: list[dict] = []
    current: dict | None = None
    for raw in md_text.splitlines():
        line = raw.rstrip()
        text = line.strip()
        if not text:
            continue
        if text.startswith("# ") and not title:
            title = text[2:].strip()
            continue
        if text.startswith("## "):
            current = {"heading": text[3:].strip(), "blocks": []}
            sections.append(current)
            continue
        if current is None:
            continue
        match = BULLET_RE.match(text)
        if match:
            current["blocks"].append({"type": "bullet", "text": match.group(1)})
            continue
        if text.startswith("|"):
            current["blocks"].append({"type": "table", "text": text})
            continue
        current["blocks"].append({"type": "p", "text": text})
    return title, sections


def _is_figure_section(heading: str) -> bool:
    return "图表" in heading and "数据" not in heading


def _is_data_appendix(heading: str) -> bool:
    return "数据发现明细" in heading or "取数" in heading


def _is_future(heading: str) -> bool:
    return "未来" in heading


def _image_size(path: Path, max_w: float = 13.2, max_h: float = 8.6) -> tuple[float, float] | None:
    try:
        from PIL import Image

        with Image.open(path) as img:
            w, h = img.size
    except Exception:  # noqa: BLE001 - 图坏了就别插
        return None
    if not w or not h:
        return None
    width = max_w
    height = width * h / w
    if height > max_h:
        height = max_h
        width = height * w / h
    return width, height


def _add_cover(doc: Document, meta: dict) -> None:
    for _ in range(2):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run(meta.get("company") or "经营分析"), "黑体", 14,
             color=(0x44, 0x4C, 0x55))
    p.paragraph_format.space_after = Pt(18)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run(meta.get("title") or "经营周报"), "黑体", 30, True)
    p.paragraph_format.space_after = Pt(10)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run(meta.get("subtitle") or ""), "宋体", 13,
             color=(0x33, 0x33, 0x33))
    p.paragraph_format.space_after = Pt(4)
    _para_border(p, "bottom", 6)

    for _ in range(6):
        doc.add_paragraph()

    rows = [
        ("报告周期", meta.get("period") or "—"),
        ("编制单位", meta.get("company") or "—"),
        ("审　　核", meta.get("reviewer") or "—"),
        ("版本", meta.get("version") or "—"),
        ("交付日期", meta.get("date") or "—"),
    ]
    table = doc.add_table(rows=len(rows), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for index, (key, value) in enumerate(rows):
        row = table.rows[index]
        row.cells[0].width = Cm(4.2)
        row.cells[1].width = Cm(9.0)
        for cell, text, bold in ((row.cells[0], key, False), (row.cells[1], value, False)):
            cell.text = ""
            paragraph = cell.paragraphs[0]
            paragraph.alignment = (WD_ALIGN_PARAGRAPH.RIGHT if cell is row.cells[0]
                                   else WD_ALIGN_PARAGRAPH.LEFT)
            _set_run(paragraph.add_run(text), "宋体", 12, bold,
                     color=(0x33, 0x33, 0x33))

    for _ in range(7):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run("本报告由经营周报流水线生成，数字以台账 ledger.json 为准"),
             "宋体", 9, color=(0x77, 0x7F, 0x88))


def _add_summary(doc: Document, summary: str) -> None:
    doc.add_page_break()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run("摘　要"), "黑体", 16, True)
    p.paragraph_format.space_after = Pt(12)
    body = doc.add_paragraph()
    body.paragraph_format.first_line_indent = Pt(24)
    body.paragraph_format.line_spacing = 1.5
    body.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _set_run(body.add_run(summary), "宋体", 12)


def _add_toc(doc: Document) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(p.add_run("目　录"), "黑体", 16, True)
    p.paragraph_format.space_after = Pt(12)
    item = doc.add_paragraph()
    _field(item, 'TOC \\o "1-2" \\h \\z \\u', "打开文档后按 F9 更新目录")
    for run in item.runs:
        _set_run(run, "宋体", 10.5)


def _add_figure(doc: Document, fig: dict, extra_gap: float = 0) -> None:
    size = _image_size(fig["path"])
    if size is None:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_run(p.add_run("（图文件缺失：" + fig["path"].name + "）"), "宋体", 10.5,
                 color=(0xB3, 0x26, 0x1E))
    else:
        width, height = size
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1.0
        p.paragraph_format.keep_with_next = True
        p.add_run().add_picture(str(fig["path"]), width=Cm(width), height=Cm(height))
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.space_before = Pt(2)
    cap.paragraph_format.space_after = Pt(12 + extra_gap)
    cap.paragraph_format.line_spacing = 1.0
    _set_run(cap.add_run(fig["caption"]), "宋体", 10.5, color=(0x33, 0x33, 0x33))


def _add_table(doc: Document, rows: list[list[str]]) -> None:
    if not rows:
        return
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ri, row in enumerate(rows):
        for ci, value in enumerate(row[:len(rows[0])]):
            cell = table.cell(ri, ci)
            cell.text = ""
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.line_spacing = 1.0
            _set_run(paragraph.add_run(value), "宋体", 10.5, ri == 0)
    _three_line(table)


def build_report(md_path: str | Path, out_path: str | Path, figure_dir: str | Path,
                 manifest: str | Path | None, *, meta: dict,
                 summary: str = "", heading_map: dict | None = None) -> Path:
    """封面 →（摘要）→ 正文；不生成目录和附录，所有图都插进正文。"""
    md_path = Path(md_path)
    out_path = Path(out_path)
    figure_dir = Path(figure_dir)
    manifest_path = Path(manifest) if manifest else None

    title, sections = _parse(md_path.read_text(encoding="utf-8"))
    if not meta.get("title"):
        meta["title"] = title or "经营周报"
    figs = _figure_map(figure_dir, manifest_path)

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(12)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)

    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.54)
    section.left_margin = section.right_margin = Cm(3.17)
    section.different_first_page_header_footer = True

    _add_cover(doc, meta)
    if summary:
        _add_summary(doc, summary)

    body_section = doc.add_section(WD_SECTION.NEW_PAGE)
    body_section.top_margin = body_section.bottom_margin = Cm(2.54)
    body_section.left_margin = body_section.right_margin = Cm(3.17)
    body_section.header.is_linked_to_previous = False
    body_section.footer.is_linked_to_previous = False
    body_section.different_first_page_header_footer = False
    page_type = OxmlElement("w:pgNumType")
    page_type.set(qn("w:start"), "1")
    body_section._sectPr.append(page_type)

    header = body_section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _set_run(header.add_run(meta.get("header") or (meta.get("title") or "经营周报")),
             "宋体", 9, color=(0x66, 0x6E, 0x77))
    footer = body_section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run(footer.add_run("第 "), "宋体", 9)
    _field(footer, "PAGE", "1")
    _set_run(footer.add_run(" 页"), "宋体", 9)

    placed: set[int] = set()
    heading_no = 0

    def flush_figures() -> None:
        pending = [number for number in sorted(figs) if number not in placed]
        for position, number in enumerate(pending):
            placed.add(number)
            gap = 16 if position < len(pending) - 1 else 0
            _add_figure(doc, figs[number], extra_gap=gap)

    for sec in sections:
        heading = sec["heading"]
        if _is_data_appendix(heading) or _is_figure_section(heading) or _is_future(heading):
            continue
        # 没被引用的图接在分析部分末尾，不要落到「下周关注」后面
        if ("下周" in heading or "未来" in heading) and len(placed) < len(figs):
            flush_figures()
        heading_no += 1
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(14)
        h.paragraph_format.space_after = Pt(8)
        h.paragraph_format.keep_with_next = True
        prefix = CN_NUM[heading_no - 1] if heading_no <= len(CN_NUM) else ""
        display = (heading_map or {}).get(heading) or heading
        h.style = doc.styles["Heading 1"]
        _set_run(h.add_run(prefix + display), "黑体", 15, True)

        blocks = sec["blocks"]
        index = 0
        while index < len(blocks):
            block = blocks[index]
            if block["type"] == "table":
                rows = []
                while index < len(blocks) and blocks[index]["type"] == "table":
                    cells = [c.strip() for c in blocks[index]["text"].strip().strip("|").split("|")]
                    if not (cells and all(TABLE_SEP_RE.match(c) for c in cells)):
                        rows.append(cells)
                    index += 1
                _add_table(doc, rows)
                continue
            if block["type"] == "bullet":
                paragraph = doc.add_paragraph(style="List Bullet")
                paragraph.paragraph_format.left_indent = Cm(0.74)
                paragraph.paragraph_format.first_line_indent = Cm(0)
                paragraph.paragraph_format.line_spacing = 1.5
                _set_run(paragraph.add_run(block["text"]), "宋体", 12)
                index += 1
                continue

            text = block["text"]
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            paragraph.paragraph_format.first_line_indent = Pt(24)
            paragraph.paragraph_format.line_spacing = 1.5
            _set_run(paragraph.add_run(text), "宋体", 12)
            index += 1

            pending = [number for number in _fig_numbers(text)
                       if number not in placed and number in figs]
            for position, number in enumerate(pending):
                placed.add(number)
                gap = 16 if position < len(pending) - 1 else 0
                _add_figure(doc, figs[number], extra_gap=gap)

    # 没有被正文引用的图，接在正文最后，保证每张图都用上
    flush_figures()

    # 未来事项作为普通小节，不叫附录
    futures = meta.get("future") or []
    if futures:
        heading_no += 1
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(14)
        h.paragraph_format.space_after = Pt(8)
        h.paragraph_format.keep_with_next = True
        prefix = CN_NUM[heading_no - 1] if heading_no <= len(CN_NUM) else ""
        h.style = doc.styles["Heading 1"]
        _set_run(h.add_run(prefix + "未来事项"), "黑体", 15, True)
        for item in futures:
            text = str(item.get("fixed") or item.get("original") or "").strip()
            if not text:
                continue
            paragraph = doc.add_paragraph(style="List Bullet")
            paragraph.paragraph_format.left_indent = Cm(0.74)
            paragraph.paragraph_format.first_line_indent = Cm(0)
            paragraph.paragraph_format.line_spacing = 1.5
            _set_run(paragraph.add_run(text), "宋体", 12)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path
