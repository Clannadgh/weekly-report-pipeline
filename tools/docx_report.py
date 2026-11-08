#!/usr/bin/env python3
"""docx_report —— 把带 [[f:]] 替换好的周报 Markdown 排成论文式 Word。

版式（按数学建模论文惯例，也是 规范.md 第九节的要求）：
  A4；页边距上下 2.54cm、左右 3.17cm
  正文 宋体小四（10.5pt 号字按四号换算见 --body-size）、1.5 倍行距、首行缩进 2 字符
  一级标题 黑体四号加粗，中文序号「一、二、…」
  表格 三线表（顶线、栏目线、底线，无竖线），表题居中置上
  图片 居中，图题居中置下
  页眉 右对齐小五，页脚 居中页码

用法：
    python tools/docx_report.py <正文.md> --out <输出.docx> \
        --figures <图目录> [--manifest <图表清单.json>] \
        [--title 标题] [--subtitle 副标题] [--header 页眉] \
        [--table-titles "表 1 核心指标|表 2 明细"]

正文里的「图 N 说明」行会被替换成 图片 + 图题；「| … |」块会被排成三线表。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

CN_NUM = ["一、", "二、", "三、", "四、", "五、", "六、",
          "七、", "八、", "九、", "十、"]
FIG_CAP_RE = re.compile(r"^\s*图\s*(\d+)\s+(.+)$")
TABLE_SEP_RE = re.compile(r"^[\s\-:|]+$")


def _set_font(run, name: str, size: float, bold: bool = False) -> None:
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)


def _add_page_field(par) -> None:
    run = par.add_run()
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run._element.addnext(fld)


def _three_line(table, header_rows: int = 1) -> None:
    """三线表：上下粗线 + 表头下细线，去掉所有竖线。"""
    pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge, size in (("top", 12), ("bottom", 12)):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:color"), "000000")
        borders.append(el)
    for edge in ("left", "right", "insideV", "insideH"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "none")
        borders.append(el)
    pr.append(borders)
    for cell in table.rows[header_rows - 1].cells:
        tc_pr = cell._tc.get_or_add_tcPr()
        tc_b = OxmlElement("w:tcBorders")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:color"), "000000")
        tc_b.append(bottom)
        tc_pr.append(tc_b)


def _figure_paths(figure_dir: Path, manifest: Path | None) -> dict[int, Path]:
    """图号 → PNG 路径。优先读图表清单，读不到按文件名 fig01_* 排序兜底。"""
    mapping: dict[int, Path] = {}
    if manifest and manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for fig in data.get("figures", []):
            for item in fig.get("files", []):
                p = Path(item["path"])
                if p.suffix.lower() == ".png":
                    mapping[int(fig["no"])] = p if p.exists() else figure_dir / p.name
    if not mapping and figure_dir.exists():
        pngs = sorted(figure_dir.glob("fig*.png"))
        for i, p in enumerate(pngs, 1):
            num = re.match(r"fig(\d+)", p.name)
            mapping[int(num.group(1)) if num else i] = p
    return mapping


def build(md_path: Path, out_path: Path, figure_dir: Path, *,
          manifest: Path | None = None, title: str = "", subtitle: str = "",
          header: str = "", table_titles: list[str] | None = None,
          body_size: float = 12, heading_size: float = 14,
          figure_width_cm: float = 13.5) -> Path:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    figs = _figure_paths(figure_dir, manifest)
    table_titles = table_titles or []

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.top_margin = sec.bottom_margin = Cm(2.54)
    sec.left_margin = sec.right_margin = Cm(3.17)

    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(body_size)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    pf = normal.paragraph_format
    pf.line_spacing = 1.5
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)

    h1 = doc.styles["Heading 1"]
    h1.font.name = "黑体"
    h1.font.size = Pt(heading_size)
    h1.font.bold = True
    h1.font.color.rgb = RGBColor(0, 0, 0)
    h1.element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
    h1.paragraph_format.space_before = Pt(12)
    h1.paragraph_format.space_after = Pt(6)
    h1.paragraph_format.line_spacing = 1.5

    if header:
        hp = sec.header.paragraphs[0]
        hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _set_font(hp.add_run(header), "宋体", 9)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_font(fp.add_run("— "), "宋体", 10)
    _add_page_field(fp)
    _set_font(fp.add_run(" —"), "宋体", 10)

    # 报头：md 里的 "# " 行或 --title
    if title:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_font(p.add_run(title), "黑体", 18, True)
        p.paragraph_format.space_after = Pt(4)
    if subtitle:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_font(p.add_run(subtitle), "宋体", 10.5)
        p.paragraph_format.space_after = Pt(10)

    sec_no = 0
    tbl_no = 0
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.startswith("# ") and title and line[2:].strip() == title:
            i += 1
            continue
        if line.startswith("## "):
            text = line[3:].strip()
            head = CN_NUM[sec_no] + text if sec_no < len(CN_NUM) else text
            doc.add_heading(head, level=1)
            sec_no += 1
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not (cells and all(TABLE_SEP_RE.match(c) for c in cells)):
                    rows.append(cells)
                i += 1
            if not rows:
                continue
            tbl_no += 1
            cap_text = (table_titles[tbl_no - 1] if tbl_no - 1 < len(table_titles)
                        else f"表 {tbl_no}")
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_font(cap.add_run(cap_text), "宋体", 10.5)
            cap.paragraph_format.space_before = Pt(6)
            cap.paragraph_format.space_after = Pt(2)
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for ri, row in enumerate(rows):
                for ci, val in enumerate(row):
                    cell = table.cell(ri, ci)
                    cell.text = ""
                    p = cell.paragraphs[0]
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p.paragraph_format.line_spacing = 1.0
                    _set_font(p.add_run(val), "宋体", 10.5, ri == 0)
            _three_line(table)
            doc.add_paragraph().paragraph_format.space_after = Pt(6)
            continue
        m = FIG_CAP_RE.match(line)
        if m:
            no = int(m.group(1))
            img = figs.get(no)
            if img and img.exists():
                doc.add_picture(str(img), width=Cm(figure_width_cm))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                doc.paragraphs[-1].paragraph_format.space_before = Pt(6)
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_font(cap.add_run(line.strip()), "宋体", 10.5)
            cap.paragraph_format.space_after = Pt(8)
            i += 1
            continue
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.first_line_indent = Pt(body_size * 2)
        _set_font(p.add_run(line.strip()), "宋体", body_size)
        i += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def _cli() -> int:
    ap = argparse.ArgumentParser(description="把周报 Markdown 排成论文式 Word")
    ap.add_argument("md")
    ap.add_argument("--out", required=True)
    ap.add_argument("--figures", default="", help="图目录，默认取 md 同级的 figures/")
    ap.add_argument("--manifest", default="", help="图表清单 JSON，用于图号→文件映射")
    ap.add_argument("--title", default="")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--header", default="")
    ap.add_argument("--table-titles", default="", help="多个表题用 | 分隔")
    ap.add_argument("--body-size", type=float, default=12)
    ap.add_argument("--heading-size", type=float, default=14)
    ap.add_argument("--figure-width-cm", type=float, default=13.5)
    args = ap.parse_args()

    md = Path(args.md)
    if not md.exists():
        print(f"正文不存在：{md}", file=sys.stderr)
        return 2
    fig_dir = Path(args.figures) if args.figures else md.parent / "figures"
    titles = [t for t in args.table_titles.split("|") if t.strip()]
    out = build(md, Path(args.out), fig_dir,
                manifest=Path(args.manifest) if args.manifest else None,
                title=args.title, subtitle=args.subtitle, header=args.header,
                table_titles=titles, body_size=args.body_size,
                heading_size=args.heading_size,
                figure_width_cm=args.figure_width_cm)
    print(f"[docx_report] wrote {out}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_cli())
