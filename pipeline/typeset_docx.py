"""用 Pandoc + Word 样式模板把排版稿转成 docx。

Pandoc 负责字体、行距、标题、图注、页眉页脚；本模块只负责调用和校验。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class TypesetError(RuntimeError):
    """Pandoc 不可用或转换失败。"""


def pandoc_path() -> str:
    return shutil.which("pandoc") or ""


def available() -> bool:
    return bool(pandoc_path())


def default_reference() -> Path:
    return Path(__file__).resolve().parent.parent / "tools" / "templates" / "report-reference.docx"


def typeset(md_path: str | Path, out_docx: str | Path,
            reference_docx: str | Path | None = None,
            timeout: int = 120) -> dict:
    md_path = Path(md_path)
    out_docx = Path(out_docx)
    ref = Path(reference_docx) if reference_docx else default_reference()
    exe = pandoc_path()
    if not exe:
        raise TypesetError("找不到 pandoc，无法用模板排版")
    if not md_path.is_file():
        raise TypesetError("排版稿不存在：" + str(md_path))
    if not ref.is_file():
        raise TypesetError("Word 样式模板不存在：" + str(ref) + "（先运行 tools/make_reference_docx.py）")

    out_docx.parent.mkdir(parents=True, exist_ok=True)
    command = [
        exe, md_path.name,
        "-o", str(out_docx.resolve()),
        "--from", "markdown+implicit_figures",
        "--reference-doc", str(ref.resolve()),
        "--resource-path", str(md_path.parent.resolve()),
    ]
    result = subprocess.run(command, cwd=str(md_path.parent), capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise TypesetError("pandoc 转换失败（退出码 " + str(result.returncode) + "）：" + detail[:600])
    if not out_docx.is_file() or out_docx.stat().st_size == 0:
        raise TypesetError("pandoc 没有生成 docx：" + str(out_docx))

    try:
        from docx import Document

        doc = Document(str(out_docx))
    except Exception as exc:  # noqa: BLE001 - 产物打不开就明确失败
        raise TypesetError("生成的 docx 无法打开：" + str(exc)) from exc
    images = len(doc.inline_shapes)
    if images == 0:
        raise TypesetError("生成的 docx 没有图片，排版稿里的图可能没被识别")
    _postprocess(doc)
    doc.save(str(out_docx))
    return {
        "engine": "pandoc",
        "pandoc": result.stdout.strip()[:200],
        "images": images,
        "paragraphs": len(doc.paragraphs),
        "bytes": out_docx.stat().st_size,
    }


def _postprocess(doc) -> None:
    """Pandoc 不读表格边框等细节，这里统一补三线表和居中图片。"""
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    for paragraph in doc.paragraphs:
        if paragraph._element.xpath(".//w:drawing"):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.first_line_indent = Pt(0)
            paragraph.paragraph_format.keep_with_next = True
        if paragraph.style is not None and paragraph.style.name == "Image Caption":
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.first_line_indent = Pt(0)

    for table in doc.tables:
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        tbl_pr = table._tbl.tblPr
        borders = tbl_pr.find(qn("w:tblBorders"))
        if borders is None:
            borders = OxmlElement("w:tblBorders")
            tbl_pr.append(borders)
        for edge, size in (("top", "12"), ("bottom", "12"), ("insideH", "4")):
            node = borders.find(qn("w:" + edge))
            if node is None:
                node = OxmlElement("w:" + edge)
                borders.append(node)
            node.set(qn("w:val"), "single")
            node.set(qn("w:sz"), size)
            node.set(qn("w:space"), "0")
            node.set(qn("w:color"), "1E1C19")
        for edge in ("left", "right", "insideV"):
            node = borders.find(qn("w:" + edge))
            if node is None:
                node = OxmlElement("w:" + edge)
                borders.append(node)
            node.set(qn("w:val"), "nil")
        count = max(1, len(table.columns))
        first_width = 3.0
        rest_width = (14.6 - first_width) / max(1, count - 1)
        for row in table.rows:
            for index, cell in enumerate(row.cells):
                cell.width = Cm(first_width if index == 0 else rest_width)
        width = OxmlElement("w:tblW")
        width.set(qn("w:type"), "dxa")
        width.set(qn("w:w"), str(int(14.6 * 567)))
        tbl_pr.append(width)
        margins = OxmlElement("w:tblCellMar")
        for edge, value in (("top", 28), ("bottom", 28), ("left", 57), ("right", 57)):
            node = OxmlElement("w:" + edge)
            node.set(qn("w:w"), str(value))
            node.set(qn("w:type"), "dxa")
            margins.append(node)
        tbl_pr.append(margins)
        for row in table.rows:
            cant_split = OxmlElement("w:cantSplit")
            row._tr.get_or_add_trPr().append(cant_split)
        for row_index, row in enumerate(table.rows):
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    paragraph.paragraph_format.first_line_indent = Pt(0)
                    paragraph.paragraph_format.space_before = Pt(2)
                    paragraph.paragraph_format.space_after = Pt(2)
                    for run in paragraph.runs:
                        run.font.size = Pt(10)
                        run.font.name = "宋体"
                        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
                        if row_index == 0:
                            run.font.bold = True
