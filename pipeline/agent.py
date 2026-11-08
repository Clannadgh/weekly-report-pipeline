"""主 agent：对话 + 调工具。

边界：它只负责理解、调工具、转述结果；不计算数字、不改判定、不写状态。
工具返回什么，它就说什么。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from .llm import chat, chat_stream
from .s1_intake import STEPS, run_intake

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUN_ROOT = PROJECT_ROOT / "runs"

SYSTEM = """你是「经营周报助手」，负责和用户对话，并通过工具完成周报流水线。

规则：
1. 所有数字、状态、问题都必须来自工具返回，不得自己编造、估算或改写。
2. 工具返回 state=暂停 或 待补充 时，必须原样转述 message 与 questions，然后停下来等用户回答。
   不得自行选择周期、口径、数据版本，也不得为了推进而替用户做决定。
3. 用户材料有矛盾时（例如提示词写第 42 周、日期却是第 44 周），直接指出矛盾并给出选项。
4. 回答用中文，简洁。需要列清单时用短列表，不要长篇大论。
5. 还没有上传文件时，告诉用户先上传数据文件和纪要。
6. 只描述工具返回了什么，不要声称自己执行了检查或计算。
7. 用户问「S1 返回了什么」时，按三层回答：一句话结论、问题清单、需要看细节时给 run_id。
8. 用户要求出周报或检查材料时，必须调用 run_s1 重新检查本次会话的文件；不得用历史运行的结果代替。
   回答时点明本次查到的文件名与行数，让用户能确认读的是哪一批数据。
9. 用户说「继续」时，按本次会话的进度判断：
   - 还没跑过 S1 → 直接调 run_s1；
   - S1 是暂停或待补充 → 追问缺的那一项（例如「还缺 1 份纪要」），不要列历史运行让用户选；
   - S1 已准入但还没拆卡 → 调 run_s2；
   - 只有用户明确说「看看以前的运行」时，才调用 list_runs。
10. list_runs 只返回本次会话的运行记录。别的会话的 w42/w43/w44 不属于当前上下文，不要主动提，
    也不要把它们当成可选项让用户挑。
11. 用户明确说「不用管」「忽略」「豁免」某条冲突时，调用 waive_conflicts，把用户的原话作为 reason 传进去。
    这是人工裁决，不是工具改判：豁免会写进 waiver.json，S8 归档时能查到是谁、什么时候、为什么放行。
    用户没说之前，绝不自己决定忽略。
12. 豁免后必须重跑一次 S1，以新的 state 为准；不要假设豁免一定会让状态变成准入通过。
13. 出周报的顺序是 S1 → S2。只有本次会话的 S1 返回「准入通过」时才能调 run_s2；
    S1 还是暂停或待补充时，先把问题处理完，不要跳过。
14. S2 只整理不判断。卡片里的「能对数据」才是可核对的结论，「不能」「不确定」只是留档，
    不要把它们当成结论讲给用户。
15. 转述 S2 结果时给三个数：卡片总数、可对数据数、动作项数；再挑可核对的卡片说原文和指标。
    不要自己计算卡片里的数字，也不要替 S2 判断卡片对不对。
16. 一次只推进一个阶段：调完 run_s1 就报告结果并停下，等用户说「继续」再调 run_s2。
    只有用户明确说「一路做完」「不用问我」时，才允许连续调用。
17. 当前流水线只实现到 S2。S3 取数、S4 改稿、S5 写正文都还没有接，
    绝对不要自己写周报正文、章节标题、汇总表格，也不要把卡片原话改写成段落。
    用户要正文时，直接说明「下一步 S3 取数还没接入，现在给不了正文」。
18. 转述卡片时字段值照抄：指标、期望、单位、目标值是什么就说什么，
    不要把「具体数值」译成「七成」，也不要从原话里再加工出别的说法。
19. 顺序是 S1 → S2 → S3。S3 对账要等 S2 拆完卡才能调；S1 没准入、S2 没拆卡时不要跳步。
20. S3 返回 issues 时，先报三个数：阻断、提示、信息；再把阻断级的问题逐条念出来
    （对象、纪要怎么说、数据是多少、建议怎么改）。有阻断时明确说「必须先处理才能往下」，
    不要用「基本一致」「大致吻合」这类话把 mismatch 说轻。
21. S3 的数字只能照抄工具返回的 display / raw，不要自己再算一遍或换算单位。
22. 用户对某张卡片的取数口径提出调整（例如「本周不算周六周日」「剔除停业那几天」）时，
    调 recalc_card：card_id 传卡片编号，hint 把用户的口径要求写成一句能被代码执行的说明
    （例如「只统计销售日期为周一至周五的记录，排除周六和周日」）。
    只重算这一张，其他卡片结果保留。不要自己重算数字，也不要改 ledger.json。
23. 口径说明必须具体到能落成条件（字段 + 判断），不要写「合理一点」「按惯例」这类空话；
    写不出来就问用户要明确的判断条件。
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_s1",
            "description": ("对本次会话已上传的数据文件和纪要执行 S1 准入检查。"
                            "返回 state（准入通过/待补充/暂停）、message、questions、run_id。"
                            "prompt 参数传用户本次的写作要求原文，不要改写。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "用户本次的写作要求原文"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_run",
            "description": "读取某次运行的详情。section 可选 summary/files/requirements/all。",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "section": {"type": "string"},
                },
                "required": ["run_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_s3",
            "description": ("对本次会话已拆卡的运行做 S3 数据对账：拿卡片去数据里取数、判对错，"
                            "产出问题清单与数字台账。必须在 S2 拆卡完成之后调用。"
                            "返回阻断/提示/信息条数、问题明细、覆盖率。"),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_s2",
            "description": ("对本次会话已准入的运行做 S2 纪要拆卡：把纪要拆成一张张卡片。"
                            "必须在 S1 返回准入通过之后才能调用。"
                            "返回卡片总数、可对数据数、可核对卡片的原文与指标、动作项与未来事项。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "force": {"type": "boolean",
                              "description": "已经拆过卡时是否强制重拆，默认 false 表示复用"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recalc_card",
            "description": ("给一张卡片附加口径说明并只重算这一张，其他卡片结果保留。"
                            "用于用户明确提出取数口径调整，例如「不算周末」「剔除停业期」。"
                            "返回重算后的问题清单与统计。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "卡片编号，如 M09"},
                    "hint": {"type": "string",
                             "description": "写成可执行条件的口径说明，如「只统计销售日期为周一至周五的记录」"},
                },
                "required": ["card_id", "hint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "waive_conflicts",
            "description": ("把用户明确要求忽略的冲突登记为人工豁免，并重跑 S1。"
                            "items 传要豁免的冲突关键词（取自 questions 原文），"
                            "reason 传用户的原话。只有用户明确说忽略/不用管/豁免时才能调用。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要豁免的冲突关键词，如「栗子蒙布朗」",
                    },
                    "reason": {"type": "string", "description": "用户给出的理由原话"},
                },
                "required": ["items", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_runs",
            "description": "列出本次会话内产生过的运行（周期、状态、时间），不含其他会话的历史。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _summarize(intake: dict) -> dict:
    """给模型的摘要：够它回答，又不至于塞满上下文。"""
    run = intake.get("run") or {}
    minutes = intake.get("minutes") or {}
    requirements = intake.get("requirements") or []
    return {
        "ok": True,
        "run_id": run.get("period_tag") or "",
        "dir": run.get("dir") or "",
        "state": intake.get("state"),
        "message": intake.get("message"),
        "questions": intake.get("questions") or [],
        "counts": intake.get("counts") or {},
        "period": {
            "week": minutes.get("week"),
            "start": minutes.get("period_start"),
            "end": minutes.get("period_end"),
            "note": minutes.get("note"),
        },
        "files": [{"name": f.get("name"), "role": f.get("role"),
                   "readable": f.get("readable"), "rows": f.get("rows")}
                  for f in intake.get("files") or []],
        "requirements": [{"item": r.get("item"), "kind": r.get("kind"),
                          "status": r.get("status"),
                          "evidence": r.get("evidence") if r.get("status") != "have" else ""}
                         for r in requirements],
        "artifacts": {"intake": (run.get("dir") or "") + "/intake.json"},
    }


def _summarize_cards(result) -> dict:
    """S2 结果给模型的摘要：够回答，又不把几十张卡片塞进上下文。"""
    data = result.model_dump(mode="json")
    checkable = [card for card in data.get("cards") or [] if card.get("checkable") == "能"]
    payload = {
        "ok": data.get("state") == "ok",
        "run_id": data.get("run_id"),
        "state": data.get("state"),
        "message": data.get("message"),
        "period": data.get("period"),
        "compare_period": data.get("compare_period"),
        "source": data.get("source"),
        "stats": data.get("stats") or {},
        "checkable_cards": [
            {
                "id": card.get("id"),
                "quote": card.get("quote"),
                "entity": card.get("entity"),
                "entity_type": card.get("entity_type"),
                "metric": card.get("metric"),
                "expect": card.get("expect"),
                "unit": card.get("unit"),
                "target_value": card.get("target_value"),
                "source": card.get("source"),
            }
            for card in checkable[:8]
        ],
        "checkable_total": len(checkable),
        "other_cards": len(data.get("cards") or []) - len(checkable),
        "actions": (data.get("actions") or [])[:5],
        "future": [item.get("text") for item in (data.get("future") or [])[:5]],
        "artifacts": {"cards": str(data.get("dir") or "") + "/cards.json"},
    }
    if data.get("questions"):
        payload["questions"] = data["questions"]
    if data.get("dropped"):
        payload["dropped"] = data["dropped"][:5]
    return payload


def _summarize_issues(result) -> dict:
    data = result.model_dump(mode="json")
    stats = data.get("stats") or {}
    blocking = [item for item in (data.get("issues") or []) if item.get("level") == "阻断"]
    warn = [item for item in (data.get("issues") or []) if item.get("level") == "提示"]
    uncovered = [c for c in (data.get("coverage") or []) if c.get("status") == "uncovered"]
    return {
        "ok": data.get("state") == "ok",
        "run_id": data.get("run_id"),
        "state": data.get("state"),
        "message": data.get("message"),
        "stats": stats,
        "blocking": blocking[:10],
        "warn": warn[:5],
        "uncovered": [c.get("item") for c in uncovered][:10],
        "checked_results": [
            {"card": item.get("card"), "value": item.get("value"),
             "explain": item.get("explain")}
            for item in (data.get("results") or [])[:5]
        ],
        "artifacts": {"issues": str(data.get("run_id") or "") + "/issues.json",
                      "ledger": str(data.get("run_id") or "") + "/ledger.json"},
    }


class Session:
    """一次对话：文件固定，消息累积，工具可重复调用。"""

    def __init__(self, files: list[Path], run_root: Path | None = None,
                 spec_path: Path | None = None, folder: Path | None = None,
                 session_id: str | None = None) -> None:
        self.id = session_id or uuid.uuid4().hex[:12]
        self.folder = Path(folder) if folder else None
        self.created_at = dt.datetime.now().isoformat(timespec="seconds")
        self.files = [Path(f) for f in files]
        self.run_root = Path(run_root or DEFAULT_RUN_ROOT)
        self.spec_path = Path(spec_path) if spec_path else None
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM}]
        self.last_run = ""
        self.last_prompt = ""
        self.run_ids: list[str] = []
        self.thoughts: list[str] = []
        self.progress: dict = {
            "started": False,
            "finished": False,
            "current": -1,
            "detail": "",
            "steps": [],
            "states": [],
        }

    def _files_note(self) -> str:
        if not self.files:
            return "\n\n本次会话还没有附件。"
        names = "、".join(f.name for f in self.files)
        return "\n\n本次会话已绑定 " + str(len(self.files)) + " 个文件：" + names + "。"

    # -- 落盘 --------------------------------------------------------------
    def snapshot(self) -> dict:
        """只存不含 system 的消息：提示词随代码更新，恢复时用当前版本。"""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "files": [f.name for f in self.files],
            "messages": self.messages[1:],
            "last_run": self.last_run,
            "last_prompt": self.last_prompt,
            "run_ids": list(self.run_ids),
        }

    def save(self) -> None:
        if self.folder is None:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / "session.json").write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass  # 落盘失败不影响这一轮对话

    @classmethod
    def restore(cls, folder: Path, run_root: Path | None = None,
                spec_path: Path | None = None) -> "Session | None":
        """从 session.json 恢复。文件不在了就只恢复对话，不恢复附件。"""
        folder = Path(folder)
        path = folder / "session.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        names = data.get("files") or []
        files = [folder / name for name in names if (folder / name).is_file()]
        session = cls(files, run_root=run_root, spec_path=spec_path,
                      folder=folder, session_id=str(data.get("id") or "") or None)
        session.created_at = str(data.get("created_at") or session.created_at)
        session.messages = [{"role": "system", "content": SYSTEM + session._files_note()}] \
            + list(data.get("messages") or [])
        session.last_run = str(data.get("last_run") or "")
        session.last_prompt = str(data.get("last_prompt") or "")
        session.run_ids = [str(x) for x in (data.get("run_ids") or [])]
        return session

    def add_files(self, paths: list[Path]) -> list[str]:
        """把新文件并入本次会话；同名文件视为替换。"""
        for path in paths:
            path = Path(path)
            self.files = [f for f in self.files if f.name != path.name]
            self.files.append(path)
        return [f.name for f in self.files]

    def progress_snapshot(self) -> dict:
        data = dict(self.progress)
        data["steps"] = list(self.progress.get("steps") or STEPS)
        data["states"] = list(self.progress.get("states") or [])
        data["thoughts"] = [item[:600] for item in self.thoughts[-6:]]
        return data

    # -- 工具 --------------------------------------------------------------
    def _run_s1(self, args: dict, fallback_prompt: str) -> dict:
        if not self.files:
            return {"ok": False, "error": "本次会话还没有上传文件，请先上传数据文件和纪要。"}
        prompt = str(args.get("prompt") or fallback_prompt or self.last_prompt or "").strip()
        self.last_prompt = prompt
        self.progress = {"started": False, "finished": False, "current": -1,
                         "detail": "", "steps": list(STEPS),
                         "states": ["pending"] * len(STEPS)}

        def on_progress(index: int, name: str, status: str, detail: str = "") -> None:
            states = self.progress["states"]
            if 0 <= index < len(states):
                if status == "active":
                    states[index] = "active"
                    self.progress["current"] = index
                    self.progress["started"] = True
                elif status == "done":
                    states[index] = "done"
            if detail:
                self.progress["detail"] = detail

        result, _status, _log = run_intake(
            files=self.files, prompt=prompt, run_root=self.run_root,
            spec_path=self.spec_path, progress=on_progress)
        self.progress["finished"] = True
        intake = result.model_dump()
        self.last_run = (intake.get("run") or {}).get("period_tag") or ""
        if self.last_run and self.last_run not in self.run_ids:
            self.run_ids.append(self.last_run)
        return _summarize(intake)

    def _get_run(self, args: dict) -> dict:
        run_id = str(args.get("run_id") or "").strip()
        section = str(args.get("section") or "summary").strip()
        folder = self.run_root / run_id
        intake = _read_json(folder / "intake.json")
        if not intake:
            return {"ok": False, "error": "找不到这次运行：" + run_id}
        if section in ("summary", "", "all"):
            data = _summarize(intake)
            if section == "all":
                data["requirements"] = intake.get("requirements") or []
                data["consistency"] = intake.get("consistency") or {}
                data["missing"] = intake.get("missing") or []
            return data
        if section == "files":
            return {"ok": True, "run_id": run_id, "files": intake.get("files") or []}
        if section == "requirements":
            return {"ok": True, "run_id": run_id,
                    "requirements": intake.get("requirements") or []}
        return {"ok": False, "error": "未知 section：" + section}

    def _list_runs(self) -> dict:
        """只列本次会话跑出来的运行；别的会话的历史不算。"""
        if not self.run_ids:
            return {"ok": True, "runs": [],
                    "note": "本次会话还没有运行记录，先调用 run_s1。"}
        items = []
        if self.run_root.exists():
            for folder in sorted(self.run_root.iterdir()):
                if not folder.is_dir() or folder.name.startswith("_"):
                    continue
                intake = _read_json(folder / "intake.json")
                if not intake:
                    continue
                run = intake.get("run") or {}
                status = _read_json(folder / "status.json")
                run_id = run.get("period_tag") or folder.name
                if run_id not in self.run_ids:
                    continue
                items.append({"run_id": run_id,
                              "dir": run.get("dir") or folder.name,
                              "state": intake.get("state"),
                              "at": status.get("at") or ""})
        items.sort(key=lambda x: x["at"], reverse=True)
        return {"ok": True, "runs": items}

    def _run_s2(self, args: dict) -> dict:
        """S2 拆卡。用本次会话最近一次准入的运行，不用用户再选。"""
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False,
                    "error": "还没有准入通过的运行，先调 run_s1，确认 state=准入通过 后再拆卡。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s2_cards import run_s2

            result, logs = run_s2(folder, force=bool(args.get("force")))
        except FileNotFoundError as exc:
            return {"ok": False, "error": "前置文件缺失：" + str(exc)}
        except Exception as exc:  # noqa: BLE001 - 失败原因交给模型转述
            return {"ok": False, "error": "S2 执行失败：" + str(exc)}
        payload = _summarize_cards(result)
        payload["log"] = logs[-6:]
        return payload

    def _run_s3(self, args: dict) -> dict:
        """S3 对账。纯代码取数，不调模型。"""
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先调 run_s1 和 run_s2。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s3_reconcile import run_s3

            result, logs = run_s3(folder)
        except FileNotFoundError as exc:
            return {"ok": False, "error": "前置文件缺失：" + str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "S3 执行失败：" + str(exc)}
        payload = _summarize_issues(result)
        payload["log"] = logs[-6:]
        return payload

    def _recalc_card(self, args: dict) -> dict:
        """只重算指定卡片，口径说明写进 overrides.json。"""
        card_id = str(args.get("card_id") or "").strip()
        hint = str(args.get("hint") or "").strip()
        if not card_id or not hint:
            return {"ok": False, "error": "card_id 和 hint 都必须给"}
        run_id = str(self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先跑 S1/S2/S3"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s3_reconcile import _save_override, run_s3

            _save_override(folder, [card_id], hint)
            result, logs = run_s3(folder, only=[card_id])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "重算失败：" + str(exc)}
        payload = _summarize_issues(result)
        payload["recalculated"] = card_id
        payload["caliber_hint"] = hint
        payload["log"] = logs[-6:]
        return payload

    def _waive_conflicts(self, args: dict) -> dict:
        """登记人工豁免，然后重跑 S1。豁免只对本次运行生效，写进 waiver.json 留痕。"""
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        items = [str(x).strip() for x in (args.get("items") or []) if str(x).strip()]
        reason = str(args.get("reason") or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先调 run_s1。"}
        if not items:
            return {"ok": False, "error": "没有给要豁免的冲突关键词。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到这次运行：" + run_id}
        payload = {
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "by": "用户",
            "items": items,
            "reason": reason or "用户确认可忽略",
        }
        (folder / "waiver.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = self._run_s1({"prompt": self.last_prompt}, self.last_prompt)
        summary["waived"] = items
        summary["waiver_reason"] = payload["reason"]
        return summary

    def _execute(self, name: str, args: dict, fallback_prompt: str) -> dict:
        if name == "run_s1":
            return self._run_s1(args, fallback_prompt)
        if name == "run_s2":
            return self._run_s2(args)
        if name == "run_s3":
            return self._run_s3(args)
        if name == "recalc_card":
            return self._recalc_card(args)
        if name == "waive_conflicts":
            return self._waive_conflicts(args)
        if name == "get_run":
            return self._get_run(args)
        if name == "list_runs":
            return self._list_runs()
        return {"ok": False, "error": "未知工具：" + name}

    # -- 对话 --------------------------------------------------------------
    def _stream_round(self, on_event) -> dict:
        """跑一轮流式 LLM，把 reasoning/content 实时抛给回调，返回最终结果。"""
        final = None
        for event in chat_stream(self.messages, temperature=0.2, max_tokens=8192,
                                 tools=TOOLS, timeout=180):
            if event.get("type") == "final":
                final = event.get("data") or {}
            else:
                on_event(event)
        if final is None:
            raise RuntimeError("流式返回缺少最终结果")
        return final

    def send(self, text: str, on_event=None) -> dict:
        self.thoughts = []
        self.progress = {"started": False, "finished": False, "current": -1,
                         "detail": "", "steps": list(STEPS),
                         "states": ["pending"] * len(STEPS)}
        text = (text or "").strip()
        if not text:
            self.progress["finished"] = True
            return {"reply": "请说说你想做什么。", "events": [], "run_id": self.last_run}
        self.messages[0] = {"role": "system", "content": SYSTEM + self._files_note()}
        self.messages.append({"role": "user", "content": text})
        events: list[dict] = []

        for _ in range(6):
            if on_event is None:
                out = chat(self.messages, temperature=0.2, max_tokens=8192,
                           tools=TOOLS, timeout=180)
            else:
                out = self._stream_round(on_event)
            reasoning = (out.get("reasoning") or "").strip()
            if reasoning:
                self.thoughts.append(reasoning[:2000])
            message = out.get("message") or {}
            calls = out.get("tool_calls") or []
            if not calls:
                reply = (message.get("content") or "").strip()
                if not reply:
                    reply = "模型没有返回内容，请稍后再试。"
                self.messages.append({"role": "assistant", "content": reply})
                self.progress["finished"] = True
                self.save()
                return {"reply": reply, "events": events, "run_id": self.last_run,
                        "thoughts": list(self.thoughts)}

            self.messages.append({"role": "assistant",
                                  "content": message.get("content") or "",
                                  "tool_calls": calls})
            if on_event is not None:
                # 这一轮只是决定调工具，前面流出的正文不是最终回答
                on_event({"type": "content_reset"})
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._execute(name, args, text)
                events.append({
                    "tool": name,
                    "run_id": result.get("run_id") or "",
                    "state": result.get("state") or "",
                    "ok": bool(result.get("ok")),
                })
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": json.dumps(result, ensure_ascii=False),
                })

        self.progress["finished"] = True
        self.save()
        return {"reply": "工具调用轮次过多，先停在这里。请把问题说得更具体一些。",
                "events": events, "run_id": self.last_run,
                "thoughts": list(self.thoughts)}
