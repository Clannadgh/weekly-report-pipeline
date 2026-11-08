"""主 agent：对话 + 调工具。

边界：它只负责理解、调工具、转述结果；不计算数字、不改判定、不写状态。
工具返回什么，它就说什么。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from .llm import chat
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
9. 用户说「继续」时，先看本次会话最近一次 run_s1 的结果：
   - 如果它是暂停或待补充，就追问缺的那一项（例如「还缺 1 份纪要」），不要列历史运行让用户选；
   - 如果本次会话还没跑过，就直接对当前文件跑 run_s1；
   - 只有用户明确说「看看以前的运行」时，才调用 list_runs。
10. list_runs 只返回本次会话的运行记录。别的会话的 w42/w43/w44 不属于当前上下文，不要主动提，
    也不要把它们当成可选项让用户挑。
11. 用户明确说「不用管」「忽略」「豁免」某条冲突时，调用 waive_conflicts，把用户的原话作为 reason 传进去。
    这是人工裁决，不是工具改判：豁免会写进 waiver.json，S8 归档时能查到是谁、什么时候、为什么放行。
    用户没说之前，绝不自己决定忽略。
12. 豁免后必须重跑一次 S1，以新的 state 为准；不要假设豁免一定会让状态变成准入通过。
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


class Session:
    """一次对话：文件固定，消息累积，工具可重复调用。"""

    def __init__(self, files: list[Path], run_root: Path | None = None,
                 spec_path: Path | None = None, folder: Path | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.folder = Path(folder) if folder else None
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
        if name == "waive_conflicts":
            return self._waive_conflicts(args)
        if name == "get_run":
            return self._get_run(args)
        if name == "list_runs":
            return self._list_runs()
        return {"ok": False, "error": "未知工具：" + name}

    # -- 对话 --------------------------------------------------------------
    def send(self, text: str) -> dict:
        self.thoughts = []
        self.progress = {"started": False, "finished": False, "current": -1,
                         "detail": "", "steps": list(STEPS),
                         "states": ["pending"] * len(STEPS)}
        text = (text or "").strip()
        if not text:
            return {"reply": "请说说你想做什么。", "events": [], "run_id": self.last_run}
        self.messages.append({"role": "user", "content": text})
        events: list[dict] = []

        for _ in range(6):
            out = chat(self.messages, temperature=0.2, max_tokens=8192,
                       tools=TOOLS, timeout=180)
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
                return {"reply": reply, "events": events, "run_id": self.last_run,
                        "thoughts": list(self.thoughts)}

            self.messages.append({"role": "assistant",
                                  "content": message.get("content") or "",
                                  "tool_calls": calls})
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

        return {"reply": "工具调用轮次过多，先停在这里。请把问题说得更具体一些。",
                "events": events, "run_id": self.last_run,
                "thoughts": list(self.thoughts)}
