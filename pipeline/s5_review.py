"""S5 审核：扩写生成后自动送审，审核人在 S5 页面通过或退回。

历史送审与意见归集已并入 S5，本模块只服务一个阶段：
- open_record：S5 扩写成功后由 s5_write 调用，整理材料、写 review_record.json；
- submit：旧稿（state=已生成）补送审，或已在待审核时重建缺失的审核记录；
- decide：只由审核人在 S5 页面调用。通过 → 已通过（审核人签名一并写入）；
  退回 → 退回修改，意见按类别归到 S3 / S5（S3 的数字意见要再经 S4 改稿）；
- 本模块不替审核人判断，不自动放行，也不改正文；签名由审核人在通过时确认，
  S6 交付只读取这份签名，不再单独要求交付签名。

旧的 qc_result.json 只有在 draft_after_sha256 与当前 draft 一致时才采信；
重新生成的稿子，旧质检一律视为失效，不假装 PASS。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATE_READY = "已生成"
STATE_REVIEW = "待审核"
STATE_PASSED = "已通过"
STATE_REJECTED = "退回修改"
RECORD_FILE = "review_record.json"
MAX_ROUNDS = 3

_CATEGORY_BACK = {
    "wording": "S5", "structure": "S5", "length": "S5", "chart": "S5",
    "number": "S3", "caliber": "S3", "omission": "S3",
}


@dataclass
class ReviewOutcome:
    ok: bool
    state: str
    message: str
    run_id: str = ""
    dir: str = ""
    record: dict = field(default_factory=dict)
    materials: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "state": self.state, "message": self.message,
            "run_id": self.run_id, "dir": self.dir,
            "record": self.record, "materials": self.materials,
            "log": self.log,
        }


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(value: Any) -> str:
    return re.sub(r"[\s\u3000\ufeff]+", " ", str(value or "")).strip()


def _first_sentence(text: str, limit: int = 140) -> str:
    text = text.strip()
    match = re.search(r"[。！？!?]", text)
    if match:
        text = text[:match.end()]
    return text[:limit]


def _sections(draft: str) -> tuple[str, list[dict]]:
    """取文档标题与每个二级标题下的首句，不贴全文。"""
    title = ""
    sections: list[dict] = []
    current: dict | None = None
    for line in draft.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if match:
            level = len(match.group(1))
            heading = match.group(2).strip()
            if level == 1 and not title:
                title = heading
                continue
            current = {"heading": heading, "first": ""}
            sections.append(current)
            continue
        if current is not None and not current["first"]:
            current["first"] = _first_sentence(stripped)
    if not sections:
        first = next((line.strip() for line in draft.splitlines() if line.strip()), "")
        sections = [{"heading": title or "正文", "first": _first_sentence(first)}]
    return title, sections


def _unconfirmed(corrected: dict, facts: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in corrected.get("items") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        if status not in ("blocked", "future") and item.get("confirmed") is not False:
            continue
        fid = str(item.get("id") or "")
        if fid:
            seen.add(fid)
        out.append({
            "id": fid,
            "kind": status or "unconfirmed",
            "original": str(item.get("original") or ""),
            "fixed": str(item.get("fixed") or ""),
            "reason": str(item.get("note") or item.get("issue")
                          or ("未来事项，未写入本周" if status == "future" else "未确认")),
            "section": str(item.get("section") or ""),
        })
    for item in facts.get("future") or []:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or "")
        if fid and fid in seen:
            continue
        out.append({
            "id": fid,
            "kind": "future",
            "original": "",
            "fixed": str(item.get("fixed") or ""),
            "reason": "未来事项，未写入本周",
            "section": "",
        })
    return out


def _qc_info(run_dir: Path, draft: str) -> dict:
    qc = _read_json(run_dir / "qc_result.json")
    if not qc:
        return {
            "available": False, "stale": False, "state": "", "verdict": "",
            "summary": "", "note": "本轮没有再跑独立质检（历史质检已并入 S5）",
        }
    try:
        current = hashlib.sha256((run_dir / "draft.md").read_bytes()).hexdigest()
    except OSError:
        current = _sha256_text(draft)
    after = str(qc.get("draft_after_sha256") or qc.get("draft_before_sha256") or "")
    fresh = bool(after) and after == current
    return {
        "available": fresh,
        "stale": not fresh,
        "state": str(qc.get("state") or ""),
        "verdict": str(qc.get("verdict") or ""),
        "summary": str(qc.get("summary") or ""),
        "note": ("质检结果与当前稿一致" if fresh
                 else "旧质检结果对应别的稿子，已失效（原质检已并入 S5）"),
    }


def build_materials(run_dir: str | Path) -> dict:
    folder = Path(run_dir)
    intake = _read_json(folder / "intake.json")
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
    status = _read_json(folder / "status.json")
    facts = _read_json(folder / "s5_facts.json")
    corrected = _read_json(folder / "corrected.json")
    manifest = _read_json(folder / "figures_manifest.json")
    record = load_record(folder)
    draft = ""
    try:
        draft = (folder / "draft.md").read_text(encoding="utf-8")
    except OSError:
        draft = ""
    title, sections = _sections(draft)
    stats = facts.get("stats") if isinstance(facts.get("stats"), dict) else {}
    figures = []
    for item in manifest.get("figures") or []:
        if isinstance(item, dict):
            figures.append({
                "number": item.get("number"),
                "title": str(item.get("title") or ""),
                "caption": str(item.get("caption") or ""),
            })
    history = record.get("history") if isinstance(record.get("history"), list) else []
    previous = None
    if history:
        if (str(record.get("state") or "") in (STATE_PASSED, STATE_REJECTED)
                and str(record.get("decision") or "")):
            # 当前结论已经单独展示，上一轮取更早的一条，避免重复
            previous = history[-2] if len(history) >= 2 else None
        else:
            previous = history[-1]
    generated_at = ""
    status_history = status.get("history") if isinstance(status.get("history"), list) else []
    for event in reversed(status_history):
        if (isinstance(event, dict) and event.get("updated_by") == "S5"
                and event.get("state") in (STATE_REVIEW, STATE_READY)):
            generated_at = str(event.get("at") or "")
            break
    if not generated_at:
        try:
            generated_at = dt.datetime.fromtimestamp(
                (folder / "draft.md").stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            generated_at = str(status.get("at") or "")
    return {
        "run_id": str(run.get("period_tag") or folder.name),
        "dir": str(run.get("dir") or folder.name),
        "week": str(minutes.get("week") or ""),
        "period": (str(minutes.get("period_start") or "") + " 至 "
                   + str(minutes.get("period_end") or "")).strip(" 至"),
        "title": title,
        "sections": sections,
        "stats": {
            "chars": int(stats.get("chars_body_rendered") or 0),
            "figures": len(figures),
            "discoveries": int(stats.get("discoveries") or 0),
            "state": str(status.get("state") or ""),
        },
        "qc": _qc_info(folder, draft),
        "unconfirmed": _unconfirmed(corrected, facts),
        "figures": figures,
        "generated_at": generated_at,
        "version": str(run.get("period_tag") or folder.name),
        "round": int(record.get("round") or 0),
        "paused": bool(record.get("paused")),
        "previous_review": previous,
        "signature": str(record.get("signature") or ""),
        "signed_at": str(record.get("signed_at") or ""),
    }


def load_record(run_dir: str | Path) -> dict:
    data = _read_json(Path(run_dir) / RECORD_FILE)
    return data if isinstance(data, dict) else {}


def _write_status(run_dir: Path, state: str, note: str, **extra: Any) -> None:
    path = run_dir / "status.json"
    data = _read_json(path)
    at = _now()
    history = data.get("history") if isinstance(data.get("history"), list) else []
    history.append({"at": at, "state": state, "updated_by": "S5", "note": note})
    data.update({
        "state": state, "updated_by": "S5", "at": at, "note": note,
        "stage": "s5", "history": history[-50:],
    })
    for key, value in extra.items():
        data[key] = value
    _write_json(path, data)


def open_record(run_dir: str | Path, materials: dict | None = None) -> dict:
    """写本轮送审记录；保留累计退回轮次与历史，清空本轮结论与意见。"""
    folder = Path(run_dir)
    intake = _read_json(folder / "intake.json")
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    run_id = str(run.get("period_tag") or folder.name)
    previous = load_record(folder)
    history = previous.get("history") if isinstance(previous.get("history"), list) else []
    record = {
        "run_id": run_id,
        "round": int(previous.get("round") or 0),
        "reviewer": "",
        "signature": "",
        "signed_at": "",
        "decision": "",
        "state": STATE_REVIEW,
        "comments": [],
        "mapping": [],
        "at": _now(),
        "history": history[-20:],
        "paused": bool(previous.get("paused")),
        "escalated": bool(previous.get("escalated")),
        "materials": materials or build_materials(folder),
    }
    _write_json(folder / RECORD_FILE, record)
    return record


def _targets(comments: list[dict]) -> list[str]:
    found = {str(item.get("back_to") or "") for item in comments}
    ordered = []
    if "S3" in found:
        ordered.append("S3→S4")
    if "S5" in found:
        ordered.append("S5")
    return ordered or ["S5"]


def submit(run_dir: str | Path) -> ReviewOutcome:
    """旧稿补送审：state=已生成 → 待审核；已在待审核时重建材料，不改结论。"""
    folder = Path(run_dir)
    intake = _read_json(folder / "intake.json")
    if not intake:
        raise FileNotFoundError("找不到 intake.json，先跑完 S1：" + str(folder))
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    run_id = str(run.get("period_tag") or folder.name)
    dir_label = str(run.get("dir") or folder.name)
    status = _read_json(folder / "status.json")
    state = str(status.get("state") or "")
    record = load_record(folder)

    if state == STATE_REVIEW:
        materials = build_materials(folder)
        if str(record.get("state") or "") == STATE_REVIEW:
            return ReviewOutcome(True, STATE_REVIEW, "已经在待审核，材料如下。",
                                 run_id=run_id, dir=dir_label, record=record,
                                 materials=materials)
        record = open_record(folder, materials)
        return ReviewOutcome(True, STATE_REVIEW, "已补齐送审记录，仍在待审核。",
                             run_id=run_id, dir=dir_label, record=record,
                             materials=materials)
    elif state != STATE_READY:
        msg = ("S5 只接受已生成（旧稿）或待审核的稿子；当前状态：" + (state or "未知")
               + "。请先扩写，或按退回意见修改。")
        return ReviewOutcome(False, state or "未知", msg, run_id=run_id, dir=dir_label)

    if not (folder / "draft.md").is_file():
        return ReviewOutcome(False, state, "没有 draft.md，先跑 S5。",
                             run_id=run_id, dir=dir_label)
    if not (folder / "figures_manifest.json").is_file():
        return ReviewOutcome(False, state, "没有 figures_manifest.json，先跑 S5。",
                             run_id=run_id, dir=dir_label)
    if not (folder / "corrected.json").is_file():
        return ReviewOutcome(False, state, "没有 corrected.json，先跑 S4。",
                             run_id=run_id, dir=dir_label)
    if not (folder / "ledger.json").is_file():
        return ReviewOutcome(False, state, "没有 ledger.json，先跑 S3。",
                             run_id=run_id, dir=dir_label)

    materials = build_materials(folder)
    qc = materials.get("qc") or {}
    if qc.get("available") and str(qc.get("verdict") or "").upper() not in ("", "PASS"):
        return ReviewOutcome(False, state,
                             "质检结论不是 PASS，不能送审：" + str(qc.get("verdict") or ""),
                             run_id=run_id, dir=dir_label, materials=materials)

    record = open_record(folder, materials)
    round_no = int(record.get("round") or 0)
    paused = bool(record.get("paused"))
    note = "送审：等待签名审核（第 " + str(round_no + 1) + " 轮）"
    if paused:
        note += "；已累计退回 " + str(MAX_ROUNDS) + " 次，需线下沟通"
    _write_status(folder, STATE_REVIEW, note, paused=paused,
                  escalated=bool(record.get("escalated")),
                  reviewer="", round=round_no)
    materials.update({"round": round_no, "paused": paused})
    message = ("已送审：材料已整理，状态改为 " + STATE_REVIEW
               + "。请在 S5 页面签名确认审核或退回修改。")
    if paused:
        message += " 注意：已累计退回 " + str(MAX_ROUNDS) + " 次，需线下沟通。"
    return ReviewOutcome(True, STATE_REVIEW, message, run_id=run_id, dir=dir_label,
                         record=record, materials=materials)


def decide(run_dir: str | Path, reviewer: str, decision: str,
           comments: list[dict] | None = None, signature: str = "") -> ReviewOutcome:
    folder = Path(run_dir)
    intake = _read_json(folder / "intake.json")
    if not intake:
        raise FileNotFoundError("找不到 intake.json：" + str(folder))
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    run_id = str(run.get("period_tag") or folder.name)
    dir_label = str(run.get("dir") or folder.name)
    status = _read_json(folder / "status.json")
    state = str(status.get("state") or "")
    record = load_record(folder)
    if state != STATE_REVIEW or str(record.get("state") or "") != STATE_REVIEW:
        return ReviewOutcome(False, state or "未知",
                             "当前不在待审核：" + (state or "未知") + "。先送审。",
                             run_id=run_id, dir=dir_label, record=record,
                             materials=build_materials(folder))
    reviewer = _clean(reviewer)
    if not reviewer:
        return ReviewOutcome(False, state, "必须填写审核人。",
                             run_id=run_id, dir=dir_label, record=record,
                             materials=build_materials(folder))
    signature = _clean(signature) or reviewer
    decision = _clean(decision).lower()
    if decision not in ("pass", "reject"):
        return ReviewOutcome(False, state, "decision 只能是 pass / reject。",
                             run_id=run_id, dir=dir_label, record=record,
                             materials=build_materials(folder))

    round_no = int(record.get("round") or 0)
    history = record.get("history") if isinstance(record.get("history"), list) else []
    at = _now()

    if decision == "pass":
        history.append({
            "round": round_no, "reviewer": reviewer, "signature": signature,
            "decision": "pass", "state": STATE_PASSED, "comments": [],
            "mapping": [], "at": at, "signed_at": at,
        })
        record = {
            "run_id": run_id, "round": round_no, "reviewer": reviewer,
            "signature": signature, "signed_at": at,
            "decision": "pass", "state": STATE_PASSED, "comments": [],
            "mapping": [], "at": at, "history": history[-20:],
            "paused": False, "escalated": False,
            "materials": record.get("materials") or {},
        }
        _write_json(folder / RECORD_FILE, record)
        note = "审核通过并签名：" + signature
        _write_status(folder, STATE_PASSED, note, paused=False, escalated=False,
                      reviewer=reviewer, signature=signature, signed_at=at,
                      round=round_no)
        return ReviewOutcome(True, STATE_PASSED, note, run_id=run_id, dir=dir_label,
                             record=record, materials=build_materials(folder))

    clean_comments: list[dict] = []
    for index, item in enumerate(comments or []):
        if not isinstance(item, dict):
            continue
        raw = _clean(item.get("raw"))
        if not raw:
            continue
        category = _clean(item.get("category")).lower()
        if category not in _CATEGORY_BACK:
            category = "wording"
        clean_comments.append({
            "raw": raw,
            "category": category,
            "back_to": _CATEGORY_BACK[category],
            "where": _clean(item.get("where")),
            "expect": _clean(item.get("expect")),
        })
    if not clean_comments:
        return ReviewOutcome(False, state, "退回必须至少写一条具体意见。",
                             run_id=run_id, dir=dir_label, record=record,
                             materials=build_materials(folder))

    round_no += 1
    paused = round_no >= MAX_ROUNDS
    mapping = [{
        "comment_index": index,
        "changed": "",
        "section": item.get("where") or "",
        "verified": False,
    } for index, item in enumerate(clean_comments)]
    history.append({
        "round": round_no, "reviewer": reviewer, "decision": "reject",
        "state": STATE_REJECTED, "comments": clean_comments,
        "mapping": mapping, "at": at, "paused": paused,
    })
    record = {
        "run_id": run_id, "round": round_no, "reviewer": reviewer,
        "signature": "", "signed_at": "",
        "decision": "reject", "state": STATE_REJECTED,
        "comments": clean_comments, "mapping": mapping, "at": at,
        "history": history[-20:], "paused": paused,
        "escalated": bool(record.get("escalated")) or paused,
        "materials": record.get("materials") or {},
    }
    _write_json(folder / RECORD_FILE, record)
    targets = _targets(clean_comments)
    note = ("审核退回：意见 " + "、".join(str(i + 1) for i in range(len(clean_comments)))
            + "；回 " + "、".join(targets))
    if paused:
        note += "；已累计退回 " + str(MAX_ROUNDS) + " 次，需线下沟通，暂停流程"
    _write_status(folder, STATE_REJECTED, note, paused=paused,
                  escalated=bool(record.get("escalated")),
                  reviewer=reviewer, round=round_no)
    message = note
    return ReviewOutcome(True, STATE_REJECTED, message, run_id=run_id, dir=dir_label,
                         record=record, materials=build_materials(folder),
                         log=[note])


def read_status(run_dir: str | Path) -> dict:
    folder = Path(run_dir)
    intake = _read_json(folder / "intake.json")
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    status = _read_json(folder / "status.json")
    record = load_record(folder)
    return {
        "ok": True,
        "run_id": str(run.get("period_tag") or folder.name),
        "state": str(status.get("state") or ""),
        "note": str(status.get("note") or ""),
        "at": str(status.get("at") or ""),
        "round": int(record.get("round") or 0),
        "reviewer": str(record.get("reviewer") or ""),
        "signature": str(record.get("signature") or ""),
        "signed_at": str(record.get("signed_at") or ""),
        "decision": str(record.get("decision") or ""),
        "comments": record.get("comments") or [],
        "mapping": record.get("mapping") or [],
        "paused": bool(record.get("paused") or status.get("paused")),
        "escalated": bool(record.get("escalated") or status.get("escalated")),
        "record_state": str(record.get("state") or ""),
        "has_record": bool(record),
    }
