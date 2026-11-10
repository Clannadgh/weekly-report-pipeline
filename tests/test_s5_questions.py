"""S5 数据发现问题清单的确定性测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from pipeline import s5_questions as sq
from pipeline.s5_discover import discover


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _facts(run: Path, discoveries=None) -> None:
    _write(run / "s5_facts.json", {"discoveries": discoveries or []})


# ---------------------------------------------------------------- 规范化

def test_normalize_keeps_order_and_fixes_ids():
    out = sq.normalize_questions([
        {"id": "D2", "question": " 按门店分组求实收 ", "shape": "TABLE", "reason": " 看门店 "},
        {"question": "全公司实收合计", "shape": "number"},
        {"id": "D2", "question": "重复编号"},
    ])
    assert [q["id"] for q in out] == ["D2", "D3", "D2_2"]
    assert out[0]["question"] == "按门店分组求实收"
    assert out[0]["shape"] == "table"
    assert out[0]["reason"] == "看门店"


def test_normalize_strict_rejects_bad_input():
    with pytest.raises(ValueError):
        sq.normalize_questions([], strict=True)
    with pytest.raises(ValueError):
        sq.normalize_questions([{"question": "x", "shape": "pie"}], strict=True)
    with pytest.raises(ValueError):
        sq.normalize_questions([{"shape": "number"}], strict=True)
    with pytest.raises(ValueError):
        sq.normalize_questions([{"question": "x"}] * 13, strict=True)


def test_diff_questions_reports_changed_reused_removed():
    old = [
        {"id": "D1", "question": "全公司实收", "shape": "number", "reason": "总量"},
        {"id": "D2", "question": "毛利率", "shape": "number", "reason": "结构"},
    ]
    new = [
        {"id": "D1", "question": "全公司实收", "shape": "number", "reason": "总量"},
        {"id": "D2", "question": "毛利率（不含退货）", "shape": "number", "reason": "结构"},
        {"id": "D3", "question": "渠道实收", "shape": "number", "reason": "渠道"},
    ]
    diff = sq.diff_questions(new, old)
    assert diff["changed_ids"] == ["D2", "D3"]
    assert diff["reused_ids"] == ["D1"]
    assert diff["removed_ids"] == []
    assert diff["order_changed"] is False

    diff2 = sq.diff_questions([new[1], new[0]], old)
    assert diff2["order_changed"] is True
    assert diff2["removed_ids"] == []
    assert diff2["changed_ids"] == ["D2"]

    diff3 = sq.diff_questions([new[0]], old)
    assert diff3["removed_ids"] == ["D2"]
    assert diff3["changed_ids"] == []
    assert diff3["reused_ids"] == ["D1"]


def test_next_id_uses_max_existing():
    assert sq.next_id([]) == "D1"
    assert sq.next_id([{"id": "D1"}, {"id": "D7"}]) == "D8"
    assert sq.next_id([{"id": "D2"}, {"id": "D2_2"}]) == "D3"


# ---------------------------------------------------------------- 读写

def test_save_load_reset_roundtrip(tmp_path):
    run = tmp_path / "w99"
    run.mkdir()
    doc = sq.save_plan(run, [
        {"id": "D1", "question": "按门店分组求实收", "shape": "table", "reason": "看门店差异"},
        {"id": "D2", "question": "全公司实收合计", "shape": "number", "reason": "看总量"},
    ], source="user", run_id="w99")
    assert doc["source"] == "user"
    loaded = sq.load_plan(run)
    assert loaded is not None
    assert loaded["run_id"] == "w99"
    assert len(loaded["questions"]) == 2

    assert sq.reset_plan(run) is True
    assert sq.load_plan(run) is None
    assert sq.reset_plan(run) is False


def test_load_rejects_corrupt_or_empty_plan(tmp_path):
    run = tmp_path / "w99"
    run.mkdir()
    (run / sq.PLAN_FILE).write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError):
        sq.load_plan(run)

    _write(run / sq.PLAN_FILE, {"questions": []})
    with pytest.raises(ValueError):
        sq.load_plan(run)


# ---------------------------------------------------------------- 生效与脏状态

def test_effective_falls_back_to_findings(tmp_path):
    run = tmp_path / "w99"
    _facts(run, [
        {"id": "D1", "question": "按门店分组求实收", "shape": "table", "reason": "看差异"},
    ])
    data = sq.effective(run)
    assert data["source"] == "last_run"
    assert data["count"] == 1
    assert data["applied"] is True
    assert data["dirty"] is False


def test_effective_marks_dirty_until_rerun(tmp_path):
    run = tmp_path / "w99"
    _facts(run, [
        {"id": "D1", "question": "按门店分组求实收", "shape": "table", "reason": "看差异"},
    ])
    sq.save_plan(run, [
        {"id": "D1", "question": "按门店和品类分组求实收", "shape": "table", "reason": "看差异"},
    ], source="user", run_id="w99")
    data = sq.effective(run)
    assert data["source"] == "saved"
    assert data["dirty"] is True
    assert data["applied"] is False

    # 模拟重跑后的 facts：hash 与清单一致
    questions = sq.load_plan(run)["questions"]
    _write(run / "s5_facts.json", {
        "discoveries": [], "questions_hash": sq.questions_hash(questions),
    })
    data = sq.effective(run)
    assert data["dirty"] is False
    assert data["applied"] is True


def test_effective_exposes_question_diff(tmp_path):
    run = tmp_path / "w99"
    old = [
        {"id": "D1", "question": "全公司实收", "shape": "number", "reason": "总量"},
        {"id": "D2", "question": "毛利率", "shape": "number", "reason": "结构"},
    ]
    _write(run / "s5_facts.json", {
        "questions": old,
        "questions_hash": sq.questions_hash(old),
        "discoveries": [{"id": "D1"}, {"id": "D2"}],
    })
    new = [
        old[0],
        {"id": "D2", "question": "毛利率（不含退货）", "shape": "number", "reason": "结构"},
        {"id": "D3", "question": "渠道实收", "shape": "number", "reason": "渠道"},
    ]
    sq.save_plan(run, new, source="user", run_id="w99")
    data = sq.effective(run)
    assert data["changed_ids"] == ["D2", "D3"]
    assert data["reused_ids"] == ["D1"]
    assert data["changed_count"] == 2
    assert data["reused_count"] == 1
    assert data["dirty"] is True


def test_effective_no_plan_no_findings(tmp_path):
    run = tmp_path / "w99"
    run.mkdir()
    data = sq.effective(run)
    assert data["source"] == "none"
    assert data["count"] == 0


# ---------------------------------------------------------------- discover 不重新出题

def test_discover_uses_plan_without_calling_chat(tmp_path):
    calls = {"chat": 0}

    def chat_fn(*args, **kwargs):
        calls["chat"] += 1
        raise AssertionError("有清单时不应再调模型出题")

    class QA:
        model = "qa-stub"

        def ask(self, question, expect_shape="number", use_cache=True):
            return {"ok": True, "value": 12.0, "code": "result = 12.0", "rows": None}

    out = discover(
        tmp_path, pd.DataFrame({"a": [1]}), {}, {}, [],
        chat_fn=chat_fn, qa_factory=lambda *args, **kwargs: QA(),
        questions=[{"id": "D1", "question": "全公司实收合计", "shape": "number",
                    "reason": "看总量"}],
    )
    assert calls["chat"] == 0
    assert out["source"] == "plan"
    assert out["questions"][0]["id"] == "D1"
    assert out["findings"][0]["value"] == 12.0


def test_discover_rejects_empty_plan(tmp_path):
    out = discover(
        tmp_path, pd.DataFrame({"a": [1]}), {}, {}, [],
        chat_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应调模型")),
        qa_factory=lambda *args, **kwargs: None,
        questions=[],
    )
    assert out["findings"] == []
    assert out["questions"] == []
    assert any("问题清单无效" in line for line in out["log"])


# ---------------------------------------------------------------- 主 agent 工具

def test_agent_question_tool(tmp_path):
    from pipeline.agent import TOOLS, Session

    assert any(tool["function"]["name"] == "s5_questions" for tool in TOOLS)
    run = tmp_path / "w99"
    _facts(run, [
        {"id": "D1", "question": "按门店分组求实收", "shape": "table", "reason": "看差异"},
        {"id": "D2", "question": "全公司实收合计", "shape": "number", "reason": "看总量"},
    ])
    session = Session([], run_root=tmp_path)
    session.last_run = "w99"

    listed = session._s5_questions({"action": "list"})
    assert listed["ok"] is True
    assert listed["count"] == 2
    dispatched = session._execute("s5_questions", {"action": "list"}, "")
    assert dispatched["ok"] is True
    assert dispatched["count"] == 2

    added = session._s5_questions({
        "action": "add", "question": "按渠道分组求实收",
        "shape": "table", "reason": "看渠道结构",
    })
    assert added["ok"] is True
    assert added["count"] == 3
    assert added["questions"][-1]["id"] == "D3"
    assert added["dirty"] is True

    updated = session._s5_questions({
        "action": "update", "id": "D3", "question": "按渠道分组求实收金额",
    })
    assert updated["questions"][-1]["question"] == "按渠道分组求实收金额"

    deleted = session._s5_questions({"action": "delete", "id": "D2"})
    assert deleted["ok"] is True
    assert deleted["count"] == 2

    missing = session._s5_questions({"action": "delete", "id": "D9"})
    assert missing["ok"] is False
    assert "D9" in missing["error"]

    reset = session._s5_questions({"action": "reset"})
    assert reset["ok"] is True
    assert reset["source"] == "last_run"
    assert not (run / sq.PLAN_FILE).exists()
