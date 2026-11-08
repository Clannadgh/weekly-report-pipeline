#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S1 准入检查的夹具驱动测试。

两种跑法都行：
    pytest tests/test_s1.py -q
    python tests/test_s1.py

测试不污染项目 runs/：每次在 tempfile 临时根目录里建 inputs/ 与 runs/，
并把 cwd 切到临时根目录，run.dir 的 runs/w44 落在临时目录内。

pipeline.s1_intake 由另一路并行实现，本文件只在运行时导入它；
入口函数名与参数名用下面的映射适配，找不到就报清晰的错。
如实现用了别的名字，可用环境变量指定：S1_ENTRY=pipeline.s1_intake:check
"""
from __future__ import annotations

import csv
import importlib
import inspect
import os

os.environ.setdefault("S1_USE_AI", "0")  # 测试只验代码路径，不依赖网络
os.environ.setdefault("S1_ALLOW_RULES", "1")  # 仅测试显式允许规则回退
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROMPT = (
    "请生成本周经营周报，统计周期 2026-10-26 至 2026-11-01，"
    "覆盖华东、华南两个区域，生鲜、日化、酒饮三个品类，"
    "核心指标为实收、销售数量、毛利率、目标达成率。"
)

MODULE_CANDIDATES = ("pipeline.s1_intake", "pipeline.s1")
ENTRY_NAMES = ("run_s1", "run_intake", "check_intake", "check", "intake", "run", "main")

INPUT_KEYS = {
    "inputs", "input_dir", "inputs_dir", "input_path", "src_dir",
    "source_dir", "files_dir", "input", "input_root",
}
RUN_KEYS = {
    "run_root", "runs_root", "runs_dir", "root", "project_root",
    "output_root", "out_root", "out_dir", "base_dir", "workdir",
}
PROMPT_KEYS = {"prompt", "prompt_text", "instruction", "task_prompt", "request", "brief"}
FILES_KEYS = {"files", "paths", "input_files", "sources", "file_list"}

_FIXTURES: Path | None = None


# ---------------------------------------------------------------- 夹具准备

def fixtures_dir() -> Path:
    """优先用仓库里已生成的 fixtures/w44，缺失则即时构建到临时目录。"""
    global _FIXTURES
    if _FIXTURES is not None:
        return _FIXTURES

    built = ROOT / "fixtures" / "w44"
    needed = ("sales.csv", "minutes.md", "minutes_second.md", "broken.csv")
    if all((built / n).is_file() for n in needed):
        _FIXTURES = built
        return _FIXTURES

    sys.path.insert(0, str(ROOT / "scripts"))
    import make_fixture  # noqa: WPS433  仅在缺夹具时导入

    tmp = Path(tempfile.mkdtemp(prefix="s1-fixtures-"))
    make_fixture.build(tmp)
    _FIXTURES = tmp
    return _FIXTURES


# ---------------------------------------------------------------- 调用适配

def _load_module():
    forced = os.environ.get("S1_ENTRY", "").strip()
    if forced:
        mod_name = forced.split(":", 1)[0]
        return importlib.import_module(mod_name)
    errors = []
    for name in MODULE_CANDIDATES:
        try:
            return importlib.import_module(name)
        except ImportError as exc:
            errors.append("%s: %s" % (name, exc))
    raise ModuleNotFoundError(
        "找不到 S1 实现（pipeline.s1_intake 可能还没写完）：" + "；".join(errors)
    )


def _entry(module):
    forced = os.environ.get("S1_ENTRY", "").strip()
    if forced and ":" in forced:
        func_name = forced.split(":", 1)[1]
        fn = getattr(module, func_name, None)
        if fn is None:
            raise AttributeError("%s 里没有 %s" % (module.__name__, func_name))
        return fn
    for name in ENTRY_NAMES:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    raise AttributeError(
        "%s 里找不到入口函数，候选：%s" % (module.__name__, ", ".join(ENTRY_NAMES))
    )


def _normalize(result, run_root: Path) -> dict:
    if isinstance(result, (tuple, list)) and result:
        return _normalize(result[0], run_root)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    if isinstance(result, (str, Path)):
        target = Path(result)
        if target.is_dir():
            target = target / "intake.json"
        return json.loads(target.read_text(encoding="utf-8"))
    if result is None:
        found = sorted(Path(run_root).glob("*/intake.json"))
        if len(found) == 1:
            return json.loads(found[0].read_text(encoding="utf-8"))
        raise AssertionError(
            "入口函数返回 None，且在 %s 下找到 %d 个 intake.json" % (run_root, len(found))
        )
    raise TypeError("入口函数返回了不支持的类型：%r" % type(result))


def _invoke(fn, inputs: Path, runs: Path, prompt: str) -> dict:
    sig = inspect.signature(fn)
    kwargs: dict[str, object] = {}
    unknown: list[str] = []
    for pname, param in sig.parameters.items():
        low = pname.lower()
        if param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
            continue
        if low in INPUT_KEYS:
            kwargs[pname] = str(inputs)
        elif low in RUN_KEYS:
            kwargs[pname] = str(runs)
        elif low in PROMPT_KEYS:
            kwargs[pname] = prompt
        elif low in FILES_KEYS:
            kwargs[pname] = sorted(str(p) for p in inputs.iterdir())
        elif param.default is not inspect.Parameter.empty:
            continue
        else:
            unknown.append(pname)
    if unknown:
        raise TypeError(
            "无法为入口函数 %s 的参数 %s 提供取值；请在 tests/test_s1.py 的映射里补上"
            % (fn.__name__, unknown)
        )
    return _normalize(fn(**kwargs), runs)


# ---------------------------------------------------------------- 临时工作区

class Workspace:
    """一次测试的输入目录 + 运行目录，cwd 一并切过去。"""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="s1-run-")
        self.root = Path(self._tmp.name)
        self.inputs = self.root / "inputs"
        self.runs = self.root / "runs"
        self.inputs.mkdir()
        self.runs.mkdir()
        self._cwd: Path | None = None

    def __enter__(self) -> "Workspace":
        self._cwd = Path.cwd()
        os.chdir(self.root)
        return self

    def __exit__(self, *exc) -> bool:
        if self._cwd is not None:
            os.chdir(self._cwd)
        self._tmp.cleanup()
        return False

    def add(self, name: str, source: Path | None = None,
            text: str | None = None, data: bytes | None = None) -> Path:
        dst = self.inputs / name
        if source is not None:
            shutil.copyfile(source, dst)
        elif text is not None:
            dst.write_text(text, encoding="utf-8", newline="")
        elif data is not None:
            dst.write_bytes(data)
        else:
            raise ValueError("add() 需要 source / text / data 之一")
        return dst

    def add_fixture(self, name: str) -> Path:
        return self.add(name, source=fixtures_dir() / name)

    def run(self, prompt: str = PROMPT) -> dict:
        return _invoke(_entry(_load_module()), self.inputs, self.runs, prompt)

    def run_dirs(self) -> list[Path]:
        return [p for p in self.runs.iterdir() if p.is_dir()]


def _by_name(out: dict, name: str) -> dict:
    for f in out.get("files") or []:
        if f.get("name") == name:
            return f
    raise AssertionError("files 数组里没有 %s，实际有：%s"
                         % (name, [f.get("name") for f in out.get("files") or []]))


def _reqs_matching(out: dict, *keywords: str) -> list[dict]:
    hits = []
    for req in out.get("requirements") or []:
        blob = str(req.get("item", "")) + " " + " ".join(req.get("needs") or [])
        if any(k in blob for k in keywords):
            hits.append(req)
    return hits


def _drop_column(src: Path, dst: Path, column: str) -> None:
    with src.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = [c for c in reader.fieldnames or [] if c != column]
        rows = list(reader)
    with dst.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fields})


DATELESS_MINUTES = """# 经营分析会纪要

- 会议形式：线上会议
- 主持人：周宁
- 参会：区域运营、品类采购、数据组

## 结论

1. 华南区域本周实收下滑，生鲜品类占比最高。
2. 华东区域本周实收回升，日化品类承压。

## 口径要求

核心指标表须覆盖实收、销售数量、毛利率、目标达成率四项。

## 行动项

- 数据组补齐缺失的销售明细。
"""


# ---------------------------------------------------------------- 1 正常输入

def test_01_normal_input_passes():
    with Workspace() as ws:
        ws.add_fixture("sales.csv")
        ws.add_fixture("minutes.md")
        out = ws.run()

        assert out["state"] == "准入通过", "正常输入应准入通过，实际 %s：%s" % (
            out["state"], out.get("message"))
        assert out["run"]["period_tag"] == "w44", "周期应为 w44，实际 %s" % out["run"]["period_tag"]
        assert Path(out["run"]["dir"]).name == "w44", "run.dir 应指向 w44，实际 %s" % out["run"]["dir"]
        assert out["counts"]["data"] == 1, out["counts"]
        assert out["counts"]["minutes"] == 1, out["counts"]
        assert out["counts"]["prompt"] == 1, out["counts"]

        roles = [f["role"] for f in out["files"]]
        assert roles.count("纪要") == 1, "纪要应恰好 1 条，实际 %s" % roles
        assert roles.count("数据") >= 1, "数据文件应至少 1 条，实际 %s" % roles
        for f in out["files"]:
            assert f["readable"] is True, "%s 应可读" % f["name"]
            assert f["sha256"], "%s 缺少 sha256" % f["name"]
            assert len(f["sha256"]) == 64, "%s 的 sha256 长度不对" % f["name"]
            assert f["size"] > 0, "%s 的 size 应为正" % f["name"]

        sales = _by_name(out, "sales.csv")
        assert sales["format"].lower() == "csv", sales["format"]
        with (fixtures_dir() / "sales.csv").open(encoding="utf-8", newline="") as fh:
            truth_rows = sum(1 for _ in csv.DictReader(fh))
        assert sales["rows"] == truth_rows, (
            "S1 报的 rows 应与 CSV 实际数据行数一致：报 %s，实为 %s" % (sales["rows"], truth_rows))
        drange = sales["date_range"]
        assert "2026-10-19" in drange and "2026-11-01" in drange, (
            "date_range 应覆盖 2026-10-19 至 2026-11-01，实际 %s" % drange)
        for col in ("记录ID", "销售日期", "门店编码", "品类", "销售数量",
                    "实收金额", "成本金额", "目标金额"):
            assert col in sales["columns"], "columns 缺 %s：%s" % (col, sales["columns"])

        minutes = _by_name(out, "minutes.md")
        assert minutes["role"] == "纪要", minutes["role"]
        assert out["minutes"]["period_start"] == "2026-10-26", out["minutes"]
        assert out["minutes"]["period_end"] == "2026-11-01", out["minutes"]
        assert out["minutes"]["actions"] >= 1, "行动项应至少 1 条：%s" % out["minutes"]

        for kw in ("实收", "数量", "毛利", "目标"):
            hits = _reqs_matching(out, kw)
            assert hits, "requirements 没有覆盖核心指标「%s」：%s" % (
                kw, [r["item"] for r in out["requirements"]])
            for req in hits:
                assert req["status"] in ("have", "degrade"), (
                    "正常输入下「%s」不应是 %s：%s" % (req["item"], req["status"], req.get("note")))
                assert req["needs"], "「%s」的 needs 为空" % req["item"]
                assert req["kind"] in ("核心", "补充"), req["kind"]

        assert out["coverage"]["covers_prompt_scope"] is True, out["coverage"]
        assert out["consistency"]["conflicts"] == [], out["consistency"]["conflicts"]
        assert out["questions"] == [], "准入通过时 questions 必须为空：%s" % out["questions"]
        assert out["next_action"] == "continue", out["next_action"]

        landed = sorted(ws.runs.rglob("intake.json"))
        assert landed, "应把 intake.json 落盘到 run.dir"
        assert landed[0].parent.name == "w44", "落盘目录应为 w44：%s" % landed[0]


# ---------------------------------------------------------------- 2 两个纪要

def test_02_two_minutes_pauses():
    with Workspace() as ws:
        ws.add_fixture("sales.csv")
        ws.add_fixture("minutes.md")
        ws.add_fixture("minutes_second.md")
        out = ws.run()

        assert out["state"] == "暂停", "两份纪要应暂停，实际 %s" % out["state"]
        assert out["next_action"] == "wait", out["next_action"]
        assert out["counts"]["minutes"] == 2, out["counts"]
        assert out["questions"], "两份纪要必须给出 questions 要求只保留一份"
        names = [f["name"] for f in out["files"]]
        assert "minutes_second.md" in names, "第二份纪要也要出现在 files 里：%s" % names
        assert _by_name(out, "minutes_second.md")["role"] == "纪要"
        assert out["run"]["dir"] == "", "暂停时不应落盘到运行目录：%s" % out["run"]


# ---------------------------------------------------------------- 3 无数据文件

def test_03_no_data_file_pauses():
    with Workspace() as ws:
        ws.add_fixture("minutes.md")
        out = ws.run()

        assert out["state"] == "暂停", "没有数据文件应暂停，实际 %s" % out["state"]
        assert out["next_action"] == "wait", out["next_action"]
        assert out["counts"]["data"] == 0, out["counts"]
        assert out["counts"]["minutes"] == 1, out["counts"]
        assert out["questions"], "缺数据文件必须写 questions"
        hav = [r["item"] for r in out.get("requirements") or [] if r["status"] == "have"]
        assert not hav, "没有数据文件时不应有任何 have 的需求：%s" % hav


# ---------------------------------------------------------------- 4 不可读文件

def test_04_unreadable_file_pauses():
    with Workspace() as ws:
        ws.add_fixture("sales.csv")
        ws.add_fixture("minutes.md")
        ws.add_fixture("broken.csv")
        out = ws.run()

        assert out["state"] == "暂停", "存在不可读文件应暂停，实际 %s" % out["state"]
        assert out["next_action"] == "wait", out["next_action"]
        broken = _by_name(out, "broken.csv")
        assert broken["readable"] is False, "broken.csv 应标 readable=False，实际 %s" % broken
        assert broken["sha256"], "不可读文件也要给 sha256（按字节取）"
        assert out["questions"], "不可读文件必须写 questions 指出是哪个文件"
        assert any("broken" in q for q in out["questions"]), "questions 应点名 broken.csv：%s" % out["questions"]


# ---------------------------------------------------------------- 5 缺成本列

def test_05_missing_cost_column_blocks():
    with Workspace() as ws:
        _drop_column(fixtures_dir() / "sales.csv", ws.inputs / "sales.csv", "成本金额")
        ws.add_fixture("minutes.md")
        out = ws.run()

        hits = _reqs_matching(out, "毛利")
        assert hits, "缺成本列时仍要列出毛利率需求：%s" % [r["item"] for r in out["requirements"]]
        assert any(r["status"] == "missing" for r in hits), (
            "毛利率应判 missing，实际 %s" % [r["status"] for r in hits])
        missing = [r for r in hits if r["status"] == "missing"][0]
        assert missing["kind"] == "核心", "纪要写明的毛利率是核心指标，实际 %s" % missing["kind"]
        assert "成本" in (missing["note"] + missing["evidence"] + missing["impact"]), (
            "missing 的说明要点出缺「成本金额」：%s" % missing)
        assert out["state"] == "待补充", "核心需求 missing 应待补充，实际 %s" % out["state"]
        assert out["next_action"] == "wait", out["next_action"]
        assert out["questions"], "待补充必须写 questions"
        assert out["missing"], "missing 数组要逐条记录"

        sales = _by_name(out, "sales.csv")
        assert "成本金额" not in sales["columns"], sales["columns"]
        assert "实收金额" in sales["columns"], sales["columns"]


# ---------------------------------------------------------------- 6 同文件复制两份

def test_06_duplicate_file_by_hash():
    with Workspace() as ws:
        ws.add_fixture("sales.csv")
        ws.add("sales_copy.csv", source=fixtures_dir() / "sales.csv")
        ws.add_fixture("minutes.md")
        out = ws.run()

        dups = out["consistency"]["cross_file_duplicates"]
        assert dups, "同哈希的两份文件必须记进 cross_file_duplicates"
        assert any(d["kind"] == "same_hash" for d in dups), dups
        pair = [d for d in dups if d["kind"] == "same_hash"][0]
        assert len(pair["files"]) == 2, pair["files"]
        assert "sales_copy.csv" in pair["files"], pair["files"]

        original = _by_name(out, "sales.csv")
        copy = _by_name(out, "sales_copy.csv")
        assert copy["sha256"] == original["sha256"], "同哈希才算同一文件"
        assert copy["duplicate_of"] == "sales.csv" or original["duplicate_of"] == "sales_copy.csv", (
            "重复的一方 duplicate_of 要指向另一方：%s / %s"
            % (copy["duplicate_of"], original["duplicate_of"]))
        assert out["counts"]["data"] == 2, out["counts"]


# ---------------------------------------------------------------- 7 周期解析失败

def test_07_dateless_minutes_falls_back_to_prompt_period():
    """纪要没写周期时，用提示词里的周期，但必须在 note 里写明来源。"""
    with Workspace() as ws:
        ws.add_fixture("sales.csv")
        ws.add("minutes.md", text=DATELESS_MINUTES)
        out = ws.run()

        assert out["state"] == "准入通过", "提示词写了周期就不该暂停：%s / %s" % (
            out["state"], out.get("questions"))
        assert out["next_action"] == "continue", out["next_action"]
        assert out["run"]["period_tag"] != "", out["run"]
        assert out["minutes"]["period_start"] != "", out["minutes"]
        assert "提示词" in out["minutes"]["note"], out["minutes"]["note"]


def test_08_no_period_anywhere_creates_no_dir():
    """纪要和提示词都没有周期，才算周期定不出。"""
    with Workspace() as ws:
        ws.add_fixture("sales.csv")
        ws.add("minutes.md", text=DATELESS_MINUTES)
        out = ws.run(prompt="出一份本周经营周报，覆盖全公司。")

        assert out["state"] == "暂停", "周期定不出应暂停，实际 %s" % out["state"]
        assert out["run"]["period_tag"] == "", out["run"]
        assert out["run"]["dir"] == "", out["run"]
        assert ws.run_dirs() == [], "周期定不出时不得建目录"
        assert out["questions"], "周期解析失败必须写 questions"


# ---------------------------------------------------------------- runner

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001  测试运行器要汇总所有失败
            failed += 1
            print("FAIL %s: %s" % (name, exc))
            traceback.print_exc()
        else:
            print("PASS %s" % name)
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
