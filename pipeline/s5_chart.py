"""S5 图表渲染：让 pandasai 画图，但数据只来自已核对的两列表。

设计红线：
- 只接收 labels + points（ledger 的展示数据），不接收原始销售表；
- 图型由 s5_write._chart_kind 决定，通过 prompt 约束，不让模型自由改题；
- 只接受 pandasai 的 type="plot" 结果，从 last_result 取 PNG；
- PNG 必须在本图工作目录内、DPI>=300，代码必须真的读 df 且 savefig；
- 失败返回结构化结果，由上层统一记日志；绝不静默回退旧 matplotlib 模板。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.ask_data import code_flags, profile_code  # noqa: E402


class ChartRenderError(RuntimeError):
    """单张图渲染失败，由 _make_figures 记录后继续下一张。"""


@dataclass
class ChartRenderResult:
    ok: bool
    renderer: str = "pandasai"
    png_path: str = ""
    svg_path: str = ""
    script_path: str = ""
    prompt: str = ""
    prompt_sha256: str = ""
    model: str = ""
    generated_code: str = ""
    code_sha256: str = ""
    unit_scale: float = 1.0
    plot_values: list[float] = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    result_source: str = ""
    elapsed_ms: int = 0
    error: str = ""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, target: Path) -> bool:
    try:
        return os.path.commonpath([str(root.resolve()), str(target.resolve())]) == str(root.resolve())
    except ValueError:
        return False


def build_chart_frame(spec: dict) -> tuple[pd.DataFrame, dict]:
    """把 figure spec 的 labels/points 变成严格两列 DataFrame。

    百分比量纲统一在这里处理：0.42 这类小数乘 100 变成 42.0，42.0 则保持；
    同一张图里混用两种量纲直接报错，不猜。
    """
    labels = [str(item) for item in (spec.get("labels") or [])]
    points = spec.get("points") or []
    keys = [str(item) for item in (spec.get("keys") or []) if item]
    if not labels or len(labels) != len(points):
        raise ChartRenderError("labels 与 points 数量不一致")
    if keys and len(keys) != len(points):
        raise ChartRenderError("keys 与 points 数量不一致")

    values: list[float] = []
    for index, point in enumerate(points):
        try:
            value = float(point.get("value"))
        except (TypeError, ValueError) as exc:
            raise ChartRenderError(f"第 {index + 1} 个点不是数字：{point!r}") from exc
        if not math.isfinite(value):
            raise ChartRenderError(f"第 {index + 1} 个点不是有限数字")
        values.append(value)

    unit = str(spec.get("unit") or (points[0].get("unit") if points else "") or "")
    unit_scale = 1.0
    if unit == "%":
        small = [abs(value) <= 1.5 for value in values]
        if any(small) and not all(small):
            raise ChartRenderError("百分比数值量纲混用（0.42 与 42.0 不能同图）")
        if small and all(small):
            unit_scale = 100.0
            values = [value * unit_scale for value in values]

    frame = pd.DataFrame({"分组": labels, "数值": values})
    meta = {
        "labels": labels,
        "keys": keys,
        "unit": unit,
        "unit_scale": unit_scale,
        "plot_values": values,
        "n": len(labels),
    }
    return frame, meta


def _figsize(kind: str, n: int) -> tuple[float, float]:
    if kind == "barh":
        return 7.2, max(2.8, 1.1 + 0.42 * n)
    if kind == "line":
        return 7.2, 3.2
    if kind == "share":
        return 7.2, 1.55
    return 7.2, 3.4


def build_chart_prompt(spec: dict, frame: pd.DataFrame, meta: dict) -> str:
    """生成画图 prompt：图型、标题、颜色、尺寸、保存方式全部写死。"""
    kind = str(spec.get("kind") or "bar")
    if kind not in ("barh", "bar", "line", "share"):
        raise ChartRenderError("未知图型：" + kind)
    title = str(spec.get("title") or "")
    n = len(frame)
    width, height = _figsize(kind, n)
    unit = str(meta.get("unit") or "")
    if unit == "%":
        value_format = 'f"{v:.1f}%"'
    elif unit in ("件", "条", "笔"):
        value_format = 'f"{v:,.0f}"'
    else:
        value_format = 'f"{v:,.0f}"'

    if kind == "barh":
        chart_rules = (
            "画横向柱状图：ax.barh(df[\"分组\"], df[\"数值\"])，"
            "然后用 ax.invert_yaxis() 让第一行在最上面；所有柱用 #0072B2，"
            "数值最大的那根用 #D55E00。每根柱右侧标数值。"
        )
    elif kind == "line":
        chart_rules = (
            "画折线图：x 轴用 range(len(df))，ax.plot(x, df[\"数值\"], "
            "marker=\"o\", color=\"#0072B2\")；x 轴刻度用 df[\"分组\"] 的文本。"
        )
    elif kind == "share":
        chart_rules = (
            "画一条 100% 堆叠横向条：占比 = df[\"数值\"] / df[\"数值\"].sum()，"
            "x 从 0 到 1；第一段 #0072B2，第二段 #E69F00，段间白色分隔线；"
            "段内标注“分组 + 百分比”（share>=0.14 时白字，否则深色字放上方）；"
            "隐藏 y 轴刻度和四周 spine，x 轴也不要刻度。"
        )
    else:
        chart_rules = (
            "画竖向柱状图：ax.bar(range(len(df)), df[\"数值\"])；"
            "所有柱 #0072B2，最大值 #D55E00；x 轴刻度用 df[\"分组\"]，柱顶标数值。"
        )

    prompt = f"""你是 matplotlib 图表代码生成器。只输出一个 ```python 代码块，不要解释。

数据已经放在名为 df 的 DataFrame 里，只有两列：\"分组\"（字符串）和 \"数值\"（float）。行顺序就是展示顺序，共 {n} 行。

硬性要求：
1. 只能读取 df，不得筛选、聚合、排序、改数、补数、新增行，不得把数值写成新的字面量列表。
2. 必须显式引用 df[\"分组\"] 和 df[\"数值\"] 两个 Series。
3. {chart_rules}
4. 标题逐字使用：{title!r}。用 ax.set_title(..., loc="left", fontsize=11)。
5. 风格：白底；去掉上、右 spine；浅灰横向网格 (#E6E8EB) 并 ax.set_axisbelow(True)；不要图例；不要 seaborn / plotly / pandas.plot。
6. figsize=({width}, {height})，constrained_layout=True。
7. 数值标签格式用 {value_format}。
8. 不要 plt.show()、不要 print、不要写除 temp_chart.png 之外的任何文件。
9. 最后必须调用：
   plt.savefig("temp_chart.png", dpi=300, bbox_inches="tight", facecolor="white")
   然后赋值：
   result = {{"type": "plot", "value": "temp_chart.png"}}
"""
    return prompt


def audit_chart_code(code: str) -> tuple[dict, list[str], bool]:
    """检查生成代码是否真的从 df 取数并保存图，而不是硬编码一套数。"""
    profile = profile_code(code)
    flags = code_flags(profile, code)
    columns = set(profile.get("columns") or [])
    if not code:
        flags.append("code_unavailable")
    if "分组" not in columns or "数值" not in columns:
        flags.append("no_df_reference")
    if "savefig" not in set(profile.get("methods") or []):
        flags.append("no_savefig")
    hard = any(
        flag == "code_unavailable"
        or flag == "no_df_reference"
        or flag == "no_savefig"
        or flag.startswith("dangerous_call:")
        or flag.startswith("writes_file:")
        for flag in flags
    )
    return profile, flags, hard


def _resolve_plot_path(agent: Any, result: dict, work_dir: Path) -> tuple[Path, str]:
    value = result.get("value")
    if isinstance(value, str) and value.startswith("data:image/png;base64,"):
        payload = value.split(",", 1)[1]
        target = work_dir / "result_from_base64.png"
        target.write_bytes(base64.b64decode(payload))
        return target, "base64"

    if not isinstance(value, str) or not value.strip():
        raise ChartRenderError("pandasai 的 plot 结果没有 PNG 路径")

    candidate = Path(value)
    if not candidate.is_absolute():
        in_work = (work_dir / candidate).resolve()
        in_cwd = candidate.resolve()
        candidate = in_work if in_work.is_file() else in_cwd
    candidate = candidate.resolve()
    if not _inside(work_dir, candidate):
        raise ChartRenderError("pandasai 返回的图不在本次工作目录内：" + str(candidate))
    if candidate.is_file():
        return candidate, "last_result"

    # 某些版本把 value 写成 temp_chart.png，实际文件按 prompt_id 命名
    prompt_id = str(getattr(agent, "last_prompt_id", "") or "")
    fallback = (work_dir / (prompt_id + ".png")).resolve() if prompt_id else None
    if fallback and fallback.is_file() and _inside(work_dir, fallback):
        return fallback, "prompt_id_file"
    raise ChartRenderError("pandasai 返回的 PNG 不存在：" + str(candidate))


def _check_png(path: Path) -> dict:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 环境缺 Pillow
        raise ChartRenderError("Pillow 未安装，无法校验 PNG") from exc
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ChartRenderError("不是 PNG 文件：" + str(path))
            dpi = image.info.get("dpi")
            size = list(image.size)
    except ChartRenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ChartRenderError("PNG 无法读取：" + str(exc)) from exc
    if not dpi:
        raise ChartRenderError("PNG 没有 DPI 元数据，无法确认 300dpi")
    dx = dpi[0] if isinstance(dpi, (tuple, list)) else dpi
    if round(float(dx)) < 300:
        raise ChartRenderError("PNG DPI=" + str(round(float(dx))) + "，低于 300")
    return {"ok": True, "dpi": [round(float(v)) for v in (dpi if isinstance(dpi, (tuple, list)) else (dpi, dpi))],
            "size_px": size}


def _save_script(path: Path, spec: dict, prompt: str, model: str, code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# pandasai 生成图表代码（审计用，不直接执行）\n"
        "# figure: " + str(spec.get("name") or "") + "\n"
        "# title: " + str(spec.get("title") or "") + "\n"
        "# finding_id: " + str(spec.get("finding_id") or "") + "\n"
        "# keys: " + json.dumps(spec.get("keys") or [], ensure_ascii=False) + "\n"
        "# values: " + json.dumps(spec.get("points") or [], ensure_ascii=False) + "\n"
        "# model: " + model + "\n"
        "# prompt_sha256: " + _sha256_text(prompt) + "\n"
        "# code_sha256: " + _sha256_text(code) + "\n"
        "# " + "-" * 60 + "\n\n"
    )
    path.write_text(header + code.rstrip() + "\n", encoding="utf-8")


def render_chart(
    spec: dict,
    *,
    out_png: str | Path,
    script_path: str | Path,
    work_dir: str | Path,
    agent_factory: Callable[[pd.DataFrame, Path], Any] | None = None,
    prepare_style: bool = True,
    max_retries: int = 1,
) -> ChartRenderResult:
    """让 pandasai 画一张图；失败返回 ok=False，绝不回退旧模板。"""
    started = time.perf_counter()
    work = Path(work_dir)
    out = Path(out_png)
    script = Path(script_path)
    prompt = ""
    code = ""
    model = ""
    flags: list[str] = []
    try:
        work.mkdir(parents=True, exist_ok=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        frame, meta = build_chart_frame(spec)
        prompt = build_chart_prompt(spec, frame, meta)

        if prepare_style:
            import matplotlib
            matplotlib.use("Agg")
            from tools.figure.setup_style import setup_style

            style = setup_style(journal="general", lang="zh")
            font = str(style.get("cjk_font") or "")
            if not font or font == "DejaVu Sans":
                raise ChartRenderError("没有可用的中文字体，拒绝生成乱码图")

        if agent_factory:
            agent = agent_factory(frame, work)
        else:
            from .pandasai_glm import make_chart_agent

            agent = make_chart_agent(frame, work, max_retries=max_retries)
        model = str(getattr(getattr(agent, "config", None), "model", "") or "")
        if not model:
            try:
                from .llm import get_config

                model = str(get_config().model)
            except Exception:  # noqa: BLE001 - 模型名只做审计展示
                model = ""

        for attr in ("last_result", "last_code_generated", "last_code_executed", "last_prompt_id"):
            try:
                setattr(agent, attr, None)
            except Exception:  # noqa: BLE001 - 个别属性只读
                pass

        agent.chat(prompt, output_type="plot")
        result = getattr(agent, "last_result", None)
        if not isinstance(result, dict) or str(result.get("type") or "") != "plot":
            raise ChartRenderError("pandasai 没有返回 plot 结果")

        code = str(getattr(agent, "last_code_generated", "") or getattr(agent, "last_code", "") or "")
        profile, flags, hard = audit_chart_code(code)
        if hard:
            raise ChartRenderError("生成代码未通过审计：" + "、".join(flags))

        png, source = _resolve_plot_path(agent, result, work)
        quality = _check_png(png)

        tmp = out.with_suffix(".tmp.png")
        shutil.copyfile(png, tmp)
        os.replace(tmp, out)
        _save_script(script, spec, prompt, model, code)
        return ChartRenderResult(
            ok=True, png_path=str(out), script_path=str(script),
            prompt=prompt, prompt_sha256=_sha256_text(prompt), model=model,
            generated_code=code, code_sha256=_sha256_text(code),
            unit_scale=float(meta.get("unit_scale") or 1.0),
            plot_values=[float(v) for v in meta.get("plot_values") or []],
            quality=quality, flags=flags, result_source=source,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - 单图失败交给上层日志
        return ChartRenderResult(
            ok=False, prompt=prompt, prompt_sha256=_sha256_text(prompt) if prompt else "",
            model=model, generated_code=code, code_sha256=_sha256_text(code) if code else "",
            flags=flags, elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
