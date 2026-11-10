#!/usr/bin/env python3
"""text_stats —— 把「规范.md」里能数的项数出来，供 S5 统计与审计取证。

只做确定性的计数与扫描，不做判断、不改正文：
  - 正文字数（按规范剔除图表题注与附录）
  - 摘要字数与句数
  - 每节字数、段数
  - 禁用词（引用块内豁免）
  - 未替换的 [[f:键名]] 占位符
  - 正文数字：写作稿查「裸数字」（题注、表头豁免），终稿查「无来源数字」
    （每个数字都要能在台账 display 里找到；日期、图号引用豁免）
  - 图号连续性、题注有无、正文引用是否齐全
  - 页数推算（按规范给出的换算，标注为推算值）

两个阶段分开判，规则见 规范.md 第六节：

    --stage draft   S5 写作稿：度量值都是 [[f:…]]，裸数字必须为 0
    --stage final   S5 终稿：占位符必须为 0，数字必须能在台账里找到来源

用法：
    python tools/text_stats.py draft.md --spec 规范.md --stage draft --json
    python tools/text_stats.py draft.md --spec 规范.md --stage final --ledger ledger.json --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\[\[f:([^\]]+)\]\]")
FIGURE_CAPTION_RE = re.compile(r"^\s*(?:图|表)\s*(\d+)\s*[、.．:：\s]")
FIGURE_REF_RE = re.compile(r"图\s*(\d+)")
SENTENCE_ENDS = "。！？；!?;"
TABLE_ROW_RE = re.compile(r"^\s*\|")
NUM_TOKEN_RE = re.compile(r"-?\d+(?:\.\d+)?")
# 千分位逗号：只抹掉夹在数字中间的那些，避免 177,306.00 被拆成 177 和 306.00
THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# 终稿里不算「数字」的东西：日期、周次、章节序号、图表引用
NON_METRIC_PATTERNS = [
    re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"),
    re.compile(r"\d{4}\s*W\d{1,2}"),
    re.compile(r"W\d{1,2}"),
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日"),
    re.compile(r"第\s*\d+\s*[章节条页天周]"),
    re.compile(r"[图表]\s*\d+"),
    re.compile(r"\d{4}\s*年"),
    re.compile(r"\d{1,2}\s*月"),
    re.compile(r"\d{1,2}\s*日"),
]


def mask_non_metric(line: str) -> str:
    """把日期、周次、图表引用抹掉，剩下的数字才算度量值。"""
    out = line
    for pat in NON_METRIC_PATTERNS:
        out = pat.sub(" ", out)
    return out


def number_is_sourced(value: float, numbers: set[float]) -> bool:
    """终稿里的数字是否来自台账。

    精确等值太脆：台账 display 写「33.333%」而正文按一位小数写「33.3%」，
    精确比对会把合规的正文判成无来源。这里放宽到「同量级四舍五入后一致」：
    差值在 0.005 以内，或相对误差在千分之一以内，都算同一个数。
    """
    if not numbers:
        return False
    if value in numbers:
        return True
    nearest = min(numbers, key=lambda x: abs(x - value))
    diff = abs(nearest - value)
    return diff <= 0.005 or diff <= abs(nearest) * 1e-3


def load_ledger_displays(path: str | Path) -> dict[str, str]:
    """key → display，用于把写作稿的占位符折算成终稿字数。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("entries") or data.get("items") or [] if isinstance(data, dict) else data
    out: dict[str, str] = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("key"):
            out[str(item["key"])] = str(item.get("display", "") or "")
    return out


def load_ledger_numbers(path: str | Path) -> set[float]:
    """从台账里取全部可比数值（display / value / ref），供终稿溯源。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        items = (data.get("entries") or data.get("items")
                 or data.get("ledger") or data.get("results") or [])
    elif isinstance(data, list):
        items = data
    else:
        items = []
    numbers: set[float] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in ("display", "value", "ref"):
            raw = item.get(field)
            if raw is None or isinstance(raw, bool):
                continue
            text = str(raw).replace(",", "").replace("，", "").replace("−", "-")
            for token in NUM_TOKEN_RE.findall(text):
                try:
                    numbers.add(round(float(token), 6))
                except ValueError:
                    continue
    return numbers

DEFAULT_BANNED = [
    "大约", "左右", "基本", "基本上", "显著", "大幅", "有所",
    "一定程度上", "应该是", "众所周知", "众所周知地", "据不完全统计",
]
# 规范.md 给了换算口径：A4、正文小四、1.5 倍行距下每页约 450 字，
# 一张整宽图（含题注）约占 0.45 页。
CHARS_PER_PAGE = 450
PAGES_PER_FIGURE = 0.45


def load_banned(spec_path: Path | None) -> list[str]:
    """从规范.md 第五节取禁用词；取不到就用内置默认表。"""
    if spec_path is None or not spec_path.exists():
        return list(DEFAULT_BANNED)
    text = spec_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## 五、")), None)
    if start is None:
        return list(DEFAULT_BANNED)
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("##"):
            break
        if stripped.startswith("-") or stripped.startswith("唯一例外"):
            continue
        words = [w.strip() for w in stripped.split("、") if w.strip()]
        if len(words) >= 3:
            return words
    return list(DEFAULT_BANNED)


def _table_data_rows(lines: list[str]) -> set[int]:
    """标记 markdown 表格的数据行（表头行与分隔行不算）。"""
    rows: set[int] = set()
    in_table = False
    for no, line in enumerate(lines, 1):
        if not TABLE_ROW_RE.match(line):
            in_table = False
            continue
        if not in_table:
            in_table = True           # 第一行是表头
            continue
        if re.match(r"^\s*\|[\s\-:|]+\|?\s*$", line):
            continue                  # 分隔行
        rows.add(no)
    return rows


def _section_chars(text: str, name: str) -> int:
    """按 ## 取某一节，返回剔除题注与表格行后的字数（用于渲染后统计）。"""
    lines = text.splitlines()
    grab = False
    buf: list[str] = []
    for line in lines:
        if line.startswith("## "):
            grab = name in line[3:]
            continue
        if grab and not _is_exempt_line(line):
            buf.append(line)
    return _count_chars(re.sub(r"\s", "", "\n".join(buf)))


def _body_section_lines(lines: list[str]) -> list[list[str]]:
    """按 ## 切节并剔除附录，返回各正文节的行。"""
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    title = ""
    for line in lines:
        if line.startswith("## "):
            title = line[3:].strip()
            cur = []
            if not title.startswith("附录"):
                blocks.append(cur)
            continue
        if cur is not None and not title.startswith("附录"):
            cur.append(line)
    return blocks


def _is_exempt_line(line: str) -> bool:
    """题注行与表格行不计入正文，也不参与裸数字扫描。"""
    return bool(FIGURE_CAPTION_RE.match(line)) or bool(TABLE_ROW_RE.match(line))


def _count_chars(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def _count_sentences(text: str) -> int:
    n = sum(text.count(ch) for ch in SENTENCE_ENDS)
    tail = text.strip()
    if tail and tail[-1] not in SENTENCE_ENDS:
        n += 1
    return n


def analyze(text: str, banned: list[str] | None = None, *,
            stage: str = "draft",
            ledger_numbers: set[float] | None = None,
            ledger_path: str = "",
            ledger_displays: dict[str, str] | None = None) -> dict:
    """stage=draft 判裸数字；stage=final 判无来源数字（需 ledger_numbers）。"""
    if stage not in ("draft", "final"):
        raise ValueError(f"stage 只能是 draft 或 final，收到：{stage}")
    banned = banned if banned is not None else list(DEFAULT_BANNED)
    lines = text.splitlines()

    sections: list[dict] = []
    current: dict | None = None
    for line in lines:
        if line.startswith("## "):
            current = {"title": line[3:].strip(), "chars": 0, "paras": 0,
                       "sentences": 0, "lines": []}
            sections.append(current)
            continue
        if current is None:
            continue
        current["lines"].append(line)

    for sec in sections:
        body = []
        for line in sec["lines"]:
            if _is_exempt_line(line):
                continue
            body.append(line)
        joined = "\n".join(body)
        sec["chars"] = _count_chars(joined)
        sec["sentences"] = _count_sentences(re.sub(r"\s+", "", joined))
        sec["paras"] = len([b for b in re.split(r"\n\s*\n", joined) if b.strip()])
        sec.pop("lines", None)

    appendix_idx = [i for i, s in enumerate(sections) if s["title"].startswith("附录")]
    body_sections = [s for i, s in enumerate(sections) if i not in appendix_idx]
    chars_body = sum(s["chars"] for s in body_sections)

    abstract = next((s for s in sections if "摘要" in s["title"]), None)

    placeholders = PLACEHOLDER_RE.findall(text)
    keys = sorted(set(placeholders))

    banned_hits = []
    for no, line in enumerate(lines, 1):
        in_quote = line.lstrip().startswith(">")
        for word in banned:
            if word in line:
                banned_hits.append({"word": word, "line": no, "in_quote": in_quote,
                                    "text": line.strip()[:80]})
    banned_outside = [h for h in banned_hits if not h["in_quote"]]

    bare: list[dict] = []
    unmatched: list[dict] = []
    if stage == "draft":
        for no, line in enumerate(lines, 1):
            if _is_exempt_line(line) or line.lstrip().startswith(">") or line.startswith("#"):
                continue
            # 先把占位符、日期、图号抹掉再找数字：
            # 一行里既有占位符又有裸数字，也不能放过
            probe = mask_non_metric(PLACEHOLDER_RE.sub("", line.replace("−", "-")))
            if not re.search(r"\d", probe):
                continue
            bare.append({"line": no, "text": line.strip()[:80]})
    else:
        table_rows = _table_data_rows(lines)
        for no, line in enumerate(lines, 1):
            if line.lstrip().startswith(">") or line.startswith("#"):
                continue
            if _is_exempt_line(line) and no not in table_rows:
                continue
            stripped = THOUSANDS_RE.sub("", line.replace("−", "-"))
            rest = mask_non_metric(PLACEHOLDER_RE.sub(" ", stripped))
            for token in NUM_TOKEN_RE.findall(rest):
                try:
                    value = round(float(token), 6)
                except ValueError:
                    continue
                if ledger_numbers is None or not number_is_sourced(value, ledger_numbers):
                    unmatched.append({"line": no, "token": token,
                                      "text": line.strip()[:80]})

    captions = []
    for line in lines:
        m = FIGURE_CAPTION_RE.match(line)
        if m:
            captions.append(int(m.group(1)))
    body_text = "\n".join(
        ln for ln in lines if not FIGURE_CAPTION_RE.match(ln))
    refs = sorted({int(n) for n in FIGURE_REF_RE.findall(body_text)})
    figure_numbers = sorted(set(captions))
    max_no = max(figure_numbers) if figure_numbers else 0
    missing_no = [n for n in range(1, max_no + 1) if n not in figure_numbers]

    # 写作稿里的 [[f:…]] 有二十来个字符，直接数字数会虚高；按台账 display 折算一次，
    # 得到的才是成稿后的真实字数
    rendered = text
    if ledger_displays:
        rendered = PLACEHOLDER_RE.sub(lambda m: ledger_displays.get(m.group(1), m.group(1)), text)
    chars_body_rendered = sum(
        _count_chars(re.sub(r"\s", "", "\n".join(
            ln for ln in sec_lines if not _is_exempt_line(ln))))
        for sec_lines in _body_section_lines(rendered.splitlines()))

    pages = chars_body_rendered / CHARS_PER_PAGE + len(figure_numbers) * PAGES_PER_FIGURE

    warnings = []
    if not sections:
        warnings.append("没有识别到 ## 级章节标题，检查正文格式")
    if not figure_numbers:
        warnings.append("没有识别到图题注（形如「图 1 …」）")

    return {
        "sections": sections,
        "section_count": len(sections),
        "chars_body": chars_body,
        "chars_body_rendered": chars_body_rendered if ledger_displays else chars_body,
        "chars_total": _count_chars(text),
        "abstract": {
            "found": abstract is not None,
            "chars": abstract["chars"] if abstract else 0,
            "chars_rendered": _section_chars(rendered, "摘要") if ledger_displays
                              else (abstract["chars"] if abstract else 0),
            "sentences": abstract["sentences"] if abstract else 0,
        },
        "figures": {
            "captions": figure_numbers,
            "cited_in_body": refs,
            "missing_numbers": missing_no,
            "never_cited": [n for n in figure_numbers if n not in refs],
            "types_note": "图型种数需人工确认，本工具不判",
        },
        "stage": stage,
        "placeholders": {"total": len(placeholders), "keys": keys},
        "banned": {"list_size": len(banned), "outside_quote": banned_outside,
                   "inside_quote": [h for h in banned_hits if h["in_quote"]]},
        "bare_numbers": {"count": len(bare), "hits": bare,
                         "applies": stage == "draft"},
        "traceability": {
            "ledger": ledger_path,
            "ledger_numbers": len(ledger_numbers) if ledger_numbers is not None else 0,
            "unmatched": unmatched,
            "applies": stage == "final",
        },
        "pages_estimate": {
            "value": round(pages, 1),
            "rule": f"正文字数/{CHARS_PER_PAGE} + 图数*{PAGES_PER_FIGURE}（规范.md 第一节换算口径）",
            "is_estimate": True,
        },
        "warnings": warnings,
    }


def _cli() -> int:
    p = argparse.ArgumentParser(description="周报正文的确定性计数与扫描")
    p.add_argument("path", help="正文文件（md/txt）")
    p.add_argument("--spec", help="规范.md 路径，用于取禁用词")
    p.add_argument("--json", action="store_true", help="输出 JSON（默认也是 JSON）")
    p.add_argument("--stage", choices=["draft", "final"], default="draft",
                   help="draft=写作稿查裸数字；final=终稿查无来源数字")
    p.add_argument("--ledger", help="台账 JSON；final 阶段必填，用于查数字来源")
    args = p.parse_args()
    if args.stage == "final" and not args.ledger:
        print("final 阶段必须给 --ledger，否则无法判断数字有没有来源", file=sys.stderr)
        return 2

    path = Path(args.path)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8")
    ledger_numbers = None
    ledger_displays = None
    if args.ledger:
        ledger_path = Path(args.ledger)
        if not ledger_path.exists():
            print(f"台账不存在：{ledger_path}", file=sys.stderr)
            return 2
        ledger_numbers = load_ledger_numbers(ledger_path)
        ledger_displays = load_ledger_displays(ledger_path)
    result = analyze(text, load_banned(Path(args.spec) if args.spec else None),
                     stage=args.stage, ledger_numbers=ledger_numbers,
                     ledger_path=args.ledger or "", ledger_displays=ledger_displays)
    result["file"] = str(path)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_cli())
