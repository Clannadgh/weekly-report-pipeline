"""S1 准入检查的数据契约。

所有落盘文件（intake.json / status.json）与 Web API 的响应都按这里的模型序列化，
下游步骤只认这些字段，不读对话。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["数据", "纪要", "规范"]
ReqKind = Literal["核心", "补充"]
ReqStatus = Literal["have", "degrade", "missing"]
IntakeState = Literal["准入通过", "暂停", "待补充"]
NextAction = Literal["continue", "wait"]


class FileInfo(BaseModel):
    """一个输入文件的清点结果。哈希、行数、表名、日期范围一律现取。"""

    name: str                      # 原始文件名（不含路径）
    path: str = ""                 # 相对运行目录的路径，如 inputs/sales.csv
    format: str = ""               # csv / xlsx / md / txt / docx ...
    size: int = 0                  # 字节
    sha256: str = ""
    role: Role = "数据"
    readable: bool = True
    sheets: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    date_range: str = ""           # 2026-10-26 至 2026-11-01；取不到留空
    rows: int = 0
    duplicate_of: str | None = None
    error: str = ""                # 不可读时的原因


class Counts(BaseModel):
    data: int = 0
    minutes: int = 0
    prompt: int = 0


class PromptInfo(BaseModel):
    present: bool = False
    goal: str = ""
    scope: str = ""
    extras: list[str] = Field(default_factory=list)


class MinutesInfo(BaseModel):
    week: str = ""                 # 2026-W44
    period_start: str = ""         # 2026-10-26
    period_end: str = ""           # 2026-11-01
    compare_start: str = ""        # 2026-10-19
    compare_end: str = ""          # 2026-10-25
    entities: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    actions: int = 0
    parse_ok: bool = True
    note: str = ""


class Requirement(BaseModel):
    item: str
    kind: ReqKind = "核心"
    needs: list[str] = Field(default_factory=list)
    status: ReqStatus = "missing"
    evidence: str = ""             # 实际表名/列名/日期范围
    impact: str = ""
    note: str = ""


class ColumnMismatch(BaseModel):
    column: str
    missing_in: list[str] = Field(default_factory=list)
    types: dict[str, str] = Field(default_factory=dict)


class DateOverlap(BaseModel):
    start: str
    end: str
    files: list[str] = Field(default_factory=list)


class DuplicateFile(BaseModel):
    files: list[str] = Field(default_factory=list)
    kind: Literal["same_hash", "overlap_content"] = "same_hash"
    note: str = ""


class Conflict(BaseModel):
    kind: str                      # period / entity / caliber
    detail: str


class Consistency(BaseModel):
    column_mismatch: list[ColumnMismatch] = Field(default_factory=list)
    date_overlap: list[DateOverlap] = Field(default_factory=list)
    cross_file_duplicates: list[DuplicateFile] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


class Coverage(BaseModel):
    merged_start: str = ""
    merged_end: str = ""
    covers_prompt_scope: bool = True


class MissingItem(BaseModel):
    what: str
    impact: str = ""
    how: str = ""


class DegradeItem(BaseModel):
    item: str
    impact: str = ""


class RunInfo(BaseModel):
    period_tag: str = ""           # w44
    dir: str = ""                  # runs/w44（相对项目根）


class Waiver(BaseModel):
    """一条人工豁免：人确认「已知悉，接受这个冲突」，不是工具判错了。"""
    at: str = ""
    by: str = ""
    items: list[str] = Field(default_factory=list)
    reason: str = ""


class IntakeResult(BaseModel):
    state: IntakeState
    run: RunInfo = Field(default_factory=RunInfo)
    files: list[FileInfo] = Field(default_factory=list)
    counts: Counts = Field(default_factory=Counts)
    prompt: PromptInfo = Field(default_factory=PromptInfo)
    minutes: MinutesInfo = Field(default_factory=MinutesInfo)
    requirements: list[Requirement] = Field(default_factory=list)
    consistency: Consistency = Field(default_factory=Consistency)
    coverage: Coverage = Field(default_factory=Coverage)
    missing: list[MissingItem] = Field(default_factory=list)
    degrade: list[DegradeItem] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    waived: list[Waiver] = Field(default_factory=list)
    message: str = ""
    next_action: NextAction = "wait"


class StatusHistory(BaseModel):
    at: str
    state: str
    updated_by: str
    note: str = ""


class Status(BaseModel):
    stage: str = "intake"
    state: str = ""
    updated_by: str = "S1"
    at: str = ""
    note: str = ""
    history: list[StatusHistory] = Field(default_factory=list)


# ---------------------------------------------------------------- S2 纪要拆卡

CardCheckable = Literal["能", "不能", "不确定"]
CardsState = Literal["ok", "empty", "blocked"]


class CardEntry(BaseModel):
    """纪要的一个段落。"""
    no: int = 0
    region: str = ""
    owner: str = ""
    para: int = 0
    text: str = ""


class Card(BaseModel):
    """一句话拆出来的一张卡片。十一项必填字段见 s2.txt 第 2 步。"""
    id: str = ""
    quote: str = ""
    entity: str = "不确定"
    entity_type: str = "未知"
    metric: str = "其他"
    unit: str = ""
    period: str = ""
    compare_period: str = ""
    expect: str = "说不清"
    target_value: str | None = None
    checkable: CardCheckable = "不确定"
    para: int = 0
    sentence: int = 0
    source: str = ""
    note: str = ""


class CardAction(BaseModel):
    item: str = ""
    owner: str = ""
    due: str = ""
    source: str = ""


class CardFuture(BaseModel):
    text: str = ""
    source: str = ""


class CardStats(BaseModel):
    cards: int = 0
    checkable: int = 0
    unknown_entity: int = 0
    unresolved_date: int = 0
    actions: int = 0


class CardsResult(BaseModel):
    state: CardsState = "ok"
    run_id: str = ""
    dir: str = ""
    source: str = ""
    period: str = ""
    compare_period: str = ""
    entries: list[CardEntry] = Field(default_factory=list)
    cards: list[Card] = Field(default_factory=list)
    actions: list[CardAction] = Field(default_factory=list)
    future: list[CardFuture] = Field(default_factory=list)
    stats: CardStats = Field(default_factory=CardStats)
    dropped: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    message: str = ""
    model: str = ""


# ---------------------------------------------------------------- S3 数据对账

class IssueItem(BaseModel):
    """一条问题：数字对不上、方向反了、实体不存在、材料缺失。"""
    id: str = ""
    level: str = "信息"          # 阻断 / 提示 / 信息
    type: str = "number"         # number/direction/entity/missing/caliber/data_gap
    card: str = ""
    subject: str = ""
    minutes_said: str = ""
    raw: str = ""
    value: float | None = None
    diff: float | None = None
    question_hash: str = ""
    suggest: str = ""


class ResultItem(BaseModel):
    """一条已对上的结果，explain 是六项固定说明。"""
    card: str = ""
    subject: str = ""
    raw: str = ""
    explain: str = ""
    value: float | None = None
    question_hash: str = ""


class ReconcileStats(BaseModel):
    blocking: int = 0
    warn: int = 0
    info: int = 0
    checked: int = 0
    calls: int = 0
    ok: int = 0
    parse_failed: int = 0


class IssuesResult(BaseModel):
    week: str = ""
    run_id: str = ""
    issues: list[IssueItem] = Field(default_factory=list)
    results: list[ResultItem] = Field(default_factory=list)
    stats: ReconcileStats = Field(default_factory=ReconcileStats)
    coverage: list[dict] = Field(default_factory=list)
    message: str = ""
    state: str = "ok"


class LedgerEntry(BaseModel):
    key: str = ""
    card: str = ""
    value: float = 0.0
    unit: str = "元"
    display: str = ""
    spec: str = ""
    question_hash: str = ""
    code_hash: str = "无代码"
    status: str = "ok"
    verdict: str = "none"
    diff: float | None = None
    shape: str = "number"


class Ledger(BaseModel):
    entries: list[LedgerEntry] = Field(default_factory=list)


class CaliberNote(BaseModel):
    """人对某张卡片给的口径建议。写进 overrides.json，不覆盖工具判定。"""
    card: str = ""
    note: str = ""
    hint: str = ""
    by: str = ""
    at: str = ""
