"""S6 AI 排版层与 Pandoc 工具的确定性测试。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from pipeline import s6_layout as sl
from pipeline import typeset_docx as td

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _sections() -> list[dict]:
    return [
        {"heading": "纪要骨架与数据补充", "blocks": [
            {"type": "p", "text": "本周销售回落。"},
            {"type": "p", "text": "渠道与区域分化。（见图 4、图 7）"},
            {"type": "p", "text": "门店实收区间约 28,099 至 37,928 元。（见图 1）"},
        ]},
        {"heading": "下周关注", "blocks": [
            {"type": "bullet", "text": "跟踪数据补录。"},
        ]},
        {"heading": "未来事项", "blocks": [
            {"type": "bullet", "text": "确认停业时间。"},
        ]},
    ]


def _figures() -> list[dict]:
    return [
        {"number": 1, "caption": "图 1　门店实收（2026-10-12 至 2026-10-18）",
         "path": "figures/fig01.png", "shape": "barh"},
        {"number": 4, "caption": "图 4　渠道占比（2026-10-12 至 2026-10-18）",
         "path": "figures/fig04.png", "shape": "share"},
        {"number": 7, "caption": "图 7　区域实收（2026-10-12 至 2026-10-18）",
         "path": "figures/fig07.png", "shape": "bar"},
    ]


def _meta() -> dict:
    return {"title": "2026 年第 42 周经营周报", "subtitle": "统计周期 2026-10-12 至 2026-10-18",
            "period": "2026-10-12 至 2026-10-18", "company": "青禾烘焙",
            "reviewer": "张三", "version": "v1", "date": "2026-11-09"}


def _table() -> dict:
    return {
        "caption": "表 1　核心指标（2026-10-12 至 2026-10-18）",
        "headers": ["区域", "实收（元）", "较上周", "毛利率"],
        "rows": [["全公司", "605,909", "-11.7%", "61.0%"],
                 ["华东", "230,947", "-15.8%", "61.0%"]],
        "note": "较上周与 2026-10-05 至 2026-10-11 比较。",
    }


# ---------------------------------------------------------------- 选图与计划

def test_deterministic_plan_selects_diverse_figures():
    sections, figures = _sections(), _figures()
    plan = sl.deterministic_plan(sections, figures, {"纪要骨架与数据补充": "本周经营情况"})
    numbers = [item["number"] for item in plan["figures"]]
    assert numbers == [1, 4, 7]
    assert sl.validate_plan(sections, figures, plan) == []
    assert plan["headings"]["纪要骨架与数据补充"] == "本周经营情况"


def test_deterministic_plan_limits_three_figures():
    sections = _sections()
    figures = _figures() + [
        {"number": 2, "caption": "图 2　门店实收（2026-10-12 至 2026-10-18）",
         "path": "figures/fig02.png", "shape": "barh"},
        {"number": 6, "caption": "图 6　品类实收（2026-10-12 至 2026-10-18）",
         "path": "figures/fig06.png", "shape": "bar"},
    ]
    plan = sl.deterministic_plan(sections, figures)
    assert len(plan["figures"]) == 3


def test_validate_plan_rejects_bad_figure_count():
    sections, figures = _sections(), _figures()
    plan = sl.deterministic_plan(sections, figures)
    plan["figures"] = plan["figures"][:1]
    errors = sl.validate_plan(sections, figures, plan)
    assert any("选图数量" in item for item in errors)


def test_validate_plan_rejects_out_of_range_subheading():
    sections, figures = _sections(), _figures()
    plan = sl.deterministic_plan(sections, figures)
    plan["subheadings"] = [{"after": 99, "text": "越界"}]
    errors = sl.validate_plan(sections, figures, plan)
    assert any("越界" in item for item in errors)


def test_validate_plan_accepts_numbered_page_break():
    sections, figures = _sections(), _figures()
    plan = sl.deterministic_plan(sections, figures)
    plan["page_break_before"] = ["二、下周关注", "三、未来事项"]
    assert sl.validate_plan(sections, figures, plan) == []


def test_validate_plan_rejects_unknown_heading():
    sections, figures = _sections(), _figures()
    plan = sl.deterministic_plan(sections, figures)
    plan["headings"]["不存在的小节"] = "xx"
    errors = sl.validate_plan(sections, figures, plan)
    assert any("多出小节" in item for item in errors)


# ---------------------------------------------------------------- 渲染

def test_render_markdown_keeps_paragraphs_and_captions():
    sections, figures, meta = _sections(), _figures(), _meta()
    plan = sl.deterministic_plan(sections, figures)
    md = sl.render_markdown(sections, figures, meta, plan,
                            summary="本周销售回落，渠道与区域分化。",
                            tables=[_table()])
    assert sl.validate_markdown(md, sections, figures) == []
    for fig in figures:
        assert md.count("![" + fig["caption"] + "]") == 1
    assert "# 一、" in md
    assert "# 二、" in md
    assert "# 摘要" in md
    assert "| 区域 |" in md
    assert "目　录" not in md and "附录" not in md
    assert "- 跟踪数据补录。" in md
    # 封面信息是独立段落，不再挤在一行
    assert md.count('custom-style="Cover Meta"') >= 4
    assert md.count('custom-style="Cover Kicker"') == 1


def test_render_markdown_strips_ai_numbering():
    sections, figures, meta = _sections(), _figures(), _meta()
    plan = sl.deterministic_plan(sections, figures)
    plan["headings"]["下周关注"] = "二、下周工作重点"
    md = sl.render_markdown(sections, figures, meta, plan)
    assert "# 二、下周工作重点" in md
    assert "# 二、二、" not in md


def test_validate_markdown_rejects_forbidden_marker():
    sections, figures, meta = _sections(), _figures(), _meta()
    plan = sl.deterministic_plan(sections, figures)
    md = sl.render_markdown(sections, figures, meta, plan)
    bad = md + "\n【数据发现】不该出现。"
    errors = sl.validate_markdown(bad, sections, figures)
    assert any("过程标记" in item for item in errors)


def test_plan_layout_with_stub_ai():
    sections, figures, meta = _sections(), _figures(), _meta()
    log: list[str] = []
    ai = {
        "headings": {"纪要骨架与数据补充": "本周经营综述", "下周关注": "下周关注事项",
                     "未来事项": "未来事项"},
        "figures": [{"number": 4, "width": "wide"}, {"number": 7, "width": "wide"}],
        "subheadings": [{"after": 2, "text": "渠道与区域"}],
        "page_break_before": ["下周关注事项"],
    }
    payload = json.dumps(ai, ensure_ascii=False)

    def chat_fn(*args, **kwargs):
        return {"text": payload}

    result = sl.plan_layout(sections, figures, meta, use_ai=True,
                            chat_fn=chat_fn, log=log,
                            summary="本周销售回落。", tables=[_table()])
    assert result["source"] == "ai"
    assert result["errors"] == []
    assert [item["number"] for item in result["plan"]["figures"]] == [4, 7]
    assert result["pages_estimate"] > 0
    md = result["markdown"]
    assert "## 渠道与区域" in md
    assert md.index("![图 4") < md.index("![图 7")
    assert "见图 1" not in md
    assert "见图 图" not in md
    cleaned = sl.clean_sections(sections, {4, 7})
    assert sl.validate_markdown(md, cleaned, figures,
                                result["plan"]["figures"]) == []


def test_plan_layout_invalid_ai_falls_back():
    sections, figures, meta = _sections(), _figures(), _meta()
    log: list[str] = []

    def chat_fn(*args, **kwargs):
        return {"text": json.dumps({"figures": [{"number": 99, "width": "wide"}]},
                                   ensure_ascii=False)}

    result = sl.plan_layout(sections, figures, meta, use_ai=True,
                            chat_fn=chat_fn, log=log)
    assert result["source"] == "deterministic"
    assert result["errors"] == []
    assert any("不合法" in item for item in log)


def test_page_estimate_falls_back_to_two_figures(tmp_path):
    from PIL import Image

    sections = [{"heading": "经营情况", "blocks": [
        {"type": "p", "text": "本周销售回落。" * 500},
    ]}]
    figures = []
    for number in (1, 4, 7):
        path = tmp_path / ("fig%02d.png" % number)
        Image.new("RGB", (1200, 900), "white").save(path)
        figures.append({"number": number, "caption": "图 %d　说明" % number,
                        "path": str(path), "source_path": str(path), "shape": "bar"})
    ai = {"figures": [{"number": 1, "width": "wide"},
                      {"number": 4, "width": "wide"},
                      {"number": 7, "width": "wide"}]}
    log: list[str] = []

    def chat_fn(*args, **kwargs):
        return {"text": json.dumps(ai, ensure_ascii=False)}

    result = sl.plan_layout(sections, figures, _meta(), use_ai=True,
                            chat_fn=chat_fn, log=log)
    assert len(result["plan"]["figures"]) == 2
    assert any("退回 2 图" in item for item in log)


# ---------------------------------------------------------------- Pandoc

def test_reference_template_exists():
    assert td.default_reference().is_file()


@pytest.mark.skipif(not td.available(), reason="本机没有 pandoc")
def test_typeset_produces_docx_with_image_and_caption(tmp_path):
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "fig01.png").write_bytes(_PNG)
    md = tmp_path / "layout.md"
    md.write_text(
        "::: {custom-style=\"Cover Title\"}\n测试周报\n:::\n\n"
        "# 一、本周经营情况\n\n本周销售回落。\n\n"
        "![图 1　门店实收](figures/fig01.png){width=13cm}\n",
        encoding="utf-8")
    out = tmp_path / "out.docx"
    info = td.typeset(md, out)
    assert info["engine"] == "pandoc"
    assert info["images"] == 1
    from docx import Document

    doc = Document(str(out))
    styles = {p.style.name for p in doc.paragraphs}
    assert "Cover Title" in styles
    assert "Image Caption" in styles
    assert any("图 1" in p.text for p in doc.paragraphs)
