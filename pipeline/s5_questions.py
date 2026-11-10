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


def effective(run_dir: str | Path) -> dict:
    """当前生效的问题清单：优先 s5_questions.json，其次上一稿的 discoveries。

    返回 questions / source(saved|last_run|none) / updated_at / count /
    dirty（清单与当前稿不一致）/ applied（当前稿就是用这份清单生成的）。
    """
    folder = Path(run_dir)
    plan = load_plan(folder)
    if plan is not None:
        current_hash = questions_hash(plan["questions"])
        facts_hash = _facts_hash(folder)
        applied = bool(facts_hash) and facts_hash == current_hash
        diff = diff_questions(plan["questions"], _facts_plan_questions(folder))
        return {
            "questions": plan["questions"],
            "source": "saved",
            "updated_at": plan["updated_at"],
            "count": len(plan["questions"]),
            "dirty": bool(facts_hash) and not applied,
            "applied": applied,
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
