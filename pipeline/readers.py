"""输入文件的取证式读取。

哈希、行数、表名、列名、日期范围一律现取，取不到就留空并写 error，不估算、不编造。
本模块对外只返回 FileInfo 与原始文本/键值，不做任何业务判断。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pandas as pd

from .models import FileInfo

ENCODINGS = ("utf-8-sig", "gbk", "utf-8", "gb18030")
TEXT_FORMATS = ("md", "txt", "docx", "markdown")
TABLE_FORMATS = ("csv", "tsv", "xlsx", "xlsm", "parquet")

_DATE_NAME_HINT = re.compile(r"日期|时间|date|_dt|^ds$", re.I)
_DATE_VALUE_HINT = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}")


def sha256_file(path: str | Path) -> str:
    """分块计算 sha256 十六进制摘要。"""
    digest = hashlib.sha256()
    with open(Path(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: str | Path) -> str:
    """按 md/txt/docx 读正文，编码依次尝试 utf-8-sig、gbk。"""
    path = Path(path)
    fmt = path.suffix.lower().lstrip(".")
    if fmt == "docx":
        return _docx_text(path)
    if fmt in ("md", "txt", "markdown", ""):
        return _plain_text(path)
    raise ValueError(f"不是文本格式：{fmt}")


def read_key_values(path: str | Path, max_unique: int = 200, limit: int = 3000) -> list[str]:
    """读对象列（唯一值 <= max_unique）的去重值，供实体匹配使用；不读度量列与日期列。"""
    values: list[str] = []
    for _name, frame in load_tables(Path(path)):
        for col in frame.columns:
            if _DATE_NAME_HINT.search(str(col)):
                continue
            series = frame[col]
            if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
                continue
            cleaned = series.dropna().astype(str).str.strip()
            uniques = [value for value in pd.unique(cleaned) if value]
            if 0 < len(uniques) <= max_unique:
                values.extend(uniques)
            if len(values) >= limit:
                return values[:limit]
    return values


def column_dtypes(path: str | Path) -> dict[str, str]:
    """取主表各列 dtype，用于列结构比对；失败返回空字典。"""
    try:
        tables = load_tables(Path(path))
    except Exception:
        return {}
    if not tables:
        return {}
    _name, frame = max(tables, key=lambda item: len(item[1]))
    return {str(col): str(frame[col].dtype) for col in frame.columns}


def load_tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    """装载表格，返回 [(sheet 名, DataFrame)]；csv/parquet 的 sheet 名为空串。"""
    fmt = path.suffix.lower().lstrip(".")
    if fmt in ("csv", "tsv"):
        return [("", _read_csv(path))]
    if fmt in ("xlsx", "xlsm"):
        return [(name, pd.read_excel(path, sheet_name=name, engine="openpyxl"))
                for name in _sheet_names(path)]
    if fmt == "parquet":
        return [("", pd.read_parquet(path))]
    raise ValueError(f"不是表格格式：{fmt or '无扩展名'}")


def _read_csv(path: Path) -> pd.DataFrame:
    sep = "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    last: Exception | None = None
    for enc in ENCODINGS:
        try:
            return pd.read_csv(path, sep=sep, encoding=enc)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            last = exc
    raise ValueError(f"CSV 解码或解析失败：{last}")


def _sheet_names(path: Path) -> list[str]:
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    try:
        return list(book.sheetnames)
    finally:
        book.close()


def read_file(path: str | Path) -> FileInfo:
    """读取一个文件并填 FileInfo；任何异常都不外抛，转成 readable=False + error。"""
    path = Path(path)
    info = FileInfo(name=path.name, format=path.suffix.lower().lstrip("."))
    try:
        if not path.is_file():
            info.readable = False
            info.error = "文件不存在或不是普通文件"
            return info
        info.size = path.stat().st_size
    except OSError as exc:
        info.readable = False
        info.error = f"读取文件属性失败：{exc}"
        return info

    try:
        info.sha256 = sha256_file(path)
    except OSError as exc:
        info.readable = False
        info.error = f"计算 sha256 失败：{exc}"
        return info

    if info.size == 0:
        info.readable = False
        info.error = "文件为空（0 字节）"
        return info

    try:
        if info.format in ("csv", "tsv", "xlsx", "xlsm", "parquet"):
            _fill_table(info, path)
        elif info.format in ("md", "txt", "markdown"):
            _fill_text(info, path)
        elif info.format == "docx":
            _fill_docx(info, path)
        else:
            info.readable = False
            info.error = f"不支持的格式：{info.format or '无扩展名'}"
    except Exception as exc:  # noqa: BLE001 - 读取失败一律降级为不可读
        info.readable = False
        info.error = f"读取失败：{type(exc).__name__}: {exc}"
    return info


def _fill_table(info: FileInfo, path: Path) -> None:
    tables = load_tables(path)
    if info.format in ("xlsx", "xlsm"):
        info.sheets = [name for name, _ in tables]

    trimmed = [(name, frame.dropna(how="all")) for name, frame in tables]
    nonempty = [(name, frame) for name, frame in trimmed if not frame.empty]

    if info.format in ("xlsx", "xlsm"):
        info.tables = [name for name, _ in nonempty]
        info.rows = sum(len(frame) for _, frame in nonempty)
        main = max(nonempty, key=lambda item: len(item[1])) if nonempty else None
        picked = main[1] if main else (trimmed[0][1] if trimmed else None)
        range_frames = [frame for _, frame in nonempty]
    else:
        info.rows = len(trimmed[0][1]) if trimmed else 0
        picked = trimmed[0][1] if trimmed else None
        range_frames = [picked] if picked is not None else []

    if picked is not None:
        info.columns = [str(col) for col in picked.columns]
    info.date_range = _format_range(_date_span(range_frames))


def _fill_text(info: FileInfo, path: Path) -> None:
    text = _plain_text(path)
    info.rows = len(text.splitlines())
    info.columns = []
    info.date_range = ""


def _fill_docx(info: FileInfo, path: Path) -> None:
    text = _docx_text(path)
    info.rows = sum(1 for line in text.splitlines() if line.strip())
    info.columns = []
    info.date_range = ""


def _date_span(frames: list[pd.DataFrame]) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    low = high = None
    for frame in frames:
        if frame is None:
            continue
        for col in frame.columns:
            series = _col_dates(frame[col], str(col))
            if series is None or series.empty:
                continue
            try:
                col_low, col_high = series.min(), series.max()
            except (TypeError, ValueError):
                continue
            if pd.isna(col_low) or pd.isna(col_high):
                continue
            low = col_low if low is None else min(low, col_low)
            high = col_high if high is None else max(high, col_high)
    if low is None or high is None:
        return None
    return pd.Timestamp(low), pd.Timestamp(high)


def _col_dates(series: pd.Series, name: str) -> pd.Series | None:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dropna()
    if not _DATE_NAME_HINT.search(name):
        return None
    sample = series.dropna()
    if sample.empty:
        return None
    head = sample.astype(str).head(50)
    if not any(_DATE_VALUE_HINT.search(value) for value in head):
        return None
    try:
        parsed = pd.to_datetime(sample, errors="coerce")
    except (TypeError, ValueError):
        return None
    return parsed.dropna()


def _format_range(span: tuple[pd.Timestamp, pd.Timestamp] | None) -> str:
    if span is None:
        return ""
    start, end = span
    return f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d}"


def _plain_text(path: Path) -> str:
    raw = path.read_bytes()
    last: Exception | None = None
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError as exc:
            last = exc
    raise ValueError(f"无法按 utf-8/gbk 解码：{last}")


def _docx_text(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover
        raise ValueError(f"缺少 python-docx，无法读 docx：{exc}") from exc

    doc = Document(str(path))
    parts = [paragraph.text for paragraph in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(parts)
