"""S6 交付归档：S5 扩写与审核之后的独立阶段。

入口是 S5 审核的 state=已通过：
- 检查审核结论、审核人、时间；没有通过就不归档；
- 交付签名由 S5 审核通过时写入 review_record.json，本步只读取，不再要求用户签名；
- 把 draft.md 排成 docx，连同 md、图表、台账、图表清单、质检说明、审核记录、清单
  一起放进 <运行目录>/deliver/<版本>/；
- 版本只在内容指纹或版式版本变化时递增，同指纹直接返回原版本（幂等），绝不覆盖历史版本；
- 先写临时目录，全部成功后再原子替换；中途失败不留半成品；
- 收尾把 status.json 写成 已交付，updated_by=S6。

质检说明：历史质检已并入 S5。qc_result.json 只有在 draft 哈希一致时才采信；
否则按「本轮无独立质检」写一份说明文件，不伪造 PASS。
"""
from __future__ import annotations

import datetime as dt
import hashlib
from collections import Counter
import json
import os
import platform
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATE_PASSED = "已通过"
STATE_DELIVERED = "已交付"
DELIVER_DIR = "deliver"
MANIFEST_NAME = "manifest.json"
TMP_PREFIX = ".tmp-"
# 改版式时递增：内容没变也会重排出新版本，旧版本保留
LAYOUT_VERSION = "paper-2026-30"

_PARAMS = {
    "amount_tolerance": "50 元与 5% 取大",
    "ratio_tolerance": "5 个百分点",
    "margin_tolerance": "1 个百分点",
    "count_tolerance": "必须完全相等",
    "materiality": "环比绝对值超过 10% 才单列",
    "group_sample_ratio": 3,
}
_LAYOUT = {
    "page": "A4",
    "margin_cm": {"top": 2.54, "bottom": 2.54, "left": 3.17, "right": 3.17},
    "body": "宋体小四，1.5 倍行距，首行缩进 2 字符",
    "heading": "黑体四号加粗，中文序号",
    "table": "三线表，表题居中置上",
    "figure": "居中，图题居中置于图下",
    "header": "右对齐小五",
    "footer": "居中页码",
}
_MIDDLEWARE = (
    "intake.json", "cards.json", "issues.json", "corrected.json", "ledger.json",
    "profile.json", "figures_manifest.json", "qc_result.json", "review_record.json",
    "s5_fixes.json", "waiver.json",
)


@dataclass
class DeliveryOutcome:
    ok: bool
    state: str
    message: str
    run_id: str = ""
    dir: str = ""
    version: str = ""
    path: str = ""
    idempotent: bool = False
    signature: str = ""
    files: list = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    log: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "state": self.state, "message": self.message,
            "run_id": self.run_id, "dir": self.dir, "version": self.version,
            "path": self.path, "idempotent": self.idempotent,
            "signature": self.signature,
            "files": self.files, "manifest": self.manifest, "log": self.log,
        }


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _version_no(name: str) -> int:
    match = re.fullmatch(r"v(\d+)", name)
    return int(match.group(1)) if match else 0


def _existing(deliver_dir: Path) -> list[tuple[int, Path]]:
    if not deliver_dir.is_dir():
        return []
    found = []
    for entry in deliver_dir.iterdir():
        if entry.is_dir() and not entry.name.startswith(TMP_PREFIX):
            number = _version_no(entry.name)
            if number:
                found.append((number, entry))
    return sorted(found)


def _write_status(run_dir: Path, state: str, note: str, **extra: Any) -> None:
    path = run_dir / "status.json"
    data = _read_json(path)
    at = _now()
    history = data.get("history") if isinstance(data.get("history"), list) else []
    history.append({"at": at, "state": state, "updated_by": "S6", "note": note})
    data.update({"state": state, "updated_by": "S6", "at": at, "note": note,
                 "stage": "s6", "history": history[-50:]})
    for key, value in extra.items():
        data[key] = value
    _write_json(path, data)


def _qc_info(run_dir: Path, draft_path: Path) -> dict:
    """qc_result.json 只在 draft 哈希一致时采信；否则明确写「本轮无独立质检」。"""
    qc_path = run_dir / "qc_result.json"
    if not qc_path.is_file():
        return {
            "available": False, "stale": False, "verdict": "",
            "summary": "", "note": "历史质检已并入 S5，本轮无独立质检",
            "payload": {
                "available": False, "verdict": "",
                "note": "历史质检已并入 S5，本轮无独立质检；S5 收尾已替换占位符并核对数字来源。",
            },
        }
    data = _read_json(qc_path)
    current = _sha256(draft_path) if draft_path.is_file() else ""
    after = str(data.get("draft_after_sha256") or data.get("draft_before_sha256") or "")
    fresh = bool(after) and after == current
    if fresh:
        return {
            "available": True, "stale": False,
            "verdict": str(data.get("verdict") or ""),
            "summary": str(data.get("summary") or ""),
            "note": "质检结果与当前稿一致",
            "payload": data,
        }
    return {
        "available": False, "stale": True, "verdict": "",
        "summary": "", "note": "旧质检结果对应别的稿子，已失效（原质检已并入 S5）",
        "payload": {
            "available": False, "stale": True, "verdict": "",
            "note": "旧质检结果对应别的稿子，已失效（原质检已并入 S5）；本轮无独立质检。",
        },
    }


def _fingerprint(run_dir: Path, draft_path: Path, qc: dict,
                 review_path: Path) -> tuple[str, str]:
    """五类内容指纹：主产物、图表、台账、质检结果、审核记录。"""
    entries: list[tuple[str, str]] = []
    entries.append(("draft.md", _sha256(draft_path)))
    fig_dir = run_dir / "figures"
    if fig_dir.is_dir():
        for item in sorted(fig_dir.glob("*")):
            if item.is_file() and item.suffix.lower() in (".png", ".svg"):
                entries.append(("figures/" + item.name, _sha256(item)))
    ledger_path = run_dir / "ledger.json"
    entries.append(("ledger.json", _sha256(ledger_path) if ledger_path.is_file() else "none"))
    entries.append(("qc_result.json", _sha_text(json.dumps(
        qc.get("payload") or {}, ensure_ascii=False, sort_keys=True))))
    entries.append(("review_record.json",
                    _sha256(review_path) if review_path.is_file() else "none"))
    fingerprint_input = "\n".join(
        name + "=" + value for name, value in sorted(entries))
    return _sha_text(fingerprint_input), fingerprint_input


def _tools_hashes(root: Path) -> dict:
    out = {}
    for rel in ("tools/ask_data.py", "tools/text_stats.py"):
        path = root / rel
        if path.is_file():
            out[rel] = _sha256(path)
    fig_dir = root / "tools" / "figure"
    if fig_dir.is_dir():
        for item in sorted(fig_dir.glob("*.py")):
            out["tools/figure/" + item.name] = _sha256(item)
    return out


def _config_hashes(root: Path) -> dict:
    out = {}
    for path in sorted(root.glob("s[0-9]*.txt")):
        if re.fullmatch(r"s[0-9]+\.txt", path.name):
            out[path.name] = _sha256(path)
    spec = root / "规范.md"
    if spec.is_file():
        out["规范.md"] = _sha256(spec)
    return out


def _env() -> dict:
    deps = {}
    try:
        from importlib import metadata

        for name in ("pandas", "numpy", "pandasai", "altair",
                     "vl-convert-python", "python-docx", "matplotlib"):
            try:
                deps[name] = metadata.version(name)
            except Exception:  # noqa: BLE001 - 未装就不记
                continue
    except Exception:  # noqa: BLE001
        deps = {}
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "dependencies": deps,
    }


def _process(facts: dict) -> list[dict]:
    out = []
    for item in facts.get("discoveries") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "")
        out.append({
            "id": str(item.get("id") or ""),
            "question_hash": _sha_text(question)[:16] if question else "",
            "code_hash": str(item.get("code_hash") or ""),
        })
    return out


def _reproduce(run_dir: Path, facts: dict, root: Path) -> dict:
    figures = []
    src_dir = run_dir / "figures" / "src"
    if src_dir.is_dir():
        for item in sorted(src_dir.glob("*")):
            if item.is_file():
                figures.append({
                    "name": item.name,
                    "sha256": _sha256(item),
                    "command": "pipeline/s5_altair.render_chart(spec, out_png=..., script_path=...)",
                })
    prompt = root / "s5.txt"
    return {
        "data": [{"id": item["id"], "question_hash": item["question_hash"],
                  "code_hash": item["code_hash"]}
                 for item in _process(facts)],
        "figures": figures,
        "text": {
            "model": str(facts.get("model") or ""),
            "prompt_sha256": _sha256(prompt) if prompt.is_file() else "",
            "note": "正文由模型生成，不承诺逐字复现；数字与图可复现",
        },
    }


def _week_tag(run: dict, minutes: dict, folder: Path) -> str:
    week = str(minutes.get("week") or "").strip()
    if week:
        return week.replace(" ", "")
    return str(run.get("period_tag") or folder.name)


def _build_docx(draft: Path, out: Path, fig_dir: Path, manifest: Path,
                meta: dict, summary: str, log: list[str]) -> None:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from pipeline import report_docx
    except Exception as exc:  # noqa: BLE001 - 版式模块不可用就明确失败
        raise RuntimeError("report_docx 不可用：" + str(exc)) from exc
    report_docx.build_report(draft, out, fig_dir, manifest,
                             meta=meta, summary=summary,
                             heading_map=meta.get("heading_map") or {})
    log.append("docx 已排版：" + out.name + ("（含摘要）" if summary else ""))


_NUM_RE = re.compile("[0-9][0-9,]*(?:[.][0-9]+)?")


def _numbers(text: str) -> Counter:
    return Counter(item.replace(",", "").rstrip(".") for item in _NUM_RE.findall(text or ""))


def _ai_summary(draft: str, log: list[str], chat_fn=None) -> str:
    """让模型写一段摘要；只允许引用原文，数字必须原样出现在正文里。失败就留空。"""
    env_off = str(os.environ.get("S5_AI_SUMMARY")
                  or os.environ.get("S6_AI_SUMMARY")
                  or os.environ.get("S7_AI_SUMMARY")
                  or os.environ.get("S8_AI_SUMMARY") or "").strip().lower()
    if env_off in ("0", "false", "off", "no"):
        log.append("已关闭 AI 摘要（S5_AI_SUMMARY=0）")
        return ""
    try:
        if chat_fn is None:
            from .llm import chat as chat_fn  # type: ignore
    except Exception as exc:  # noqa: BLE001 - 没有模型就按无摘要排版
        log.append("AI 摘要跳过：" + str(exc))
        return ""
    rules = [
        "下面是一份经营周报正文。请写一段 150–220 字的中文摘要，要求：",
        "1) 只依据正文，不新增事实、不新增数字；",
        "2) 摘要里出现的每个数字都必须原样出现在正文中；",
        "3) 按「整体经营 → 主要发现 → 风险与下周关注」组织；",
        "4) 不写数据质量内容：缺数、缺失、重复、去重、异常记录、负数 / 冲销、",
        "   记录数 / 行数 / 条数 / 天数、POS 上传 / 网络故障、数据核对等；",
        "5) 不写「待核实」「需进一步核实」「是否一致」「疑似」「可能」等不确定表述；",
        "   无法解释的原因不写；",
        "6) 输出 JSON：{summary: 摘要文本}。",
        "",
        "正文：",
        draft,
    ]
    messages = [
        {"role": "system",
         "content": "你是周报编辑，只输出 JSON，不写解释、不写代码块。"},
        {"role": "user", "content": chr(10).join(rules)},
    ]
    try:
        out = chat_fn(messages, timeout=120, max_tokens=800, thinking="disabled")
    except TypeError:
        out = chat_fn(messages, timeout=120, max_tokens=800)
    except Exception as exc:  # noqa: BLE001
        log.append("AI 摘要失败：" + str(exc)[:120])
        return ""
    text = out.get("text") if isinstance(out, dict) else str(out or "")
    try:
        left = text.find("{")
        right = text.rfind("}")
        data = json.loads(text[left:right + 1] if left >= 0 and right > left else text)
        summary = str(data.get("summary") or "").strip()
    except Exception:  # noqa: BLE001
        log.append("AI 摘要不是合法 JSON，跳过")
        return ""
    summary = _trim_summary(summary, 240)
    from . import s5_filters

    cleaned = s5_filters.clean_text(summary)
    if cleaned != summary:
        log.append("AI 摘要已过滤数据质量/待核实表述")
        summary = cleaned
    if not (120 <= len(summary) <= 300):
        log.append("AI 摘要长度不合适（" + str(len(summary)) + " 字），跳过")
        return ""
    sentences = [item for item in re.split(r"[。！？!?]", summary) if item.strip()]
    if len(sentences) > 5:
        log.append("AI 摘要超过 5 句，跳过")
        return ""
    missing = sorted(_numbers(summary) - _numbers(draft), key=len, reverse=True)
    if missing:
        log.append("AI 摘要含正文没有的数字（" + "、".join(missing[:5]) + "），跳过")
        return ""
    log.append("AI 摘要已生成（" + str(len(summary)) + " 字）")
    return summary


def _trim_summary(text: str, limit: int = 240) -> str:
    """保留首句（整体）和末句（风险/下周），中间句按字数择优，不截断句子。"""
    if len(text) <= limit:
        return text
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?])", text)
                 if item.strip()]
    if len(sentences) <= 2:
        return text
    head, tail = sentences[0], sentences[-1]
    chosen = [head]
    if len(head) + len(tail) <= limit:
        for item in sentences[1:-1]:
            if len("".join(chosen)) + len(item) + len(tail) <= limit:
                chosen.append(item)
        chosen.append(tail)
    else:
        for item in sentences[1:]:
            if len("".join(chosen)) + len(item) <= limit:
                chosen.append(item)
    trimmed = "".join(chosen)
    return trimmed if len(trimmed) >= 120 else text


def _core_metrics_table(folder: Path, intake: dict, log: list[str]) -> list[dict]:
    """从源数据生成核心指标表；数字与台账对不上就不放表，避免两套口径。"""
    try:
        from .s5_write import _load_data, _period_info  # noqa: PLC0415 - 延迟导入
    except Exception as exc:  # noqa: BLE001
        log.append("核心指标表跳过：" + str(exc)[:120])
        return []
    try:
        frame, meta = _load_data(folder, intake)
        info = _period_info(frame, intake, folder.name)
    except Exception as exc:  # noqa: BLE001
        log.append("核心指标表跳过：" + str(exc)[:120])
        return []
    if frame.empty or "实收金额" not in frame.columns or "区域" not in frame.columns:
        log.append("核心指标表跳过：数据缺少实收金额或区域列")
        return []
    start, end = info.get("start"), info.get("end")
    if not start or not end:
        return []
    cur = frame[(frame["_date"] >= start) & (frame["_date"] <= end)].copy()
    compare_start, compare_end = info.get("compare_start"), info.get("compare_end")
    prev = (frame[(frame["_date"] >= compare_start) & (frame["_date"] <= compare_end)].copy()
            if compare_start and compare_end else frame.iloc[0:0].copy())
    if cur.empty:
        log.append("核心指标表跳过：本期没有数据")
        return []
    cur["实收金额"] = cur["实收金额"].astype(float)
    if "成本金额" in cur.columns:
        cur["成本金额"] = cur["成本金额"].astype(float)
    if not prev.empty and "实收金额" in prev.columns:
        prev["实收金额"] = prev["实收金额"].astype(float)

    def amount(value) -> str:
        return f"{float(value):,.0f}"

    def change(current: float, previous: float | None) -> str:
        if previous is None or not previous:
            return "—"
        return f"{(current - previous) / previous * 100:+.1f}%"

    def margin(revenue: float, cost: float | None) -> str:
        if cost is None or not revenue:
            return "—"
        return f"{(revenue - cost) / revenue * 100:.1f}%"

    regions = []
    for region, group in cur.groupby("区域"):
        revenue = float(group["实收金额"].sum())
        cost = float(group["成本金额"].sum()) if "成本金额" in group.columns else None
        previous = None
        if not prev.empty and region in set(prev["区域"]):
            previous = float(prev[prev["区域"] == region]["实收金额"].sum())
        regions.append((str(region), revenue, cost, previous))
    regions.sort(key=lambda item: item[1], reverse=True)

    # 与台账的区域实收核对，避免表里和正文两套数
    ledger = _read_json(folder / "ledger.json")
    ledger_values: list[float] = []
    for entry in ledger.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        spec = str(entry.get("spec") or "")
        if "按区域分组" in spec and "实收金额" in spec:
            try:
                ledger_values.append(float(entry.get("value")))
            except (TypeError, ValueError):
                continue
    if ledger_values:
        for _, revenue, _, _ in regions:
            if not any(abs(revenue - value) <= 1.0 for value in ledger_values):
                log.append("核心指标表跳过：区域实收与台账不一致")
                return []
        total = float(cur["实收金额"].sum())
        if abs(total - sum(ledger_values)) > 1.0:
            log.append("核心指标表跳过：全司实收与台账分项和不一致")
            return []

    headers = ["区域", "实收（元）", "较上周"]
    if "成本金额" in cur.columns:
        headers.append("毛利率")
    targets = meta.get("targets") or {}
    if targets:
        headers.append("目标达成率")

    total_revenue = float(cur["实收金额"].sum())
    total_cost = float(cur["成本金额"].sum()) if "成本金额" in cur.columns else None
    total_previous = (float(prev["实收金额"].sum()) if not prev.empty else None)
    total_target = (float(sum(float(value) for value in targets.values()))
                    if targets else None)
    total_row = ["全公司", amount(total_revenue), change(total_revenue, total_previous)]
    if "成本金额" in cur.columns:
        total_row.append(margin(total_revenue, total_cost))
    if targets:
        total_row.append(f"{total_revenue / total_target * 100:.1f}%"
                         if total_target else "—")
    rows = [total_row]
    for region, revenue, cost, previous in regions:
        row = [region, amount(revenue), change(revenue, previous)]
        if "成本金额" in cur.columns:
            row.append(margin(revenue, cost))
        if targets:
            target = targets.get(region)
            row.append(f"{revenue / float(target) * 100:.1f}%" if target else "—")
        rows.append(row)

    note = ("较上周与 " + str(info.get("compare") or "上期") + " 比较；")
    if "成本金额" in cur.columns:
        note += "毛利率 =（实收 − 成本）÷ 实收；"
    if targets:
        note += "目标达成率 = 实收 ÷ 区域目标。"
    else:
        note += "本期数据未提供目标值，不列目标达成率。"
    table = {
        "caption": "表 1　核心指标（" + str(info.get("period") or "") + "）",
        "headers": headers,
        "rows": rows,
        "note": note,
        "source": {"period": info.get("period"), "compare": info.get("compare")},
    }
    log.append("核心指标表已生成（" + str(len(rows)) + " 行）")
    return [table]


_FIG_LINE_RE = re.compile("^图[ 　]*[0-9]+")


def _clean_line(text: str) -> str:
    """去掉过程标记与图号引用；不改数字和事实。"""
    from . import s5_filters

    out = text.replace("【数据发现】", "").strip()
    out = out.replace("数据发现显示，", "").replace("数据发现显示", "")
    if out.startswith("数据显示，") or out.startswith("数据显示"):
        out = out.replace("数据显示，", "", 1).replace("数据显示", "", 1).strip()
    out = s5_filters.strip_figure_refs(out)
    out = re.sub("（见图[ 　]*([0-9]+)）[ 　]*（见图[ 　]*([0-9]+)）",
                 lambda m: "（见图 " + m.group(1) + "、图 " + m.group(2) + "）", out)
    out = re.sub("（见图[ 　]*([0-9]+)）[ 　]*数据", lambda m: "（见图 " + m.group(1) + "）", out)
    out = re.sub("见图[ 　]*([0-9]+)[ 　]*数据", lambda m: "见图 " + m.group(1), out)
    out = re.sub("见图[ 　]*([0-9]+)[ 　]*[。；]?[ 　]*（见图[ 　]*\\1）",
                 lambda m: "见图 " + m.group(1) + "。", out)
    out = out.replace("品类的品类收入", "品类收入")
    out = out.replace("实收占约", "实收约")
    return out.strip()


def _body_lines(text: str) -> tuple[list[str], list[int]]:
    """返回全部行 + 需要润色的正文段落下标（跳过标题、列表、图题、附录、附图）。"""
    lines = text.splitlines()
    skip = False
    indexes: list[int] = []
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            skip = (("数据发现明细" in heading)
                    or ("图表" in heading and "数据" not in heading)
                    or ("未来" in heading))
            continue
        if not stripped or skip:
            continue
        if stripped.startswith("#") or stripped.startswith("|"):
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            continue
        if _FIG_LINE_RE.match(stripped):
            continue
        indexes.append(index)
    return lines, indexes


def _polish_once(texts: list[str], log: list[str], chat_fn, *,
                 minimal: bool = False) -> list[str] | None:
    if minimal:
        rules = [
            "下面是一份经营周报的正文段落（编号从 1 开始）。请只做最小编辑：",
            "1) 删掉「数据发现」「模型」「取数」等过程词，把句子理顺；",
            "2) 不新增、不删事实，不合并、不拆分段落，段落数量必须完全一致；",
            "3) 数字（含千分位）、日期、单位、门店名、人名必须原样保留；",
            "4) 每段长度与原文相差不超过 15%，不要压缩、不要扩写；",
            "5) 输出 JSON：{paragraphs: [第1段, 第2段, ...]}，不要输出其他内容。",
            "",
        ]
        ratio_min, ratio_max = 0.8, 1.18
        label = "AI 正文润色（最小编辑）"
    else:
        rules = [
            "下面是一份经营周报的正文段落（编号从 1 开始）。请逐段做编辑润色：",
            "1) 删掉「数据发现」「模型」「取数」等过程词和口水话，让句子像正式周报；",
            "2) 不新增事实、不删事实、不合并或拆分段落，段落数量必须完全一致；",
            "3) 所有数字（含千分位）、日期、单位、门店名、人名必须原样保留，不得改写；",
            "4) 允许合并同义表达、删掉重复修饰，每段压到原文的 70%–100%，不要写长；",
            "5) 不要用「显著」「大幅」「大约」这类空泛词，直接写数字和事实；",
            "6) 输出 JSON：{paragraphs: [第1段, 第2段, ...]}，不要输出其他内容。",
            "",
        ]
        ratio_min, ratio_max = 0.65, 1.05
        label = "AI 正文润色"
    rules += [str(i + 1) + ". " + item for i, item in enumerate(texts)]
    try:
        out = chat_fn(
            [{"role": "system", "content": "你是中文财经编辑，只输出 JSON。"},
             {"role": "user", "content": chr(10).join(rules)}],
            timeout=180, max_tokens=3000, thinking="disabled", temperature=0)
    except TypeError:
        out = chat_fn(
            [{"role": "system", "content": "你是中文财经编辑，只输出 JSON。"},
             {"role": "user", "content": chr(10).join(rules)}],
            timeout=180, max_tokens=3000, thinking="disabled")
    except Exception as exc:  # noqa: BLE001
        log.append(label + "失败：" + str(exc)[:120])
        return None
    text = out.get("text") if isinstance(out, dict) else str(out or "")
    try:
        left = text.find("{")
        right = text.rfind("}")
        data = json.loads(text[left:right + 1] if left >= 0 else text)
        items = data.get("paragraphs") if isinstance(data, dict) else data
    except Exception:  # noqa: BLE001
        log.append(label + "不是合法 JSON，整批跳过")
        return None
    if not isinstance(items, list) or len(items) != len(texts):
        log.append(label + "段落数不一致，整批跳过")
        return None

    result: list[str] = []
    rejected = 0
    for source, raw in zip(texts, items):
        candidate = re.sub("[ 　]+", " ", str(raw or "")).strip()
        ok = bool(candidate)
        if ok and any(token in candidate for token in ("【数据发现】", "pandasai", "s5_facts")):
            ok = False
        if ok and _numbers(candidate) != _numbers(source):
            ok = False
        if ok:
            ratio = len(candidate) / max(len(source), 1)
            ok = ratio_min <= ratio <= ratio_max
        if not ok:
            rejected += 1
            result.append(source)
        else:
            result.append(candidate)
    if rejected:
        log.append(label + "：保留规则清洗 " + str(rejected) + " 段")
    if rejected == len(texts):
        return None
    log.append(label + "：" + str(len(texts) - rejected) + " 段")
    return result


def _ai_polish(texts: list[str], log: list[str], chat_fn=None) -> list[str] | None:
    """让模型把正文句子理顺，数字必须一一对应；不合格的段落保留规则清洗版。"""
    if not texts:
        return None
    try:
        if chat_fn is None:
            from .llm import chat as chat_fn  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.append("AI 正文润色跳过：" + str(exc))
        return None
    result: list[str] = []
    polished = 0
    for start in range(0, len(texts), 6):
        chunk = texts[start:start + 6]
        part = _polish_once(chunk, log, chat_fn)
        if part is None:
            part = _polish_once(chunk, log, chat_fn, minimal=True)
        if part is None:
            result.extend(chunk)
            continue
        polished += sum(1 for before, after in zip(chunk, part) if before != after)
        result.extend(part)
    if not polished:
        return None
    log.append("AI 正文润色完成：" + str(polished) + " 段，未改 "
               + str(len(texts) - polished) + " 段")
    return result


_DEFAULT_HEADINGS = {
    "纪要骨架与数据补充": "本周经营情况",
    "数据发现明细（pandasai 取数，供核对）": "数据发现明细",
}


def _md_title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _week_title(week: str, fallback: str) -> str:
    match = re.fullmatch(r"(\d{4})-W(\d{1,2})", week or "")
    if match:
        return match.group(1) + " 年第 " + str(int(match.group(2))) + " 周经营周报"
    return fallback


def deliver(run_dir: str | Path, root: str | Path | None = None,
            use_ai: bool = True, chat_fn=None, signature: str = "") -> DeliveryOutcome:
    folder = Path(run_dir)
    signature = re.sub(r"\s+", " ", str(signature or "")).strip()[:80]
    project_root = Path(root) if root else Path(__file__).resolve().parent.parent
    log: list[str] = []

    intake = _read_json(folder / "intake.json")
    if not intake:
        return DeliveryOutcome(False, "未知", "找不到 intake.json，先跑完 S1。")
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
    waivers = []
    for waiver in intake.get("waived") or []:
        if not isinstance(waiver, dict):
            continue
        items = [str(item).strip() for item in (waiver.get("items") or [])
                 if str(item).strip()]
        if not items:
            continue
        waivers.append({
            "at": str(waiver.get("at") or ""),
            "by": str(waiver.get("by") or ""),
            "items": items,
            "reason": str(waiver.get("reason") or ""),
        })
    run_id = str(run.get("period_tag") or folder.name)
    dir_label = str(run.get("dir") or folder.name)
    status = _read_json(folder / "status.json")
    state = str(status.get("state") or "")
    record = _read_json(folder / "review_record.json")

    # ---- 前置检查：只认已通过（已交付只用于幂等回放）
    if state not in (STATE_PASSED, STATE_DELIVERED):
        return DeliveryOutcome(False, state or "未知",
                         "交付只接受已通过的稿子；当前状态：" + (state or "未知")
                         + "。先在 S5 完成审核。", run_id=run_id, dir=dir_label)
    if str(record.get("state") or "") != STATE_PASSED or \
            str(record.get("decision") or "") != "pass":
        return DeliveryOutcome(False, state, "审核记录不是通过，不能归档。",
                         run_id=run_id, dir=dir_label)
    reviewer = str(record.get("reviewer") or "").strip()
    at = str(record.get("at") or "").strip()
    if not reviewer or not at:
        return DeliveryOutcome(False, state, "审核记录缺少审核人或时间，不能归档。",
                         run_id=run_id, dir=dir_label)

    # 交付签名在 S5 审核通过时确认；这里只读，不再向用户要一次
    if not signature:
        signature = re.sub(
            r"\s+", " ",
            str(record.get("signature") or record.get("reviewer") or "")).strip()[:80]

    draft_path = folder / "draft.md"
    ledger_path = folder / "ledger.json"
    manifest_path = folder / "figures_manifest.json"
    review_path = folder / "review_record.json"
    waiver_path = folder / "waiver.json"
    for path, label in ((draft_path, "draft.md"), (ledger_path, "ledger.json"),
                        (manifest_path, "figures_manifest.json"),
                        (review_path, "review_record.json")):
        if not path.is_file():
            return DeliveryOutcome(False, state, "缺少 " + label + "，不能归档。",
                             run_id=run_id, dir=dir_label)

    fig_manifest = _read_json(manifest_path)
    figures = [item for item in (fig_manifest.get("figures") or [])
               if isinstance(item, dict)]
    if not figures:
        return DeliveryOutcome(False, state, "图表清单为空，不能归档。",
                         run_id=run_id, dir=dir_label)
    missing = []
    for item in figures:
        name = str(item.get("name") or "")
        number = item.get("number")
        base = name or ("fig" + str(number).zfill(2))
        for suffix in (".png", ".svg"):
            if not (folder / "figures" / (base + suffix)).is_file():
                missing.append(base + suffix)
    if missing:
        return DeliveryOutcome(False, state, "缺图：" + "、".join(missing) + "，不能归档。",
                         run_id=run_id, dir=dir_label)

    # ---- 质检：一致才采信，否则写「本轮无独立质检」说明
    qc = _qc_info(folder, draft_path)
    if qc["available"] and str(qc["verdict"] or "").upper() not in ("", "PASS"):
        return DeliveryOutcome(False, state, "质检结论不是 PASS，不能归档："
                         + str(qc["verdict"] or ""),
                         run_id=run_id, dir=dir_label)

    facts = _read_json(folder / "s5_facts.json")
    content_hash, fingerprint_input = _fingerprint(folder, draft_path, qc, review_path)
    input_hashes = {str(item.get("name") or ""): str(item.get("sha256") or "")
                    for item in (intake.get("files") or []) if isinstance(item, dict)}

    deliver_dir = folder / DELIVER_DIR
    existing = _existing(deliver_dir)
    render_key = _sha_text(content_hash + "|layout=" + LAYOUT_VERSION)[:16]
    same = None
    same_content = None
    for number, path in existing:
        data = _read_json(path / MANIFEST_NAME)
        if str(data.get("content_fingerprint") or "") != content_hash:
            continue
        if same_content is None:
            same_content = (number, path, data)
        if str(data.get("layout_version") or "") == LAYOUT_VERSION:
            same = (number, path, data)
            break
    if same is not None:
        number, path, old = same
        note = "内容未变，返回原版本 " + path.name
        old_inputs = {str(item.get("name") or ""): str(item.get("sha256") or "")
                      for item in (old.get("inputs") or []) if isinstance(item, dict)}
        changed_inputs = [name for name, value in input_hashes.items()
                          if old_inputs.get(name) and old_inputs.get(name) != value]
        if changed_inputs:
            note += "；输入已更新、内容一致（" + "、".join(changed_inputs) + "）"
            old["note"] = "输入已更新、内容一致：" + "、".join(changed_inputs)
            _write_json(path / MANIFEST_NAME, old)
        if signature:
            note += "；交付签名 " + signature
        _write_status(folder, STATE_DELIVERED, note, version=path.name,
                      deliver_path=str(path), idempotent=True)
        log.append(note)
        return DeliveryOutcome(True, STATE_DELIVERED, "已交付：" + path.name + "（" + note + "）",
                         run_id=run_id, dir=dir_label, version=path.name,
                         path=str(path), idempotent=True, signature=signature,
                         files=[{"name": item.get("name"), "sha256": item.get("sha256"),
                                 "bytes": item.get("bytes")}
                                for item in old.get("files") or []],
                         manifest=old, log=log)

    if state == STATE_DELIVERED and same_content is None:
        return DeliveryOutcome(False, state,
                         "已交付，但内容与已归档版本不一致；需重新走 S5 审核后再归档。",
                         run_id=run_id, dir=dir_label)

    version_no = (existing[-1][0] + 1) if existing else 1
    version = "v" + str(version_no)
    week_tag = _week_tag(run, minutes, folder)
    base_name = "经营周报_" + week_tag + "_" + version
    draft_text = draft_path.read_text(encoding="utf-8")
    title = (_md_title(draft_text)
             or str(minutes.get("title") or "").strip()
             or _week_title(week_tag, "经营周报"))
    period = ("统计周期 " + str(minutes.get("period_start") or "") + " 至 "
              + str(minutes.get("period_end") or "")).strip()
    company = str(minutes.get("company") or "").strip()
    header = (company + " · " if company else "") + title
    summary = _ai_summary(draft_text, log, chat_fn) if use_ai else ""
    heading_map = dict(_DEFAULT_HEADINGS)
    docx_meta = {
        "title": title,
        "company": company,
        "subtitle": period,
        "period": period.replace("统计周期 ", ""),
        "reviewer": reviewer,
        "version": version,
        "date": dt.date.today().isoformat(),
        "header": header,
        "heading_map": heading_map,
        "future": [item for item in (facts.get("future") or [])
                   if isinstance(item, dict)],
    }

    deliver_dir.mkdir(parents=True, exist_ok=True)
    tmp = deliver_dir / (TMP_PREFIX + str(os.getpid()) + "-"
                         + dt.datetime.now().strftime("%Y%m%d%H%M%S%f"))
    try:
        tmp.mkdir(parents=True)
        # 正文：规则清洗 + 可选 AI 润色；数字由代码校验，原稿另存备查
        lines, body_indexes = _body_lines(draft_text)
        before = [lines[i].strip() for i in body_indexes]
        cleaned = [_clean_line(item) for item in before]
        final = cleaned
        if use_ai and cleaned:
            polished = _ai_polish(cleaned, log, chat_fn)
            if polished:
                final = [_clean_line(item) for item in polished]
        for index, value in zip(body_indexes, final):
            lines[index] = value
        report_text = chr(10).join(lines)
        from . import report_docx as _rd, s6_layout, typeset_docx

        sections = []
        for sec in _rd._parse(report_text)[1]:
            heading = str(sec.get("heading") or "")
            if ("数据发现明细" in heading or heading == "图表" or "未来" in heading):
                continue
            blocks = [{"type": b["type"], "text": b["text"]}
                      for b in (sec.get("blocks") or [])
                      if b.get("type") in ("p", "bullet")]
            if blocks:
                sections.append({"heading": heading, "blocks": blocks})
        future_blocks = []
        for item in docx_meta.get("future") or []:
            text = str(item.get("fixed") or item.get("original") or "").strip()
            if text:
                future_blocks.append({"type": "bullet", "text": text})
        if future_blocks:
            sections.append({"heading": "未来事项", "blocks": future_blocks})

        layout_figures = []
        for item in figures:
            name = str(item.get("name") or ("fig" + str(item.get("number")).zfill(2)))
            layout_figures.append({
                "number": int(item.get("number")),
                "caption": str(item.get("caption") or item.get("title")
                               or ("图 " + str(item.get("number")))),
                "path": "figures/" + name + ".png",
                "source_path": str(folder / "figures" / (name + ".png")),
                "shape": str(item.get("shape") or ""),
            })

        layout_meta = {
            "title": title, "subtitle": period,
            "period": period.replace("统计周期 ", ""),
            "company": company or "—", "reviewer": reviewer,
            "version": version, "date": docx_meta["date"],
        }
        core_tables = _core_metrics_table(folder, intake, log)
        layout = s6_layout.plan_layout(sections, layout_figures, layout_meta,
                                       headings=heading_map, use_ai=use_ai,
                                       chat_fn=chat_fn, log=log,
                                       summary=summary, tables=core_tables)
        plan_headings = (layout.get("plan") or {}).get("headings") or heading_map
        for index, line in enumerate(lines):
            stripped = line.strip()
            for old, new in plan_headings.items():
                if ("数据发现明细" in old or old == "图表" or "未来" in old):
                    continue
                if stripped == "## " + old:
                    lines[index] = "## " + new
                    break
        report_text = chr(10).join(lines)
        md_out = tmp / (base_name + ".md")
        md_out.write_text(report_text, encoding="utf-8")
        (tmp / "draft_original.md").write_text(draft_text, encoding="utf-8")
        _write_json(tmp / "polish_map.json", {
            "applied": final is not cleaned,
            "count": len(body_indexes),
            "items": [{"before": src, "after": dst}
                      for src, dst in zip(before, final)],
        })

        # 图先落盘，Pandoc 需要相对路径
        fig_out = tmp / "figures"
        fig_out.mkdir()
        for item in figures:
            base = str(item.get("name") or ("fig" + str(item.get("number")).zfill(2)))
            for suffix in (".png", ".svg"):
                src = folder / "figures" / (base + suffix)
                shutil.copyfile(src, fig_out / src.name)

        docx_out = tmp / (base_name + ".docx")
        engine = "python-docx"
        layout_path = tmp / "layout.md"
        if layout.get("markdown"):
            layout_path.write_text(layout["markdown"], encoding="utf-8")
        if layout.get("markdown") and typeset_docx.available():
            try:
                info = typeset_docx.typeset(layout_path, docx_out)
                engine = str(info.get("engine") or "pandoc")
                # 主产物 md 与 docx 同源：排版稿同时作为 Markdown 备份
                md_out.write_text(layout["markdown"], encoding="utf-8")
                log.append("Pandoc 排版完成：图 " + str(info.get("images")) + " 张")
            except Exception as exc:  # noqa: BLE001 - 失败回退当前模板
                log.append("Pandoc 排版失败，回退 python-docx：" + str(exc)[:160])
        if engine != "pandoc":
            docx_meta["heading_map"] = plan_headings
            try:
                _build_docx(md_out, docx_out, folder / "figures", manifest_path,
                            docx_meta, summary, log)
            except Exception as exc:  # noqa: BLE001 - 排版失败按明确失败返回
                raise RuntimeError("Word 生成失败：" + str(exc)) from exc
        _write_json(tmp / "layout.json", {
            "engine": engine,
            "layout_version": LAYOUT_VERSION,
            "source": layout.get("source"),
            "errors": layout.get("errors") or [],
            "pages_estimate": layout.get("pages_estimate") or 0,
            "summary_included": bool(summary),
            "figures_included": [int(item.get("number"))
                                 for item in (layout.get("plan") or {}).get("figures") or []
                                 if isinstance(item, dict)],
            "core_table": bool(core_tables),
            "plan": layout.get("plan") or {},
        })

        # 台账 / 图表清单 / 质检说明 / 审核记录
        shutil.copyfile(ledger_path, tmp / "ledger.json")
        shutil.copyfile(manifest_path, tmp / "figures_manifest.json")
        _write_json(tmp / "qc_result.json", qc["payload"])
        shutil.copyfile(review_path, tmp / "review_record.json")
        if waiver_path.is_file():
            shutil.copyfile(waiver_path, tmp / "waiver.json")

        files = []
        for item in sorted(tmp.rglob("*")):
            if item.is_file() and item.name != MANIFEST_NAME:
                files.append({"name": item.relative_to(tmp).as_posix(),
                              "sha256": _sha256(item),
                              "bytes": item.stat().st_size})
        middleware = {}
        for name in _MIDDLEWARE:
            path = folder / name
            if path.is_file():
                middleware[name] = _sha256(path)
        manifest_doc = {
            "run_id": run_id,
            "version": version,
            "week": week_tag,
            "delivered_at": _now(),
            "state": STATE_DELIVERED,
            "idempotent": False,
            "inputs": [{"name": str(item.get("name") or ""),
                        "path": str(item.get("path") or ""),
                        "sha256": str(item.get("sha256") or ""),
                        "bytes": int(item.get("size") or 0)}
                       for item in (intake.get("files") or []) if isinstance(item, dict)],
            "config": _config_hashes(project_root),
            "tools": _tools_hashes(project_root),
            "env": _env(),
            "params": dict(_PARAMS),
            "process": _process(facts),
            "middleware": middleware,
            "waivers": waivers,
            "qc": {"available": qc["available"], "stale": qc["stale"],
                   "verdict": qc["verdict"], "summary": qc["summary"],
                   "note": qc["note"]},
            "review": {"reviewer": reviewer, "signature": signature,
                       "decision": "pass", "at": at,
                       "comments": len(record.get("comments") or []),
                       "round": int(record.get("round") or 0)},
            "delivery": {"signature": signature, "at": _now()} if signature else {},
            "content_fingerprint": content_hash,
            "render_fingerprint": render_key,
            "layout_version": LAYOUT_VERSION,
            "layout_engine": engine,
            "layout_source": layout.get("source") or "",
            "layout_checked": not (layout.get("errors") or []),
            "pages_estimate": layout.get("pages_estimate") or 0,
            "figures_included": [int(item.get("number"))
                                 for item in (layout.get("plan") or {}).get("figures") or []
                                 if isinstance(item, dict)],
            "summary_included": bool(summary),
            "core_table": bool(core_tables),
            "fingerprint_input": fingerprint_input,
            "layout": dict(_LAYOUT),
            "reproduce": _reproduce(folder, facts, project_root),
            "files": files,
            "hand_filled": False,
        }
        _write_json(tmp / MANIFEST_NAME, manifest_doc)
        final = deliver_dir / version
        if final.exists():
            raise RuntimeError("版本目录已存在，拒绝覆盖：" + str(final))
        # Windows 上杀毒/索引可能短暂占用临时目录，重试几次再报错
        last_error: OSError | None = None
        for attempt in range(3):
            try:
                os.replace(tmp, final)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.3)
        if last_error is not None:
            raise last_error
        log.append("交付包已落盘：" + str(final))
    except Exception as exc:  # noqa: BLE001 - 清理临时目录，保留旧版本
        shutil.rmtree(tmp, ignore_errors=True)
        return DeliveryOutcome(False, state, "没有交付成功：" + str(exc),
                         run_id=run_id, dir=dir_label, log=log)

    note = "已交付：" + version + "，审核人 " + reviewer
    if waivers:
        note += "，S1 人工豁免 " + str(len(waivers)) + " 条随包归档"
    if signature:
        note += "，交付签名 " + signature
    _write_status(folder, STATE_DELIVERED, note, version=version,
                  deliver_path=str(final), idempotent=False)
    return DeliveryOutcome(True, STATE_DELIVERED,
                     note + "；交付包 " + str(final),
                     run_id=run_id, dir=dir_label, version=version,
                     path=str(final), idempotent=False, signature=signature,
                     files=files, manifest=manifest_doc, log=log)


def read_status(run_dir: str | Path) -> dict:
    folder = Path(run_dir)
    intake = _read_json(folder / "intake.json")
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    status = _read_json(folder / "status.json")
    versions = []
    for number, path in _existing(folder / DELIVER_DIR):
        data = _read_json(path / MANIFEST_NAME)
        versions.append({
            "version": path.name,
            "delivered_at": str(data.get("delivered_at") or ""),
            "content_fingerprint": str(data.get("content_fingerprint") or ""),
            "files": len(data.get("files") or []),
            "path": str(path),
        })
    latest = _read_json(folder / DELIVER_DIR / versions[-1]["version"] / MANIFEST_NAME) \
        if versions else {}
    return {
        "ok": True,
        "run_id": str(run.get("period_tag") or folder.name),
        "state": str(status.get("state") or ""),
        "note": str(status.get("note") or ""),
        "at": str(status.get("at") or ""),
        "versions": versions,
        "manifest": latest,
    }
