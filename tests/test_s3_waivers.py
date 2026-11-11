"""S3 沿用 S1 人工豁免的测试。"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline import pandasai_glm
from pipeline import s3_reconcile as s3


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_waived_entity_is_skipped_and_recorded(tmp_path, monkeypatch):
    run = tmp_path / "w99"
    (run / "inputs").mkdir(parents=True)
    (run / "inputs" / "sales.csv").write_text(
        "记录ID,销售日期,门店名称,渠道,实收金额\n"
        "R1,2026-10-19,北京朝阳店,堂食自提,100\n",
        encoding="utf-8")
    _write(run / "intake.json", {
        "state": "准入通过",
        "run": {"period_tag": "w99", "dir": str(run)},
        "minutes": {"week": "2026-W99"},
        "files": [{"name": "sales.csv", "path": "inputs/sales.csv", "role": "数据"}],
        "waived": [{
            "at": "2026-11-11T09:03:51", "by": "用户",
            "items": ["纪要提到的渠道「商场店、街边店、社区店、写字楼店」在数据列「渠道」里不存在"],
            "reason": "渠道只是概述",
        }],
    })
    _write(run / "cards.json", {"state": "ok", "cards": [{
        "id": "M01", "checkable": "能", "entity": "商场店", "entity_type": "渠道",
        "metric": "实收", "expect": "具体数值", "period": "2026-10-19 至 2026-10-25",
        "compare_period": "", "quote": "商场店实收 100 元",
    }]})

    class _QA:
        def ask(self, *args, **kwargs):
            raise AssertionError("被豁免的卡片不应再取数")

    monkeypatch.setattr(pandasai_glm, "make_qa", lambda *a, **k: _QA())
    result, _ = s3.run_s3(run)

    assert result.stats.waived == 1
    assert result.issues == []
    assert result.waived[0]["cards"] == ["M01"]
    saved = json.loads((run / "issues.json").read_text(encoding="utf-8"))
    assert saved["stats"]["waived"] == 1
    assert saved["waived"][0]["item"].startswith("纪要提到的渠道")
