"""ask_data —— 给「核对」环节用的取数工具。

对上层暴露一个 ask()：输入一句口径明确的问题，返回原始答案、解析值、代码、
口径指纹与判定结果。内部封装 PandasAI 的 chat()，只取答案与 last_code_generated。

用法：
    from tools.ask_data import DataQA

    qa = DataQA({"df_sales": sales, "df_stores": stores},
                model="gpt-4o-mini", api_key="...")

    r = qa.ask(
        "用 df_sales 和 df_stores，计算 2026-10-26 至 2026-11-01、区域为「华南」的"
        "实收金额合计。退货金额为负数直接计入；按记录ID去重保留第一条；结果保留两位小数。"
        "只输出一个数字，不要单位、不要千分位、不要解释、不要图表。",
        expect_shape="number",
        expected={"type": "flat", "kind": "change", "tolerance": "ratio_pp:5"},
        reference=-0.1271,          # 独立复算得到的值，必须由规则侧算，不能复用本工具的代码
    )

    r["value"], r["verdict"], r["flags"]

注意：
- chat() 会先执行代码再返回，本工具只能事后审计，拦不住危险动作。要执行前拦截，
  请改用 generate_code() 自己执行。
- reference 必须是独立算出来的值；传同一段代码的结果进来等于没有比对。
- 未安装 pandasai 时可用 chat_fn 注入任意回调（自测即走这条路）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import pandas as pd

DEFAULT_MODEL = "gpt-4o-mini"
# 容差没有默认值：expected 缺 tolerance 时按 0 判，并在结果里标 fallback=True，
# 由调用方决定是否重问。可用写法：amount:50,0.05、ratio_pp:5、margin_pp:1、count、abs:3

DANGEROUS_CALLS = {
    "eval", "exec", "compile", "open", "__import__",
    "system", "popen", "remove", "unlink", "rmtree", "rename",
    "to_csv", "to_excel", "to_pickle", "to_sql", "to_parquet",
    "drop",
}
FILE_WRITES = {"to_csv", "to_excel", "to_pickle", "to_sql", "to_parquet"}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


class AskDataError(RuntimeError):
    """工具本身的配置或调用错误，不是数据回答错误。"""


# ---------------------------------------------------------------- schema hash

def schema_hash(frames: Mapping[str, pd.DataFrame]) -> str:
    parts = []
    for name in sorted(frames):
        df = frames[name]
        cols = ",".join(f"{c}:{df[c].dtype}" for c in df.columns)
        parts.append(f"{name}[{cols}]")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def question_hash(question: str, model: str, schema: str, extra: str = "") -> str:
    """缓存键必须含判定参数。

    只拿问句做键会出事故：同一句话换了 expected 或 reference 仍命中上一轮的缓存，
    拿回旧 verdict。extra 由调用方传入 expected / reference / expect_shape 的指纹。
    """
    raw = f"{model}\x00{schema}\x00{question}\x00{extra}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def judge_fingerprint(expect_shape: str, expected: Mapping[str, Any] | None,
                      reference: Any) -> str:
    """把判定参数压成一个稳定指纹，进缓存键。"""
    return json.dumps({"shape": expect_shape, "expected": expected, "reference": reference},
                      sort_keys=True, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- code profile

def profile_code(code: str | None) -> dict[str, Any]:
    """从代码里抽确定事实：用了哪些表、列、方法、字面量。抽不出就返回空。"""
    empty = {"tables": [], "columns": [], "methods": [], "calls": [], "literals": [],
             "imports": [], "hardcoded_result": False, "mutation": False}
    if not code:
        return empty

    try:
        tree = ast.parse(code)
    except SyntaxError:
        empty["parse_error"] = True
        return empty

    tables: set[str] = set()
    columns: set[str] = set()
    methods: set[str] = set()
    calls: set[str] = set()
    literals: list[str] = []
    imports: list[str] = []
    hardcoded = False
    mutation = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("df_"):
            tables.add(node.id)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if isinstance(node.slice.value, str):
                columns.add(node.slice.value)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                methods.add(func.attr)
            elif isinstance(func, ast.Name):
                calls.add(func.id)
            for kw in node.keywords:
                if kw.arg == "inplace" and isinstance(kw.value, ast.Constant):
                    if kw.value.value is True:
                        mutation = True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "result" not in targets:
            continue
        if isinstance(node.value, ast.Constant):
            hardcoded = True
        if isinstance(node.value, (ast.List, ast.Tuple)) and node.value.elts:
            hardcoded = all(isinstance(e, ast.Constant) for e in node.value.elts)

    return {
        "tables": sorted(tables),
        "columns": sorted(columns),
        "methods": sorted(methods),
        "calls": sorted(calls),
        "literals": literals[:20],
        "imports": sorted(set(imports)),
        "hardcoded_result": hardcoded,
        "mutation": mutation,
    }


def code_flags(profile: Mapping[str, Any], code: str | None) -> list[str]:
    flags: list[str] = []
    hits = (set(profile.get("methods", [])) | set(profile.get("calls", []))) & DANGEROUS_CALLS
    if hits:
        flags.append("dangerous_call:" + ",".join(sorted(hits)))
    writes = set(profile.get("methods", [])) & FILE_WRITES
    if writes:
        flags.append("writes_file:" + ",".join(sorted(writes)))
    if profile.get("imports"):
        flags.append("import:" + ",".join(profile["imports"]))
    if profile.get("mutation"):
        flags.append("mutation:inplace")
    if profile.get("hardcoded_result"):
        flags.append("hardcoded_result")
    if code and "inplace=True" in code.replace(" ", ""):
        if "mutation:inplace" not in flags:
            flags.append("mutation:inplace")
    return flags


# ---------------------------------------------------------------- parse answer

def parse_number(raw: Any) -> tuple[float | None, str]:
    """只接受唯一一个数字。0 个、多个、非文本非数值一律判失败，不猜。

    返回的第二项是小单位标记：ok / pct / pp / wan / yi。
    带「万」「亿」的一并折算到基本单位，避免 17.7 万元 被当成 17.7 元与
    177,306.00 元同场比较。
    """
    if isinstance(raw, bool):
        return None, "failed"
    if isinstance(raw, (int, float)):
        return float(raw), "ok"
    if not isinstance(raw, str):
        return None, "failed"

    text = raw.strip().replace(",", "").replace("，", "")
    nums = _NUM_RE.findall(text)
    if len(nums) != 1:
        return None, "failed"

    value = float(nums[0])
    if "%" in text:
        return value / 100.0, "pct"
    if "个百分点" in text:
        return value / 100.0, "pp"
    if "亿" in text:
        return value * 1e8, "yi"
    if "万" in text:
        return value * 1e4, "wan"
    return value, "ok"


def to_number(raw: Any) -> tuple[float | None, str]:
    """把参考值规约成与 parse_number 同量纲的浮点。

    参考值来自纪要原句，可能是 35.2%、-12.7%、177,306.00 元、17.7 万元
    这类字符串；不能直接参与浮点减法。
    """
    if raw is None:
        return None, "none"
    if isinstance(raw, bool):
        return None, "failed"
    if isinstance(raw, (int, float)):
        return float(raw), "ok"
    if isinstance(raw, str):
        return parse_number(raw)
    return None, "failed"


def parse_rows(raw: Any) -> list[dict[str, Any]] | None:
    """表格结果。空表返回 []（是「查到了 0 行」，不是解析失败）。"""
    if isinstance(raw, pd.DataFrame):
        if raw.empty:
            return []
        payload = raw.head(200).to_json(orient="records", force_ascii=False)
        if payload is None:
            return None
        return json.loads(str(payload))
    if isinstance(raw, list):
        if not raw:
            return []
        if all(isinstance(x, dict) for x in raw):
            return raw[:200]
    return None


# ---------------------------------------------------------------- tolerance

def tolerance_spec(expected: Mapping[str, Any] | None) -> tuple[str, bool]:
    """返回 (容差串, 是否缺失)。缺失时不套默认值，由调用方显式标记并按 0 判。"""
    spec = ""
    if expected:
        spec = str(expected.get("tolerance") or "").strip()
    return spec, not spec


def parse_tolerance(expected: Mapping[str, Any] | None,
                    base: float | None) -> tuple[float, str]:
    """把容差串解析成与 value 同量纲的绝对容差。

    返回 (容差, 状态)。状态让调用方分得清「没传」「写错」「没基准算百分比」：
      ok              正常解析
      missing         没传 tolerance —— 按 0 判，调用方须标 fallback
      unparsed        写法不认识（如 pp:5、5%）—— 同样是按 0 判，但要单独报，不能静默
      ok_no_ref_pct   amount:50,0.05 但没有 reference，只能取绝对部分；
                      绝不能用本轮 value 当基准，那等于自己给自己定容差
    """
    spec, missing = tolerance_spec(expected)
    if missing:
        return 0.0, "missing"
    head, _, tail = spec.partition(":")
    try:
        if head in ("ratio_pp", "margin_pp"):
            return float(tail) / 100.0, "ok"
        if head == "count":
            return 0.0, "ok"
        if head == "abs":
            return float(tail), "ok"
        if head == "amount":
            abs_part, pct_part = (tail.split(",") + ["0"])[:2]
            pct = float(pct_part)
            if pct == 0.0:
                return float(abs_part), "ok"
            if base is None:
                return float(abs_part), "ok_no_ref_pct"
            return max(float(abs_part), pct * abs(base)), "ok"
    except (ValueError, IndexError):
        return 0.0, "unparsed"
    return 0.0, "unparsed"


def judge(
    value: float | None,
    expected: Mapping[str, Any] | None,
    reference: Any,
    tolerance: float,
) -> tuple[bool, str, float | None]:
    """返回 (evaluated, verdict, diff)。

    reference 可以是数字，也可以是纪要原句里的「35.2%」「17.7 万元」这类文本，
    一律先规约成与 value 同量纲的浮点再比；规约不了按 unsupported 处理，不猜。
    """
    if value is None:
        return False, "unsupported", None
    if not expected:
        # 未传 expected 表示不判定，不能默认成 match
        return False, "none", None

    kind = str(expected.get("kind") or "change")
    etype = str(expected.get("type") or "none")

    if etype == "none":
        return False, "unsupported", None

    ref: float | None = None
    if reference is not None:
        ref, _ = to_number(reference)
        if ref is None:
            return False, "unsupported", None

    diff = None
    if ref is not None:
        diff = value - ref

    def ok(cond: bool) -> tuple[bool, str, float | None]:
        return True, ("match" if cond else "mismatch"), diff

    if etype == "flat":
        if kind == "change":
            return ok(abs(value) <= tolerance)
        if ref is None:
            return False, "unsupported", None
        # kind=total 比的是绝对差，与 diff 同量纲；不除以 |ref|，
        # 否则比例量纲下 4 个百分点的差会被算成 20% 相对差
        return ok(abs(value - ref) <= tolerance)

    if etype in ("up", "down"):
        if kind == "change":
            # value 本身是变化率，只有方向可判，没有可比的幅度基准
            return ok(value > 0 if etype == "up" else value < 0)
        if ref is None:
            return False, "unsupported", None
        # kind=total：方向一致之外还要幅度落在容差内，否则「涨了 0.9」也判 match
        direction_ok = value > ref if etype == "up" else value < ref
        return ok(direction_ok and abs(value - ref) <= tolerance)

    if etype in ("value", "ratio"):
        target, _ = to_number(expected.get("value"))
        if target is None:
            return False, "unsupported", None
        return ok(abs(value - target) <= tolerance)

    if etype == "range":
        low, _ = to_number(expected.get("low"))
        high, _ = to_number(expected.get("high"))
        if low is None or high is None:
            return False, "unsupported", None
        return ok(low <= value <= high)

    return False, "unsupported", None


# ---------------------------------------------------------------- the tool

class DataQA:
    """PandasAI chat() 的受控封装：串行调用、取代码、解析、审计、缓存。"""

    def __init__(
        self,
        frames: Mapping[str, pd.DataFrame],
        *,
        llm: Any = None,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        cache_dir: str | Path | None = None,
        max_retries: int = 1,
        chat_fn: Callable[[str], Any] | None = None,
    ) -> None:
        if not frames:
            raise AskDataError("frames 为空")
        for name in frames:
            if not name.startswith("df_"):
                raise AskDataError(f"表名必须以 df_ 开头，收到：{name}")

        self.frames = dict(frames)
        self.model = model
        self.schema = schema_hash(self.frames)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._lake = None
        self._chat_fn = chat_fn or self._make_pandasai(llm, model, api_key, base_url, max_retries)

    # -- PandasAI 初始化 ---------------------------------------------------

    def _make_pandasai(self, llm, model, api_key, base_url, max_retries):
        try:
            import pandasai  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise AskDataError(
                "未安装 pandasai。请 pip install pandasai，或用 chat_fn 注入回调。"
            ) from exc

        if llm is None:
            from pandasai.llm import OpenAI  # type: ignore

            kwargs: dict[str, Any] = {"model": model}
            if api_key:
                kwargs["api_token"] = api_key
            if base_url:
                kwargs["base_url"] = base_url
            llm = OpenAI(**kwargs)

        base_config = {
            "llm": llm,
            "enable_cache": False,
            "save_charts": False,
            "open_charts": False,
            "verbose": False,
            "conversational": False,
            "max_retries": max_retries,
        }

        lake = None
        try:  # 1.x
            from pandasai import SmartDatalake  # type: ignore

            lake = SmartDatalake(list(self.frames.values()), config=base_config)
        except Exception:
            from pandasai import Agent  # type: ignore

            lake = Agent(list(self.frames.values()), config=base_config)

        self._lake = lake
        return lake.chat

    # -- 主入口 -------------------------------------------------------------

    def ask(
        self,
        question: str,
        *,
        expect_shape: str = "number",
        expected: Mapping[str, Any] | None = None,
        reference: float | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        if expect_shape not in ("number", "table"):
            raise AskDataError(
                f"expect_shape 只能是 number 或 table，收到：{expect_shape!r}"
            )

        started = time.perf_counter()
        qhash = question_hash(question, self.model, self.schema,
                              judge_fingerprint(expect_shape, expected, reference))

        cached = self._load_cache(qhash) if use_cache else None
        if cached is not None:
            cached["cached"] = True
            return cached

        with self._lock:
            raw = self._chat_fn(question)
            code = self._capture_code()

        profile = profile_code(code)
        flags = code_flags(profile, code)
        if code is None:
            # 拿不到代码就等于丢失审计线索，必须让上层看见，不能静默算通过
            flags.append("code_unavailable")

        rows: list[dict[str, Any]] | None = None
        value: float | None = None
        unit = "ok"
        if expect_shape == "table":
            rows = parse_rows(raw)
            status = "ok" if rows is not None else "parse_failed"
            if rows == []:
                flags.append("empty_rows")
        else:
            value, unit = parse_number(raw)
            status = "ok" if value is not None else "parse_failed"
            if unit in ("pct", "pp", "wan", "yi"):
                flags.append(f"unit_{unit}_normalized")

        # 参考值可能来自纪要原句（"35.2%"、"-12.7%"、"17.7 万元"），
        # 先规约成与 value 同量纲的浮点；规约不了按无参考值处理并标记
        ref, ref_unit = to_number(reference)
        if reference is not None and ref is None:
            flags.append("reference_unparsed")

        # 比例题的答案丢了百分号：参考值是 0.127 这类小数，答案却是 12.7。
        # 差 100 倍不能当成「数据对不上」，判 parse_failed 让上层重问一次，
        # 重问时在问题里要求带 % 输出；仍不给就记无法核对，不替它除以 100。
        etype_for_unit = str((expected or {}).get("type") or "")
        if (status == "ok" and expect_shape == "number" and unit == "ok"
                and ref is not None and abs(ref) <= 1.0
                and 1.5 <= abs(value or 0.0) <= 100.0
                and etype_for_unit in ("flat", "up", "down", "ratio")):
            flags.append("unit_pct_suspect")
            status = "parse_failed"
            value = None

        tolerance, tol_state = parse_tolerance(expected, ref)
        fallback = False
        if expected:
            if tol_state == "missing" and etype_for_unit != "range":
                fallback = True
                flags.append("tolerance_missing")
            elif tol_state == "unparsed":
                fallback = True
                flags.append("tolerance_unparsed")
            elif tol_state == "ok_no_ref_pct":
                flags.append("pct_tolerance_without_ref")
        # judge 内部再规约一次原始 reference：规约不了就判 unsupported，
        # 不能因为本地 ref 为 None 就当成「没有参考值」继续判定
        evaluated, verdict, diff = judge(value, expected, reference, tolerance)

        within = None
        if ref is not None and value is not None:
            within = abs(value - ref) <= tolerance

        result = {
            "ok": status == "ok",
            "raw": raw if isinstance(raw, (str, int, float)) else str(raw),
            "value": value,
            "rows": rows,
            "status": status,
            "verdict": verdict,
            "evaluated": evaluated,
            "diff": diff,
            "ref": ref,
            "ref_unit": ref_unit,
            "within_tolerance": within,
            "fallback": fallback,
            "code": code,
            "profile": profile,
            "flags": flags,
            "question_hash": qhash,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "cached": False,
        }

        if use_cache and status == "ok":
            self._save_cache(qhash, result)
        return result

    # -- 内部 ---------------------------------------------------------------

    def _capture_code(self) -> str | None:
        lake = getattr(self, "_lake", None)
        if lake is None:
            return None
        for attr in ("last_code_generated", "last_code"):
            code = getattr(lake, attr, None)
            if isinstance(code, str) and code.strip():
                return code
        return None

    def _cache_path(self, qhash: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{qhash}.json"

    def _load_cache(self, qhash: str) -> dict[str, Any] | None:
        path = self._cache_path(qhash)
        if path and path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _save_cache(self, qhash: str, result: dict[str, Any]) -> None:
        path = self._cache_path(qhash)
        if not path:
            return
        try:
            _ = path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass


# ---------------------------------------------------------------- selftest

def _selftest() -> None:
    df_sales = pd.DataFrame(
        {
            "记录ID": ["r1", "r2", "r3"],
            "销售日期": pd.to_datetime(["2026-10-26", "2026-10-27", "2026-11-01"]),
            "门店编码": ["HZ01", "HZ01", "SZ01"],
            "销售数量": [10, -1, 5],
            "实收金额": [1000.0, -100.0, 500.0],
            "成本金额": [600.0, -60.0, 300.0],
        }
    )
    df_stores = pd.DataFrame({"门店编码": ["HZ01", "SZ01"], "区域": ["华东", "华南"]})
    frames = {"df_sales": df_sales, "df_stores": df_stores}

    qa = DataQA(frames, chat_fn=lambda q: "177,306.00")
    r = qa.ask("q1", expected={"type": "flat", "kind": "change", "tolerance": "ratio_pp:5"},
               reference=-0.1271)
    assert r["value"] == 177306.0 and r["status"] == "ok", r
    assert r["verdict"] == "mismatch", r
    assert r["within_tolerance"] is False, r

    qa2 = DataQA(frames, chat_fn=lambda q: "-12.7%")
    r2 = qa2.ask("q2", expected={"type": "flat", "kind": "change", "tolerance": "ratio_pp:5"})
    assert abs(r2["value"] - (-0.127)) < 1e-9, r2
    assert r2["verdict"] == "mismatch", r2
    assert "unit_pct_normalized" in r2["flags"], r2

    qa3 = DataQA(frames, chat_fn=lambda q: "约 17.7 万，环比 -12.7%")
    r3 = qa3.ask("q3")
    assert r3["status"] == "parse_failed", r3

    qa4 = DataQA(frames, chat_fn=lambda q: pd.DataFrame({"门店": ["SZ01"], "实收": [500.0]}))
    r4 = qa4.ask("q4", expect_shape="table")
    assert r4["rows"] == [{"门店": "SZ01", "实收": 500.0}], r4

    profile = profile_code(
        "cur = df_sales[df_sales['实收金额'] > 0]\nresult = 177306.0\n"
    )
    assert profile["hardcoded_result"] is True, profile
    assert profile["columns"] == ["实收金额"], profile
    assert "hardcoded_result" in code_flags(profile, ""), profile

    profile2 = profile_code("result = df_sales['实收金额'].sum()\n")
    assert profile2["hardcoded_result"] is False, profile2
    assert profile2["tables"] == ["df_sales"], profile2

    # --- 容差语义：flat/total 用绝对差，不除以 |reference|
    tol5, tol_state = parse_tolerance({"tolerance": "ratio_pp:5"}, -0.20)
    assert (tol5, tol_state) == (0.05, "ok"), (tol5, tol_state)
    ev, v, d = judge(-0.24, {"type": "flat", "kind": "total", "tolerance": "ratio_pp:5"},
                     -0.20, tol5)
    assert (ev, v) == (True, "match"), (ev, v, d)
    assert abs(d - (-0.04)) < 1e-9, d

    # --- up/total：方向对但幅度超容差 → mismatch
    ev, v, d = judge(0.90, {"type": "up", "kind": "total", "tolerance": "ratio_pp:5"},
                     -0.01, 0.05)
    assert (ev, v) == (True, "mismatch"), (ev, v, d)

    # --- 未传 expected 不得判 match
    assert judge(1.0, None, None, 0.0)[:2] == (False, "none")

    # --- 缺 tolerance：容差 0、fallback 标记、精确判等
    assert tolerance_spec({"type": "value", "value": 100}) == ("", True)
    qa5 = DataQA(frames, chat_fn=lambda q: "100.0")
    r5 = qa5.ask("q5", expected={"type": "value", "value": 104.9})
    assert r5["fallback"] is True and r5["verdict"] == "mismatch", r5
    assert "tolerance_missing" in r5["flags"], r5

    # --- 单位折算：万元 折成元，避免与「元」同场比较
    assert parse_number("17.7 万元") == (177000.0, "wan")
    assert parse_number("177,306.00 元") == (177306.0, "ok")
    assert parse_number("1.2 亿元") == (120000000.0, "yi")
    assert to_number("-12.7%")[0] == -0.127
    assert to_number(None)[0] is None
    assert to_number("说不清")[0] is None

    # --- 参考值可能是纪要原句文本：规约后参与减法，不抛 TypeError
    qa6 = DataQA(frames, chat_fn=lambda q: "-0.5%")
    r6 = qa6.ask("q6",
                 expected={"type": "flat", "kind": "change", "tolerance": "ratio_pp:5"},
                 reference="-12.7%")
    assert r6["ref"] == -0.127 and r6["verdict"] == "match", r6
    assert abs(r6["diff"] - 0.122) < 1e-9, r6

    # --- 参考值规约不了：不崩，判 unsupported 并留痕
    qa7 = DataQA(frames, chat_fn=lambda q: "1.0")
    r7 = qa7.ask("q7",
                 expected={"type": "value", "value": 1.0, "tolerance": "abs:0"},
                 reference="原因待核实")
    assert r7["verdict"] == "unsupported", r7
    assert "reference_unparsed" in r7["flags"], r7

    # --- expected.value 同样接受带单位的字符串
    qa8 = DataQA(frames, chat_fn=lambda q: "35.2%")
    r8 = qa8.ask("q8",
                 expected={"type": "ratio", "kind": "total", "value": "35.2%",
                           "tolerance": "ratio_pp:5"})
    assert abs(r8["value"] - 0.352) < 1e-9 and r8["verdict"] == "match", r8

    # --- 缓存键含判定参数：换容差不许命中旧 verdict
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        qa_c = DataQA(frames, chat_fn=lambda q: "0.10", cache_dir=d)
        e5 = {"type": "flat", "kind": "change", "tolerance": "ratio_pp:5"}
        e20 = {"type": "flat", "kind": "change", "tolerance": "ratio_pp:20"}
        c1 = qa_c.ask("q_cache", expected=e5)
        c2 = qa_c.ask("q_cache", expected=e20)
        assert c1["verdict"] == "mismatch", c1
        assert c2["verdict"] == "match" and c2["cached"] is False, c2

    # --- 容差写法不认识：不静默按 0 蒙混
    qa_bad = DataQA(frames, chat_fn=lambda q: "100.0")
    rb = qa_bad.ask("q_bad", expected={"type": "value", "value": 100.0,
                                      "tolerance": "pp:5"})
    assert rb["fallback"] is True and "tolerance_unparsed" in rb["flags"], rb

    # --- 百分比丢了 %：判 parse_failed 让上层重问，不替它除 100
    qa_pct = DataQA(frames, chat_fn=lambda q: "12.7")
    rp = qa_pct.ask("q_pct",
                    expected={"type": "flat", "kind": "change", "tolerance": "ratio_pp:5"},
                    reference="-12.7%")
    assert rp["status"] == "parse_failed" and rp["value"] is None, rp
    assert "unit_pct_suspect" in rp["flags"], rp

    # --- amount 无 reference：只取绝对部分，不以答案自身定 5%
    tol_amt, tol_st = parse_tolerance({"tolerance": "amount:50,0.05"}, None)
    assert (tol_amt, tol_st) == (50.0, "ok_no_ref_pct"), (tol_amt, tol_st)

    # --- range 不该因为没有容差就触发 fallback
    qa_rg = DataQA(frames, chat_fn=lambda q: "5.0")
    rr = qa_rg.ask("q_rg", expected={"type": "range", "low": 1, "high": 10})
    assert rr["fallback"] is False and rr["verdict"] == "match", rr

    # --- 空表是「0 行」，不是解析失败
    qa_em = DataQA(frames, chat_fn=lambda q: pd.DataFrame())
    r_em = qa_em.ask("q_empty", expect_shape="table")
    assert r_em["status"] == "ok" and r_em["rows"] == [], r_em
    assert "empty_rows" in r_em["flags"], r_em

    print("selftest passed")


if __name__ == "__main__":
    _selftest()
