"""S5 扩写：纪要骨架逐句保留，由 pandasai 做数据发现并补肉、出图、落盘。

分工（与前面各步一以贯之）：
- GLM / pandasai 只做「发现/取数/算数/选题」与「把发现写成补充句」；
- 骨架逐句进正文、台账键值、[[f:key]] 占位符、不变量校验、哈希与落盘全由代码做。

两条不可逾越的底线（不是格式规制）：
1. corrected.json 里非 future/blocked 的 items[].fixed 必须逐字出现在 draft.md，
   一字不改、不删、不重组；blocked 原句不写进正文，也不生成待核实段落；
   future 原句不进本周报告；
2. 正文里每个 [[f:key]] 都要能在 ledger.json 里找到，台账条目带 spec + code_hash。

业务数分工：摘要/区域/品类/前 5/门店由 pandasai 固定问句生成并执行 pandas 代码算出，
代码用源数据做参考值复核（容差 1 元），一致才采用 AI 口径；数据质量由代码取证；
最后 GLM 复核正文与数据质量，有疑问可调 pandasai 再查。

已去掉：规范.md 的硬性校验、固定六节模板、兜底稿、字数裁剪机、禁用词替换、
硬编码指标与固定两张图。图数、章节形态由发现结果决定。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from .llm import LLMError, chat  # noqa: E402
from . import s5_filters

DEFAULT_RUN_ROOT = PROJECT_ROOT / "runs"
SPEC_PATH = PROJECT_ROOT / "规范.md"
FIGURE_TOOLS = PROJECT_ROOT / "tools" / "figure"

STATUS_READY = "可扩写"
STATUS_DONE = "已生成"
STATUS_REVIEW = "待审核"
STATUS_REJECTED = "退回修改"
STATUS_NEED = "待补充"

CITY_REGION = {
    "北京": "华北", "天津": "华北", "石家庄": "华北", "太原": "华北", "呼和浩特": "华北",
    "上海": "华东", "杭州": "华东", "南京": "华东", "苏州": "华东", "宁波": "华东",
    "合肥": "华东", "济南": "华东", "青岛": "华东", "福州": "华东", "厦门": "华东",
    "广州": "华南", "深圳": "华南", "东莞": "华南", "佛山": "华南", "南宁": "华南", "海口": "华南",
    "成都": "西南", "重庆": "西南", "昆明": "西南", "贵阳": "西南", "拉萨": "西南",
    "武汉": "华中", "长沙": "华中", "郑州": "华中", "南昌": "华中",
    "西安": "西北", "兰州": "西北", "西宁": "西北", "银川": "西北", "乌鲁木齐": "西北",
    "沈阳": "东北", "大连": "东北", "长春": "东北", "哈尔滨": "东北",
}
CATEGORY_RULES = [
    (("生日蛋糕", "慕斯", "提拉米苏", "瑞士卷", "切块", "芝士蛋糕", "蛋糕"), "蛋糕"),
    (("吐司", "面包", "法棍", "可颂", "菠萝包", "贝果", "欧包"), "面包"),
    (("咖啡", "拿铁", "茶", "巧克力", "牛奶", "果汁", "气泡水"), "饮品"),
    (("曲奇", "马卡龙", "牛轧糖", "酥", "蛋挞", "泡芙", "脆片", "冻干", "饼干"), "西点"),
    (("礼盒",), "礼盒"),
]

_PLACEHOLDER_RE = re.compile(r"\[\[f:([^\]]+)\]\]")


@dataclass
class S5Outcome:
    ok: bool
    state: str
    message: str
    draft: str = ""
    stats: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    figures: list[dict] = field(default_factory=list)
    hotspots: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    run_id: str = ""
    dir: str = ""
    week: str = ""
    discoveries: list[dict] = field(default_factory=list)
    skeleton: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    questions_source: str = ""
    incremental: bool = False
    recomputed: int = 0
    reused: int = 0
    removed: int = 0
    review: dict = field(default_factory=dict)
    qc: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "state": self.state, "message": self.message,
            "draft": self.draft, "stats": self.stats, "manifest": self.manifest,
            "profile": self.profile, "figures": self.figures,
            "hotspots": self.hotspots, "log": self.log,
            "run_id": self.run_id, "dir": self.dir, "week": self.week,
            "discoveries": self.discoveries, "skeleton": self.skeleton,
            "questions": self.questions, "questions_source": self.questions_source,
            "incremental": self.incremental, "recomputed": self.recomputed,
            "reused": self.reused, "removed": self.removed,
            "review": self.review, "qc": self.qc,
        }


# ---------------------------------------------------------------- 基础工具

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _write_status(run_dir: Path, state: str, note: str) -> None:
    path = run_dir / "status.json"
    data = _read_json(path)
    at = _now()
    history = data.get("history") if isinstance(data.get("history"), list) else []
    history.append({"at": at, "state": state, "updated_by": "S5", "note": note})
    data["state"] = state
    data["updated_by"] = "S5"
    data["at"] = at
    data["note"] = note
    data["history"] = history[-50:]
    data["stage"] = "s5"
    _write_json(path, data)


def _archive_previous(run_dir: Path, log: list[str]) -> str:
    """把上一版 S5 产物归档到 history/<时间>/；没有旧稿返回空串。

    归档发生在任何写入之前：新一版失败时，旧稿仍可从 history 取回。
    """
    draft = run_dir / "draft.md"
    if not draft.is_file():
        return ""
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    root = run_dir / "history"
    target = root / stamp
    suffix = 1
    while target.exists():
        target = root / (stamp + "_" + str(suffix))
        suffix += 1
    target.mkdir(parents=True)
    for name in ("draft.md", "figures_manifest.json", "s5_facts.json",
                 "ledger.json", "status.json", "profile.json", "corrected.json"):
        src = run_dir / name
        if src.is_file():
            shutil.copy2(src, target / name)
    figures = run_dir / "figures"
    if figures.is_dir():
        shutil.copytree(figures, target / "figures")

    facts = _read_json(run_dir / "s5_facts.json")
    manifest = _read_json(run_dir / "figures_manifest.json")
    status = _read_json(run_dir / "status.json")
    meta = {
        "archived_at": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "stamp": target.name,
        "state": str(status.get("state") or ""),
        "note": str(status.get("note") or ""),
        "stats": facts.get("stats") if isinstance(facts.get("stats"), dict) else {},
        "figures": len(manifest.get("figures") or []),
    }
    _write_json(target / "meta.json", meta)

    # 归档里的清单要对应当前这版 draft：从 facts 还原，而不是复制刚改过的新清单。
    from . import s5_questions

    archived_questions = facts.get("questions")
    if not isinstance(archived_questions, list) or not archived_questions:
        archived_questions = s5_questions.questions_from_findings(
            facts.get("discoveries") or [])
    archived_questions = s5_questions.normalize_questions(
        archived_questions, strict=False)
    if archived_questions:
        _write_json(target / "s5_questions.json", {
            "run_id": str(facts.get("run_id") or ""),
            "updated_at": meta["archived_at"],
            "source": str(facts.get("questions_source") or "archived"),
            "questions": archived_questions,
        })
    log.append("上一版已归档：" + str(target))
    return str(target)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug(text: str, index: int = 0) -> str:
    ascii_part = re.sub(r"[^a-z0-9]+", "", str(text or "").lower())
    return ascii_part or ("x" + str(index))


def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    renamed = []
    for col in df.columns:
        name = str(col).replace("\ufeff", "").replace(" ", "").replace("\u3000", "").strip()
        renamed.append(name or "列")
    df.columns = renamed
    if df.columns.duplicated().any():
        merged = pd.DataFrame(index=df.index)
        for name in dict.fromkeys(df.columns):
            block = df.loc[:, df.columns == name]
            merged[name] = block.bfill(axis=1).iloc[:, 0] if block.shape[1] > 1 else block.iloc[:, 0]
        df = merged
    return df


def _read_table(path: Path) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """读一个数据文件；xlsx 返回主表与其余工作表。"""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls", ".xlsm"):
        book = pd.ExcelFile(path)
        try:
            sheets = {str(name): _norm_cols(pd.read_excel(book, sheet_name=name))
                      for name in book.sheet_names}
        finally:
            # Windows 下不显式关闭会一直占着文件句柄，后续无法删/覆盖运行目录
            book.close()
        main = None
        for name in ("销售明细", "明细", "数据"):
            if name in sheets:
                main = sheets[name]
                break
        if main is None:
            main = next((frame for frame in sheets.values() if "记录ID" in frame.columns), None)
        if main is None:
            main = next(iter(sheets.values()))
        return main, sheets
    last: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return _norm_cols(pd.read_csv(path, encoding=encoding)), {}
        except UnicodeDecodeError as exc:
            last = exc
    raise ValueError("无法解码 " + path.name + "：" + str(last))


def _locate_data_files(run_dir: Path, intake: dict) -> list[Path]:
    files: list[Path] = []
    for info in intake.get("files") or []:
        if not isinstance(info, dict) or info.get("role") != "数据":
            continue
        candidates = [
            run_dir / str(info.get("path") or ""),
            run_dir / "inputs" / str(info.get("name") or ""),
        ]
        for path in candidates:
            if path.is_file():
                files.append(path)
                break
    if not files:
        inputs = run_dir / "inputs"
        if inputs.is_dir():
            files = [p for p in inputs.iterdir() if p.suffix.lower() in (".csv", ".tsv", ".xlsx", ".xls", ".xlsm")]
    return files


def _city_region(store_name: str) -> str:
    name = str(store_name or "")
    for city, region in CITY_REGION.items():
        if city in name:
            return region
    return "其他"


def _category_of(product: str) -> str:
    name = str(product or "")
    for words, category in CATEGORY_RULES:
        if any(word in name for word in words):
            return category
    return "其他"


def _load_data(run_dir: Path, intake: dict) -> tuple[pd.DataFrame, dict]:
    files = _locate_data_files(run_dir, intake)
    if not files:
        raise FileNotFoundError("运行目录里找不到 S1 登记的数据文件：" + str(run_dir / "inputs"))
    frames: list[pd.DataFrame] = []
    extras: dict[str, pd.DataFrame] = {}
    for path in files:
        frame, sheets = _read_table(path)
        frame["_source"] = path.name
        frames.append(frame)
        for name, sheet in sheets.items():
            if sheet is not frame:
                extras.setdefault(name, sheet)
    df = pd.concat(frames, ignore_index=True, sort=False) if len(frames) > 1 else frames[0]
    for col in ("记录ID", "销售日期", "门店名称", "实收金额"):
        if col not in df.columns:
            raise FileNotFoundError("数据缺少必需列：" + col + "（"
                                   + "、".join(str(p) for p in files) + "）")

    raw_rows = len(df)
    df["_date"] = pd.to_datetime(df["销售日期"], errors="coerce")
    invalid_dates = int(df["_date"].isna().sum())
    duplicate_rows = df[df.duplicated("记录ID", keep="first")
                        & df["记录ID"].notna()].copy()
    df = df.drop_duplicates("记录ID", keep="first").copy()

    region_source = "源数据"
    if "区域" not in df.columns:
        merged = False
        for name, sheet in extras.items():
            if "区域" in sheet.columns and ("门店编码" in sheet.columns or "门店名称" in sheet.columns):
                keys = [key for key in ("门店编码", "门店名称") if key in df.columns and key in sheet.columns]
                if keys:
                    merge_cols = ["区域"] + (["店型"] if "店型" in sheet.columns else [])
                    df = df.merge(sheet[keys + merge_cols].drop_duplicates(keys),
                                  on=keys, how="left")
                    merged = True
                    region_source = "门店信息表"
                    break
        if not merged:
            df["区域"] = df["门店名称"].map(_city_region)
            region_source = "按门店城市推断"
    df["区域"] = df["区域"].fillna("其他").replace("", "其他")

    if "店型" not in df.columns:
        for name, sheet in extras.items():
            if "店型" in sheet.columns and ("门店编码" in sheet.columns or "门店名称" in sheet.columns):
                keys = [key for key in ("门店编码", "门店名称") if key in df.columns and key in sheet.columns]
                if keys:
                    df = df.merge(sheet[keys + ["店型"]].drop_duplicates(keys),
                                  on=keys, how="left")
                    break

    category_source = "源数据"
    if "品类" not in df.columns:
        merged = False
        for name, sheet in extras.items():
            if "品类" in sheet.columns and ("商品编码" in sheet.columns or "商品名称" in sheet.columns):
                keys = [key for key in ("商品编码", "商品名称") if key in df.columns and key in sheet.columns]
                if keys:
                    df = df.merge(sheet[keys + ["品类"]].drop_duplicates(keys), on=keys, how="left")
                    merged = True
                    category_source = "商品信息表"
                    break
        if not merged:
            df["品类"] = df["商品名称"].map(_category_of)
            category_source = "按商品名称归类"
    df["品类"] = df["品类"].fillna("其他").replace("", "其他")

    for col in ("实收金额", "成本金额", "销售数量", "目标金额"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    targets: dict[str, float] = {}
    target_source = ""
    minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
    period_start = str(minutes.get("period_start") or "")
    period_end = str(minutes.get("period_end") or "")
    for name, sheet in extras.items():
        if "区域" in sheet.columns and ("实收目标(元)" in sheet.columns or "目标金额" in sheet.columns):
            target_col = "实收目标(元)" if "实收目标(元)" in sheet.columns else "目标金额"
            for _, row in sheet.iterrows():
                if period_start and period_end and "周起始日" in sheet.columns:
                    try:
                        start = pd.to_datetime(row.get("周起始日"))
                        end = pd.to_datetime(row.get("周结束日"))
                        if not (start <= pd.Timestamp(period_start)
                                and end >= pd.Timestamp(period_end)):
                            continue
                    except (TypeError, ValueError):
                        pass
                region = str(row.get("区域") or "").strip()
                if region:
                    try:
                        targets[region] = float(row.get(target_col) or 0)
                    except (TypeError, ValueError):
                        continue
            if targets:
                target_source = name
                break
    if not targets and "目标金额" in df.columns:
        for region, group in df.groupby("区域"):
            value = pd.to_numeric(group["目标金额"], errors="coerce").sum()
            if pd.notna(value):
                targets[str(region)] = float(value)
        target_source = "数据列 目标金额" if targets else ""

    dup = len(duplicate_rows)
    meta = {
        "files": [path.name for path in files],
        "raw_rows": raw_rows,
        "duplicates": dup,
        "duplicate_rows": duplicate_rows,
        "invalid_dates": invalid_dates,
        "region_source": region_source,
        "category_source": category_source,
        "target_source": target_source,
        "targets": targets,
    }
    return df, meta


# ---------------------------------------------------------------- 格式化 / 周期

def _fmt_amount(value) -> str:
    return f"{float(value):,.0f}"


def _fmt_pct(value) -> str:
    return f"{float(value) * 100:.1f}%"


def _fmt_change(value) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:+.1f}%"


def _tok(key: str) -> str:
    return "[[f:" + key + "]]"


def _period_info(df: pd.DataFrame, intake: dict, tag: str) -> dict:
    """只取周期口径，不再算任何硬编码指标（指标全部来自 pandasai 发现）。"""
    minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
    week = str(minutes.get("week") or tag)
    start = str(minutes.get("period_start") or "")
    end = str(minutes.get("period_end") or "")
    prev_start = str(minutes.get("compare_start") or "")
    prev_end = str(minutes.get("compare_end") or "")
    if not (start and end):
        dates = df["_date"].dropna()
        if len(dates):
            end_dt = dates.max().normalize()
            start_dt = end_dt - pd.Timedelta(days=6)
            start, end = start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
            prev_end_dt = start_dt - pd.Timedelta(days=1)
            prev_start_dt = prev_end_dt - pd.Timedelta(days=6)
            prev_start, prev_end = prev_start_dt.strftime("%Y-%m-%d"), prev_end_dt.strftime("%Y-%m-%d")

    missing_days = 0
    if start and end and "_date" in df.columns:
        cur = df[(df["_date"] >= start) & (df["_date"] <= end)]
        present = {d.strftime("%Y-%m-%d") for d in cur["_date"].dropna()}
        days = pd.date_range(start=start, end=end, freq="D")
        missing_days = sum(1 for d in days if d.strftime("%Y-%m-%d") not in present)
    return {
        "week": week, "tag": tag,
        "start": start, "end": end,
        "compare_start": prev_start, "compare_end": prev_end,
        "period": (start + " 至 " + end).strip(" 至"),
        "compare": (prev_start + " 至 " + prev_end).strip(" 至"),
        "missing_days": max(0, missing_days),
    }


# ---------------------------------------------------------------- 业务硬要求

def _fmt_money(value) -> str:
    return f"{float(value):,.0f}"


def _fmt_rate(value) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:+.1f}%"


def _week_label(info: dict) -> str:
    week = str(info.get("week") or "")
    match = re.search(r"W(\d{1,2})", week)
    return "第" + str(int(match.group(1))) + "周" if match else week


def _match_reason(name: str, items: list[dict], week: str) -> str:
    if not name:
        return ""
    candidates = [name]
    if len(name) > 3 and name.endswith("店"):
        candidates.append(name[2:])
    for item in items:
        text = str(item.get("fixed") or item.get("original") or "")
        for candidate in candidates:
            if candidate and candidate in text:
                for sentence in re.split(r"[。！？!?]", text):
                    if candidate in sentence:
                        if any(word in sentence for word in
                               ("重复上传", "尚未处理", "正在核实", "疑似")):
                            continue
                        return sentence.strip()[:120] + f"（{week}纪要）"
    return ""


def _compact_reason(text: str) -> str:
    """把纪要长句压成一个可读的原因标签；压不了就截断。"""
    value = str(text or "").strip()
    if "复工" in value:
        return "复工回升"
    if any(word in value for word in ("节后", "假期", "国庆", "商场店、街边店", "社区店")):
        return "节后回落"
    if "收银系统故障" in value or "POS" in value:
        return "POS 故障"
    if "奶油" in value:
        return "奶油批次退货"
    if "消防" in value:
        return "消防改造"
    if "结束" in value and ("促销" in value or "第二杯半价" in value):
        return "活动结束回落"
    if "促销" in value or "第二杯半价" in value:
        return "华南促销"
    if "爬坡" in value:
        return "新店爬坡"
    if "竞品" in value or "开业" in value:
        return "竞品开业"
    return value[:60]


def _mentions_period(text: str, start: dt.date, end: dt.date) -> bool:
    """未来事项只有在日期落在本期时才拿来当原因。"""
    match = re.search(
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*[至到\-–]\s*"
        r"(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日", str(text or ""))
    if match:
        month1, day1, month2, day2 = match.groups()
        try:
            first = dt.date(start.year, int(month1), int(day1))
            second = dt.date(start.year, int(month2 or month1), int(day2))
        except ValueError:
            return False
        return not (second < start or first > end)
    match = re.search(
        r"(\d{4}-\d{2}-\d{2})\s*[至到\-–]\s*(\d{4}-\d{2}-\d{2})", str(text or ""))
    if match:
        try:
            first = dt.date.fromisoformat(match.group(1))
            second = dt.date.fromisoformat(match.group(2))
        except ValueError:
            return False
        return not (second < start or first > end)
    return False


def _inconsistency_clause(note: str, issue: str = "") -> str:
    """从 S4 note/issue 里取一句「与数据不一致」的可读说明。"""
    text = (str(note or "") + " " + str(issue or "")).strip()
    if "不一致" not in text and "不符" not in text:
        return ""
    clauses = [clause.strip() for clause in re.split(r"[；;。]", text)
               if "不一致" in clause or "不符" in clause]
    clause = clauses[0] if clauses else text
    clause = re.sub(r"^信息级[^；;]*；?", "", clause)
    clause = re.sub(r"正文不引用[^；;]*", "", clause)
    clause = re.sub(r"[，,]?建议后续核实[^；;。]*", "", clause)
    clause = re.sub(r"Q\d+", "", clause).strip("，,；;。 ")
    if "退货" in clause and ("1 笔" in clause or "台账退货" in clause):
        return ""
    return clause


def _quality_facts(frame: pd.DataFrame, meta: dict, info: dict,
                   corrected_items: list[dict]) -> dict:
    """数据质量取证结果；文本和 AI 复核共用同一份事实。"""
    start, end = info.get("start"), info.get("end")
    rev = "实收金额"
    facts: dict = {
        "duplicates": {"count": 0, "amount": 0.0, "pending": False},
        "returns": {"count": 0, "amount": 0.0},
        "missing": [],
        "invalid_dates": int(meta.get("invalid_dates") or 0),
    }
    if not start or not end or frame.empty or rev not in frame.columns:
        return facts
    cur = frame[(frame["_date"] >= pd.Timestamp(start))
                & (frame["_date"] <= pd.Timestamp(end))].copy()
    if cur.empty:
        return facts
    duplicate_rows = meta.get("duplicate_rows")
    if isinstance(duplicate_rows, pd.DataFrame) and not duplicate_rows.empty:
        duplicates = duplicate_rows[(duplicate_rows["_date"] >= pd.Timestamp(start))
                                    & (duplicate_rows["_date"] <= pd.Timestamp(end))]
        if not duplicates.empty:
            facts["duplicates"] = {
                "count": int(len(duplicates)),
                "amount": float(duplicates[rev].sum()),
                "pending": any(
                    word in str(item.get("fixed") or "") + str(item.get("original") or "")
                    for item in corrected_items
                    for word in ("尚未处理", "正在核实")
                ),
            }
    returns = (cur[(cur["销售数量"] < 0) | (cur[rev] < 0)]
               if "销售数量" in cur.columns else cur[cur[rev] < 0])
    if len(returns):
        facts["returns"] = {"count": int(len(returns)),
                            "amount": float(returns[rev].sum())}
    if "门店名称" in cur.columns:
        days = pd.date_range(start=start, end=end, freq="D")
        for store, group in cur.groupby("门店名称"):
            present = {date.normalize() for date in group["_date"].dropna()}
            if not present:
                continue
            low, high = min(present), max(present)
            missing = [date for date in days
                       if low <= date <= high and date.normalize() not in present]
            if not missing:
                continue
            week = _week_label(info)
            reason = _match_reason(str(store), corrected_items, week)
            if reason:
                reason = _compact_reason(re.sub(r"（第\d+周纪要）$", "", reason))
            facts["missing"].append({
                "store": str(store),
                "dates": [date.strftime("%m-%d") for date in missing],
                "reason": reason,
            })
    return facts


def _quality_lines(facts: dict) -> list[str]:
    lines: list[str] = []
    duplicates = facts.get("duplicates") or {}
    if duplicates.get("count"):
        line = (f"重复记录 {duplicates['count']} 条（实收合计 "
                f"{_fmt_money(duplicates.get('amount') or 0)} 元），"
                "已按记录ID保留第一条后计算")
        if duplicates.get("pending"):
            line += "；纪要所称「尚未处理」以本报告为准"
        lines.append(line)
    else:
        lines.append("未发现重复记录")
    returns = facts.get("returns") or {}
    if returns.get("count"):
        lines.append(f"退货 {returns['count']} 笔，合计 "
                     f"{_fmt_money(returns.get('amount') or 0)} 元，已按负数冲减")
    else:
        lines.append("本周无退货记录")
    missing = facts.get("missing") or []
    if missing:
        parts = []
        for item in missing:
            labels = "、".join(item.get("dates") or [])
            reason = str(item.get("reason") or "")
            hint = "（" + reason + "）" if reason else ""
            parts.append(f"{item.get('store')} {labels} 无系统数据{hint}，未补零")
        lines.append("；".join(parts))
    else:
        lines.append("未发现门店日期缺失")
    if facts.get("invalid_dates"):
        lines.append(f"无效日期 {int(facts['invalid_dates'])} 条，未计入")
    return lines


def _business_facts(frame: pd.DataFrame, meta: dict, info: dict,
                    corrected_items: list[dict]) -> dict:
    """按《周报格式要求》直接算正文必需内容，不交给模型自由发挥。"""
    start, end = info.get("start"), info.get("end")
    if not start or not end or frame.empty:
        return {}
    cur = frame[(frame["_date"] >= pd.Timestamp(start))
                & (frame["_date"] <= pd.Timestamp(end))].copy()
    if cur.empty:
        return {}
    compare_start = info.get("compare_start")
    compare_end = info.get("compare_end")
    prev = (frame[(frame["_date"] >= pd.Timestamp(compare_start))
                  & (frame["_date"] <= pd.Timestamp(compare_end))].copy()
            if compare_start and compare_end else frame.iloc[0:0].copy())
    rev = "实收金额"
    if rev not in cur.columns:
        return {}

    def change(current: float, previous: float | None) -> str:
        if previous is None or not previous:
            return "—"
        return _fmt_rate((current - previous) / previous)

    total = float(cur[rev].sum())
    prev_total = float(prev[rev].sum()) if not prev.empty else None
    cost = float(cur["成本金额"].sum()) if "成本金额" in cur.columns else None
    margin = (total - cost) / total * 100 if cost is not None and total else None
    targets = {str(key): float(value) for key, value in (meta.get("targets") or {}).items()
               if value}
    total_target = sum(targets.values())
    attainment = total / total_target * 100 if total_target else None

    categories: list[str] = []
    if "品类" in cur.columns:
        rows = []
        for name, group in cur.groupby("品类"):
            amount = float(group[rev].sum())
            previous = (float(prev[prev["品类"] == name][rev].sum())
                        if not prev.empty and "品类" in prev.columns else None)
            rows.append((str(name), amount, previous))
        rows.sort(key=lambda row: row[1], reverse=True)
        categories = [f"{name} {_fmt_money(amount)} 元（较上周 {change(amount, previous)}）"
                      for name, amount, previous in rows]

    top_products: list[str] = []
    if "商品名称" in cur.columns:
        top = cur.groupby("商品名称")[rev].sum().sort_values(ascending=False).head(5)
        top_products = [f"{name} {_fmt_money(value)} 元" for name, value in top.items()]

    week = _week_label(info)
    anomaly_short: list[str] = []
    try:
        start_date = dt.date.fromisoformat(start)
        end_date = dt.date.fromisoformat(end)
    except ValueError:
        start_date = end_date = None
    anomaly_items = [item for item in corrected_items
                     if str(item.get("status") or "") not in ("blocked", "future")]
    if start_date and end_date:
        anomaly_items += [
            item for item in corrected_items
            if str(item.get("status") or "") == "future"
            and _mentions_period(str(item.get("fixed") or item.get("original") or ""),
                                 start_date, end_date)
        ]

    def collect(column: str, scope: str) -> list[str]:
        if column not in cur.columns:
            return []
        rows = []
        for name, group in cur.groupby(column):
            amount = float(group[rev].sum())
            previous = (float(prev[prev[column] == name][rev].sum())
                        if not prev.empty and column in prev.columns else None)
            if previous and abs((amount - previous) / previous) > 0.10:
                rows.append((str(name), amount, previous,
                             (amount - previous) / previous))
        rows.sort(key=lambda row: abs(row[3]), reverse=True)
        lines: list[str] = []
        for name, amount, previous, rate in rows:
            reason = _match_reason(name, anomaly_items, week)
            if not reason and scope == "门店":
                same = cur[cur[column] == name]
                store_type = (str(same.iloc[0].get("店型") or "").strip()
                              if "店型" in same.columns and len(same) else "")
                if store_type:
                    reason = _match_reason(store_type, anomaly_items, week)
                if not reason and "区域" in same.columns and len(same):
                    reason = _match_reason(str(same.iloc[0].get("区域") or ""),
                                           anomaly_items, week)
            if not reason and scope == "区域":
                for keyword in ("商场店", "街边店", "社区店", "写字楼店"):
                    reason = _match_reason(keyword, anomaly_items, week)
                    if reason:
                        break
            if not reason:
                region = (str(cur[cur[column] == name].iloc[0].get("区域") or "")
                          if scope == "门店" and "区域" in cur.columns else "")
                if region and scope == "门店":
                    reason = _match_reason(region, anomaly_items, week)
            if reason:
                base = re.sub(r"（第\d+周纪要）$", "", reason)
                reason = _compact_reason(base) + f"，{week}纪要"
            reason = reason or "原因待核实"
            lines.append(f"{name} {_fmt_rate(rate)}（{reason}）")
            anomaly_short.append(f"{name}（{_fmt_rate(rate)}）")
        return lines

    anomalies: list[str] = []
    regions = collect("区域", "区域")
    stores = collect("门店名称", "门店")
    if regions:
        anomalies.append("区域：" + "；".join(regions))
    if stores:
        anomalies.append("门店：" + "；".join(stores))

    quality_facts = _quality_facts(frame, meta, info, corrected_items)
    quality = _quality_lines(quality_facts)

    inconsistencies: list[str] = []
    for item in corrected_items:
        clause = _inconsistency_clause(str(item.get("note") or ""),
                                       str(item.get("issue") or ""))
        if not clause:
            continue
        original = str(item.get("original") or item.get("fixed") or "").strip()
        if original:
            text = (f"纪要原句「{original[:80]}」与数据不一致：{clause}；"
                    "本报告以数据为准。")
        else:
            text = f"纪要说法与数据不一致：{clause}；本报告以数据为准。"
        inconsistencies.append(text)
        if len(inconsistencies) >= 5:
            break

    summary_parts = [f"全公司实收 {_fmt_money(total)} 元"]
    if prev_total:
        summary_parts.append(f"较上周 {_fmt_rate((total - prev_total) / prev_total)}")
    if margin is not None:
        summary_parts.append(f"毛利率 {margin:.1f}%")
    if attainment is not None:
        summary_parts.append(f"目标达成率 {attainment:.1f}%")
    summary = "本周" + "，".join(summary_parts) + "。"
    if anomaly_short:
        summary += "波动最大的是" + "、".join(anomaly_short[:2]) + "。"
    else:
        summary += "本周未发现环比超过 ±10% 的区域或门店。"

    return {
        "summary": summary,
        "categories": categories,
        "top_products": top_products,
        "anomalies": anomalies,
        "quality": quality,
        "quality_facts": quality_facts,
        "inconsistencies": inconsistencies,
    }


# ---------------------------------------------------------------- AI 出数 + 代码校验

def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).replace(",", "").replace("，", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _numbers_from_frame(frame: pd.DataFrame, meta: dict, info: dict) -> dict:
    """代码侧参考值：只用于复核 AI 出数，不直接决定正文措辞。"""
    start, end = info.get("start"), info.get("end")
    if not start or not end or frame.empty:
        return {}
    cur = frame[(frame["_date"] >= pd.Timestamp(start))
                & (frame["_date"] <= pd.Timestamp(end))].copy()
    if cur.empty or "实收金额" not in cur.columns:
        return {}
    prev = (frame[(frame["_date"] >= pd.Timestamp(info["compare_start"]))
                  & (frame["_date"] <= pd.Timestamp(info["compare_end"]))].copy()
            if info.get("compare_start") and info.get("compare_end")
            else frame.iloc[0:0].copy())
    rev = "实收金额"
    total = float(cur[rev].sum())
    prev_total = float(prev[rev].sum()) if not prev.empty and rev in prev.columns else None
    cost = float(cur["成本金额"].sum()) if "成本金额" in cur.columns else None
    targets = {str(key): float(value) for key, value in (meta.get("targets") or {}).items()
               if value}

    def section(column: str) -> list[dict]:
        if column not in cur.columns:
            return []
        rows = []
        for name, group in cur.groupby(column):
            amount = float(group[rev].sum())
            previous = (float(prev[prev[column] == name][rev].sum())
                        if not prev.empty and column in prev.columns else None)
            item = {"name": str(name), "amount": amount, "previous": previous}
            if column == "区域":
                item["target"] = targets.get(str(name))
            rows.append(item)
        rows.sort(key=lambda item: item["amount"], reverse=True)
        return rows

    products = [{"name": item["name"], "amount": item["amount"]}
                for item in section("商品名称")[:5]]
    return {
        "total": total, "prev_total": prev_total, "cost": cost,
        "total_target": sum(targets.values()) if targets else None,
        "regions": section("区域"),
        "categories": section("品类"),
        "products": products,
        "stores": section("门店名称"),
    }


def _row_text(row: dict, *needles: str) -> str:
    for key, value in row.items():
        if any(needle in str(key) for needle in needles):
            text = str(value or "").strip()
            if text and _as_float(text) is None:
                return text
    return ""


def _row_number(row: dict, *needles: str) -> float | None:
    for key, value in row.items():
        if any(needle in str(key) for needle in needles):
            number = _as_float(value)
            if number is not None:
                return number
    return None


def _is_total_name(name: str) -> bool:
    return not name or name in ("合计", "总计", "全公司", "全部") or "合计" in name


def _numbers_from_findings(findings: list[dict]) -> dict:
    """把 pandasai 的 BA/BB 两批发现映射成业务数；缺项返回空。"""
    by_id = {str(item.get("id")): item for item in findings or []
             if isinstance(item, dict)}
    if not by_id:
        return {}

    def number(fid: str) -> float | None:
        return _as_float((by_id.get(fid) or {}).get("value"))

    def rows(fid: str) -> list[dict]:
        value = (by_id.get(fid) or {}).get("rows")
        return [row for row in value or [] if isinstance(row, dict)]

    def table(fid: str, name_needles: tuple[str, ...],
              amount_needles: tuple[str, ...]) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in rows(fid):
            name = _row_text(row, *name_needles)
            if _is_total_name(name):
                continue
            amount = _row_number(row, *amount_needles)
            if amount is not None:
                out[name] = amount
        return out

    total = number("BA1")
    cost = number("BA2")
    prev_total = number("BB1")
    if total is None:
        return {}
    regions_now = table("BA3", ("区域",), ("本期",))
    regions_prev = table("BB2", ("区域",), ("上期",))
    targets = table("BA7", ("区域",), ("目标",))
    categories_now = table("BA4", ("品类",), ("本期",))
    categories_prev = table("BB3", ("品类",), ("上期",))
    products_now = table("BA5", ("商品",), ("本期",))
    stores_now = table("BA6", ("门店",), ("本期",))
    stores_prev = table("BB4", ("门店",), ("上期",))
    if not (regions_now and categories_now and products_now and stores_now):
        return {}

    def merge(current: dict[str, float], previous: dict[str, float],
              target: dict[str, float] | None = None) -> list[dict]:
        out = []
        for name, amount in current.items():
            item = {"name": name, "amount": amount,
                    "previous": previous.get(name)}
            if target is not None:
                item["target"] = target.get(name)
            out.append(item)
        return out

    regions = merge(regions_now, regions_prev, targets)
    categories = merge(categories_now, categories_prev)
    stores = merge(stores_now, stores_prev)
    products = [{"name": name, "amount": amount}
                for name, amount in sorted(products_now.items(),
                                           key=lambda pair: pair[1],
                                           reverse=True)[:5]]
    total_target = (sum(targets.values())
                    if targets and all(name in targets for name in regions_now) else None)
    return {
        "total": total, "prev_total": prev_total, "cost": cost,
        "total_target": total_target,
        "regions": regions, "categories": categories,
        "products": products, "stores": stores,
    }


def _numbers_close(ai: dict, ref: dict, tol: float = 1.0) -> bool:
    """AI 出数必须与代码参考值在 1 元内一致，否则不采用 AI 口径。"""
    if not ai or not ref:
        return False
    for key in ("total", "prev_total", "cost", "total_target"):
        left, right = ai.get(key), ref.get(key)
        if right is None:
            continue
        if left is None or abs(float(left) - float(right)) > tol:
            return False
    for section in ("regions", "categories", "products", "stores"):
        ai_rows = {item["name"]: item for item in ai.get(section) or []
                   if isinstance(item, dict) and item.get("name")}
        ref_rows = {item["name"]: item for item in ref.get(section) or []
                    if isinstance(item, dict) and item.get("name")}
        if set(ai_rows) != set(ref_rows):
            return False
        for name, ref_item in ref_rows.items():
            ai_item = ai_rows[name]
            for key in ("amount", "previous", "target"):
                right = ref_item.get(key)
                if right is None:
                    continue
                left = ai_item.get(key)
                if left is None or abs(float(left) - float(right)) > tol:
                    return False
    return True


def _with_target_column(frame: pd.DataFrame, meta: dict) -> pd.DataFrame:
    targets = {str(key): float(value) for key, value in (meta.get("targets") or {}).items()
               if value}
    if not targets or "区域" not in frame.columns:
        return frame
    data = frame.copy()
    data["_目标金额"] = data["区域"].map(targets)
    return data


def _business_question_batches(info: dict) -> list[list[dict]]:
    start, end = info.get("start"), info.get("end")
    compare_start, compare_end = info.get("compare_start"), info.get("compare_end")
    current = [
        {"id": "BA1", "shape": "number",
         "question": f"筛选销售日期在 {start} 至 {end} 的记录，对实收金额求和，只输出一个数字。",
         "reason": "全公司本期实收"},
        {"id": "BA2", "shape": "number",
         "question": f"筛选销售日期在 {start} 至 {end} 的记录，对成本金额求和，只输出一个数字。",
         "reason": "全公司本期成本"},
        {"id": "BA3", "shape": "table",
         "question": (f"筛选销售日期在 {start} 至 {end} 的记录，按区域分组，"
                      "对实收金额求和，返回两列：区域、本期实收。"),
         "reason": "区域本期实收"},
        {"id": "BA4", "shape": "table",
         "question": (f"筛选销售日期在 {start} 至 {end} 的记录，按品类分组，"
                      "对实收金额求和，返回两列：品类、本期实收。"),
         "reason": "品类本期实收"},
        {"id": "BA5", "shape": "table",
         "question": (f"筛选销售日期在 {start} 至 {end} 的记录，按商品名称分组，"
                      "对实收金额求和并降序取前 5，返回两列：商品名称、本期实收。"),
         "reason": "实收前 5 单品"},
        {"id": "BA6", "shape": "table",
         "question": (f"筛选销售日期在 {start} 至 {end} 的记录，按门店名称分组，"
                      "对实收金额求和，返回两列：门店名称、本期实收。"),
         "reason": "门店本期实收"},
        {"id": "BA7", "shape": "table",
         "question": (f"筛选销售日期在 {start} 至 {end} 的记录，按区域分组，"
                      "返回两列：区域、目标金额；目标金额取 _目标金额 列每个区域的一个值。"),
         "reason": "区域目标金额"},
    ]
    previous = [
        {"id": "BB1", "shape": "number",
         "question": f"筛选销售日期在 {compare_start} 至 {compare_end} 的记录，对实收金额求和，只输出一个数字。",
         "reason": "全公司上期实收"},
        {"id": "BB2", "shape": "table",
         "question": (f"筛选销售日期在 {compare_start} 至 {compare_end} 的记录，按区域分组，"
                      "对实收金额求和，返回两列：区域、上期实收。"),
         "reason": "区域上期实收"},
        {"id": "BB3", "shape": "table",
         "question": (f"筛选销售日期在 {compare_start} 至 {compare_end} 的记录，按品类分组，"
                      "对实收金额求和，返回两列：品类、上期实收。"),
         "reason": "品类上期实收"},
        {"id": "BB4", "shape": "table",
         "question": (f"筛选销售日期在 {compare_start} 至 {compare_end} 的记录，按门店名称分组，"
                      "对实收金额求和，返回两列：门店名称、上期实收。"),
         "reason": "门店上期实收"},
    ]
    return [current, previous]


def _discover_business(run_dir: Path, frame: pd.DataFrame, intake: dict, profile: dict,
                       info: dict, log: list[str]) -> dict:
    if os.environ.get("S5_AI_BUSINESS", "1").lower() in ("0", "false", "no"):
        log.append("S5_AI_BUSINESS=0，跳过 AI 业务取数")
        return {}
    try:
        from . import s5_discover

        by_id: dict[str, dict] = {}
        for batch in _business_question_batches(info):
            pending = list(batch)
            for attempt in range(2):
                if not pending:
                    break
                result = s5_discover.discover(run_dir, frame, intake, profile, [],
                                              questions=pending)
                found = [item for item in (result.get("findings") or [])
                         if isinstance(item, dict) and item.get("id")]
                for item in found:
                    by_id[str(item["id"])] = item
                log.extend(str(line) for line in (result.get("log") or []))
                if found:
                    log.append("AI 业务取数第 " + str(attempt + 1) + " 次："
                               + str(len(found)) + "/" + str(len(pending)) + " 条")
                got = {str(item["id"]) for item in found}
                pending = [question for question in pending
                           if str(question.get("id")) not in got]
        findings = list(by_id.values())
        numbers = _numbers_from_findings(findings)
        log.append("AI 业务取数：" + str(len(findings)) + " 条，"
                   + ("可用" if numbers else "不可用"))
        return numbers
    except Exception as exc:  # noqa: BLE001 - AI 取数失败由代码复核兜底
        log.append("AI 业务取数失败：" + str(exc)[:160])
        return {}


_PANDASAI_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_pandasai",
        "description": ("对源数据再问一个单步问题，由 pandasai 生成并执行 pandas 代码。"
                        "只在复核时对数据质量或正文数字有疑问时调用。"),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "单步可答的问题"},
                "shape": {"type": "string", "enum": ["number", "table"],
                          "description": "number=一个数；table=分组/多行"},
            },
            "required": ["question", "shape"],
        },
    },
}


def _pandasai_review_query(run_dir: str | Path, frame: pd.DataFrame, intake: dict,
                           profile: dict, question: str, shape: str,
                           log: list[str]) -> dict:
    """复核工具：允许数据质量问句，直接交给 pandasai 取数。"""
    question = str(question or "").strip()[:400]
    shape = shape if shape in ("number", "table") else "table"
    if not question:
        return {"ok": False, "error": "问题为空"}
    try:
        from . import s5_discover

        result = s5_discover.discover(
            run_dir, frame, intake, profile, [],
            questions=[{"id": "R1", "question": question, "shape": shape}],
            keep_quality=True)
        findings = [item for item in (result.get("findings") or [])
                    if isinstance(item, dict)]
        log.extend(str(line) for line in (result.get("log") or []))
        if not findings:
            return {"ok": False, "error": "pandasai 没有返回结果"}
        finding = findings[0]
        rows = finding.get("rows")
        if isinstance(rows, list):
            rows = rows[:50]
        return {
            "ok": True, "question": question, "shape": shape,
            "value": finding.get("value"), "display": finding.get("display"),
            "rows": rows, "code_hash": finding.get("code_hash") or "",
        }
    except Exception as exc:  # noqa: BLE001 - 复核查询失败如实返回
        return {"ok": False, "error": type(exc).__name__ + "：" + str(exc)[:160]}


def _s1_waiver_lines(intake: dict) -> list[str]:
    """把 S1 人工豁免转成复核提示，避免后面阶段重新判成缺口。"""
    lines: list[str] = []
    for waiver in intake.get("waived") or []:
        if not isinstance(waiver, dict):
            continue
        reason = str(waiver.get("reason") or "").strip()
        for item in waiver.get("items") or []:
            text = str(item or "").strip()
            if not text or text == "人工放权":
                continue
            line = "S1 人工豁免：" + text
            if reason:
                line += "（用户理由：" + reason + "）"
            lines.append(line)
    return lines


def _ai_review_draft(text: str, business: dict, qc: dict, info: dict,
                     run_dir: str | Path, frame: pd.DataFrame, intake: dict,
                     profile: dict, log: list[str], chat_fn=None,
                     filtered: dict | None = None, has_target: bool = True,
                     skeleton: list[dict] | None = None,
                     waivers: list[str] | None = None,
                     waive_issues: list[dict] | None = None) -> dict:
    """代码质检后让 GLM 复核；不满意可调用 ask_pandasai 再查。"""
    try:
        if chat_fn is None:
            from .llm import chat as chat_fn
    except Exception as exc:  # noqa: BLE001 - 没有模型就跳过复核
        return {"ok": True, "skipped": True, "reason": "模型不可用：" + str(exc)[:80],
                "queries": []}
    target_rule = ("1) 摘要是否含总实收、较上周、毛利率、目标达成；\n"
                   if has_target else
                   "1) 摘要是否含总实收、较上周、毛利率；本周期没有销售目标数据，"
                   "不要要求目标达成；\n")
    skeleton_lines = [
        "- " + str(item.get("id") or "") + " " + str(item.get("fixed") or "")
        for item in (skeleton or []) if str(item.get("fixed") or "").strip()
    ]
    waiver_lines = [str(item).strip() for item in (waivers or []) if str(item).strip()]
    waive_issue_lines = []
    for item in waive_issues or []:
        match = str(item.get("match") or "").strip()
        if not match:
            continue
        reason = str(item.get("reason") or "").strip()
        waive_issue_lines.append(match + ("（用户理由：" + reason + "）" if reason else ""))
    prompt = (
        "你是经营周报的复核人。代码已经完成自动质检，请你再看一遍正文。\n"
        "只核对以下事项，不重算数字、不改写正文：\n"
        + target_rule +
        "2) 品类表现、实收前 5 单品、±10% 异常、数据质量是否齐全；\n"
        "3) 正文数字与业务事实是否自洽；\n"
        "4) 原因是否有纪要依据，没依据的写「原因待核实」；"
        "骨架原句本身就是纪要原文，不要以「无出处 / 缺引用」判不通过；\n"
        "5) 数据质量的重复/退货/缺日数字是否可信；\n"
        "6) 是否出现数据不支持的说法（客单价、同比、估算值等）；"
        "骨架原句里的估算或模糊说法属于纪要原文，不要据此判不通过，"
        "最多写进 suggestions 提示上游改纪要。\n"
        "如果你对某个数字或数据质量结果有疑问，可以调用 ask_pandasai 再查一次；"
        "最多查 3 次。查完请给结论。\n"
        "只输出 JSON：{\"ok\": true/false, \"issues\": [\"...\"], \"suggestions\": [\"...\"]}。\n\n"
        + (("纪要骨架原句（逐字来自纪要，视为有来源，不要要求补出处）：\n"
            + "\n".join(skeleton_lines[:80]) + "\n\n") if skeleton_lines else "")
        + (("用户已确认、不得再作为不通过理由的事项：\n"
            + "\n".join("- " + line for line in waiver_lines) + "\n\n") if waiver_lines else "")
        + (("用户已逐条确认以下复核问题为误判；只豁免这些条目，"
            "未被列出的问题仍按原规则判断，不要整体放行：\n"
            + "\n".join("- " + line for line in waive_issue_lines) + "\n\n")
           if waive_issue_lines else "")
        + "业务事实：\n" + json.dumps(business, ensure_ascii=False) + "\n\n"
        "被正文过滤掉的数据发现（仅供复核，不要要求写进正文）：\n"
        + json.dumps(filtered or {}, ensure_ascii=False)[:5000] + "\n\n"
        "代码质检：\n" + json.dumps(qc.get("checks") or [], ensure_ascii=False) + "\n\n"
        "正文：\n" + text[:12000]
    )
    messages = [
        {"role": "system", "content": "你是严谨的经营周报复核人，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    queries: list[dict] = []
    for _ in range(3):
        try:
            out = chat_fn(messages, temperature=0, max_tokens=2000,
                          tools=[_PANDASAI_REVIEW_TOOL],
                          response_format={"type": "json_object"},
                          thinking="disabled", timeout=180)
        except TypeError:  # 测试桩可能不收 tools / response_format
            try:
                out = chat_fn(messages, max_tokens=2000)
            except Exception as exc:  # noqa: BLE001
                return {"ok": True, "skipped": True,
                        "reason": "复核调用失败：" + str(exc)[:80], "queries": queries}
        except Exception as exc:  # noqa: BLE001
            return {"ok": True, "skipped": True,
                    "reason": "复核调用失败：" + str(exc)[:80], "queries": queries}
        calls = out.get("tool_calls") if isinstance(out, dict) else None
        if not calls:
            if isinstance(out, dict):
                raw = str(out.get("text") or (out.get("message") or {}).get("content") or "")
            else:
                raw = str(out or "")
            try:
                data = _extract_json(raw)
            except Exception as exc:  # noqa: BLE001
                log.append("AI 复核原文：" + raw[:160])
                return {"ok": True, "skipped": True,
                        "reason": "复核返回不可解析：" + str(exc)[:80], "queries": queries}
            if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
                return {"ok": True, "skipped": True,
                        "reason": "复核未返回明确结论", "queries": queries}
            return {
                "ok": bool(data.get("ok")),
                "issues": [str(item) for item in (data.get("issues") or [])][:8],
                "suggestions": [str(item) for item in (data.get("suggestions") or [])][:8],
                "queries": queries,
            }
        message = out.get("message") or {}
        messages.append({"role": "assistant",
                         "content": message.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name != "ask_pandasai":
                result = {"ok": False, "error": "不开放的工具：" + name}
            else:
                result = _pandasai_review_query(
                    run_dir, frame, intake, profile,
                    str(args.get("question") or ""), str(args.get("shape") or "table"), log)
            queries.append({"request": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.get("id") or "",
                             "content": json.dumps(result, ensure_ascii=False)})
    return {"ok": False, "issues": ["复核轮次用完，未给出结论"],
            "suggestions": [], "queries": queries}


# ---------------------------------------------------------------- 画像

def _profile(frame: pd.DataFrame, meta: dict, info: dict) -> dict:
    from tools.figure.profile_data import profile_data

    group_col = "门店编码" if "门店编码" in frame.columns else (
        "区域" if "区域" in frame.columns else None)
    try:
        raw = profile_data(frame, [group_col] if group_col else None)
    except Exception as exc:  # noqa: BLE001 - 画像失败不影响正文，但要落盘说明
        return {"ok": False, "error": str(exc), "warnings": ["画像失败：" + str(exc)]}

    dates = frame["_date"].dropna()
    coverage = {"start": dates.min().strftime("%Y-%m-%d") if len(dates) else "",
                "end": dates.max().strftime("%Y-%m-%d") if len(dates) else "",
                "period_start": info["start"], "period_end": info["end"],
                "missing_days": info["missing_days"]}
    groups = []
    if group_col:
        for name, one in frame.groupby(group_col):
            groups.append({"group": str(name), "n": int(len(one))})
        groups.sort(key=lambda item: item["n"], reverse=True)
        if groups and groups[0]["n"] and groups[-1]["n"]:
            ratio = groups[0]["n"] / groups[-1]["n"]
            coverage["group_ratio"] = round(ratio, 2)
            coverage["group_comparable"] = ratio < 3.0
    raw["coverage"] = coverage
    raw["ok"] = True
    raw["meta"] = {key: meta.get(key) for key in
                   ("region_source", "category_source", "target_source")}
    return raw


# ---------------------------------------------------------------- 发现 → 台账 / 图

def _norm_label(value) -> str:
    """表格型发现里带空格的脏分组名（如「广 州天河店」）在此规范化。"""
    return re.sub(r"\s+", "", str(value or ""))


def _map_label(label: str, mapping: dict | None) -> str:
    """编码 → 名称（SZ01 → 深圳南山店）；改名只影响展示，不改数值。"""
    text = str(label or "")
    if not mapping or not text:
        return text
    if text in mapping:
        return mapping[text]
    parts = [part.strip() for part in text.split(" / ")]
    mapped = [mapping.get(part, part) for part in parts]
    return " / ".join(mapped) if mapped != parts else text


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _table_columns(rows: list[dict]) -> tuple[list[str], list[str]]:
    """数值列 / 分组列，顺序与 s5_discover 的 keys 映射约定一致。"""
    if not rows or not isinstance(rows[0], dict):
        return [], []
    cols = list(rows[0].keys())
    measures = [c for c in cols if all(_to_number(r.get(c)) is not None for r in rows)]
    groups = [c for c in cols if c not in measures]
    return measures, groups


def _finding_points(finding: dict, label_map: dict | None = None) -> list[tuple[str, float, str]]:
    """抽 (规范化分组名, 数值, 列名)，顺序与 keys 对齐：keys[i] ↔ rows[i//M][measures[i%M]]。"""
    points: list[tuple[str, float, str]] = []
    rows = finding.get("rows")
    if str(finding.get("shape") or "") == "table" and isinstance(rows, list) and rows:
        measures, groups = _table_columns(rows)
        for i, row in enumerate(rows):
            label = " / ".join(_norm_label(row.get(g, "")) for g in groups) or str(i + 1)
            label = _map_label(label, label_map)
            for col in measures:
                value = _to_number(row.get(col))
                if value is not None:
                    points.append((label, value, str(col)))
    else:
        value = _to_number(finding.get("value"))
        if value is not None:
            points.append(("", value, ""))
    return points


def _display_value(value, finding: dict, col: str = "") -> tuple[str, str]:
    hint = (str(col) + " " + str(finding.get("display") or "") + " "
            + str(finding.get("question") or ""))
    pct = "%" in hint or any(word in hint for word in ("率", "占比"))
    if pct:
        if abs(float(value)) <= 1.5:
            return _fmt_pct(value), "%"
        return f"{float(value):.1f}%", "%"
    if any(word in hint for word in ("数量", "销量", "件数")):
        return f"{float(value):,.0f}", "件"
    if any(word in hint for word in ("条数", "记录数", "天数")):
        return f"{float(value):,.0f}", "条"
    if "笔数" in hint:
        return f"{float(value):,.0f}", "笔"
    return _fmt_amount(value), "元"


def _stable_key(fid: str, idx: int, label: str, tag: str) -> str:
    return "find_" + _slug(fid, idx) + "_" + str(idx) + "_" + _slug(label, idx) + "_" + tag


_TIME_HINT = re.compile(r"\d{4}[-/年]|\d{1,2}\s*月\s*\d{1,2}\s*日|周[一二三四五六日]|季度|Q[1-4]")


def _looks_like_time(labels: list[str]) -> bool:
    """按分组名判断是不是时间序列（日期 / 周次 / 月份），不靠关键词猜图型。"""
    if len(labels) < 2:
        return False
    hits = sum(1 for item in labels if _TIME_HINT.search(str(item)))
    return hits >= max(2, len(labels) // 2)


def _short_label(text: str, limit: int = 14) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _conclusion_title(labels: list[str], values: list[float], kind: str,
                      metric: str = "") -> str:
    """结论式短标题：谁 + 指标 + 方向。时间序列写区间走势，分类写最高/最低；并列要写清。"""
    if not labels or not values:
        return "数据发现"
    if kind == "line":
        return _short_label(labels[0]) + "—" + _short_label(labels[-1]) + "走势"
    prefix = _short_label(metric, 10) + "：" if str(metric or "").strip() else ""
    if kind == "share" and len(labels) == 2:
        total = sum(values)
        if total:
            return (prefix + _short_label(labels[0]) + " " + f"{values[0] / total:.1%}"
                    + "，" + _short_label(labels[1]) + " " + f"{values[1] / total:.1%}")
    top = max(values)
    bottom = min(values)
    if top == bottom:
        return prefix + "各分组持平"

    def _side(value: float) -> str:
        names = [_short_label(label) for label, val in zip(labels, values) if val == value]
        return names[0] if len(names) == 1 else names[0] + " 等 " + str(len(names)) + " 个"

    return prefix + _side(top) + "最高，" + _side(bottom) + "最低"


_CHART_SKIP_WORDS = ("记录数", "记录条数", "负数量", "缺失值", "重复记录",
                     "异常记录", "天数", "行数")


def _skip_chart_metric(finding: dict) -> bool:
    """数据质量盘点（记录数、异常数）不是经营结论，别画成排名图。"""
    text = (str(finding.get("question") or "") + " "
            + str(finding.get("reason") or "") + " "
            + str(finding.get("spec") or ""))
    return any(word in text for word in _CHART_SKIP_WORDS)


def _worth_share(values: list[float]) -> bool:
    """两类构成：极端 99:1 画出来只剩一条边，不如写一句话。"""
    if len(values) != 2:
        return False
    total = sum(values)
    if total <= 0:
        return False
    small = min(values) / total
    return 0.03 <= small <= 0.97


def _worth_chart(finding: dict, values: list[float]) -> bool:
    """只有差距看得出来的排名才值得画；几乎持平的十条柱子没有信息。"""
    if len(values) < 3 or _skip_chart_metric(finding):
        return False
    high, low = max(values), min(values)
    if high <= 0:
        return False
    return (high - low) / high >= 0.12


def _chart_kind(finding: dict, labels: list[str], values: list[float]) -> str:
    """按数据形态定图型：时间→折线；两类构成→占比条；分类多/名字长→横向柱；其余→竖向柱。"""
    if _looks_like_time(labels):
        return "line"
    text = str(finding.get("question") or "") + " " + str(finding.get("reason") or "")
    if any(word in text for word in ("趋势", "走势", "逐日", "每日", "按日", "时序")):
        return "line"
    if (len(labels) == 2 and min(values) > 0
            and not _skip_chart_metric(finding)):
        return "share"
    if len(labels) > 8 or max((len(str(x)) for x in labels), default=0) > 8:
        return "barh"
    return "bar"


def _code_name_map(frame: pd.DataFrame) -> dict:
    """从数据里取编码→名称（门店/商品），一张表内名称唯一才映射，避免张冠李戴。"""
    mapping: dict[str, str] = {}
    for code_col, name_col in (("门店编码", "门店名称"), ("商品编码", "商品名称")):
        if code_col not in frame.columns or name_col not in frame.columns:
            continue
        pairs = frame[[code_col, name_col]].dropna().astype(str)
        pairs = pairs[pairs[code_col].str.strip() != ""]
        for code, group in pairs.groupby(code_col):
            names = {name.strip() for name in group[name_col] if name.strip()}
            if len(names) == 1:
                name = names.pop()
                if name and name != code:
                    mapping[str(code).strip()] = name
    return mapping


def _prepare_findings(findings: list[dict], info: dict,
                      label_map: dict | None = None) -> tuple[list[dict], dict, list[dict]]:
    """发现 → (ledger 条目, id→键名映射, 可画的图候选)。无可用的数值不落台账也不成图。"""
    entries: list[dict] = []
    key_map: dict[str, list[str]] = {}
    figure_specs: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if s5_filters.is_quality_finding(finding):
            continue
        fid = str(finding.get("id") or "D0")
        question = str(finding.get("question") or "")
        spec_text = str(finding.get("spec") or "")
        reason = str(finding.get("reason") or "")
        shape = str(finding.get("shape") or "")
        code = str(finding.get("code") or "")
        code_hash = str(finding.get("code_hash") or "").strip()
        flags = [str(flag) for flag in (finding.get("flags") or [])]
        if not code_hash:
            code_hash = _sha256_text(code)[:16] if code else "无代码"
        if code_hash == "无代码" and "无代码" not in flags:
            flags.append("无代码")
        keys = [str(key) for key in (finding.get("keys") or []) if key]
        points = _finding_points(finding, label_map)
        if not points:
            continue

        assigned: list[str] = []
        ledger_points: list[dict] = []
        for idx, (label, value, col) in enumerate(points):
            key = keys[idx] if idx < len(keys) else _stable_key(fid, idx, label, info["tag"])
            display, unit = _display_value(value, finding, col)
            spec = ("S5 数据发现（pandasai 生成代码取数）；问句：" + question
                    + "；口径：" + (spec_text or "按生成代码取数")
                    + "；对象：" + (label or "全表"))
            entries.append({
                "key": key, "card": "", "value": value, "unit": unit,
                "display": display, "spec": spec,
                "question_hash": "", "code_hash": code_hash,
                "status": "ok", "verdict": "info", "shape": "number",
                "added_by": "S5", "question": question, "code": code,
                "flags": flags, "label": label, "column": col,
            })
            assigned.append(key)
            ledger_points.append({"key": key, "value": value,
                                  "display": display, "label": label,
                                  "unit": unit})
        key_map[fid] = assigned

        # 一张图只回答一个问题：取每个分组的首个数值列，至少两个点才画
        rows = finding.get("rows") if shape == "table" else None
        if isinstance(rows, list) and len(rows) >= 2:
            measures, groups = _table_columns(rows)
            if measures:
                step = len(measures)
                fig_keys, fig_labels, fig_points = [], [], []
                for i, row in enumerate(rows):
                    pos = i * step
                    if pos >= len(assigned):
                        break
                    label = " / ".join(_norm_label(row.get(g, "")) for g in groups) or str(i + 1)
                    label = _map_label(label, label_map)
                    fig_labels.append(label)
                    fig_keys.append(assigned[pos])
                    fig_points.append(ledger_points[pos])
                if len(fig_keys) >= 2:
                    values = [float(p.get("value") or 0) for p in fig_points]
                    unit = str(fig_points[0].get("unit") or "元") if fig_points else "元"
                    kind = _chart_kind(finding, fig_labels, values)
                    if kind == "share" and unit == "%":
                        kind = "bar"
                    if kind == "share":
                        if not _worth_share(values):
                            continue
                    elif not _worth_chart(finding, values):
                        continue
                    if kind not in ("line", "share"):
                        # 排名类图按数值降序，读者一眼看到头部
                        order = sorted(range(len(values)), key=lambda i: values[i],
                                       reverse=True)
                        fig_keys = [fig_keys[i] for i in order]
                        fig_labels = [fig_labels[i] for i in order]
                        fig_points = [fig_points[i] for i in order]
                        values = [values[i] for i in order]
                        if len(fig_keys) > 10:
                            fig_keys, fig_labels, fig_points = (fig_keys[:10], fig_labels[:10],
                                                                fig_points[:10])
                            values = values[:10]
                    title = _conclusion_title(fig_labels, values, kind, measures[0])
                    figure_specs.append({
                        "finding_id": fid, "title": title, "question": question,
                        "kind": kind, "unit": unit, "labels": fig_labels,
                        "keys": fig_keys, "points": fig_points,
                    })
    return entries, key_map, figure_specs


def _merge_ledger(ledger_old: dict, entries: list[dict], info: dict) -> dict:
    """保留 S3 原有条目，按 key 覆盖 S5 发现条目（重跑不重复）。"""
    result = [dict(item) for item in (ledger_old.get("entries") or []) if isinstance(item, dict)]
    index = {str(item.get("key")): i for i, item in enumerate(result)}
    for entry in entries:
        key = str(entry["key"])
        if key in index:
            result[index[key]] = entry
        else:
            index[key] = len(result)
            result.append(entry)
    return {"week": info["week"], "period": info["period"],
            "compare": info["compare"], "entries": result}


# ---------------------------------------------------------------- 图表（pandasai 渲染）

def _make_figures(run_dir: Path, ctx: dict, log: list[str], renderer=None) -> dict:
    """按 table 型发现出图，默认用 Altair 固定模板；没有可画发现就 0 图。"""
    from . import s5_altair

    renderer = renderer or s5_altair.render_chart
    fig_dir = run_dir / "figures"
    src_dir = fig_dir / "src"
    work_root = fig_dir / ".pandasai"
    for folder in (fig_dir, src_dir, work_root):
        folder.mkdir(parents=True, exist_ok=True)

    def _rel(path: Path) -> str:
        return str(path.relative_to(run_dir)).replace("\\", "/")

    manifest = {"run_id": ctx["tag"], "week": ctx["week"],
                "period": ctx["period"], "compare": ctx["compare"],
                "renderer": "altair", "renderer_version": "",
                "figures": []}
    for cand in ctx["figure_specs"]:
        number = len(manifest["figures"]) + 1
        name = "fig" + f"{number:02d}"
        title = cand["title"]
        unit_label = str(cand.get("unit") or "元")
        caption = ("图 " + str(number) + "　" + title + "（" + ctx["period"]
                   + "，单位：" + unit_label + "，比较基准：" + ctx["compare"] + "）")
        spec = dict(cand)
        spec.update({"number": number, "name": name, "caption": caption})

        try:
            result = renderer(
                spec,
                out_png=fig_dir / (name + ".png"),
                script_path=src_dir / (name + ".py"),
                work_dir=work_root / name,
            )
        except Exception as exc:  # noqa: BLE001 - 单图失败不拖垮整步
            log.append("出图 " + name + " 失败：" + str(exc))
            continue

        if not getattr(result, "ok", False):
            log.append("出图 " + name + " 失败："
                       + (str(getattr(result, "error", "") or "渲染器未返回可用图")))
            continue

        png = fig_dir / (name + ".png")
        script = Path(getattr(result, "script_path", "") or (src_dir / (name + ".py")))
        if not png.is_file() or not script.is_file():
            log.append("出图 " + name + " 失败：PNG 或生成脚本未落盘")
            continue

        files = {"png": _rel(png)}
        svg_path = Path(getattr(result, "svg_path", "") or "")
        if svg_path and svg_path.is_file():
            files["svg"] = _rel(svg_path)
        output_sha = {"png": _sha256_file(png)}
        if "svg" in files:
            output_sha["svg"] = _sha256_file(run_dir / files["svg"])

        manifest["figures"].append({
            "number": number, "name": name, "title": title, "caption": caption,
            "question": cand["question"], "finding_id": cand["finding_id"],
            "shape": cand["kind"], "keys": cand["keys"], "points": cand["points"],
            "labels": cand["labels"],
            "renderer": getattr(result, "renderer", "altair"),
            "model": getattr(result, "model", ""),
            "prompt_sha256": getattr(result, "prompt_sha256", ""),
            "code_sha256": getattr(result, "code_sha256", ""),
            "files": files,
            "script": _rel(script),
            "script_sha256": _sha256_file(script),
            "output_sha256": output_sha,
            "quality": getattr(result, "quality", {}) or {},
            "unit_scale": getattr(result, "unit_scale", 1.0),
            "plot_values": getattr(result, "plot_values", []),
            "result_source": getattr(result, "result_source", ""),
            "flags": getattr(result, "flags", []) or [],
        })
        log.append("出图 " + name + "：ok（" + str(getattr(result, "renderer", "altair")) + "）")

    renderers = {str(item.get("renderer") or "") for item in manifest["figures"]}
    if len(renderers) == 1:
        manifest["renderer"] = next(iter(renderers))
    elif len(renderers) > 1:
        manifest["renderer"] = "mixed"
    try:
        from importlib.metadata import version as _pkg_version

        if manifest["renderer"] == "altair":
            manifest["renderer_version"] = _pkg_version("altair")
        elif manifest["renderer"] == "pandasai":
            manifest["renderer_version"] = _pkg_version("pandasai")
    except Exception:  # noqa: BLE001 - 版本只做审计展示
        pass

    # 只有新图成功或本来就没有图时才清旧件；全部失败时保留上一次的交付件
    if manifest["figures"] or not ctx["figure_specs"]:
        keep_paths = {run_dir / item["files"][key] for item in manifest["figures"]
                      for key in item["files"]}
        keep_paths |= {run_dir / item["script"] for item in manifest["figures"]}
        stale = (list(fig_dir.glob("fig*.png")) + list(fig_dir.glob("fig*.svg"))
                 + list(src_dir.glob("fig*.py")) + list(src_dir.glob("fig*.vl.json")))
        for path in stale:
            if path in keep_paths:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    shutil.rmtree(work_root, ignore_errors=True)

    if manifest["figures"]:
        check = subprocess.run([sys.executable, str(FIGURE_TOOLS / "check_figure.py"),
                                str(fig_dir), "--min-dpi", "300", "--strict"],
                               cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120)
        if check.returncode != 0:
            log.append("图表合规检查未通过："
                       + ((check.stdout or "") + (check.stderr or ""))[-400:])
        else:
            log.append("图表合规：ok")
    else:
        log.append("没有可画的 table 发现，本次 0 图，正文不引用图")
    return manifest


# ---------------------------------------------------------------- 正文写作

def _extract_json(text: str) -> dict:
    body = str(text or "").strip()
    fence = chr(96) * 3
    if body.startswith(fence):
        body = body[len(fence):]
        body = re.sub(r"^[a-zA-Z]*\s*", "", body)
        body = body.rstrip()
        if body.endswith(fence):
            body = body[:-len(fence)]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    return json.loads(body[start:end + 1])


def _call_llm(prompt: str) -> str:
    result = chat(
        [{"role": "system", "content": "你是严谨的经营周报扩写助手，只输出 JSON。"},
         {"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=4096, timeout=240, thinking="disabled",
    )
    if isinstance(result, dict):
        return str(result.get("text") or "")
    return str(result or "")


def _supplement_prompt(info: dict, skeleton_items: list[dict], findings: list[dict],
                  key_map: dict, figures: list[dict]) -> str:
    model = {
        "week": info["week"], "period": info["period"], "compare": info["compare"],
        "skeleton": [{"id": str(item.get("id") or ""),
                      "fixed": str(item.get("fixed") or "")} for item in skeleton_items],
        "findings": [{"id": str(f.get("id") or ""),
                      "question": str(f.get("question") or ""),
                      "reason": str(f.get("reason") or ""),
                      "display": str(f.get("display") or ""),
                      "keys": key_map.get(str(f.get("id") or ""), [])}
                     for f in findings],
        "available_keys": sorted({key for keys in key_map.values() for key in keys}),
        "figures": [{"number": f["number"], "title": f["title"],
                     "points": [{"key": p.get("key"), "label": p.get("label"),
                                 "display": p.get("display"), "unit": p.get("unit")}
                                for p in (f.get("points") or [])]}
                    for f in figures],
    }
    return (
        "你是经营周报的扩写助手。只输出一个 JSON 对象，不要解释、不要 markdown 代码块。\n\n"
        "骨架句清单是纪要底稿，由程序逐字放进正文；你【不要】重写、删减、复制骨架句本身。\n"
        "数据发现由 pandasai 生成并执行 pandas 代码得出，是「补肉」，必须挂在同主题骨架句之后。\n\n"
        "写作要求：\n"
        "1. supplements：以骨架句的 id 为键，给每条同主题骨架句写一句「补充句」。\n"
        "   补充句必须注明是「数据发现」、不是纪要说法；数值一律原样使用 available_keys 里的\n"
        "   [[f:键名]] 占位符，不得造键、不得写裸数字、不得改动或复制骨架句。\n"
        "   只写该骨架句主题下的发现；没有相关发现就不要给这个 id 写补充句。\n"
        "2. standalone：无法归到任何骨架句的发现，单独成句，同样注明数据发现并用占位符。\n"
        "3. next_week：下周关注事项数组，可为空；不要写未来清单里的原句。\n"
        "4. 不要输出章节标题、表格、图号引用（正文不写「见图 N」）、Markdown，只输出 JSON。\n"
        "5. 补充句必须写出对象名与数值，不要写「居首/垫底的单品见图 X」这类只指向图的空句；\n"
        "   对象名只能来自 figures[].points[].label，数值只能写成对应的 [[f:键名]]。\n"
        "6. 不写数据质量内容：缺数、缺失、重复、去重、异常记录、负数 / 冲销、记录数 / 行数 /\n"
        "   条数 / 天数、POS 上传 / 网络故障、数据核对等一律不写。\n"
        "7. 不评价纪要说法是否一致；不写「待核实」「需进一步核实」「原因待核实」「疑似」「可能」\n"
        "   等不确定表述。无法解释的原因直接省略，不要用「待核实」占位。\n"
        "8. 不得新增纪要里没有的原因；只写数据能确定的结论。\n\n"
        '输出结构：{"supplements":{"M01":"..."},"standalone":["..."],"next_week":["..."]}\n\n'
        "输入：\n" + json.dumps(model, ensure_ascii=False, indent=1)
    )


def _clean_writing(writing: dict) -> dict:
    """模型返回的补充句统一过一遍 S5 口径过滤。"""
    supplements = writing.get("supplements")
    standalone = writing.get("standalone")
    next_week = writing.get("next_week")
    return {
        "supplements": s5_filters.clean_mapping(
            supplements if isinstance(supplements, dict) else {}),
        "standalone": s5_filters.clean_items(
            standalone if isinstance(standalone, list) else []),
        "next_week": s5_filters.clean_items(
            next_week if isinstance(next_week, list) else []),
    }


def _sanitize_placeholders(text: str, ledger_keys: set[str]) -> str:
    """规范模型文字里的占位符形态并保住可追溯底线（骨架句不走这里）。

    - 合法 [[f:键名]] 且键在台账 → 原样保留；
    - 模型偶尔漏写 f: 前缀写成 [[键名]] → 补成合法 [[f:键名]]（键必须在台账）；
    - 键不在台账的占位符 → 抹掉，避免留下无法替换的畸形记号。
    """
    any_token = re.compile(r"\[\[(?:f:)?([^\]]+)\]\]")

    def replace(match):
        key = match.group(1).strip()
        return "[[f:" + key + "]]" if key in ledger_keys else ""
    return any_token.sub(replace, str(text or ""))


def _report_title(info: dict) -> str:
    week = str(info.get("week") or "")
    match = re.search(r"(\d{4})\D*W(\d{1,2})", week)
    if match:
        return match.group(1) + " 年第 " + str(int(match.group(2))) + " 周经营周报"
    return (week + " 经营周报").strip()


def _assemble_draft(info: dict, skeleton_items: list[dict],
                    future_items: list[dict], writing: dict, figures: list[dict],
                    findings: list[dict], key_map: dict, ledger_keys: set[str],
                    cards_actions: list[dict], business: dict | None = None) -> str:
    """代码装配：业务硬要求 → 骨架 fixed 逐字 → 补充句；再补发现明细 / 图表 / 下周关注。"""
    lines: list[str] = ["# " + _report_title(info), ""]
    business = business or {}
    summary = str(business.get("summary") or "").strip()
    if summary:
        lines += ["## 本周摘要", "", summary, ""]
    categories = [str(item) for item in (business.get("categories") or []) if str(item)]
    top_products = [str(item) for item in (business.get("top_products") or []) if str(item)]
    if categories or top_products:
        lines += ["## 品类表现", ""]
        for item in categories:
            lines.append("- " + item)
        if top_products:
            lines.append("- 本周实收前 5 单品：" + "、".join(top_products))
        lines.append("")
    anomalies = [str(item) for item in (business.get("anomalies") or []) if str(item)]
    if anomalies:
        lines += ["## 异常与原因", ""] + ["- " + item for item in anomalies] + [""]
    quality = [str(item) for item in (business.get("quality") or []) if str(item)]
    inconsistencies = [str(item) for item in (business.get("inconsistencies") or []) if str(item)]
    if quality or inconsistencies:
        lines += ["## 数据质量与口径说明", ""]
        lines += ["- " + item for item in quality + inconsistencies]
        lines.append("")
    lines += ["## 纪要骨架与数据补充", ""]
    supplements = writing.get("supplements") or {}
    for item in skeleton_items:
        fixed = str(item.get("fixed") or "")
        if fixed:
            lines.append(fixed)          # 一字不改、不裁剪、不替换、不 strip
            clause = _inconsistency_clause(str(item.get("note") or ""),
                                           str(item.get("issue") or ""))
            if clause:
                lines.append(f"（数据核实：{clause}；以数据为准。）")
            lines.append("")
        sid = str(item.get("id") or "")
        supp = str(supplements.get(sid) or "").strip()
        if supp:
            supp = _sanitize_placeholders(supp, ledger_keys)
            lines.append("【数据发现】" + supp)
            lines.append("")

    discovery_lines: list[str] = []
    for finding in findings:
        fid = str(finding.get("id") or "")
        keys = key_map.get(fid) or []
        tokens = "、".join(_tok(key) for key in keys)
        if not tokens:
            continue
        question = str(finding.get("question") or "")
        reason = str(finding.get("reason") or "")
        text = "【数据发现】" + question
        if reason:
            text += "（" + reason + "）"
        discovery_lines.append(text + "：" + tokens)
    for text in (writing.get("standalone") or []):
        cleaned = _sanitize_placeholders(str(text or ""), ledger_keys).strip()
        if cleaned:
            discovery_lines.append("【数据发现】" + cleaned)
    if discovery_lines:
        lines += ["## 数据发现明细（pandasai 取数，供核对）", ""] + discovery_lines + [""]

    if figures:
        lines += ["## 图表", ""]
        for fig in figures:
            lines.append(fig["caption"])
            lines.append("")

    next_items = [str(item).strip() for item in (writing.get("next_week") or [])
                 if str(item).strip()]
    for action in cards_actions[:3]:
        text = s5_filters.clean_text(str(action.get("item") or ""))
        if text and text not in next_items and not s5_filters.is_quality_text(text):
            next_items.append(text)
    if next_items:
        lines += ["## 下周关注", ""]
        for item in next_items:
            lines.append("- " + _sanitize_placeholders(item, ledger_keys))
        lines.append("")

    if future_items:
        # s5.txt:10 未来原句不写进本周报告；明细落在 s5_facts.json 的 future 字段
        lines += ["## 未来清单", "",
                  "本周期纪要另有若干未来事项，未写入本周报告，明细见 s5_facts.json 的 future 字段。", ""]
    return "\n".join(lines) + "\n"


def _check_invariants(draft: str, skeleton_items: list[dict],
                      ledger_keys: set[str]) -> list[str]:
    """可追溯底线，不是格式规制：骨架逐字 + 每个占位符都在台账。"""
    errors: list[str] = []
    for item in skeleton_items:
        fixed = str(item.get("fixed") or "")
        if fixed and fixed not in draft:
            errors.append("骨架句未逐字进正文：" + fixed[:50])
    for key in _PLACEHOLDER_RE.findall(draft):
        if key not in ledger_keys:
            errors.append("占位符键不在台账：" + key)
    return errors


def _qc_final(text: str, business: dict, manifest: dict,
              has_target: bool = True) -> dict:
    """S5 收尾自动质检：结构、摘要指标、数据质量、数字追溯、图表题注。

    has_target=False（本周期没有销售目标数据）时不要求摘要写目标达成率。
    """
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    headings = ("## 本周摘要", "## 品类表现", "## 异常与原因",
                "## 数据质量与口径说明")
    missing = [heading for heading in headings if heading not in text]
    check("结构完整", not missing,
          "缺少：" + "、".join(missing) if missing else "四个必需章节齐全")

    summary = str((business or {}).get("summary") or "")
    wanted = ["实收", "较上周", "毛利率"]
    if has_target:
        wanted.append("目标达成率")
    absent = [word for word in wanted if word not in summary]
    check("摘要指标", not absent,
          "缺少：" + "、".join(absent)
          if absent else
          ("总实收/环比/毛利率/目标达成齐全"
           if has_target else "总实收/环比/毛利率齐全（本周期无目标数据）"))
    sentences = [item for item in re.split(r"[。！？!?]", summary) if item.strip()]
    check("摘要句数", len(sentences) <= 5, f"{len(sentences)} 句")
    check("前5单品", "本周实收前 5 单品：" in text,
          "已列前 5 单品" if "本周实收前 5 单品：" in text else "未列前 5 单品")

    quality = [str(item) for item in ((business or {}).get("quality") or [])]
    disclosed = any(("重复" in item or "退货" in item or "无系统数据" in item)
                    for item in quality)
    check("数据质量披露", bool(quality) and disclosed,
          "已写重复/退货/缺失" if disclosed else "未写数据质量")

    unresolved = _PLACEHOLDER_RE.findall(text)
    check("数字可追溯", not unresolved,
          "无未替换占位符" if not unresolved else "未替换：" + "、".join(unresolved[:5]))

    figures = [item for item in (manifest.get("figures") or []) if isinstance(item, dict)]
    bad = [str(item.get("number")) for item in figures
           if not all(word in str(item.get("caption") or "")
                      for word in ("2026", "单位", "比较基准"))]
    check("图表题注", not bad,
          "题注含周期/单位/基准" if not bad else "题注缺项：图 " + "、".join(bad))

    passed = sum(1 for item in checks if item["ok"])
    verdict = "PASS" if passed == len(checks) else "FAIL"
    return {
        "state": verdict, "verdict": verdict,
        "summary": f"{passed}/{len(checks)} 项通过",
        "checks": checks, "attempts": 1,
        "at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def _display_stats(text: str, ledger: dict) -> dict:
    """text_stats 只用于界面展示，绝不据此判失败。"""
    try:
        from tools.text_stats import analyze, load_banned
        displays = {str(entry.get("key")): str(entry.get("display") or "")
                    for entry in ledger.get("entries") or [] if entry.get("key")}
        return analyze(text, load_banned(SPEC_PATH), stage="draft",
                       ledger_displays=displays)
    except Exception as exc:  # noqa: BLE001 - 统计失败不挡落盘
        return {"error": str(exc)}


def _finding_hotspot(finding: dict) -> dict:
    return {"object": str(finding.get("reason") or finding.get("question")
                          or finding.get("id") or ""),
            "metric": "pandasai 发现", "change": None, "impact": None,
            "written": True, "section": "数据发现",
            "reason": str(finding.get("reason") or "")}


# ---------------------------------------------------------------- 主流程

_S5_RUN_LOCK = threading.Lock()


def is_running() -> bool:
    """当前进程里是否已有一次 S5 在跑，供接口显示“扩写中”。"""
    return bool(_S5_RUN_LOCK.locked())


def run_s5(run_dir: str | Path, run_root: str | Path | None = None,
           use_ai: bool = True, full: bool = False) -> S5Outcome:
    """同一进程只允许一次 S5：并发会互相覆盖 ledger / figures / draft。"""
    if not _S5_RUN_LOCK.acquire(blocking=False):
        return S5Outcome(False, "S5 正在运行",
                         "没有生成成功：已有一次 S5 正在运行，等它结束再试。")
    try:
        return _run_s5(run_dir, run_root, use_ai, full)
    finally:
        _S5_RUN_LOCK.release()


def _plan_incremental(run_dir: Path, plan_questions: list[dict] | None,
                      state: str, full: bool, log: list[str],
                      s5_questions) -> dict | None:
    """已扩写过、这次只改问题清单时，找出可复用的 findings。

    在已生成 / 待审核 / 退回修改时启用：此时数据、画像、S4 骨架都没有变，复用安全。
    返回 None 表示走全量。
    """
    if full or plan_questions is None or state not in (
            STATUS_DONE, STATUS_REVIEW, STATUS_REJECTED):
        return None
    if not (run_dir / "draft.md").is_file():
        return None
    facts = _read_json(run_dir / "s5_facts.json")
    old_findings = [item for item in (facts.get("discoveries") or [])
                    if isinstance(item, dict)]
    if not old_findings:
        return None
    old_questions = facts.get("questions")
    if not isinstance(old_questions, list) or not old_questions:
        old_questions = s5_questions.questions_from_findings(old_findings)
    try:
        diff = s5_questions.diff_questions(plan_questions, old_questions)
    except Exception as exc:  # noqa: BLE001 - 比较失败退回全量
        log.append("增量比较失败，改走全量：" + str(exc))
        return None
    runnable_ids = set(diff["changed_ids"])
    reuse_ids = set(diff["reused_ids"])
    found_ids = {str(item.get("id")) for item in old_findings}
    missing = [q for q in plan_questions
               if str(q.get("id")) in reuse_ids and str(q.get("id")) not in found_ids]
    if missing:
        missing_ids = {str(q.get("id")) for q in missing}
        runnable_ids |= missing_ids
        reuse_ids -= missing_ids
    runnable = [q for q in plan_questions if str(q.get("id")) in runnable_ids]
    reuse = [f for f in old_findings if str(f.get("id")) in reuse_ids]
    log.append("增量扩写：重算 " + str(len(runnable)) + " 条，复用 "
               + str(len(reuse)) + " 条，移除 "
               + str(len(diff["removed_ids"])) + " 条"
               + ("（补算上次未出结果：" + "、".join(
                   str(q.get("id")) for q in missing) + "）" if missing else ""))
    return {"diff": diff, "runnable": runnable, "reuse": reuse,
            "reuse_ids": reuse_ids, "facts": facts}


def _run_s5(run_dir: str | Path, run_root: str | Path | None = None,
            use_ai: bool = True, full: bool = False) -> S5Outcome:
    run_dir = Path(run_dir)
    if not run_dir.is_dir() and run_root:
        run_dir = Path(run_root) / run_dir
    if not run_dir.is_dir():
        raise FileNotFoundError("找不到运行目录：" + str(run_dir))
    log: list[str] = []

    intake = _read_json(run_dir / "intake.json")
    if not intake:
        raise FileNotFoundError("找不到 intake.json，先跑完 S1：" + str(run_dir / "intake.json"))
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    run_id = str(run.get("period_tag") or run_dir.name)
    minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
    week = str(minutes.get("week") or run_id)
    dir_label = str(run.get("dir") or run_dir.name)

    # 前置门禁（保留现状）：状态机 + 上游四件齐备
    status = _read_json(run_dir / "status.json")
    state = str(status.get("state") or "")
    allowed = state in (STATUS_READY, STATUS_DONE, STATUS_REVIEW, STATUS_REJECTED)
    if not allowed:
        msg = ("S5 只接受状态「可扩写」「已生成」「待审核」或「退回修改」的运行；当前状态："
               + (state or "未知") + "。先回上游处理。")
        return S5Outcome(False, state or "未知", msg, log=[msg], run_id=run_id,
                         dir=dir_label, week=week)

    corrected = _read_json(run_dir / "corrected.json")
    if not corrected:
        raise FileNotFoundError("还没有 corrected.json，先跑 S4：" + str(run_dir / "corrected.json"))
    items = [item for item in corrected.get("items") or [] if isinstance(item, dict)]
    skeleton_items = [item for item in items
                      if str(item.get("status") or "") not in ("future", "blocked")]
    future_items = [item for item in items if str(item.get("status") or "") == "future"]
    blocked_items = [item for item in items if str(item.get("status") or "") == "blocked"]

    cards_doc = _read_json(run_dir / "cards.json")
    if not cards_doc.get("cards"):
        raise FileNotFoundError("cards.json 没有可用卡片，先跑 S2：" + str(run_dir / "cards.json"))
    ledger_old = _read_json(run_dir / "ledger.json")
    if not ledger_old.get("entries"):
        raise FileNotFoundError("还没有 ledger.json，先跑 S3：" + str(run_dir / "ledger.json"))

    # 失败一律直说「没有生成成功」+ 原因，不兜底、不用旧模板/纯骨架凑稿，且不落盘
    def _fail(msg: str) -> S5Outcome:
        full = "没有生成成功：" + msg
        log.append(full)
        return S5Outcome(False, state, full, log=log, run_id=run_id,
                         dir=dir_label, week=week)

    if not use_ai:
        return _fail("已关闭模型（use_ai=False），S5 需要模型做发现和补肉")

    # 问题清单是 S5 的输入：有清单就只跑清单，不重新出题；损坏则明确失败。
    from . import s5_questions

    try:
        plan = s5_questions.load_plan(run_dir)
    except Exception as exc:  # noqa: BLE001 - 清单损坏不静默回退自动出题
        return _fail("问题清单读取失败：" + str(exc))
    plan_questions = plan["questions"] if plan else None
    if plan:
        log.append("读取问题清单 " + str(len(plan_questions)) + " 条（source="
                   + str(plan.get("source") or "") + "）")
    question_filter = None
    filtered_questions: list[dict] = []
    if plan_questions is not None:
        raw_questions = list(plan_questions or [])
        plan_questions, question_filter = s5_questions.filter_for_run(
            run_dir, plan_questions, log=log)
        dropped = list(question_filter.get("dropped") or [])
        if dropped:
            dropped_ids = {str(item.get("id") or "") for item in dropped}
            filtered_questions = [item for item in raw_questions
                                  if str(item.get("id") or "") in dropped_ids]
            log.append("问题过滤跳过：" + "、".join(
                str(item.get("id") or "") + "（"
                + (str(item.get("why") or "不适合写进周报")[:30]) + "）"
                for item in dropped))
        if not plan_questions:
            log.append("问题清单没有可执行问题，改为自动出题")
            plan_questions = None
            plan = None
    plan_vetted = plan_questions is not None

    inc = _plan_incremental(run_dir, plan_questions, state, full, log, s5_questions)

    # 先归档上一版，再开始任何写入；归档失败则不覆盖旧稿
    try:
        _archive_previous(run_dir, log)
    except Exception as exc:  # noqa: BLE001 - 归档失败必须挡住覆盖
        return _fail("版本归档失败，为避免覆盖上一版，未开始扩写：" + str(exc))

    frame, meta = _load_data(run_dir, intake)
    log.append("读入数据 " + "、".join(meta["files"]) + "，原始 " + str(meta["raw_rows"])
               + " 条，去重 " + str(meta["duplicates"]) + " 条")
    info = _period_info(frame, intake, run_id)
    reason_items = [item for item in items
                    if str(item.get("status") or "") != "blocked"]
    business = _business_facts(frame, meta, info, reason_items)
    business_source = "code"
    if business:
        log.append("业务硬要求已计算：摘要 / 品类 / 前 5 单品 / 异常 / 数据质量")

    # 画像先算在内存里，落盘推到成功之后，保证失败路径不落交付件。
    # 增量时数据没变，直接复用上一版 profile.json，省一次全表扫描。
    profile_path = run_dir / "profile.json"
    if inc is not None and profile_path.is_file():
        profile = _read_json(profile_path)
        log.append("画像复用上一版 profile.json")
    else:
        profile = _profile(frame, meta, info)
        log.append("画像已计算（" + ("ok" if profile.get("ok") else "失败") + "）")

    # 业务硬要求：先让 AI 出数，再用代码参考值复核；不一致就退回代码值
    reference_numbers = _numbers_from_frame(frame, meta, info)
    has_target = bool((reference_numbers or {}).get("total_target"))
    business_frame = _with_target_column(frame, meta)
    ai_numbers = _discover_business(run_dir, business_frame, intake, profile, info, log)
    if ai_numbers and _numbers_close(ai_numbers, reference_numbers):
        business_source = "ai"
        log.append("AI 业务取数与代码复核一致，正文采用 AI 口径")
    else:
        business_source = "code"
        log.append("AI 业务取数缺失或与代码复核不一致，正文采用代码复核值")

    # 发现层：pandasai 生成并执行 pandas 代码，单步问句
    findings: list[dict] = []
    filtered_findings: list[dict] = []
    failed_questions: list[str] = []
    disc_model = ""
    disc_questions: list[dict] = []
    questions_source = "plan" if plan_questions is not None else "auto"
    incremental = inc is not None
    recomputed = 0
    reused = 0
    removed = 0

    if incremental:
        questions_source = "incremental"
        disc_questions = list(plan_questions or [])
        runnable = inc["runnable"]
        reuse_findings: list[dict] = []
        for item in inc["reuse"] or []:
            if not isinstance(item, dict):
                continue
            if not plan_vetted and s5_filters.is_quality_finding(item):
                filtered_findings.append(item)
            else:
                reuse_findings.append(item)
        new_findings: list[dict] = []
        if runnable:
            try:
                from . import s5_discover
                disc = s5_discover.discover(run_dir, frame, intake, profile,
                                            skeleton_items, questions=runnable,
                                            keep_quality=plan_vetted)
                filtered_questions.extend(
                    item for item in (disc.get("dropped_questions") or [])
                    if isinstance(item, dict))
                for item in disc.get("findings") or []:
                    if not isinstance(item, dict):
                        continue
                    if not plan_vetted and s5_filters.is_quality_finding(item):
                        filtered_findings.append(item)
                    else:
                        new_findings.append(item)
                log.extend(str(line) for line in (disc.get("log") or []))
                disc_model = str(disc.get("model") or "")
                log.append("增量取数完成：" + str(len(new_findings)) + " / "
                           + str(len(runnable)) + " 条")
            except Exception as exc:  # noqa: BLE001
                return _fail("增量取数异常：" + str(exc))
        else:
            disc_model = str(inc["facts"].get("model") or "")
        new_by_id = {str(item.get("id")): item for item in new_findings}
        failed = [str(q.get("id")) for q in runnable
                  if str(q.get("id")) not in new_by_id]
        if failed:
            return _fail("改动的问句没有取到结果：" + "、".join(failed)
                         + "；旧稿未覆盖，修好问题后重试。")
        reuse_by_id = {str(item.get("id")): item for item in reuse_findings}
        for question in disc_questions:
            fid = str(question.get("id") or "")
            if fid in new_by_id:
                findings.append(new_by_id[fid])
            elif fid in reuse_by_id:
                findings.append(reuse_by_id[fid])
        recomputed = len(runnable)
        reused = len(reuse_findings)
        removed = len(inc["diff"]["removed_ids"])
        found_ids = {str(item.get("id") or "") for item in findings}
        failed_questions = [str(q.get("id") or "") for q in disc_questions
                            if str(q.get("id") or "") not in found_ids]
        log.append("增量合并后可用发现 " + str(len(findings)) + " 条")
    else:
        try:
            from . import s5_discover
            disc = s5_discover.discover(run_dir, frame, intake, profile, skeleton_items,
                                        questions=plan_questions,
                                        keep_quality=plan_vetted)
            filtered_questions.extend(
                item for item in (disc.get("dropped_questions") or [])
                if isinstance(item, dict))
            for item in disc.get("findings") or []:
                if not isinstance(item, dict):
                    continue
                if not plan_vetted and s5_filters.is_quality_finding(item):
                    filtered_findings.append(item)
                else:
                    findings.append(item)
            disc_questions = [item for item in (disc.get("questions") or [])
                              if isinstance(item, dict)]
            questions_source = str(disc.get("source") or questions_source)
            log.extend(str(line) for line in (disc.get("log") or []))
            disc_model = str(disc.get("model") or "")
            log.append("pandasai 发现 " + str(len(findings)) + " 条"
                       + ("（模型 " + disc_model + "）" if disc_model else ""))
            found_ids = {str(item.get("id") or "") for item in findings}
            filtered_ids = {str(item.get("id") or "") for item in filtered_findings}
            failed_questions = [str(q.get("id") or "") for q in disc_questions
                                if str(q.get("id") or "") not in found_ids
                                and str(q.get("id") or "") not in filtered_ids]
        except Exception as exc:  # noqa: BLE001
            return _fail("数据发现阶段异常：" + str(exc))

    filtered_context = {
        "questions": [
            {"id": str(item.get("id") or ""),
             "question": str(item.get("question") or ""),
             "reason": str(item.get("reason") or "")}
            for item in filtered_questions[:20] if isinstance(item, dict)
        ],
        "findings": [
            {"id": str(item.get("id") or ""),
             "question": str(item.get("question") or ""),
             "reason": str(item.get("reason") or ""),
             "display": str(item.get("display") or ""),
             "value": item.get("value"),
             "rows": (item.get("rows") or [])[:5]
                     if isinstance(item.get("rows"), list) else None}
            for item in filtered_findings[:30] if isinstance(item, dict)
        ],
    }

    if not findings:
        return _fail("pandasai 没有产出任何可用发现（问句失败/超时/空结果），未生成正文")
    if questions_source == "auto" and disc_questions:
        try:
            s5_questions.save_plan(run_dir, disc_questions, source="auto", run_id=run_id)
            log.append("问题清单已保存：" + str(len(disc_questions)) + " 条")
        except Exception as exc:  # noqa: BLE001 - 清单落盘失败明确报错
            return _fail("问题清单落盘失败：" + str(exc))

    # 增量时先丢掉上一轮 S5 写进 ledger 的旧发现，避免删改问题后留下脏键
    if incremental:
        ledger_old = {
            **ledger_old,
            "entries": [entry for entry in (ledger_old.get("entries") or [])
                        if str(entry.get("added_by") or "") != "S5"],
        }

    # 发现数值 → ledger（保留 S3 原有条目）
    label_map = _code_name_map(frame)
    entries, key_map, figure_specs = _prepare_findings(findings, info, label_map)
    ledger = _merge_ledger(ledger_old, entries, info)
    ledger_keys = {str(item.get("key")) for item in ledger["entries"] if item.get("key")}
    log.append("台账已扩到 " + str(len(ledger["entries"])) + " 条（S5 新增 "
               + str(len(entries)) + " 条）")

    # 出图：图数由 table 型发现决定，0 图也合法
    ctx = {"tag": run_id, "week": week, "period": info["period"],
           "compare": info["compare"], "figure_specs": figure_specs}
    try:
        manifest = _make_figures(run_dir, ctx, log)
    except Exception as exc:  # noqa: BLE001 - 出图异常也是 S5 失败，不落盘
        return _fail("出图阶段异常：" + str(exc))
    if figure_specs and not manifest.get("figures"):
        reason = next((line for line in reversed(log) if "失败" in line),
                      "pandasai 没有返回可用的 plot 结果")
        return _fail("pandasai 出图全部失败：" + reason)
    log.append("图表清单已生成（" + str(len(manifest["figures"])) + " 张图）")

    # 补充句写作（GLM 只写补肉，骨架不交给模型）；失败直说，不用纯骨架凑稿
    writing: dict = {"supplements": {}, "standalone": [], "next_week": []}
    cards_actions: list[dict] = []
    try:
        prompt = _supplement_prompt(info, skeleton_items, findings, key_map,
                               manifest["figures"])
        raw = _call_llm(prompt)
        if not raw.strip():
            return _fail("补充句模型返回为空，未生成正文")
        data = _extract_json(raw)
        writing = _clean_writing({
            "supplements": data.get("supplements") if isinstance(data.get("supplements"), dict) else {},
            "standalone": data.get("standalone") if isinstance(data.get("standalone"), list) else [],
            "next_week": data.get("next_week") if isinstance(data.get("next_week"), list) else [],
        })
        log.append("补充句模型返回已解析")
    except Exception as exc:  # noqa: BLE001
        return _fail("补充句生成失败：" + str(exc))
    actions = cards_doc.get("actions")
    if isinstance(actions, list):
        cards_actions = [item for item in actions if isinstance(item, dict)]

    # 装配正文
    text = _assemble_draft(info, skeleton_items, future_items, writing,
                           manifest["figures"], findings, key_map, ledger_keys,
                           cards_actions, business=business)

    # 不变量强制校验（可追溯底线）
    errors = _check_invariants(text, skeleton_items, ledger_keys)
    if errors:
        log.extend(errors[:8])
        return _fail("；".join(errors[:3]) + "（骨架/占位符可追溯校验未通过）")

    stats_out = _display_stats(text, ledger)
    stats_out["hotspots"] = len(findings)
    stats_out["discoveries"] = len(findings)
    stats_out["figures"] = len(manifest["figures"])

    skeleton_out = [{"id": str(item.get("id") or ""),
                     "fixed": str(item.get("fixed") or ""),
                     "status": str(item.get("status") or "")}
                    for item in skeleton_items]
    hotspots = [_finding_hotspot(item) for item in findings]
    facts_out = {
        "run_id": run_id, "week": week, "period": info["period"],
        "compare": info["compare"], "model": disc_model,
        "questions": disc_questions,
        "questions_hash": (s5_questions.questions_hash(disc_questions)
                           if disc_questions else ""),
        "questions_source": questions_source,
        "discoveries": findings, "skeleton": skeleton_out,
        "future": [{"id": str(item.get("id") or ""),
                    "fixed": str(item.get("fixed") or ""), "status": "future"}
                   for item in future_items],
        "blocked": [{"id": str(item.get("id") or ""), "status": "blocked"}
                    for item in blocked_items],
        "question_filter": question_filter,
        "failed_questions": failed_questions,
        "filtered_discoveries": filtered_context,
        "business": business, "business_source": business_source,
        "business_ai_numbers": ai_numbers,
        "hotspots": hotspots, "figures": manifest["figures"], "stats": stats_out,
    }

    (run_dir / "draft.md").write_text(text, encoding="utf-8")
    _write_json(run_dir / "ledger.json", ledger)
    _write_json(run_dir / "figures_manifest.json", manifest)
    _write_json(run_dir / "profile.json", profile)
    _write_json(run_dir / "s5_facts.json", facts_out)

    final_text = text
    displays = {str(entry.get("key")): str(entry.get("display") or "")
                for entry in ledger.get("entries") or [] if entry.get("key")}
    if displays:
        final_text = _PLACEHOLDER_RE.sub(
            lambda match: displays.get(match.group(1), match.group(0)), text)
    if final_text != text:
        (run_dir / "draft.md").write_text(final_text, encoding="utf-8")

    stats_out = _display_stats(final_text, ledger)
    stats_out["hotspots"] = len(findings)
    stats_out["discoveries"] = len(findings)
    stats_out["figures"] = len(manifest["figures"])
    if incremental and inc is not None:
        stats_out["incremental"] = {
            "recomputed": recomputed, "reused": reused, "removed": removed,
            "order_changed": bool(inc["diff"]["order_changed"]),
        }
    facts_out["stats"] = stats_out
    facts_out.pop("qc", None)
    _write_json(run_dir / "s5_facts.json", facts_out)

    qc = _qc_final(final_text, business, manifest, has_target=has_target)
    saved_waivers = _read_json(run_dir / "s5_fixes.json").get("waivers") or []
    review = _ai_review_draft(final_text, business, qc, info, run_dir,
                              business_frame, intake, profile, log,
                              filtered=filtered_context, has_target=has_target,
                              skeleton=skeleton_out,
                              waivers=_s1_waiver_lines(intake) + list(saved_waivers))
    qc["ai_review"] = review
    qc["business_source"] = business_source
    detail = ("跳过：" + str(review.get("reason") or "")
              if review.get("skipped") else
              ("通过" if review.get("ok") else "未通过：" +
               ("；".join((review.get("issues") or [])[:3])
                or "复核人只给了未通过结论，没有列出具体问题")))
    qc["checks"].append({"name": "AI复核", "ok": bool(review.get("ok")),
                         "detail": detail})
    passed = sum(1 for item in qc["checks"] if item["ok"])
    qc["verdict"] = "PASS" if passed == len(qc["checks"]) else "FAIL"
    qc["summary"] = f"{passed}/{len(qc['checks'])} 项通过"
    qc["draft_after_sha256"] = _sha256_file(run_dir / "draft.md")
    _write_json(run_dir / "qc_result.json", qc)
    facts_out["business_review"] = review
    facts_out["business_source"] = business_source
    _write_json(run_dir / "s5_facts.json", facts_out)
    if qc["verdict"] != "PASS":
        failed = [item["name"] for item in qc["checks"] if not item["ok"]]
        out = _fail("自动质检未通过：" + "、".join(failed)
                    + (("；" + "；".join((review.get("issues") or [])[:3]))
                       if review.get("issues") else ""))
        out.qc = qc
        out.review = review
        out.draft = final_text
        out.stats = stats_out
        return out

    from . import s5_review

    round_no = int(s5_review.load_record(run_dir).get("round") or 0)
    found_ids = [str(item.get("id") or "") for item in findings]
    message = ("骨架 " + str(len(skeleton_items)) + " 句逐字保留，数据发现 "
               + str(len(findings)) + " 条"
               + ("（" + "、".join(found_ids) + "）" if found_ids else "")
               + "，" + str(len(manifest["figures"]))
               + " 张图，正文 " + str(stats_out.get("chars_body_rendered") or "—")
               + " 字；自动质检 " + str(qc.get("summary") or "")
               + "；占位符已替换，已自动送审，状态为待审核（第 "
               + str(round_no + 1) + " 轮）。")
    if incremental:
        message += ("增量：重算 " + str(recomputed) + " 条，复用 "
                    + str(reused) + " 条"
                    + ("，移除 " + str(removed) + " 条" if removed else ""))
    if question_filter and question_filter.get("dropped"):
        message += ("问题过滤跳过 " + str(len(question_filter["dropped"])) + " 条："
                    + "、".join(str(item.get("id") or "")
                                for item in question_filter["dropped"]) + "。")
    if failed_questions:
        message += ("问题取数未出结果 " + str(len(failed_questions)) + " 条："
                    + "、".join(failed_questions) + "。")
    _write_status(run_dir, STATUS_REVIEW, message)
    try:
        record = s5_review.open_record(run_dir)
    except Exception as exc:  # noqa: BLE001 - 正文已生成，但送审记录必须落盘
        log.append("送审记录写入失败：" + repr(exc))
        return S5Outcome(False, STATUS_REVIEW,
                         "正文已生成，但送审记录写入失败：" + str(exc),
                         draft=final_text, stats=stats_out, manifest=manifest,
                         profile=profile, figures=manifest["figures"],
                         hotspots=hotspots, log=log, run_id=run_id,
                         dir=dir_label, week=week, discoveries=findings,
                         skeleton=skeleton_out, questions=disc_questions,
                         questions_source=questions_source)
    log.append("已写入 draft.md / figures_manifest.json / profile.json")
    log.append("占位符已替换；已自动送审，状态 → " + STATUS_REVIEW)
    return S5Outcome(True, STATUS_REVIEW, message, draft=final_text, stats=stats_out,
                     manifest=manifest, profile=profile, figures=manifest["figures"],
                     hotspots=hotspots, log=log, run_id=run_id,
                     dir=dir_label, week=week,
                     discoveries=findings, skeleton=skeleton_out,
                     questions=disc_questions, questions_source=questions_source,
                     incremental=incremental, recomputed=recomputed,
                     reused=reused, removed=removed, review=record)

def _apply_issue_waivers(review: dict, waive_issues: list[dict] | None) -> dict:
    """只豁免用户点名的 AI 复核条目；代码质检不受影响。"""
    matches = [str(item.get("match") or "").strip()
               for item in (waive_issues or [])
               if isinstance(item, dict) and str(item.get("match") or "").strip()]
    issues = list(review.get("issues") or [])
    if not matches or not issues:
        return review
    kept: list[str] = []
    waived: list[str] = []
    for issue in issues:
        text = str(issue)
        if any(match in text for match in matches):
            waived.append(text)
        else:
            kept.append(text)
    if not waived:
        return review
    out = dict(review)
    out["issues"] = kept
    out["waived"] = list(out.get("waived") or []) + waived
    if out.get("ok") is False and not kept:
        out["ok"] = True
    return out


def _replace_edits(value, edits: list[dict]) -> tuple[object, int]:
    """把同一组精确替换应用到任意 JSON 结构里的字符串。"""
    if isinstance(value, str):
        total = 0
        for edit in edits:
            find = str(edit.get("find") or "")
            replace = str(edit.get("replace") or "")
            if find and find in value:
                total += value.count(find)
                value = value.replace(find, replace)
        return value, total
    if isinstance(value, list):
        total = 0
        out = []
        for item in value:
            changed, count = _replace_edits(item, edits)
            out.append(changed)
            total += count
        return out, total
    if isinstance(value, dict):
        total = 0
        out = {}
        for key, item in value.items():
            changed, count = _replace_edits(item, edits)
            out[key] = changed
            total += count
        return out, total
    return value, 0


def fix_draft(run_dir: str | Path, edits: list[dict], waivers: list[str] | None = None,
              ledger_updates: list[dict] | None = None,
              waive_issues: list[dict] | None = None, use_ai: bool = True,
              by: str = "用户") -> S5Outcome:
    """质检未通过后，按用户决定定点改稿并只重跑质检，不重跑扩写。"""
    if not _S5_RUN_LOCK.acquire(blocking=False):
        return S5Outcome(False, "S5 正在运行",
                         "现在不能改稿：已有一次 S5 正在运行，等它结束再试。")
    try:
        return _fix_draft(run_dir, edits, waivers, ledger_updates,
                          waive_issues, use_ai, by)
    finally:
        _S5_RUN_LOCK.release()


def _fix_draft(run_dir: str | Path, edits: list[dict], waivers: list[str] | None,
               ledger_updates: list[dict] | None,
               waive_issues: list[dict] | None, use_ai: bool, by: str) -> S5Outcome:
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise FileNotFoundError("找不到运行目录：" + str(run_dir))
    draft_path = run_dir / "draft.md"
    if not draft_path.is_file():
        return S5Outcome(False, "未知", "还没有 S5 正文，先跑 run_s5。")
    facts = _read_json(run_dir / "s5_facts.json")
    corrected = _read_json(run_dir / "corrected.json")
    manifest = _read_json(run_dir / "figures_manifest.json")
    if not facts or not corrected:
        return S5Outcome(False, "未知", "缺少 s5_facts.json 或 corrected.json，先跑 S5。")
    status = _read_json(run_dir / "status.json")
    current_state = str(status.get("state") or "")
    run_id = str(facts.get("run_id") or run_dir.name)
    week = str(facts.get("week") or run_id)
    intake = _read_json(run_dir / "intake.json")
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    dir_label = str(run.get("dir") or run_dir.name)
    log: list[str] = []

    clean_edits = []
    for edit in edits or []:
        find = str(edit.get("find") or "")
        replace = str(edit.get("replace") or "")
        if not find or find == replace:
            continue
        clean_edits.append({"find": find, "replace": replace})
    clean_waivers = [str(item).strip() for item in (waivers or []) if str(item).strip()]
    clean_waive_issues = [item for item in (waive_issues or [])
                          if isinstance(item, dict) and str(item.get("match") or "").strip()]
    if not clean_edits and not clean_waivers and not clean_waive_issues:
        return S5Outcome(False, current_state, "没有要给 S5 的修改或确认事项。")

    draft = draft_path.read_text(encoding="utf-8")
    missing = [edit["find"] for edit in clean_edits if edit["find"] not in draft]
    if missing:
        return S5Outcome(False, current_state,
                         "这些原文在 draft.md 里找不到，未做任何修改："
                         + "；".join(item[:80] for item in missing[:3]))

    before_sha = _sha256_file(draft_path)
    _archive_previous(run_dir, log)
    for edit in clean_edits:
        draft = draft.replace(edit["find"], edit["replace"])
    new_facts, _ = _replace_edits(facts, clean_edits)
    new_corrected, _ = _replace_edits(corrected, clean_edits)
    new_manifest, _ = _replace_edits(manifest, clean_edits)
    facts, corrected, manifest = new_facts, new_corrected, new_manifest

    ledger = _read_json(run_dir / "ledger.json")
    changed_ledger = 0
    if ledger_updates:
        entries = ledger.get("entries") if isinstance(ledger.get("entries"), list) else []
        by_key = {str(item.get("key") or ""): item for item in entries
                  if isinstance(item, dict)}
        for update in ledger_updates:
            key = str(update.get("key") or "").strip()
            entry = by_key.get(key)
            if not entry:
                continue
            if "value" in update:
                entry["value"] = update["value"]
            if "display" in update:
                entry["display"] = str(update["display"])
            entry["note"] = (str(entry.get("note") or "")
                             + "；S5 定点修改：" + str(update.get("note") or by)).strip("；")
            changed_ledger += 1
    if changed_ledger:
        _write_json(run_dir / "ledger.json", ledger)

    audit_path = run_dir / "s5_fixes.json"
    audit = _read_json(audit_path)
    all_waivers = list(audit.get("waivers") or [])
    for item in clean_waivers:
        if item not in all_waivers:
            all_waivers.append(item)

    profile = _read_json(run_dir / "profile.json")
    business = facts.get("business") if isinstance(facts.get("business"), dict) else {}
    has_target = "目标达成" in str(business.get("summary") or "")
    qc = _qc_final(draft, business, manifest, has_target=has_target)
    review: dict = {}
    if use_ai:
        try:
            frame, meta = _load_data(run_dir, intake)
            business_frame = _with_target_column(frame, meta)
            review = _ai_review_draft(
                draft, business, qc, {}, run_dir, business_frame, intake,
                profile, log, filtered=facts.get("filtered_discoveries") or {},
                has_target=has_target, skeleton=facts.get("skeleton") or [],
                waivers=_s1_waiver_lines(intake) + all_waivers,
                waive_issues=clean_waive_issues)
        except Exception as exc:  # noqa: BLE001 - 复核失败如实返回
            review = {"ok": True, "skipped": True,
                      "reason": "复核调用失败：" + str(exc)[:80], "queries": []}
    else:
        review = {"ok": True, "skipped": True, "reason": "未调用模型复核", "queries": []}
    review = _apply_issue_waivers(review, clean_waive_issues)
    qc["ai_review"] = review
    qc["business_source"] = facts.get("business_source") or ""
    waived_count = len(review.get("waived") or [])
    detail = ("跳过：" + str(review.get("reason") or "")
              if review.get("skipped") else
              ("通过" + ("（用户豁免 " + str(waived_count) + " 条）" if waived_count else "")
               if review.get("ok") else "未通过：" +
               ("；".join((review.get("issues") or [])[:3])
                or "复核人只给了未通过结论，没有列出具体问题")))
    qc["checks"].append({"name": "AI复核", "ok": bool(review.get("ok")),
                         "detail": detail})
    passed = sum(1 for item in qc["checks"] if item["ok"])
    qc["verdict"] = "PASS" if passed == len(qc["checks"]) else "FAIL"
    qc["summary"] = f"{passed}/{len(qc['checks'])} 项通过"

    old_stats = facts.get("stats") if isinstance(facts.get("stats"), dict) else {}
    stats_out = _display_stats(draft, ledger)
    stats_out["hotspots"] = len(facts.get("hotspots") or [])
    stats_out["discoveries"] = len(facts.get("discoveries") or [])
    stats_out["figures"] = len(manifest.get("figures") or [])
    for key in ("incremental",):
        if key in old_stats:
            stats_out[key] = old_stats[key]
    facts["stats"] = stats_out
    facts["business_review"] = review

    draft_path.write_text(draft, encoding="utf-8")
    _write_json(run_dir / "corrected.json", corrected)
    _write_json(run_dir / "figures_manifest.json", manifest)
    _write_json(run_dir / "s5_facts.json", facts)
    qc["draft_after_sha256"] = _sha256_file(draft_path)
    _write_json(run_dir / "qc_result.json", qc)

    after_sha = _sha256_file(draft_path)
    history = audit.get("history") if isinstance(audit.get("history"), list) else []
    history.append({
        "at": _now(), "by": by, "edits": clean_edits,
        "waivers": clean_waivers, "waive_issues": clean_waive_issues,
        "ledger_updates": ledger_updates or [],
        "waived_review_issues": review.get("waived") or [],
        "draft_before_sha256": before_sha, "draft_after_sha256": after_sha,
        "qc_verdict": qc["verdict"], "qc_summary": qc["summary"],
    })
    _write_json(audit_path, {"run_id": run_id, "updated_at": _now(),
                            "waivers": all_waivers, "history": history[-50:]})

    if qc["verdict"] != "PASS":
        failed = [item["name"] for item in qc["checks"] if not item["ok"]]
        out = S5Outcome(False, current_state,
                        "定点修改后自动质检仍未通过：" + "、".join(failed)
                        + (("；" + "；".join((review.get("issues") or [])[:3]))
                           if review.get("issues") else ""),
                        draft=draft, stats=stats_out, qc=qc, review=review,
                        log=log, run_id=run_id, dir=dir_label, week=week,
                        skeleton=facts.get("skeleton") or [],
                        discoveries=facts.get("discoveries") or [],
                        questions=facts.get("questions") or [])
        return out

    from . import s5_review

    round_no = int(s5_review.load_record(run_dir).get("round") or 0)
    message = ("定点修改后自动质检通过（" + str(qc.get("summary") or "") + "）；改动 "
               + str(len(clean_edits)) + " 处，已重新送审，状态为待审核（第 "
               + str(round_no + 1) + " 轮）。")
    _write_status(run_dir, STATUS_REVIEW, message)
    record = s5_review.open_record(run_dir)
    log.append("定点修改已写入 draft.md；只重跑质检，未重跑扩写")
    return S5Outcome(True, STATUS_REVIEW, message, draft=draft, stats=stats_out,
                     manifest=manifest, profile=profile,
                     figures=manifest.get("figures") or [],
                     hotspots=facts.get("hotspots") or [],
                     log=log, run_id=run_id, dir=dir_label, week=week,
                     discoveries=facts.get("discoveries") or [],
                     skeleton=facts.get("skeleton") or [],
                     questions=facts.get("questions") or [], review=record,
                     qc=qc)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S5 扩写：纪要骨架 + pandasai 发现 + 出图")
    parser.add_argument("--run", required=True, help="运行目录，如 runs/w43")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-ai", action="store_true",
                        help="不调模型，明确报「没有生成成功」且不落盘")
    parser.add_argument("--full", action="store_true",
                        help="全量重跑，不复用当前稿的 findings")
    args = parser.parse_args(argv)

    try:
        outcome = run_s5(args.run, args.run_root, use_ai=not args.no_ai,
                         full=args.full)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "state": "blocked", "message": str(exc)},
                         ensure_ascii=False))
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 只报原因
        print(json.dumps({"ok": False, "state": "blocked", "message": str(exc)},
                         ensure_ascii=False))
        return 1

    if args.json:
        print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
    else:
        for line in outcome.log:
            print("- " + line)
        print()
        print(outcome.message)
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
