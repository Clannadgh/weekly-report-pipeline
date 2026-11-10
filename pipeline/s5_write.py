"""S5 扩写：纪要骨架逐句保留，由 pandasai 做数据发现并补肉、出图、落盘。

分工（与前面各步一以贯之）：
- GLM / pandasai 只做「发现/取数/算数/选题」与「把发现写成补充句」；
- 骨架逐句进正文、台账键值、[[f:key]] 占位符、不变量校验、哈希与落盘全由代码做。

两条不可逾越的底线（不是格式规制）：
1. corrected.json 里非 future/blocked 的 items[].fixed 必须逐字出现在 draft.md，
   一字不改、不删、不重组；blocked 只写「原因待核实」，future 原句不进本周报告；
2. 正文里每个 [[f:key]] 都要能在 ledger.json 里找到，台账条目带 spec + code_hash。

已去掉：规范.md 的硬性校验、固定六节模板、兜底稿、字数裁剪机、禁用词替换、
硬编码指标与固定两张图。图数、章节形态由发现结果决定。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
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
            "review": self.review,
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
                 "ledger.json", "status.json", "profile.json"):
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
    dup = int(df.duplicated("记录ID", keep="first").sum()) if df["记录ID"].notna().any() else 0
    df = df.drop_duplicates("记录ID", keep="first").copy()

    region_source = "源数据"
    if "区域" not in df.columns:
        merged = False
        for name, sheet in extras.items():
            if "区域" in sheet.columns and ("门店编码" in sheet.columns or "门店名称" in sheet.columns):
                keys = [key for key in ("门店编码", "门店名称") if key in df.columns and key in sheet.columns]
                if keys:
                    df = df.merge(sheet[keys + ["区域"]].drop_duplicates(keys), on=keys, how="left")
                    merged = True
                    region_source = "门店信息表"
                    break
        if not merged:
            df["区域"] = df["门店名称"].map(_city_region)
            region_source = "按门店城市推断"
    df["区域"] = df["区域"].fillna("其他").replace("", "其他")

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
    for name, sheet in extras.items():
        if "区域" in sheet.columns and ("实收目标(元)" in sheet.columns or "目标金额" in sheet.columns):
            target_col = "实收目标(元)" if "实收目标(元)" in sheet.columns else "目标金额"
            for _, row in sheet.iterrows():
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

    meta = {
        "files": [path.name for path in files],
        "raw_rows": raw_rows,
        "duplicates": dup,
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
        "figures": [{"number": f["number"], "title": f["title"], "keys": f["keys"]}
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
        "4. 不要输出章节标题、表格、图号引用（程序补「见图 N」）、Markdown，只输出 JSON。\n"
        "5. 不得新增纪要里没有的原因；解释不足时写「原因待核实」。\n\n"
        '输出结构：{"supplements":{"M01":"..."},"standalone":["..."],"next_week":["..."]}\n\n'
        "输入：\n" + json.dumps(model, ensure_ascii=False, indent=1)
    )


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


def _assemble_draft(info: dict, skeleton_items: list[dict], blocked_items: list[dict],
                    future_items: list[dict], writing: dict, figures: list[dict],
                    findings: list[dict], key_map: dict, ledger_keys: set[str],
                    cards_actions: list[dict]) -> str:
    """代码装配：骨架 fixed 逐字 → 同主题补充句 → 图引用；再补发现明细 / 图表 / 下周关注。"""
    lines: list[str] = ["# " + _report_title(info), "", "## 纪要骨架与数据补充", ""]
    supplements = writing.get("supplements") or {}
    for item in skeleton_items:
        fixed = str(item.get("fixed") or "")
        if fixed:
            lines.append(fixed)          # 一字不改、不裁剪、不替换、不 strip
            lines.append("")
        sid = str(item.get("id") or "")
        supp = str(supplements.get(sid) or "").strip()
        if supp:
            supp = _sanitize_placeholders(supp, ledger_keys)
            cited = [str(fig["number"]) for fig in figures
                     if any(key in supp for key in (fig.get("keys") or []))]
            ref = "".join("（见图 " + number + "）" for number in cited)
            lines.append("【数据发现】" + supp + ref)
            lines.append("")

    if blocked_items:
        lines += ["## 待核实", ""]
        for _item in blocked_items:
            lines.append("原因待核实")   # s5.txt:10：blocked 原句不扩写、不进正文
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
            lines.append("（见图 " + str(fig["number"]) + "）" + fig["title"] + "。")
            lines.append("")

    next_items = [str(item).strip() for item in (writing.get("next_week") or [])
                 if str(item).strip()]
    for action in cards_actions[:3]:
        text = str(action.get("item") or "").strip()
        if text and text not in next_items:
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
    runnable = [q for q in plan_questions if str(q.get("id")) in runnable_ids]
    reuse_ids = set(diff["reused_ids"])
    reuse = [f for f in old_findings if str(f.get("id")) in reuse_ids]
    log.append("增量扩写：重算 " + str(len(runnable)) + " 条，复用 "
               + str(len(reuse)) + " 条，移除 "
               + str(len(diff["removed_ids"])) + " 条")
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

    # 画像先算在内存里，落盘推到成功之后，保证失败路径不落交付件。
    # 增量时数据没变，直接复用上一版 profile.json，省一次全表扫描。
    profile_path = run_dir / "profile.json"
    if inc is not None and profile_path.is_file():
        profile = _read_json(profile_path)
        log.append("画像复用上一版 profile.json")
    else:
        profile = _profile(frame, meta, info)
        log.append("画像已计算（" + ("ok" if profile.get("ok") else "失败") + "）")

    # 发现层：pandasai 生成并执行 pandas 代码，单步问句
    findings: list[dict] = []
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
        reuse_findings = inc["reuse"]
        new_findings: list[dict] = []
        if runnable:
            try:
                from . import s5_discover
                disc = s5_discover.discover(run_dir, frame, intake, profile,
                                            skeleton_items, questions=runnable)
                new_findings = [item for item in (disc.get("findings") or [])
                                if isinstance(item, dict)]
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
        log.append("增量合并后可用发现 " + str(len(findings)) + " 条")
    else:
        try:
            from . import s5_discover
            disc = s5_discover.discover(run_dir, frame, intake, profile, skeleton_items,
                                        questions=plan_questions)
            findings = [item for item in (disc.get("findings") or [])
                        if isinstance(item, dict)]
            disc_questions = [item for item in (disc.get("questions") or [])
                              if isinstance(item, dict)]
            questions_source = str(disc.get("source") or questions_source)
            log.extend(str(line) for line in (disc.get("log") or []))
            disc_model = str(disc.get("model") or "")
            log.append("pandasai 发现 " + str(len(findings)) + " 条"
                       + ("（模型 " + disc_model + "）" if disc_model else ""))
        except Exception as exc:  # noqa: BLE001
            return _fail("数据发现阶段异常：" + str(exc))

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
        writing = {
            "supplements": data.get("supplements") if isinstance(data.get("supplements"), dict) else {},
            "standalone": data.get("standalone") if isinstance(data.get("standalone"), list) else [],
            "next_week": data.get("next_week") if isinstance(data.get("next_week"), list) else [],
        }
        log.append("补充句模型返回已解析")
    except Exception as exc:  # noqa: BLE001
        return _fail("补充句生成失败：" + str(exc))
    actions = cards_doc.get("actions")
    if isinstance(actions, list):
        cards_actions = [item for item in actions if isinstance(item, dict)]

    # 装配正文
    text = _assemble_draft(info, skeleton_items, blocked_items, future_items, writing,
                           manifest["figures"], findings, key_map, ledger_keys, cards_actions)

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

    from . import s5_review

    round_no = int(s5_review.load_record(run_dir).get("round") or 0)
    message = ("骨架 " + str(len(skeleton_items)) + " 句逐字保留，数据发现 "
               + str(len(findings)) + " 条，" + str(len(manifest["figures"]))
               + " 张图，正文 " + str(stats_out.get("chars_body_rendered") or "—")
               + " 字；占位符已替换，已自动送审，状态为待审核（第 "
               + str(round_no + 1) + " 轮）。")
    if incremental:
        message += ("增量：重算 " + str(recomputed) + " 条，复用 "
                    + str(reused) + " 条"
                    + ("，移除 " + str(removed) + " 条" if removed else ""))
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
