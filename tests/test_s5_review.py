"""S5 审核（送审与意见归集）的确定性测试。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline import s5_review as review


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_run(root: Path, state: str = "已生成") -> None:
    _write(root / "intake.json", {
        "state": "准入通过",
        "run": {"period_tag": "w99", "dir": str(root)},
        "minutes": {"week": "2026-W99", "period_start": "2026-10-19",
                    "period_end": "2026-10-25"},
        "files": [{"name": "sales.csv", "path": "inputs/sales.csv", "role": "数据"}],
    })
    (root / "draft.md").write_text(
        "# 2026 年第 99 周经营周报\n\n## 经营情况\n\n本周实收较上周下降。\n\n"
        "## 数据发现\n\n门店差异明显。\n", encoding="utf-8")
    _write(root / "figures_manifest.json", {
        "figures": [{"number": 1, "title": "门店实收", "caption": "图 1"}],
    })
    _write(root / "corrected.json", {
        "items": [
            {"id": "M01", "status": "kept", "original": "本周实收下降。",
             "fixed": "本周实收较上周下降。", "confirmed": True},
            {"id": "M02", "status": "blocked", "original": "某店恢复增长。",
             "fixed": "某店恢复增长。", "confirmed": False, "note": "数据无法核实"},
            {"id": "M03", "status": "future", "original": "下周做活动。",
             "fixed": "下周做活动。", "confirmed": True},
        ],
        "stats": {"blocked": 1, "future": 1},
    })
    _write(root / "ledger.json", {"entries": [
        {"key": "old_key_w99", "display": "1", "value": 1.0, "unit": "元"},
    ]})
    _write(root / "s5_facts.json", {
        "stats": {"chars_body_rendered": 1200, "figures": 1, "discoveries": 2},
        "discoveries": [{"id": "D1", "question": "门店实收", "display": "1"}],
        "future": [{"id": "M03", "fixed": "下周做活动。", "status": "future"}],
        "blocked": [{"id": "M02", "status": "blocked"}],
    })
    _write(root / "status.json", {
        "stage": "s5", "state": state, "updated_by": "S5",
        "at": "2026-10-25T12:00:00", "note": "已生成。", "history": [],
    })


def _draft_hash(root: Path) -> str:
    return hashlib.sha256((root / "draft.md").read_bytes()).hexdigest()


# ---------------------------------------------------------------- 送审

def test_submit_requires_generated(tmp_path):
    _make_run(tmp_path, state="可扩写")
    out = review.submit(tmp_path)
    assert not out.ok
    assert "已生成" in out.message


def test_submit_writes_record_and_materials(tmp_path):
    _make_run(tmp_path)
    out = review.submit(tmp_path)
    assert out.ok
    assert out.state == "待审核"
    record = review.load_record(tmp_path)
    assert record["state"] == "待审核"
    assert record["round"] == 0
    assert len(out.materials["sections"]) == 2
    assert out.materials["sections"][0]["heading"] == "经营情况"
    assert out.materials["sections"][0]["first"].startswith("本周实收")
    kinds = {item["kind"] for item in out.materials["unconfirmed"]}
    assert "blocked" in kinds and "future" in kinds
    assert out.materials["qc"]["available"] is False
    assert "并入 S5" in out.materials["qc"]["note"]
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "待审核" and status["updated_by"] == "S5"


def test_stale_qc_is_not_counted_as_pass(tmp_path):
    _make_run(tmp_path)
    _write(tmp_path / "qc_result.json", {
        "verdict": "FAIL", "state": "待审核",
        "draft_after_sha256": "old-hash",
    })
    out = review.submit(tmp_path)
    assert out.ok
    assert out.materials["qc"]["available"] is False
    assert out.materials["qc"]["stale"] is True

    _write(tmp_path / "qc_result.json", {
        "verdict": "FAIL", "state": "待审核",
        "draft_after_sha256": _draft_hash(tmp_path),
    })
    # 重新回到已生成才能再次送审
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    status["state"] = "已生成"
    _write(tmp_path / "status.json", status)
    out = review.submit(tmp_path)
    assert not out.ok
    assert "PASS" in out.message


# ---------------------------------------------------------------- 审核决定

def test_pass_records_reviewer(tmp_path):
    _make_run(tmp_path)
    review.submit(tmp_path)
    out = review.decide(tmp_path, "张三", "pass", [])
    assert out.ok
    assert out.state == "已通过"
    record = review.load_record(tmp_path)
    assert record["decision"] == "pass"
    assert record["reviewer"] == "张三"
    assert record["signature"] == "张三"
    assert record["signed_at"]
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "已通过" and status["updated_by"] == "S5"
    assert status["signature"] == "张三"


def test_reject_requires_comment_and_maps_targets(tmp_path):
    _make_run(tmp_path)
    review.submit(tmp_path)
    bad = review.decide(tmp_path, "张三", "reject", [])
    assert not bad.ok
    assert "具体意见" in bad.message

    out = review.decide(tmp_path, "张三", "reject", [
        {"raw": "第 2 段门店实收和台账对不上", "category": "number", "where": "第 2 段"},
        {"raw": "第 1 段太长", "category": "length", "where": "第 1 段"},
    ])
    assert out.ok
    assert out.state == "退回修改"
    record = review.load_record(tmp_path)
    assert record["round"] == 1
    assert [item["back_to"] for item in record["comments"]] == ["S3", "S5"]
    assert len(record["mapping"]) == 2
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "退回修改"
    assert "审核退回：意见 1、2" in status["note"]
    assert "S3→S4" in status["note"] and "S5" in status["note"]


def test_three_rejects_pause(tmp_path):
    _make_run(tmp_path)
    for index in range(3):
        review.submit(tmp_path)
        out = review.decide(tmp_path, "张三", "reject", [
            {"raw": "第 1 段需要改", "category": "wording", "where": "第 1 段"},
        ])
        assert out.ok
        if index < 2:
            status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
            status["state"] = "已生成"
            _write(tmp_path / "status.json", status)
    record = review.load_record(tmp_path)
    assert record["round"] == 3
    assert record["paused"] is True
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["paused"] is True
    assert "需线下沟通" in status["note"]


# ---------------------------------------------------------------- 主 agent

def test_agent_review_tool(tmp_path):
    from pipeline.agent import TOOLS, Session

    names = {tool["function"]["name"] for tool in TOOLS}
    assert "run_s5" in names and "get_review" in names
    assert "run_s6" in names and "get_delivery" in names
    assert "run_deliver" not in names
    run_dir = tmp_path / "w99"
    _make_run(run_dir)
    session = Session([], run_root=tmp_path)
    session.last_run = "w99"
    review.submit(run_dir)
    review.decide(run_dir, "李四", "reject", [
        {"raw": "第 1 段太长", "category": "length", "where": "第 1 段"},
    ])
    got = session._execute("get_review", {}, "")
    assert got["ok"] is True
    assert got["state"] == "退回修改"
    assert got["comments"][0]["back_to"] == "S5"
