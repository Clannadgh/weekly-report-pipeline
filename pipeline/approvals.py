"""S1/S2/S3 的人工批准状态，统一存在 run 目录的 approval.json。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

STAGES = ("s1", "s2", "s3")
FILENAME = "approval.json"


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


def approve(run_dir: str | Path, stages, note: str = "", by: str = "用户") -> dict:
    data = load(run_dir)
    at = dt.datetime.now().isoformat(timespec="seconds")
    for stage in stages:
        if stage not in STAGES:
            continue
        data[stage] = {
            "approved": True,
            "at": at,
            "by": by,
            "note": note or "用户在界面点击批准",
        }
    save(run_dir, data)
    return data
