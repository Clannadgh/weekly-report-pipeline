"""S4 修纪要的确定性测试：不调模型，只验校验与落盘。"""
from __future__ import annotations

import json
from pathlib import Path

import pipeline.s4_draft as s4

QUOTE = "本周华南整体销售与上周基本持平"


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_run(root: Path, decision: str | None = "accept",
              level: str = "阻断") -> None:
    intake = {
        "state": "准入通过",
        "run": {"period_tag": "w99", "dir": "runs/w99"},
        "minutes": {"week": "2026-W99", "period_start": "2026-01-01",
                    "period_end": "2026-01-07"},
    }
    cards = {
        "state": "ok", "stats": {"cards": 1, "checkable": 1},
        "cards": [{"id": "M01", "quote": QUOTE, "entity": "华南", "metric": "实收",
                   "unit": "元", "expect": "持平", "checkable": "能"}],
        "future": [],
    }
    issues = {
        "state": "ok",
        "stats": {"blocking": 1 if level == "阻断" else 0,
                  "warn": 1 if level == "提示" else 0,
                  "info": 1 if level == "信息" else 0, "checked": 1},
        "issues": [{"id": "Q01", "level": level, "type": "number", "card": "M01",
                    "subject": "华南 实收", "minutes_said": QUOTE,
                    "raw": "-14.0%", "value": -0.1404,
                    "suggest": "以数据为准改整句"}],
    }
    ledger = {"entries": [
        {"key": "region_hn_amt_w99", "card": "M01", "display": "122,054",
         "unit": "元", "spec": "时间范围 2026-01-01 至 2026-01-07"},
        {"key": "region_hn_rev_w99", "card": "M01", "display": "-14.0%",
         "unit": "%", "spec": "较上周"},
    ]}
    status = {"stage": "intake", "state": "准入通过", "updated_by": "S1",
              "at": "2026-01-01T00:00:00", "note": "", "history": []}
    _write(root / "intake.json", intake)
    _write(root / "cards.json", cards)
    _write(root / "issues.json", issues)
    _write(root / "ledger.json", ledger)
    _write(root / "status.json", status)
    if decision:
        _write(root / "approval.json", {"s3": {"approved": True, "decisions": {
            "Q01": {"decision": decision, "at": "2026-01-01T01:00:00", "by": "用户"}
        }}})


def _fake(text: str):
    def chat(messages, **kwargs):
        return {"text": text, "model": "fake", "finish_reason": "stop"}
    return chat


def _payload(fixed: str, basis: str = "region_hn_amt_w99、region_hn_rev_w99",
             status: str = "corrected", issue: str = "Q01") -> str:
    return json.dumps({"items": [{
        "id": "M01", "original": QUOTE, "fixed": fixed, "basis": basis,
        "status": status, "confirmed": True, "issue": issue, "note": "",
    }], "cards": []}, ensure_ascii=False)


def test_accept_writes_corrected_and_status(tmp_path, monkeypatch):
    _make_run(tmp_path)
    fixed = "本周华南整体实收 122,054 元，较上周 -14.0%"
    monkeypatch.setattr(s4, "chat", _fake(_payload(fixed)))

    outcome = s4.run_s4(tmp_path)
    assert outcome.ok and outcome.state == "可扩写"
    item = outcome.corrected["items"][0]
    assert item["status"] == "corrected" and "122,054" in item["fixed"]
    assert outcome.corrected["stats"] == {"corrected": 1, "kept": 0, "future": 0, "blocked": 0}
    assert outcome.corrected["decisions"][0]["choice"] == "data"

    saved = json.loads((tmp_path / "corrected.json").read_text(encoding="utf-8"))
    assert saved["items"][0]["basis"] == "region_hn_amt_w99、region_hn_rev_w99"
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "可扩写" and status["updated_by"] == "S4"
    assert status["history"][-1]["updated_by"] == "S4"


def test_reject_forces_keep(tmp_path, monkeypatch):
    _make_run(tmp_path, decision="reject")
    monkeypatch.setattr(s4, "chat", _fake(_payload("本周华南整体实收 122,054 元")))

    outcome = s4.run_s4(tmp_path)
    item = outcome.corrected["items"][0]
    assert item["status"] == "kept" and item["fixed"] == QUOTE
    assert "用户拒绝该建议" in item["note"]
    assert outcome.corrected["decisions"][0]["choice"] == "skip"
    assert outcome.state == "可扩写"


def test_unknown_basis_blocks(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s4, "chat", _fake(_payload("本周华南整体实收 122,054 元", basis="no_such_key")))

    outcome = s4.run_s4(tmp_path)
    item = outcome.corrected["items"][0]
    assert item["status"] == "blocked" and "台账没有键" in item["note"]
    assert outcome.state == "待补充"
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "待补充"


def test_foreign_number_blocks(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s4, "chat", _fake(_payload("本周华南整体实收 999,999 元")))

    outcome = s4.run_s4(tmp_path)
    item = outcome.corrected["items"][0]
    assert item["status"] == "blocked" and "数字不在原句或台账里" in item["note"]


def test_banned_word_blocks(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s4, "chat", _fake(_payload("本周华南整体实收大约 122,054 元")))

    outcome = s4.run_s4(tmp_path)
    item = outcome.corrected["items"][0]
    assert item["status"] == "blocked" and "禁用词" in item["note"]


def test_correction_must_use_ledger_display(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s4, "chat", _fake(_payload("本周华南整体销售与上周持平")))

    outcome = s4.run_s4(tmp_path)
    item = outcome.corrected["items"][0]
    assert item["status"] == "blocked" and "没有引用台账数值" in item["note"]


def test_accepted_blocker_cannot_be_marked_future(tmp_path, monkeypatch):
    _make_run(tmp_path)
    monkeypatch.setattr(s4, "chat", _fake(_payload(QUOTE, status="future")))

    outcome = s4.run_s4(tmp_path)
    item = outcome.corrected["items"][0]
    assert item["status"] == "blocked" and "未改写" in item["note"]


def test_undecided_blocker_stops_before_model(tmp_path, monkeypatch):
    _make_run(tmp_path, decision=None)
    called = []
    monkeypatch.setattr(s4, "chat", lambda *args, **kwargs: called.append(1))

    outcome = s4.run_s4(tmp_path)
    assert not outcome.ok and outcome.state == "待补充"
    assert outcome.corrected is None and not called
    assert not (tmp_path / "corrected.json").exists()
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "待补充"


def test_info_level_is_never_rewritten(tmp_path, monkeypatch):
    _make_run(tmp_path, decision=None, level="信息")
    monkeypatch.setattr(s4, "chat", _fake(_payload("本周华南整体实收 122,054 元", issue="Q01")))

    outcome = s4.run_s4(tmp_path)
    item = outcome.corrected["items"][0]
    assert item["status"] == "kept" and item["fixed"] == QUOTE
    assert "信息项不改原文" in item["note"]


def test_agent_s4_tool_summarizes(tmp_path, monkeypatch):
    from pipeline.agent import Session, TOOLS
    from pipeline.s4_draft import S4Outcome

    assert any(tool["function"]["name"] == "run_s4" for tool in TOOLS)

    (tmp_path / "w99").mkdir()
    session = Session([], run_root=tmp_path)
    session.last_run = "w99"
    corrected = {
        "week": "2026-W99",
        "items": [
            {"id": "M01", "status": "corrected", "original": "x", "fixed": "y",
             "basis": "k", "issue": "Q01", "note": "", "confirmed": True},
            {"id": "M02", "status": "kept", "original": "a", "fixed": "a",
             "basis": "", "issue": "", "note": "", "confirmed": True},
        ],
        "cards": [{"id": "T4-1", "question": "q"}],
        "future": [{"text": "f"}],
        "stats": {"corrected": 1, "kept": 1, "future": 0, "blocked": 0},
    }
    monkeypatch.setattr("pipeline.s4_draft.run_s4",
                        lambda folder: S4Outcome(True, "可扩写", "ok", corrected,
                                                 ["log"], "w99", "runs/w99", "2026-W99"))

    out = session._execute("run_s4", {}, "")
    assert out["ok"] and out["state"] == "可扩写"
    assert out["stats"]["corrected"] == 1
    assert [item["id"] for item in out["items"]] == ["M01"]
    assert out["future_count"] == 1 and out["cards"][0]["id"] == "T4-1"


def test_agent_s4_blocked_has_no_corrected(tmp_path, monkeypatch):
    from pipeline.agent import Session
    from pipeline.s4_draft import S4Outcome

    (tmp_path / "w99").mkdir()
    session = Session([], run_root=tmp_path)
    session.last_run = "w99"
    monkeypatch.setattr("pipeline.s4_draft.run_s4",
                        lambda folder: S4Outcome(False, "待补充", "先去裁决", None,
                                                 ["log"], "w99", "runs/w99", "2026-W99"))

    out = session._run_s4({})
    assert not out["ok"] and out["state"] == "待补充"
    assert out["items"] == [] and out["message"] == "先去裁决"
