"""S2 纪要拆卡：把纪要拆成一张张可核对的卡片。

只整理，不判断对错、不计算数字、不改写原句。
模型负责「读句子、拆卡片」，代码负责校验（原话必须逐字出自纪要、字段枚举合法、
默认值补齐、统计数重算）。落盘到 <run.dir>/cards.json，S3 只读该文件。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .approvals import clear as clear_approvals
from .llm import LLMError, chat, get_config
from .models import (
    Card, CardAction, CardEntry, CardFuture, CardsResult, CardStats,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUN_ROOT = PROJECT_ROOT / "runs"

METRICS = ("实收", "数量", "毛利率", "目标达成率", "退货", "渠道", "其他")
ENTITY_TYPES = ("区域", "门店", "品类", "商品", "全司", "未知")
EXPECTS = ("持平", "上升", "下降", "具体数值", "百分比", "说不清")
CHECKABLE = ("能", "不能", "不确定")

# 单位默认值：纪要没写单位时按指标给，并在 note 注明
UNIT_DEFAULT = {
    "实收": "元", "数量": "件", "毛利率": "%", "目标达成率": "%",
    "退货": "笔", "渠道": "%", "其他": "未定",
}

MAX_MINUTES_CHARS = 12000

SYSTEM = """你是纪要拆卡器。把一份经营周报的会议纪要拆成一张张卡片，供下一步核对数据。

只整理，不判断对错，不计算任何数字，不改写原句。

【切分】
按段落读，记下每段序号、区域、责任人。每段按句号、问号、叹号、分号切句，原句照抄。

【拆卡】
一句话说了几件事就拆成几张卡片，每张带同一句原话。每张填：
- quote：逐字照抄原句，一个字都不能改
- entity：区域/门店/品类/商品/全司的具体名称；说不清写「不确定」
- entity_type：区域/门店/品类/商品/全司/未知
- metric：实收/数量/毛利率/目标达成率/退货/渠道/其他
- unit：元/%/件/笔；纪要没写留空字符串，由程序按指标补默认
- expect：持平/上升/下降/具体数值/百分比/说不清
- target_value：只搬运原句里已经出现的数字，逐字照搬（如 "35.2%"、"17.7 万元"）；
  没有就填 null。不换算、不四舍五入、不反推
- checkable：能/不能/不确定
  能 = 带数字、方向、比例、排名的结论句，且 metric 在枚举内且不为「其他」
  不能 = 原因解释、背景、评价、未来计划、数据里没有的指标
  判断不了写「不确定」，不要硬归类

  判定示例（照此尺度判，不要过度保守）：
  - 「当日实收约 5,100 元」→ metric=实收, expect=具体数值, checkable=能
  - 「退货处理 15 份」→ metric=退货, expect=具体数值, checkable=能
  - 「本周销售较上周下降 12%」→ metric=实收, expect=下降, checkable=能
  - 「华东区域排名第一」→ metric=实收, expect=具体数值, checkable=能
  - 「销售较上周普遍下降」（无数值）→ checkable=不能
  - 「客流明显回升」（无数字、指标不在枚举）→ metric=其他, checkable=不能
  - 「计划下周开展第二杯半价活动」→ 归 future，不拆卡

  只要句子里有可核对的数字或明确方向且指标能落到枚举，就判「能」。
- para：段落序号（从 1 开始）
- sentence：该段内的句子序号（从 1 开始）
- source：出处，格式「第 X 段第 Y 句 · 区域 · 责任人」
- note：需要说明的默认值或判断依据，没有就留空

【规则】
- 说不清是哪个区域或门店 → 写「不确定」，不要按段落归属去猜
- 日期解析不出 → 写「解析不出」，不参与本期核对
- 没有原话支撑的内容 → 不收录
- 「下周将/计划/预计」这类话归到 future，保留原文，不改写成已完成
- 动作表整表读入 actions，缺项留空，不补

【输出】只输出一个 JSON 对象，不要解释、不要 markdown 代码块：
{
  "entries": [{"no": 1, "region": "", "owner": "", "para": 1, "text": "整段原文"}],
  "cards": [{"quote": "", "entity": "", "entity_type": "", "metric": "", "unit": "",
             "expect": "", "target_value": null, "checkable": "",
             "para": 1, "sentence": 1, "source": "", "note": ""}],
  "actions": [{"item": "", "owner": "", "due": "", "source": ""}],
  "future": [{"text": "", "source": ""}]
}
"""


# ---------------------------------------------------------------- 工具

def _compact(text: str) -> str:
    """去掉所有空白与常见标点差异，只用于比对原话是否出自纪要。"""
    return re.sub(r"[\s\u3000]+", "", str(text or ""))


def _extract_json(text: str) -> dict:
    body = text.strip()
    fence = chr(96) * 3
    if body.startswith(fence):
        body = body[len(fence):]
        body = re.sub(r"^[a-zA-Z]*\s*", "", body)
        body = body.rstrip()
        if body.endswith(fence):
            body = body[: -len(fence)]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    return json.loads(body[start:end + 1])


def _load_intake(run_dir: Path) -> dict:
    path = run_dir / "intake.json"
    if not path.exists():
        raise FileNotFoundError("找不到 intake.json，先跑完 S1：" + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def _minutes_file(run_dir: Path, intake: dict) -> Path:
    """原始纪要文件：优先用 inputs/ 里的副本，其次用 intake.json 记录的路径。"""
    for info in intake.get("files") or []:
        if info.get("role") != "纪要":
            continue
        rel = info.get("path") or ""
        for candidate in (run_dir / rel, run_dir / "inputs" / info.get("name", ""),
                          Path(info.get("path") or "")):
            if str(candidate) and candidate.is_file():
                return candidate
    raise FileNotFoundError("找不到纪要原文，intake.json 里没有可用的 role=纪要 文件路径")


def _read_minutes(path: Path) -> str:
    if path.suffix.lower() in (".docx", ".doc"):
        from .readers import _docx_text
        return _docx_text(path)
    from .readers import _plain_text
    return _plain_text(path)


def _period_text(start: str, end: str) -> str:
    if not start or not end:
        return ""
    return start + " 至 " + end


# ---------------------------------------------------------------- 校验

def _validate(raw: dict, minutes_text: str, period: str, compare_period: str,
              result: CardsResult) -> None:
    flat = _compact(minutes_text)

    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        result.entries.append(CardEntry(
            no=int(item.get("no") or len(result.entries) + 1),
            region=str(item.get("region") or "").strip(),
            owner=str(item.get("owner") or "").strip(),
            para=int(item.get("para") or 0),
            text=text,
        ))

    for item in raw.get("cards") or []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote:
            result.dropped.append("卡片缺原话，已丢弃")
            continue
        if _compact(quote) not in flat:
            result.dropped.append("原话与纪要不一致，已丢弃：" + quote[:40])
            continue

        metric = str(item.get("metric") or "").strip()
        if metric not in METRICS:
            metric = "其他"
        entity_type = str(item.get("entity_type") or "").strip()
        if entity_type not in ENTITY_TYPES:
            entity_type = "未知"
        expect = str(item.get("expect") or "").strip()
        if expect not in EXPECTS:
            expect = "说不清"
        checkable = str(item.get("checkable") or "").strip()
        if checkable not in CHECKABLE:
            checkable = "不确定"

        note_parts: list[str] = []
        given_note = str(item.get("note") or "").strip()
        if given_note:
            note_parts.append(given_note)

        unit = str(item.get("unit") or "").strip()
        if not unit:
            unit = UNIT_DEFAULT[metric]
            note_parts.append("单位按指标默认")

        target = item.get("target_value")
        target_value = None
        if target is not None and str(target).strip():
            target_value = str(target).strip()
            if _compact(target_value) not in _compact(quote):
                result.dropped.append("目标值不在原句里，已置空：" + target_value)
                target_value = None

        # 自检 2：能对数据的卡片，指标和期望不能是「其他」「说不清」
        if checkable == "能" and (metric == "其他" or expect == "说不清"):
            checkable = "不确定"
            note_parts.append("指标或期望说不清，降级为不确定")

        result.cards.append(Card(
            id="", quote=quote,
            entity=str(item.get("entity") or "不确定").strip() or "不确定",
            entity_type=entity_type, metric=metric, unit=unit,
            period=period, compare_period=compare_period,
            expect=expect, target_value=target_value, checkable=checkable,
            para=int(item.get("para") or 0), sentence=int(item.get("sentence") or 0),
            source=str(item.get("source") or "").strip(),
            note="；".join(note_parts),
        ))

    for index, card in enumerate(result.cards, 1):
        card.id = "M" + str(index).zfill(2)

    for item in raw.get("actions") or []:
        if not isinstance(item, dict):
            continue
        fields = {key: str(item.get(key) or "").strip()
                  for key in ("item", "owner", "due", "source")}
        if not any(fields.values()):
            continue
        result.actions.append(CardAction(**fields))

    for item in raw.get("future") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        result.future.append(CardFuture(text=text,
                                        source=str(item.get("source") or "").strip()))


def _recount(result: CardsResult) -> None:
    """统计数一律重算，不信任模型给的 stats。"""
    result.stats = CardStats(
        cards=len(result.cards),
        checkable=sum(1 for card in result.cards if card.checkable == "能"),
        unknown_entity=sum(1 for card in result.cards if card.entity == "不确定"),
        unresolved_date=sum(1 for card in result.cards if card.period == "解析不出"),
        actions=len(result.actions),
    )


# ---------------------------------------------------------------- 主流程

def run_s2(run_dir: str | Path, run_root: str | Path | None = None,
           use_ai: bool = True, force: bool = False) -> tuple[CardsResult, list[str]]:
    run_dir = Path(run_dir)
    if not run_dir.is_dir() and run_root:
        run_dir = Path(run_root) / run_dir
    log: list[str] = []
    clear_approvals(run_dir, ("s2", "s3"))
    for stale in ("issues.json", "ledger.json", "corrected.json"):
        try:
            (run_dir / stale).unlink()
        except FileNotFoundError:
            continue
    log.append("本次重跑 S2，已清理下游产物 issues.json / ledger.json / corrected.json")

    intake = _load_intake(run_dir)
    result = CardsResult()
    result.run_id = (intake.get("run") or {}).get("period_tag") or run_dir.name
    result.dir = (intake.get("run") or {}).get("dir") or str(run_dir)

    state = intake.get("state")
    if state != "准入通过" or intake.get("next_action") != "continue":
        result.state = "blocked"
        result.questions = list(intake.get("questions") or [])
        result.message = ("S1 未准入（当前：" + str(state or "未知") + "），先回第一步把问题处理完。")
        log.append("前置不满足：S1 state=" + str(state))
        return result, log

    minutes = intake.get("minutes") or {}
    start = minutes.get("period_start") or ""
    end = minutes.get("period_end") or ""
    if not start or not end:
        result.state = "blocked"
        result.message = "S1 没有给出本期周期起止，S2 不自己推算，退回第一步。"
        result.questions = ["S1 的 minutes.period_start / period_end 为空"]
        log.append("周期缺失，停止")
        return result, log

    cmp_start = minutes.get("compare_start") or ""
    cmp_end = minutes.get("compare_end") or ""
    result.period = _period_text(start, end)
    result.compare_period = _period_text(cmp_start, cmp_end)
    log.append("本期 " + result.period + "，对比期 " + (result.compare_period or "未给"))

    try:
        minutes_path = _minutes_file(run_dir, intake)
        minutes_text = _read_minutes(minutes_path)
    except Exception as exc:  # noqa: BLE001
        result.state = "blocked"
        result.message = "读不到纪要原文：" + str(exc)
        return result, log
    result.source = minutes_path.name
    log.append("纪要原文 " + minutes_path.name + "，" + str(len(minutes_text)) + " 字")

    if not use_ai:
        result.state = "blocked"
        result.message = "已关闭模型，S2 需要模型读句子。"
        return result, log

    cards_path = run_dir / "cards.json"
    if cards_path.exists() and not force:
        try:
            cached = json.loads(cards_path.read_text(encoding="utf-8"))
            if cached.get("state") == "ok" and cached.get("cards"):
                log.append("复用已落盘的 cards.json（" + str(len(cached["cards"]))
                           + " 张卡片）；要重拆加 --force")
                return CardsResult(**cached), log
        except Exception:  # noqa: BLE001 - 缓存坏了就重拆
            log.append("旧的 cards.json 读不出来，重新拆")

    entities = [str(item) for item in (minutes.get("entities") or [])][:40]
    payload = (
        "本期：" + result.period + "\n"
        "对比期：" + (result.compare_period or "未给，按本期各前推 7 天") + "\n"
        "周次：" + str(minutes.get("week") or "") + "\n"
        "已知实体：" + ("、".join(entities) if entities else "（无）") + "\n\n"
        "纪要正文：\n" + minutes_text[:MAX_MINUTES_CHARS]
    )
    if len(minutes_text) > MAX_MINUTES_CHARS:
        log.append("纪要超长，只取前 " + str(MAX_MINUTES_CHARS) + " 字")

    try:
        out = chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": payload}],
            temperature=0.0, max_tokens=8192, timeout=180, thinking="disabled",
        )
    except LLMError as exc:
        result.state = "blocked"
        result.message = "模型调用失败：" + str(exc)
        return result, log

    result.model = out.get("model") or ""
    if not (out.get("text") or "").strip():
        result.state = "blocked"
        result.message = "模型没有返回内容（finish_reason=" + str(out.get("finish_reason")) + "）"
        return result, log

    try:
        raw = _extract_json(out["text"])
    except Exception as exc:  # noqa: BLE001
        result.state = "blocked"
        result.message = "模型返回的不是合法 JSON：" + str(exc)
        log.append((out.get("text") or "")[:200])
        return result, log

    _validate(raw, minutes_text, result.period, result.compare_period, result)
    _recount(result)

    if not result.cards:
        result.state = "empty"
        result.questions = ["整篇没有抽出可核对的卡片，按规格退回 S1 复查纪要。"]
        result.message = "一条卡片都没抽出来，纪要需要复查。"
    else:
        result.state = "ok"
        result.message = ("拆出 " + str(result.stats.cards) + " 张卡片，其中 "
                          + str(result.stats.checkable) + " 张可对数据；动作 "
                          + str(result.stats.actions) + " 项，未来事项 "
                          + str(len(result.future)) + " 条。")
    log.append(result.message)
    if result.dropped:
        log.append("校验丢弃 " + str(len(result.dropped)) + " 项")

    (run_dir / "cards.json").write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8")
    log.append("已写入 " + str(run_dir / "cards.json"))
    return result, log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S2 纪要拆卡")
    parser.add_argument("--run", required=True, help="运行目录，如 runs/w44")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-ai", action="store_true", help="只做前置检查，不调模型")
    parser.add_argument("--force", action="store_true", help="忽略已有 cards.json，重新拆卡")
    args = parser.parse_args(argv)

    try:
        result, log = run_s2(args.run, args.run_root,
                             use_ai=not args.no_ai, force=args.force)
    except FileNotFoundError as exc:
        print(json.dumps({"state": "blocked", "message": str(exc)}, ensure_ascii=False))
        return 1

    if args.json:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        for line in log:
            print("- " + line)
        print()
        print(result.message)
    return 0 if result.state == "ok" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
