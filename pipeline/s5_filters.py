"""S5 口径过滤：不讨论数据质量，不写“核实/是否一致”这类占位话。

只处理 S5 自己生成的问题、发现和补充句；S4 骨架仍按原规则逐字保留。
"""
from __future__ import annotations

import re

QUALITY_WORDS = (
    "记录数", "记录条数", "记录行数", "记录数量", "数据行", "行数", "条数", "天数",
    "缺失", "缺数", "空值", "重复", "去重", "异常记录", "负数", "冲销",
    "数据质量", "数据核对", "核对数据", "重复上传", "上传数据", "网络波动",
    "POS 故障", "POS故障",
)

META_WORDS = (
    "核实", "是否一致", "不一致", "疑似", "待确认", "需确认", "为什么", "原因",
)

_FIG_REF_RE = re.compile(
    r"[（(]\s*(?:见|详见|参见)\s*图\s*[0-9]+"
    r"(?:\s*[、,，和及]\s*图\s*[0-9]+)*\s*[）)]"
    r"|(?:见|详见|参见)\s*图\s*[0-9]+"
    r"(?:\s*[、,，和及]\s*图\s*[0-9]+)*"
    r"|如\s*图\s*[0-9]+\s*所示"
    r"|图\s*[0-9]+\s*(?:所示|显示|可见)"
)

_SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]?")
_HEDGE_RE = re.compile(
    r"[，,；;]?\s*(?:"
    r"[^，。；;!?]*?是否[^，。；;!?]*"
    r"|与[^，。；;!?]*?(?:表述)?是否一致[^，。；;!?]*"
    r"|(?:需|需要)?(?:进一步)?核实[^，。；;!?]*"
    r"|原因待核实"
    r"|待核实"
    r"|疑似[^，。；;!?]*"
    r"|可能[^，。；;!?]*"
    r"|待确认[^，。；;!?]*"
    r"|需确认[^，。；;!?]*"
    r"|与[^，。；;!?]*不符"
    r")"
)


def is_quality_text(text: str) -> bool:
    value = str(text or "")
    return any(word in value for word in QUALITY_WORDS)


def is_meta_text(text: str) -> bool:
    value = str(text or "")
    return any(word in value for word in META_WORDS)


def is_quality_question(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    text = str(item.get("question") or "") + " " + str(item.get("reason") or "")
    return is_quality_text(text) or is_meta_text(text)


def is_quality_finding(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    text = " ".join(str(item.get(key) or "") for key in
                    ("question", "reason", "spec", "display"))
    return is_quality_text(text) or is_meta_text(text)


def filter_questions(questions: list[dict]) -> tuple[list[dict], list[str]]:
    """返回 (保留的问题, 被跳过的编号)。不改原列表。"""
    kept: list[dict] = []
    dropped: list[str] = []
    for item in questions or []:
        if not isinstance(item, dict):
            continue
        if is_quality_question(item):
            dropped.append(str(item.get("id") or "?"))
        else:
            kept.append(item)
    return kept, dropped


def strip_figure_refs(text: str) -> str:
    """正文不写「见图 N」：图由程序就近放置，正文只写对象、数值与结论。"""
    out = _FIG_REF_RE.sub("", str(text or ""))
    out = re.sub(r"[（(]\s*[）)]", "", out)
    out = re.sub(r"[，,；;]\s*([。！？!?])", r"\1", out)
    out = re.sub(r"^[，,；;。！？!?\s]+", "", out)
    out = re.sub(r"[ \u3000]{2,}", " ", out)
    return out.strip()


def _tidy(sentence: str) -> str:
    value = re.sub(r"[，,；;]\s*([。！？!?])", r"\1", sentence)
    value = re.sub(r"([。！？!?])[，,；;]+", r"\1", value)
    value = re.sub(r"[，,；;]{2,}", "，", value)
    value = re.sub(r"^[，,；;。！？!?\s]+", "", value)
    value = re.sub(r"[ \u3000]{2,}", " ", value)
    return value.strip()


def clean_text(text: str) -> str:
    """删掉数据质量句，清掉“待核实/是否一致/疑似/可能”等从句与图号引用。"""
    out: list[str] = []
    for raw in _SENTENCE_RE.findall(strip_figure_refs(text)):
        sentence = raw.strip()
        if not sentence:
            continue
        if is_quality_text(sentence):
            continue
        sentence = _HEDGE_RE.sub("", sentence)
        sentence = _tidy(sentence)
        if len(sentence.strip("。！？!?，,；; ")) >= 6:
            out.append(sentence)
    return "".join(out).strip()


def clean_items(items: list) -> list[str]:
    cleaned: list[str] = []
    for item in items or []:
        text = clean_text(str(item or ""))
        if text:
            cleaned.append(text)
    return cleaned


def clean_mapping(mapping: dict) -> dict:
    cleaned: dict = {}
    for key, value in (mapping or {}).items():
        text = clean_text(str(value or ""))
        if text:
            cleaned[str(key)] = text
    return cleaned
