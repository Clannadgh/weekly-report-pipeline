"""S5 质检未通过后的定点修改：只改稿、只重跑质检，不重跑扩写。"""
from __future__ import annotations

import json

from pipeline import s5_write


def _write(path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_run(tmp_path):
    draft = "本周华南实收较上周下降 14.0%，实收金额合计 122,054 元。\n"
    (tmp_path / "draft.md").write_text(draft, encoding="utf-8")
    _write(tmp_path / "intake.json", {
        "run": {"period_tag": "w44", "dir": "runs/w44"},
        "minutes": {"week": "2026-W44"},
    })
    _write(tmp_path / "status.json", {"state": "可扩写", "history": []})
    _write(tmp_path / "corrected.json", {"items": [{
        "id": "M09", "status": "corrected",
        "original": "本周华南整体销售与上周基本持平",
        "fixed": "本周华南实收较上周下降 14.0%，实收金额合计 122,054 元",
    }]})
    _write(tmp_path / "figures_manifest.json", {"figures": []})
    _write(tmp_path / "ledger.json", {"entries": [{
        "key": "region_hn_rev_w44", "card": "M09",
        "value": -0.1404, "display": "-14.0%",
    }, {
        "key": "region_hn_amt_w44", "card": "M09",
        "value": 122054.19, "display": "122,054",
    }]})
    _write(tmp_path / "s5_facts.json", {
        "run_id": "w44", "week": "2026-W44",
        "business": {
            "summary": "本周全公司实收 573,635 元，较上周 -9.3%。",
            "anomalies": ["区域：华南 -12.7%（本周华南实收较上周下降 14.0%，"
                          "实收金额合计 122,054 元，第44周纪要）"],
        },
        "skeleton": [{"id": "M09", "status": "corrected",
                      "fixed": "本周华南实收较上周下降 14.0%，实收金额合计 122,054 元"}],
        "stats": {},
    })
    return draft


def _passing_qc(text, business, manifest, has_target=True):
    return {"checks": [{"name": "结构完整", "ok": True, "detail": ""}]}


def _failing_qc(text, business, manifest, has_target=True):
    return {"checks": [{"name": "结构完整", "ok": False, "detail": "缺章节"}]}


def test_fix_draft_applies_edits_and_passes(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s5_write, "_qc_final", _passing_qc)

    outcome = s5_write.fix_draft(
        tmp_path,
        edits=[{
            "find": "本周华南实收较上周下降 14.0%，实收金额合计 122,054 元",
            "replace": "本周华南实收较上周下降 12.7%",
        }],
        ledger_updates=[{"key": "region_hn_rev_w44", "value": -0.1271,
                         "display": "-12.7%"}],
        use_ai=False,
        by="测试",
    )

    assert outcome.ok is True
    assert outcome.state == "待审核"
    assert outcome.qc["verdict"] == "PASS"
    fixed = (tmp_path / "draft.md").read_text(encoding="utf-8")
    assert "下降 12.7%" in fixed and "14.0%" not in fixed
    corrected = json.loads((tmp_path / "corrected.json").read_text(encoding="utf-8"))
    assert "12.7%" in corrected["items"][0]["fixed"]
    facts = json.loads((tmp_path / "s5_facts.json").read_text(encoding="utf-8"))
    assert "12.7%" in facts["business"]["anomalies"][0]
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["entries"][0]["display"] == "-12.7%"
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["state"] == "待审核"
    audit = json.loads((tmp_path / "s5_fixes.json").read_text(encoding="utf-8"))
    assert audit["history"][-1]["qc_verdict"] == "PASS"
    assert "12.7" in (tmp_path / "draft.md").read_text(encoding="utf-8")


def test_fix_draft_rejects_missing_find_without_writing(tmp_path, monkeypatch):
    draft = _make_run(tmp_path)
    monkeypatch.setattr(s5_write, "_qc_final", _passing_qc)

    outcome = s5_write.fix_draft(
        tmp_path,
        edits=[{"find": "不存在的原文", "replace": "新文本"}],
        use_ai=False,
    )

    assert outcome.ok is False
    assert "找不到" in outcome.message
    assert (tmp_path / "draft.md").read_text(encoding="utf-8") == draft
    assert not (tmp_path / "s5_fixes.json").exists()


def test_fix_draft_recheck_failure_keeps_state(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s5_write, "_qc_final", _failing_qc)

    outcome = s5_write.fix_draft(
        tmp_path,
        edits=[{"find": "122,054", "replace": "177,306"}],
        use_ai=False,
    )

    assert outcome.ok is False
    assert outcome.state == "可扩写"
    assert "仍未通过" in outcome.message
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "可扩写"
    # 修改仍然落盘，用户可以继续定点改
    assert "177,306" in (tmp_path / "draft.md").read_text(encoding="utf-8")


def test_get_s5_draft_returns_text_and_qc(tmp_path):
    from pipeline.agent import Session

    run_dir = tmp_path / "w44"
    run_dir.mkdir()
    _make_run(run_dir)
    _write(run_dir / "qc_result.json", {
        "verdict": "FAIL", "summary": "7/8 项通过",
        "checks": [{"name": "AI复核", "ok": False, "detail": "未通过"}],
        "ai_review": {"ok": False, "issues": ["口径矛盾"], "suggestions": ["统一口径"]},
    })

    session = Session([], run_root=tmp_path)
    data = session._get_s5_draft({"run_id": "w44"})

    assert data["ok"] is True
    assert data["fixable"] is True
    assert "华南" in data["draft"]
    assert data["qc"]["issues"] == ["口径矛盾"]
    assert data["ledger_hints"][0]["key"] == "region_hn_amt_w44"


def test_issue_waiver_only_removes_named_item():
    review = {"ok": False, "issues": ["『两成』估算无依据", "华南口径矛盾"]}

    out = s5_write._apply_issue_waivers(
        review, [{"match": "两成", "reason": "这是纪要原句"}])

    assert out["ok"] is False
    assert out["issues"] == ["华南口径矛盾"]
    assert out["waived"] == ["『两成』估算无依据"]


def test_fix_draft_waives_single_review_issue_and_passes(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s5_write, "_qc_final", _passing_qc)
    monkeypatch.setattr(s5_write, "_load_data", lambda *_: (None, {}))
    monkeypatch.setattr(s5_write, "_with_target_column", lambda frame, meta: frame)

    def fake_review(*args, **kwargs):
        return {"ok": False, "issues": ["『两成』估算无依据"],
                "suggestions": [], "queries": []}

    monkeypatch.setattr(s5_write, "_ai_review_draft", fake_review)
    outcome = s5_write.fix_draft(
        tmp_path,
        edits=[],
        waive_issues=[{"match": "两成", "reason": "这是纪要原句"}],
        use_ai=True,
        by="测试",
    )

    assert outcome.ok is True
    assert outcome.state == "待审核"
    assert outcome.qc["ai_review"]["waived"] == ["『两成』估算无依据"]
    assert "用户豁免 1 条" in outcome.qc["checks"][-1]["detail"]
    audit = json.loads((tmp_path / "s5_fixes.json").read_text(encoding="utf-8"))
    assert audit["history"][-1]["waive_issues"][0]["match"] == "两成"


def test_code_qc_failure_cannot_be_waived(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s5_write, "_qc_final", _failing_qc)
    monkeypatch.setattr(s5_write, "_load_data", lambda *_: (None, {}))
    monkeypatch.setattr(s5_write, "_with_target_column", lambda frame, meta: frame)
    monkeypatch.setattr(s5_write, "_ai_review_draft",
                        lambda *a, **k: {"ok": False, "issues": ["复核问题"]})

    outcome = s5_write.fix_draft(
        tmp_path,
        edits=[],
        waive_issues=[{"match": "复核问题", "reason": "误判"}],
        use_ai=True,
    )

    assert outcome.ok is False
    assert "仍未通过" in outcome.message
