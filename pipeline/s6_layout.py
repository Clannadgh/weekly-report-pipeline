"""S6 AI 排版层：交付时由模型决定选图、小节标题、小标题与分页，代码负责版式与校验。

分工：
- 模型只输出 JSON 计划（headings / figures / subheadings / page_break_before），不写正文；
- 正文段落、图注、表格数字全部来自输入，代码逐字写进排版稿；
- 图片位置由代码按正文引用锁定，图宽按图片比例和上限计算；
- 计划不合法或预估页数超限时退回确定性计划，不阻塞交付。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CN_NUM = ["一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、", "九、", "十、",
          "十一、", "十二、", "十三、", "十四、", "十五、"]
FIG_REF_GROUP = re.compile("（见图[^）]*）")
FIG_REF_NUM = re.compile("[0-9]+")
NUM_RE = re.compile("[0-9][0-9,]*(?:[.][0-9]+)?")
FORBIDDEN = ("【数据发现】", "pandasai", "s5_facts")

WIDTH_CM = {"wide": 11.0, "medium": 9.0, "small": 7.5}
MAX_FIG_HEIGHT_CM = 5.4
MAX_FIGURES = 3
MIN_FIGURES = 2
TARGET_PAGES = 3.6

_TOPIC_RULES = (
    ("区域", ("区域", "华东", "华南", "华北", "西南")),
    ("品类", ("品类", "商品", "饮品", "蛋糕", "面包", "西点", "礼盒")),
    ("渠道", ("渠道", "堂食", "外卖", "自提")),
    ("门店", ("门店", "店")),
    ("折扣", ("折扣",)),
)


def _numbers(text: str):
    from collections import Counter

    return Counter(item.replace(",", "").rstrip(".") for item in NUM_RE.findall(text or ""))


def _fig_numbers(text: str) -> list[int]:
    out: list[int] = []
    for match in FIG_REF_GROUP.finditer(text or ""):
        for raw in FIG_REF_NUM.findall(match.group(0)):
            value = int(raw)
            if value not in out:
                out.append(value)
    return out


def _clean(value) -> str:
    return re.sub("[ \\u3000]+", " ", str(value or "")).strip()


_CAPTION_STOP = ("最高", "最低", "合计", "比较基准", "单位", "周期", "走势",
                 "分布", "构成", "情况")


def _caption_keywords(caption: str) -> list[str]:
    """从图题里取可用于定位正文段落的关键词（对象名 + 指标名）。"""
    text = re.sub("（[^）]*）", "", str(caption or ""))
    text = re.sub("^图[ 　]*[0-9]+[ 　]*", "", text)
    words: list[str] = []
    for part in re.split(r"[：:，,、；;\s]+", text):
        cleaned = part
        for stop in _CAPTION_STOP:
            cleaned = cleaned.replace(stop, "")
        cleaned = cleaned.strip()
        if len(cleaned) >= 2 and cleaned not in words:
            words.append(cleaned)
    return words


def _anchor_by_keywords(fig: dict, rows: list[tuple[int, int, int, str]],
                        used: set[int]) -> int:
    """正文不写「见图 N」后，按图题与段落的共同关键词就近落图。"""
    words = _caption_keywords(str(fig.get("caption") or fig.get("title") or ""))
    if not words:
        return 0
    best_score, best_index = 0, 0
    for _, _, index, text in rows:
        if index in used:
            continue
        score = sum(1 for word in words if word in text)
        if score > best_score:
            best_score, best_index = score, index
    return best_index if best_score >= 2 else 0


def _figure_targets(sections: list[dict], figures: list[dict],
                    selected: list[dict]) -> dict[int, int]:
    """每张入选图落在哪个段落之后；图号按这个位置从上到下重排。"""
    rows = _paragraph_index(sections)
    total = rows[-1][2] if rows else 0
    analysis_end = total
    _, sec_ends = _positions(sections)
    for sec_index, sec in enumerate(sections):
        heading = str(sec.get("heading") or "")
        if "下周" in heading or "未来" in heading:
            analysis_end = sec_ends[sec_index] - len(sec.get("blocks") or [])
            break
    by_number = {int(fig["number"]): fig for fig in figures}
    targets: dict[int, int] = {}
    used: set[int] = set()
    for item in selected:
        try:
            number = int(item.get("number"))
        except (TypeError, ValueError):
            continue
        fig = by_number.get(number)
        if not fig:
            continue
        target = 0
        for _, _, index, text in rows:
            if number in _fig_numbers(text):
                target = index
                break
        if not target:
            target = _anchor_by_keywords(fig, rows, used)
        if target:
            used.add(target)
        targets[number] = target or analysis_end or total
    return targets


def _caption_with_number(caption: str, number: int) -> str:
    text = str(caption or "").strip()
    if re.match("^图[ 　]*[0-9]+", text):
        return re.sub("^图[ 　]*[0-9]+", "图 " + str(number), text, count=1)
    return "图 " + str(number) + ("　" + text if text else "")


def _renumber_for_display(sections: list[dict], figures: list[dict],
                          plan: dict) -> tuple[list[dict], dict, dict[int, int]]:
    """按图在正文中的实际落位重排图号，返回用于渲染的图与计划。"""
    selected = [item for item in (plan.get("figures") or []) if isinstance(item, dict)]
    if not selected:
        return figures, plan, {}
    targets = _figure_targets(sections, figures, selected)
    ordered = sorted(enumerate(selected),
                     key=lambda pair: (targets.get(int(pair[1].get("number")), 0), pair[0]))
    mapping = {int(item["number"]): display_no
               for display_no, (_, item) in enumerate(ordered, 1)}
    if all(source == display for source, display in mapping.items()):
        return figures, plan, {}
    by_number = {int(fig["number"]): fig for fig in figures}
    display_figures: list[dict] = []
    display_plan = dict(plan)
    display_plan["figures"] = []
    for display_no, (_, item) in enumerate(ordered, 1):
        source_no = int(item["number"])
        fig = by_number.get(source_no)
        if not fig:
            continue
        clone = dict(fig)
        clone["number"] = display_no
        clone["caption"] = _caption_with_number(
            fig.get("caption") or fig.get("title"), display_no)
        display_figures.append(clone)
        width = dict(item)
        width["number"] = display_no
        display_plan["figures"].append(width)
    display_plan["figure_number_map"] = [
        {"source": source, "display": display}
        for source, display in sorted(mapping.items(), key=lambda pair: pair[1])
    ]
    return display_figures, display_plan, mapping


def _display(title: str, headings: dict | None) -> str:
    return (headings or {}).get(title) or title


def _block_text(block) -> str:
    if isinstance(block, dict):
        return str(block.get("text") or "")
    return str(block or "")


def _positions(sections: list[dict]) -> tuple[list[int], list[int]]:
    """每个小节的段落起止（全局 1-based，闭区间）与总段落数。"""
    starts: list[int] = []
    ends: list[int] = []
    cursor = 0
    for sec in sections:
        count = len(sec.get("blocks") or [])
        starts.append(cursor + 1)
        cursor += count
        ends.append(cursor)
    return starts, ends


def _paragraph_index(sections: list[dict]) -> list[tuple[int, int, int, str]]:
    """(小节下标, 段落下标, 全局段落号, 文本)，全局段落号从 1 开始。"""
    rows: list[tuple[int, int, int, str]] = []
    cursor = 0
    for sec_index, sec in enumerate(sections):
        for block_index, block in enumerate(sec.get("blocks") or []):
            cursor += 1
            rows.append((sec_index, block_index, cursor, _block_text(block)))
    return rows


def _topic(caption: str) -> str:
    text = str(caption or "")
    text = re.sub("（[^）]*）", "", text)
    for name, words in _TOPIC_RULES:
        if any(word in text for word in words):
            return name
    return "其他"


def _clean_refs(text: str) -> str:
    """正文不写「见图 N」：删掉所有图号引用，图改由关键词就近落位。"""
    from . import s5_filters

    return s5_filters.strip_figure_refs(text)


def clean_sections(sections: list[dict], selected: set[int] | None = None) -> list[dict]:
    out: list[dict] = []
    for sec in sections:
        blocks = []
        for block in sec.get("blocks") or []:
            item = dict(block) if isinstance(block, dict) else {"type": "p", "text": str(block)}
            item["text"] = _clean_refs(item.get("text") or "")
            blocks.append(item)
        out.append({"heading": sec.get("heading"), "blocks": blocks})
    return out


def _figure_width_key(fig: dict) -> str:
    shape = str(fig.get("shape") or "").lower()
    if shape in ("share", "bar", "line"):
        return "wide"
    if shape in ("barh",):
        return "small"
    return "medium"


def select_figures(sections: list[dict], figures: list[dict],
                   limit: int = MAX_FIGURES) -> list[dict]:
    """确定性选图：优先不同主题、被正文引用、图形不重复，最多三张。"""
    referenced: set[int] = set()
    for _, _, _, text in _paragraph_index(sections):
        referenced.update(_fig_numbers(text))
    topics: dict[str, dict] = {}
    for fig in figures:
        number = int(fig["number"])
        topic = _topic(str(fig.get("caption") or fig.get("title") or ""))
        score = 0
        if number in referenced:
            score += 10
        score += {"区域": 5, "品类": 4, "渠道": 3, "门店": 2, "折扣": 1}.get(topic, 0)
        caption = str(fig.get("caption") or "")
        shape = str(fig.get("shape") or "").lower()
        score += {"share": 2, "bar": 1, "barh": 0, "line": -2}.get(shape, 0)
        if "走势" in caption or "趋势" in caption:
            score -= 3
        if "实收" in caption:
            score += 1
        score -= max(0, len(caption) - 80) / 100
        if topic not in topics or score > topics[topic]["_score"]:
            topics[topic] = {"fig": fig, "_score": score}
    ordered = sorted(topics.values(), key=lambda item: item["_score"], reverse=True)
    chosen = [item["fig"] for item in ordered[:limit]]
    if len(chosen) < MIN_FIGURES:
        chosen = figures[:limit]
    chosen.sort(key=lambda fig: int(fig["number"]))
    return [{"number": int(fig["number"]), "width": _figure_width_key(fig)}
            for fig in chosen]


def _subheadings_from_text(sections: list[dict]) -> list[dict]:
    """没有 AI 时的保守小标题：只按正文关键词切分，最多三个。"""
    rules = (
        ("渠道与区域", ("渠道", "区域")),
        ("门店与品类", ("门店", "品类")),
        ("数据质量与异常", ("退货", "POS", "补录", "故障")),
    )
    out: list[dict] = []
    used: set[str] = set()
    # 按全文顺序插入，但只在分析小节内
    for sec_index, sec in enumerate(sections):
        heading = str(sec.get("heading") or "")
        if "下周" in heading or "未来" in heading:
            continue
        cursor = sum(len(item.get("blocks") or []) for item in sections[:sec_index])
        for offset, block in enumerate(sec.get("blocks") or []):
            text = _block_text(block)
            for label, words in rules:
                if label in used or not any(word in text for word in words):
                    continue
                # 小标题放在命中段落的上一段之后，让它统领这一段
                if offset == 0:
                    continue
                out.append({"after": cursor + offset, "text": label})
                used.add(label)
                break
    return out[:3]


def deterministic_plan(sections: list[dict], figures: list[dict],
                       headings: dict | None = None,
                       limit: int = MAX_FIGURES) -> dict:
    return {
        "headings": {sec["heading"]: _display(sec["heading"], headings)
                     for sec in sections},
        "figures": select_figures(sections, figures, limit=limit),
        "subheadings": _subheadings_from_text(sections),
        "page_break_before": [],
    }


def validate_plan(sections: list[dict], figures: list[dict], plan: dict) -> list[str]:
    errors: list[str] = []
    headings = plan.get("headings") or {}
    original = {str(sec.get("heading") or "") for sec in sections}
    if not isinstance(headings, dict):
        return ["headings 不是对象"]
    for key, value in headings.items():
        if key not in original:
            errors.append("多出小节：" + str(key))
        text = _clean(value)
        if not text or len(text) > 20:
            errors.append("小节标题为空或过长：" + str(key))

    total = sum(len(sec.get("blocks") or []) for sec in sections)
    known = {int(fig["number"]) for fig in figures}
    selected = plan.get("figures")
    if not isinstance(selected, list):
        errors.append("figures 不是数组")
    else:
        wanted_min = min(MIN_FIGURES, len(figures))
        if len(selected) < wanted_min or len(selected) > MAX_FIGURES:
            errors.append("选图数量应为 " + str(wanted_min) + "–" + str(MAX_FIGURES) + " 张")
        seen_numbers: set[int] = set()
        for item in selected:
            if not isinstance(item, dict):
                errors.append("选图项不是对象")
                continue
            try:
                number = int(item.get("number"))
            except (TypeError, ValueError):
                errors.append("选图编号不是整数")
                continue
            if number not in known:
                errors.append("选了不存在的图：" + str(number))
            if number in seen_numbers:
                errors.append("重复选图：" + str(number))
            seen_numbers.add(number)
            if str(item.get("width") or "") not in WIDTH_CM:
                errors.append("图 " + str(number) + " 的宽度不是 wide/medium/small")

    subs = plan.get("subheadings") or []
    if not isinstance(subs, list):
        errors.append("subheadings 不是数组")
    else:
        seen_after: set[int] = set()
        for item in subs:
            if not isinstance(item, dict):
                errors.append("小标题项不是对象")
                continue
            try:
                after = int(item.get("after"))
            except (TypeError, ValueError):
                errors.append("小标题位置不是整数")
                continue
            if after < 1 or after > total:
                errors.append("小标题位置越界：" + str(after))
            if after in seen_after:
                errors.append("同一段后重复加小标题：" + str(after))
            seen_after.add(after)
            text = _clean(item.get("text"))
            if not text or len(text) > 24:
                errors.append("小标题为空或过长：" + str(item.get("after")))

    breaks = plan.get("page_break_before") or []
    if not isinstance(breaks, list):
        errors.append("page_break_before 不是数组")
    else:
        displays = {_strip_number(_clean(v)) for v in headings.values()}
        displays |= {_strip_number(_clean(str(sec.get("heading") or "")))
                     for sec in sections}
        for item in breaks:
            if _strip_number(_clean(item)) not in displays:
                errors.append("分页位置不存在：" + str(item))
    return errors


def _strip_number(text: str) -> str:
    """允许模型在分页标签里带「二、」这类编号。"""
    return re.sub("^[一二三四五六七八九十]+、[ \\u3000]*", "", text or "")


def ai_plan(sections: list[dict], figures: list[dict], meta: dict,
            chat_fn=None, log: list[str] | None = None) -> dict | None:
    log = log if log is not None else []
    try:
        if chat_fn is None:
            from .llm import chat as chat_fn  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.append("AI 排版跳过：" + str(exc))
        return None

    rows = _paragraph_index(sections)
    refs: dict[int, int] = {}
    for _, _, number, text in rows:
        for fig_no in _fig_numbers(text):
            refs.setdefault(fig_no, number)

    lines = ["下面是经营周报的小节和段落（段落全局编号从 1 开始）。请做排版计划："]
    lines.append("1) 给每个小节起一个更适合管理层阅读的标题（不要编号）；")
    lines.append("2) 从下面图片里选 2–3 张放进报告，优先不同主题，避免同一指标重复；")
    lines.append("   优先 share / bar 这类横图；barh 门店排名长图很占高度，除非结论只在排名里，否则不选；")
    lines.append("3) 每张图给一个宽度：wide（通栏 11cm）/ medium（9cm）/ small（7.5cm）；")
    lines.append("4) 可选地在某些段落之后加小标题（最多 4 个，每个不超过 24 字）；")
    lines.append("5) 默认不要分页；只有某节前确实需要另起一页时才写，最多 1 处。")
    lines.append("")
    lines.append("小节与段落：")
    cursor = 0
    for sec in sections:
        lines.append("## " + str(sec.get("heading") or ""))
        for block in sec.get("blocks") or []:
            cursor += 1
            lines.append(str(cursor) + ". " + _block_text(block))
    lines.append("")
    lines.append("图片：")
    for fig in figures:
        number = int(fig["number"])
        suffix = ("（正文第 " + str(refs[number]) + " 段引用）"
                  if refs.get(number) else "")
        lines.append("- 图 " + str(number) + "：" + str(fig.get("caption") or "") + suffix)
    lines.append("")
    lines.append("只输出 JSON：")
    lines.append('{"headings": {"原标题": "新标题"}, '
                 '"figures": [{"number": 7, "width": "wide"}, {"number": 6, "width": "wide"}], '
                 '"subheadings": [{"after": 5, "text": "渠道与区域"}], '
                 '"page_break_before": ["二、下周工作重点"]}')
    lines.append("不要新增、删除或改写正文段落；不要给图编号。")

    try:
        out = chat_fn(
            [{"role": "system", "content": "你是中文报告排版编辑，只输出 JSON。"},
             {"role": "user", "content": chr(10).join(lines)}],
            timeout=120, max_tokens=1200, temperature=0, thinking="disabled")
    except TypeError:
        out = chat_fn(
            [{"role": "system", "content": "你是中文报告排版编辑，只输出 JSON。"},
             {"role": "user", "content": chr(10).join(lines)}],
            timeout=120, max_tokens=1200, temperature=0)
    except Exception as exc:  # noqa: BLE001
        log.append("AI 排版失败：" + str(exc)[:120])
        return None
    text = out.get("text") if isinstance(out, dict) else str(out or "")
    try:
        left = text.find("{")
        right = text.rfind("}")
        data = json.loads(text[left:right + 1] if left >= 0 else text)
    except Exception:  # noqa: BLE001
        log.append("AI 排版不是合法 JSON，用确定性计划")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _page_break() -> str:
    return ("\n```{=openxml}\n"
            "<w:p><w:r><w:br w:type=\"page\"/></w:r></w:p>\n"
            "```\n")


def _custom(style: str, text: str) -> list[str]:
    return ['::: {custom-style="' + style + '"}', str(text), ":::", ""]


def _cover(meta: dict) -> str:
    company = str(meta.get("company") or "").strip()
    if company in ("", "—"):
        company = "经营分析"
    parts: list[str] = []
    parts += _custom("Cover Spacer", "　")
    parts += _custom("Cover Spacer", "　")
    parts += _custom("Cover Kicker", company)
    parts += _custom("Cover Title", str(meta.get("title") or "经营周报"))
    if meta.get("subtitle"):
        parts += _custom("Cover Subtitle", str(meta["subtitle"]))
    parts += _custom("Cover Rule", "　")
    rows = [
        ("报告周期", meta.get("period") or "—"),
        ("审　　核", meta.get("reviewer") or "—"),
        ("版本", meta.get("version") or "—"),
        ("交付日期", meta.get("date") or "—"),
    ]
    for key, value in rows:
        parts += _custom("Cover Meta", "　" + key + "：" + str(value))
    parts += _custom("Cover Spacer", "　")
    parts += _custom("Cover Spacer", "　")
    parts += _custom("Cover Foot", "内部资料 · 请勿外传")
    return chr(10).join(parts)


def _figure_height_cm(fig: dict, width_cm: float) -> float:
    path = fig.get("source_path") or fig.get("path")
    try:
        from PIL import Image

        with Image.open(str(path)) as img:
            width_px, height_px = img.size
        if width_px and height_px:
            return width_cm * height_px / width_px
    except Exception:  # noqa: BLE001 - 读不到尺寸就按上限估
        pass
    return width_cm * 0.5


def _figure_width_cm(fig: dict, width_key: str) -> float:
    width_cm = WIDTH_CM.get(str(width_key), WIDTH_CM["medium"])
    height_cm = _figure_height_cm(fig, width_cm)
    if height_cm > MAX_FIG_HEIGHT_CM:
        width_cm = width_cm * MAX_FIG_HEIGHT_CM / height_cm
    return round(max(5.0, min(width_cm, 11.5)), 1)


def _table_markdown(table: dict) -> list[str]:
    parts = _custom("Table Caption", str(table.get("caption") or "表"))
    headers = [str(item) for item in (table.get("headers") or [])]
    rows = table.get("rows") or []
    if not headers or not rows:
        return []
    parts.append("| " + " | ".join(headers) + " |")
    parts.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        parts.append("| " + " | ".join(str(item) for item in row) + " |")
    parts.append("")
    if table.get("note"):
        parts += _custom("Table Note", str(table["note"]))
    return parts


def _figure_block(fig: dict, width_key: str) -> list[str]:
    caption = str(fig.get("caption") or ("图 " + str(fig.get("number"))))
    width = _figure_width_cm(fig, width_key)
    return ["![" + caption + "](" + str(fig.get("path") or "") + "){width="
            + str(width) + "cm}", ""]


def render_markdown(sections: list[dict], figures: list[dict], meta: dict,
                    plan: dict, summary: str = "",
                    tables: list[dict] | None = None) -> str:
    headings = plan.get("headings") or {}
    selected = [item for item in (plan.get("figures") or []) if isinstance(item, dict)]
    selected_numbers = {int(item["number"]) for item in selected}
    breaks = {_strip_number(_clean(item)) for item in (plan.get("page_break_before") or [])}
    by_number = {int(fig["number"]): fig for fig in figures}
    sub_map: dict[int, str] = {}
    for item in plan.get("subheadings") or []:
        if isinstance(item, dict):
            try:
                sub_map[int(item.get("after"))] = _clean(item.get("text"))
            except (TypeError, ValueError):
                continue

    rows = _paragraph_index(sections)
    total = rows[-1][2] if rows else 0
    targets = _figure_targets(sections, figures, selected)
    after: dict[int, list[dict]] = {}
    for item in selected:
        number = int(item["number"])
        fig = by_number.get(number)
        if not fig:
            continue
        after.setdefault(targets.get(number) or total or 1, []).append(
            {"fig": fig, "width": item.get("width") or "medium"})

    parts = [_cover(meta), _page_break()]
    if summary:
        parts += ["# 摘要", "", str(summary), ""]
    for table in tables or []:
        parts += _table_markdown(table)
    cursor = 0
    first = True
    for index, sec in enumerate(sections):
        display = _strip_number(_display(str(sec.get("heading") or ""), headings))
        if not first and display in breaks:
            parts.append(_page_break())
        prefix = CN_NUM[index] if index < len(CN_NUM) else ""
        parts.append("# " + prefix + display)
        parts.append("")
        for block in sec.get("blocks") or []:
            cursor += 1
            text = _block_text(block)
            if isinstance(block, dict) and block.get("type") == "bullet":
                text = "- " + text
            parts.append(text)
            parts.append("")
            for item in after.get(cursor, []):
                parts += _figure_block(item["fig"], item["width"])
            if cursor in sub_map:
                parts += ["## " + sub_map[cursor], ""]
        first = False
    return chr(10).join(parts).rstrip() + chr(10)


def validate_markdown(markdown: str, sections: list[dict],
                      figures: list[dict],
                      selected: list[dict] | None = None) -> list[str]:
    """排版稿里的正文必须逐字来自输入，选中的图注必须逐字来自清单。"""
    errors: list[str] = []
    selected_numbers = ({int(item["number"]) for item in selected if isinstance(item, dict)}
                        if selected is not None else None)
    cursor = 0
    for sec in sections:
        for block in sec.get("blocks") or []:
            text = _block_text(block)
            if not text:
                continue
            found = markdown.find(text, cursor)
            if found < 0:
                errors.append("正文段落丢失或顺序变化：" + text[:20])
                break
            cursor = found + len(text)
    for fig in figures:
        if selected_numbers is not None and int(fig["number"]) not in selected_numbers:
            continue
        caption = str(fig.get("caption") or "")
        if caption and caption not in markdown:
            errors.append("图注丢失：" + caption[:20])
    for token in FORBIDDEN:
        if token in markdown:
            errors.append("排版稿含过程标记：" + token)
    return errors


def estimate_pages(sections: list[dict], plan: dict, figures: list[dict],
                   summary: str = "", tables: list[dict] | None = None) -> float:
    """按 A4、宋体小四、1.5 倍行距估算页数；只用于决策，最终以 Word 为准。"""
    rows = _paragraph_index(sections)
    chars = sum(len(text) for _, _, _, text in rows)
    body_cm = (chars / 34.0) * 0.64 + len(rows) * 0.12
    heading_cm = (len(sections) + len(plan.get("subheadings") or [])) * 1.1
    summary_cm = ((len(summary) / 34.0) * 0.64 + 1.2) if summary else 0.0
    table_cm = 0.0
    for table in tables or []:
        table_cm += 1.0 + len(table.get("rows") or []) * 0.75
    by_number = {int(fig["number"]): fig for fig in figures}
    fig_cm = 0.0
    for item in plan.get("figures") or []:
        if not isinstance(item, dict):
            continue
        fig = by_number.get(int(item.get("number")))
        if not fig:
            continue
        width = _figure_width_cm(fig, item.get("width") or "medium")
        fig_cm += min(_figure_height_cm(fig, width), MAX_FIG_HEIGHT_CM) + 1.2
    total_cm = 24.6 + body_cm + heading_cm + summary_cm + table_cm + fig_cm
    return round(total_cm / 24.6, 2)


def plan_layout(sections: list[dict], figures: list[dict], meta: dict, *,
                headings: dict | None = None, use_ai: bool = True,
                chat_fn=None, log: list[str] | None = None,
                summary: str = "", tables: list[dict] | None = None) -> dict:
    """返回 {plan, markdown, source, errors, pages_estimate}；失败时 markdown 为空。"""
    log = log if log is not None else []
    base = deterministic_plan(sections, figures, headings)
    source = "deterministic"
    plan = base
    if use_ai:
        candidate = ai_plan(sections, figures, meta, chat_fn, log)
        if candidate:
            errors = validate_plan(sections, figures, candidate)
            if errors:
                log.append("AI 排版计划不合法，用确定性计划：" + "；".join(errors[:4]))
            else:
                plan = candidate
                source = "ai"
    errors = validate_plan(sections, figures, plan)
    if errors:
        return {"plan": base, "markdown": "", "source": "invalid",
                "errors": errors, "pages_estimate": 0.0}

    def selected_numbers(current: dict) -> set[int]:
        return {int(item["number"]) for item in current.get("figures") or []
                if isinstance(item, dict)}

    display_sections = clean_sections(sections, selected_numbers(plan))
    pages = estimate_pages(display_sections, plan, figures, summary, tables)
    if pages > TARGET_PAGES and len(plan.get("figures") or []) > 2:
        log.append("预估 " + str(pages) + " 页，超过 " + str(TARGET_PAGES)
                   + " 页，退回 2 图确定性计划")
        plan = deterministic_plan(sections, figures, headings, limit=2)
        source = "deterministic"
        display_sections = clean_sections(sections, selected_numbers(plan))
        pages = estimate_pages(display_sections, plan, figures, summary, tables)
    if plan.get("page_break_before") and pages < 4.2:
        log.append("预估 " + str(pages) + " 页，取消强制分页，避免整页留白")
        plan = dict(plan)
        plan["page_break_before"] = []
        pages = estimate_pages(display_sections, plan, figures, summary, tables)
    display_figures, display_plan, _ = _renumber_for_display(
        display_sections, figures, plan)
    markdown = render_markdown(display_sections, display_figures, meta, display_plan,
                               summary, tables)
    errors = validate_markdown(markdown, display_sections, display_figures,
                               display_plan.get("figures") or [])
    if errors:
        return {"plan": base, "markdown": "", "source": "invalid",
                "errors": errors, "pages_estimate": pages}
    return {"plan": display_plan, "figures": display_figures, "markdown": markdown,
            "source": source, "errors": [], "pages_estimate": pages}
