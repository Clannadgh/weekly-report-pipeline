"""S3 阻断建议的逐条结论（批准/拒绝），存在 run 目录的 approval.json。

每条结论是 S4 改稿的依据：批准＝采纳建议，拒绝＝不改。
所有阻断都有结论后，S3 才算通过。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

STAGES = ("s1", "s2", "s3")
FILENAME = "approval.json"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def load(run_dir: str | Path) -> dict:
    """读批准状态；兼容旧的单阶段 approval.json。"""
    path = Path(run_dir) / FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "approved" in data:
        stage = str(data.get("stage") or "s3").lower()
        return {stage: data} if stage in STAGES else {}
    return {key: value for key, value in data.items()
            if key in STAGES and isinstance(value, dict)}


def save(run_dir: str | Path, approvals: dict) -> None:
    path = Path(run_dir) / FILENAME
    clean = {key: value for key, value in (approvals or {}).items()
             if key in STAGES and isinstance(value, dict)}
    if not clean:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def clear(run_dir: str | Path, stages) -> None:
    """清掉指定阶段的批准；上游重跑时必须调用，避免旧批准继续生效。"""
    data = load(run_dir)
    for stage in stages:
        data.pop(stage, None)
    save(run_dir, data)


def tally(entry: dict | None, blocking_ids) -> dict:
    """按当前阻断清单统计已批准、已拒绝、待处理。"""
    blocking = [str(item) for item in (blocking_ids or []) if str(item)]
    decisions = (entry or {}).get("decisions")
    if not isinstance(decisions, dict):
        decisions = {}
    accepted, rejected, pending = [], [], []
    for issue_id in blocking:
        decision = (decisions.get(issue_id) or {}).get("decision")
        if decision == "accept":
            accepted.append(issue_id)
        elif decision == "reject":
            rejected.append(issue_id)
        else:
            pending.append(issue_id)
    return {
        "total": len(blocking),
        "accepted": accepted,
        "rejected": rejected,
        "pending": pending,
        "approved": not pending,
    }


def _write(run_dir, data, decisions, blocking, at, by, note):
    state = tally({"decisions": decisions}, blocking)
    entry = {
        "approved": state["approved"],
        "at": at,
        "by": by,
        "note": note,
        "blocking": state["total"],
        "decisions": decisions,
        "accepted": state["accepted"],
        "rejected": state["rejected"],
        "pending": state["pending"],
    }
    data["s3"] = entry
    save(run_dir, data)
    return entry


def decide(run_dir: str | Path, issue_id: str, decision: str, blocking_ids,
           note: str = "", by: str = "用户") -> dict:
    """记录一条阻断建议的结论：accept＝批准，reject＝拒绝。"""
    if decision not in ("accept", "reject"):
        raise ValueError("decision 只能是 accept / reject")
    blocking = [str(item) for item in (blocking_ids or []) if str(item)]
    issue_id = str(issue_id or "").strip()
    if issue_id not in blocking:
        raise ValueError("不是当前的阻断项：" + (issue_id or "（空）"))
    data = load(run_dir)
    entry = data.get("s3") if isinstance(data.get("s3"), dict) else {}
    decisions = entry.get("decisions") if isinstance(entry.get("decisions"), dict) else {}
    at = _now()
    label = "批准" if decision == "accept" else "拒绝"
    note = note or ("用户" + label + " " + issue_id + " 的建议")
    decisions[issue_id] = {"decision": decision, "at": at, "by": by, "note": note}
    return _write(run_dir, data, decisions, blocking, at, by, note)


def decide_all(run_dir: str | Path, blocking_ids, decision: str = "accept",
               note: str = "", by: str = "用户") -> dict:
    """对剩下的待处理阻断一次性给结论（默认全部批准）。"""
    if decision not in ("accept", "reject"):
        raise ValueError("decision 只能是 accept / reject")
    blocking = [str(item) for item in (blocking_ids or []) if str(item)]
    data = load(run_dir)
    entry = data.get("s3") if isinstance(data.get("s3"), dict) else {}
    decisions = entry.get("decisions") if isinstance(entry.get("decisions"), dict) else {}
    at = _now()
    label = "批准" if decision == "accept" else "拒绝"
    note = note or ("用户" + label + "剩余阻断建议")
    for issue_id in blocking:
        if issue_id not in decisions:
            decisions[issue_id] = {"decision": decision, "at": at, "by": by, "note": note}
    return _write(run_dir, data, decisions, blocking, at, by, note)
