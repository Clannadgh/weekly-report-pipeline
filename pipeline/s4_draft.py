"""S4 修纪要底稿：按 S3 问题清单逐句改纪要，落盘 corrected.json。

AI 只负责「把这句话改成和数据一致的说法」，代码负责校验：
原句必须逐字出自 cards.json、修正句的数字必须来自原句或台账、依据必须是台账里已有的键、
阻断建议必须按用户裁决处理。统计数与状态一律由代码重算，不信任模型给的数。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .approvals import load as load_approvals, tally as tally_approvals
from .llm import LLMError, chat
from .models import (
    CardFuture, CorrectedItem, CorrectedResult, CorrectedStats,
    S4Decision, S4QuestionCard,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUN_ROOT = PROJECT_ROOT / "runs"

BANNED = ("大约", "左右", "基本", "显著", "大幅", "有所",
          "一定程度上", "应该是", "众所周知", "据不完全统计")
STATUS_READY = "可扩写"
STATUS_NEED = "待补充"
MAX_CARDS = 60

SYSTEM = """你是纪要修稿器。按问题清单，把纪要里说错、说不清的单句改成和数据一致的说法。
本步只改纪要底稿，不写报告、不下经营结论。

【输入】每张卡片会给出：原句、对象、指标、问题、用户裁决、可用台账键。
【怎么改】
1. 没有问题的卡片：原句照抄，status=kept，note 留空。
2. 阻断级：
   - 用户裁决 accept：必须改。以数据为准重写整句，数字只能引用台账里的 display；
     basis 填用到的台账键，多个用「、」分隔；status=corrected；issue 填问题 id。
   - 用户裁决 reject：不改。status=kept，note 写「用户拒绝该建议」。
3. 提示级：能改成可核实说法就改（status=corrected）；不能改就保留原句，note 写清怎么处理（status=kept）。
4. 信息级：不改原文，note 记一句处置。
5. 实体不存在、口径不唯一、台账没有可用键：不改，status=blocked，note 写原因；
   确实需要用户裁决时，另出一张问题卡（cards），最多 2 张。
6. 未来事项、下周计划：不写成本周成绩。本期未来事项另有清单，不要重复。
【改写规范】
- 一句一改，不合并两句、不删句子、不整段重写。
- 金额千分位、百分比一位小数、环比统一写「较上周」。
- 不用「大约、左右、基本、显著、大幅、有所、一定程度上、应该是、众所周知、据不完全统计」。
- 不新增纪要没写的原因、动作、责任人、时间；不把不确定写成确定。
- 修正句里的数字必须来自原句或台账 display；不重算、不换算单位。
【输出】只输出一个 JSON 对象，不要解释、不要 markdown 代码块：
{"items":[{"id":"M01","original":"","fixed":"","basis":"","status":"corrected|kept|future|blocked","confirmed":true,"issue":"","note":""}],
 "cards":[{"id":"T4-1","issue":"","question":"","options":[],"suggest":""}]}
items 必须覆盖输入里的每一张卡片。
"""


@dataclass
class S4Outcome:
    ok: bool
    state: str
    message: str
    corrected: dict | None = None
    log: list[str] = field(default_factory=list)
    run_id: str = ""
    dir: str = ""
    week: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "state": self.state, "message": self.message,
            "corrected": self.corrected, "log": self.log,
            "run_id": self.run_id, "dir": self.dir, "week": self.week,
        }


# ---------------------------------------------------------------- 小工具

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _compact(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(text or ""))


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


def _write_status(run_dir: Path, state: str, note: str) -> None:
    path = run_dir / "status.json"
    data = _read_json(path)
    at = dt.datetime.now().isoformat(timespec="seconds")
    history = data.get("history")
    if not isinstance(history, list):
        history = []
    history.append({"at": at, "state": state, "updated_by": "S4", "note": note})
    data["state"] = state
    data["updated_by"] = "S4"
    data["at"] = at
    data["note"] = note
    data["history"] = history[-50:]
    if not data.get("stage"):
        data["stage"] = "s4"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _split_keys(basis: str) -> list[str]:
    return [item for item in re.split(r"[、,，;；\s]+", str(basis or "")) if item]


_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_METRIC_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(元|%|笔|件|份|倍|天|个百分点)")
_BARE_NUM = re.compile(r"(?<![\d.,])(\d[\d,]{3,}(?:\.\d+)?)(?![\d.,\-/年])")


def _norm_num(token: str) -> str:
    return str(token or "").replace(",", "").replace("+", "").replace("-", "")


def _allowed_numbers(text: str) -> set[str]:
    allowed: set[str] = set()
    for item in _NUM_TOKEN.findall(str(text or "")):
        norm = _norm_num(item)
        allowed.add(norm)
        if norm.endswith("%"):
            allowed.add(norm[:-1])
    return allowed


def _foreign_numbers(fixed: str, original: str, keys, entries: dict) -> list[str]:
    """只查带单位或 4 位以上的数，日期与序号放过，避免误伤。"""
    allowed = _allowed_numbers(original)
    for key in keys:
        entry = entries.get(key) or {}
        allowed |= _allowed_numbers(entry.get("display") or "")
        allowed |= _allowed_numbers(entry.get("spec") or "")
    bad: list[str] = []
    seen: set[str] = set()
    for match in _METRIC_NUM.finditer(fixed or ""):
        token = match.group(1)
        if _norm_num(token) not in allowed and token not in seen:
            seen.add(token)
            bad.append(token)
    for match in _BARE_NUM.finditer(fixed or ""):
        token = match.group(1)
        if _norm_num(token) not in allowed and token not in seen:
            seen.add(token)
            bad.append(token)
    return bad


def _issue_rank(issue: dict) -> int:
    return {"阻断": 0, "提示": 1, "信息": 2}.get(str(issue.get("level") or ""), 3)


def _issues_by_card(issues: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in issues:
        if not isinstance(item, dict):
            continue
        card = str(item.get("card") or "").strip()
        if card:
            grouped.setdefault(card, []).append(item)
    for card in grouped:
        grouped[card].sort(key=_issue_rank)
    return grouped


def _decisions(approvals: dict) -> dict[str, dict]:
    entry = approvals.get("s3") if isinstance(approvals.get("s3"), dict) else {}
    decisions = entry.get("decisions")
    return decisions if isinstance(decisions, dict) else {}


# ---------------------------------------------------------------- 提示词装配

def _card_block(card, issues: list[dict], decisions: dict, ledger_entries: list[dict]) -> str:
    lines = ["[卡片 " + str(card.get("id") or "") + "]"]
    lines.append("原句：" + str(card.get("quote") or ""))
    lines.append("对象：" + str(card.get("entity") or "不确定")
                 + "｜指标：" + str(card.get("metric") or "其他")
                 + "｜单位：" + str(card.get("unit") or "未定")
                 + "｜期望：" + str(card.get("expect") or "说不清")
                 + "｜可核对：" + str(card.get("checkable") or "不确定"))
    if not issues:
        lines.append("问题：无（原句照抄）")
    for issue in issues:
        lines.append("问题：" + str(issue.get("id") or "") + "｜" + str(issue.get("level") or "")
                     + "｜" + str(issue.get("type") or "") + "｜" + str(issue.get("subject") or ""))
        if issue.get("minutes_said"):
            lines.append("纪要： " + str(issue.get("minutes_said")))
        if issue.get("raw"):
            line = "数据： " + str(issue.get("raw"))
            if issue.get("diff") is not None:
                line += "（差异 " + str(issue.get("diff")) + "）"
            lines.append(line)
        if issue.get("suggest"):
            lines.append("建议： " + str(issue.get("suggest")))
    if issues:
        decision = decisions.get(str(issues[0].get("id") or ""))
        if decision:
            lines.append("用户裁决：" + str(decision.get("decision") or "")
                         + "（" + str(decision.get("by") or "用户")
                         + " " + str(decision.get("at") or "") + "）")
        elif str(issues[0].get("level") or "") == "阻断":
            lines.append("用户裁决：无（不要改写，status=blocked）")
    if ledger_entries:
        lines.append("台账（只能引用这些键）：")
        for entry in ledger_entries:
            lines.append("  - " + str(entry.get("key") or "")
                         + " = " + str(entry.get("display") or "")
                         + " " + str(entry.get("unit") or "")
                         + "｜口径：" + str(entry.get("spec") or ""))
    else:
        lines.append("台账：无（不能引用任何数字）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 校验

def _build_item(card: dict, raw: dict, issue: dict | None, decision: dict | None,
                ledger_entries: list[dict], entries: dict,
                warnings: list[str]) -> CorrectedItem:
    quote = str(card.get("quote") or "")
    cid = str(card.get("id") or "")
    status = str(raw.get("status") or "").strip()
    fixed = str(raw.get("fixed") or "").strip()
    basis = str(raw.get("basis") or "").strip()
    note = str(raw.get("note") or "").strip()
    confirmed = bool(raw.get("confirmed"))
    original = str(raw.get("original") or "").strip() or quote

    if _compact(original) != _compact(quote):
        warnings.append(cid + " 原句与 cards.json 不一致，已按卡片原文校正")
        original = quote
    if status not in ("corrected", "kept", "future", "blocked"):
        status = "corrected" if (fixed and fixed != quote) else "kept"
    if not fixed:
        fixed = quote

    issue_id = str((issue or {}).get("id") or raw.get("issue") or "").strip()
    level = str((issue or {}).get("level") or "")
    choice = str((decision or {}).get("decision") or "")

    if status == "corrected" and not issue:
        status, fixed = "kept", quote
        note = (note + "；" if note else "") + "没有问题却改写，已还原原文"
        warnings.append(cid + " 没有问题却改写，已还原")
    if level == "信息" and status in ("corrected", "future", "blocked"):
        status, fixed = "kept", quote
        note = (note + "；" if note else "") + "信息项不改原文"
    if level == "阻断" and choice == "reject":
        status, fixed, confirmed = "kept", quote, True
        note = (note + "；" if note else "") + "用户拒绝该建议"
    if level == "阻断" and choice == "accept" and status in ("kept", "future"):
        status = "blocked"
        note = (note + "；" if note else "") + "用户已批准建议，但模型未改写"
    if level == "阻断" and not decision:
        status, fixed = "blocked", quote
        note = (note + "；" if note else "") + "阻断未裁决"

    if status != "corrected":
        fixed = quote
    if status == "corrected":
        if fixed == quote:
            status = "kept"
            note = (note + "；" if note else "") + "模型给出的修正与原句相同"
        else:
            keys = _split_keys(basis)
            if not keys:
                status, fixed = "blocked", quote
                note = (note + "；" if note else "") + "修正缺台账依据"
            else:
                invalid = [key for key in keys if key not in entries]
                if invalid:
                    status, fixed = "blocked", quote
                    note = (note + "；" if note else "") + "台账没有键 " + "、".join(invalid)
                else:
                    banned = [word for word in BANNED if word in fixed]
                    if banned:
                        status, fixed = "blocked", quote
                        note = (note + "；" if note else "") + "修正含禁用词 " + "、".join(banned)
                    else:
                        bad = _foreign_numbers(fixed, quote, keys, entries)
                        if bad:
                            status, fixed = "blocked", quote
                            note = (note + "；" if note else "") + "数字不在原句或台账里：" + "、".join(bad)
                        else:
                            used = False
                            for key in keys:
                                if _allowed_numbers((entries.get(key) or {}).get("display") or "") & _allowed_numbers(fixed):
                                    used = True
                                    break
                            if not used:
                                status, fixed = "blocked", quote
                                note = (note + "；" if note else "") + "修正句没有引用台账数值"
    if level == "提示" and status == "kept" and not note:
        note = "提示项保留原文，报告里补充说明"
    if level == "信息" and not note:
        note = str((issue or {}).get("suggest") or "按信息项记录")
    if status == "corrected" and not confirmed:
        warnings.append(cid + " 的修正未确认")

    return CorrectedItem(
        id=cid, original=quote, fixed=fixed, basis=basis if status == "corrected" else "",
        status=status, confirmed=confirmed, issue=issue_id, note=note,
    )


def _validate(raw: dict, cards: list[dict], issues_by_card: dict, decisions: dict,
              ledger_by_card: dict, entries: dict,
              result: CorrectedResult, warnings: list[str]) -> None:
    raw_items = raw.get("items") if isinstance(raw, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    by_id: dict[str, dict] = {}
    for item in raw_items:
        if isinstance(item, dict):
            cid = str(item.get("id") or "").strip()
            if cid:
                by_id[cid] = item
    known = {str(card.get("id") or "") for card in cards}
    for cid in by_id:
        if cid not in known:
            warnings.append("模型返回了未知卡片 " + cid + "，已忽略")

    for card in cards:
        cid = str(card.get("id") or "")
        raw_item = by_id.get(cid)
        if raw_item is None:
            warnings.append(cid + " 模型没有返回，按原文保留")
            raw_item = {}
        card_issues = issues_by_card.get(cid) or []
        issue = card_issues[0] if card_issues else None
        decision = decisions.get(str(issue.get("id") or "")) if issue else None
        result.items.append(_build_item(card, raw_item, issue, decision,
                                        ledger_by_card.get(cid) or [], entries, warnings))

    raw_cards = raw.get("cards") if isinstance(raw, dict) else []
    if isinstance(raw_cards, list):
        for index, item in enumerate(raw_cards):
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            if len(result.cards) >= 2:
                warnings.append("待裁决卡超过 2 张，只保留前 2 张")
                break
            options = item.get("options")
            if not isinstance(options, list):
                options = [options] if options else []
            result.cards.append(S4QuestionCard(
                id="T4-" + str(len(result.cards) + 1),
                issue=str(item.get("issue") or ""),
                question=question,
                options=[str(opt).strip() for opt in options if str(opt).strip()],
                suggest=str(item.get("suggest") or ""),
            ))


# ---------------------------------------------------------------- 主流程

def run_s4(run_dir: str | Path, run_root: str | Path | None = None,
           use_ai: bool = True) -> S4Outcome:
    run_dir = Path(run_dir)
    if not run_dir.is_dir() and run_root:
        run_dir = Path(run_root) / run_dir
    log: list[str] = []

    intake = _read_json(run_dir / "intake.json")
    if not intake:
        raise FileNotFoundError("找不到 intake.json，先跑完 S1：" + str(run_dir / "intake.json"))
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
    run_id = str(run.get("period_tag") or run_dir.name)
    week = str(minutes.get("week") or run_id)
    period = ((str(minutes.get("period_start") or "") + " 至 "
               + str(minutes.get("period_end") or "")).strip(" 至"))

    if intake.get("state") != "准入通过":
        msg = "S1 未准入（当前：" + str(intake.get("state") or "未知") + "），先回第一步处理。"
        return S4Outcome(False, "blocked", msg, None, [msg], run_id, str(run.get("dir") or ""), week)

    cards_doc = _read_json(run_dir / "cards.json")
    cards = cards_doc.get("cards") if isinstance(cards_doc.get("cards"), list) else []
    if cards_doc.get("state") != "ok" or not cards:
        raise FileNotFoundError("cards.json 还没有可用卡片，先跑 S2")
    issues_doc = _read_json(run_dir / "issues.json")
    if not issues_doc:
        raise FileNotFoundError("还没有 issues.json，先跑 S3")
    issues = issues_doc.get("issues") if isinstance(issues_doc.get("issues"), list) else []
    ledger = _read_json(run_dir / "ledger.json")
    entries_list = ledger.get("entries") if isinstance(ledger.get("entries"), list) else []
    entries = {str(item.get("key") or ""): item for item in entries_list
               if isinstance(item, dict) and item.get("key")}
    ledger_by_card: dict[str, list[dict]] = {}
    for item in entries_list:
        if isinstance(item, dict) and item.get("card"):
            ledger_by_card.setdefault(str(item["card"]), []).append(item)

    approvals = load_approvals(run_dir)
    decisions = _decisions(approvals)
    blocking_ids = [str(item.get("id")) for item in issues
                    if isinstance(item, dict) and item.get("level") == "阻断" and item.get("id")]
    state = tally_approvals(approvals.get("s3"), blocking_ids)
    if blocking_ids and not state["approved"]:
        msg = ("S3 还有 " + str(len(state["pending"])) + " 条阻断未裁决：先到 S3 详情逐条点"
               "「批准」或「拒绝」，再回来修纪要。")
        _write_status(run_dir, STATUS_NEED, msg)
        return S4Outcome(False, STATUS_NEED, msg, None, [msg], run_id,
                         str(run.get("dir") or ""), week)

    log.append("本期 " + week + "（" + (period or "周期未给") + "），卡片 " + str(len(cards))
               + " 张，问题 " + str(len(issues)) + " 条，台账 " + str(len(entries)) + " 条")
    if len(cards) > MAX_CARDS:
        log.append("卡片超过 " + str(MAX_CARDS) + " 张，只取前 " + str(MAX_CARDS) + " 张")
        cards = cards[:MAX_CARDS]
    if not use_ai:
        msg = "已关闭模型，S4 需要模型改句。"
        return S4Outcome(False, "blocked", msg, None, [msg], run_id, str(run.get("dir") or ""), week)

    issues_by_card = _issues_by_card(issues)
    blocks = []
    for card in cards:
        blocks.append(_card_block(card, issues_by_card.get(str(card.get("id") or "")) or [],
                                  decisions, ledger_by_card.get(str(card.get("id") or "")) or []))
    payload = ("本期：" + week + "（" + (period or "周期未给") + "）\n"
               "卡片数：" + str(len(cards)) + "\n"
               "未裁决阻断：" + str(len(state["pending"])) + "\n\n"
               + "\n\n".join(blocks)
               + "\n\n输出 JSON，items 必须覆盖上面全部 " + str(len(cards)) + " 张卡片。")

    try:
        out = chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": payload}],
            temperature=0.0, max_tokens=8192, timeout=240, thinking="disabled",
        )
    except LLMError as exc:
        msg = "模型调用失败：" + str(exc)
        return S4Outcome(False, "blocked", msg, None, log + [msg], run_id,
                         str(run.get("dir") or ""), week)

    if not (out.get("text") or "").strip():
        msg = "模型没有返回内容（finish_reason=" + str(out.get("finish_reason")) + "）"
        return S4Outcome(False, "blocked", msg, None, log + [msg], run_id,
                         str(run.get("dir") or ""), week)
    try:
        raw = _extract_json(out["text"])
    except Exception as exc:  # noqa: BLE001
        msg = "模型返回的不是合法 JSON：" + str(exc)
        log.append((out.get("text") or "")[:200])
        return S4Outcome(False, "blocked", msg, None, log + [msg], run_id,
                         str(run.get("dir") or ""), week)

    result = CorrectedResult(week=week)
    result.future = [CardFuture(text=str(item.get("text") or ""),
                                source=str(item.get("source") or ""))
                     for item in (cards_doc.get("future") or []) if isinstance(item, dict)]
    warnings: list[str] = []
    _validate(raw, cards, issues_by_card, decisions, ledger_by_card, entries, result, warnings)

    for issue in issues:
        if not isinstance(issue, dict) or issue.get("level") != "阻断":
            continue
        decision = decisions.get(str(issue.get("id") or ""))
        if not decision:
            continue
        result.decisions.append(S4Decision(
            issue=str(issue.get("id") or ""),
            choice="data" if decision.get("decision") == "accept" else "skip",
            by=str(decision.get("by") or ""),
            auto=False,
            at=str(decision.get("at") or ""),
        ))

    result.stats = CorrectedStats(
        corrected=sum(1 for item in result.items if item.status == "corrected"),
        kept=sum(1 for item in result.items if item.status == "kept"),
        future=sum(1 for item in result.items if item.status == "future"),
        blocked=sum(1 for item in result.items if item.status == "blocked"),
    )
    blocked_cards = len(result.cards)
    if result.stats.blocked or blocked_cards:
        state = STATUS_NEED
        msg = ("修正 " + str(result.stats.corrected) + " 条，保留 " + str(result.stats.kept)
               + " 条，待处理 " + str(result.stats.blocked) + " 条"
               + ("，待裁决 " + str(blocked_cards) + " 张卡" if blocked_cards else "")
               + "；处理完再扩写。")
    else:
        state = STATUS_READY
        msg = ("修正 " + str(result.stats.corrected) + " 条，保留 " + str(result.stats.kept)
               + " 条，转未来 " + str(result.stats.future) + " 条；可以进入 S5 扩写。")

    payload_json = result.model_dump(mode="json")
    (run_dir / "corrected.json").write_text(
        json.dumps(payload_json, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_status(run_dir, state, msg)
    log.append("模型 " + str(out.get("model") or "未知") + "；校验提示 " + str(len(warnings)) + " 条")
    log.extend(warnings[:10])
    log.append("已写入 " + str(run_dir / "corrected.json"))
    log.append("status.json → " + state)

    return S4Outcome(True, state, msg, payload_json, log, run_id,
                     str(run.get("dir") or ""), week)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S4 修纪要底稿")
    parser.add_argument("--run", required=True, help="运行目录，如 runs/w44")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-ai", action="store_true", help="只做前置检查，不调模型")
    args = parser.parse_args(argv)

    try:
        outcome = run_s4(args.run, args.run_root, use_ai=not args.no_ai)
    except FileNotFoundError as exc:
        print(json.dumps({"state": "blocked", "message": str(exc)}, ensure_ascii=False))
        return 1

    if args.json:
        print(json.dumps({
            "ok": outcome.ok, "state": outcome.state, "message": outcome.message,
            "corrected": outcome.corrected, "log": outcome.log,
        }, ensure_ascii=False, indent=2))
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
