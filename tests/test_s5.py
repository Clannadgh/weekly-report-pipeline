"""S5 扩写的确定性测试（重写版）。

全部 monkeypatch s5_discover.discover 与 chat，不真调 GLM / pandasai、不真出图。
验的是新流程的不变量，而不是旧的「格式规制」：
  前置门禁、纪要骨架逐句保留、发现驱动台账、去掉格式规制、
  可追溯占位符、落盘与状态、S5Outcome 字段。
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

import pipeline.s5_write as s5
from pipeline.s5_write import S5Outcome, run_s5

_PLACEHOLDER_RE = re.compile(r"\[\[f:([^\]]+)\]\]")


# ---------------------------------------------------------------- 夹具

def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_items():
    return [
        {"id": "M01", "status": "kept", "original": "本周全司实收回升。",
         "fixed": "本周全司实收较上周回升。"},
        {"id": "M02", "status": "kept", "original": "华南增长领先。",
         "fixed": "华南增长领先，华东、华北回落。"},
    ]


def _make_run(root: Path, items=None, state="可扩写", updated_by="S4", note="") -> None:
    """最小但字段齐全的运行目录：两周数据、四区域、六单品、含退货与重复。"""
    inputs = root / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    header = ("记录ID,销售日期,门店编码,门店名称,区域,品类,商品名称,"
              "销售数量,实收金额,成本金额,目标金额,渠道\n")
    products = [
        ("拿铁", "饮品"), ("生椰拿铁", "饮品"), ("美式咖啡", "饮品"),
        ("6寸生日蛋糕", "蛋糕"), ("8寸生日蛋糕", "蛋糕"), ("原味吐司", "面包"),
        ("瑞士卷", "西点"), ("月饼礼盒", "礼盒"),
    ]
    stores = [("北京朝阳店", "华北"), ("上海静安店", "华东"),
              ("广州海珠店", "华南"), ("成都高新店", "西南")]
    rows = []
    idx = 0
    for week, dates in ((0, ["2026-10-12", "2026-10-13", "2026-10-14"]),
                        (1, ["2026-10-19", "2026-10-20", "2026-10-21"])):
        for date in dates:
            for si, (store, region) in enumerate(stores):
                for pi, (product, category) in enumerate(products[:6]):
                    idx += 1
                    base = 1000 + week * 300 + si * 50 + pi * 10
                    amt = base if (si + pi) % 7 else -base  # 少量退货负数
                    rows.append(f"R{idx:04d},{date},S{si},{store},{region},{category},"
                                f"{product},3,{amt},{int(base * 0.4)},{base * 2},堂食\n")
    rows.append(rows[0])  # 一条重复记录（按记录ID去重）
    (inputs / "sales.csv").write_text(header + "".join(rows), encoding="utf-8")

    intake = {
        "state": "准入通过",
        "run": {"period_tag": "w99", "dir": str(root)},
        "minutes": {"week": "2026-W99", "period_start": "2026-10-19",
                    "period_end": "2026-10-25", "compare_start": "2026-10-12",
                    "compare_end": "2026-10-18"},
        "files": [{"name": "sales.csv", "path": "inputs/sales.csv", "role": "数据"}],
    }
    _write(root / "intake.json", intake)
    _write(root / "cards.json", {"state": "ok", "cards": [{"id": "M01", "quote": "x"}],
                                 "actions": [{"item": "复核区域回落", "owner": "李某", "due": "10/30"}],
                                 "future": []})
    _write(root / "issues.json", {"state": "ok", "stats": {"blocking": 0}, "issues": []})
    _write(root / "ledger.json", {"week": "2026-W99", "entries": [
        {"key": "old_key_w99", "display": "1", "unit": "元", "value": 1.0,
         "spec": "S3 遗留", "code_hash": "old1234", "shape": "number", "status": "ok"}]})
    _write(root / "corrected.json", {"week": "2026-W99", "stats": {},
                                     "items": items if items is not None else _default_items(),
                                     "decisions": [], "cards": [], "future": []})
    _write(root / "status.json", {"stage": "s4", "state": state, "updated_by": updated_by,
                                  "at": "2026-10-26T00:00:00", "note": note, "history": []})


def _findings():
    """两条 number 型发现：key 稳定、无 table 型，出图会自然得到 0 张图。"""
    return [
        {"id": "D1", "question": "本期全司实收金额合计是多少？", "shape": "number",
         "value": 123456.0, "rows": None, "code": "df_sales['实收金额'].sum()",
         "code_hash": "aaaa1111bbbb2222",
         "spec": "问句：本期全司实收金额合计；口径：sum(实收金额)",
         "display": "123,456.0", "keys": ["find_D1_0_rev"], "reason": "金额", "flags": []},
        {"id": "D2", "question": "本期毛利率是多少？", "shape": "number",
         "value": 0.42, "rows": None, "code": "df_sales['实收金额'].sum()",
         "code_hash": "cccc3333dddd4444",
         "spec": "问句：本期毛利率；口径：毛利/实收",
         "display": "42.0%", "keys": ["find_D2_0_pct"], "reason": "结构", "flags": []},
    ]


# ---------------------------------------------------------------- 桩

def _ledger_keys(run_dir) -> list[str]:
    try:
        doc = json.loads((Path(run_dir) / "ledger.json").read_text(encoding="utf-8"))
        return [str(e.get("key")) for e in doc.get("entries", []) if e.get("key")]
    except (OSError, ValueError):
        return []


def _patch_bound(monkeypatch, modules, original, replacement) -> None:
    """把所有指向 original 的模块级绑定都换成 replacement。

    兼容 import 模块 / from ... import 名 / 函数内 import 三种写法：源模块的
    属性被替换后，函数内 `from .llm import chat` 也会取到桩。
    """
    for mod in modules:
        for name, value in list(vars(mod).items()):
            if name.startswith("__") or value is not original:
                continue
            monkeypatch.setattr(mod, name, replacement, raising=False)


def _stub_s5(monkeypatch, run_dir, findings=None, item_ids=None, note=None) -> None:
    """替换 discover / chat / 出图：不碰 GLM、pandasai、matplotlib。"""
    llm = importlib.import_module("pipeline.llm")
    try:
        disc_mod = importlib.import_module("pipeline.s5_discover")
    except Exception:  # 新模块尚未就绪时不致命，由上层决定是否降级
        disc_mod = None

    findings = json.loads(json.dumps(findings or [], ensure_ascii=False))
    item_ids = list(item_ids or [])
    note = note or "数据发现：该指标值得关注"
    discover_doc = {"findings": findings, "log": ["stub discover"], "model": "stub"}

    def _discover(*args, **kwargs):
        return json.loads(json.dumps(discover_doc, ensure_ascii=False))

    def _chat(*args, **kwargs):
        # 占位符只用在落盘台账里真实存在的键，保证 step 8b 可追溯校验通过
        disk = _ledger_keys(run_dir)
        preferred = [f["keys"][0] for f in findings if f.get("keys")]
        valid = [k for k in preferred if k in disk]
        s5keys = [k for k in disk if k.startswith("find_")]
        key = (valid or s5keys or disk or ["old_key_w99"])[0]
        sentence = note + "（[[f:" + key + "]]，数据发现，非纪要结论）。"
        supp = {}
        for i, iid in enumerate(item_ids):
            supp[str(iid)] = sentence          # 按 item.id 取
            supp[str(i)] = sentence            # 按序号取
            supp["M%02d" % (i + 1)] = sentence
        body = {"supplements": supp, "standalone": [sentence], "next_week": []}
        return {"text": json.dumps(body, ensure_ascii=False), "reasoning": "",
                "message": {}, "tool_calls": [], "model": "stub",
                "usage": {}, "finish_reason": "stop", "elapsed_ms": 0}

    def _figures(*args, **kwargs):
        return {"run_id": "w99", "week": "2026-W99", "period": "", "compare": "",
                "figures": []}

    discover_orig = getattr(disc_mod, "discover", None) if disc_mod else None
    chat_orig = getattr(llm, "chat", None)
    modules = [s5, llm] + ([disc_mod] if disc_mod else [])
    if discover_orig is not None:
        _patch_bound(monkeypatch, modules, discover_orig, _discover)
    if chat_orig is not None:
        _patch_bound(monkeypatch, modules, chat_orig, _chat)
    monkeypatch.setattr(s5, "_make_figures", _figures, raising=False)


# ---------------------------------------------------------------- 用例

# a) 前置门禁：状态非「可扩写 / 已生成 / 待审核 / 退回修改」时拒绝，不写 draft
@pytest.mark.parametrize("bad_state", ["待补充", "进行中", "已完成", "未知状态"])
def test_gate_rejects_non_expandable_state(tmp_path, monkeypatch, bad_state):
    _make_run(tmp_path, state=bad_state)
    _stub_s5(monkeypatch, tmp_path)
    outcome = run_s5(tmp_path)
    assert not outcome.ok
    assert not (tmp_path / "draft.md").exists()


# b) corrected 有 blocked：blocked 原句不得作为骨架/扩写内容进正文，
#    也不再生成「待核实」段落；kept 骨架照常保留
def test_blocked_corrected_is_not_expanded_or_rejected(tmp_path, monkeypatch):
    items = [
        {"id": "M01", "status": "kept", "original": "a",
         "fixed": "KEPT_ONLY_S5：本周实收回升。"},
        {"id": "M02", "status": "blocked", "original": "b",
         "fixed": "BLOCKED_MARKER_S5：华东回落的具体原因（不得扩写）。"},
    ]
    _make_run(tmp_path, items=items)
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])
    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    draft = (tmp_path / "draft.md").read_text(encoding="utf-8")
    assert "KEPT_ONLY_S5" in draft
    assert "BLOCKED_MARKER_S5" not in draft


# b2) 模型写出的数据质量/待核实句子在装配前被过滤
def test_generated_quality_and_hedges_are_filtered(tmp_path, monkeypatch):
    _make_run(tmp_path)
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])
    body = {
        "supplements": {
            "M01": "本周实收回升，见图 2，原因待核实。",
            "M02": "深圳南山店记录数 202 条，疑似重复上传。",
        },
        "standalone": ["持续跟踪重复上传数据的核实结果。"],
        "next_week": ["核实并处理南山店重复数据", "关注竞品开业影响。"],
    }
    monkeypatch.setattr(
        s5, "_call_llm", lambda prompt: json.dumps(body, ensure_ascii=False))
    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    draft = (tmp_path / "draft.md").read_text(encoding="utf-8")
    assert "202 条" not in draft
    assert "持续跟踪重复上传数据的核实结果" not in draft
    assert "见图" not in draft
    assert "关注竞品开业影响" in draft
    assert "## 数据质量与口径说明" in draft


# c) 核心红线：每个非 future/blocked 的 items[].fixed 逐字出现在 draft；
#    future 原句不进本周报告正文（个别实现若单列到末尾「未来清单」，也不得与骨架混排）
def test_skeleton_fixed_is_preserved_verbatim(tmp_path, monkeypatch):
    kept = [
        {"id": "M01", "status": "kept", "original": "x",
         "fixed": "KEPT_A_S5：本周全司实收较上周回升。"},
        {"id": "M02", "status": "kept", "original": "y",
         "fixed": "KEPT_B_S5：华南增长领先，华东、华北回落。"},
    ]
    future = {"id": "M03", "status": "future", "original": "z",
              "fixed": "FUTURE_C_S5：下月上线会员体系。"}
    blocked = {"id": "M04", "status": "blocked", "original": "w",
               "fixed": "BLOCKED_D_S5：华东回落的具体原因。"}
    _make_run(tmp_path, items=kept + [future, blocked])
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02", "M03", "M04"])

    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message

    draft = (tmp_path / "draft.md").read_text(encoding="utf-8")
    for item in kept:
        assert item["fixed"] in draft, item["id"]
    kept_pos = max(draft.find(k["fixed"]) for k in kept)
    fpos = draft.find(future["fixed"])
    assert fpos == -1 or fpos > kept_pos


# d) 发现驱动：2 条 finding 的数值进 ledger；正文出现 [[f:key]]；
#    且每个占位符键都能在 ledger 找到（可追溯底线，不是规制）
def test_discoveries_drive_ledger_and_placeholders(tmp_path, monkeypatch):
    _make_run(tmp_path)
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])

    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message

    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    keys = {e["key"] for e in ledger["entries"]}
    assert "old_key_w99" in keys                       # 原有条目保留
    assert len(keys) >= 2                              # 发现转成了新台账条目
    new = [e for e in ledger["entries"] if e["key"] != "old_key_w99"]
    assert new and all("spec" in e and "code_hash" in e for e in new)
    assert any(isinstance(e.get("value"), (int, float)) for e in new)

    draft = (tmp_path / "draft.md").read_text(encoding="utf-8")
    ph = _PLACEHOLDER_RE.findall(draft)
    assert not ph                                      # S5 收尾已把占位符替换成 display
    displays = [str(e.get("display") or "") for e in new if e.get("display")]
    assert any(display in draft for display in displays)  # 新发现的 display 进了正文


# e) 无规制：超长、带「大约/基本/显著/大幅/有所/据不完全统计」等禁用词的骨架句
#    仍原样保留，不被裁剪、不替换禁用词
def test_no_format_regulation_on_skeleton(tmp_path, monkeypatch):
    long_fixed = ("本季度全司实收金额大约基本显著大幅有所提升，"
                  "据不完全统计左右应该是众所周知。" + "并且延续回升态势，" * 120
                  + "以上为超长骨架句，用于验证不做字数裁剪与禁用词替换。")
    items = [{"id": "M01", "status": "kept", "original": long_fixed,
              "fixed": long_fixed}]
    _make_run(tmp_path, items=items)
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01"])

    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    draft = (tmp_path / "draft.md").read_text(encoding="utf-8")
    assert long_fixed in draft
    for word in ("大约", "基本", "显著", "大幅", "有所", "据不完全统计", "左右", "应该是"):
        assert word in draft


# e2) 失败直说、不兜底：关闭模型 / 发现为空 / 补肉失败，都明确报「没有生成成功」且不落盘
def test_no_ai_reports_failure_without_draft(tmp_path, monkeypatch):
    _make_run(tmp_path)
    _stub_s5(monkeypatch, tmp_path, findings=_findings())
    outcome = run_s5(tmp_path, use_ai=False)
    assert not outcome.ok
    assert "没有生成成功" in outcome.message
    assert not (tmp_path / "draft.md").exists()


def test_empty_discoveries_reports_failure_without_draft(tmp_path, monkeypatch):
    _make_run(tmp_path)
    _stub_s5(monkeypatch, tmp_path, findings=[])  # pandasai 无任何可用发现
    outcome = run_s5(tmp_path)
    assert not outcome.ok
    assert "没有生成成功" in outcome.message
    assert "发现" in outcome.message
    assert not (tmp_path / "draft.md").exists()


# f) 落盘三件 + s5_facts.json + review_record.json；占位符已替换，
#    扩写完成自动送审，status.state=待审核、updated_by=S5
def test_outputs_and_status(tmp_path, monkeypatch):
    _make_run(tmp_path)
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])

    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    for name in ("draft.md", "figures_manifest.json", "profile.json",
                 "s5_facts.json", "review_record.json", "qc_result.json"):
        assert (tmp_path / name).is_file(), name
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "待审核"
    assert status["updated_by"] == "S5"
    record = json.loads((tmp_path / "review_record.json").read_text(encoding="utf-8"))
    assert record["state"] == "待审核"
    assert record["round"] == 0
    assert record["decision"] == ""
    qc = json.loads((tmp_path / "qc_result.json").read_text(encoding="utf-8"))
    assert qc["verdict"] == "PASS"
    assert qc["draft_after_sha256"]
    draft = (tmp_path / "draft.md").read_text(encoding="utf-8")
    assert "[[f:" not in draft
    history = status.get("history") or []
    assert isinstance(history, list) and any(h.get("updated_by") == "S5" for h in history)


# g0) 占位符形态规范化：模型漏写 f: 前缀要补成 [[f:key]]，未知键要抹掉
def test_placeholder_normalization():
    normalize = s5._sanitize_placeholders
    keys = {"find_D1_0_rev", "find_D2_0_amt"}
    assert normalize("本周实收 [[f:find_D1_0_rev]] 元", keys) == "本周实收 [[f:find_D1_0_rev]] 元"
    assert normalize("本周实收 [[find_D1_0_rev]] 元", keys) == "本周实收 [[f:find_D1_0_rev]] 元"
    assert normalize("未知 [[find_NOPE]] 键", keys) == "未知  键"
    assert normalize("混合 [[f:find_D2_0_amt]] 与 [[find_D1_0_rev]]", keys) == \
        "混合 [[f:find_D2_0_amt]] 与 [[f:find_D1_0_rev]]"


# g) S5Outcome.to_dict 含 discoveries 与 skeleton 字段
def test_outcome_has_discoveries_and_skeleton(tmp_path, monkeypatch):
    _make_run(tmp_path)
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])

    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    assert isinstance(outcome, S5Outcome)
    data = outcome.to_dict()
    assert "discoveries" in data and "skeleton" in data
    assert isinstance(data["discoveries"], list)
    assert isinstance(data["skeleton"], list)


# h) 标题遇到并列值要说清「等 N 个」，不能只报第一个
def test_conclusion_title_handles_ties():
    title = s5._conclusion_title(["A店", "B店", "C店", "D店"], [3, 3, 1, 1], "bar", "记录数")
    assert "A店 等 2 个最高" in title
    assert "C店 等 2 个最低" in title
    assert "各分组持平" in s5._conclusion_title(["A", "B"], [2, 2], "bar", "记录数")


# j) 编码→名称只改展示
def test_label_mapping():
    mapping = {"SZ01": "深圳南山店", "BJ01": "北京朝阳店"}
    assert s5._map_label("SZ01", mapping) == "深圳南山店"
    assert s5._map_label("未知", mapping) == "未知"
    assert s5._map_label("", mapping) == ""


# k) 两类构成画 100% 占比条，不画两根大柱子
def test_two_categories_use_share_chart():
    finding = {"question": "按渠道分组汇总实收金额", "reason": "渠道结构", "spec": ""}
    assert s5._chart_kind(finding, ["堂食自提", "外卖"], [414882, 191027]) == "share"
    title = s5._conclusion_title(["堂食自提", "外卖"], [414882, 191027], "share", "实收金额合计")
    assert "68.5%" in title and "31.5%" in title


# l) 记录条数、几乎持平的排名不配图
def test_low_signal_rankings_are_not_charted():
    quality = {"question": "按门店编码分组统计记录条数", "reason": "数据完整性", "spec": ""}
    assert not s5._worth_chart(quality, [380, 378, 376])
    flat = {"question": "按门店分组实收金额", "reason": "", "spec": ""}
    assert not s5._worth_chart(flat, [100, 99, 98])
    assert s5._worth_chart(flat, [100, 60, 30])
    assert not s5._worth_share([99, 1])
    assert s5._worth_share([70, 30])


# m) 有可画发现但 pandasai 一张图都没成功：S5 明确失败，不落 draft
def test_all_figure_fail_fails_without_draft(tmp_path, monkeypatch):
    _make_run(tmp_path)
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])
    spec = {"finding_id": "D1", "title": "测试图", "kind": "barh", "unit": "元",
            "labels": ["A", "B"], "keys": [], "points": [], "question": "q"}
    monkeypatch.setattr(s5, "_prepare_findings", lambda *a, **k: ([], {}, [spec]))
    outcome = s5.run_s5(tmp_path)
    assert not outcome.ok
    assert "没有生成成功" in outcome.message
    assert not (tmp_path / "draft.md").exists()


# n) 重跑前归档上一版：第二次 S5 必须在 history/<时间>/ 留下旧稿
def test_rerun_archives_previous_version(tmp_path, monkeypatch):
    _make_run(tmp_path)
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])
    first = run_s5(tmp_path)
    assert first.ok, first.message
    second = run_s5(tmp_path)
    assert second.ok, second.message
    history = list((tmp_path / "history").iterdir())
    assert len(history) == 1
    archived = history[0]
    assert (archived / "draft.md").is_file()
    assert (archived / "figures_manifest.json").is_file()
    assert (archived / "meta.json").is_file()

# o) 有 s5_questions.json 时，run_s5 把清单原样交给 discover，不再重新出题
def test_run_s5_passes_saved_questions(tmp_path, monkeypatch):
    import pipeline.s5_discover as disc_mod
    from pipeline import s5_questions as sq

    _make_run(tmp_path)
    sq.save_plan(tmp_path, [
        {"id": "D1", "question": "全公司实收合计", "shape": "number", "reason": "看总量"},
        {"id": "D2", "question": "毛利率", "shape": "number", "reason": "看结构"},
    ], source="user", run_id="w99")
    seen = {}

    def fake_discover(*args, **kwargs):
        seen["questions"] = kwargs.get("questions")
        return {"findings": _findings(), "log": ["stub discover"], "model": "stub",
                "questions": kwargs.get("questions") or [], "source": "plan"}

    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])
    monkeypatch.setattr(disc_mod, "discover", fake_discover, raising=True)
    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    assert [q["id"] for q in seen["questions"]] == ["D1", "D2"]
    assert outcome.questions_source == "plan"
    facts = json.loads((tmp_path / "s5_facts.json").read_text(encoding="utf-8"))
    assert facts["questions_hash"] == sq.questions_hash(seen["questions"])
    assert (tmp_path / "s5_questions.json").is_file()

def _plan_with_ai_keep(run: Path, questions: list[dict]) -> None:
    """保存清单并预置 AI 过滤结果：全部保留，后续 S5 直接命中缓存。"""
    from pipeline import s5_questions as sq

    sq.save_plan(run, questions, source="user", run_id="w99")
    sq.filter_for_run(
        run, questions,
        chat_fn=lambda *args, **kwargs: {"text": json.dumps({
            "keep": [item["id"] for item in questions], "drop": []},
            ensure_ascii=False)})


# o2) AI 已保留的问题（即使理由含「核实 / 是否」）不得被词表二次过滤
def test_run_s5_trusts_ai_question_filter(tmp_path, monkeypatch):
    import pipeline.s5_discover as disc_mod

    _make_run(tmp_path)
    q1 = {"id": "D1", "question": "按区域分组汇总实收金额",
          "shape": "number", "reason": "核实华南拉动是否所有门店共享"}
    q2 = {"id": "D2", "question": "本期全司实收金额合计",
          "shape": "number", "reason": "金额"}
    _plan_with_ai_keep(tmp_path, [q1, q2])

    f1 = dict(_findings()[0])
    f1.update({"id": "D1", "question": q1["question"], "reason": q1["reason"],
               "keys": ["find_D1_0_rev"]})
    f2 = dict(_findings()[1])
    f2.update({"id": "D2", "question": q2["question"], "reason": q2["reason"]})
    seen = {}

    def fake_discover(*args, **kwargs):
        seen["keep_quality"] = kwargs.get("keep_quality")
        return {"findings": [f1, f2], "log": ["stub discover"], "model": "stub",
                "questions": kwargs.get("questions") or [], "source": "plan"}

    _stub_s5(monkeypatch, tmp_path, findings=[f1, f2], item_ids=["M01", "M02"])
    monkeypatch.setattr(disc_mod, "discover", fake_discover, raising=True)
    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    assert seen["keep_quality"] is True
    facts = json.loads((tmp_path / "s5_facts.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in facts["discoveries"]] == ["D1", "D2"]
    assert facts["filtered_discoveries"]["findings"] == []


# o3) 清单里没取到结果的问题要在状态提示里点名，不能只报「发现 N 条」
def test_run_s5_reports_questions_without_results(tmp_path, monkeypatch):
    import pipeline.s5_discover as disc_mod

    _make_run(tmp_path)
    questions = [
        {"id": "D1", "question": "本期全司实收金额合计", "shape": "number",
         "reason": "金额"},
        {"id": "D2", "question": "本期毛利率", "shape": "number", "reason": "结构"},
        {"id": "D3", "question": "按渠道分组求实收金额", "shape": "number",
         "reason": "渠道结构"},
    ]
    _plan_with_ai_keep(tmp_path, questions)
    findings = _findings()

    def fake_discover(*args, **kwargs):
        if not kwargs.get("keep_quality"):
            return {"findings": [], "log": [], "model": "stub",
                    "questions": [], "source": "plan"}
        return {"findings": findings, "log": ["stub discover"], "model": "stub",
                "questions": kwargs.get("questions") or [], "source": "plan"}

    _stub_s5(monkeypatch, tmp_path, findings=findings, item_ids=["M01", "M02"])
    monkeypatch.setattr(disc_mod, "discover", fake_discover, raising=True)
    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    assert "数据发现 2 条（D1、D2）" in outcome.message
    assert "问题取数未出结果 1 条：D3" in outcome.message
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert "问题取数未出结果 1 条：D3" in status["note"]


# p) 没有清单时由模型出题，成功后把题目保存下来，供下次编辑 / 复用
def test_run_s5_saves_auto_questions(tmp_path, monkeypatch):
    import pipeline.s5_discover as disc_mod
    from pipeline import s5_questions as sq

    _make_run(tmp_path)
    questions = [
        {"id": "D1", "question": "全公司实收合计", "shape": "number", "reason": "看总量"},
        {"id": "D2", "question": "按门店分组求实收", "shape": "table", "reason": "看门店差异"},
    ]

    def fake_discover(*args, **kwargs):
        return {"findings": _findings(), "log": ["stub discover"], "model": "stub",
                "questions": questions, "source": "auto"}

    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])
    monkeypatch.setattr(disc_mod, "discover", fake_discover, raising=True)
    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    doc = json.loads((tmp_path / "s5_questions.json").read_text(encoding="utf-8"))
    assert doc["source"] == "auto"
    assert [q["id"] for q in doc["questions"]] == ["D1", "D2"]
    facts = json.loads((tmp_path / "s5_facts.json").read_text(encoding="utf-8"))
    assert facts["questions_hash"] == sq.questions_hash(questions)

# q) 已生成过、只改部分问题时只重算改动项，其余复用上一稿 findings
def test_run_s5_incremental_reuses_unchanged_findings(tmp_path, monkeypatch):
    import pipeline.s5_discover as disc_mod
    from pipeline import s5_questions as sq

    _make_run(tmp_path)
    q1 = {"id": "D1", "question": "全公司实收合计", "shape": "number", "reason": "看总量"}
    q2 = {"id": "D2", "question": "毛利率", "shape": "number", "reason": "看结构"}
    calls = []

    def first_discover(*args, **kwargs):
        calls.append(kwargs.get("questions"))
        return {"findings": _findings(), "log": ["stub discover"], "model": "stub",
                "questions": [q1, q2], "source": "auto"}

    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])
    monkeypatch.setattr(disc_mod, "discover", first_discover, raising=True)
    first = run_s5(tmp_path)
    assert first.ok, first.message
    assert (tmp_path / "s5_questions.json").is_file()

    q3 = {"id": "D3", "question": "渠道实收合计", "shape": "number", "reason": "看渠道"}
    sq.save_plan(tmp_path, [q1, q3], source="user", run_id="w99")

    f3 = dict(_findings()[1])
    f3.update({"id": "D3", "question": q3["question"], "reason": q3["reason"],
               "value": 50.0, "display": "50.0",
               "keys": ["find_D3_0_rev"], "code_hash": "eeee5555ffff6666"})

    def second_discover(*args, **kwargs):
        calls.append(kwargs.get("questions"))
        return {"findings": [f3], "log": ["stub incremental"], "model": "stub",
                "questions": kwargs.get("questions") or [], "source": "plan"}

    monkeypatch.setattr(disc_mod, "discover", second_discover, raising=True)
    second = run_s5(tmp_path)
    assert second.ok, second.message
    assert second.incremental is True
    assert second.recomputed == 1
    assert second.reused == 1
    assert second.removed == 1
    user_calls = [call for call in calls
                  if call and str(call[0].get("id") or "").startswith("D")]
    assert [item["id"] for item in user_calls[-1]] == ["D3"]
    assert "增量" in second.message

    facts = json.loads((tmp_path / "s5_facts.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in facts["questions"]] == ["D1", "D3"]
    assert facts["questions_source"] == "incremental"
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    keys = {entry["key"] for entry in ledger["entries"]}
    assert "find_D1_0_rev" in keys
    assert "find_D3_0_rev" in keys
    assert not any("find_D2" in key for key in keys)


# h) AI 复核可以调用 pandasai；工具结果进 queries，最终 JSON 决定 ok
def test_ai_review_can_call_pandasai(monkeypatch):
    import pipeline.s5_write as s5
    from pipeline.s5_write import _ai_review_draft

    calls = []

    def fake_query(run_dir, frame, intake, profile, question, shape, log):
        calls.append({"question": question, "shape": shape})
        return {"ok": True, "value": 29, "display": "29", "rows": None}

    monkeypatch.setattr(s5, "_pandasai_review_query", fake_query)
    replies = [
        {"message": {"content": ""}, "tool_calls": [
            {"id": "c1", "function": {
                "name": "ask_pandasai",
                "arguments": json.dumps({"question": "本周重复记录多少条",
                                         "shape": "number"}, ensure_ascii=False)}}]},
        {"text": json.dumps({"ok": True, "issues": [], "suggestions": []},
                            ensure_ascii=False), "tool_calls": []},
    ]

    def fake_chat(*args, **kwargs):
        return replies.pop(0)

    review = _ai_review_draft("正文", {"summary": "本周实收 1 元"},
                              {"checks": []}, {"week": "2026-W99"},
                              Path("."), None, {}, {}, [],
                              chat_fn=fake_chat)
    assert review["ok"] is True
    assert calls == [{"question": "本周重复记录多少条", "shape": "number"}]
    assert review["queries"][0]["result"]["value"] == 29


def _qc_text() -> str:
    return ("## 本周摘要\n摘要\n## 品类表现\n品类\n## 异常与原因\n异常\n"
            "## 数据质量与口径说明\n质量\n本周实收前 5 单品：拿铁 1 元\n")


# i) 没有销售目标数据时，质检查摘要不得硬要「目标达成率」
def test_qc_final_skips_target_when_data_absent():
    business = {"summary": "本周全公司实收 1 元，较上周 +1.0%，毛利率 50.0%。",
                "quality": ["重复记录 1 条"]}
    qc = s5._qc_final(_qc_text(), business, {"figures": []}, has_target=False)
    item = next(check for check in qc["checks"] if check["name"] == "摘要指标")
    assert item["ok"] is True
    assert "无目标数据" in item["detail"]
    assert qc["verdict"] == "PASS"


# i2) 有目标数据时，摘要仍必须写目标达成率
def test_qc_final_requires_target_when_data_present():
    business = {"summary": "本周全公司实收 1 元，较上周 +1.0%，毛利率 50.0%。",
                "quality": ["重复记录 1 条"]}
    qc = s5._qc_final(_qc_text(), business, {"figures": []}, has_target=True)
    item = next(check for check in qc["checks"] if check["name"] == "摘要指标")
    assert item["ok"] is False
    assert "目标达成率" in item["detail"]


# j) AI 出数必须与代码参考值一致才采用
def test_numbers_close_rejects_mismatch():
    from pipeline.s5_write import _numbers_close

    ref = {"total": 100.0, "prev_total": 90.0, "cost": 40.0,
           "total_target": 120.0,
           "regions": [{"name": "华东", "amount": 100.0,
                        "previous": 90.0, "target": 120.0}],
           "categories": [], "products": [], "stores": []}
    same = json.loads(json.dumps(ref))
    assert _numbers_close(same, ref) is True
    same["total"] = 101.0
    assert _numbers_close(same, ref) is True
    same["total"] = 102.0
    assert _numbers_close(same, ref) is False


# k) S1 人工豁免要进入 S5 的 AI 复核提示
def test_s1_waivers_reach_ai_review(tmp_path, monkeypatch):
    _make_run(tmp_path)
    intake = json.loads((tmp_path / "intake.json").read_text(encoding="utf-8"))
    intake["waived"] = [{
        "at": "2026-11-11T09:03:51", "by": "用户",
        "items": ["纪要提到的渠道「商场店、街边店、社区店、写字楼店」在数据列「渠道」里不存在"],
        "reason": "渠道只是概述",
    }]
    (tmp_path / "intake.json").write_text(
        json.dumps(intake, ensure_ascii=False), encoding="utf-8")
    _stub_s5(monkeypatch, tmp_path, findings=_findings(), item_ids=["M01", "M02"])
    captured = {}

    def fake_review(*args, **kwargs):
        captured["waivers"] = kwargs.get("waivers") or []
        return {"ok": True, "issues": [], "suggestions": [], "queries": []}

    monkeypatch.setattr(s5, "_ai_review_draft", fake_review)
    outcome = run_s5(tmp_path)
    assert outcome.ok, outcome.message
    assert any("S1 人工豁免" in line and "商场店" in line
               for line in captured["waivers"])
