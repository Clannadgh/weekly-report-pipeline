"""周期解析与 ISO 周标识。

只负责把「2026-10-26 至 2026-11-01」这类文本还原成起止日期，以及周次标签与日期平移。
不做业务判断，也不猜「上周」这类相对说法——猜不出就返回 None。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# 2026-10-26 / 2026/10/26 / 2026.10.26 / 2026年10月26日
_YEAR_DATE = re.compile(
    r"(?P<y>\d{4})\s*[年./\-]\s*(?P<m>\d{1,2})\s*[月./\-]\s*(?P<d>\d{1,2})\s*日?"
)
# 10月26日（缺年份）
_MONTH_DAY = re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?")

_CONNECTOR = re.compile(r"^\s*(?:至|到|—|–|－|~|～|-|to|until)\s*$", re.I)

_MAX_SPAN_DAYS = 400


class _Token:
    __slots__ = ("start", "end", "year", "month", "day")

    def __init__(self, start: int, end: int, year: int | None, month: int, day: int) -> None:
        self.start = start
        self.end = end
        self.year = year
        self.month = month
        self.day = day


def parse_period(text: str, fallback_year: int | None = None) -> tuple[date, date] | None:
    """从文本中解析起止日期，解析不出返回 None。

    支持 2026-10-26 至 2026-11-01、2026/10/26-2026/11/01、2026年10月26日至11月1日、
    10月26日至11月1日（缺年份用 fallback_year）。连接符支持 至/到/-/—/~/～。
    """
    if not text:
        return None

    tokens = _tokenize(text)
    if len(tokens) < 2:
        return None

    best: tuple[int, tuple[date, date]] | None = None
    for left, right in zip(tokens, tokens[1:]):
        if not _CONNECTOR.match(text[left.end:right.start]):
            continue
        span = _build(left, right, fallback_year)
        if span is None:
            continue
        score = int(left.year is not None) + int(right.year is not None)
        if best is None or score > best[0]:
            best = (score, span)
    return best[1] if best else None


def iso_week_tag(d: date) -> str:
    """2026-10-26 -> w44。"""
    return f"w{d.isocalendar()[1]:02d}"


def week_label(d: date) -> str:
    """2026-10-26 -> 2026-W44。"""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def shift_days(d: date, n: int) -> date:
    """按天平移。"""
    return d + timedelta(days=n)


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    taken: list[tuple[int, int]] = []

    for match in _YEAR_DATE.finditer(text):
        token = _Token(match.start(), match.end(), int(match.group("y")),
                       int(match.group("m")), int(match.group("d")))
        if not _valid(token):
            continue
        tokens.append(token)
        taken.append((token.start, token.end))

    for match in _MONTH_DAY.finditer(text):
        start, end = match.start(), match.end()
        if any(s < end and start < e for s, e in taken):
            continue
        token = _Token(start, end, None, int(match.group("m")), int(match.group("d")))
        if not _valid(token):
            continue
        tokens.append(token)

    tokens.sort(key=lambda item: item.start)
    return tokens


def _valid(token: _Token) -> bool:
    try:
        date(token.year or 2000, token.month, token.day)
    except ValueError:
        return False
    return True


def _build(left: _Token, right: _Token, fallback_year: int | None) -> tuple[date, date] | None:
    if left.year is not None:
        start_year = left.year
    elif right.year is not None:
        start_year = right.year
    elif fallback_year is not None:
        start_year = fallback_year
    else:
        return None

    if right.year is not None:
        end_year = right.year
    else:
        end_year = start_year
        if (right.month, right.day) < (left.month, left.day):
            end_year += 1

    if left.year is None and right.year is not None:
        if (left.month, left.day) > (right.month, right.day):
            start_year -= 1

    try:
        start = date(start_year, left.month, left.day)
        end = date(end_year, right.month, right.day)
    except ValueError:
        return None

    if end < start or (end - start).days > _MAX_SPAN_DAYS:
        return None
    return start, end

def parse_year(text: str) -> int | None:
    """从文本里找四位年份；找不到返回 None，不猜。"""
    match = re.search(r"(?<!\d)(20\d{2})(?!\d)", text or "")
    return int(match.group(1)) if match else None

_WEEK_PATTERNS = (
    re.compile(r"(?:(?P<y>20\d{2})\s*年?\s*)?(?:第\s*)?(?P<w>\d{1,2})\s*周"),
    re.compile(r"(?:(?P<y>20\d{2})\s*[-_/]?\s*)?[Ww](?P<w>\d{1,2})(?!\d)"),
)


def parse_week(text: str, fallback_year: int | None = None) -> tuple[date, date] | None:
    """从「2026 年第 42 周」「W42」这类写法推出该 ISO 周的周一到周日。"""
    for pattern in _WEEK_PATTERNS:
        for match in pattern.finditer(text or ""):
            week = int(match.group("w"))
            year = int(match.group("y")) if match.group("y") else fallback_year
            if not year or not 1 <= week <= 53:
                continue
            try:
                start = date.fromisocalendar(year, week, 1)
            except ValueError:
                continue
            return start, start + timedelta(days=6)
    return None
