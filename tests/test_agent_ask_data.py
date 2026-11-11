"""主助手直接查数据：preview 读原始行，question 走 pandasai。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import pipeline.agent as agent
from pipeline.agent import Session, TOOLS


def _write_csv(path: Path, ids: list[str]) -> None:
    frame = pd.DataFrame({
        "记录ID": ids,
        "销售日期": ["2026-08-31"] * len(ids),
        "门店名称": ["北京一店"] * len(ids),
        "实收金额": [100.0 + i for i in range(len(ids))],
    })
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def test_tool_is_exposed():
    names = [tool["function"]["name"] for tool in TOOLS]
    assert "ask_data" in names


def test_preview_returns_first_rows(tmp_path):
    path = tmp_path / "sales.csv"
    _write_csv(path, ["R1", "R2", "R3"])
    session = Session([path], run_root=tmp_path)

    result = session._execute("ask_data", {"preview": 2}, "")

    assert result["ok"] is True
    assert result["table"] == "df_sales"
    assert result["total_rows"] == 3
    assert [row["记录ID"] for row in result["rows"]] == ["R1", "R2"]
    assert result["rows"][0]["实收金额"] == 100.0
    assert result["columns"] == ["记录ID", "销售日期", "门店名称", "实收金额"]


def test_preview_prefers_run_directory(tmp_path):
    chat_file = tmp_path / "session.csv"
    _write_csv(chat_file, ["CHAT"])
    run_dir = tmp_path / "w1"
    (run_dir / "inputs").mkdir(parents=True)
    _write_csv(run_dir / "inputs" / "sales.csv", ["RUN1", "RUN2"])
    (run_dir / "intake.json").write_text(json.dumps({
        "files": [{"name": "sales.csv", "path": "inputs/sales.csv", "role": "数据"}],
    }, ensure_ascii=False), encoding="utf-8")

    session = Session([chat_file], run_root=tmp_path)
    session.last_run = "w1"

    result = session._execute("ask_data", {"preview": 5}, "")

    assert result["ok"] is True
    assert result["run_id"] == "w1"
    assert [row["记录ID"] for row in result["rows"]] == ["RUN1", "RUN2"]


def test_question_goes_through_pandasai(tmp_path, monkeypatch):
    path = tmp_path / "sales.csv"
    _write_csv(path, ["R1", "R2"])
    seen: dict = {}

    class FakeQA:
        def ask(self, question, *, expect_shape, use_cache):
            seen.update({"question": question, "shape": expect_shape,
                         "use_cache": use_cache, "frames": seen.get("frames")})
            return {"ok": True, "value": 123.0, "raw": "123.0", "rows": None,
                    "flags": ["import:pandas"],
                    "code": "result = dfs[0]['实收金额'].sum()",
                    "question_hash": "qhash", "elapsed_ms": 5}

    def fake_factory(frames):
        seen["frames"] = frames
        return FakeQA()

    monkeypatch.setattr(agent, "_make_data_qa", fake_factory)
    session = Session([path], run_root=tmp_path)

    result = session._execute(
        "ask_data", {"question": "实收金额合计", "shape": "number"}, "")

    assert result["ok"] is True
    assert result["value"] == 123.0
    assert result["shape"] == "number"
    assert len(result["code_hash"]) == 16
    assert seen["shape"] == "number"
    assert seen["use_cache"] is False
    assert "只做只读取数" in seen["question"]
    assert list(seen["frames"]) == ["df_sales"]
    assert len(seen["frames"]["df_sales"]) == 2


def test_write_flags_are_rejected(tmp_path, monkeypatch):
    path = tmp_path / "sales.csv"
    _write_csv(path, ["R1"])

    class FakeQA:
        def ask(self, question, *, expect_shape, use_cache):
            return {"ok": True, "value": 1.0, "raw": "1.0", "rows": None,
                    "flags": ["writes_file:to_csv"], "code": "df.to_csv('x.csv')",
                    "question_hash": "q", "elapsed_ms": 1}

    monkeypatch.setattr(agent, "_make_data_qa", lambda frames: FakeQA())
    session = Session([path], run_root=tmp_path)

    result = session._execute("ask_data", {"question": "算一下"}, "")

    assert result["ok"] is False
    assert "写文件" in result["error"]


def test_unknown_run_is_reported(tmp_path):
    session = Session([], run_root=tmp_path)
    result = session._execute("ask_data", {"preview": 5, "run_id": "w404"}, "")
    assert result["ok"] is False
    assert "w404" in result["error"]
