"""工具调用必须实时抛给前端：tool_start -> tool_end，而不是等整轮跑完。"""
from __future__ import annotations

import json

from pipeline.agent import Session


def test_tool_start_and_end_are_streamed():
    session = Session([])
    rounds = [
        {"message": {"content": "先重算 M09。"}, "tool_calls": [
            {"id": "call_1", "function": {
                "name": "recalc_card",
                "arguments": json.dumps(
                    {"card_id": "M09", "hint": "只统计周一至周五"},
                    ensure_ascii=False),
            }}]},
        {"message": {"content": "已按新口径重算 M09。"}, "tool_calls": []},
    ]

    def fake_stream(on_event):
        out = rounds.pop(0)
        if out.get("tool_calls"):
            on_event({"type": "content", "delta": "先重算 M09。"})
        return out

    session._stream_round = fake_stream
    session._execute = lambda name, args, text: {
        "ok": True, "run_id": "w42", "state": "已对账",
    }

    seen: list[dict] = []
    result = session.send("排除周六周日，重算 M09", on_event=seen.append)

    types = [event.get("type") for event in seen]
    assert types == ["content", "content_reset", "tool_start", "tool_end"]

    started = seen[2]
    assert started["tool"] == "recalc_card"
    assert started["args"] == {"card_id": "M09", "hint": "只统计周一至周五"}

    finished = seen[3]
    assert finished["tool"] == "recalc_card"
    assert finished["call_id"] == "call_1"
    assert finished["ok"] is True
    assert finished["run_id"] == "w42"
    assert finished["elapsed_ms"] >= 0

    assert result["events"] == [
        {"tool": "recalc_card", "run_id": "w42", "state": "已对账", "ok": True},
    ]


def test_fixable_s5_failure_asks_user_without_retry():
    session = Session([])
    rounds = [
        {"message": {"content": ""}, "tool_calls": [
            {"id": "call_1", "function": {"name": "run_s5", "arguments": "{}"}},
        ]},
        {"message": {"content": "需要你定口径。"}, "tool_calls": [
            {"id": "call_2", "function": {"name": "run_s5", "arguments": "{}"}},
            {"id": "call_3", "function": {"name": "fix_s5", "arguments": "{}"}},
        ]},
        {"message": {"content": "请选择：全周 -12.7% 还是工作日 -14.0%？"},
         "tool_calls": []},
    ]

    def fake_stream(on_event):
        return rounds.pop(0)

    session._stream_round = fake_stream
    executed: list[str] = []

    def fake_execute(name, args, text):
        executed.append(name)
        return {"ok": False, "state": "可扩写", "fixable": True,
                "message": "自动质检未通过：AI复核", "run_id": "w44", "qc": {}}

    session._execute = fake_execute
    seen: list[dict] = []
    result = session.send("继续", on_event=seen.append)

    assert executed == ["run_s5"]
    assert result["reply"] == "请选择：全周 -12.7% 还是工作日 -14.0%？"
    assert [event["type"] for event in seen] == [
        "content_reset", "tool_start", "tool_end", "content_reset",
    ]


def test_failed_stage_stops_turn_without_retry():
    session = Session([])
    rounds = [
        {"message": {"content": ""}, "tool_calls": [
            {"id": "call_1", "function": {"name": "run_s5", "arguments": "{}"}},
            {"id": "call_2", "function": {"name": "run_s5", "arguments": "{}"}},
        ]},
        {"message": {"content": "再试一次"}, "tool_calls": [
            {"id": "call_3", "function": {"name": "run_s5", "arguments": "{}"}},
        ]},
        {"message": {"content": "不应该走到这里"}, "tool_calls": []},
    ]

    def fake_stream(on_event):
        return rounds.pop(0)

    session._stream_round = fake_stream
    executed: list[str] = []

    def fake_execute(name, args, text):
        executed.append(name)
        return {"ok": False, "state": "可扩写",
                "message": "没有生成成功：自动质检未通过。", "run_id": "w44"}

    session._execute = fake_execute
    seen: list[dict] = []
    result = session.send("重新扩写", on_event=seen.append)

    assert executed == ["run_s5"]
    assert [event["type"] for event in seen] == [
        "content_reset", "tool_start", "tool_end",
    ]
    assert result["reply"] == (
        "没有生成成功：自动质检未通过。\n\n已停止，不会自动重试。确认要重跑时告诉我。"
    )
    skipped = session.messages[-1]
    assert skipped["role"] == "tool" and skipped["tool_call_id"] == "call_2"
    assert "未执行" in skipped["content"]


def test_fix_s5_is_routed_to_tool():
    session = Session([])
    seen: dict = {}

    def fake_fix(args):
        seen.update(args)
        return {"ok": True, "state": "待审核"}

    session._fix_s5 = fake_fix
    result = session._execute(
        "fix_s5",
        {"edits": [{"find": "旧数字", "replace": "新数字"}],
         "waivers": ["骨架原句逐字保留"]},
        "",
    )

    assert result == {"ok": True, "state": "待审核"}
    assert seen["edits"] == [{"find": "旧数字", "replace": "新数字"}]
    assert seen["waivers"] == ["骨架原句逐字保留"]
