"""
scipilot-figure-skill :: check_figure.py
========================================
Pre-submission figure compliance audit.

逐文件检查：格式（矢量 vs 位图 vs 不允许的 JPEG）、像素尺寸/DPI、
矢量 PDF 的字体嵌入类型（必须 TrueType/Type 42），输出问题清单。
非破坏性——只读、不修改原图。

Usage
-----
    from check_figure import check_figure, print_report
    issues, info = check_figure("figs/fig1.pdf", min_dpi=300, target_inches=(3.5, 2.625))
    print_report("figs/fig1.pdf", issues, info)

CLI:
    python check_figure.py figs/fig1.pdf figs/fig2.png --min-dpi 300
    python check_figure.py figs/*.pdf --strict   # 任意 FAIL 即 exit 2
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from typing import Any

JPEG_FORMATS = {"jpg", "jpeg"}
VECTOR_FORMATS = {"pdf", "svg"}
RASTER_OK_FORMATS = {"png", "tiff", "tif"}

# Severity 序号越高越严重
SEVERITY = {"INFO": 0, "WARN": 1, "FAIL": 2}


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def _check_raster(path: str, ext: str, min_dpi: int,
                  target_inches: tuple[float, float] | None,
                  strict: bool = False) -> tuple[list, dict]:
    """位图（PNG/TIFF/JPEG）合规性检查。"""
    issues: list[tuple[str, str]] = []
    info: dict[str, Any] = {"category": "raster", "ext": ext}

    if ext in JPEG_FORMATS:
        issues.append(("FAIL",
                       "JPEG 不适合线条/文字类数据图（有损压缩）。"
                       "改用 PDF/SVG（矢量）或 PNG/TIFF（位图）。"))

    try:
        from PIL import Image
    except ImportError:
        issues.append(("FAIL" if strict else "INFO",
                       "Pillow 未安装，无法核实像素与 DPI —— 严格模式下按不通过处理："
                       "pip install Pillow 后可启用。"))
        return issues, info

    try:
        img = Image.open(path)
        info["size_px"] = img.size  # (w, h)
        dpi = img.info.get("dpi")
        info["dpi"] = dpi
    except Exception as e:
        issues.append(("FAIL", f"无法读取图像：{e}"))
        return issues, info

    if not dpi:
        # 没有 DPI 就等于无法证明这张图是 300 DPI，不能只给个 WARN 放过去
        issues.append(("FAIL",
                       "文件没有 DPI 元数据，无法核实是否达到要求的 "
                       f"{min_dpi} DPI。用 fig.savefig(..., dpi={min_dpi}) 重出一次。"))
    else:
        dx = float(dpi[0]) if isinstance(dpi, (tuple, list)) else float(dpi)
        if not dx:
            issues.append(("FAIL",
                           "DPI 元数据为 0，无法核实分辨率，按未达标处理。"))
            return issues, info
        # PIL roundtrips DPI as float and may give 299.9994 for a value saved
        # at 300; round before compare to absorb that.
        dx_rounded = round(float(dx))
        if dx_rounded < min_dpi:
            issues.append(("FAIL",
                           f"DPI = {dx_rounded} 低于要求的 {min_dpi}。"
                           "重新 savefig(dpi=...) 一次。"))
        if target_inches is not None:
            tw, th = target_inches
            actual_w_in = info["size_px"][0] / float(dx)
            actual_h_in = info["size_px"][1] / float(dx)
            tol = 0.1  # 英寸容差
            if abs(actual_w_in - tw) > tol or abs(actual_h_in - th) > tol:
                issues.append((
                    "WARN",
                    f"实际尺寸 ≈ {actual_w_in:.2f}×{actual_h_in:.2f} in，"
                    f"目标 {tw}×{th} in。请在画图时直接设 figsize=({tw}, {th})，"
                    "不要在 Word/LaTeX 里二次缩放。"
                ))
    return issues, info


def _check_pdf_fonts(path: str) -> list[tuple[str, str]]:
    """检查 PDF 的字体嵌入类型——多家期刊拒收 Type 3 (CFF outlines)。"""
    issues: list[tuple[str, str]] = []
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # noqa: F401
        except ImportError:
            issues.append(("INFO",
                           "pypdf / PyPDF2 未安装，跳过字体嵌入类型检查。"
                           "pip install pypdf 后可启用。"))
            return issues

    try:
        reader = PdfReader(path)
    except Exception as e:
        issues.append(("WARN", f"PDF 无法解析以检查字体：{e}"))
        return issues

    bad_fonts: list[str] = []
    not_embedded: list[str] = []
    for page in reader.pages:
        try:
            resources = page.get("/Resources")
            if not resources:
                continue
            fonts = resources.get("/Font")
            if not fonts:
                continue
            for fname, fobj in fonts.items():
                font = fobj.get_object()
                subtype = str(font.get("/Subtype", ""))
                base = str(font.get("/BaseFont", "?"))
                descriptor = font.get("/FontDescriptor")
                if descriptor:
                    descriptor = descriptor.get_object()
                    embedded = any(k in descriptor for k in
                                   ("/FontFile", "/FontFile2", "/FontFile3"))
                else:
                    embedded = False
                if "Type3" in subtype:
                    bad_fonts.append(f"{base} ({subtype})")
                elif not embedded and "Type1" not in subtype:
                    not_embedded.append(base)
        except Exception:
            continue
    if bad_fonts:
        issues.append((
            "FAIL",
            f"PDF 中含 Type 3 字体: {', '.join(set(bad_fonts))[:200]}. "
            "Type 3 字体放大后会糊，多家期刊拒收。"
            "在 matplotlib 中设 rcParams['pdf.fonttype'] = 42 重出图。"
        ))
    if not_embedded:
        issues.append((
            "WARN",
            f"PDF 中以下字体可能未嵌入: {', '.join(set(not_embedded))[:200]}. "
            "导致他人电脑打开变成替代字体。"
        ))
    return issues


def _check_svg(path: str) -> list[tuple[str, str]]:
    """SVG 简检：警告 base64 嵌入的位图（破坏矢量优势）。"""
    issues: list[tuple[str, str]] = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(50000)
    except Exception as e:
        issues.append(("WARN", f"SVG 读失败: {e}"))
        return issues
    text_nodes = head.count("<text")
    if text_nodes:
        issues.append(("WARN",
                       f"SVG 含 {text_nodes} 个未转曲的 <text> 节点：文字靠阅读端字体渲染，"
                       "换机器可能变成替代字体。导出时设 svg.fonttype='path' 可转曲（等价嵌入）。"))
    if "data:image/png;base64" in head or "data:image/jpeg;base64" in head:
        issues.append(("WARN",
                       "SVG 中包含 base64 嵌入的位图——会丢失矢量优势。"
                       "检查是否在 plot 中误用了 imshow 或图像贴图。"))
    return issues


def check_figure(path: str, min_dpi: int = 300,
                 target_inches: tuple[float, float] | None = None,
                 strict: bool = False
                 ) -> tuple[list[tuple[str, str]], dict]:
    """
    审计一张图。返回 (issues, info)。
    issues: [(severity, message), ...]; severity ∈ {INFO, WARN, FAIL}
    info: 元数据字典（格式、像素、DPI 等）
    """
    issues: list[tuple[str, str]] = []
    info: dict[str, Any] = {"path": path}

    if not os.path.exists(path):
        return [("FAIL", f"文件不存在: {path}")], info

    ext = _ext(path)
    info["ext"] = ext
    info["size_bytes"] = os.path.getsize(path)

    if ext == "eps":
        info["category"] = "vector"
        issues.append(("WARN",
                       "EPS 未做合规检查（本工具只审 SVG/PDF/PNG/TIFF），"
                       "交付前请改用 PDF 或 SVG。"))
    elif ext in VECTOR_FORMATS:
        info["category"] = "vector"
        if ext == "pdf":
            issues.extend(_check_pdf_fonts(path))
        elif ext == "svg":
            issues.extend(_check_svg(path))
    elif ext in RASTER_OK_FORMATS or ext in JPEG_FORMATS:
        sub_issues, sub_info = _check_raster(path, ext, min_dpi, target_inches, strict)
        issues.extend(sub_issues)
        info.update(sub_info)
    else:
        issues.append(("WARN", f"未识别的扩展名: .{ext}"))

    return issues, info


def print_report(path: str, issues: list, info: dict) -> str:
    """以可读格式打印一张图的审计结果，返回 overall verdict 字符串。"""
    print(f"\n--- {path} ---")
    if "category" in info:
        print(f"  category: {info['category']}  ext: {info['ext']}  "
              f"size: {info.get('size_bytes', '?')} B")
    if info.get("size_px"):
        print(f"  pixels: {info['size_px'][0]}x{info['size_px'][1]}  "
              f"dpi: {info.get('dpi')}")

    if not issues:
        print("  [PASS] 无问题。")
        return "PASS"

    max_sev = max(SEVERITY[s] for s, _ in issues)
    verdict = {2: "FAIL", 1: "WARN", 0: "INFO"}[max_sev]
    for severity, msg in sorted(issues, key=lambda x: -SEVERITY[x[0]]):
        print(f"  [{severity}] {msg}")
    print(f"  >>> verdict: {verdict}")
    return verdict


FIGURE_EXTS = (".svg", ".png", ".pdf", ".jpg", ".jpeg", ".tif", ".tiff")


def _expand(paths: list[str]) -> tuple[list[str], list[str]]:
    """把目录与 glob 展开成图文件清单。

    目录只取一层内的图文件；展开不出任何图文件的目录记为未解析，
    由调用方决定是否视为失败——传一个空目录不算检查通过。
    """
    files: list[str] = []
    unresolved: list[str] = []
    for pat in paths:
        hits = glob.glob(pat) or [pat]
        for hit in hits:
            if os.path.isdir(hit):
                found = [
                    p for p in sorted(glob.glob(os.path.join(hit, "*")))
                    if os.path.splitext(p)[1].lower() in FIGURE_EXTS
                ]
                if found:
                    files.extend(found)
                else:
                    unresolved.append(hit)
            else:
                files.append(hit)

    seen: set[str] = set()
    unique: list[str] = []
    for f in files:
        key = os.path.abspath(f)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique, unresolved


def _cli() -> int:
    p = argparse.ArgumentParser(description="scipilot-figure-skill compliance checker")
    p.add_argument("paths", nargs="+", help="图文件路径或目录，可用 glob")
    p.add_argument("--min-dpi", type=int, default=300)
    p.add_argument("--width-in", type=float, help="目标宽度(英寸)")
    p.add_argument("--height-in", type=float, help="目标高度(英寸)")
    p.add_argument("--strict", action="store_true",
                   help="任意 FAIL 或没有可检文件即 exit code 2")
    args = p.parse_args()

    target = None
    if args.width_in and args.height_in:
        target = (args.width_in, args.height_in)

    expanded, unresolved = _expand(args.paths)
    for path in unresolved:
        print(f"--- {path} ---")
        print("  [FAIL] 目录内没有可检的图文件（svg/png/pdf/jpg/tif）")
        print("  >>> verdict: FAIL")

    any_fail = bool(unresolved)
    for path in expanded:
        issues, info = check_figure(path, min_dpi=args.min_dpi, target_inches=target,
                                    strict=args.strict)
        verdict = print_report(path, issues, info)
        if verdict == "FAIL":
            any_fail = True

    print()
    print(f"[scipilot-figure-skill] checked {len(expanded)} file(s)")
    if not expanded and not unresolved:
        print("[scipilot-figure-skill] no file matched — nothing was checked")
        return 2 if args.strict else 0
    if unresolved:
        # 传进来的目录里一张图都没有，上面已经逐条打了 FAIL，退出码不能是 0
        print("[scipilot-figure-skill] some paths had no figure — exit 2")
        return 2
    if args.strict and any_fail:
        print("[scipilot-figure-skill] strict mode: at least one FAIL — exit 2")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
