"""把纪要里的指标与实体映射到数据列。

边界：AI 只做「自然语言 → 结构化」的映射，不判断准入是否通过。
产物落盘到 <run.dir>/domain_map.json，按输入哈希冻结：同一份材料重跑读文件，
不会再问一次模型，保证同一输入得到同一映射。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .llm import LLMError, chat, get_config

MAX_MINUTES_CHARS = 6000


@dataclass
class MetricMap:
    name: str
    kind: str                 # 核心 | 补充
    columns: list[str] = field(default_factory=list)
    evidence: str = ""
    confidence: float = 0.0


@dataclass
class EntityMap:
    type: str
    column: str
    values: list[str] = field(default_factory=list)


@dataclass
class DomainMap:
    metrics: list[MetricMap] = field(default_factory=list)
    entities: list[EntityMap] = field(default_factory=list)
    model: str = ""
    input_hash: str = ""
    dropped: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


PROMPT = """你是数据口径映射器。只做一件事：把会议纪要里提到的指标和实体，映射到给定的数据列上。

规则：
1. 只能使用下面列出的列名，一个字都不能改；找不到就留空数组，不要猜、不要造。
2. 每个指标给：名称、类别（核心/补充）、需要的列、纪要里的原句（必须逐字摘自纪要）。
3. 「核心」指纪要明确要求写进周报主表的指标；其余为「补充」。
4. 每个实体给：类型（区域/门店/品类/商品/平台/店铺/类目/SKU/渠道/其他）、对应列、纪要里出现的取值。
5. 纪要要求与上周/上期/同期比较时，额外输出一项 name=「较上周」、kind=核心，columns 写日期列与金额列。
6. 只输出一个 JSON 对象，不要解释、不要 markdown 代码块。

输出格式：
{
  "metrics": [
    {"name": "实收", "kind": "核心", "columns": ["实收金额"], "evidence": "原句", "confidence": 0.9}
  ],
  "entities": [
    {"type": "区域", "column": "区域", "values": ["华南", "华东"]}
  ]
}

数据文件与列：
%s

会议纪要：
%s
"""


def _columns_blob(columns_by_file: dict[str, list[str]]) -> str:
    lines = []
    for name, columns in columns_by_file.items():
        lines.append("- " + name + "：" + "、".join(columns))
    return "\n".join(lines) or "（没有数据文件）"


def _extract_json(text: str) -> dict:
    fence = chr(96) * 3
    text = text.strip()
    if text.startswith(fence):
        text = text[len(fence):]
        text = re.sub(r"^[a-zA-Z]*\s*", "", text)
        text = text.rstrip()
        if text.endswith(fence):
            text = text[:-len(fence)]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    return json.loads(text[start:end + 1])


def _match_column(name: str, all_columns: dict[str, str]) -> str | None:
    """列名容错：去掉空白后比对，处理模型写成「实 收金额」这类情况。"""
    if name in all_columns:
        return name
    compact = re.sub(r"s+", "", name)
    for column in all_columns:
        if re.sub(r"s+", "", column) == compact:
            return column
    return None


def _validate(raw: dict, columns_by_file: dict[str, list[str]],
              minutes_text: str) -> DomainMap:
    all_columns = {column: name for name, columns in columns_by_file.items()
                   for column in columns}
    compact_minutes = re.sub(r"\s+", "", minutes_text)
    result = DomainMap()

    for item in raw.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            result.dropped.append("指标缺名称")
            continue
        raw_columns = [str(c).strip() for c in (item.get("columns") or []) if str(c).strip()]
        columns: list[str] = []
        unknown: list[str] = []
        for candidate in raw_columns:
            matched = _match_column(candidate, all_columns)
            if matched:
                if matched not in columns:
                    columns.append(matched)
            else:
                unknown.append(candidate)
        if unknown:
            result.dropped.append(name + " 引用了不存在的列：" + "、".join(unknown))
        evidence = str(item.get("evidence") or "").strip()
        if evidence and re.sub(r"\s+", "", evidence) not in compact_minutes:
            result.dropped.append(name + " 的证据不是纪要原句")
            evidence = ""
        kind = str(item.get("kind") or "补充").strip()
        if kind not in ("核心", "补充"):
            kind = "补充"
        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if any(existing.name == name for existing in result.metrics):
            result.dropped.append("重复指标：" + name)
            continue
        result.metrics.append(MetricMap(name=name, kind=kind, columns=columns,
                                        evidence=evidence, confidence=confidence))

    for item in raw.get("entities") or []:
        if not isinstance(item, dict):
            continue
        raw_column = str(item.get("column") or "").strip()
        column = _match_column(raw_column, all_columns) if raw_column else None
        if column is None:
            if raw_column:
                result.dropped.append("实体列不存在：" + raw_column)
            continue
        if any(hint in column.lower() for hint in ("日期", "时间", "date", "time")):
            result.dropped.append("日期列不当实体：" + column)
            continue
        values = [str(v).strip() for v in (item.get("values") or []) if str(v).strip()]
        values = [v for v in values if not re.fullmatch(
            r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?", v)]
        if not values:
            continue
        result.entities.append(EntityMap(type=str(item.get("type") or "其他"),
                                         column=column, values=values))
    return result


def input_hash(minutes_text: str, columns_by_file: dict[str, list[str]], model: str) -> str:
    raw = (model + "\x00" + minutes_text + "\x00"
           + json.dumps(columns_by_file, sort_keys=True, ensure_ascii=False))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def propose(minutes_text: str, columns_by_file: dict[str, list[str]]) -> DomainMap:
    """调一次模型，返回校验过的映射。失败抛 LLMError / ValueError。"""
    config = get_config()
    prompt = PROMPT % (_columns_blob(columns_by_file),
                       minutes_text.strip()[:MAX_MINUTES_CHARS])
    # 这个模型是推理模型，reasoning 会吃掉大量 token：给 2048 时正文是空的，8192 才稳定
    out = chat([{"role": "user", "content": prompt}], temperature=0.0,
               max_tokens=4096, timeout=180, thinking="disabled")
    if not out["text"].strip():
        raise ValueError(f"模型没有返回内容（finish_reason={out['finish_reason']}）")
    mapping = _validate(_extract_json(out["text"]), columns_by_file, minutes_text)
    mapping.model = out["model"]
    mapping.input_hash = input_hash(minutes_text, columns_by_file, out["model"])
    if not mapping.metrics:
        raise ValueError("模型没有给出任何可用指标映射")
    return mapping


def load_or_build(run_dir: Path, minutes_text: str,
                  columns_by_file: dict[str, list[str]]) -> DomainMap:
    """先读 <run_dir>/domain_map.json；输入变了才重新问模型。"""
    config = get_config()
    path = Path(run_dir) / "domain_map.json"
    digest = input_hash(minutes_text, columns_by_file, config.model)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("input_hash") == digest:
                return DomainMap(
                    metrics=[MetricMap(**m) for m in data.get("metrics") or []],
                    entities=[EntityMap(**e) for e in data.get("entities") or []],
                    model=data.get("model", ""), input_hash=digest,
                    dropped=data.get("dropped") or [], note="复用已落盘的映射",
                )
        except Exception:
            pass
    mapping = propose(minutes_text, columns_by_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = mapping.to_dict()
    payload["input_hash"] = digest
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return mapping
