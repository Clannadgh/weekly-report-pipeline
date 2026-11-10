"""pandasai 图表渲染的确定性测试：全部用 fake agent，不真调 pandasai/GLM。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pipeline import s5_chart
import pipeline.s5_write as s5


class FakeAgent:
    def __init__(self, work: Path, *, result_type="plot", code=None, dpi=(300, 300),
                 outside: Path | None = None):
        self.work = Path(work)
        self.result_type = result_type
        self.code = code if code is not None else (
            "import matplotlib.pyplot as plt\n"
            "plt.barh(df[\"分组\"], df[\"数值\"])\n"
            "plt.savefig(\"temp_chart.png\", dpi=300)\n"
            "result = {\"type\": \"plot\", \"value\": \"temp_chart.png\"}\n"
        )
        self.dpi = dpi
        self.outside = outside
        self.config = SimpleNamespace(model="fake")
        self.last_result = None
        self.last_code_generated = None
        self.last_code_executed = None
        self.last_prompt_id = None

    def chat(self, prompt, output_type=None):
        assert output_type == "plot"
        if self.result_type != "plot":
            self.last_result = {"type": self.result_type, "value": 1}
            self.last_code_generated = self.code
            return "1"
        target = self.outside or (self.work / "chart.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 600), "white").save(target, dpi=self.dpi)
        self.last_result = {"type": "plot", "value": str(target)}
        self.last_code_generated = self.code
        return str(target)


def _spec(kind="barh", labels=None, values=None):
    labels = labels or ["A", "B", "C"]
    values = values or [3, 2, 1]
    return {"name": "fig01", "title": "测试图", "finding_id": "D1", "kind": kind,
            "unit": "元", "labels": labels,
            "keys": [f"k{i}" for i in range(len(labels))],
            "points": [{"key": f"k{i}", "value": v, "unit": "元"}
                       for i, v in enumerate(values)]}


def test_build_chart_frame_scales_percentages():
    spec = _spec("bar", ["A", "B"], [0.42, 0.58])
    spec["unit"] = "%"
    frame, meta = s5_chart.build_chart_frame(spec)
    assert list(frame.columns) == ["分组", "数值"]
    assert meta["unit_scale"] == 100.0
    assert frame["数值"].tolist() == pytest.approx([42.0, 58.0])

    spec2 = _spec("bar", ["A", "B"], [42.0, 58.0])
    spec2["unit"] = "%"
    _, meta2 = s5_chart.build_chart_frame(spec2)
    assert meta2["unit_scale"] == 1.0


def test_build_chart_frame_rejects_mixed_units():
    spec = _spec("bar", ["A", "B"], [0.42, 58.0])
    spec["unit"] = "%"
    with pytest.raises(s5_chart.ChartRenderError):
        s5_chart.build_chart_frame(spec)


def test_prompt_contains_kind_title_and_300dpi():
    for kind in ("barh", "bar", "line", "share"):
        spec = _spec(kind, ["A", "B"], [6, 4])
        frame, meta = s5_chart.build_chart_frame(spec)
        prompt = s5_chart.build_chart_prompt(spec, frame, meta)
        assert "测试图" in prompt
        assert "dpi=300" in prompt
        assert "df[\"分组\"]" in prompt and "df[\"数值\"]" in prompt
        assert ("barh" in prompt) if kind == "barh" else True
        assert ("100% 堆叠" in prompt) if kind == "share" else True


def _render(tmp_path, monkeypatch, **agent_kwargs):
    work = tmp_path / "work"
    out = tmp_path / "figures" / "fig01.png"
    script = tmp_path / "figures" / "src" / "fig01.py"

    def factory(frame, work_dir):
        return FakeAgent(work_dir, **agent_kwargs)

    return s5_chart.render_chart(
        _spec(), out_png=out, script_path=script, work_dir=work,
        agent_factory=factory, prepare_style=False,
    )


def test_render_chart_success(tmp_path):
    result = _render(tmp_path, None)
    assert result.ok, result.error
    assert result.quality["dpi"] == [300, 300]
    assert Path(result.png_path).is_file()
    assert Path(result.script_path).is_file()
    assert "pandasai 生成图表代码" in Path(result.script_path).read_text(encoding="utf-8")


def test_render_chart_rejects_non_plot(tmp_path):
    result = _render(tmp_path, None, result_type="number")
    assert not result.ok
    assert "plot" in result.error


def test_render_chart_rejects_code_without_df(tmp_path):
    code = "import matplotlib.pyplot as plt\nplt.bar([1,2],[3,4])\nplt.savefig('x.png')\n"
    result = _render(tmp_path, None, code=code)
    assert not result.ok
    assert "no_df_reference" in result.error or "审计" in result.error


def test_render_chart_rejects_dangerous_code(tmp_path):
    code = ("import matplotlib.pyplot as plt\n"
            "plt.barh(df[\"分组\"], df[\"数值\"])\n"
            "open('x.txt','w').write('x')\n"
            "plt.savefig('x.png')\n")
    result = _render(tmp_path, None, code=code)
    assert not result.ok


def test_render_chart_rejects_outside_path(tmp_path):
    result = _render(tmp_path, None, outside=tmp_path.parent / "outside.png")
    assert not result.ok
    assert "工作目录" in result.error


def test_render_chart_rejects_low_dpi(tmp_path):
    result = _render(tmp_path, None, dpi=(72, 72))
    assert not result.ok
    assert "300" in result.error


def test_make_figures_writes_pandasai_manifest(tmp_path, monkeypatch):
    run = tmp_path / "w99"
    run.mkdir()
    ctx = {"tag": "w99", "week": "2026-W99", "period": "p", "compare": "c",
           "figure_specs": [{"finding_id": "D1", "title": "测试图", "kind": "barh",
                             "unit": "元", "labels": ["A", "B"], "keys": ["k1", "k2"],
                             "points": [{"key": "k1", "value": 2, "unit": "元"},
                                        {"key": "k2", "value": 1, "unit": "元"}],
                             "question": "q"}]}

    def fake_renderer(spec, *, out_png, script_path, work_dir):
        out_png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 600), "white").save(out_png, dpi=(300, 300))
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text("# generated\n", encoding="utf-8")
        return s5_chart.ChartRenderResult(ok=True, quality={"ok": True, "dpi": [300, 300]},
                                          unit_scale=1.0, plot_values=[2.0, 1.0])

    monkeypatch.setattr(s5.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    manifest = s5._make_figures(run, ctx, [], renderer=fake_renderer)
    assert manifest["renderer"] == "pandasai"
    item = manifest["figures"][0]
    assert item["renderer"] == "pandasai"
    assert set(item["files"]) == {"png"}
    assert set(item["output_sha256"]) == {"png"}
    assert item["keys"] == ["k1", "k2"]
    assert (run / "figures" / "fig01.png").is_file()
    assert (run / "figures" / "src" / "fig01.py").is_file()


def test_make_figures_all_fail_returns_empty(tmp_path):
    run = tmp_path / "w99"
    run.mkdir()
    ctx = {"tag": "w99", "week": "2026-W99", "period": "p", "compare": "c",
           "figure_specs": [{"finding_id": "D1", "title": "测试图", "kind": "barh",
                             "unit": "元", "labels": ["A", "B"], "keys": [], "points": [], "question": "q"}]}
    log = []

    def fake_renderer(*args, **kwargs):
        return s5_chart.ChartRenderResult(ok=False, error="fake 失败")

    manifest = s5._make_figures(run, ctx, log, renderer=fake_renderer)
    assert manifest["figures"] == []
    assert any("fake 失败" in line for line in log)
