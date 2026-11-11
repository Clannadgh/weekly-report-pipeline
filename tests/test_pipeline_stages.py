import json

from web import server


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_s3_stage_shows_pending_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS_DIR", str(tmp_path))
    run = tmp_path / "w99"
    run.mkdir()
    _write(run / "intake.json", {"run": {"period_tag": "w99"}, "state": "准入通过"})
    _write(run / "cards.json", {"state": "ok"})
    _write(run / "issues.json", {
        "state": "ok",
        "stats": {"blocking": 1},
        "issues": [{"id": "Q01", "level": "阻断", "card": "M09"}],
    })

    stages = server.build_pipeline_stages("w99")["stages"]
    assert stages["s1"]["state"] == "准入通过"
    assert stages["s2"]["state"] == "ok"
    assert stages["s3"]["state"] == "待裁决"
    assert stages["s3"]["pending"] == ["Q01"]
    assert stages["s3"]["ok"] is False


def test_s3_stage_clears_after_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS_DIR", str(tmp_path))
    run = tmp_path / "w99"
    run.mkdir()
    _write(run / "intake.json", {"run": {"period_tag": "w99"}, "state": "准入通过"})
    _write(run / "cards.json", {"state": "ok"})
    _write(run / "issues.json", {
        "state": "ok",
        "stats": {"blocking": 1},
        "issues": [{"id": "Q01", "level": "阻断", "card": "M09"}],
    })
    _write(run / "approval.json", {
        "s3": {"approved": True, "decisions": {"Q01": {"decision": "accept"}}}
    })

    stages = server.build_pipeline_stages("w99")["stages"]
    assert stages["s3"]["state"] == "已核对"
    assert stages["s3"]["ok"] is True


def test_downstream_stages_come_from_run_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS_DIR", str(tmp_path))
    run = tmp_path / "w99"
    run.mkdir()
    _write(run / "intake.json", {"run": {"period_tag": "w99"}, "state": "准入通过"})
    _write(run / "cards.json", {"state": "ok"})
    _write(run / "issues.json", {"state": "ok", "issues": [], "stats": {}})
    _write(run / "corrected.json", {"stats": {"blocked": 1}})
    (run / "draft.md").write_text("正文", encoding="utf-8")
    _write(run / "status.json", {"stage": "s5", "state": "待审核"})

    stages = server.build_pipeline_stages("w99")["stages"]
    assert stages["s4"]["state"] == "待补充"
    assert stages["s5"]["state"] == "待审核"
    assert "s6" not in stages

    _write(run / "status.json", {"stage": "s6", "state": "已交付"})
    stages = server.build_pipeline_stages("w99")["stages"]
    assert stages["s6"]["state"] == "已交付"
    assert stages["s6"]["ok"] is True


def test_s5_qc_failure_shows_red_state(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS_DIR", str(tmp_path))
    run = tmp_path / "w99"
    run.mkdir()
    _write(run / "intake.json", {"run": {"period_tag": "w99"}, "state": "准入通过"})
    draft = run / "draft.md"
    draft.write_text("正文", encoding="utf-8")
    _write(run / "status.json", {"stage": "intake", "state": "可扩写"})
    _write(run / "qc_result.json", {
        "verdict": "FAIL", "summary": "7/8 项通过",
        "draft_after_sha256": server.file_sha256(str(draft)),
    })

    stages = server.build_pipeline_stages("w99")["stages"]
    assert stages["s5"]["state"] == "质检未通过"
    assert stages["s5"]["ok"] is False


def test_s5_stale_qc_is_not_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS_DIR", str(tmp_path))
    run = tmp_path / "w99"
    run.mkdir()
    _write(run / "intake.json", {"run": {"period_tag": "w99"}, "state": "准入通过"})
    (run / "draft.md").write_text("新正文", encoding="utf-8")
    _write(run / "status.json", {"stage": "intake", "state": "可扩写"})
    _write(run / "qc_result.json", {
        "verdict": "PASS", "summary": "8/8 项通过",
        "draft_after_sha256": "old-hash",
    })

    stages = server.build_pipeline_stages("w99")["stages"]
    assert stages["s5"]["state"] == "未送审"
    assert stages["s5"]["ok"] is False
