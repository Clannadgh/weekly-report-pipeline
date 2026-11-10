"""Altair 固定模板出图的确定性测试：不调模型，只验规格与 300dpi 产物。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from pipeline import s5_altair
import pipeline.s5_write as s5


def _spec(kind="barh", labels=None, values=None, unit="元"):
    labels = labels or ["A店", "B店", "C店"]
    values = values or [300, 200, 100]
    return {"number": 1, "name": "fig01", "title": "测试图", "finding_id": "D1",
            "kind": kind, "unit": unit, "labels": labels,
            "keys": [f"k{i}" for i in range(len(labels))],
            "points": [{"key": f"k{i}", "value": v, "unit": unit}
                       for i, v in enumerate(values)]}


def test_build_chart_barh_and_share():
    barh = s5_altair.build_chart(_spec("barh")).to_dict()
    assert barh["layer"][0]["mark"]["type"] == "bar"
    share = s5_altair.build_chart(_spec("share", ["堂食", "外卖"], [70, 30])).to_dict()
    assert share["layer"][0]["mark"]["type"] == "bar"
    assert share["layer"][1]["mark"]["type"] == "text"


def test_render_chart_writes_svg_png_and_spec(tmp_path):
    out = tmp_path / "figures" / "fig01.png"
    script = tmp_path / "figures" / "src" / "fig01.py"
    result = s5_altair.render_chart(_spec(), out_png=out, script_path=script)
    assert result.ok, result.error
    assert result.renderer == "altair"
    assert out.is_file() and out.with_suffix(".svg").is_file()
    assert Path(result.script_path).suffix == ".json"
    assert Path(result.script_path).is_file()
    with Image.open(out) as image:
        assert round(float(image.info["dpi"][0])) >= 300
    assert "vega-lite" in Path(result.script_path).read_text(encoding="utf-8").lower() or "mark" in Path(result.script_path).read_text(encoding="utf-8")


def test_render_chart_rejects_bad_spec(tmp_path):
    spec = _spec("barh", ["A", "B"], [1, 2])
    spec["points"] = [{"value": "x"}, {"value": "y"}]
    result = s5_altair.render_chart(spec, out_png=tmp_path / "x.png",
                                    script_path=tmp_path / "x.py")
    assert not result.ok


def test_make_figures_with_altair_manifest(tmp_path, monkeypatch):
    run = tmp_path / "w99"
    run.mkdir()
    ctx = {"tag": "w99", "week": "2026-W99", "period": "p", "compare": "c",
           "figure_specs": [{"finding_id": "D1", "title": "测试图", "kind": "barh",
                             "unit": "元", "labels": ["A", "B", "C"],
                             "keys": ["k1", "k2", "k3"],
                             "points": [{"key": "k1", "value": 3, "unit": "元"},
                                        {"key": "k2", "value": 2, "unit": "元"},
                                        {"key": "k3", "value": 1, "unit": "元"}],
                             "question": "q"}]}
    monkeypatch.setattr(s5.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(returncode=0, stdout="", stderr=""))
    manifest = s5._make_figures(run, ctx, [])
    assert manifest["renderer"] == "altair"
    item = manifest["figures"][0]
    assert item["renderer"] == "altair"
    assert set(item["files"]) == {"png", "svg"}
    assert (run / item["script"]).is_file()
    assert (run / item["files"]["svg"]).is_file()
