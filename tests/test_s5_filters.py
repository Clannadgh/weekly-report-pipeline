"""S5 口径过滤：不写数据质量，不写核实/是否一致类占位话。"""
from __future__ import annotations

from pipeline.s5_filters import (
    clean_text,
    filter_questions,
    is_quality_finding,
)


def test_clean_hedges_keeps_business_facts():
    text = ("分区域实收金额中华东最高（224,516），西南最低（81,116），"
            "与“其余区域平稳”的表述是否一致，需进一步核实。")
    assert clean_text(text) == "分区域实收金额中华东最高（224,516），西南最低（81,116）。"


def test_clean_internal_shifou_clause():
    text = ("分区域实收金额中华东最高（224,516），西南最低（81,116），"
            "区域间是否与“其余区域平稳”一致，需进一步核实。")
    cleaned = clean_text(text)
    assert "是否" not in cleaned
    assert "核实" not in cleaned
    assert "华东最高（224,516）" in cleaned


def test_clean_reason_placeholder_keeps_facts():
    text = ("销售数量合计以拿铁最高（4,216）、柠檬茶最低（4,168），"
            "原因待核实。")
    assert clean_text(text) == "销售数量合计以拿铁最高（4,216）、柠檬茶最低（4,168）。"


def test_figure_refs_are_removed_from_body():
    text = ("本周各商品销售数量合计居首与垫底的单品见图 2，"
            "其中销售数量合计为 4,216。（见图 2）")
    cleaned = clean_text(text)
    assert "见图" not in cleaned
    assert "4,216" in cleaned
    assert clean_text("销售数量分布见图 1、图 2。") == "销售数量分布。"
    assert clean_text("折扣分布如图 3 所示，拿铁最高。") == "折扣分布，拿铁最高。"


def test_quality_sentences_are_dropped():
    assert clean_text(
        "深圳南山店记录为 202 条，疑似重复上传，需进一步核实。") == ""
    assert clean_text(
        "持续跟踪深圳南山店 POS 重复上传数据的核实与处理结果。") == ""


def test_hedge_only_sentence_is_dropped():
    assert clean_text("促销活动可能带动连带销售。") == ""


def test_filter_questions_drops_quality_and_meta_questions():
    questions = [
        {"id": "D1", "question": "筛选记录行数，用于核实 POS 是否重复上传",
         "reason": "需要核实"},
        {"id": "D2", "question": "按记录ID去重前的行数 count",
         "reason": "量化重复规模"},
        {"id": "D3", "question": "按门店分组实收金额 sum",
         "reason": "验证华南促销"},
        {"id": "D8", "question": "统计销售数量小于 0 的记录数量",
         "reason": "盘点负数记录"},
    ]
    kept, dropped = filter_questions(questions)
    assert [item["id"] for item in kept] == ["D3"]
    assert dropped == ["D1", "D2", "D8"]


def test_clean_writing_drops_quality_and_hedges():
    from pipeline.s5_write import _clean_writing

    cleaned = _clean_writing({
        "supplements": {
            "M01": "本周实收回升，原因待核实。",
            "M02": "深圳南山店记录数 202 条，疑似重复上传。",
        },
        "standalone": ["持续跟踪重复上传数据的核实结果。"],
        "next_week": ["核实并处理南山店重复数据", "关注竞品开业影响并每日上报。"],
    })
    assert cleaned["supplements"] == {"M01": "本周实收回升。"}
    assert cleaned["standalone"] == []
    assert cleaned["next_week"] == ["关注竞品开业影响并每日上报。"]


def test_quality_finding_detects_question_and_reason():
    assert is_quality_finding({
        "question": "按门店分组实收金额",
        "reason": "验证是否存在重复上传",
    })
    assert not is_quality_finding({
        "question": "按门店分组实收金额",
        "reason": "验证促销拉动",
    })
