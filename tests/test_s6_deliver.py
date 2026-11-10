"""S6 交付归档的确定性测试。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from pipeline import s6_deliver as delivery

# 1x1 PNG，docx 里的 add_picture 需要真图片
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_run(root: Path, state: str = "已通过") -> Path:
    _write(root / "intake.json", {
        "state": "准入通过",
        "run": {"period_tag": "w99", "dir": str(root)},
        "minutes": {"week": "2026-W99", "period_start": "2026-10-19",
                    "period_end": "2026-10-25", "title": "2026 年第 99 周经营周报"},
        "files": [{"name": "销售明细.csv", "path": "inputs/销售明细.csv",
                   "size": 1234, "sha256": "a" * 64}],
    })
    draft = (
        "# 2026 年第 99 周经营周报\n\n"
        "## 经营情况\n\n本周实收较上周下降。\n\n"
        "图 1　门店实收（2026-10-19 至 2026-10-25，单位：元）\n\n"
        "## 数据发现\n\n门店差异明显。\n"
    )
    (root / "draft.md").write_text(draft, encoding="utf-8")
    fig_dir = root / "figures"
    (fig_dir / "src").mkdir(parents=True, exist_ok=True)
    (fig_dir / "fig01.png").write_bytes(_PNG)
    (fig_dir / "fig01.svg").write_text("<svg/>", encoding="utf-8")
    (fig_dir / "src" / "fig01.vl.json").write_text("{}", encoding="utf-8")
    _write(root / "figures_manifest.json", {
        "run_id": "w99", "week": "2026-W99",
        "figures": [{
            "number": 1, "name": "fig01", "title": "门店实收",
            "caption": "图 1　门店实收",
            "files": {"png": "figures/fig01.png", "svg": "figures/fig01.svg"},
            "script": "figures/src/fig01.vl.json",
        }],
    })
    _write(root / "ledger.json", {"entries": [
        {"key": "old_key_w99", "display": "1", "value": 1.0, "unit": "元"},
    ]})
    _write(root / "review_record.json", {
        "run_id": "w99", "round": 0, "reviewer": "张三", "signature": "张三",
        "signed_at": "2026-10-25T12:00:00", "decision": "pass",
        "state": "已通过", "comments": [], "at": "2026-10-25T12:00:00",
    })
    _write(root / "s5_facts.json", {
        "run_id": "w99", "week": "2026-W99", "model": "glm-4.5",
        "discoveries": [{"id": "D1", "question": "门店实收", "code_hash": "abcdef1234567890"}],
        "stats": {"chars_body_rendered": 1200},
    })
    _write(root / "status.json", {
        "stage": "s5", "state": state, "updated_by": "S5",
        "at": "2026-10-25T12:00:00", "note": "审核通过", "history": [],
    })
    return root


# ---------------------------------------------------------------- 前置检查

def test_requires_passed_state(tmp_path):
    run = _make_run(tmp_path / "w99", state="已生成")
    out = delivery.deliver(run, use_ai=False)
    assert not out.ok
    assert "已通过" in out.message
    assert not (run / "deliver").exists()
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "已生成"


def test_requires_pass_review_record(tmp_path):
    run = _make_run(tmp_path / "w99")
    record = json.loads((run / "review_record.json").read_text(encoding="utf-8"))
    record["decision"] = "reject"
    _write(run / "review_record.json", record)
    out = delivery.deliver(run, use_ai=False)
    assert not out.ok
    assert "审核记录" in out.message


def test_missing_figure_does_not_create_package(tmp_path):
    run = _make_run(tmp_path / "w99")
    (run / "figures" / "fig01.png").unlink()
    out = delivery.deliver(run, use_ai=False)
    assert not out.ok
    assert "缺图" in out.message
    assert not (run / "deliver").exists()
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "已通过"


# ---------------------------------------------------------------- 归档

def test_deliver_builds_complete_package(tmp_path):
    run = _make_run(tmp_path / "w99")
    out = delivery.deliver(run, use_ai=False)
    assert out.ok, out.message
    assert out.version == "v1"
    assert out.idempotent is False
    pkg = Path(out.path)
    assert pkg.name == "v1"
    for name in ("ledger.json", "figures_manifest.json", "qc_result.json",
                 "review_record.json", "manifest.json"):
        assert (pkg / name).is_file(), name
    assert list(pkg.glob("*.docx")), "缺少 docx"
    assert list(pkg.glob("*.md")), "缺少 md"
    assert (pkg / "figures" / "fig01.png").is_file()
    assert (pkg / "figures" / "fig01.svg").is_file()

    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "已交付"
    assert manifest["content_fingerprint"]
    assert manifest["inputs"][0]["name"] == "销售明细.csv"
    assert len(manifest["process"]) == 1
    assert manifest["process"][0]["question_hash"]
    assert manifest["review"]["reviewer"] == "张三"
    assert manifest["review"]["signature"] == "张三"
    assert manifest["delivery"]["signature"] == "张三"
    assert manifest["qc"]["available"] is False
    assert manifest["hand_filled"] is False
    assert "draft.md=" in manifest["fingerprint_input"]
    assert manifest["files"]

    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "已交付"
    assert status["updated_by"] == "S6"
    assert status["version"] == "v1"


def test_signature_is_recorded(tmp_path):
    run = _make_run(tmp_path / "w99")
    out = delivery.deliver(run, use_ai=False, signature="王五")
    assert out.ok, out.message
    assert out.signature == "王五"
    manifest = json.loads((Path(out.path) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["delivery"]["signature"] == "王五"
    assert manifest["review"]["signature"] == "王五"
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    assert "交付签名 王五" in status["note"]


def test_second_run_is_idempotent(tmp_path):
    run = _make_run(tmp_path / "w99")
    first = delivery.deliver(run, use_ai=False)
    assert first.ok
    second = delivery.deliver(run, use_ai=False)
    assert second.ok
    assert second.idempotent is True
    assert second.version == first.version
    assert sorted(item.name for item in (run / "deliver").iterdir()) == ["v1"]


def test_content_change_creates_next_version(tmp_path):
    run = _make_run(tmp_path / "w99")
    assert delivery.deliver(run, use_ai=False).version == "v1"
    (run / "draft.md").write_text(
        (run / "draft.md").read_text(encoding="utf-8") + "\n补充一句。\n",
        encoding="utf-8")
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    status["state"] = "已通过"
    _write(run / "status.json", status)
    out = delivery.deliver(run, use_ai=False)
    assert out.ok, out.message
    assert out.version == "v2"
    assert sorted(item.name for item in (run / "deliver").iterdir()) == ["v1", "v2"]
    # 旧版本没有被覆盖
    assert (run / "deliver" / "v1" / "manifest.json").is_file()


def test_layout_change_creates_new_version_from_delivered(tmp_path):
    run = _make_run(tmp_path / "w99")
    assert delivery.deliver(run, use_ai=False).version == "v1"
    manifest_path = run / "deliver" / "v1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["layout_version"] = "old-layout"
    _write(manifest_path, manifest)
    out = delivery.deliver(run, use_ai=False)
    assert out.ok, out.message
    assert out.version == "v2"
    assert sorted(item.name for item in (run / "deliver").iterdir()) == ["v1", "v2"]


def test_delivered_state_with_changed_content_is_rejected(tmp_path):
    run = _make_run(tmp_path / "w99")
    assert delivery.deliver(run, use_ai=False).ok
    (run / "draft.md").write_text("改了。\n", encoding="utf-8")
    out = delivery.deliver(run, use_ai=False)
    assert not out.ok
    assert "重新走 S5 审核" in out.message


def test_stale_qc_is_not_treated_as_pass(tmp_path):
    run = _make_run(tmp_path / "w99")
    _write(run / "qc_result.json", {"verdict": "FAIL", "draft_after_sha256": "old"})
    out = delivery.deliver(run, use_ai=False)
    assert out.ok
    manifest = json.loads((Path(out.path) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["qc"]["stale"] is True
    assert manifest["qc"]["available"] is False
    packaged = json.loads((Path(out.path) / "qc_result.json").read_text(encoding="utf-8"))
    assert packaged["available"] is False


def test_read_status_lists_versions(tmp_path):
    run = _make_run(tmp_path / "w99")
    delivery.deliver(run, use_ai=False)
    data = delivery.read_status(run)
    assert data["ok"] is True
    assert data["state"] == "已交付"
    assert [item["version"] for item in data["versions"]] == ["v1"]
    assert data["manifest"]["content_fingerprint"]


# ---------------------------------------------------------------- 主 agent

def test_agent_delivery_tools(tmp_path, monkeypatch):
    import shutil

    from pipeline.agent import TOOLS, Session

    monkeypatch.setenv("S5_AI_SUMMARY", "0")
    names = {tool["function"]["name"] for tool in TOOLS}
    assert "run_s6" in names and "get_delivery" in names
    run = _make_run(tmp_path / "w99")
    # Session 的 run_root 指向临时目录，交付时会读项目里的 tools/config
    session = Session([], run_root=tmp_path)
    session.last_run = "w99"
    out = session._execute("run_s6", {}, "")
    assert out["ok"] is True
    assert out["state"] == "已交付"
    assert out["version"] == "v1"
    got = session._execute("get_delivery", {}, "")
    assert got["ok"] is True
    assert got["versions"][0]["version"] == "v1"
    assert got["manifest"]["content_fingerprint"]


# ---------------------------------------------------------------- 摘要

def test_trim_summary_never_cuts_mid_sentence():
    text = "整体经营回落。" + ("中间说明很长。" * 20) + "风险与下周关注。"
    out = delivery._trim_summary(text, 40)
    assert out.endswith("。")
    assert "风。" not in out
    assert out in text


# ---------------------------------------------------------------- 排版引擎

def test_pandoc_is_default_engine(tmp_path):
    run = _make_run(tmp_path / "w99")
    out = delivery.deliver(run, use_ai=False)
    assert out.ok, out.message
    pkg = Path(out.path)
    layout = json.loads((pkg / "layout.json").read_text(encoding="utf-8"))
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    from pipeline import typeset_docx as td

    if td.available():
        assert layout["engine"] == "pandoc"
        assert manifest["layout_engine"] == "pandoc"
        assert (pkg / "layout.md").is_file()
    else:
        assert layout["engine"] == "python-docx"
    assert manifest["layout_checked"] is True
    assert manifest["layout_source"] in ("ai", "deterministic")


def test_python_docx_fallback_when_pandoc_missing(tmp_path, monkeypatch):
    from pipeline import typeset_docx as td

    monkeypatch.setattr(td, "available", lambda: False)
    run = _make_run(tmp_path / "w99")
    out = delivery.deliver(run, use_ai=False)
    assert out.ok, out.message
    layout = json.loads((Path(out.path) / "layout.json").read_text(encoding="utf-8"))
    assert layout["engine"] == "python-docx"
    from docx import Document

    d = Document(str(next(Path(out.path).glob("*.docx"))))
    assert len(d.inline_shapes) >= 1
