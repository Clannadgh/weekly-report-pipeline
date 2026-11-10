"""主 agent：对话 + 调工具。

边界：它只负责理解、调工具、转述结果；不计算数字、不改判定、不写状态。
工具返回什么，它就说什么。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from .approvals import load as load_approvals, tally as tally_approvals
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
   - S2 已拆卡但还没对账 → 调 run_s3；
   - S3 已对账、阻断建议都已有结论但还没修纪要 → 调 run_s4；
   - S4 已可扩写但还没写正文 → 调 run_s5；
   - S3 有阻断还没裁决 → 提示用户到 S3 详情逐条「批准/拒绝」，不要调 run_s4；
   - 只有用户明确说「看看以前的运行」或问「上次/之前/上次缺什么」时，才调用 list_runs。
   - 用户问「上次」时，先看 list_runs 返回的 history 和 attempts：同一周期重跑会复用目录，但历次状态和归档都在。
     不要因为当前只有一个运行目录，就说「之前没有失败的运行」。
10. list_runs 只返回本次会话的运行记录。别的会话的 w42/w43/w44 不属于当前上下文，不要主动提，
    也不要把它们当成可选项让用户挑。
11. 用户明确说「不用管」「忽略」「豁免」某条冲突时，调用 waive_conflicts，把用户的原话作为 reason 传进去。
    这是人工裁决，不是工具改判：豁免会写进 waiver.json，交付归档时能查到是谁、什么时候、为什么放行。
    用户没说之前，绝不自己决定忽略。
12. 豁免后必须重跑一次 S1，以新的 state 为准；不要假设豁免一定会让状态变成准入通过。
13. 出周报的顺序是 S1 → S2 → S3 → S4 → S5 → S6。只有本次会话的 S1 返回「准入通过」时才能调 run_s2；
    S1 还是暂停或待补充时，先把问题处理完，不要跳过。
14. S2 只整理不判断。卡片里的「能对数据」才是可核对的结论，「不能」「不确定」只是留档，
    不要把它们当成结论讲给用户。
15. 转述 S2 结果时给三个数：卡片总数、可对数据数、动作项数；再挑可核对的卡片说原文和指标。
    不要自己计算卡片里的数字，也不要替 S2 判断卡片对不对。
16. 一次只推进一个阶段：调完 run_s1 就报告结果并停下，等用户说「继续」再调 run_s2；
    S3、S4、S5、S6 同理。只有用户明确说「一路做完」「不用问我」时，才允许连续调用。
17. 当前流水线实现到 S6（S5 扩写与审核，S6 交付）。S5 写完稿会替换 [[f:键名]] 并自动送审：
    状态从 可扩写 变为 待审核。不要自己替换占位符、不要手打数字顶上；
    S5 返回 待审核 时，报字数、图数、热点数和正文片段，并提醒用户到 /s5.html 审核。
18. 转述卡片时字段值照抄：指标、期望、单位、目标值是什么就说什么，
    不要把「具体数值」译成「七成」，也不要从原话里再加工出别的说法。
19. 顺序是 S1 → S2 → S3 → S4 → S5 → S6。S3 对账要等 S2 拆完卡；S4 修纪要要等 S3 对账完成、
    阻断建议都有结论；S5 扩写要等 S4 返回可扩写，扩写完成会自动送审，审核通过后到 S6 交付；
    S1 没准入、S2 没拆卡、S3 没对账、S4 没可扩写、S5 没完成或审核没通过时不要跳步。
20. S3 返回 issues 时，先报三个数：阻断、提示、信息；再把阻断级的问题逐条念出来
    （对象、纪要怎么说、数据是多少、建议怎么改）。有阻断时明确说「要逐条确认建议才能往下」，
    不要用「基本一致」「大致吻合」这类话把 mismatch 说轻。
21. S3 的数字只能照抄工具返回的 display / raw，不要自己再算一遍或换算单位。
22. 只有 S3 有阻断时才需要用户逐条处理建议：
    - S1 准入通过、S2 拆完卡就可以直接继续，不需要批准，也不要让用户点确认。
    - S1 不通过仍然走人工放权（补齐材料或按用户明确要求 waive_conflicts），
      不要让模型替用户放行，也不要自己宣布准入通过。
    - S3 有阻断（blocking > 0）时，提示用户打开 S3 详情，在「核对清单」里逐条点「批准」或「拒绝」：
      批准＝采纳这条修改建议，S4 改稿会照此改；拒绝＝不改。也可以点「全部批准」接受剩余建议。
    - 只有所有阻断都有结论后 S3 才算通过；进入下一步前调用 check_approval，
      未通过时不要继续，也不要重复跑 S3。
    - 阻断都裁决完后，用户说「继续」→ 调 run_s4 修纪要。S4 返回 state=待补充 时，
      先把 blocked 项或待裁决卡列出来，不要继续 S5。
    - S4 返回 state=可扩写 后，用户说「继续」→ 调 run_s5 扩写。S5 返回 state=待审核 时，
      报字数、图数、热点数和正文片段；占位符已由 S5 收尾替换，不要自己改数字或状态。
23. 用户对某张卡片的取数口径提出调整（例如「本周不算周六周日」「剔除停业那几天」）时，
    调 recalc_card：card_id 传卡片编号，hint 把用户的口径要求写成一句能被代码执行的说明
    （例如「只统计销售日期为周一至周五的记录，排除周六和周日」）。
    只重算这一张，其他卡片结果保留。不要自己重算数字，也不要改 ledger.json。
24. 口径说明必须具体到能落成条件（字段 + 判断），不要写「合理一点」「按惯例」这类空话；
    写不出来就问用户要明确的判断条件。
25. 用户在 S5 要增加 / 修改 / 删除数据发现的问题时，调 s5_questions：
    - 先 list 拿现有编号；update / delete 必须带 id（如 D3）；add 必须带 question 和 shape
      （一个总数=number，分组 / 排名 / 多行=table），reason 写为什么值得问。
    - 用户说的口径要原样落到 question 里，不要自己改条件；拿不准形状就问，不要猜。
    - 改完只是保存清单，不要声称已经重新扩写；告诉用户「重新扩写后生效」，
      用户同意后再调 run_s5。reset 表示恢复自动出题。
    - run_s5 默认增量：只重算新增 / 改动的问题，没改的复用当前稿结果；
      用户明确说「全部重跑 / 全量重跑」时才传 full=true。
    - 不要自己编造数据结果，也不要改 draft.md / ledger.json。
26. S5 扩写完成会自动送审，没有单独送审工具。
    - S5 返回 待审核 后，告诉用户打开 /s5.html 的审核台，由审核人填写签名并点「签名确认审核」
      或「退回修改」；不要替审核人判断，不要自己宣布通过，也不要把「待审核」说成「已通过」。
    - 签名是审核确认的一部分：审核人签名与通过结论一起写进 review_record.json，
      S6 交付只读取这份签名，不再要求用户签第二次。
    - 用户问审核结果或为什么退回时调 get_review，按 comments 的 back_to 回答：
      S3（数字 / 口径 / 遗漏）先回 S3 重取数，再经 S4 改稿；S5（措辞 / 结构 / 篇幅 / 图表）
      回 S5 重新扩写，扩写完成会再次自动送审。不要跳过 S3/S4 直接改正文。
    - 同一周累计退回 3 次会标「需线下沟通」并暂停；遇到就提示用户先线下确认，不要继续扩写送审。
    - 旧 qc_result.json 与当前稿哈希不一致时视为失效，不要当成 PASS 转述。
27. S5 审核返回已通过后，用户说「交付 / 归档 / 出正式版 / 要 Word」时进入 S6：调 run_s6。
    - run_s6 只读已落盘的成稿，按 S5 审核记录里的签名生成交付包；不要再向用户要交付签名。
    - run_s6 生成 Word + Markdown + 图表 + 台账 + 图表清单 + 质检说明 +
      审核记录 + 清单，落到 <运行目录>/deliver/<版本>/，状态改为 已交付。
    - 同内容重复归档返回原版本（幂等），只有内容变化才递增版本；不要承诺「一定出新版本」。
    - 不修改正文、不改数字、不重画图；用户要改内容，先回 S3/S5 并重新走 S5 审核。
    - 用户问交付进度、第几版、文件在哪时调 get_delivery，给版本号、指纹前 12 位、文件数、审核人。
      交付在 /s6.html 的第六步页面完成，审核签名已在 S5 记录；下载链接用工具返回的地址，不要自己拼路径。
    - 原质检已并入 S5；没有独立质检结论时如实说「本轮无独立质检」，不要说成 PASS。
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
            "name": "run_s4",
            "description": ("对本次会话已对账的运行做 S4 修纪要：按 S3 问题清单和用户逐条裁决，"
                            "把纪要里说错、说不清的单句改成和数据一致的说法，产出 corrected.json。"
                            "必须在 S3 对账完成、阻断建议都有结论后调用。"
                            "返回修正/保留/转未来/待处理条数、状态（可扩写/待补充）和待裁决卡。"),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_s5",
            "description": ("对本次会话已修纪要的运行做 S5 扩写与送审：按 corrected.json 骨架补数据细节、"
                            "按数据发现出图、在纪要骨架上补数据细节，产出 draft.md 并自动整理送审材料。"
                            "使用 s5_questions.json 里的数据发现问题清单；没有清单时由模型出题并保存。"
                            "已扩写过且只改了部分问题时，默认只重算改动的问题，其余复用当前结果。"
                            "必须在 S4 返回 state=可扩写 后调用。"
                            "完成后 state=待审核；返回字数、图数、热点、增量条数、审核轮次与正文片段；"
                            "不要替审核人通过，也不要自己改数字或状态。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "full": {"type": "boolean",
                             "description": "true=全量重跑；默认 false，只重算改动的问题"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "s5_questions",
            "description": ("查看 / 增加 / 修改 / 删除 S5 数据发现的问题清单。"
                            "action=list 查看当前清单；add 增加（question、shape 必填，reason 可选）；"
                            "update 按 id 改 question/shape/reason；delete 按 id 删除；"
                            "reset 删除清单，下次扩写重新由模型出题。"
                            "保存后不会自动重跑；用户同意后再调 run_s5。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "add", "update", "delete", "reset"],
                               "description": "要做的动作"},
                    "run_id": {"type": "string",
                               "description": "运行编号；不传用本次会话最近一次运行"},
                    "id": {"type": "string",
                           "description": "问题编号，如 D3；update / delete 必填"},
                    "question": {"type": "string",
                                 "description": "问题原文；add 必填，update 可选"},
                    "shape": {"type": "string", "enum": ["number", "table"],
                              "description": "number=一个数；table=分组/排名多行；add 必填"},
                    "reason": {"type": "string",
                               "description": "为什么值得问（一句话）"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_review",
            "description": ("读 S5 审核状态、退回意见与归位（不改状态）。"
                            "用户问「审核结果 / 为什么退回 / 退回改哪里」时调用。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string",
                               "description": "运行编号；不传用本次会话最近一次运行"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_s6",
            "description": ("S6 交付：把 S5 审核已通过并签名的稿子归档交付。检查审核结论与文件，"
                            "按 S5 审核记录里的签名生成交付包（不再单独要签名）。"
                            "生成 Word + Markdown + 图表 + 台账 + 图表清单 + 质检说明 + 审核记录 + 清单，"
                            "落到 <运行目录>/deliver/<版本>/，状态改为 已交付。"
                            "同内容重复归档返回原版本（幂等），只有内容变化才递增版本。"
                            "不修改正文、不改数字、不重画图。"),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_delivery",
            "description": ("读 S6 交付状态、版本列表与最新清单（不改状态）。"
                            "用户问「交付到哪了 / 第几版 / 文件在哪 / 下载」时调用。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string",
                               "description": "运行编号；不传用本次会话最近一次运行"},
                },
            },
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
            "name": "check_approval",
            "description": ("检查本次运行的 S3 阻断建议是否都已有结论（批准或拒绝）。"
                            "只有 S3 存在阻断时才需要调用；"
                            "approved=false 时提示用户到 S3 详情逐条「批准/拒绝」或点「全部批准」，不要继续。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "可选，默认最近一次运行"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_runs",
            "description": "列出本次会话内产生过的运行（周期、当前状态、时间、同一周期的历次状态 history、归档的详细尝试 attempts），不含其他会话的历史。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _blocking_ids(folder: Path) -> list[str]:
    """当前 issues.json 里阻断项的 id，顺序与文件一致。"""
    data = _read_json(folder / "issues.json")
    items = data.get("issues") if isinstance(data, dict) else None
    return [str(item.get("id")) for item in (items or [])
            if isinstance(item, dict) and item.get("level") == "阻断" and item.get("id")]


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

    def _check_approval(self, args: dict) -> dict:
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "approved": False,
                    "error": "还没有运行记录，先跑 S3。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "approved": False,
                    "error": "找不到运行目录：" + run_id}
        if not (folder / "issues.json").is_file():
            return {"ok": False, "approved": False,
                    "error": "还没有 S3 结果，先跑 S3。"}
        blocking = _blocking_ids(folder)
        if not blocking:
            return {"ok": True, "run_id": run_id, "stage": "s3",
                    "approved": True, "required": False, "blocking": 0,
                    "pending": [], "note": ""}
        approvals = load_approvals(folder)
        state = tally_approvals(approvals.get("s3"), blocking)
        return {
            "ok": True,
            "run_id": run_id,
            "stage": "s3",
            "approved": state["approved"],
            "required": not state["approved"],
            "blocking": state["total"],
            "accepted": state["accepted"],
            "rejected": state["rejected"],
            "pending": state["pending"],
            "at": (approvals.get("s3") or {}).get("at") or "",
            "by": (approvals.get("s3") or {}).get("by") or "",
            "note": ("" if state["approved"] else
                     "还有 " + str(len(state["pending"])) + " 条阻断待处理，请用户打开 S3 详情，"
                     "在「核对清单」里逐条点「批准」或「拒绝」，或点「全部批准」。"),
        }

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
                attempts = []
                attempts_dir = folder / "attempts"
                if attempts_dir.is_dir():
                    for path in sorted(attempts_dir.glob("*.json"), reverse=True)[:5]:
                        archived = _read_json(path)
                        old_intake = archived.get("intake") or {}
                        old_status = archived.get("status") or {}
                        attempts.append({
                            "at": archived.get("archived_at") or old_status.get("at") or "",
                            "state": old_intake.get("state") or old_status.get("state") or "",
                            "message": old_intake.get("message") or old_status.get("note") or "",
                            "questions": old_intake.get("questions") or [],
                        })
                items.append({"run_id": run_id,
                              "dir": run.get("dir") or folder.name,
                              "state": intake.get("state"),
                              "at": status.get("at") or "",
                              "history": (status.get("history") or [])[-10:],
                              "attempts": attempts})
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

    def _with_approval(self, folder: Path, payload: dict, logs: list) -> dict:
        """把当前阻断与逐条结论一起返回，避免模型误以为旧批准仍然有效。"""
        blocking = _blocking_ids(folder)
        state = tally_approvals(load_approvals(folder).get("s3"), blocking)
        payload["approved"] = state["approved"]
        payload["approval_required"] = bool(blocking) and not state["approved"]
        payload["blocking"] = state["total"]
        payload["accepted"] = state["accepted"]
        payload["rejected"] = state["rejected"]
        payload["pending"] = state["pending"]
        payload["approval_note"] = (
            "" if state["approved"] else
            "S3 有 " + str(len(state["pending"])) + " 条阻断待处理，请用户打开 S3 详情，"
            "逐条点「批准」或「拒绝」，或点「全部批准」。")
        payload["log"] = logs[-6:]
        return payload

    def _run_s3(self, args: dict) -> dict:
        """S3 对账：用 pandasai 生成取数代码，判定由代码按容差做。"""
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
        return self._with_approval(folder, payload, logs)

    def _run_s4(self, args: dict) -> dict:
        """S4 修纪要：读 S3 问题与用户裁决，逐句改纪要底稿。"""
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先跑 S1 → S2 → S3。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s4_draft import run_s4

            outcome = run_s4(folder)
        except FileNotFoundError as exc:
            return {"ok": False, "error": "前置文件缺失：" + str(exc)}
        except Exception as exc:  # noqa: BLE001 - 失败原因交给模型转述
            return {"ok": False, "error": "S4 执行失败：" + str(exc)}
        data = outcome.to_dict() if hasattr(outcome, "to_dict") else outcome
        corrected = data.get("corrected") or {}
        payload = {
            "ok": bool(data.get("ok")),
            "state": data.get("state") or "",
            "message": data.get("message") or "",
            "run_id": data.get("run_id") or run_id,
            "week": data.get("week") or "",
            "stats": corrected.get("stats") or {},
            "items": [
                {key: item.get(key) for key in
                 ("id", "status", "original", "fixed", "basis", "issue", "note", "confirmed")}
                for item in (corrected.get("items") or [])
                if item.get("status") != "kept"
            ][:12],
            "cards": corrected.get("cards") or [],
            "future_count": len(corrected.get("future") or []),
            "log": (data.get("log") or [])[-6:],
        }
        return payload

    def _run_s5(self, args: dict) -> dict:
        """S5 扩写：画像、发现、出图，在纪要骨架上补细节，产出带占位符的 draft.md。"""
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先跑 S1 → S2 → S3 → S4。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s5_write import run_s5

            outcome = run_s5(folder, full=bool(args.get("full")))
        except FileNotFoundError as exc:
            return {"ok": False, "error": "前置文件缺失：" + str(exc)}
        except Exception as exc:  # noqa: BLE001 - 失败原因交给模型转述
            return {"ok": False, "error": "S5 执行失败：" + str(exc)}
        data = outcome.to_dict() if hasattr(outcome, "to_dict") else outcome
        stats = data.get("stats") or {}
        pages = stats.get("pages_estimate")
        hotspots = []
        for item in (data.get("hotspots") or [])[:8]:
            change = item.get("change")
            impact = item.get("impact")
            hotspots.append({
                "object": item.get("object") or "",
                "metric": item.get("metric") or "",
                "change": (f"{float(change) * 100:+.1f}%" if isinstance(change, (int, float)) else ""),
                "impact": (f"{float(impact):,.0f}" if isinstance(impact, (int, float)) else ""),
                "written": bool(item.get("written")),
                "section": item.get("section") or "",
            })
        figures = [{
            "number": item.get("number"), "title": item.get("title") or "",
            "caption": item.get("caption") or "",
        } for item in (data.get("figures") or [])]
        payload = {
            "ok": bool(data.get("ok")),
            "state": data.get("state") or "",
            "message": data.get("message") or "",
            "run_id": data.get("run_id") or run_id,
            "week": data.get("week") or "",
            "stats": {
                "chars": stats.get("chars_body_rendered") or 0,
                "figures": stats.get("figures") or 0,
                "hotspots": stats.get("hotspots") or 0,
                "pages": pages.get("value") if isinstance(pages, dict) else 0,
                "sections": stats.get("section_count") or 0,
                "placeholders": (stats.get("placeholders") or {}).get("total") or 0,
            },
            "figures": figures,
            "hotspots": hotspots,
            "questions": len(data.get("questions") or []),
            "questions_source": data.get("questions_source") or "",
            "incremental": bool(data.get("incremental")),
            "recomputed": int(data.get("recomputed") or 0),
            "reused": int(data.get("reused") or 0),
            "removed": int(data.get("removed") or 0),
            "round": int((data.get("review") or {}).get("round") or 0),
            "unconfirmed": len(((data.get("review") or {}).get("materials") or {})
                               .get("unconfirmed") or []),
            "review_page": "/s5.html?run=" + run_id,
            "draft_excerpt": (data.get("draft") or "")[:700],
            "log": (data.get("log") or [])[-6:],
        }
        return payload

    def _s5_questions(self, args: dict) -> dict:
        """S5 数据发现问题清单：查看 / 增删改 / 恢复自动出题。"""
        action = str(args.get("action") or "list").strip().lower()
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先跑 S1 → S4。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s5_write import is_running

            if is_running():
                return {"ok": False, "error": "S5 正在运行，等它结束后再改问题。"}
        except Exception:  # noqa: BLE001 - 运行标记失败不挡编辑
            pass
        try:
            from . import s5_questions as sq

            if action == "list":
                payload = self._questions_payload(folder, run_id, action)
                if payload.get("source") == "last_run":
                    payload["note"] = ("这是当前稿的 "
                                       + str(payload.get("count") or 0)
                                       + " 条发现，不是完整问题清单；下次扩写会重新出题。"
                                       "要沿用，先用 add / update 保存。")
                elif payload.get("dirty"):
                    payload["note"] = "清单已保存，但与当前稿不一致；重新扩写后生效。"
                elif payload.get("applied"):
                    payload["note"] = "清单已保存，当前稿已使用。"
                else:
                    payload["note"] = "清单已保存；下次扩写使用。"
                return payload
            if action == "reset":
                sq.reset_plan(folder)
                payload = self._questions_payload(folder, run_id, action)
                payload["note"] = ("已恢复自动出题；下次扩写会重新生成问题。"
                                   "当前显示的是上一稿的问题。")
                return payload
            if action not in ("add", "update", "delete"):
                return {"ok": False,
                        "error": "action 只能是 list / add / update / delete / reset。"}

            current = sq.effective(folder)
            questions = [dict(item) for item in current["questions"]]
            if action == "add":
                question = str(args.get("question") or "").strip()
                shape = str(args.get("shape") or "").strip().lower()
                if not question:
                    return {"ok": False, "error": "add 需要 question。"}
                if shape not in ("number", "table"):
                    return {"ok": False,
                            "error": "add 需要 shape：number（一个数）或 table（分组 / 排名多行）。"}
                questions.append({
                    "id": sq.next_id(questions),
                    "question": question,
                    "shape": shape,
                    "reason": str(args.get("reason") or "").strip(),
                })
            elif action == "update":
                qid = str(args.get("id") or "").strip()
                if not qid:
                    return {"ok": False, "error": "update 需要 id，例如 D3。"}
                target = next((q for q in questions if str(q.get("id")) == qid), None)
                if target is None:
                    return {"ok": False,
                            "error": "没有编号 " + qid + " 的问题；现有编号："
                                     + "、".join(str(q.get("id")) for q in questions)}
                changed = False
                if args.get("question") is not None:
                    target["question"] = str(args.get("question") or "").strip()
                    changed = True
                if args.get("shape") is not None:
                    target["shape"] = str(args.get("shape") or "").strip().lower()
                    changed = True
                if args.get("reason") is not None:
                    target["reason"] = str(args.get("reason") or "").strip()
                    changed = True
                if not changed:
                    return {"ok": False,
                            "error": "update 至少给一个要改的字段：question / shape / reason。"}
            else:
                qid = str(args.get("id") or "").strip()
                if not qid:
                    return {"ok": False, "error": "delete 需要 id，例如 D3。"}
                before = len(questions)
                questions = [q for q in questions if str(q.get("id")) != qid]
                if len(questions) == before:
                    return {"ok": False,
                            "error": "没有编号 " + qid + " 的问题；现有编号："
                                     + "、".join(str(q.get("id")) for q in questions)}

            sq.save_plan(folder, questions, source="user", run_id=run_id)
            payload = self._questions_payload(folder, run_id, action)
            if payload.get("dirty"):
                payload["note"] = "已保存。问题清单与当前稿不一致，重新扩写后才会生效。"
            elif payload.get("applied"):
                payload["note"] = "已保存；当前稿就是用这份清单生成的。"
            else:
                payload["note"] = ("已保存；下次扩写只跑清单里的 "
                                   + str(payload.get("count") or 0)
                                   + " 条问题，不会自动补题。")
            return payload
        except ValueError as exc:
            return {"ok": False, "error": "问题清单无效：" + str(exc)}
        except Exception as exc:  # noqa: BLE001 - 失败原因交给模型转述
            return {"ok": False, "error": "问题清单操作失败：" + str(exc)}

    def _questions_payload(self, folder: Path, run_id: str, action: str) -> dict:
        from . import s5_questions as sq

        data = sq.effective(folder)
        return {
            "ok": True, "run_id": run_id, "action": action,
            "count": data["count"], "source": data["source"],
            "dirty": data["dirty"], "applied": data["applied"],
            "updated_at": data["updated_at"],
            "questions": data["questions"],
        }

    def _get_review(self, args: dict) -> dict:
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先跑 S5。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s5_review import read_status

            data = read_status(folder)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "读取 S5 审核状态失败：" + str(exc)}
        data["ok"] = True
        data["run_id"] = run_id
        data["review_page"] = "/s5.html?run=" + run_id
        return data

    def _run_s6(self, args: dict) -> dict:
        """S6 交付归档：只读成稿，按内容指纹出版本。"""
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先跑 S1 → S5 并通过审核。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s6_deliver import deliver

            outcome = deliver(folder)
        except Exception as exc:  # noqa: BLE001 - 失败原因交给模型转述
            return {"ok": False, "error": "交付失败：" + str(exc)}
        data = outcome.to_dict()
        return {
            "ok": bool(data.get("ok")),
            "state": data.get("state") or "",
            "message": data.get("message") or "",
            "run_id": data.get("run_id") or run_id,
            "version": data.get("version") or "",
            "path": data.get("path") or "",
            "idempotent": bool(data.get("idempotent")),
            "files": len(data.get("files") or []),
            "fingerprint": str((data.get("manifest") or {}).get("content_fingerprint") or ""),
            "deliver_page": "/s6.html?run=" + run_id,
        }

    def _get_delivery(self, args: dict) -> dict:
        run_id = str(args.get("run_id") or self.last_run or "").strip()
        if not run_id:
            return {"ok": False, "error": "还没有运行记录，先跑 S5 并通过审核。"}
        folder = self.run_root / run_id
        if not folder.is_dir():
            return {"ok": False, "error": "找不到运行目录：" + run_id}
        try:
            from .s6_deliver import read_status

            data = read_status(folder)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "读取交付状态失败：" + str(exc)}
        manifest = data.get("manifest") or {}
        data["manifest"] = {
            "version": manifest.get("version") or "",
            "content_fingerprint": manifest.get("content_fingerprint") or "",
            "layout_version": manifest.get("layout_version") or "",
            "delivered_at": manifest.get("delivered_at") or "",
            "files": len(manifest.get("files") or []),
            "review": manifest.get("review") or {},
            "qc": manifest.get("qc") or {},
        }
        data["ok"] = True
        data["run_id"] = run_id
        return data

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
        return self._with_approval(folder, payload, logs)

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
        if name == "run_s4":
            return self._run_s4(args)
        if name == "run_s5":
            return self._run_s5(args)
        if name == "s5_questions":
            return self._s5_questions(args)
        if name == "get_review":
            return self._get_review(args)
        if name == "run_s6":
            return self._run_s6(args)
        if name == "get_delivery":
            return self._get_delivery(args)
        if name == "recalc_card":
            return self._recalc_card(args)
        if name == "waive_conflicts":
            return self._waive_conflicts(args)
        if name == "get_run":
            return self._get_run(args)
        if name == "check_approval":
            return self._check_approval(args)
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
