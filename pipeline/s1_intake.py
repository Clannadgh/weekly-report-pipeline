"""S1 准入检查：清点、读纪要定需求、按需求匹配数据、判定状态并落盘。

判定依据是 s1.txt：暂停 > 待补充 > 准入通过。
下游只读 intake.json / status.json，不读本步对话。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from .models import (
    ColumnMismatch,
    Conflict,
    Consistency,
    Counts,
    Coverage,
    DateOverlap,
    DegradeItem,
    DuplicateFile,
    FileInfo,
    IntakeResult,
    MinutesInfo,
    MissingItem,
    PromptInfo,
    Requirement,
    RunInfo,
    Status,
    StatusHistory,
)
from .period import iso_week_tag, parse_period, parse_week, parse_year, shift_days, week_label
from .readers import column_dtypes, read_file, read_key_values, read_text

_TEXT_FORMATS = ("md", "txt", "docx", "markdown")

# 对外展示的 8 个阶段，顺序与 run_intake 的执行顺序一致
STEPS = ["清点与角色", "读纪要", "读提示词", "建运行目录",
         "需求矩阵", "一致性", "判定", "落盘"]

_AMOUNT_HINTS = ("实收", "销售额", "营收", "收入", "销售金额", "成交额", "金额", "amount", "revenue", "sales")
_AMOUNT_EXCLUDE = ("退货", "退单", "退款", "成本", "目标", "毛利", "数量", "销量")
_COST_HINTS = ("成本", "cost")
_GROSS_HINTS = ("毛利率", "毛利", "gross", "margin")
_TARGET_HINTS = ("目标", "达成", "target", "plan")
_QTY_HINTS = ("数量", "销量", "件数", "qty", "quantity")
_RETURN_HINTS = ("退货", "退单", "退款")
_CHANNEL_HINTS = ("渠道", "线上", "线下", "channel")
_DATE_HINTS = ("日期", "时间", "date", "dt")

_METRIC_RULES = (
    ("实收", ("实收", "销售额", "营收", "收入")),
    ("数量", ("数量", "销量", "件数")),
    ("毛利率", ("毛利",)),
    ("目标", ("目标", "达成")),
    ("退货", ("退货", "退单")),
    ("渠道", ("渠道", "线上", "线下")),
)

_WEEK_RE = re.compile(r"第\s*(\d{1,2})\s*周|(?<![A-Za-z])[Ww](\d{1,2})(?!\d)")
_STORE_CODE_RE = re.compile(r"[A-Z]{2}\d{2,}")
_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*至\s*(\d{4}-\d{2}-\d{2})$")


# ---------------------------------------------------------------- 通用小工具

def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _same_path(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    return _path_key(a) == _path_key(b)


def _split_range(text: str) -> tuple[date, date] | None:
    match = _RANGE_RE.match((text or "").strip())
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
    except ValueError:
        return None


def _pick(columns: list[str], hints: tuple[str, ...], exclude: tuple[str, ...] = ()) -> str:
    for col in columns:
        low = col.lower()
        if any(token in low for token in exclude):
            continue
        if any(hint.lower() in low for hint in hints):
            return col
    return ""


def _week_number(text: str) -> int:
    match = _WEEK_RE.search(text or "")
    if not match:
        return 0
    return int(match.group(1) or match.group(2))


def _norm_dtype(dtype: str) -> str:
    value = (dtype or "").lower()
    if not value or value in ("object", "string", "str"):
        return ""
    if "int" in value or "float" in value or "decimal" in value:
        return "number"
    if "datetime" in value:
        return "datetime"
    if "bool" in value:
        return "bool"
    return value


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 角色判定

def _detect_role(path: Path, info: FileInfo, forced: bool = False) -> str:
    """按内容判定角色，不看文件名（文件名只用于「规范」这一条兜底）。"""
    if forced:
        return "规范"
    if info.format in _TEXT_FORMATS:
        if not info.readable:
            return "数据"
        try:
            text = read_text(path)
        except Exception:
            return "数据"
        if "禁用词" in text and "篇幅" in text:
            return "规范"
        if _looks_like_minutes(text):
            return "纪要"
    return "数据"


def _looks_like_minutes(text: str) -> bool:
    """表格状文本先排掉，避免把数据文件当纪要；再看关键词与周期。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if lines:
        tabular = sum(1 for line in lines if line.count(",") + line.count("\t") >= 3)
        if tabular / len(lines) > 0.4:
            return False
    hits = sum(1 for keyword in ("周", "纪要", "会议", "责任人") if keyword in text)
    if hits >= 2:
        return True
    return hits >= 1 and parse_period(text, fallback_year=date.today().year) is not None


# ---------------------------------------------------------------- 纪要抽取

def _extract_metrics(text: str) -> list[str]:
    found: list[str] = []
    for name, keywords in _METRIC_RULES:
        if any(keyword in text for keyword in keywords):
            found.append(name)
    return found


def _core_metrics(text: str, metrics: list[str]) -> set[str]:
    """纪要里写成「核心指标」的那句里出现的指标，按核心处理。"""
    core = {"实收", "毛利率", "目标达成率"}
    for sentence in re.split(r"[。；\n]", text or ""):
        if "核心指标" in sentence:
            for metric in metrics:
                if metric in sentence:
                    core.add(metric)
    return core


def _count_actions(text: str) -> int:
    """优先数行动项列表与表格行，数不到再退回句子计数。"""
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|") and stripped.count("|") >= 3:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if any(re.search(r"\d{1,4}\s*[-/月]\s*\d{1,2}", cell) for cell in cells):
                count += 1
            continue
        if re.match(r"^([-*+]|\d+[.、)])\s*", stripped) and any(
                key in stripped for key in ("责任人", "截止", "前完成", "前补齐", "跟进")):
            count += 1
    if count:
        return count
    sentences = re.split(r"[。！？!?\n；;]", text)
    return sum(1 for sentence in sentences
               if any(k in sentence for k in ("责任人", "截止")))


def _is_date_like(value: str) -> bool:
    """日期串、纯数字不当实体。"""
    text = value.strip()
    if re.fullmatch(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?", text):
        return True
    if re.fullmatch(r"\d{1,2}\s*月\s*\d{1,2}\s*日?", text):
        return True
    return bool(re.fullmatch(r"[\d.\-]+", text))


def _collect_entities(data_recs: list[tuple[Path, FileInfo]], text: str):
    vocab: set[str] = set()
    for path, _info in data_recs:
        try:
            vocab.update(read_key_values(path))
        except Exception:
            continue
        if len(vocab) > 4000:
            break

    hits = sorted(
        value for value in vocab
        if len(value) >= 2 and value in text and not _is_date_like(value)
    )

    codes: list[str] = []
    for match in _STORE_CODE_RE.finditer(text):
        code = match.group(0)
        if code not in codes:
            codes.append(code)

    entities: list[str] = []
    for value in hits + codes:
        if value not in entities:
            entities.append(value)
    return entities[:50], vocab, codes


# ---------------------------------------------------------------- 需求矩阵

def _build_requirements(
    data_recs: list[tuple[Path, FileInfo]],
    metrics: list[str],
    merged: tuple[date, date] | None,
    compare: tuple[str, str],
    core_metrics: set[str] | None = None,
) -> tuple[list[Requirement], list[MissingItem], list[DegradeItem]]:
    columns: list[str] = []
    for _path, info in data_recs:
        for col in info.columns:
            if col not in columns:
                columns.append(col)

    amount = _pick(columns, _AMOUNT_HINTS, _AMOUNT_EXCLUDE)
    cost = _pick(columns, _COST_HINTS)
    gross = _pick(columns, _GROSS_HINTS)
    target = _pick(columns, _TARGET_HINTS)
    quantity = _pick(columns, _QTY_HINTS)
    returns = _pick(columns, _RETURN_HINTS)
    channel = _pick(columns, _CHANNEL_HINTS)
    date_col = _pick(columns, _DATE_HINTS)
    has_date = bool(date_col) or any(info.date_range for _p, info in data_recs)
    date_label = date_col or ("datetime 列" if has_date else "无")

    brief = "；".join(
        f"{info.name}（{info.rows} 行，{info.date_range or '日期范围未取到'}）"
        for _p, info in data_recs
    ) or "无数据文件"

    compare_text = f"{compare[0]} 至 {compare[1]}" if compare[0] and compare[1] else "对比期未定"
    compare_ok = bool(
        merged
        and compare[0]
        and compare[1]
        and merged[0] <= date.fromisoformat(compare[0])
        and merged[1] >= date.fromisoformat(compare[1])
    )
    merged_text = f"{merged[0].isoformat()} 至 {merged[1].isoformat()}" if merged else "未取到日期范围"

    requirements: list[Requirement] = []
    missing: list[MissingItem] = []
    degrade: list[DegradeItem] = []

    def add(item: str, kind: str, needs: list[str], status: str, evidence: str,
            impact: str = "", note: str = "", how: str = "") -> None:
        requirements.append(Requirement(item=item, kind=kind, needs=needs, status=status,
                                        evidence=evidence, impact=impact, note=note))
        if status == "missing":
            missing.append(MissingItem(what=item, impact=impact or "报告缺该项",
                                       how=how or f"补充「{item}」所需列后重跑 S1"))
        elif status == "degrade":
            degrade.append(DegradeItem(item=item, impact=impact or note))

    # 1 实收
    if amount and has_date:
        add("实收", "核心",
            ["金额列（实收/销售额/营收/收入/amount）", "日期列（日期/时间/date）"],
            "have", f"{brief}；金额列「{amount}」，日期列「{date_label}」",
            impact="用于第 2 节核心指标表与品类表现")
    elif amount:
        add("实收", "核心",
            ["金额列（实收/销售额/营收/收入/amount）", "日期列（日期/时间/date）"],
            "degrade", f"{brief}；金额列「{amount}」，无日期列",
            impact="只能出全场合计，无法按周切分与出趋势",
            note="缺日期列，实收按整表合计统计，报告需注明统计区间不可切分")
    else:
        add("实收", "核心",
            ["金额列（实收/销售额/营收/收入/amount）", "日期列（日期/时间/date）"],
            "missing", f"{brief}；共 {len(columns)} 列，无实收/销售额/营收/收入类金额列",
            impact="第 2 节核心指标表无法出数，整篇周报失去主指标",
            how="补充含实收金额（或销售额/营收）列的数据文件")

    # 2 环比
    if amount and has_date and compare_ok:
        add("环比", "核心",
            ["金额列", "日期列", f"对比期数据（{compare_text}）"],
            "have", f"{brief}；金额列「{amount}」，日期列「{date_label}」，合并覆盖 {merged_text} 已含对比期 {compare_text}",
            impact="用于第 2 节环比与第 4 节超阈值清单")
    elif amount and has_date:
        add("环比", "核心",
            ["金额列", "日期列", f"对比期数据（{compare_text}）"],
            "degrade", f"{brief}；金额列「{amount}」，日期列「{date_label}」，合并覆盖 {merged_text} 不含对比期 {compare_text}",
            impact="较上周一列无法计算，第 4 节超阈值筛选失效",
            note=f"数据未覆盖对比期 {compare_text}，环比改报本期绝对值并在第 5 节标注「无对比期数据」")
    else:
        add("环比", "核心",
            ["金额列", "日期列", f"对比期数据（{compare_text}）"],
            "missing", f"{brief}；金额列「{amount or '无'}」，日期列「{date_label}」",
            impact="无法计算较上周，第 2、4 节均缺环比",
            how="补充同时含金额列与日期列的数据，并覆盖对比期")

    # 3 毛利率
    if gross:
        add("毛利率", "核心",
            ["毛利率列，或 金额列 + 成本列"],
            "have", f"{brief}；毛利率列「{gross}」" + (f"，金额列「{amount}」" if amount else ""),
            impact="用于第 2 节核心指标表与第 3 节品类表现")
    elif amount and cost:
        add("毛利率", "核心",
            ["毛利率列，或 金额列 + 成本列"],
            "have", f"{brief}；无毛利率列，有金额列「{amount}」与成本列「{cost}」",
            impact="用于第 2 节核心指标表与第 3 节品类表现",
            note=f"用 (「{amount}」-「{cost}」)/「{amount}」推算毛利率，第 5 节披露成本口径")
    else:
        add("毛利率", "核心",
            ["毛利率列，或 金额列 + 成本列"],
            "missing", f"{brief}；共 {len(columns)} 列，无毛利/毛利率列，也无成本列",
            impact="第 2 节核心指标表毛利率一列无法出数",
            how="补充成本列或毛利率列")

    # 4 目标达成率
    if target and amount:
        add("目标达成率", "核心",
            ["目标列（目标/达成/target）", "金额列"],
            "have", f"{brief}；目标列「{target}」，金额列「{amount}」",
            impact="用于第 2 节核心指标表")
    elif amount:
        add("目标达成率", "核心",
            ["目标列（目标/达成/target）", "金额列"],
            "degrade", f"{brief}；金额列「{amount}」，无目标列",
            impact="达成率一列改为较上周环比呈现，目标缺口无法量化",
            note="无目标列，目标达成率改用「较上周」代替，第 2 节表头标注「（无目标，改列较上周）」")
    elif target:
        add("目标达成率", "核心",
            ["目标列（目标/达成/target）", "金额列"],
            "degrade", f"{brief}；目标列「{target}」，无金额列",
            impact="只能列目标值，无法计算达成率",
            note="有目标列但无实收金额列，达成率留空，仅报目标值")
    else:
        add("目标达成率", "核心",
            ["目标列（目标/达成/target）", "金额列"],
            "missing", f"{brief}；共 {len(columns)} 列，无目标/达成列",
            impact="第 2 节核心指标表目标达成率一列无法出数",
            how="补充目标金额列")

    # 5 补充项（来自纪要）
    if "数量" in metrics:
        quantity_kind = "核心" if core_metrics and "数量" in core_metrics else "补充"
        if quantity:
            add("数量", quantity_kind, ["数量列（数量/销量/件数/qty）"], "have",
                f"{brief}；数量列「{quantity}」", impact="用于第 3 节品类表现")
        else:
            add("数量", quantity_kind, ["数量列（数量/销量/件数/qty）"], "missing",
                f"{brief}；共 {len(columns)} 列，无数量/销量/件数列",
                impact="第 3 节只能报金额，不能报件数结构",
                how="补充数量列；确无法补充时经人确认后不写该维度")

    if "退货" in metrics:
        if returns and amount:
            add("退货", "补充", ["退货列（退货/退单/退款）", "金额列"], "have",
                f"{brief}；退货列「{returns}」，金额列「{amount}」", impact="用于第 5 节数据质量")
        elif returns:
            add("退货", "补充", ["退货列（退货/退单/退款）", "金额列"], "degrade",
                f"{brief}；退货列「{returns}」，无金额列",
                impact="只能报退货笔数，不能报退货金额占比",
                note="退货仅报笔数，不参与金额口径")
        else:
            add("退货", "补充", ["退货列（退货/退单/退款）", "金额列"], "missing",
                f"{brief}；共 {len(columns)} 列，无退货/退单列",
                impact="第 5 节无法量化退货，只能写「本期未取到退货数据」",
                how="补充退货金额列；确无法补充时在第 5 节披露")

    if "渠道" in metrics:
        if channel:
            add("渠道", "补充", ["渠道列（渠道/线上/线下/channel）"], "have",
                f"{brief}；渠道列「{channel}」", impact="用于第 3 节品类与渠道拆分")
        else:
            add("渠道", "补充", ["渠道列（渠道/线上/线下/channel）"], "missing",
                f"{brief}；共 {len(columns)} 列，无渠道/线上/线下列",
                impact="渠道维度无法展开，报告只做整体与品类",
                how="补充渠道列；确无法补充时经人确认后不写渠道段")

    return requirements, missing, degrade


# ---------------------------------------------------------------- 一致性

def _dtype_of(path: Path, cache: dict[str, dict[str, str]]) -> dict[str, str]:
    key = _path_key(path)
    if key not in cache:
        try:
            cache[key] = column_dtypes(path)
        except Exception:
            cache[key] = {}
    return cache[key]


def _build_consistency(
    data_recs: list[tuple[Path, FileInfo]],
    same_hash: list[DuplicateFile],
    prompt_period: tuple[date, date] | None,
    minutes_period: tuple[date, date] | None,
    merged: tuple[date, date] | None,
    prompt_week: int,
    minutes_week: int,
    minutes_label: str,
    unknown_codes: list[str],
) -> tuple[Consistency, Coverage]:
    consistency = Consistency()
    consistency.cross_file_duplicates.extend(same_hash)

    # 列结构
    union: list[str] = []
    for _path, info in data_recs:
        for col in info.columns:
            if col not in union:
                union.append(col)

    dtype_cache: dict[str, dict[str, str]] = {}
    for col in union:
        holders = [(p, info) for p, info in data_recs if col in info.columns]
        lacking = [info.name for _p, info in data_recs if col not in info.columns]
        if not lacking or not holders:
            continue
        types: dict[str, str] = {}
        for path, info in holders:
            dtype = _dtype_of(path, dtype_cache).get(col, "")
            if dtype:
                types[info.name] = dtype
        consistency.column_mismatch.append(
            ColumnMismatch(column=col, missing_in=lacking, types=types)
        )
        if len(types) >= 2:
            normalized = {value for value in (_norm_dtype(dt) for dt in types.values()) if value}
            if len(normalized) > 1:
                detail = "，".join(f"{name}={types[name]}" for name in types)
                consistency.conflicts.append(
                    Conflict(kind="caliber", detail=f"列「{col}」跨文件类型不一致：{detail}")
                )

    # 时间重叠与内容重复
    for index in range(len(data_recs)):
        for other in range(index + 1, len(data_recs)):
            path_a, info_a = data_recs[index]
            path_b, info_b = data_recs[other]
            span_a = _split_range(info_a.date_range)
            span_b = _split_range(info_b.date_range)
            if not span_a or not span_b:
                continue
            start = max(span_a[0], span_b[0])
            end = min(span_a[1], span_b[1])
            if start > end:
                continue
            if info_a.sha256 and info_a.sha256 == info_b.sha256:
                continue  # 同一份文件不算重叠，避免把「视为同一文件」升级成待补充
            consistency.date_overlap.append(
                DateOverlap(start=start.isoformat(), end=end.isoformat(),
                            files=[info_a.name, info_b.name])
            )
            if info_a.sha256 and info_a.sha256 == info_b.sha256:
                continue
            if info_a.columns and sorted(info_a.columns) == sorted(info_b.columns):
                consistency.cross_file_duplicates.append(
                    DuplicateFile(
                        files=[info_a.name, info_b.name],
                        kind="overlap_content",
                        note=f"列结构相同（{len(info_a.columns)} 列）且日期区间 {start.isoformat()} 至 "
                             f"{end.isoformat()} 重叠，疑似重复或版本冲突，未自动合并",
                    )
                )

    # 周期冲突
    for label, span in (("提示词", prompt_period), ("纪要", minutes_period)):
        if span is None:
            continue
        start, end = span
        if merged is None:
            consistency.conflicts.append(
                Conflict(kind="period",
                         detail=f"{label}周期 {start.isoformat()} 至 {end.isoformat()}，"
                                f"但数据文件未取到日期范围，无法核对覆盖")
            )
        elif start < merged[0] or end > merged[1]:
            consistency.conflicts.append(
                Conflict(kind="period",
                         detail=f"{label}周期 {start.isoformat()} 至 {end.isoformat()} 超出数据合并覆盖 "
                                f"{merged[0].isoformat()} 至 {merged[1].isoformat()}")
            )

    if prompt_week and minutes_week and prompt_week != minutes_week:
        consistency.conflicts.append(
            Conflict(kind="period",
                     detail=(f"提示词周期落在第 {prompt_week} 周，纪要落在第 {minutes_week} 周"
                             f"（{minutes_label}），两者不一致"))
        )

    if unknown_codes:
        consistency.conflicts.append(
            Conflict(kind="entity",
                     detail="纪要提到实体编码 " + "、".join(unknown_codes[:5]) +
                            "，数据键值列中未见对应值")
        )

    coverage = Coverage()
    if merged is not None:
        coverage.merged_start = merged[0].isoformat()
        coverage.merged_end = merged[1].isoformat()
    covers = True
    for span in (prompt_period, minutes_period):
        if span is None:
            continue
        if merged is None or span[0] < merged[0] or span[1] > merged[1]:
            covers = False
    coverage.covers_prompt_scope = covers
    return consistency, coverage


# ---------------------------------------------------------------- 运行目录

def _choose_run_dir(run_root: Path, tag: str) -> Path:
    index = 1
    candidate = run_root / tag
    while candidate.exists():
        state = ""
        status_file = candidate / "status.json"
        if status_file.is_file():
            try:
                state = str(json.loads(status_file.read_text(encoding="utf-8")).get("state", ""))
            except (OSError, ValueError):
                state = ""
        if state != "已交付":
            break
        index += 1
        candidate = run_root / f"{tag}-{index}"
    return candidate


def _relative_dir(path: Path) -> str:
    absolute = path.absolute()
    try:
        relative = Path(os.path.relpath(absolute, Path.cwd().absolute())).as_posix()
        if not relative.startswith(".."):
            return relative
    except ValueError:
        pass
    return absolute.as_posix()


def _copy_inputs(paths: list[Path], run_dir: Path, log: list[str]) -> dict[str, str]:
    target = run_dir / "inputs"
    if target.exists():
        # 复用同一周的目录时，先清掉上一批输入，避免新旧文件混在一起被下游读到
        for old in target.iterdir():
            try:
                shutil.rmtree(old) if old.is_dir() else old.unlink()
            except OSError as exc:
                log.append(f"清理旧输入 {old.name} 失败：{exc}")
    target.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    used: dict[str, int] = {}
    for path in paths:
        name = path.name
        seen = used.get(name, 0)
        used[name] = seen + 1
        if seen:
            name = f"{path.stem}_{seen + 1}{path.suffix}"
        try:
            shutil.copy2(path, target / name)
        except OSError as exc:
            log.append(f"复制 {path.name} 到 inputs 失败：{exc}")
            continue
        mapping[_path_key(path)] = f"inputs/{name}"
        log.append(f"复制输入 {path.name} → inputs/{name}")
    return mapping


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_history(status_file: Path) -> list[StatusHistory]:
    try:
        raw = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    history: list[StatusHistory] = []
    for item in raw.get("history", []) if isinstance(raw, dict) else []:
        if isinstance(item, dict) and item.get("at"):
            try:
                history.append(StatusHistory(**item))
            except (TypeError, ValueError):
                continue
    return history


# ---------------------------------------------------------------- 主流程

def _requirements_from_map(mapping, data_recs: list[tuple[Path, FileInfo]]):
    """把 AI 的映射转成需求矩阵；通过与否仍按列是否存在来判。"""
    all_columns: set[str] = set()
    for _path, info in data_recs:
        all_columns.update(info.columns)

    requirements: list[Requirement] = []
    missing: list[MissingItem] = []
    degrade: list[DegradeItem] = []
    for metric in mapping.metrics:
        present = [c for c in metric.columns if c in all_columns]
        absent = [c for c in metric.columns if c not in all_columns]
        if not metric.columns:
            status, note = "missing", "数据列里没有对应字段"
        elif not absent:
            status, note = "have", ""
        elif present:
            status, note = "degrade", "部分列缺失：" + "、".join(absent)
        else:
            status, note = "missing", "缺列：" + "、".join(absent)
        evidence = "、".join(present) if present else "无对应列"
        if metric.evidence:
            evidence += "｜纪要：" + metric.evidence[:60]
        requirements.append(Requirement(
            item=metric.name, kind=metric.kind,
            needs=metric.columns or ["未映射到列"],
            status=status, evidence=evidence, note=note,
            impact="对应章节无法出数" if status != "have" else "",
        ))
        if status == "degrade":
            degrade.append(DegradeItem(item=metric.name, impact=note))
        elif status == "missing":
            missing.append(MissingItem(what=metric.name, impact="对应章节无法出数",
                                       how=("补充 " + "、".join(metric.columns)) if metric.columns
                                       else "人工确认该指标用哪些列"))
    return requirements, missing, degrade


def _ai_enabled(use_ai: bool | None) -> bool:
    """默认：有 GLM_API_KEY 就用 AI 做映射；S1_USE_AI=0 可强制关掉。"""
    if use_ai is not None:
        return use_ai
    env = os.environ.get("S1_USE_AI", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        from .llm import get_config

        get_config()
        return True
    except Exception:
        return False


def run_intake(
    files: list[Path],
    prompt: str,
    run_root: Path,
    spec_path: Path | None = None,
    use_ai: bool | None = None,
    progress: "Callable[[int, str, str], None] | None" = None,
) -> tuple[IntakeResult, Status, list[str]]:
    log: list[str] = []

    def _emit(index: int, status: str, detail: str = "") -> None:
        if progress is None:
            return
        try:
            progress(index, STEPS[index], status, detail)
        except Exception:  # noqa: BLE001 - 进度回调不能影响主流程
            pass

    _emit(0, "active", "清点文件与角色")
    prompt_text = (prompt or "").strip()
    run_root = Path(run_root)

    paths: list[Path] = [Path(str(p)) for p in files if str(p).strip()]
    spec = Path(spec_path) if spec_path else None
    if spec is not None and not any(_same_path(spec, path) for path in paths):
        paths.append(spec)

    log.append(f"收到 {len(paths)} 个输入文件，提示词 {len(prompt_text)} 字")

    records: list[tuple[Path, FileInfo]] = []
    for path in paths:
        info = read_file(path)
        records.append((path, info))
        readable = "可读" if info.readable else f"不可读（{info.error}）"
        log.append(
            f"清点 {info.name}：{info.format or '未知格式'}，{info.size} 字节，"
            f"sha256 {info.sha256[:16] or '未取到'}…，{readable}，{info.rows} 行，{len(info.columns)} 列"
        )

    for path, info in records:
        info.role = _detect_role(path, info, forced=_same_path(path, spec) if spec else False)
        log.append(f"判定角色 {info.name} → {info.role}")

    # 重复文件：同 sha256 后出现的记 duplicate_of
    first_by_hash: dict[str, str] = {}
    same_hash: list[DuplicateFile] = []
    for _path, info in records:
        if not info.sha256:
            continue
        first = first_by_hash.get(info.sha256)
        if first is None:
            first_by_hash[info.sha256] = info.name
            continue
        info.duplicate_of = first
        same_hash.append(
            DuplicateFile(files=[first, info.name], kind="same_hash",
                          note=f"sha256 相同（{info.sha256[:12]}…），视为同一文件，未自动合并")
        )
        log.append(f"重复文件：{info.name} 与 {first} 哈希相同，记 duplicate_of")

    counts = Counts(
        data=sum(1 for _p, info in records if info.role == "数据"),
        minutes=sum(1 for _p, info in records if info.role == "纪要"),
        prompt=1 if prompt_text else 0,
    )
    log.append(f"数量核对：数据 {counts.data} 个，纪要 {counts.minutes} 个，提示词 {counts.prompt} 条")

    data_recs = [(p, info) for p, info in records if info.role == "数据"]

    _emit(0, "done")
    _emit(1, "active", "解析周期、实体与指标")
    # 读纪要
    minutes = MinutesInfo()
    minutes_text = ""
    minutes_span: tuple[date, date] | None = None
    vocab: set[str] = set()
    codes: list[str] = []
    minutes_rec = next(((p, info) for p, info in records if info.role == "纪要"), None)

    if counts.minutes == 1 and minutes_rec is not None:
        minutes_path, minutes_info = minutes_rec
        if not minutes_info.readable:
            minutes.parse_ok = False
            minutes.note = "纪要文件不可读，无法推导需求"
            log.append("纪要不可读，跳过需求解析")
        else:
            try:
                minutes_text = read_text(minutes_path)
            except Exception as exc:
                minutes.parse_ok = False
                minutes.note = f"纪要正文读取失败：{exc}"
                log.append(f"纪要正文读取失败：{exc}")

    minutes_span: tuple[date, date] | None = None
    minutes_span_from_week = False
    if minutes_text:
        minutes_year = parse_year(minutes_text) or date.today().year
        minutes_span = parse_period(minutes_text, fallback_year=minutes_year)
        if minutes_span is None:
            minutes_span = parse_week(minutes_text, fallback_year=minutes_year)
            minutes_span_from_week = minutes_span is not None
        if minutes_span is None:
            minutes.parse_ok = False
            minutes.note = "纪要正文未找到可解析的周期区间"
            log.append("纪要周期解析失败")
        else:
            start, end = minutes_span
            minutes.period_start = start.isoformat()
            minutes.period_end = end.isoformat()
            minutes.compare_start = shift_days(start, -7).isoformat()
            minutes.compare_end = shift_days(end, -7).isoformat()
            minutes.week = week_label(start)
            if minutes_span_from_week:
                minutes.note = (f"纪要只写了周次，按 ISO 第 {start.isocalendar()[1]} 周推定为 "
                                f"{start.isoformat()} 至 {end.isoformat()}")
                log.append("纪要未写日期区间，按周次推定周期")
            elif not re.search(r"\d{4}\s*[年./\-]", minutes_text):
                minutes.note = f"纪要未写年份，按当前年份 {date.today().year} 推定"
            log.append(
                f"纪要周期 {minutes.period_start} 至 {minutes.period_end}（{minutes.week}），"
                f"对比期 {minutes.compare_start} 至 {minutes.compare_end}"
            )

        entities, vocab, codes = _collect_entities(data_recs, minutes_text)
        minutes.entities = entities
        minutes.metrics = _extract_metrics(minutes_text)
        minutes.actions = _count_actions(minutes_text)
        log.append(
            f"纪要抽取：实体 {len(minutes.entities)} 个，指标 {'/'.join(minutes.metrics) or '无'}，动作项 {minutes.actions} 条"
        )
        if not minutes.entities and not minutes.metrics:
            minutes.parse_ok = False
            minutes.note = (minutes.note + "；" if minutes.note else "") + "未抽出任何实体或指标"
            log.append("纪要未抽出实体或指标，判定解析失败")

    _emit(1, "done")
    _emit(2, "active", "读取写作要求")
    # 提示词
    prompt_span: tuple[date, date] | None = None
    if prompt_text:
        prompt_year = parse_year(prompt_text) or date.today().year
        prompt_span = (parse_period(prompt_text, fallback_year=prompt_year)
                       or parse_week(prompt_text, fallback_year=prompt_year))
    prompt_info = PromptInfo(present=bool(prompt_text))
    if prompt_text:
        lines = [line.strip() for line in prompt_text.splitlines() if line.strip()]
        prompt_info.goal = (lines[0] if lines else prompt_text)[:200]
        prompt_info.extras = lines[1:11]
        if prompt_span is not None:
            prompt_info.scope = f"{prompt_span[0].isoformat()} 至 {prompt_span[1].isoformat()}"
        else:
            match = _WEEK_RE.search(prompt_text)
            if match:
                prompt_info.scope = match.group(0).strip()

    # 纪要没写周期时，退一步用提示词里的周期，但要在 note 里说清楚来源
    if minutes_span is None and prompt_span is not None and counts.minutes == 1:
        start, end = prompt_span
        minutes.period_start = start.isoformat()
        minutes.period_end = end.isoformat()
        minutes.compare_start = shift_days(start, -7).isoformat()
        minutes.compare_end = shift_days(end, -7).isoformat()
        minutes.week = week_label(start)
        minutes.parse_ok = counts.minutes == 1
        minutes.note = "纪要未写统计周期，周期按提示词推定，未在纪要中确认"
        log.append(f"纪要未写周期，按提示词取 {start.isoformat()} 至 {end.isoformat()}")

    _emit(2, "done")
    _emit(3, "active", "建立运行目录并复制输入")
    # 运行目录
    run = RunInfo()
    run_dir: Path | None = None
    if minutes.period_start:
        tag = iso_week_tag(date.fromisoformat(minutes.period_start))
        run_dir = _choose_run_dir(run_root, tag)
        run.period_tag = tag
        run.dir = _relative_dir(run_dir)
        log.append(f"周期标识 {tag}，运行目录 {run.dir}")

    if run_dir is not None:
        mapping = _copy_inputs(paths, run_dir, log)
        for path, info in records:
            info.path = mapping.get(_path_key(path), "")

    _emit(3, "done")
    _emit(4, "active", "匹配指标与数据列")
    # 合并覆盖
    starts = [span[0] for _p, info in data_recs if (span := _split_range(info.date_range))]
    ends = [span[1] for _p, info in data_recs if (span := _split_range(info.date_range))]
    merged = (min(starts), max(ends)) if starts and ends else None
    log.append(f"数据合并覆盖：{merged[0]} 至 {merged[1]}" if merged else "数据文件未取到日期范围")

    compare = (minutes.compare_start, minutes.compare_end)
    columns_by_file = {info.name: list(info.columns) for _p, info in data_recs if info.columns}
    domain = None
    if _ai_enabled(use_ai) and run_dir is not None and minutes_text and columns_by_file:
        try:
            from .domain_map import load_or_build

            domain = load_or_build(run_dir, minutes_text, columns_by_file)
            log.append(f"AI 映射：{len(domain.metrics)} 个指标、{len(domain.entities)} 类实体"
                       f"（模型 {domain.model}，{domain.note or '新建'}）")
            for item in domain.dropped[:5]:
                log.append("映射丢弃：" + item)
        except Exception as exc:  # noqa: BLE001 - 映射失败不能拖垮整步
            log.append(f"AI 映射不可用，退回规则匹配：{exc}")
            domain = None

    if domain is not None and domain.metrics:
        requirements, missing, degrade = _requirements_from_map(domain, data_recs)
    else:
        requirements, missing, degrade = _build_requirements(
            data_recs, minutes.metrics, merged, compare, _core_metrics(minutes_text, minutes.metrics))
    log.append(
        "需求矩阵："
        + "，".join(f"{r.item}={r.status}" for r in requirements)
    )

    unknown_codes = [code for code in codes if code not in vocab] if vocab else []
    _emit(4, "done")
    _emit(5, "active", "核对跨文件一致性与覆盖")
    consistency, coverage = _build_consistency(
        data_recs,
        same_hash,
        prompt_span,
        minutes_span,
        merged,
        (prompt_span[0].isocalendar()[1] if prompt_span else _week_number(prompt_text)),
        int(minutes.week.split("-W")[1]) if minutes.week else 0,
        minutes.week,
        unknown_codes,
    )
    prompt_label_week = _week_number(prompt_text)
    if (prompt_span and prompt_label_week
            and prompt_label_week != prompt_span[0].isocalendar()[1]):
        consistency.conflicts.append(Conflict(
            kind="period",
            detail=(f"提示词自身不一致：写第 {prompt_label_week} 周，但给出的日期 "
                    f"{prompt_span[0].isoformat()} 至 {prompt_span[1].isoformat()} 属于第 "
                    f"{prompt_span[0].isocalendar()[1]} 周")))
    if domain is not None and minutes_text:
        known_values = set(vocab) if "vocab" in locals() else set()
        for entity in domain.entities:
            unknown = [value for value in entity.values
                       if value not in known_values
                       and not re.fullmatch(r"d{4}s*[-/年]s*d{1,2}s*[-/月]s*d{1,2}s*日?", value)]
            if unknown:
                consistency.conflicts.append(Conflict(
                    kind="entity",
                    detail=(f"纪要提到的{entity.type}「" + "、".join(unknown[:5]) +
                            f"」在数据列「{entity.column}」里不存在")))
        log.append(f"实体核对：{sum(len(e.values) for e in domain.entities)} 个取值")

    log.append(
        f"一致性：列差异 {len(consistency.column_mismatch)} 项，日期重叠 {len(consistency.date_overlap)} 处，"
        f"疑似重复 {len(consistency.cross_file_duplicates)} 处，冲突 {len(consistency.conflicts)} 项"
    )

    _emit(5, "done")
    _emit(6, "active", "按阈值判定准入状态")
    # 判定
    questions: list[str] = []
    pause_reasons: list[str] = []

    unreadable = [info for _p, info in records if not info.readable]
    if unreadable:
        pause_reasons.append(
            "文件不可读：" + "；".join(f"「{info.name}」（{info.error}）" for info in unreadable)
        )
        for info in unreadable:
            questions.append(f"文件「{info.name}」不可读（{info.error}），请重新导出后再提交。")

    if counts.data == 0:
        pause_reasons.append("数据文件为 0 个")
        questions.append("数据文件为 0 个，请至少提供 1 个销售数据文件（csv/xlsx/parquet 等）。")
    if counts.minutes == 0:
        pause_reasons.append("纪要文件为 0 个")
        questions.append("纪要文件为 0 个，请提供 1 份本周纪要（md/txt/docx）。")
    elif counts.minutes > 1:
        names = "、".join(info.name for _p, info in records if info.role == "纪要")
        pause_reasons.append(f"纪要文件 {counts.minutes} 个")
        questions.append(f"纪要共 {counts.minutes} 份（{names}），请只保留 1 份；系统不自行挑选、不合并。")
    if counts.prompt == 0:
        pause_reasons.append("未收到提示词")
        questions.append("未收到输入提示词，请补充本周周报的写作要求。")
    if counts.minutes == 1 and not minutes.parse_ok:
        pause_reasons.append(f"纪要解析失败（{minutes.note}）")
        questions.append("纪要未给出可解析的周期（如 2026-10-26 至 2026-11-01），请补充明确的统计周期。")

    core_missing = [r for r in requirements if r.kind == "核心" and r.status == "missing"]
    blocked_items: list[str] = []
    if core_missing:
        blocked_items.append("核心指标缺 " + "、".join(r.item for r in core_missing))
        for item in core_missing:
            questions.append(f"核心指标「{item.item}」缺数据：{item.evidence}，请补充所需列后重跑 S1。")
    if consistency.conflicts:
        blocked_items.append(f"一致性冲突 {len(consistency.conflicts)} 项")
        for conflict in consistency.conflicts:
            questions.append(f"一致性冲突（{conflict.kind}）：{conflict.detail}；请确认后重跑 S1。")
    if consistency.date_overlap:
        blocked_items.append(f"日期区间重叠 {len(consistency.date_overlap)} 处")
        for overlap in consistency.date_overlap:
            questions.append(
                f"{'、'.join(overlap.files)} 的日期区间 {overlap.start} 至 {overlap.end} 重叠，请确认取舍。"
            )
    if any(item.kind == "overlap_content" for item in consistency.cross_file_duplicates):
        blocked_items.append("疑似重复或版本冲突")
        for duplicate in consistency.cross_file_duplicates:
            if duplicate.kind == "overlap_content":
                questions.append(f"{'、'.join(duplicate.files)}：{duplicate.note}，请确认保留哪份。")
    if not coverage.covers_prompt_scope and merged is not None:
        blocked_items.append("数据覆盖未包含提示周期")
        questions.append(
            f"数据合并覆盖 {coverage.merged_start} 至 {coverage.merged_end}，"
            f"未覆盖提示词或纪要的周期，请补充对应周期数据。"
        )

    if pause_reasons:
        state = "暂停"
    elif blocked_items:
        state = "待补充"
    else:
        state = "准入通过"

    if state == "准入通过" and questions:
        state = "待补充"
    if state == "待补充" and not questions:
        questions.append("存在待人工确认的事项，请核对 intake.json 后重跑 S1。")

    if state == "准入通过":
        extra = f"；含 {len(degrade)} 项降级口径" if degrade else ""
        message = (
            f"材料齐备：{counts.data} 个数据文件 + 1 份纪要，周期 {minutes.week}"
            f"（{minutes.period_start} 至 {minutes.period_end}）{extra}，可进入 S2。"
        )
        next_action = "continue"
    elif state == "待补充":
        message = f"{'、'.join(blocked_items)}，已写入 questions，补齐后重跑 S1。"
        next_action = "wait"
    else:
        message = f"暂停：{pause_reasons[0]}；处理后重跑 S1。"
        next_action = "wait"

    log.append(f"判定 {state}，next_action={next_action}")

    result = IntakeResult(
        state=state,
        run=run,
        files=[info for _p, info in records],
        counts=counts,
        prompt=prompt_info,
        minutes=minutes,
        requirements=requirements,
        consistency=consistency,
        coverage=coverage,
        missing=missing,
        degrade=degrade,
        questions=questions,
        message=message,
        next_action=next_action,
    )

    _emit(6, "done")
    _emit(7, "active", "写入 intake.json 与 status.json")
    now = _now()
    note = message if len(message) <= 120 else message[:117] + "…"

    if run_dir is not None:
        status_file = run_dir / "status.json"
        history = _load_history(status_file)
        status = Status(
            stage="intake",
            state=state,
            updated_by="S1",
            at=now,
            note=note,
            history=history + [StatusHistory(at=now, state=state, updated_by="S1", note=note)],
        )
        try:
            _write_json(run_dir / "intake.json", result.model_dump())
            log.append(f"已落盘 {run.dir}/intake.json")
            _write_json(status_file, status.model_dump())
            log.append(f"已落盘 {run.dir}/status.json")
        except OSError as exc:
            log.append(f"落盘失败：{exc}")
            result.state = "暂停"
            result.next_action = "wait"
            result.questions = result.questions + [f"运行目录写入失败（{exc}），请检查目录权限后重跑。"]
            result.message = f"暂停：运行目录写入失败（{exc}）。"
            status.state = "暂停"
            status.note = result.message
            status.history.append(StatusHistory(at=_now(), state="暂停", updated_by="S1", note=result.message))
    else:
        status = Status(
            stage="intake",
            state=state,
            updated_by="S1",
            at=now,
            note=note,
            history=[StatusHistory(at=now, state=state, updated_by="S1", note=note)],
        )
        log.append("周期未定，未建运行目录，仅返回 JSON")

    _emit(7, "done")
    return result, status, log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S1 准入检查：清点输入、读纪要、匹配数据、落盘 intake.json")
    parser.add_argument("--files", nargs="+", default=[], help="输入文件，空格分隔")
    parser.add_argument("--prompt", default="", help="输入提示词")
    parser.add_argument("--run-root", default="runs", help="运行目录根，默认 runs")
    parser.add_argument("--spec", default=None, help="写作规范文件，可选，只作规则引用")
    parser.add_argument("--json", action="store_true", help="只打印 JSON，不打印执行日志")
    args = parser.parse_args(argv)

    result, _status, log = run_intake(
        [Path(item) for item in args.files],
        args.prompt,
        Path(args.run_root),
        Path(args.spec) if args.spec else None,
    )
    if not args.json:
        for line in log:
            print(line, file=sys.stderr)
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
