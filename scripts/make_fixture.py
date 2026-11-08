#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 S1 准入检查用的固定夹具。

产物（默认写入 fixtures/w44/）：
  sales.csv          两周门店日销售明细：5 店 × 14 天 × 2 行 - 2 行缺数 = 138 行
                     含 1 处重复记录ID、SZ02 于 2026-10-28 缺数、3 条退货负数行
  minutes.md         中文经营分析会纪要（本周 2026-10-26 至 2026-11-01）
  minutes_second.md  第二份纪要（周期相同，含数据里不存在的门店 SZ09）
  broken.csv         不可读文件（非 UTF-8 二进制乱码 + 列数不齐）

全部内容由固定常量推导，不使用随机数，重复运行结果逐字节一致。
用法：python scripts/make_fixture.py [--out DIR]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "fixtures" / "w44"

COLUMNS = [
    "记录ID", "销售日期", "门店编码", "门店名称", "区域", "品类",
    "商品名称", "销售数量", "实收金额", "成本金额", "目标金额", "渠道",
]

# 编码, 名称, 区域, 上周（2026-10-19..10-25）实收基数
STORES = [
    ("HZ01", "杭州西湖店", "华东", 186_000),
    ("HZ02", "杭州滨江店", "华东", 142_000),
    ("SZ01", "深圳福田店", "华南", 168_000),
    ("SZ02", "深圳南山店", "华南", 121_000),
    ("GZ01", "广州天河店", "华南", 155_000),
]

CATEGORIES = ("生鲜", "日化", "酒饮")
CATEGORY_COST = {"生鲜": 0.72, "日化": 0.62, "酒饮": 0.55}
CATEGORY_PRICE = {"生鲜": 36, "日化": 68, "酒饮": 128}
PRODUCTS = {
    "生鲜": ("鲜牛奶1L", "有机蔬菜拼盘", "冷鲜牛肉500g"),
    "日化": ("洗衣液2kg", "抽取式面巾纸10包", "洗手液500ml"),
    "酒饮": ("精酿啤酒6罐", "干红葡萄酒750ml", "气泡水12瓶"),
}

WEEK1_START = date(2026, 10, 19)
WEEK2_START = date(2026, 10, 26)
DAYS = 14
DAY_WEIGHTS = (10, 9, 10, 9, 12, 13, 14)  # 周一..周日

REGION_GROWTH = {"华东": 0.04, "华南": -0.12}
TARGET_FACTOR = {"华东": 0.97, "华南": 1.05}

MISSING = {("SZ02", date(2026, 10, 28))}          # 缺数
DUPLICATE_DAY = ("HZ02", date(2026, 10, 29))      # 两行内容与 ID 完全相同
RETURNS = {
    ("SZ01", date(2026, 10, 21)): 1,
    ("GZ01", date(2026, 10, 24)): 0,
    ("HZ01", date(2026, 10, 30)): 1,
}

BROKEN_BYTES = (
    b"\xff\xfe\x00\x00BROKEN\x00\x01\x02\x03\n"
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\n"
    b"A,B,C\n"
    b"1,2\n"
    b"4,5,6,7,8\n"
)

NL = chr(10)


def _largest_remainder(total: int, weights: list[int]) -> list[int]:
    """把整数 total 按整数权重切成和恰好等于 total 的整数份额。"""
    s = sum(weights)
    raw = [total * w / s for w in weights]
    out = [int(x) for x in raw]
    rest = total - sum(out)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - out[i]), i))
    for i in order[:rest]:
        out[i] += 1
    return out


def _dates(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _plan() -> dict[str, dict[date, int]]:
    """每店每天的目标实收。区域周合计精确落在 +4% / -12%。"""
    w1_days = _dates(WEEK1_START, 7)
    w2_days = _dates(WEEK2_START, 7)

    region_w1: dict[str, int] = {}
    for _code, _name, region, base in STORES:
        region_w1[region] = region_w1.get(region, 0) + base

    plan: dict[str, dict[date, int]] = {}
    for region, growth in REGION_GROWTH.items():
        members = [s for s in STORES if s[2] == region]
        region_w2 = round(region_w1[region] * (1 + growth))
        w2_by_store = _largest_remainder(region_w2, [m[3] for m in members])
        for (code, _name, _region, base), week2_total in zip(members, w2_by_store):
            w1_split = _largest_remainder(base, [DAY_WEIGHTS[d.weekday()] for d in w1_days])
            present = [d for d in w2_days if (code, d) not in MISSING]
            w2_split = _largest_remainder(
                week2_total, [DAY_WEIGHTS[d.weekday()] for d in present]
            )
            days = dict(zip(w1_days, w1_split))
            days.update(dict(zip(present, w2_split)))
            plan[code] = days
    return plan


def _enforce_even(plan: dict[str, dict[date, int]]) -> None:
    """重复行要求两行等额，该日合计须为偶数；差额 1 元补给同周另一天，店周合计不变。"""
    code, day = DUPLICATE_DAY
    amount = plan[code][day]
    if amount % 2 == 0:
        return
    plan[code][day] = amount - 1
    same_week = [d for d in sorted(plan[code])
                 if d != day and (d < WEEK2_START) == (day < WEEK2_START)]
    if same_week:
        plan[code][same_week[0]] += 1


def _day_rows(si: int, di: int, code: str, name: str, region: str,
              day: date, amount: int) -> list[dict]:
    duplicate = (code, day) == DUPLICATE_DAY

    cat_a = CATEGORIES[(si + di) % 3]
    cat_b = cat_a if duplicate else CATEGORIES[(si + di + 1) % 3]
    prod_a = PRODUCTS[cat_a][(si + di) % len(PRODUCTS[cat_a])]
    prod_b = prod_a if duplicate else PRODUCTS[cat_b][(si + di + 1) % len(PRODUCTS[cat_b])]

    if duplicate:
        half = amount // 2
        parts = [half, amount - half]
    else:
        ratio = (0.55, 0.60, 0.45)[(si * 3 + di) % 3]
        first = round(amount * ratio)
        parts = [first, amount - first]

    ret_idx = RETURNS.get((code, day))
    if ret_idx is not None:
        refund = -max(120, round(amount * 0.03))
        parts = [amount - refund, refund] if ret_idx == 1 else [refund, amount - refund]

    channels = ("线下", "线下") if duplicate else ("线下", "线上")
    rows = []
    for cat, prod, channel, amt in zip((cat_a, cat_b), (prod_a, prod_b), channels, parts):
        qty = max(1, round(abs(amt) / CATEGORY_PRICE[cat]))
        rows.append({
            "记录ID": "",
            "销售日期": day.isoformat(),
            "门店编码": code,
            "门店名称": name,
            "区域": region,
            "品类": cat,
            "商品名称": prod,
            "销售数量": -qty if amt < 0 else qty,
            "实收金额": amt,
            "成本金额": round(amt * CATEGORY_COST[cat]),
            "目标金额": 0 if amt < 0 else round(amt * TARGET_FACTOR[region]),
            "渠道": channel,
        })
    return rows


def _rows(plan: dict[str, dict[date, int]]) -> list[dict]:
    rows: list[dict] = []
    for si, (code, name, region, _base) in enumerate(STORES):
        for di, day in enumerate(_dates(WEEK1_START, DAYS)):
            amount = plan[code].get(day)
            if amount is None:
                continue
            rows.extend(_day_rows(si, di, code, name, region, day, amount))
    return _assign_ids(rows)


def _assign_ids(rows: list[dict]) -> list[dict]:
    """顺序编号；重复日的两行共用同一个 ID。"""
    seq = 0
    dup_key = (DUPLICATE_DAY[0], DUPLICATE_DAY[1].isoformat())
    last_key: tuple[str, str] | None = None
    last_id = ""
    for row in rows:
        key = (row["门店编码"], row["销售日期"])
        if key == dup_key and key == last_key:
            row["记录ID"] = last_id
        else:
            seq += 1
            row["记录ID"] = "D%04d" % seq
        last_key, last_id = key, row["记录ID"]
    return rows


def _in_week(day: str, start: date) -> bool:
    d = date.fromisoformat(day)
    return start <= d < start + timedelta(days=7)


def _stats(rows: list[dict]) -> dict:
    def total(pred) -> int:
        return sum(r["实收金额"] for r in rows if pred(r))

    hd_w1 = total(lambda r: r["区域"] == "华东" and _in_week(r["销售日期"], WEEK1_START))
    hd_w2 = total(lambda r: r["区域"] == "华东" and _in_week(r["销售日期"], WEEK2_START))
    hn_w1 = total(lambda r: r["区域"] == "华南" and _in_week(r["销售日期"], WEEK1_START))
    hn_w2 = total(lambda r: r["区域"] == "华南" and _in_week(r["销售日期"], WEEK2_START))
    all_w2 = total(lambda r: _in_week(r["销售日期"], WEEK2_START))

    def cat_total(cat: str, week_start: date) -> int:
        return total(lambda r: r["品类"] == cat and _in_week(r["销售日期"], week_start))

    fresh_w2 = cat_total("生鲜", WEEK2_START)
    daily_w1 = cat_total("日化", WEEK1_START)
    daily_w2 = cat_total("日化", WEEK2_START)

    rev2 = all_w2
    cost2 = sum(r["成本金额"] for r in rows if _in_week(r["销售日期"], WEEK2_START))
    gm = (rev2 - cost2) / rev2 * 100 if rev2 else 0.0

    negatives = [r for r in rows if r["实收金额"] < 0]
    ids = [r["记录ID"] for r in rows]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})

    return {
        "rows": len(rows),
        "unique_ids": len(set(ids)),
        "dup_ids": dup_ids,
        "first_date": min(r["销售日期"] for r in rows),
        "last_date": max(r["销售日期"] for r in rows),
        "distinct_dates": len({r["销售日期"] for r in rows}),
        "stores": sorted({r["门店编码"] for r in rows}),
        "categories": sorted({r["品类"] for r in rows}),
        "negatives": len(negatives),
        "negative_amount": sum(r["实收金额"] for r in negatives),
        "hd_w1": hd_w1, "hd_w2": hd_w2,
        "hd_chg": (hd_w2 / hd_w1 - 1) * 100,
        "hn_w1": hn_w1, "hn_w2": hn_w2,
        "hn_chg": (hn_w2 / hn_w1 - 1) * 100,
        "all_w2": all_w2,
        "fresh_w2": fresh_w2,
        "fresh_share": fresh_w2 / all_w2 * 100,
        "daily_w2": daily_w2,
        "daily_chg": (daily_w2 / daily_w1 - 1) * 100 if daily_w1 else 0.0,
        "gm": gm,
    }


def _verify(rows: list[dict]) -> dict:
    """夹具硬约束，不满足直接报错，避免产出假夹具。"""
    def fail(msg: str) -> None:
        raise ValueError("夹具自检失败：" + msg)

    if list(rows[0].keys()) != COLUMNS:
        fail("列顺序与契约不一致")
    if len(rows) != 5 * 14 * 2 - 2:
        fail("行数应为 138，实际 %d" % len(rows))
    if {r["区域"] for r in rows} != {"华东", "华南"}:
        fail("区域取值不符")
    if {r["品类"] for r in rows} != set(CATEGORIES):
        fail("品类取值不符")
    if [r for r in rows if r["门店编码"] == "SZ02" and r["销售日期"] == "2026-10-28"]:
        fail("SZ02 在 2026-10-28 不应有记录")
    if sum(1 for r in rows if r["实收金额"] < 0) < 2:
        fail("负数行少于 2 条")

    stats = _stats(rows)
    if stats["first_date"] != "2026-10-19" or stats["last_date"] != "2026-11-01":
        fail("日期范围应为 2026-10-19 至 2026-11-01")
    if stats["distinct_dates"] != 14:
        fail("应覆盖 14 个自然日")
    if len(stats["dup_ids"]) != 1:
        fail("应恰好有 1 个重复记录ID，实际 %s" % stats["dup_ids"])
    dup_id = stats["dup_ids"][0]
    pair = [r for r in rows if r["记录ID"] == dup_id]
    if len(pair) != 2:
        fail("重复ID 应出现 2 行")
    left = {k: v for k, v in pair[0].items() if k != "记录ID"}
    right = {k: v for k, v in pair[1].items() if k != "记录ID"}
    if left != right:
        fail("重复ID 的两行内容应完全一致")
    if abs(stats["hn_chg"] + 12) > 0.5:
        fail("华南周环比应约为 -12%%，实际 %.2f%%" % stats["hn_chg"])
    if abs(stats["hd_chg"] - 4) > 0.5:
        fail("华东周环比应约为 +4%%，实际 %.2f%%" % stats["hd_chg"])
    if stats["daily_chg"] >= 0:
        fail("日化应为下降，供「方向说反」的结论使用")
    if not 0 < stats["gm"] < 100:
        fail("毛利率应在 0-100 之间")
    return stats


def _pct(x: float) -> str:
    return "%.1f%%" % x


def _yuan(x: int) -> str:
    return "{:,}".format(int(x))


def _round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def _minutes_text(s: dict) -> str:
    wrong_hd = _round_to(s["hd_w2"] * 1.09, 100)
    return """# 华东与华南区域经营分析会纪要

- 会议时间：2026 年 11 月 2 日（周一）09:30-10:40
- 会议形式：线上会议
- 主持人：周宁
- 参会：区域运营、品类采购、数据组

## 一、统计周期

本周统计周期为 2026-10-26 至 2026-11-01，对比周为 2026-10-19 至 2026-10-25。

## 二、本周结论

1. 华南区域本周实收 {hn_w2} 元，较上周 {hn_w1} 元下降 {hn_chg}，是本次降幅最大的区域。
2. 生鲜品类本周实收 {fresh} 元，占全司实收 {share}。
3. 华东区域本周实收 {wrong} 元，较上周上升 {hd_chg}。
4. 日化品类本周实收 {daily} 元，较上周上升 6.8%。

## 三、口径要求

周报核心指标表须覆盖实收、销售数量、毛利率、目标达成率四项，金额单位统一为元。
毛利率按（实收金额 - 成本金额）÷ 实收金额 计算；目标达成率按实收金额 ÷ 目标金额 计算。

## 四、门店与目标

SZ02（深圳南山店）本周目标 17.7 万元，10 月 28 日没有销售记录，缺口需要在下一周期追平。

## 五、下一阶段

下周（2026-11-02 至 2026-11-08）启动双十一预热，酒饮与日化优先备货，具体方案另行下发。

## 六、行动项

- 数据组李敏于 2026-11-04 前补齐 SZ02 在 2026-10-28 缺失的销售明细，并回传核对结果。
- 区域运营方舟于 2026-11-06 前完成华东两家门店与华南三家门店的陈列检查。
""".format(
        hn_w2=_yuan(s["hn_w2"]),
        hn_w1=_yuan(s["hn_w1"]),
        hn_chg=_pct(abs(s["hn_chg"])),
        fresh=_yuan(s["fresh_w2"]),
        share=_pct(s["fresh_share"]),
        wrong=_yuan(wrong_hd),
        hd_chg=_pct(s["hd_chg"]),
        daily=_yuan(s["daily_w2"]),
    )


def _minutes_second_text(s: dict) -> str:
    return """# 区域经营分析会纪要（补充记录）

统计周期：2026-10-26 至 2026-11-01

- 记录人：数据组
- 会议形式：线下补充讨论

## 结论

1. 华东区域本周实收 {hd_w2} 元，较上周上升 {hd_chg}。
2. 门店 SZ09（深圳宝安店）本周实收 62,480 元，较上周上升 9.1%，为华南增长最快的门店。
3. 酒饮品类本周实收 96,300 元，较上周下降 4.2%。

## 行动项

- 区域运营方舟于 2026-11-05 前核实 SZ09 的归属区域与编码口径。
""".format(hd_w2=_yuan(s["hd_w2"]), hd_chg=_pct(s["hd_chg"]))


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator=NL)
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(outdir: Path) -> dict:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    plan = _plan()
    _enforce_even(plan)
    rows = _rows(plan)
    info = _verify(rows)

    _write_csv(outdir / "sales.csv", rows)
    (outdir / "minutes.md").write_text(_minutes_text(info), encoding="utf-8", newline="")
    (outdir / "minutes_second.md").write_text(_minutes_second_text(info), encoding="utf-8", newline="")
    (outdir / "broken.csv").write_bytes(BROKEN_BYTES)

    return {
        "outdir": str(outdir),
        "sales_csv": {
            "rows": info["rows"],
            "unique_ids": info["unique_ids"],
            "duplicate_ids": info["dup_ids"],
            "date_range": info["first_date"] + " 至 " + info["last_date"],
            "distinct_dates": info["distinct_dates"],
            "stores": info["stores"],
            "categories": info["categories"],
            "negative_rows": info["negatives"],
            "negative_amount": info["negative_amount"],
        },
        "mom_pct": {"华东": round(info["hd_chg"], 2), "华南": round(info["hn_chg"], 2)},
        "week2_revenue": {
            "全司": info["all_w2"],
            "华东": info["hd_w2"],
            "华南": info["hn_w2"],
            "生鲜": info["fresh_w2"],
            "日化": info["daily_w2"],
            "毛利率_pct": round(info["gm"], 2),
        },
        "files": {
            p.name: {"size": p.stat().st_size, "sha256": _sha256(p)}
            for p in sorted(outdir.iterdir()) if p.is_file()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 S1 夹具")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录，默认 fixtures/w44")
    args = parser.parse_args(argv)

    info = build(Path(args.out))
    sc = info["sales_csv"]
    print("输出目录：%s" % info["outdir"])
    print("sales.csv 行数：%d（唯一记录ID %d）" % (sc["rows"], sc["unique_ids"]))
    print("日期范围：%s，覆盖 %d 天" % (sc["date_range"], sc["distinct_dates"]))
    print("门店：%s" % "、".join(sc["stores"]))
    print("品类：%s" % "、".join(sc["categories"]))
    print("退货负数行：%d 条，合计 %d 元" % (sc["negative_rows"], sc["negative_amount"]))
    print("重复记录ID：%s" % "、".join(sc["duplicate_ids"]))
    print("周环比：华东 %+.2f%%，华南 %+.2f%%" % (info["mom_pct"]["华东"], info["mom_pct"]["华南"]))
    print("全司本周实收：%d 元，毛利率 %.2f%%" % (info["week2_revenue"]["全司"], info["week2_revenue"]["毛利率_pct"]))
    for name, meta in info["files"].items():
        print("%-18s %6d B  %s" % (name, meta["size"], meta["sha256"]))
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
