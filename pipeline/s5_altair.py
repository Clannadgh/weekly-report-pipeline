"""S5 图表渲染：Altair / Vega-Lite 固定模板。

pandasai 只负责发现和取数；这里把已入 ledger 的两列数据按固定主题渲染成
SVG + 300dpi PNG。图型由 s5_write._chart_kind 决定，不再让模型写 matplotlib 代码。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import altair as alt
import pandas as pd
import vl_convert
from PIL import Image

from .s5_chart import ChartRenderResult, build_chart_frame

PRIMARY = "#0072B2"
CONTRAST = "#D55E00"
SECONDARY = "#E69F00"
GRID = "#E6E8EB"
INK = "#222222"
MUTED = "#555F6B"
FONT = "Microsoft YaHei"


def _title(text: str) -> alt.TitleParams:
    return alt.TitleParams(text=str(text or ""), anchor="start", fontSize=16,
                           font=FONT, color=INK, offset=14)


def _finish(chart: alt.Chart) -> alt.Chart:
    return chart.configure(
        font=FONT,
        background="white",
        view=alt.ViewConfig(stroke=None),
        axis=alt.AxisConfig(domain=False, ticks=False),
    )


def _label_series(values: list[float], unit: str) -> list[str]:
    if unit == "%":
        return [f"{value:.1f}%" for value in values]
    return [f"{value:,.0f}" for value in values]


def _barh(df: pd.DataFrame, spec: dict, unit: str) -> alt.Chart:
    max_value = float(df["数值"].max())
    base = alt.Chart(df).encode(
        y=alt.Y("分组:N", sort=None,
                axis=alt.Axis(labelLimit=170, title=None, grid=False, labelFontSize=12)),
        x=alt.X("数值:Q",
                axis=alt.Axis(format=",.0f", title=None, grid=True,
                              gridColor=GRID, tickCount=6, labelFontSize=11)),
    )
    bars = base.mark_bar(cornerRadiusEnd=3, height=18).encode(
        color=alt.condition(alt.datum.数值 == max_value,
                            alt.value(CONTRAST), alt.value(PRIMARY)),
    )
    text = base.mark_text(align="left", dx=4, fontSize=11, color=MUTED).encode(text="标签:N")
    return (bars + text).properties(width=640, height=alt.Step(32), title=_title(spec["title"]))


def _bar(df: pd.DataFrame, spec: dict, unit: str) -> alt.Chart:
    max_value = float(df["数值"].max())
    base = alt.Chart(df).encode(
        x=alt.X("分组:N", sort=None,
                axis=alt.Axis(labelAngle=0, title=None, labelFontSize=12)),
        y=alt.Y("数值:Q",
                axis=alt.Axis(format=",.0f", title=None, grid=True,
                              gridColor=GRID, tickCount=6, labelFontSize=11)),
    )
    bars = base.mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, width=44).encode(
        color=alt.condition(alt.datum.数值 == max_value,
                            alt.value(CONTRAST), alt.value(PRIMARY)),
    )
    text = base.mark_text(dy=-8, fontSize=12, color=MUTED).encode(text="标签:N")
    return (bars + text).properties(width=680, height=330, title=_title(spec["title"]))


def _line(df: pd.DataFrame, spec: dict, unit: str) -> alt.Chart:
    base = alt.Chart(df).encode(
        x=alt.X("分组:N", sort=None,
                axis=alt.Axis(labelAngle=0, title=None, labelFontSize=11)),
        y=alt.Y("数值:Q",
                axis=alt.Axis(format=",.0f", title=None, grid=True,
                              gridColor=GRID, tickCount=6, labelFontSize=11)),
    )
    line = base.mark_line(point=alt.OverlayMarkDef(size=60, filled=True),
                          strokeWidth=2.5, color=PRIMARY)
    return line.properties(width=680, height=320, title=_title(spec["title"]))


def _share(df: pd.DataFrame, spec: dict, unit: str) -> alt.Chart:
    total = float(df["数值"].sum())
    if total <= 0:
        raise ValueError("占比图需要正数合计")
    df = df.copy()
    df["占比"] = df["数值"] / total
    df["标签"] = [f"{label} {share:.1%}" for label, share in zip(df["分组"], df["占比"])]
    cumulative = df["占比"].cumsum()
    df["中点"] = cumulative - df["占比"] / 2

    base = alt.Chart(df).encode(
        x=alt.X("占比:Q", stack="normalize", axis=None),
        color=alt.Color("分组:N",
                        scale=alt.Scale(range=[PRIMARY, SECONDARY]), legend=None),
    )
    bars = base.mark_bar(height=48, cornerRadius=4)
    text = alt.Chart(df).mark_text(color="white", fontSize=14, fontWeight="bold").encode(
        x=alt.X("中点:Q", scale=alt.Scale(domain=[0, 1]), axis=None),
        text=alt.Text("标签:N"),
    )
    return (bars + text).properties(width=700, height=100, title=_title(spec["title"]))


def build_chart(spec: dict) -> alt.Chart:
    frame, meta = build_chart_frame(spec)
    unit = str(meta.get("unit") or "")
    frame = frame.copy()
    frame["标签"] = _label_series([float(v) for v in frame["数值"]], unit)
    kind = str(spec.get("kind") or "bar")
    if kind == "barh":
        chart = _barh(frame, spec, unit)
    elif kind == "line":
        chart = _line(frame, spec, unit)
    elif kind == "share":
        chart = _share(frame, spec, unit)
    else:
        chart = _bar(frame, spec, unit)
    return _finish(chart)


def render_chart(spec: dict, *, out_png: str | Path, script_path: str | Path,
                 work_dir: str | Path | None = None) -> ChartRenderResult:
    """按固定 Altair 模板出图：SVG + 300dpi PNG；失败返回 ok=False。"""
    out = Path(out_png)
    svg_path = out.with_suffix(".svg")
    script = Path(script_path).with_suffix(".vl.json")
    try:
        _frame, meta = build_chart_frame(spec)
        chart = build_chart(spec)
        vl_spec = chart.to_dict()
        out.parent.mkdir(parents=True, exist_ok=True)
        script.parent.mkdir(parents=True, exist_ok=True)

        svg = vl_convert.vegalite_to_svg(vl_spec)
        png = vl_convert.vegalite_to_png(vl_spec, ppi=300)
        svg_path.write_text(svg, encoding="utf-8")
        out.write_bytes(png)
        script.write_text(json.dumps(vl_spec, ensure_ascii=False, indent=2), encoding="utf-8")

        with Image.open(out) as image:
            dpi = image.info.get("dpi") or (0, 0)
            size = list(image.size)
        dx = dpi[0] if isinstance(dpi, (tuple, list)) else dpi
        if round(float(dx)) < 300:
            raise ValueError("PNG DPI 低于 300")
        return ChartRenderResult(
            ok=True, renderer="altair", png_path=str(out), svg_path=str(svg_path),
            script_path=str(script), model="", generated_code="",
            code_sha256="", unit_scale=float(meta.get("unit_scale") or 1.0),
            plot_values=[float(v) for v in meta.get("plot_values") or []],
            quality={"ok": True,
                     "dpi": [round(float(v)) for v in
                             (dpi if isinstance(dpi, (tuple, list)) else (dpi, dpi))],
                     "size_px": size},
            result_source="altair", flags=[],
        )
    except Exception as exc:  # noqa: BLE001 - 单图失败交给上层日志
        return ChartRenderResult(ok=False, renderer="altair", error=str(exc))
