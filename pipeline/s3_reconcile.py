"""S3 数据对账：拿 S2 的卡片去数据里取数，判对错，落 issues.json 与 ledger.json。

边界：本步不做业务判断，不改纪要，不写正文。
所有数字由内置规则算出（同输入同结果），不调用模型；因此台账的 code_hash 记「内置规则」，
不伪造代码指纹。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd

from .models import IssueItem, IssuesResult, Ledger, LedgerEntry, ReconcileStats, ResultItem

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUN_ROOT = PROJECT_ROOT / "runs"

# 门店名称前缀 → 区域。数据里没有区域列时用它推导。
CITY_TO_REGION = {
    "上海": "华东", "南京": "华东", "杭州": "华东", "苏州": "华东", "无锡": "华东",
    "北京": "华北", "天津": "华北", "石家庄": "华北", "济南": "华北",
    "广州": "华南", "深圳": "华南", "东莞": "华南", "佛山": "华南",
    "成都": "西南", "重庆": "西南", "昆明": "西南", "贵阳": "西南",
    "武汉": "华中", "长沙": "华中", "郑州": "华中",
    "西安": "西北", "兰州": "西北",
    "沈阳": "东北", "大连": "东北", "哈尔滨": "东北",
}

REGION_ABBR = {"华东": "hd", "华南": "hn", "华北": "hb", "西南": "xn",
               "西北": "xb", "东北": "db", "华中": "hz"}
METRIC_ABBR = {"实收": "rev", "数量": "qty", "毛利率": "margin",
               "退货": "refund", "渠道": "channel", "目标达成率": "target"}
GRAN_ABBR = {"区域": "region", "门店": "store", "品类": "category",
             "商品": "product", "全司": "company"}

# 对账容差：比例与环比 5 个百分点，毛利率 1 个百分点，金额 50 元与 5% 取大
TOL_RATIO = 0.05
TOL_MARGIN = 0.01
TOL_AMOUNT_ABS = 50.0
TOL_AMOUNT_PCT = 0.05


def _norm(value: object) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or ""))


def _slug(text: str) -> str:
    if text in REGION_ABBR:
        return REGION_ABBR[text]
    ascii_part = re.sub(r"[^a-z0-9]", "", str(text).lower())
    if ascii_part:
        return ascii_part[:12]
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()[:6]


def _qhash(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _display(value: float, unit: str) -> str:
    if unit == "元":
        return format(round(value), ",d")
    if unit == "%":
        return format(value * 100, ".1f") + "%"
    if unit == "件":
        return format(round(value), ",d")
    if unit == "笔":
        return format(int(value), "d")
    return str(round(value, 4))


def _parse_target(text: str | None) -> float | None:
    """把纪要原句里的目标值规约成浮点。规约不了返回 None，不猜。"""
    if not text:
        return None
    s = _norm(text)
    match = re.search(r"(-?\d+(?:\.\d+)?)万元", s)
    if match:
        return float(match.group(1)) * 10000
    match = re.search(r"(-?\d+(?:\.\d+)?)亿元", s)
    if match:
        return float(match.group(1)) * 100000000
    match = re.search(r"(-?\d+(?:\.\d+)?)%", s)
    if match:
        return float(match.group(1)) / 100
    match = re.search(r"(-?\d+(?:\.\d+)?)元", s)
    if match:
        return float(match.group(1))
    match = re.fullmatch(r"-?\d+(?:\.\d+)?", s)
    if match:
        return float(s)
    return None


# ---------------------------------------------------------------- 读数据

def _read_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        last: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
            try:
                return pd.read_csv(path, encoding=encoding)
            except UnicodeDecodeError as exc:
                last = exc
                continue
        raise ValueError("无法解码 " + path.name + "：" + str(last))
    return pd.read_excel(path)


def _load_data(run_dir: Path, intake: dict) -> tuple[pd.DataFrame, str]:
    frames: list[pd.DataFrame] = []
    names: list[str] = []
    for info in intake.get("files") or []:
        if info.get("role") != "数据":
            continue
        candidates = [
            run_dir / str(info.get("path") or ""),
            run_dir / "inputs" / str(info.get("name") or ""),
        ]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            continue
        frames.append(_read_table(path))
        names.append(path.name)
    if not frames:
        raise FileNotFoundError("运行目录里没有可读的数据文件")
    frame = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    return frame, "、".join(names)


def _date_column(frame: pd.DataFrame) -> str | None:
    for column in frame.columns:
        if re.search(r"日期|时间|date", str(column), re.I):
            return str(column)
    return None


def _locate_entity(frame: pd.DataFrame, entity: str,
                   entity_type: str) -> tuple[pd.Series, str] | None:
    """在数据里找这个实体出现在哪一列，返回布尔掩码和列说明。"""
    target = _norm(entity)
    if not target:
        return None
    for column in frame.columns:
        series = frame[column]
        if not (pd.api.types.is_object_dtype(series) or isinstance(series.dtype, pd.CategoricalDtype)):
            continue
        texts = series.map(_norm)
        exact = texts == target
        if bool(exact.any()):
            return exact, str(column)
    for column in frame.columns:
        series = frame[column]
        if not (pd.api.types.is_object_dtype(series) or isinstance(series.dtype, pd.CategoricalDtype)):
            continue
        texts = series.map(_norm)
        contains = texts.str.contains(re.escape(target), na=False, regex=True)
        if bool(contains.any()):
            return contains, str(column)
    # 区域：数据里没有区域列时，从门店名称推导
    if target in CITY_TO_REGION.values():
        store_col = next((str(c) for c in frame.columns
                          if "门店" in str(c) and "名称" in str(c)), None)
        if store_col:
            regions = frame[store_col].map(
                lambda name: next((region for city, region in CITY_TO_REGION.items()
                                   if _norm(name).startswith(city)), ""))
            mask = regions == target
            if bool(mask.any()):
                return mask, store_col + "（按城市推导）"
    return None


# ---------------------------------------------------------------- 取数

def _measure(frame: pd.DataFrame, metric: str) -> tuple[float, str] | None:
    """按指标聚合，返回 (值, 单位)。取不到的返回 None。"""
    columns = {_norm(c): str(c) for c in frame.columns}

    def pick(*hints: str) -> str | None:
        for hint in hints:
            for key, column in columns.items():
                if hint in key:
                    return column
        return None

    if metric == "实收":
        column = pick("实收", "销售额", "营收")
        return (float(frame[column].sum()), "元") if column else None
    if metric == "数量":
        column = pick("销售数量", "数量", "销量")
        return (float(frame[column].sum()), "件") if column else None
    if metric == "毛利率":
        rev = pick("实收", "销售额")
        cost = pick("成本")
        if not rev or not cost:
            return None
        total_rev = float(frame[rev].sum())
        if total_rev == 0:
            return None
        return ((total_rev - float(frame[cost].sum())) / total_rev, "%")
    if metric == "退货":
        rev = pick("实收", "销售额")
        if not rev:
            return None
        return (float((frame[rev] < 0).sum()), "笔")
    return None


def _slice(frame: pd.DataFrame, date_col: str, start: str, end: str) -> pd.DataFrame:
    dates = pd.to_datetime(frame[date_col], errors="coerce")
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    return frame[mask]


# ---------------------------------------------------------------- 判定

def _judge(expect: str, current: float, base: float | None,
           target: float | None, unit: str,
           target_text: str = "") -> tuple[str, float | None, float | None, str]:
    """返回 (verdict, diff, change, 说明)。"""
    if expect in ("上升", "下降", "持平"):
        if base is None or base == 0:
            return "unsupported", None, None, "对比期没有可比数值"
        change = (current - base) / abs(base)
        if expect == "上升":
            ok = change >= TOL_RATIO
        elif expect == "下降":
            ok = change <= -TOL_RATIO
        else:
            ok = abs(change) <= TOL_RATIO
        return ("match" if ok else "mismatch"), change, change, ""

    if expect in ("具体数值", "百分比"):
        if target is None:
            return "unsupported", None, None, "目标值「" + str(target_text) + "」折算不出数字"
        diff = current - target
        if unit == "元":
            tolerance = max(TOL_AMOUNT_ABS, abs(target) * TOL_AMOUNT_PCT)
        elif unit == "%":
            tolerance = TOL_MARGIN
        else:
            tolerance = 0.0
        ok = abs(diff) <= tolerance
        return ("match" if ok else "mismatch"), diff, None, ""

    return "unsupported", None, None, "期望说不清，不做判定"


# ---------------------------------------------------------------- 主流程

def run_s3(run_dir: str | Path, run_root: str | Path | None = None,
           engine: str = "pandasai") -> tuple[IssuesResult, list[str]]:
    run_dir = Path(run_dir)
    if not run_dir.is_dir() and run_root:
        run_dir = Path(run_root) / run_dir
    log: list[str] = []

    cards_path = run_dir / "cards.json"
    intake_path = run_dir / "intake.json"
    if not cards_path.is_file():
        raise FileNotFoundError("找不到 cards.json，先跑 S2：" + str(cards_path))
    if not intake_path.is_file():
        raise FileNotFoundError("找不到 intake.json，先跑 S1：" + str(intake_path))

    cards_data = json.loads(cards_path.read_text(encoding="utf-8"))
    intake = json.loads(intake_path.read_text(encoding="utf-8"))

    result = IssuesResult()
    result.run_id = str((intake.get("run") or {}).get("period_tag") or run_dir.name)
    result.week = str((intake.get("minutes") or {}).get("week") or "")
    ledger = Ledger()

    cards = [card for card in (cards_data.get("cards") or [])
             if card.get("checkable") == "能"]
    log.append("可对数据卡片 " + str(len(cards)) + " 张")

    if not cards:
        result.state = "empty"
        result.message = "没有可对数据的卡片，S3 无内容可核。"
        _write(run_dir, result, ledger)
        return result, log

    frame, source = _load_data(run_dir, intake)
    date_col = _date_column(frame)
    if date_col is None:
        raise ValueError("数据里找不到日期列，无法按周期取数")
    if "记录ID" in frame.columns:
        before = len(frame)
        frame = frame.drop_duplicates(subset=["记录ID"], keep="first")
        if before != len(frame):
            log.append("按记录ID去重：" + str(before - len(frame)) + " 行重复已剔除")
    log.append("数据 " + source + "，" + str(len(frame)) + " 行")

    qa = None
    if engine == "pandasai":
        from .pandasai_glm import make_qa

        qa_frame = frame.copy()
        store_col = next((str(c) for c in qa_frame.columns
                          if "门店" in str(c) and "名称" in str(c)), None)
        if store_col and "_区域" not in qa_frame.columns:
            qa_frame["_区域"] = qa_frame[store_col].map(
                lambda name: next((region for city, region in CITY_TO_REGION.items()
                                   if _norm(name).startswith(city)), ""))
        qa = make_qa({"df_sales": qa_frame}, cache_dir=run_dir / "cache")
        from .llm import get_config

        log.append("取数引擎 pandasai（模型 " + get_config().model + "）")

    issues: list[IssueItem] = []
    results: list[ResultItem] = []
    stats = ReconcileStats()

    for card in cards:
        card_id = str(card.get("id") or "")
        entity = str(card.get("entity") or "")
        entity_type = str(card.get("entity_type") or "")
        metric = str(card.get("metric") or "")
        expect = str(card.get("expect") or "")
        period = str(card.get("period") or "")
        compare = str(card.get("compare_period") or "")
        quote = str(card.get("quote") or "")

        stats.calls += 1

        located = _locate_entity(frame, entity, entity_type)
        if located is None:
            stats.info += 1
            issues.append(IssueItem(
                id="Q" + str(len(issues) + 1).zfill(2), level="信息", type="entity",
                card=card_id, subject=entity, minutes_said=quote,
                raw="", question_hash=_qhash(card_id, entity),
                suggest="数据里找不到这个实体，正文不写该结论"))
            continue

        mask, column = located
        unit = {"实收": "元", "数量": "件", "毛利率": "%",
                "退货": "笔", "渠道": "%"}.get(metric, str(card.get("unit") or "元"))
        if expect in ("上升", "下降", "持平"):
            unit = "%"   # 这三类卡片比的是变化率，不是绝对金额

        qhash = ""
        code_hash = "内置规则"
        change = None
        diff = None
        ask_flags: list[str] = []

        if qa is not None:
            qa_column = "_区域" if "推导" in column else column
            try:
                out = _ask_card(qa, card, qa_column, entity, period, compare)
            except Exception as exc:  # noqa: BLE001 - 单卡失败不影响其他卡
                out = {"ok": False, "value": None, "verdict": "unsupported",
                       "flags": ["call_failed:" + str(exc)[:80]], "raw": "",
                       "code": "", "question_hash": _qhash(card_id, "call_failed")}
            # s3.txt：同一问题最多问两次，第二次只在解析失败时进行
            if out.get("status") == "parse_failed":
                flags_now = [str(flag) for flag in (out.get("flags") or [])]
                hint = ""
                if "unit_pct_suspect" in flags_now:
                    hint = "百分比请带 % 输出。"
                elif "tolerance_unparsed" in flags_now:
                    hint = "只输出一个数字，不要加单位和百分号。"
                try:
                    retry = _ask_card(qa, card, qa_column, entity, period, compare, hint)
                except Exception:
                    retry = None
                stats.calls += 1
                if retry is not None and retry.get("status") == "ok":
                    out = retry
                    log.append("重问成功：" + card_id)

            ask_flags = [str(flag) for flag in (out.get("flags") or [])]
            current = out.get("value")
            verdict = str(out.get("verdict") or "none")
            diff = out.get("diff")
            qhash = str(out.get("question_hash") or "")
            code = str(out.get("code") or "")
            code_hash = (hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
                         if code else "无代码")
            if out.get("status") == "parse_failed":
                ask_flags = ask_flags + ["重问后仍解析失败"]
            if out.get("fallback") or verdict == "none":
                verdict = "unsupported"
            if any(flag.startswith("reference_unparsed") for flag in ask_flags):
                verdict = "unsupported"
            if current is None:
                verdict = "unsupported"
            note = _flag_text(ask_flags)[:160]
            stats.ok += 1 if out.get("status") != "parse_failed" else 0
            if out.get("status") == "parse_failed":
                stats.parse_failed += 1
        else:
            entity_frame = frame[mask]
            if "至" in period:
                p_start, p_end = [part.strip() for part in period.split("至", 1)]
                try:
                    entity_frame = _slice(entity_frame, date_col, p_start, p_end)
                except Exception:
                    pass
            measure = _measure(entity_frame, metric)
            if measure is None:
                stats.info += 1
                issues.append(IssueItem(
                    id="Q" + str(len(issues) + 1).zfill(2), level="信息", type="caliber",
                    card=card_id, subject=entity + " " + metric, minutes_said=quote,
                    raw="", question_hash=_qhash(card_id, entity, metric),
                    suggest="数据里没有能算「" + metric + "」的列，正文写无数据来源"))
                continue
            current, unit = measure
            base = None
            if compare and "至" in compare:
                start, end = [part.strip() for part in compare.split("至", 1)]
                try:
                    base_frame = _slice(frame[mask], date_col, start, end)
                    base_measure = _measure(base_frame, metric)
                    base = base_measure[0] if base_measure else None
                except Exception:
                    base = None
            target = _parse_target(card.get("target_value"))
            verdict, diff, change, note = _judge(
                expect, current, base, target, unit,
                str(card.get("target_value") or ""))
            qhash = _qhash(card_id, entity, metric)
            stats.ok += 1

        if current is None:
            stats.info += 1
            issues.append(IssueItem(
                id="Q" + str(len(issues) + 1).zfill(2), level="信息", type="caliber",
                card=card_id, subject=entity + " " + metric, minutes_said=quote,
                raw="", question_hash=qhash,
                suggest=("无法比对：" + note + "；正文只写纪要原话，不给具体数"
                         if note else "口径不可得，正文不引用该结论")))
            continue

        key = "_".join([
            GRAN_ABBR.get(entity_type, "entity"),
            _slug(entity),
            METRIC_ABBR.get(metric, "other"),
            result.run_id or "w00",
        ])
        if not qhash:
            qhash = _qhash(card_id, entity, metric, period, compare)

        ledger.entries.append(LedgerEntry(
            key=key, value=float(current), unit=unit,
            display=_display(current, unit),
            spec=("数据来自" + source + "；按记录ID保留第一条；"
                  "时间范围 " + period.replace(" 至 ", " 至 ") + "；"
                  "筛选 " + column + "=" + entity + "；"
                  "口径为" + metric + "求和"),
            question_hash=qhash, code_hash=code_hash,
            status="ok", verdict=verdict, diff=diff, shape="number"))

        explain = ("数据来自" + source + "；按记录ID保留第一条；时间范围 "
                   + period + "；筛选 " + column + " 为 " + entity
                   + "；口径为" + metric + "求和；结果 " + _display(current, unit)
                   + ("" if unit == "%" else " " + unit) + "。")
        raw_text = (_display(change, "%") if change is not None
                    else _display(current, unit))
        results.append(ResultItem(card=card_id, subject=entity + " " + metric,
                                  raw=raw_text, explain=explain,
                                  value=float(current), question_hash=qhash))
        stats.checked += 1

        if verdict == "mismatch":
            direction = "方向相反" if (expect == "下降" and (change or 0) > 0) or \
                                      (expect == "上升" and (change or 0) < 0) else "数字对不上"
            level = "阻断"
            stats.blocking += 1
            issues.append(IssueItem(
                id="Q" + str(len(issues) + 1).zfill(2), level=level,
                type="direction" if direction == "方向相反" else "number",
                card=card_id, subject=entity + " " + metric, minutes_said=quote,
                raw=(_display(change, "%") if change is not None
                     else _display(current, unit)),
                value=float(current),
                diff=diff if expect in ("具体数值", "百分比") else None,
                question_hash=qhash,
                suggest=("纪要写「" + expect + "」，数据是 "
                         + (_display(change, "%") if change is not None else _display(current, unit))
                         + "；请核对后修改纪要或数据")))
        elif verdict == "unsupported":
            stats.info += 1
            issues.append(IssueItem(
                id="Q" + str(len(issues) + 1).zfill(2), level="信息", type="caliber",
                card=card_id, subject=entity + " " + metric, minutes_said=quote,
                raw=_display(current, unit), value=float(current),
                question_hash=qhash,
                suggest=((note + "；正文只写纪要原话，不给具体数")
                         if note else "口径不足，正文不引用该结论")))
            stats.checked -= 1

    result.coverage = _coverage(intake, ledger)
    uncovered = [c for c in result.coverage if c["status"] == "uncovered"]
    for item in uncovered:
        stats.info += 1
        issues.append(IssueItem(
            id="Q" + str(len(issues) + 1).zfill(2), level="信息", type="missing",
            card="", subject=item["item"],
            minutes_said="纪要提到「" + item["item"] + "」，数据里没有对应字段",
            raw="", question_hash=_qhash("coverage", item["item"]),
            suggest=("数据里没有对应列，正文写「无数据来源」"
                     if "没有对应列" in str(item.get("note") or "")
                     else "正文写「无数据来源」：" + str(item.get("note") or "该指标没有被任何卡片覆盖"))

        ))

    mentioned = _mentioned_entities(cards, intake)
    for anomaly in _scan_anomalies(frame, date_col, cards[0].get("period", ""),
                                   cards[0].get("compare_period", ""), mentioned, source)[:8]:
        stats.warn += 1
        issues.append(IssueItem(
            id="Q" + str(len(issues) + 1).zfill(2), level="提示", type="number",
            card="", subject=anomaly["subject"] + "（" + anomaly["scope"] + "）",
            minutes_said="纪要未提及",
            raw=format(anomaly["change"] * 100, "+.1f") + "%",
            value=anomaly["current"], diff=None,
            question_hash=_qhash("anomaly", anomaly["scope"], anomaly["subject"]),
            suggest=anomaly["note"] + "；报告里需要补上或说明原因"))

    result.issues = issues
    result.results = results
    result.stats = stats
    result.state = "ok"
    result.message = ("已核对 " + str(stats.checked) + " 条；阻断 "
                      + str(stats.blocking) + " 条、提示 " + str(stats.warn)
                      + " 条；另有 " + str(stats.info) + " 条数据说明。")
    log.append(result.message)

    _write(run_dir, result, ledger)
    log.append("已写入 issues.json 与 ledger.json")
    return result, log


def _coverage(intake: dict, ledger: Ledger) -> list[dict]:
    """S1 抽出的每条需求都要有结论：covered 或 uncovered。"""
    keys = [entry.key for entry in ledger.entries]

    def needle_for(item: str) -> str:
        if any(word in item for word in ("实收", "销售", "营收", "收入", "日均",
                                         "环比", "较上周", "增长", "下滑")):
            return "_rev_"
        if any(word in item for word in ("数量", "销量", "件数")):
            return "_qty_"
        if "毛利" in item:
            return "_margin_"
        if "退货" in item or "退款" in item:
            return "_refund_"
        if any(word in item for word in ("目标", "达成")):
            return "_target_"
        return ""

    out: list[dict] = []
    for req in intake.get("requirements") or []:
        item = str(req.get("item") or "")
        needle = needle_for(item)
        hit = next((key for key in keys if needle and needle in key), "")
        if hit:
            out.append({"item": item, "status": "covered", "key": hit,
                        "note": str(req.get("note") or "")})
        else:
            out.append({"item": item, "status": "uncovered", "key": "",
                        "note": str(req.get("note") or req.get("impact") or "数据里没有对应列")})
    return out


def _scan_anomalies(frame: pd.DataFrame, date_col: str, period: str, compare: str,
                    mentioned: set[str], source: str) -> list[dict]:
    """按区域和门店算环比，找出超过 10% 但纪要没提的对象。"""
    if "至" not in period or "至" not in compare:
        return []
    p_start, p_end = [part.strip() for part in period.split("至", 1)]
    c_start, c_end = [part.strip() for part in compare.split("至", 1)]

    amount_col = next((str(c) for c in frame.columns
                       if "实收" in str(c) or "销售额" in str(c)), None)
    if amount_col is None:
        return []
    dates = pd.to_datetime(frame[date_col], errors="coerce")
    current = frame[(dates >= pd.Timestamp(p_start)) & (dates <= pd.Timestamp(p_end))]
    base = frame[(dates >= pd.Timestamp(c_start)) & (dates <= pd.Timestamp(c_end))]
    if current.empty or base.empty:
        return []

    found: list[dict] = []
    store_col = next((str(c) for c in frame.columns
                      if "门店" in str(c) and "名称" in str(c)), None)

    def add(label: str, scope: str, cur: float, prev: float) -> None:
        if prev == 0:
            return
        change = (cur - prev) / abs(prev)
        if abs(change) <= 0.10:
            return
        if _norm(label) in mentioned:
            return
        found.append({
            "scope": scope, "subject": label, "change": change,
            "current": cur, "previous": prev,
            "display": format(cur, ",.0f"),
            "note": "本周 " + format(cur, ",.0f") + " 元，环比 "
                    + format(change * 100, "+.1f") + "%，纪要未提",
        })

    if store_col:
        regions = frame[store_col].map(
            lambda name: next((region for city, region in CITY_TO_REGION.items()
                               if _norm(name).startswith(city)), ""))
        current_region = current.copy()
        base_region = base.copy()
        current_region["_region"] = regions.loc[current.index]
        base_region["_region"] = regions.loc[base.index]
        for region, group in current_region.groupby("_region"):
            if not region:
                continue
            prev_group = base_region[base_region["_region"] == region]
            if prev_group.empty:
                continue
            add(region, "区域", float(group[amount_col].sum()),
                float(prev_group[amount_col].sum()))
        for store, group in current.groupby(store_col):
            prev_group = base[base[store_col] == store]
            if prev_group.empty:
                continue
            add(str(store), "门店", float(group[amount_col].sum()),
                float(prev_group[amount_col].sum()))
    found.sort(key=lambda item: -abs(item["change"]))
    return found


def _mentioned_entities(cards: list[dict], intake: dict) -> set[str]:
    names: set[str] = set()
    for card in cards:
        entity = str(card.get("entity") or "")
        if entity:
            names.add(_norm(entity))
            names.add(entity)
    for entity in (intake.get("minutes") or {}).get("entities") or []:
        names.add(_norm(entity))
        names.add(str(entity))
    return names


# ---------------------------------------------------------------- pandasai 取数

def _metric_phrase(metric: str) -> str:
    if metric == "实收":
        return "实收金额合计"
    if metric == "数量":
        return "销售数量合计"
    if metric == "毛利率":
        return "毛利率（口径：(实收金额合计 - 成本金额合计) / 实收金额合计）"
    if metric == "退货":
        return "退货笔数（实收金额为负数的记录条数）"
    return metric + "合计"


def _build_question(column: str, entity: str, metric: str,
                    period: str, compare: str, is_change: bool) -> str:
    start, end = [part.strip() for part in period.split("至", 1)]
    target = _metric_phrase(metric)
    if not is_change:
        return (
            "用 df_sales，计算 " + start + " 至 " + end
            + "、筛选 " + column + " 为「" + entity + "」的" + target + "。"
            "退货按负数直接计入；按记录ID去重保留第一条；结果保留两位小数。"
            "只输出一个数字，不要单位、不要千分位、不要解释、不要图表。"
        )
    c_start, c_end = [part.strip() for part in compare.split("至", 1)]
    return (
        "用 df_sales，分别计算 " + c_start + " 至 " + c_end + "（上期）与 "
        + start + " 至 " + end + "（本期）、筛选 " + column + " 为「" + entity
        + "」的" + target + "，再算环比变化率 = (本期 - 上期) / 上期。"
        "退货按负数直接计入；按记录ID去重保留第一条；变化率保留四位小数。"
        "只输出一个数字（小数形式，例如 -0.1271），不要加百分号、不要说明、不要图表。"
    )


def _build_expected(expect: str, metric: str, unit: str,
                    target: float | None) -> dict | None:
    if expect in ("上升", "下降", "持平"):
        kind = {"上升": "up", "下降": "down", "持平": "flat"}[expect]
        return {"type": kind, "kind": "change", "tolerance": "ratio_pp:5"}
    if expect in ("具体数值", "百分比") and target is not None:
        tolerance = "margin_pp:1" if unit == "%" else "amount:50,0.05"
        return {"type": "value", "kind": "total", "value": target,
                "tolerance": tolerance}
    return None


def _ask_card(qa, card: dict, column: str, entity: str,
              period: str, compare: str, extra_hint: str = "") -> dict:
    """调 pandasai 取一张卡片的数。返回工具的原始结果。"""
    metric = str(card.get("metric") or "")
    expect = str(card.get("expect") or "")
    unit = str(card.get("unit") or "元")
    target = _parse_target(card.get("target_value"))
    is_change = expect in ("上升", "下降", "持平")

    question = _build_question(column, entity, metric, period, compare, is_change)
    expected = _build_expected(expect, metric, unit, target)
    reference: float | str | None = None
    if expect in ("具体数值", "百分比") and card.get("target_value"):
        reference = str(card.get("target_value"))

    kwargs: dict = {"expect_shape": "number", "use_cache": False}
    if expected is not None:
        kwargs["expected"] = expected
    if reference is not None:
        kwargs["reference"] = reference
    if extra_hint:
        question = question + " " + extra_hint
    out = qa.ask(question, **kwargs)
    out["question"] = question
    return out


FLAG_TEXT = {
    "reference_unparsed": "目标值无法折算成数字",
    "unit_pct_suspect": "答案缺百分号，已重问仍不合格",
    "tolerance_unparsed": "容差写法不认识",
    "tolerance_missing": "没给容差，判定按 0 处理",
    "code_unavailable": "拿不到执行代码",
    "empty_rows": "该对象本期无记录",
    "unit_wan_normalized": "万元已折成元",
    "unit_yi_normalized": "亿元已折成元",
    "unit_pct_normalized": "百分比已折成小数",
    "pct_tolerance_without_ref": "容差的百分比部分没有参考值可乘",
    "import:pandas": "",
}


def _flag_text(flags: list[str]) -> str:
    parts: list[str] = []
    for flag in flags:
        if not flag or flag.startswith("import:") or flag.startswith("call_failed"):
            if flag.startswith("call_failed"):
                parts.append("调用失败：" + flag.split(":", 1)[-1])
            continue
        text = FLAG_TEXT.get(flag, flag)
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts)


def _write(run_dir: Path, result: IssuesResult, ledger: Ledger) -> None:
    (run_dir / "issues.json").write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (run_dir / "ledger.json").write_text(
        json.dumps(ledger.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S3 数据对账")
    parser.add_argument("--run", required=True, help="运行目录，如 runs/w44")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--engine", choices=("pandasai", "builtin"), default="pandasai",
                        help="取数引擎：pandasai（默认）或内置规则")
    args = parser.parse_args(argv)

    try:
        result, log = run_s3(args.run, args.run_root, engine=args.engine)
    except FileNotFoundError as exc:
        print(json.dumps({"state": "blocked", "message": str(exc)}, ensure_ascii=False))
        return 1

    if args.json:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        for line in log:
            print("- " + line)
        print()
        for item in result.issues:
            print("[" + item.level + "] " + item.id + " " + item.subject + "：" + item.suggest)
        print()
        print(result.message)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
