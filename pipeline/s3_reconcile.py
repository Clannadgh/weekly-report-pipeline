"""S3 数据对账：拿 S2 的卡片去数据里取数，判对错，落 issues.json 与 ledger.json。

边界：本步不做业务判断，不改纪要，不写正文。
取数统一走 pandasai：模型只负责生成取数代码，代码执行后由本步按容差判定，
台账的 code_hash 记录这段代码；没有内置规则兜底。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd

from .approvals import clear as clear_approvals
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


def _s1_waivers(intake: dict) -> list[dict]:
    """S1 人工豁免：后续阶段沿用同一条裁决，不再把它当缺口。"""
    out: list[dict] = []
    for waiver in intake.get("waived") or []:
        if not isinstance(waiver, dict):
            continue
        reason = str(waiver.get("reason") or "").strip()
        by = str(waiver.get("by") or "").strip()
        at = str(waiver.get("at") or "").strip()
        for item in waiver.get("items") or []:
            text = str(item or "").strip()
            if not text or text == "人工放权":
                continue
            out.append({"item": text, "reason": reason, "by": by, "at": at,
                        "cards": []})
    return out


def _matched_waiver(entity: str, waivers: list[dict]) -> dict | None:
    parts = [part for part in re.split(r"[、,，;；和及与\s]+", str(entity or ""))
             if len(part) >= 2]
    if not parts:
        return None
    for waiver in waivers:
        item = _norm(waiver.get("item") or "")
        if any(_norm(part) in item for part in parts):
            return waiver
    return None


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


CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_number(text: str) -> float | None:
    text = _norm(text)
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if text in CN_DIGITS:
        return float(CN_DIGITS[text])
    if len(text) == 2 and text[0] == "十" and text[1] in CN_DIGITS:
        return float(10 + CN_DIGITS[text[1]])
    if len(text) == 2 and text[0] in CN_DIGITS and text[1] == "十":
        return float(CN_DIGITS[text[0]] * 10)
    return None


def _parse_target(text: str | None) -> float | None:
    """把纪要原句里的目标值规约成浮点。规约不了返回 None，不猜。"""
    if not text:
        return None
    s = _norm(text)
    match = re.search(r"([0-9一二三四五六七八九十]+(?:\.[0-9]+)?)成", s)
    if match:
        value = _cn_number(match.group(1))
        return value / 10 if value is not None else None
    match = re.search(r"([0-9一二三四五六七八九十]+(?:\.[0-9]+)?)倍", s)
    if match:
        return _cn_number(match.group(1))
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

# ---------------------------------------------------------------- 判定

def _ask_amount(qa, column: str, entity: str, metric: str, period: str,
                hint: str = "") -> float | None:
    """单独取本期金额，供台账建一条可被 S4 引用的金额键。"""
    question = _build_question(column, entity, metric, period, "", False, hint)
    try:
        out = qa.ask(question, expect_shape="number", use_cache=False)
    except Exception:  # noqa: BLE001
        return None
    value = out.get("value")
    return float(value) if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------- 主流程

def run_s3(run_dir: str | Path, run_root: str | Path | None = None,
           only: list[str] | None = None,
           overrides: dict | None = None) -> tuple[IssuesResult, list[str]]:
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

    clear_approvals(run_dir, ("s3",))
    try:
        (run_dir / "corrected.json").unlink()
        log.append("本次重跑 S3，已清理旧的 corrected.json（S4 需重跑）")
    except FileNotFoundError:
        pass

    cards_data = json.loads(cards_path.read_text(encoding="utf-8"))
    intake = json.loads(intake_path.read_text(encoding="utf-8"))

    result = IssuesResult()
    result.waived = _s1_waivers(intake)
    result.run_id = str((intake.get("run") or {}).get("period_tag") or run_dir.name)
    result.week = str((intake.get("minutes") or {}).get("week") or "")
    ledger = Ledger()

    cards = [card for card in (cards_data.get("cards") or [])
             if card.get("checkable") == "能"]
    log.append("可对数据卡片 " + str(len(cards)) + " 张")

    only_set = {str(item) for item in (only or []) if str(item)}
    if overrides is None:
        overrides = _load_overrides(run_dir)
    if only_set:
        cards_run = [card for card in cards if str(card.get("id")) in only_set]
        log.append("只重算 " + "、".join(sorted(only_set)))
    else:
        cards_run = cards

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

    try:
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
    except Exception as exc:  # noqa: BLE001
        result.state = "blocked"
        result.message = "S3 需要 pandasai/模型：" + str(exc)
        _write(run_dir, result, ledger)
        return result, log

    issues: list[IssueItem] = []
    results: list[ResultItem] = []
    stats = ReconcileStats()
    waived_cards = 0

    for card in cards_run:
        card_id = str(card.get("id") or "")
        caliber = overrides.get(card_id) or {}
        caliber_hint = str(caliber.get("hint") or "")
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
            waiver = _matched_waiver(entity, result.waived)
            if waiver is not None:
                cards = waiver.get("cards")
                if isinstance(cards, list):
                    cards.append(card_id)
                waived_cards += 1
                log.append("S1 人工豁免，跳过：" + card_id + " " + entity)
                continue
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
        code_hash = "无代码"
        change = None
        diff = None
        amount_value: float | None = None
        ask_flags: list[str] = []

        ratio_card = (_parse_ratio_quote(quote)
                      if expect in ("具体数值", "百分比") else None)
        is_change_card = expect in ("上升", "下降", "持平")
        if ratio_card is not None:
            unit = "倍"
        if qa is not None:
            qa_column = "_区域" if "推导" in column else column
            try:
                out = _ask_card(qa, card, qa_column, entity, period, compare,
                                caliber_hint=caliber_hint)
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
                    retry = _ask_card(qa, card, qa_column, entity, period, compare,
                                      hint, caliber_hint)
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
            if (is_change_card and current is not None
                    and out.get("status") == "ok"):
                amount_value = _ask_amount(qa, qa_column, entity, metric,
                                           period, caliber_hint)
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
        if current is None:
            stats.info += 1
            issues.append(IssueItem(
                id="Q" + str(len(issues) + 1).zfill(2), level="信息", type="caliber",
                card=card_id, subject=entity + " " + metric, minutes_said=quote,
                raw="", question_hash=qhash,
                suggest=("无法比对：" + note + "；正文只写纪要原话，不给具体数"
                         if note else "口径不可得，正文不引用该结论")))
            continue

        key_metric = "ratio" if ratio_card is not None else METRIC_ABBR.get(metric, "other")
        key = "_".join([
            GRAN_ABBR.get(entity_type, "entity"),
            _slug(entity),
            key_metric,
            result.run_id or "w00",
        ])
        if not qhash:
            qhash = _qhash(card_id, entity, metric, period, compare)

        ledger.entries.append(LedgerEntry(
            key=key, card=card_id, value=float(current), unit=unit,
            display=_display(current, unit),
            spec=("数据来自" + source + "；按记录ID保留第一条；"
                  "时间范围 " + period.replace(" 至 ", " 至 ") + "；"
                  "筛选 " + column + "=" + entity + "；"
                  "口径为" + metric + "求和"),
            question_hash=qhash, code_hash=code_hash,
            status="ok", verdict=verdict, diff=diff, shape="number"))

        if amount_value is not None:
            amount_key = "_".join([
                GRAN_ABBR.get(entity_type, "entity"),
                _slug(entity), "amt", result.run_id or "w00",
            ])
            ledger.entries.append(LedgerEntry(
                key=amount_key, card=card_id, value=float(amount_value), unit="元",
                display=_display(amount_value, "元"),
                spec=("数据来自" + source + "；按记录ID保留第一条；时间范围 "
                      + period + "；筛选 " + column + " 为 " + entity
                      + "；口径为实收金额合计"),
                question_hash=_qhash(card_id, "amount", period),
                code_hash=code_hash, status="ok", verdict="none",
                diff=None, shape="number"))

        if ratio_card is not None:
            left_name, right_name = ratio_card[0], ratio_card[1]
            explain = ("数据来自" + source + "；按记录ID保留第一条；时间范围 "
                       + period + "；筛选 门店名称 为「" + left_name + "」与「"
                       + right_name + "」；口径为日均实收金额之比（合计 / 有销售记录天数）"
                       + ("；口径补充：" + caliber_hint if caliber_hint else "")
                       + "；结果 " + _display(current, "倍") + " 倍。")
            raw_text = _display(current, "倍") + " 倍"
        else:
            explain = ("数据来自" + source + "；按记录ID保留第一条；时间范围 "
                       + period + "；筛选 " + column + " 为 " + entity
                       + "；口径为" + metric + "求和"
                       + ("；口径补充：" + caliber_hint if caliber_hint else "")
                       + "；结果 " + _display(current, unit)
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

    if only_set:
        keep_ids = {str(card.get("id")) for card in cards} - only_set
        old_issues = _read_json(run_dir / "issues.json")
        old_ledger = _read_json(run_dir / "ledger.json")
        # 重算失败的卡片要沿用上一次的结果，不能因为这次没跑出来就丢掉
        recomputed_ok = {item.card for item in results}
        kept_cards = {str(item.get("card")) for item in old_issues.get("results", [])}
        fallback_ids = {cid for cid in only_set
                        if cid in kept_cards and cid not in recomputed_ok}

        def keep(card_id: object) -> bool:
            text_id = str(card_id)
            return text_id in keep_ids or text_id in fallback_ids

        issues = [IssueItem(**item) for item in old_issues.get("issues", [])
                  if keep(item.get("card"))] + issues
        results = [ResultItem(**item) for item in old_issues.get("results", [])
                   if keep(item.get("card"))] + results
        old_entries = old_ledger.get("entries", [])
        missing_card = [entry for entry in old_entries if not entry.get("card")]
        if missing_card:
            log.append("旧台账有 " + str(len(missing_card))
                       + " 条没有 card 字段，无法归属，已丢弃；建议先跑一次完整对账")
        ledger.entries = [LedgerEntry(**entry) for entry in old_entries
                          if keep(entry.get("card"))] + ledger.entries
        if fallback_ids:
            log.append("重算失败，沿用上次结果：" + "、".join(sorted(fallback_ids)))
        log.append("保留未重算的 " + str(len(keep_ids)) + " 张卡片结果")

    result.coverage = _coverage(intake, ledger)
    uncovered = [c for c in result.coverage if c["status"] == "uncovered"]
    for item in uncovered:
        issues.append(IssueItem(
            id="Q" + str(len(issues) + 1).zfill(2), level="信息", type="missing",
            card="", subject=item["item"],
            minutes_said="纪要提到「" + item["item"] + "」，数据里没有对应字段",
            raw="", question_hash=_qhash("coverage", item["item"]),
            suggest=("数据里没有对应列，正文写「无数据来源」"
                     if "没有对应列" in str(item.get("note") or "")
                     else "正文写「无数据来源」：" + str(item.get("note") or "该指标没有被任何卡片覆盖"))

        ))

    for index, item in enumerate(issues, 1):
        item.id = "Q" + str(index).zfill(2)

    stats = ReconcileStats(
        blocking=sum(1 for item in issues if item.level == "阻断"),
        warn=sum(1 for item in issues if item.level == "提示"),
        info=sum(1 for item in issues if item.level == "信息"),
        checked=len(results),
        calls=stats.calls, ok=stats.ok, parse_failed=stats.parse_failed,
        waived=waived_cards,
    )

    result.issues = issues
    result.results = results
    result.stats = stats
    result.state = "ok"
    result.message = ("已核对 " + str(stats.checked) + " 条；阻断 "
                      + str(stats.blocking) + " 条、提示 " + str(stats.warn)
                      + " 条；另有 " + str(stats.info) + " 条数据说明。")
    if stats.waived:
        result.message += "按 S1 人工豁免跳过 " + str(stats.waived) + " 条。"
        log.append("按 S1 人工豁免跳过 " + str(stats.waived) + " 条")
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


# ---------------------------------------------------------------- pandasai 取数

STORE_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}店)")
RATIO_VALUE_RE = re.compile(r"([0-9一二三四五六七八九十]+(?:\.[0-9]+)?)\s*(成|倍|%)")
BAD_PREFIX = ("日均", "销售", "实收", "销售额", "约为", "是", "的", "达到",
              "相当于", "同期", "本周", "上周", "当月")


def _clean_store(name: str) -> str:
    """把「日均销售约为春熙路店」修成「春熙路店」。"""
    changed = True
    while changed:
        changed = False
        for bad in BAD_PREFIX:
            if name.startswith(bad) and len(name) - len(bad) >= 3:
                name = name[len(bad):]
                changed = True
    return name


def _parse_ratio_quote(quote: str) -> tuple[str, str, float] | None:
    """识别「A 约为 B 的 N 成/倍/%」，返回 (A, B, 比值)。

    只处理一句话里出现两个「…店」的情况；识别不出返回 None。
    """
    if not quote:
        return None
    if not re.search(r"约为|是|相当于|达到", quote):
        return None
    stores = [_clean_store(name) for name in STORE_RE.findall(_norm(quote))]
    stores = [name for name in stores if len(name) >= 3 and name.endswith("店")]
    if len(stores) < 2:
        return None
    value_match = RATIO_VALUE_RE.search(_norm(quote))
    if not value_match:
        return None
    number = _cn_number(value_match.group(1))
    if number is None:
        return None
    unit = value_match.group(2)
    if unit == "成":
        ratio = number / 10
    elif unit == "%":
        ratio = number / 100
    else:
        ratio = number
    if not 0 < ratio < 10:
        return None
    return stores[0], stores[1], ratio


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _load_overrides(run_dir: Path) -> dict:
    """<run>/overrides.json：人对某张卡片给的口径建议，形如
    {"M09": {"note": "本周不含周末", "hint": "只统计周一至周五的记录", "by": "用户"}}"""
    data = _read_json(run_dir / "overrides.json")
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


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
                    period: str, compare: str, is_change: bool,
                    hint: str = "") -> str:
    start_date, end_date = [part.strip() for part in period.split("至", 1)]
    base = _metric_phrase(metric)
    if not is_change:
        return (
            "用 df，筛选 销售日期 在 " + start_date + " 至 " + end_date + "、"
            + column + " 为「" + entity + "」的记录，计算" + base + "。"
            "退货按负数直接计入；按记录ID去重保留第一条；结果保留两位小数。"
            + (hint + "。" if hint else "") +
            "最后一行必须是 result = {'type': 'number', 'value': 数值}，"
            "value 里是纯数字，不要字符串、不要单位、不要千分位。"
        )
    c_start, c_end = [part.strip() for part in compare.split("至", 1)]
    return (
        "用 df，分别算 " + c_start + " 至 " + c_end + "（上期）与 "
        + start_date + " 至 " + end_date + "（本期），"
        + column + " 为「" + entity + "」的" + base + "，"
        "再算 (本期 - 上期) / 上期。"
        "退货按负数直接计入；按记录ID去重保留第一条。"
        + (hint + "。" if hint else "") +
        "最后一行必须是 result = {'type': 'number', 'value': round(变化率, 4)}，"
        "value 是小数（例如 -0.1271），不要百分号、不要字符串。"
    )


def _build_ratio_question(a: str, b: str, metric: str, period: str,
                          hint: str = "") -> str:
    """「A 约为 B 的 N 成」：让 pandasai 算两个门店日均之比。"""
    start_date, end_date = [part.strip() for part in period.split("至", 1)]
    return (
        "用 df，筛选 销售日期 在 " + start_date + " 至 " + end_date + " 的记录。"
        "实收金额合计（门店名称 包含「" + a + "」）除以 7 得到 A；"
        "实收金额合计（门店名称 包含「" + b + "」）除以 7 得到 B；"
        + (hint + "。" if hint else "") +
        "最后一行必须是 result = {'type': 'number', 'value': round(A / B, 4)}，"
        "value 里是纯数字，不要字符串、不要单位。"
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
              period: str, compare: str, extra_hint: str = "",
              caliber_hint: str = "") -> dict:
    """调 pandasai 取一张卡片的数。返回工具的原始结果。"""
    metric = str(card.get("metric") or "")
    expect = str(card.get("expect") or "")
    unit = str(card.get("unit") or "元")
    target = _parse_target(card.get("target_value"))
    is_change = expect in ("上升", "下降", "持平")

    ratio = (_parse_ratio_quote(str(card.get("quote") or ""))
             if expect in ("具体数值", "百分比") else None)
    if ratio is not None:
        left, right, value = ratio
        question = _build_ratio_question(left, right, metric, period, caliber_hint)
        expected = {"type": "ratio", "value": value, "tolerance": "ratio_pp:5"}
        reference = value
    else:
        question = _build_question(column, entity, metric, period, compare,
                                   is_change, caliber_hint)
        expected = _build_expected(expect, metric, unit, target)
        reference = None
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


def _save_override(run_dir: Path, card_ids: list[str], hint: str, note: str = "",
                   by: str = "用户") -> None:
    """把口径建议写进 <run>/overrides.json，重算时读它拼进问句。"""
    import datetime as dt

    path = run_dir / "overrides.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        data = {}
    for card_id in card_ids:
        data[str(card_id)] = {
            "card": str(card_id),
            "note": note or hint,
            "hint": hint,
            "by": by,
            "at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
    parser.add_argument("--only", action="append", default=None,
                        help="只重算指定卡片，可重复，如 --only M09")
    parser.add_argument("--hint", default="",
                        help="给 --only 的卡片加一条口径说明，如「只统计周一至周五的记录」")
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    if not run_dir.is_dir() and args.run_root:
        run_dir = Path(args.run_root) / args.run
    if args.only and args.hint:
        _save_override(run_dir, args.only, args.hint, args.hint)

    try:
        result, log = run_s3(args.run, args.run_root, only=args.only)
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
