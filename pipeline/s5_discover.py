"""S5 数据发现：GLM 出「单步可答」的探索问题，交给 pandasai 执行取数。

本模块只做「发现 / 取数 / 算数 / 选题」，不写任何文件、不落 ledger、不做规制校验：
- 传入 questions 清单时只跑清单，不再调 GLM 出题；不传时 GLM(chat_fn) 依据列名 /
  周期 / 画像 / 纪要骨架主题，提出 5–8 个单步可答的探索问题；
- 每个问题用 DataQA.ask() 执行，number 取单个值、table 取一组行；
- 表格结果里的脏分组名（如「广 州天河店」带空格）在代码里规范化后才进 finding；
- 为每个可用数值生成稳定 key（find_<id>_<idx>_<tag>）与 display（金额千分位 / 百分比一位小数）；
- code_hash = sha256(code)[:16]，拿不到代码记「无代码」并标 flags，不静默当通过；
- 单条失败 / 超时 / 空结果一律跳过并记 log，绝不造数；全部失败仍返回空 findings，
  由上层（s5_write）决定降级。

finding 字段与 s5_write 的约定（严格 12 个字段，不含多余项）：
    id / question / shape / value / rows / code / code_hash / spec / display /
    keys / reason / flags
- number：value 是 float，display 是该值的格式化串，keys 恰好 1 个，rows=None。
- table ：value=None，rows 是规范化后的 list[dict]（列名与字符串取值去空格、
          数值单元格一律转成 float）。数值列按 rows 首行的键顺序判定（值为 float 的列）。
          keys 与「行 × 数值列」一一对应，按行优先、同行内按数值列顺序生成，
          idx 从 0 递增，即 keys[i] 对应 rows[i // M][measures[i % M]]，M=数值列数。
          display 是首个数值的格式化串；其余每个 key 的 value / display 可由 s5_write
          按上面映射从 rows 复算（展示用 s5_write._fmt_amount / _fmt_pct）。
- spec 含「问句 + 口径 + 形状 + 代码指纹」，code_hash 单独成列；s5_write 直接把二者
  连同 added_by=S5 写进 ledger 条目。verdict=none/unsupported 属正常（发现类问题没有
  expected/reference），不据此判失败。

chat_fn / qa_factory 均可注入（也可 monkeypatch 本模块的 chat / make_qa），方便测试：
- chat_fn(messages) -> dict(text, model, ...) 或直接返回 JSON 字符串，默认 pipeline.llm.chat；
- qa_factory({"df_sales": frame}, cache_dir=run_dir/"cache") -> 带 .ask(question,
  expect_shape=shape, use_cache=True) 的对象，默认 pipeline.pandasai_glm.make_qa。
"""
from __future__ import annotations

import hashlib
import json
import numbers
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .llm import DEFAULT_MODEL, LLMError, chat
from .pandasai_glm import make_qa
from .s5_questions import MAX_QUESTIONS, normalize_questions as normalize_question_list
from .s5_filters import filter_questions


def _model_label(fallback: Any = None) -> str:
    """日志/结果里展示当前模型名；未配置时退回内置默认值。"""
    if fallback:
        return str(fallback)
    try:
        from .llm import get_config

        return get_config().model
    except Exception:  # noqa: BLE001 - 只用于展示
        return str(DEFAULT_MODEL)


# ---------------------------------------------------------------- 常量

_PCT_WORDS = ("占比", "比例", "百分比", "率", "%", "环比", "同比", "增幅", "降幅", "达成率")
_AMT_WORDS = ("金额", "实收", "营收", "销售额", "成本", "原价", "折扣", "毛利", "额")
_CNT_WORDS = ("数量", "笔数", "单数", "件数", "次数", "个数", "行数", "天数")
_TAG_MAP = {
    "实收金额": "rev", "成本金额": "cost", "原价金额": "orig", "折扣金额": "disc",
    "销售数量": "qty", "目标金额": "target", "毛利额": "gp", "记录ID": "id",
}
_FENCE = chr(96) * 3
_WS_RE = re.compile(r"[\s\u3000\ufeff]+")
_MAX_Q = 8

_PROMPT = """你是经营周报的数据发现助手。只依据下面给出的真实列名与周期，提出 5–8 个「有意思」的探索问题，交给 pandas 工具去取数。

<<CONTEXT>>

【硬性要求 · 单步可答】（最重要）
1. 每个问题必须能用「一步」pandas 操作回答：
   - number 类：一次筛选 + 一次 sum / mean / count / max / min，只得到一个数字；
   - table 类：一次 groupby + 一次聚合，得到一个「分组列 + 一个数值列」的两列表。
2. 严禁多步推导：禁止「先算环比 / 占比再排名前 N」「Top3」「先分组再合并 / 透视」
   「跨表 join」「窗口 / 滚动」「任何两步以上计算」——这类问句工具会直接失败。
3. 只使用上面列出的真实列名，不得臆造列；分组 / 筛选值写成数据里真实存在的值或清晰范围。
4. 数字类问句写明：只输出一个数字，不带单位、不带千分位、不带文字、不画图。
   表格类问句写明：返回两列，一列分组名、一列聚合后的数值。
5. 问句里带周期（本周起止，必要时对比期起止）与筛选条件，口径明确、可直接执行。
6. 问题指向值得写进报告的经营结论：幅度（谁最高 / 最低）、金额（谁贡献最大）、结构（占比 / 构成）、
   单店或单品的突出表现。
7. 不要提数据质量问题：缺数、缺失、重复、去重、异常记录、负数 / 冲销、记录数 / 行数 / 条数 / 天数、
   POS 上传 / 网络故障、数据核对等一律不问；不要评价纪要说法是否一致，不要写「核实 / 待核实 / 疑似 / 可能」。
8. 尽量覆盖上述多类，不要 8 个都问同一件事；不要重复问纪要骨架已明确给出的结论，只补细节，不评价底稿说法。
9. 5–8 个问题里至少 3 个 table，优先「按门店 / 品类 / 渠道 / 商品分组的金额或数量排名」。

【输出】严格 JSON，不要 markdown 代码块、不要任何解释：
{"questions":[{"id":"D1","question":"...","shape":"number","reason":"为什么值得问（一句话）"},{"id":"D2","question":"...","shape":"table","reason":"..."}]}
id 用 D1、D2……；shape 只能是 number 或 table；给 5–8 个问题。
"""


# ---------------------------------------------------------------- 小工具

def _clean_name(value: Any) -> str:
    """列名 / 分组名去空格（含全角空格、BOM）。"""
    return _WS_RE.sub("", "" if value is None else str(value))


def _to_number(value: Any) -> float | None:
    """单元格规约成 float；不是数返回 None。bool 不当数。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, numbers.Number):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("，", "").replace("%", "").strip()
        if not text:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
        return None if pd.isna(num) else num
    return None


def _clean_rows(rows: list) -> list[dict]:
    """规范化表格结果：列名去空格、字符串取值去空格、数值单元格转 float。"""
    out: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row: dict = {}
        for col, val in raw.items():
            name = _clean_name(col)
            num = _to_number(val)
            row[name] = num if num is not None else _clean_name(val)
        out.append(row)
    return out


def _split_columns(rows: list[dict]) -> tuple[list[str], list[str]]:
    """按首行推断数值列与分组列：整列都是 float 的算数值列。"""
    if not rows:
        return [], []
    keys = list(rows[0].keys())
    measure = [k for k in keys
               if all(isinstance(r.get(k), float) for r in rows)]
    group = [k for k in keys if k not in measure]
    return measure, group


def _is_pct(text: str) -> bool:
    return any(w in str(text or "") for w in _PCT_WORDS)


def _fmt(value: float, pct: bool) -> str:
    v = float(value)
    if pct:
        pv = v * 100.0 if abs(v) <= 1.5 else v
        return f"{pv:.1f}%"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,d}"
    return f"{v:,.2f}"


def _tag(text: str, col_index: int) -> str:
    mapped = _TAG_MAP.get(_clean_name(text))
    if mapped:
        return mapped
    ascii_part = re.sub(r"[^a-z0-9]+", "", str(text or "").lower())[:8]
    if ascii_part:
        return ascii_part
    if _is_pct(text):
        return "pct"
    if any(w in str(text or "") for w in _AMT_WORDS):
        return "amt"
    if any(w in str(text or "") for w in _CNT_WORDS):
        return "cnt"
    return "v" + str(col_index)


def _extract_json(text: str) -> dict:
    body = str(text or "").strip()
    if body.startswith(_FENCE):
        body = body[len(_FENCE):]
        body = re.sub(r"^[a-zA-Z]*\s*", "", body)
        body = body.rstrip()
        if body.endswith(_FENCE):
            body = body[:-len(_FENCE)]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    return json.loads(body[start:end + 1])


# ---------------------------------------------------------------- 出题上下文

def _context(frame, intake: dict, profile: dict, skeleton_items) -> str:
    cols: list[str] = []
    try:
        cols = [str(c) for c in frame.columns]
    except Exception:  # noqa: BLE001 - frame 不可用时退回画像
        cols = []
    if not cols and isinstance(profile, dict) and isinstance(profile.get("columns"), dict):
        cols = [str(c) for c in profile["columns"]]

    minutes: dict = {}
    if isinstance(intake, dict) and isinstance(intake.get("minutes"), dict):
        minutes = intake["minutes"]
    week = str(minutes.get("week") or "")
    period = ("本周 " + str(minutes.get("period_start") or "?") + " 至 "
              + str(minutes.get("period_end") or "?"))
    compare = ("对比期 " + str(minutes.get("compare_start") or "?") + " 至 "
               + str(minutes.get("compare_end") or "?"))

    plines: list[str] = []
    if isinstance(profile, dict):
        cov = profile.get("coverage")
        if isinstance(cov, dict):
            plines.append("数据覆盖 " + str(cov.get("start") or "?") + " 至 "
                          + str(cov.get("end") or "?") + "；断档 "
                          + str(cov.get("missing_days") if cov.get("missing_days") is not None else "?")
                          + " 天")
        pcols = profile.get("columns")
        if isinstance(pcols, dict):
            for name, info in list(pcols.items())[:12]:
                if not isinstance(info, dict):
                    continue
                if info.get("type") == "continuous":
                    plines.append(f"数值列 {name}：缺失率 {info.get('missing_rate')}，"
                                  f"均值 {info.get('mean')}，最小 {info.get('min')}，"
                                  f"最大 {info.get('max')}，偏度 {info.get('skewness')}")
                elif info.get("type") == "categorical":
                    cats = info.get("categories") or []
                    sample = ("（如 " + "、".join(str(c[0]) for c in cats[:5]) + "）") if cats else ""
                    plines.append(f"分组列 {name}：{info.get('n_unique')} 个取值" + sample)
        warns = profile.get("warnings")
        if isinstance(warns, list) and warns:
            plines.append("画像告警：" + "；".join(str(w) for w in warns[:3]))

    topics: list[str] = []
    for item in (skeleton_items or []):
        if isinstance(item, dict):
            sent = str(item.get("fixed") or item.get("original") or "").strip()
        else:
            sent = str(item or "").strip()
        if sent:
            topics.append(sent)
    topics_text = "\n".join("- " + t for t in topics[:20]) or "（无）"

    return ("【数据表】销售明细（单表），可用列名：\n" + "、".join(cols)
            + "\n【周期】" + (week + " " if week else "") + period + "；" + compare
            + "\n【数据画像摘要】\n" + ("\n".join(plines) or "（无）")
            + "\n【纪要骨架主题（仅作探索方向，不得改写这些原句）】\n" + topics_text)


def _normalize_questions(data: dict, log: list[str]) -> list[dict]:
    raw = data.get("questions") if isinstance(data, dict) else None
    out = normalize_question_list(raw, strict=False, max_count=_MAX_Q)
    if out and len(out) < 5:
        log.append("模型只给出 " + str(len(out)) + " 个问题（要求 5–8），按实际数量执行")
    return out


# ---------------------------------------------------------------- 执行 / 装配

def _ask(qa, question: str, shape: str):
    try:
        return qa.ask(question, expect_shape=shape, use_cache=True)
    except TypeError:  # 测试桩可能只收 question
        return qa.ask(question)


def _make_finding(fid: str, q: dict, shape: str, res: Any, log: list[str]) -> dict | None:
    if not isinstance(res, dict):
        log.append(fid + " 结果不是字典，跳过")
        return None

    ok_flag = res.get("ok", True)  # 缺 ok 时按有值处理；显式 False 判失败
    raw_flags = res.get("flags")
    flags = list(raw_flags) if isinstance(raw_flags, (list, tuple)) else []
    code = str(res.get("code") or "")
    if code.strip():
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
    else:
        code_hash = "无代码"
        flags.append("code_unavailable")

    if shape == "number":
        value = res.get("value")
        if (not ok_flag or value is None or isinstance(value, bool)
                or not isinstance(value, numbers.Number)):
            log.append(fid + " 单值结果为空 / 失败，跳过")
            return None
        value = float(value)
        pct = _is_pct(q["question"])
        display = _fmt(value, pct)
        keys = [f"find_{fid}_0_{_tag(q['question'], 0)}"]
        rows_out = None
        value_out: float | None = value
    else:
        raw_rows = res.get("rows")
        if isinstance(raw_rows, pd.DataFrame):
            raw_rows = json.loads(raw_rows.head(200).to_json(orient="records",
                                                            force_ascii=False))
        if not ok_flag or not isinstance(raw_rows, list) or len(raw_rows) == 0:
            log.append(fid + " 表格结果为空 / 失败，跳过")
            return None
        rows_out = _clean_rows(raw_rows)
        measures, _groups = _split_columns(rows_out)
        if not measures:
            log.append(fid + " 表格没有数值列，跳过")
            return None
        keys: list[str] = []
        displays: list[str] = []
        idx = 0
        for row in rows_out:
            for c_i, col in enumerate(measures):
                val = row.get(col)
                if not isinstance(val, float):
                    continue
                keys.append(f"find_{fid}_{idx}_{_tag(col, c_i)}")
                displays.append(_fmt(val, _is_pct(col)))
                idx += 1
        if not keys:
            log.append(fid + " 表格没有可用数值，跳过")
            return None
        display = displays[0]
        value_out = None
        flags.append("table_rows:" + str(len(rows_out)))

    spec = ("S5 数据发现｜问句：" + q["question"]
            + "｜口径：" + (q.get("reason") or "单步聚合")
            + "｜形状：" + shape
            + "｜单步可答（一次 groupby / 一次聚合，不多步推导）"
            + "｜代码指纹：" + code_hash)
    return {
        "id": fid,
        "question": q["question"],
        "shape": shape,
        "value": value_out,
        "rows": rows_out,
        "code": code,
        "code_hash": code_hash,
        "spec": spec,
        "display": display,
        "keys": keys,
        "reason": str(q.get("reason") or ""),
        "flags": flags,
    }


# ---------------------------------------------------------------- 主入口

def discover(run_dir, frame, intake, profile, skeleton_items,
             chat_fn=None, qa_factory=None, questions=None,
             keep_quality=False) -> dict:
    """返回 {"findings", "log", "model", "questions", "source", "dropped_questions"}。

    questions 传 None 时由 GLM 出题；传列表时只用这份清单，不再调模型出题。
    keep_quality=True 用于复核场景或已通过问题过滤的清单，跳过词表二次过滤。
    dropped_questions 是出题阶段被质量过滤掉、没有执行的问题，供 AI 复核查看。
    失败一律返回空 findings，由上层（s5_write）决定明确失败，不造数。
    """
    log: list[str] = []
    chat_fn = chat_fn or chat
    qa_factory = qa_factory or make_qa
    source = "plan" if questions is not None else "auto"
    model = ""
    dropped_questions: list[dict] = []

    def pack(findings=None, model_name=None, qa_questions=None) -> dict:
        return {"findings": findings or [], "log": log,
                "model": model if model_name is None else model_name,
                "questions": qa_questions or [], "source": source,
                "dropped_questions": dropped_questions}

    if questions is not None:
        try:
            questions = normalize_question_list(questions, strict=True,
                                                max_count=MAX_QUESTIONS)
        except ValueError as exc:
            log.append("问题清单无效：" + str(exc))
            return pack()
        if not keep_quality:
            raw_questions = list(questions)
            questions, dropped = filter_questions(questions)
            dropped_questions = [item for item in raw_questions
                                 if str(item.get("id") or "") in set(dropped)]
            if dropped:
                log.append("跳过数据质量问题：" + "、".join(dropped))
        if not questions:
            log.append("问题清单没有可用的经营问题")
            return pack()
        log.append("使用问题清单 " + str(len(questions)) + " 条，不重新出题")
    else:
        messages = [
            {"role": "system",
             "content": "你是严谨的经营数据分析助手，只输出 JSON，不写解释、不写代码块。"},
            {"role": "user", "content": _PROMPT.replace("<<CONTEXT>>",
                                                         _context(frame, intake, profile,
                                                                  skeleton_items))},
        ]
        try:
            try:
                # 出题 prompt 含画像摘要，默认 60s 常超时；单独放宽并禁用 thinking
                out = chat_fn(messages, timeout=180, max_tokens=2048, thinking="disabled")
            except TypeError:
                out = chat_fn(messages)  # 测试桩可能不收 kwargs
        except LLMError as exc:
            log.append("模型出题失败：" + str(exc))
            return pack(model_name=_model_label())
        except Exception as exc:  # noqa: BLE001 - 出题失败交给上层降级
            log.append("模型出题异常：" + str(exc))
            return pack(model_name=_model_label())

        if isinstance(out, dict):
            model = str(out.get("model") or "")
            text = str(out.get("text") or "")
        else:
            text = str(out or "")
        try:
            data = _extract_json(text)
        except Exception as exc:  # noqa: BLE001 - 模型格式问题，不续写
            log.append("模型出题返回不是合法 JSON：" + str(exc))
            return pack(model_name=_model_label(model))

        questions = _normalize_questions(data, log)
        raw_questions = list(questions)
        questions, dropped = filter_questions(questions)
        dropped_questions = [item for item in raw_questions
                             if str(item.get("id") or "") in set(dropped)]
        if dropped:
            log.append("跳过数据质量问题：" + "、".join(dropped))
        if not questions:
            log.append("模型没有给出可用的经营问题")
            return pack(model_name=_model_label(model))

    cache = Path(run_dir) / "cache" if run_dir is not None else None
    try:
        try:
            qa = qa_factory({"df_sales": frame}, cache_dir=cache)
        except TypeError:  # 测试桩可能不收 cache_dir
            qa = qa_factory({"df_sales": frame})
    except Exception as exc:  # noqa: BLE001 - 构造失败整体降级
        log.append("构造 DataQA 失败：" + str(exc))
        return pack(model_name=_model_label(model), qa_questions=questions)

    if not model:
        model = _model_label(getattr(qa, "model", ""))

    findings: list[dict] = []
    used_ids: set[str] = set()
    for i, q in enumerate(questions):
        fid = q["id"]
        if fid in used_ids:
            fid = "D" + str(i + 1)
            while fid in used_ids:
                fid += "x"
        used_ids.add(fid)
        try:
            res = _ask(qa, q["question"], q["shape"])
        except Exception as exc:  # noqa: BLE001 - 单条失败 / 超时一律跳过
            log.append(fid + " 取数失败（" + type(exc).__name__ + "）：" + str(exc)[:160])
            continue
        finding = _make_finding(fid, q, q["shape"], res, log)
        if finding is not None:
            findings.append(finding)

    log.append("发现完成：可用 " + str(len(findings)) + " 条 / 共 "
               + str(len(questions)) + " 问")
    return pack(findings=findings, qa_questions=questions)
