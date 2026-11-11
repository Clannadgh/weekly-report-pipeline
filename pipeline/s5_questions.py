"""S5 数据发现问题的可编辑清单。

问题清单是 S5 的输入，不是交付件：
- 首次 S5 由 GLM 出题后落盘 s5_questions.json（source=auto）；
- 用户 / 主 agent 可以增删改问题（source=user），下次扩写直接使用；
- reset 删除清单，下次扩写重新由 GLM 出题。

存储格式：
{
  "run_id": "w42",
  "updated_at": "2026-11-09T20:00:00",
  "source": "auto" | "user",
  "questions": [{"id": "D1", "question": "...", "shape": "number", "reason": "..."}]
}
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

PLAN_FILE = "s5_questions.json"
FILTER_FILE = "s5_question_filter.json"
FILTER_VERSION = 1
MAX_QUESTIONS = 12
_SHAPES = ("number", "table")
_WS_RE = re.compile(r"[\s\u3000\ufeff]+")


def _clean(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _clean_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "", str(value or ""))


def normalize_questions(items: Any, *, strict: bool = False,
                        max_count: int = MAX_QUESTIONS) -> list[dict]:
    """把外部问题规约成统一结构。

    strict=True 用于用户 / agent 保存：空问题、非法形状、超过上限直接报错，
    不做静默丢弃；strict=False 用于模型出题，坏条目跳过。
    """
    if not isinstance(items, list):
        if strict:
            raise ValueError("questions 必须是数组")
        return []
    out: list[dict] = []
    used: set[str] = set()
    valid_seen = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            if strict:
                raise ValueError("第 " + str(index + 1) + " 个问题不是对象")
            continue
        question = _clean(item.get("question"))
        if not question:
            if strict:
                raise ValueError("第 " + str(index + 1) + " 个问题缺少 question")
            continue
        valid_seen += 1
        if valid_seen > max_count:
            if strict:
                raise ValueError("最多保留 " + str(max_count) + " 个问题")
            break
        shape = _clean(item.get("shape")).lower()
        if shape not in _SHAPES:
            if strict:
                raise ValueError("第 " + str(index + 1) + " 个问题的 shape 只能是 number 或 table")
            shape = "number"
        qid = _clean_id(item.get("id"))
        if qid:
            base = qid
            suffix = 2
            while qid in used:
                qid = base + "_" + str(suffix)
                suffix += 1
        else:
            number = index + 1
            qid = "D" + str(number)
            while qid in used:
                number += 1
                qid = "D" + str(number)
        used.add(qid)
        out.append({
            "id": qid,
            "question": question,
            "shape": shape,
            "reason": _clean(item.get("reason")),
        })
    if strict and not out:
        raise ValueError("至少保留 1 个问题")
    return out


def next_id(questions: list[dict]) -> str:
    """下一个问题编号：沿用现有 D 编号的最大值 + 1，不重排旧编号。"""
    maximum = 0
    for item in questions:
        match = re.fullmatch(r"D(\d+)", str(item.get("id") or ""))
        if match:
            maximum = max(maximum, int(match.group(1)))
    return "D" + str(maximum + 1 if maximum else len(questions) + 1)


def questions_from_findings(findings: Any) -> list[dict]:
    """从已执行的 s5_facts.discoveries 还原问题清单（老运行没有清单时用）。"""
    raw = []
    for item in findings or []:
        if not isinstance(item, dict):
            continue
        raw.append({
            "id": item.get("id") or "",
            "question": item.get("question") or "",
            "shape": item.get("shape") or "number",
            "reason": item.get("reason") or "",
        })
    return normalize_questions(raw, strict=False, max_count=MAX_QUESTIONS)


def questions_hash(questions: list[dict]) -> str:
    """问题清单指纹：编号、问句、形状、理由和顺序都算数。"""
    payload = [{
        "id": str(item.get("id") or ""),
        "question": str(item.get("question") or ""),
        "shape": str(item.get("shape") or ""),
        "reason": str(item.get("reason") or ""),
    } for item in questions]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def diff_questions(new_questions: list[dict],
                   old_questions: list[dict]) -> dict:
    """按 id 比较计划与上一稿实际用过的问题。

    changed：新增或问句 / 形状 / 理由有变化，需要重新取数；
    reused：与上一稿完全一致，可直接复用 findings；
    removed：从清单删掉，上一稿的结果要丢弃；
    order_changed：共有问题的相对顺序变了，图序和正文顺序要重排。
    """
    old_map: dict[str, dict] = {}
    old_order: list[str] = []
    for item in old_questions or []:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "")
        if not qid:
            continue
        old_map[qid] = {
            "id": qid,
            "question": str(item.get("question") or ""),
            "shape": str(item.get("shape") or "number"),
            "reason": str(item.get("reason") or ""),
        }
        old_order.append(qid)

    new_order: list[str] = []
    changed: list[str] = []
    reused: list[str] = []
    for item in new_questions or []:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "")
        if not qid:
            continue
        new_order.append(qid)
        current = {
            "id": qid,
            "question": str(item.get("question") or ""),
            "shape": str(item.get("shape") or "number"),
            "reason": str(item.get("reason") or ""),
        }
        if old_map.get(qid) == current:
            reused.append(qid)
        else:
            changed.append(qid)

    new_set = set(new_order)
    removed = [qid for qid in old_order if qid not in new_set]
    old_shared = [qid for qid in old_order if qid in new_set]
    new_shared = [qid for qid in new_order if qid in old_map]
    return {
        "changed_ids": changed,
        "reused_ids": reused,
        "removed_ids": removed,
        "order_changed": old_shared != new_shared,
    }


def _question_key(item: dict) -> str:
    """过滤缓存的键：编号 + 问句 + 形状。只改理由不重新判断。"""
    text = "|".join((str(item.get("id") or ""),
                     _clean(item.get("question")),
                     str(item.get("shape") or "number")))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _json_object(text: str) -> dict:
    body = str(text or "").strip()
    fence = chr(96) * 3
    if body.startswith(fence):
        body = body[len(fence):]
        body = re.sub(r"^[a-zA-Z]*\s*", "", body)
        if body.rstrip().endswith(fence):
            body = body.rstrip()[:-len(fence)]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    data = json.loads(body[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    return data


def _filter_prompt(questions: list[dict]) -> str:
    payload = [{"id": str(item.get("id") or ""),
                "question": str(item.get("question") or ""),
                "shape": str(item.get("shape") or "number"),
                "reason": str(item.get("reason") or "")}
               for item in questions]
    return (
        "你是经营周报的选题审稿人。只判断问题是否值得执行并写入经营周报，不要改写问题。\n"
        "保留：能形成经营结论的问题（实收、销量、毛利、区域 / 门店 / 品类 / 渠道表现、"
        "活动效果、排名、结构等）。\n"
        "跳过：纯数据质量盘点（记录行数、去重 / 重复、缺失 / 空值、负数 / 冲销、"
        "POS 故障、上传核对等），或答案只能是「待核实 / 疑似 / 可能」、"
        "无法形成经营结论的问题。\n"
        "注意：理由里出现「核实 / 是否一致」不构成跳过理由，要看问题本身要回答什么。\n"
        "输出严格 JSON："
        '{"keep":["D4"],"drop":[{"id":"D1","why":"记录行数属于数据质量"}]}\n'
        "每个输入问题必须且只能出现在 keep 或 drop 之一。不要输出其他内容。\n\n"
        "输入：\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    )


def _ask_filter_model(questions: list[dict], chat_fn=None) -> dict[str, dict]:
    if chat_fn is None:
        from .llm import chat as chat_fn  # noqa: PLC0415 - 延迟导入

    messages = [
        {"role": "system", "content": "你是经营周报的选题审稿人，只输出 JSON。"},
        {"role": "user", "content": _filter_prompt(questions)},
    ]
    try:
        result = chat_fn(messages, temperature=0, max_tokens=1200,
                         timeout=90, thinking="disabled")
    except TypeError:
        result = chat_fn(messages, temperature=0, max_tokens=1200, timeout=90)
    text = result.get("text") if isinstance(result, dict) else str(result or "")
    data = _json_object(text)

    raw_keep = data.get("keep")
    raw_drop = data.get("drop")
    if not isinstance(raw_keep, list) or not isinstance(raw_drop, list):
        raise ValueError("过滤结果缺少 keep / drop 数组")
    keep: list[str] = []
    for value in raw_keep:
        qid = _clean_id(value)
        if not qid or qid in keep:
            raise ValueError("keep 里有空编号或重复编号")
        keep.append(qid)
    drop: dict[str, str] = {}
    for value in raw_drop:
        if isinstance(value, dict):
            qid = _clean_id(value.get("id"))
            why = _clean(value.get("why"))
        else:
            qid, why = _clean_id(value), ""
        if not qid or qid in drop or qid in keep:
            raise ValueError("drop 里有空编号、重复编号或与 keep 冲突")
        drop[qid] = why

    known = {str(item.get("id") or "") for item in questions}
    if set(keep) | set(drop) != known:
        raise ValueError("过滤结果没有覆盖全部问题")
    decisions = {qid: {"keep": True, "why": ""} for qid in keep}
    decisions.update({qid: {"keep": False, "why": why or "不适合写进周报"}
                      for qid, why in drop.items()})
    return decisions


def _load_filter_cache(run_dir: str | Path) -> dict[str, dict]:
    data = _read_json(Path(run_dir) / FILTER_FILE)
    if int(data.get("version") or 0) != FILTER_VERSION:
        return {}
    raw = data.get("decisions")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _save_filter_cache(run_dir: str | Path, decisions: dict[str, dict]) -> None:
    path = Path(run_dir) / FILTER_FILE
    items = list(decisions.items())[-100:]
    doc = {
        "version": FILTER_VERSION,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "decisions": dict(items),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def filter_for_run(run_dir: str | Path, questions: Any, *,
                   chat_fn=None, log: list[str] | None = None
                   ) -> tuple[list[dict], dict]:
    """AI 判断哪些问题值得写进周报；失败或返回不合法时回退词表过滤。

    判断结果按「编号 + 问句 + 形状」缓存，重跑同一份清单不会漂移；
    只改理由（例如删掉「核实」）不会触发重新判断。
    """
    log = log if log is not None else []
    normalized = normalize_questions(questions, strict=False, max_count=MAX_QUESTIONS)
    if not normalized:
        return [], {"source": "none", "kept": [], "dropped": [], "decisions": []}

    cache = _load_filter_cache(run_dir)
    decisions: dict[str, dict] = {}
    misses: list[dict] = []
    for item in normalized:
        key = _question_key(item)
        hit = cache.get(key)
        if isinstance(hit, dict) and str(hit.get("id") or "") == str(item.get("id") or ""):
            decisions[key] = {
                "id": str(item.get("id") or ""),
                "keep": bool(hit.get("keep")),
                "why": str(hit.get("why") or ""),
                "source": "cache",
            }
        else:
            misses.append(item)

    if misses:
        try:
            judged = _ask_filter_model(misses, chat_fn)
            for item in misses:
                qid = str(item.get("id") or "")
                verdict = judged[qid]
                decisions[_question_key(item)] = {
                    "id": qid,
                    "keep": bool(verdict["keep"]),
                    "why": str(verdict.get("why") or ""),
                    "source": "ai",
                }
            log.append("AI 问题过滤：判断 " + str(len(misses)) + " 条")
        except Exception as exc:  # noqa: BLE001 - 过滤失败回退词表
            log.append("AI 问题过滤失败，回退词表：" + str(exc)[:120])
            from . import s5_filters  # noqa: PLC0415 - 避免循环导入
            kept, dropped = s5_filters.filter_questions(misses)
            dropped_ids = {str(qid) for qid in dropped}
            for item in misses:
                qid = str(item.get("id") or "")
                keep = qid not in dropped_ids
                decisions[_question_key(item)] = {
                    "id": qid,
                    "keep": keep,
                    "why": "" if keep else "命中数据质量 / 核实词表",
                    "source": "rules",
                }
            log.append("词表问题过滤：判断 " + str(len(misses)) + " 条")

    kept_questions: list[dict] = []
    dropped_items: list[dict] = []
    ordered_decisions: list[dict] = []
    for item in normalized:
        entry = decisions[_question_key(item)]
        ordered_decisions.append({"id": entry["id"], "keep": entry["keep"],
                                  "why": entry["why"], "source": entry["source"]})
        if entry["keep"]:
            kept_questions.append(item)
        else:
            dropped_items.append({"id": entry["id"], "why": entry["why"],
                                  "source": entry["source"]})
    sources = {entry["source"] for entry in decisions.values()}
    if not sources:
        source = "none"
    elif len(sources) == 1:
        source = next(iter(sources))
    else:
        source = "mixed"
    record = {
        "version": FILTER_VERSION,
        "source": source,
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "kept": [str(item.get("id") or "") for item in kept_questions],
        "dropped": dropped_items,
        "decisions": ordered_decisions,
    }
    if any(entry["source"] == "ai" for entry in decisions.values()):
        ai_only = {key: value for key, value in decisions.items()
                   if value["source"] == "ai"}
        merged = dict(cache)
        merged.update(ai_only)
        try:
            _save_filter_cache(run_dir, merged)
        except Exception as exc:  # noqa: BLE001 - 缓存失败不影响本次过滤
            log.append("问题过滤缓存写入失败：" + str(exc)[:80])
    return kept_questions, record


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_plan(run_dir: str | Path) -> dict | None:
    """读清单；文件不存在返回 None，文件损坏 / 为空直接报错，不静默改成自动出题。"""
    path = Path(run_dir) / PLAN_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("s5_questions.json 读取失败：" + str(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("s5_questions.json 必须是 JSON 对象")
    questions = normalize_questions(data.get("questions"), strict=True)
    return {
        "run_id": str(data.get("run_id") or ""),
        "updated_at": str(data.get("updated_at") or ""),
        "source": str(data.get("source") or "user"),
        "questions": questions,
    }


def save_plan(run_dir: str | Path, questions: Any, *,
              source: str = "user", run_id: str = "") -> dict:
    """校验并原子写入清单；空清单 / 非法形状直接报错。"""
    normalized = normalize_questions(questions, strict=True)
    doc = {
        "run_id": str(run_id or ""),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "auto" if source == "auto" else "user",
        "questions": normalized,
    }
    path = Path(run_dir) / PLAN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return doc


def reset_plan(run_dir: str | Path) -> bool:
    """删除清单；下次 S5 重新由模型出题。返回是否删掉了文件。"""
    path = Path(run_dir) / PLAN_FILE
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _facts_plan_questions(run_dir: Path) -> list[dict]:
    """上一稿实际用过的问题：优先 facts.questions，老运行退回 findings。"""
    facts = _read_json(run_dir / "s5_facts.json")
    raw = facts.get("questions")
    if isinstance(raw, list) and raw:
        return normalize_questions(raw, strict=False, max_count=MAX_QUESTIONS)
    return questions_from_findings(facts.get("discoveries") or [])


def _facts_questions(run_dir: Path) -> list[dict]:
    return _facts_plan_questions(run_dir)


def _facts_hash(run_dir: Path) -> str:
    facts = _read_json(run_dir / "s5_facts.json")
    if not facts:
        return ""
    stored = str(facts.get("questions_hash") or "")
    if stored:
        return stored
    questions = questions_from_findings(facts.get("discoveries") or [])
    return questions_hash(questions) if questions else ""


def _preview_filter(folder: Path, questions: list[dict]
                    ) -> tuple[list[dict], list[dict]]:
    """预览这次实际会执行的问题：命中过滤缓存就按缓存，否则按词表兜底。

    只用于展示，不调模型；新问题按词表保守判断。返回 (保留, 跳过)。
    """
    from . import s5_filters  # noqa: PLC0415 - 与 filter_for_run 保持一致

    cache = _load_filter_cache(folder)
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in questions:
        qid = str(item.get("id") or "")
        hit = cache.get(_question_key(item))
        if isinstance(hit, dict) and str(hit.get("id") or "") == qid:
            if hit.get("keep"):
                kept.append(item)
            else:
                dropped.append({
                    "id": qid,
                    "why": str(hit.get("why") or "不适合写进周报"),
                    "source": str(hit.get("source") or "ai"),
                })
            continue
        kept_word, _ = s5_filters.filter_questions([item])
        if kept_word:
            kept.append(item)
        else:
            dropped.append({
                "id": qid,
                "why": "命中数据质量 / 核实词表",
                "source": "rules",
            })
    return kept, dropped


def _preview_questions(folder: Path, questions: list[dict]) -> list[dict]:
    kept, _ = _preview_filter(folder, questions)
    return kept


def effective(run_dir: str | Path) -> dict:
    """当前生效的问题清单：优先 s5_questions.json，其次上一稿的 discoveries。

    返回 questions / source(saved|last_run|none) / updated_at / count /
    dirty（清单与当前稿不一致）/ applied（当前稿就是用这份清单生成的）。
    被问题过滤跳过的问题不算改动。
    """
    folder = Path(run_dir)
    plan = load_plan(folder)
    if plan is not None:
        active, preview_dropped = _preview_filter(folder, plan["questions"])
        active_hash = questions_hash(active)
        facts_hash = _facts_hash(folder)
        applied = bool(facts_hash) and facts_hash == active_hash
        diff = diff_questions(active, _facts_plan_questions(folder))
        return {
            "questions": plan["questions"],
            "source": "saved",
            "updated_at": plan["updated_at"],
            "count": len(plan["questions"]),
            "dirty": bool(facts_hash) and not applied,
            "applied": applied,
            "preview_kept": [str(item.get("id") or "") for item in active],
            "preview_dropped": preview_dropped,
            "changed_ids": diff["changed_ids"],
            "reused_ids": diff["reused_ids"],
            "removed_ids": diff["removed_ids"],
            "order_changed": diff["order_changed"],
            "changed_count": len(diff["changed_ids"]),
            "reused_count": len(diff["reused_ids"]),
            "removed_count": len(diff["removed_ids"]),
        }
    questions = _facts_questions(folder)
    base = {
        "questions": questions,
        "updated_at": "",
        "preview_kept": [str(item.get("id") or "") for item in questions],
        "preview_dropped": [],
        "dirty": False,
        "changed_ids": [],
        "reused_ids": [],
        "removed_ids": [],
        "order_changed": False,
        "changed_count": 0,
        "reused_count": 0,
        "removed_count": 0,
    }
    if questions:
        return {**base, "source": "last_run", "count": len(questions),
                "applied": True}
    return {**base, "source": "none", "count": 0, "applied": False}
