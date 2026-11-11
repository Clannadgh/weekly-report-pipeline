#!/usr/bin/env python3
"""S1 准入检查的本地 Web 界面。

仅用标准库 http.server，不依赖 Flask / FastAPI。
    python web/server.py [--port 8000]

路由：
    GET  /                  web/static/index.html
    GET  /api/health        {"ok": true}
    GET  /api/runs          runs/ 下含 intake.json 的历史运行
    GET  /api/runs/<tag>    该次运行的 {"intake": ..., "status": ...}
    POST /api/intake        上传文件 + 提示词，调用 pipeline.s1_intake.run_intake
"""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "web", "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")
ASSET_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".png": "image/png",
}
RUNS_DIR = os.path.join(ROOT, "runs")
UPLOAD_ROOT = os.path.join(RUNS_DIR, "_uploads")
CHAT_ROOT = os.path.join(RUNS_DIR, "_chat")
_SESSIONS: dict[str, Any] = {}
_SESSION_LOCK = threading.Lock()

MAX_BODY = 64 * 1024 * 1024          # 请求体上限 64MB
JSON_INDENT = 2

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def log(*parts: Any) -> None:
    """日志同时写 stdout，方便排查。"""
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    print("[" + stamp + "] [web]", *parts, flush=True)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def safe_name(raw: Any) -> str:
    """只取 basename，拒绝路径穿越与空名。"""
    text = str(raw if raw is not None else "").replace("\\", "/")
    text = text.split("/")[-1].strip()
    if text in ("", ".", "..") or text.startswith(".."):
        raise ValueError("非法文件名：" + repr(raw))
    text = _UNSAFE_CHARS.sub("_", text)
    if text in ("", ".", ".."):
        raise ValueError("非法文件名：" + repr(raw))
    return text


def unique_path(folder: str, name: str) -> str:
    """同名文件不覆盖，按 -2、-3 递增。"""
    target = os.path.join(folder, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    index = 2
    while True:
        candidate = os.path.join(folder, stem + "-" + str(index) + ext)
        if not os.path.exists(candidate):
            return candidate
        index += 1


def decode_base64(raw: Any) -> bytes:
    """兼容 data URI 前缀、换行与空白。"""
    text = str(raw if raw is not None else "")
    if text.startswith("data:"):
        comma = text.find(",")
        text = text[comma + 1:] if comma >= 0 else ""
    text = "".join(text.split())
    if not text:
        return b""
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64 解码失败：" + str(exc)) from exc


def read_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def try_read_json(path: str) -> dict:
    """读不到或格式不对就当没有，不编造。"""
    try:
        data = read_json_file(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def file_sha256(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def rel_to_root(path: str) -> str:
    try:
        return os.path.relpath(path, ROOT).replace(os.sep, "/")
    except ValueError:
        return path


def to_plain(obj: Any) -> dict:
    """把 pydantic 模型 / 普通对象 / dict 统一成 dict。"""
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        func = getattr(obj, attr, None)
        if callable(func):
            try:
                data = func()
            except TypeError:
                try:
                    data = func(mode="json")
                except Exception:  # noqa: BLE001 - 兜底换写法
                    continue
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict):
                return data
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not k.startswith("_")}
    raise TypeError("run_intake 返回了无法序列化的对象：" + type(obj).__name__)


class _Tee(io.TextIOBase):
    """把 run_intake 的 print 同时留在内存与 stdout。"""

    def __init__(self, buffer: io.StringIO) -> None:
        self._buffer = buffer

    def write(self, text: str) -> int:
        self._buffer.write(text)
        try:
            sys.__stdout__.write(text)
            sys.__stdout__.flush()
        except Exception:  # noqa: BLE001 - stdout 不可写时不影响主流程
            pass
        return len(text)

    def flush(self) -> None:
        try:
            sys.__stdout__.flush()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 调用 pipeline.s1_intake.run_intake
# --------------------------------------------------------------------------

def _load_run_intake():
    from pipeline.s1_intake import run_intake  # noqa: PLC0415 - 延迟导入，缺文件时给出可读错误

    return run_intake


_DIR_NAMES = ("run_dir", "out_dir", "output_dir", "workdir", "work_dir", "base_dir",
              "root", "root_dir", "project_root", "runs_dir", "target_dir")
_FILE_NAMES = ("files", "file_paths", "paths", "uploads", "input_files", "attachments",
               "data_files", "sources")


def _build_kwargs(func, prompt: str, file_paths: list[str], upload_dir: str,
                  logs: list[str]) -> dict:
    """按 run_intake 的签名挑参数名，签名不认识就退回位置参数。"""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return {}

    kwargs: dict[str, Any] = {}
    for name, param in params.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        lower = name.lower()
        if "prompt" in lower:
            kwargs[name] = prompt
        elif lower in _FILE_NAMES or ("file" in lower and "path" in lower):
            kwargs[name] = list(file_paths)
        elif lower in ("run_root", "runs_dir", "runs_root"):
            kwargs[name] = RUNS_DIR
        elif lower in _DIR_NAMES:
            if "runs" in lower:
                kwargs[name] = RUNS_DIR
            elif lower in ("root", "root_dir", "project_root", "base_dir"):
                kwargs[name] = ROOT
            else:
                kwargs[name] = upload_dir
        elif "log" in lower and param.default is not inspect.Parameter.empty:
            kwargs[name] = logs
    return kwargs


def call_run_intake(prompt: str, file_paths: list[str], upload_dir: str,
                    logs: list[str]) -> dict:
    func = _load_run_intake()
    kwargs = _build_kwargs(func, prompt, file_paths, upload_dir, logs)
    if kwargs:
        log("调用 run_intake，参数：" + ", ".join(sorted(kwargs)))
    else:
        log("调用 run_intake（位置参数）")
    try:
        if kwargs:
            result = func(**kwargs)
        else:
            result = func(prompt, file_paths)
    except TypeError as exc:
        # 签名判断错了才回退，避免把内部 TypeError 当成参数问题
        if "argument" not in str(exc) and "positional" not in str(exc):
            raise
        log("按签名调用失败，改用位置参数：" + str(exc))
        result = func(prompt, file_paths)
    return _normalize_result(result, logs)


def _normalize_result(result: Any, logs: list[str]) -> dict:
    """run_intake 允许返回 IntakeResult，也允许返回 (IntakeResult, Status, log)。"""
    if isinstance(result, tuple):
        head = result[0] if result else {}
        if len(result) > 2 and isinstance(result[2], list):
            logs.extend(str(item) for item in result[2])
        return to_plain(head)
    return to_plain(result)


# --------------------------------------------------------------------------
# runs 目录
# --------------------------------------------------------------------------

def list_runs() -> list[dict]:
    if not os.path.isdir(RUNS_DIR):
        return []
    items: list[dict] = []
    for entry in sorted(os.listdir(RUNS_DIR)):
        folder = os.path.join(RUNS_DIR, entry)
        if entry.startswith("_") or not os.path.isdir(folder):
            continue
        intake_path = os.path.join(folder, "intake.json")
        if not os.path.isfile(intake_path):
            continue
        intake = try_read_json(intake_path)
        status = try_read_json(os.path.join(folder, "status.json"))
        at = str(status.get("at") or "")
        if not at:
            try:
                mtime = os.path.getmtime(intake_path)
                at = dt.datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
            except OSError:
                at = ""
        run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
        items.append({
            "period_tag": str(run.get("period_tag") or entry),
            "dir": rel_to_root(folder),
            "state": str(intake.get("state") or status.get("state") or ""),
            "at": at,
        })
    items.sort(key=lambda item: (item["at"], item["period_tag"]), reverse=True)
    return items


def resolve_run_dir(tag: str) -> str | None:
    """tag -> 运行目录绝对路径；越界或不存在返回 None。"""
    name = unquote(str(tag or "")).strip().replace("\\", "/")
    if name.startswith("runs/"):
        name = name[len("runs/"):]
    name = name.strip("/")
    if not name or "/" in name or name in (".", "..") or name.startswith(".."):
        return None
    folder = os.path.abspath(os.path.join(RUNS_DIR, name))
    if os.path.commonpath([folder, os.path.abspath(RUNS_DIR)]) != os.path.abspath(RUNS_DIR):
        return None
    return folder if os.path.isdir(folder) else None


def load_run(tag: str) -> dict | None:
    folder = resolve_run_dir(tag)
    if folder is None:
        return None
    intake_path = os.path.join(folder, "intake.json")
    if not os.path.isfile(intake_path):
        return None
    return {
        "intake": try_read_json(intake_path),
        "status": try_read_json(os.path.join(folder, "status.json")),
    }


def build_pipeline_stages(tag: str) -> dict | None:
    """按运行目录里的产物汇总 S1–S6 状态，供对话页恢复会话时回填流水线。"""
    folder = resolve_run_dir(tag)
    if folder is None:
        return None
    intake_path = os.path.join(folder, "intake.json")
    if not os.path.isfile(intake_path):
        return None
    intake = try_read_json(intake_path)
    status = try_read_json(os.path.join(folder, "status.json"))
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    run_id = str(run.get("period_tag") or os.path.basename(folder))
    stages: dict[str, dict] = {}

    state = str(intake.get("state") or "")
    if state:
        stages["s1"] = {"state": state, "run": run_id, "ok": state == "准入通过"}

    cards_path = os.path.join(folder, "cards.json")
    if os.path.isfile(cards_path):
        cards = try_read_json(cards_path)
        cards_state = str(cards.get("state") or "ok")
        stages["s2"] = {"state": cards_state, "run": run_id,
                        "ok": cards_state == "ok"}

    issues_path = os.path.join(folder, "issues.json")
    if os.path.isfile(issues_path):
        from pipeline.approvals import load as load_approvals, tally

        issues = try_read_json(issues_path)
        issues_state = str(issues.get("state") or "")
        blocking = [str(item.get("id")) for item in (issues.get("issues") or [])
                    if isinstance(item, dict) and item.get("level") == "阻断"
                    and item.get("id")]
        verdict = tally(load_approvals(folder).get("s3"), blocking)
        if issues_state == "ok" and not verdict["approved"]:
            label = "待裁决"
        elif issues_state == "ok":
            label = "已核对"
        elif issues_state == "blocked":
            label = "对账未完成"
        else:
            label = issues_state or "已运行"
        stages["s3"] = {"state": label, "run": run_id,
                        "ok": issues_state == "ok" and verdict["approved"],
                        "blocking": len(blocking), "pending": verdict["pending"]}

    corrected_path = os.path.join(folder, "corrected.json")
    if os.path.isfile(corrected_path):
        corrected = try_read_json(corrected_path)
        blocked = int((corrected.get("stats") or {}).get("blocked") or 0)
        stages["s4"] = {"state": "待补充" if blocked else "可扩写",
                        "run": run_id, "ok": not blocked}

    status_stage = str(status.get("stage") or "")
    status_state = str(status.get("state") or "")
    draft_path = os.path.join(folder, "draft.md")
    if os.path.isfile(draft_path):
        qc = try_read_json(os.path.join(folder, "qc_result.json"))
        qc_verdict = str(qc.get("verdict") or "")
        qc_hash = str(qc.get("draft_after_sha256") or "")
        qc_fresh = bool(qc_hash) and qc_hash == file_sha256(draft_path)
        if (qc_fresh and qc_verdict == "FAIL"
                and status_state in ("", "可扩写", "已生成")):
            s5_state = "质检未通过"
        elif status_stage in ("s5", "s6") and status_state:
            s5_state = status_state
        elif qc_fresh and qc_verdict == "PASS":
            s5_state = "待审核"
        else:
            s5_state = "未送审"
        stages["s5"] = {"state": s5_state, "run": run_id,
                        "ok": s5_state in ("已通过", "已交付")}
    if status_stage == "s6" and status_state == "已交付":
        stages["s6"] = {"state": "已交付", "run": run_id, "ok": True}

    return {"ok": True, "run_id": run_id, "stages": stages}


# ---------------------------------------------------------------- S2 纪要拆卡

def list_s2_runs() -> list[dict]:
    """只列 S1 已准入通过的运行，附上是否已经拆过卡。"""
    if not os.path.isdir(RUNS_DIR):
        return []
    items: list[dict] = []
    for entry in sorted(os.listdir(RUNS_DIR)):
        folder = os.path.join(RUNS_DIR, entry)
        if entry.startswith("_") or not os.path.isdir(folder):
            continue
        intake = try_read_json(os.path.join(folder, "intake.json"))
        if not intake or intake.get("state") != "准入通过":
            continue
        run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
        minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
        files = intake.get("files") or []
        minutes_name = ""
        for info in files:
            if isinstance(info, dict) and info.get("role") == "纪要":
                minutes_name = str(info.get("name") or "")
                break
        cards_path = os.path.join(folder, "cards.json")
        cards = try_read_json(cards_path) if os.path.isfile(cards_path) else {}
        status = try_read_json(os.path.join(folder, "status.json"))
        items.append({
            "run_id": str(run.get("period_tag") or entry),
            "dir": str(run.get("dir") or entry),
            "at": str(status.get("at") or ""),
            "period": (str(minutes.get("period_start") or "") + " 至 "
                       + str(minutes.get("period_end") or "")).strip(" 至"),
            "compare": (str(minutes.get("compare_start") or "") + " 至 "
                        + str(minutes.get("compare_end") or "")).strip(" 至"),
            "minutes_file": minutes_name,
            "cards": int((cards.get("stats") or {}).get("cards") or 0),
            "has_cards": bool(cards),
        })
    items.sort(key=lambda item: item["at"], reverse=True)
    return items


def list_chat_sessions() -> list[dict]:
    """列出落盘过的对话。按更新时间倒序。"""
    items: list[dict] = []
    if not os.path.isdir(CHAT_ROOT):
        return items
    for entry in os.listdir(CHAT_ROOT):
        folder = os.path.join(CHAT_ROOT, entry)
        path = os.path.join(folder, "session.json")
        if not os.path.isfile(path):
            continue
        data = try_read_json(path)
        if not data.get("id"):
            continue
        messages = data.get("messages") or []
        preview = ""
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user":
                preview = str(message.get("content") or "")[:60]
                break
        items.append({
            "session_id": data.get("id"),
            "created_at": data.get("created_at") or "",
            "updated_at": data.get("updated_at") or "",
            "files": data.get("files") or [],
            "turns": sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user"),
            "preview": preview,
            "folder": entry,
        })
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return items


def find_chat_folder(session_id: str) -> str | None:
    if not os.path.isdir(CHAT_ROOT):
        return None
    for entry in os.listdir(CHAT_ROOT):
        folder = os.path.join(CHAT_ROOT, entry)
        if os.path.isfile(os.path.join(folder, "session.json")):
            data = try_read_json(os.path.join(folder, "session.json"))
            if str(data.get("id") or "") == session_id:
                return folder
    return None


def public_messages(session: Any) -> list[dict]:
    """给前端渲染用：只保留有正文的用户与助手消息。"""
    out: list[dict] = []
    for message in session.messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        out.append({"role": role, "text": text.strip()})
    return out


def list_s3_runs() -> list[dict]:
    """只列已经拆过卡的运行；带 issues.json 的标出是否已对账。"""
    if not os.path.isdir(RUNS_DIR):
        return []
    items: list[dict] = []
    for entry in sorted(os.listdir(RUNS_DIR)):
        folder = os.path.join(RUNS_DIR, entry)
        if entry.startswith("_") or not os.path.isdir(folder):
            continue
        cards_path = os.path.join(folder, "cards.json")
        if not os.path.isfile(cards_path):
            continue
        cards = try_read_json(cards_path)
        if cards.get("state") != "ok":
            continue
        intake = try_read_json(os.path.join(folder, "intake.json"))
        run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
        minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
        issues_path = os.path.join(folder, "issues.json")
        issues = try_read_json(issues_path) if os.path.isfile(issues_path) else {}
        stats = issues.get("stats") if isinstance(issues.get("stats"), dict) else {}
        items.append({
            "run_id": str(run.get("period_tag") or entry),
            "dir": str(run.get("dir") or entry),
            "period": (str(minutes.get("period_start") or "") + " 至 "
                       + str(minutes.get("period_end") or "")).strip(" 至"),
            "cards": int((cards.get("stats") or {}).get("cards") or 0),
            "checkable": int((cards.get("stats") or {}).get("checkable") or 0),
            "has_issues": bool(issues),
            "blocking": int(stats.get("blocking") or 0),
            "checked": int(stats.get("checked") or 0),
        })
    items.sort(key=lambda item: item["run_id"], reverse=True)
    return items


def list_s4_runs() -> list[dict]:
    """只列 S3 已经对过账（cards.json 可用）的运行，附上改稿与结论进度。"""
    if not os.path.isdir(RUNS_DIR):
        return []
    from pipeline.approvals import load as load_approvals, tally

    items: list[dict] = []
    for entry in sorted(os.listdir(RUNS_DIR)):
        folder = os.path.join(RUNS_DIR, entry)
        if entry.startswith("_") or not os.path.isdir(folder):
            continue
        intake_path = os.path.join(folder, "intake.json")
        if not os.path.isfile(intake_path):
            continue
        cards_path = os.path.join(folder, "cards.json")
        if not os.path.isfile(cards_path):
            continue
        cards = try_read_json(cards_path)
        if cards.get("state") != "ok":
            continue
        intake = try_read_json(intake_path)
        if intake.get("state") != "准入通过":
            continue
        run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
        minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
        issues_path = os.path.join(folder, "issues.json")
        issues = try_read_json(issues_path) if os.path.isfile(issues_path) else {}
        stats = issues.get("stats") if isinstance(issues.get("stats"), dict) else {}
        blocking_ids = [str(item.get("id")) for item in (issues.get("issues") or [])
                        if isinstance(item, dict) and item.get("level") == "阻断" and item.get("id")]
        approvals = load_approvals(folder)
        state = tally(approvals.get("s3"), blocking_ids)
        status = try_read_json(os.path.join(folder, "status.json"))
        items.append({
            "run_id": str(run.get("period_tag") or entry),
            "dir": str(run.get("dir") or entry),
            "period": (str(minutes.get("period_start") or "") + " 至 "
                       + str(minutes.get("period_end") or "")).strip(" 至"),
            "cards": int((cards.get("stats") or {}).get("cards") or 0),
            "checkable": int((cards.get("stats") or {}).get("checkable") or 0),
            "has_issues": bool(issues),
            "blocking": int(stats.get("blocking") or 0),
            "decided": len(state["accepted"]) + len(state["rejected"]),
            "has_corrected": os.path.isfile(os.path.join(folder, "corrected.json")),
            "state": str(status.get("state") or ""),
            "at": str(status.get("at") or ""),
        })
    items.sort(key=lambda item: (item["at"], item["run_id"]), reverse=True)
    return items


def _figure_version(folder: str, name: str) -> str:
    """文件名不变时用 mtime+size 做版本号，避免浏览器拿旧图。"""
    try:
        info = os.stat(os.path.join(folder, "figures", name))
    except OSError:
        return ""
    return str(int(info.st_mtime)) + "-" + str(info.st_size)


def s5_figures_for_api(manifest: dict, tag: str, folder: str = "") -> list[dict]:
    """把图表清单里的图补上可直接访问的 URL，GET 与 POST 两种响应共用同一形状。"""
    out: list[dict] = []
    base = "/api/s5/" + quote(tag, safe="") + "/figure/"
    for item in (manifest.get("figures") or []) if isinstance(manifest, dict) else []:
        files = item.get("files") if isinstance(item.get("files"), dict) else {}
        png = os.path.basename(str(files.get("png") or ""))
        svg = os.path.basename(str(files.get("svg") or ""))
        png_ver = _figure_version(folder, png) if png else ""
        svg_ver = _figure_version(folder, svg) if svg else ""
        renderer = str(item.get("renderer") or ("matplotlib" if svg else "pandasai"))
        out.append({
            "number": item.get("number"), "title": item.get("title"),
            "caption": item.get("caption"), "renderer": renderer,
            "model": item.get("model") or "",
            "quality": item.get("quality") or {},
            "flags": item.get("flags") or [],
            "png": (base + quote(png, safe="") + (("?v=" + png_ver) if png_ver else "")) if png else "",
            "svg": (base + quote(svg, safe="") + (("?v=" + svg_ver) if svg_ver else "")) if svg else "",
            "keys": item.get("keys") or [],
            "bind_key": "、".join(item.get("keys") or []),
            "script": item.get("script") or "",
            "script_sha256": item.get("script_sha256") or "",
        })
    return out


def s6_delivery_for_api(tag: str, folder: str) -> dict:
    """S6 交付包：版本、最新清单与带下载 URL 的文件列表。"""
    try:
        from pipeline import s6_deliver

        data = s6_deliver.read_status(folder)
    except Exception as exc:  # noqa: BLE001 - 没有交付包不是错误
        log("读取 S6 交付状态失败：" + repr(exc))
        return {"versions": [], "manifest": {}, "files": []}
    versions = data.get("versions") or []
    manifest = data.get("manifest") or {}
    latest = versions[-1] if versions else None
    files: list[dict] = []
    if latest:
        root = os.path.join(folder, "deliver", latest["version"])
        base = ("/api/s6/" + quote(tag, safe="") + "/deliver-file/"
                + latest["version"] + "/")
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                path = os.path.join(root, name)
                if os.path.isfile(path):
                    files.append({"name": name, "bytes": os.path.getsize(path),
                                  "url": base + quote(name, safe="")})
                elif os.path.isdir(path):
                    for sub in sorted(os.listdir(path)):
                        sub_path = os.path.join(path, sub)
                        if os.path.isfile(sub_path):
                            files.append({
                                "name": name + "/" + sub,
                                "bytes": os.path.getsize(sub_path),
                                "url": base + quote(name + "/" + sub, safe=""),
                            })
    return {"versions": versions, "manifest": manifest, "files": files}


def list_s5_runs() -> list[dict]:
    """只列 S4 已交稿（corrected.json 可用）的运行，附上正文进度。"""
    if not os.path.isdir(RUNS_DIR):
        return []
    items: list[dict] = []
    for entry in sorted(os.listdir(RUNS_DIR)):
        folder = os.path.join(RUNS_DIR, entry)
        if entry.startswith("_") or not os.path.isdir(folder):
            continue
        intake_path = os.path.join(folder, "intake.json")
        if not os.path.isfile(intake_path):
            continue
        intake = try_read_json(intake_path)
        if intake.get("state") != "准入通过":
            continue
        if not os.path.isfile(os.path.join(folder, "corrected.json")):
            continue
        if not os.path.isfile(os.path.join(folder, "ledger.json")):
            continue
        run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
        minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
        status = try_read_json(os.path.join(folder, "status.json"))
        state = str(status.get("state") or "")
        can_write = state in ("可扩写", "已生成", "待审核", "退回修改")
        draft_path = os.path.join(folder, "draft.md")
        has_draft = os.path.isfile(draft_path)
        facts = try_read_json(os.path.join(folder, "s5_facts.json")) if has_draft else {}
        stats = facts.get("stats") if isinstance(facts.get("stats"), dict) else {}
        manifest = try_read_json(os.path.join(folder, "figures_manifest.json"))
        record = try_read_json(os.path.join(folder, "review_record.json"))
        pages = stats.get("pages_estimate")
        items.append({
            "run_id": str(run.get("period_tag") or entry),
            "dir": str(run.get("dir") or entry),
            "period": (str(minutes.get("period_start") or "") + " 至 "
                       + str(minutes.get("period_end") or "")).strip(" 至"),
            "week": str(minutes.get("week") or ""),
            "state": state,
            "can_write": can_write,
            "has_draft": has_draft,
            "figures": len(manifest.get("figures") or []),
            "chars": int(stats.get("chars_body_rendered") or 0),
            "pages": float(pages.get("value") or 0) if isinstance(pages, dict) else 0,
            "round": int(record.get("round") or 0),
            "reviewer": str(record.get("reviewer") or ""),
            "signature": str(record.get("signature") or ""),
            "decision": str(record.get("decision") or ""),
            "paused": bool(record.get("paused")),
            "has_record": bool(record),
            "at": str(status.get("at") or ""),
        })
    items.sort(key=lambda item: (item["at"], item["run_id"]), reverse=True)
    return items


def list_s6_runs() -> list[dict]:
    """S6 只列 S5 已经出稿的运行；是否可交付由状态决定。"""
    return [item for item in list_s5_runs() if item.get("has_draft")]


def status_for_intake(intake: dict) -> dict:
    """POST 之后回读 status.json；还没落盘就返回空对象，不替 S1 编状态。"""
    run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
    run_dir = str(run.get("dir") or "")
    if not run_dir:
        return {}
    folder = resolve_run_dir(run_dir)
    if folder is None:
        return {}
    return try_read_json(os.path.join(folder, "status.json"))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "S1IntakeWeb/1.0"
    protocol_version = "HTTP/1.1"

    # -- 输出 ------------------------------------------------------------
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect_to(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=JSON_INDENT).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def send_error_json(self, code: int, error: str, detail: str = "") -> None:
        self.send_json({"ok": False, "error": error, "detail": detail}, code)

    def send_stream_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def write_sse(self, payload: dict) -> None:
        body = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()

    def log_message(self, fmt: str, *args: Any) -> None:  # 只留一行，别刷屏
        log("%s %s" % (self.address_string(), fmt % args))

    # -- 路由 ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self.serve_index()
            elif path in ("/chat", "/chat.html"):
                self.serve_static("chat.html")
            elif path in ("/s2", "/s2.html"):
                self.serve_static("s2.html")
            elif path in ("/s3", "/s3.html"):
                self.serve_static("s3.html")
            elif path in ("/s4", "/s4.html"):
                self.serve_static("s4.html")
            elif path in ("/s5", "/s5.html"):
                self.serve_static("s5.html")
            elif path in ("/s6", "/s6.html"):
                self.serve_static("s6.html")
            elif path in ("/s7", "/s7.html", "/s8", "/s8.html"):
                # 旧交付页链接统一跳到 S6
                query = urlparse(self.path).query
                self.redirect_to("/s6.html" + ("?" + query if query else ""))
            elif path.startswith("/static/"):
                self.serve_asset(path[len("/static/"):])
            elif path == "/api/s4/runs":
                self.send_json({"ok": True, "runs": list_s4_runs()})
            elif path.startswith("/api/s4/"):
                self.serve_s4_result(path[len("/api/s4/"):])
            elif path == "/api/s5/runs":
                self.send_json({"ok": True, "runs": list_s5_runs()})
            elif path.startswith("/api/s5/"):
                rest = path[len("/api/s5/"):]
                if rest.endswith("/questions"):
                    self.serve_s5_questions(rest[:-len("/questions")])
                elif rest.endswith("/history"):
                    self.serve_s5_history(rest[:-len("/history")])
                elif "/history/" in rest:
                    tag, _, remainder = rest.partition("/history/")
                    if "/figure/" in remainder:
                        stamp, _, name = remainder.partition("/figure/")
                        self.serve_s5_history_figure(tag, stamp, name)
                    elif remainder:
                        self.serve_s5_history_detail(tag, remainder)
                    else:
                        self.serve_s5_history(tag)
                elif "/figure/" in rest:
                    tag, _, name = rest.partition("/figure/")
                    self.serve_s5_figure(tag, name)
                else:
                    self.serve_s5_result(rest)
            elif path == "/api/s6/runs":
                self.send_json({"ok": True, "runs": list_s6_runs()})
            elif path.startswith("/api/s6/"):
                rest = path[len("/api/s6/"):]
                if "/deliver-file/" in rest:
                    tag, _, tail = rest.partition("/deliver-file/")
                    self.serve_s6_delivery_file(tag, tail)
                else:
                    self.serve_s6_result(rest)
            elif path == "/api/s3/runs":
                self.send_json({"ok": True, "runs": list_s3_runs()})
            elif path.startswith("/api/s3/"):
                self.serve_s3_result(path[len("/api/s3/"):])
            elif path.startswith("/api/pipeline/"):
                self.serve_pipeline_stages(path[len("/api/pipeline/"):])
            elif path == "/api/s2/runs":
                self.send_json({"ok": True, "runs": list_s2_runs()})
            elif path.startswith("/api/s2/"):
                self.serve_s2_cards(path[len("/api/s2/"):])
            elif path == "/api/health":
                self.send_json({"ok": True})
            elif path == "/api/llm":
                self.serve_llm_info()
            elif path == "/api/chat/history":
                self.send_json({"ok": True, "sessions": list_chat_sessions()})
            elif path.startswith("/api/chat/history/"):
                self.handle_chat_restore(path[len("/api/chat/history/"):])
            elif path == "/api/chat/progress":
                self.handle_chat_progress(urlparse(self.path).query)
            elif path == "/api/runs":
                self.send_json({"runs": list_runs()})
            elif path.startswith("/api/runs/") and path.endswith("/approvals"):
                self.handle_approvals(path[len("/api/runs/"):-len("/approvals")])
            elif path.startswith("/api/runs/"):
                self.serve_run(path[len("/api/runs/"):])
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self.send_error_json(404, "未知路径：" + path)
        except Exception as exc:  # noqa: BLE001 - 服务不因单次请求崩掉
            log("GET 失败：" + repr(exc))
            self.send_error_json(500, "服务器内部错误", traceback.format_exc())

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def serve_index(self) -> None:
        try:
            with open(INDEX_HTML, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            self.send_error_json(500, "缺少 web/static/index.html", str(exc))
            return
        self._send(200, body, "text/html; charset=utf-8")

    def serve_static(self, name: str) -> None:
        target = os.path.join(STATIC_DIR, name)
        if os.path.basename(name) != name or not os.path.isfile(target):
            self.send_error_json(404, "找不到页面：" + name)
            return
        with open(target, "rb") as handle:
            body = handle.read()
        self._send(200, body, "text/html; charset=utf-8")

    def serve_asset(self, name: str) -> None:
        """静态资源：只允许白名单扩展名，且必须落在 static 目录内。"""
        name = unquote(name)
        ext = os.path.splitext(name)[1].lower()
        root = os.path.realpath(STATIC_DIR)
        target = os.path.realpath(os.path.join(STATIC_DIR, name))
        try:
            inside = os.path.commonpath([root, target]) == root
        except ValueError:
            inside = False
        if not name or not inside or ext not in ASSET_TYPES or not os.path.isfile(target):
            self.send_error_json(404, "找不到资源：" + name)
            return
        with open(target, "rb") as handle:
            body = handle.read()
        self._send(200, body, ASSET_TYPES[ext])

    def handle_waive(self, tag: str) -> None:
        """登记人工豁免/人工放权并重跑 S1；不改判定规则，只记录人的决定。"""
        payload = self.read_json_body()
        if payload is None:
            return
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        items = [str(x).strip() for x in (payload.get("items") or []) if str(x).strip()]
        override = bool(payload.get("override"))
        if not items and not override:
            self.send_error_json(400, "没有给要豁免的冲突关键词", "")
            return
        reason = (str(payload.get("reason") or "").strip()
                  or ("用户确认人工放权" if override else "用户确认可忽略"))
        waiver = {
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "by": str(payload.get("by") or "用户"),
            "items": items,
            "reason": reason,
            "override": override,
            "used": False,
        }
        try:
            with open(os.path.join(folder, "waiver.json"), "w", encoding="utf-8") as fh:
                json.dump(waiver, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.send_error_json(500, "写 waiver.json 失败", str(exc))
            return

        # 用 inputs/ 里的原始副本重跑，保证和上次读的是同一批文件
        intake_path = os.path.join(folder, "intake.json")
        intake = try_read_json(intake_path)
        prompt = ""
        prompt_file = os.path.join(folder, "prompt.txt")
        if os.path.isfile(prompt_file):
            with open(prompt_file, encoding="utf-8") as fh:
                prompt = fh.read().strip()
        if not prompt:
            prompt = str((intake.get("prompt") or {}).get("goal") or "").strip()
        inputs_dir = os.path.join(folder, "inputs")
        files = []
        if os.path.isdir(inputs_dir):
            files = [os.path.join(inputs_dir, name) for name in sorted(os.listdir(inputs_dir))
                     if os.path.isfile(os.path.join(inputs_dir, name))]
        if not files or not prompt:
            self.send_error_json(400, "缺少原始输入或提示词，无法重跑 S1",
                                 "inputs 目录为空或 prompt.txt 缺失")
            return

        try:
            from pipeline.s1_intake import run_intake

            result, _status, logs = run_intake([Path(p) for p in files], prompt,
                                               Path(RUNS_DIR))
        except Exception as exc:  # noqa: BLE001
            log("豁免后重跑 S1 失败：" + repr(exc))
            self.send_error_json(500, "重跑 S1 失败", traceback.format_exc())
            return
        log(("人工放权" if override else "人工豁免 " + ",".join(items)) + " → " + result.state)
        if override:
            waiver_used = dict(waiver)
            waiver_used["used"] = True
            try:
                with open(os.path.join(folder, "waiver.json"), "w", encoding="utf-8") as fh:
                    json.dump(waiver_used, fh, ensure_ascii=False, indent=2)
                waiver["used"] = True
            except OSError as exc:
                log("标记放权已使用失败：" + str(exc))

        note = ""
        session_id = str(payload.get("session_id") or "").strip()
        if override and session_id:
            with _SESSION_LOCK:
                session = _SESSIONS.get(session_id)
            if session is not None:
                note = ("已人工放权：S1 现在按准入通过处理，可以继续 S2。"
                        "原始问题仍保留在运行详情里。")
                try:
                    session.messages.append({"role": "assistant", "content": note})
                    session.last_run = (result.run.period_tag or session.last_run)
                    session.save()
                except Exception as exc:  # noqa: BLE001
                    log("写入会话放权提示失败：" + repr(exc))

        status = try_read_json(os.path.join(folder, "status.json"))
        self.send_json({"ok": True, "intake": to_plain(result), "status": status,
                        "waiver": waiver, "note": note, "log": logs})

    def handle_approve(self, tag: str) -> None:
        """记录用户对 S3 阻断建议的结论：accept＝批准，reject＝拒绝。"""
        payload = self.read_json_body()
        if payload is None:
            return
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        stage = str(payload.get("stage") or "s3").strip().lower()
        if stage != "s3":
            self.send_error_json(400, "现在只支持处理 S3 的阻断建议", "")
            return
        issues = try_read_json(os.path.join(folder, "issues.json"))
        if not issues:
            self.send_error_json(400, "还没有 S3 对账结果，先跑 S3", "")
            return
        blocking = [str(item.get("id")) for item in (issues.get("issues") or [])
                    if isinstance(item, dict) and item.get("level") == "阻断" and item.get("id")]
        if not blocking:
            self.send_error_json(400, "当前没有阻断，无需处理", "")
            return
        action = str(payload.get("action") or "accept").strip().lower()
        if action not in ("accept", "reject"):
            self.send_error_json(400, "action 只能是 accept / reject", "")
            return
        issue_id = str(payload.get("issue_id") or "").strip()
        note = str(payload.get("note") or "")

        from pipeline.approvals import (decide, decide_all, load as load_approvals,
                                        tally)

        before = tally(load_approvals(folder).get("s3"), blocking)
        if not issue_id and not before["pending"]:
            self.send_error_json(400, "阻断都已处理", "")
            return
        try:
            if issue_id:
                entry = decide(folder, issue_id, action, blocking, note=note)
            else:
                entry = decide_all(folder, blocking, decision=action, note=note)
        except ValueError as exc:
            self.send_error_json(400, str(exc), "")
            return
        except OSError as exc:
            self.send_error_json(500, "写 approval.json 失败", str(exc))
            return

        approvals = load_approvals(folder)
        state = tally(approvals.get("s3"), blocking)
        verb = "批准" if action == "accept" else "拒绝"
        if issue_id:
            session_note = "已" + verb + " " + issue_id + " 的建议。"
        else:
            session_note = "已" + verb + " " + str(len(before["pending"])) + " 条阻断建议。"
        if state["pending"]:
            session_note += "还有 " + str(len(state["pending"])) + " 条待处理。"
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            with _SESSION_LOCK:
                session = _SESSIONS.get(session_id)
            if session is not None:
                try:
                    session.messages.append({"role": "assistant", "content": session_note})
                    session.last_run = tag or session.last_run
                    session.save()
                except Exception as exc:  # noqa: BLE001
                    log("写入批准提示失败：" + repr(exc))
        log("S3 阻断结论：" + tag + " " + session_note)
        self.send_json({"ok": True, "run_id": tag, "approvals": approvals,
                        "approval": entry, "blocking": blocking,
                        "decisions": state, "note": session_note})

    def handle_approvals(self, tag: str) -> None:
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        from pipeline.approvals import load as load_approvals

        self.send_json({"ok": True, "run_id": tag, "approvals": load_approvals(folder)})

    def serve_s4_result(self, run_id: str) -> None:
        """读已落盘的 corrected.json / status.json，不现场重算。"""
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        path = os.path.join(folder, "corrected.json")
        if not os.path.isfile(path):
            self.send_json({"ok": False, "run_id": tag, "error": "这次运行还没修过纪要"})
            return
        self.send_json({"ok": True, "run_id": tag,
                        "corrected": try_read_json(path),
                        "status": try_read_json(os.path.join(folder, "status.json"))})

    def handle_s4(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            self.send_error_json(400, "缺少 run_id", "")
            return
        folder = resolve_run_dir(run_id)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + run_id)
            return
        try:
            from pipeline.s4_draft import run_s4

            log("S4 开始：" + folder)
            outcome = run_s4(folder)
            result = outcome.to_dict() if hasattr(outcome, "to_dict") else outcome
        except FileNotFoundError as exc:
            self.send_error_json(400, str(exc), "")
            return
        except Exception as exc:  # noqa: BLE001 - 失败原因交给前端
            log("S4 失败：" + repr(exc))
            self.send_error_json(500, "S4 执行失败", traceback.format_exc())
            return
        log("S4 完成：" + str(result.get("state") or ""))
        self.send_json(result)

    def serve_s6_delivery_file(self, run_id: str, tail: str) -> None:
        """读交付包里的单个文件（docx / md / 图）；拒绝越界路径。"""
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        parts = [unquote(item) for item in tail.split("/") if item]
        if len(parts) < 2 or any(item in (".", "..") for item in parts):
            self.send_error_json(400, "路径不合法")
            return
        version = parts[0]
        if not re.fullmatch(r"v\d+", version):
            self.send_error_json(400, "版本号不合法")
            return
        target = os.path.join(folder, "deliver", version, *parts[1:])
        root = os.path.realpath(os.path.join(folder, "deliver", version))
        real = os.path.realpath(target)
        if not (real == root or real.startswith(root + os.sep)) or not os.path.isfile(real):
            self.send_error_json(404, "文件不存在")
            return
        ctype = ASSET_TYPES.get(os.path.splitext(real)[1].lower())
        if not ctype:
            ctype = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                     if os.path.splitext(real)[1].lower() == ".docx"
                     else "application/octet-stream")
        try:
            with open(real, "rb") as handle:
                data = handle.read()
        except OSError:
            self.send_error_json(500, "读取文件失败")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         "inline; filename*=UTF-8''" + quote(parts[-1], safe=""))
        self.end_headers()
        self.wfile.write(data)

    def handle_s6_deliver(self, run_id: str) -> None:
        self.discard_body()
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        try:
            from pipeline import s6_deliver

            log("S6 交付：" + folder)
            outcome = s6_deliver.deliver(folder)
        except Exception as exc:  # noqa: BLE001
            log("S6 交付失败：" + repr(exc))
            self.send_error_json(500, "S6 交付失败", traceback.format_exc())
            return
        self.send_json(outcome.to_dict())

    def handle_s5_submit(self, run_id: str) -> None:
        self.discard_body()
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        try:
            from pipeline import s5_review

            log("S5 送审：" + folder)
            outcome = s5_review.submit(folder)
        except FileNotFoundError as exc:
            self.send_error_json(400, str(exc), "")
            return
        except Exception as exc:  # noqa: BLE001
            log("S5 送审失败：" + repr(exc))
            self.send_error_json(500, "S5 送审失败", traceback.format_exc())
            return
        self.send_json(outcome.to_dict())

    def handle_s5_decision(self, run_id: str) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        reviewer = str(payload.get("reviewer") or "").strip()
        signature = str(payload.get("signature") or "").strip()
        decision = str(payload.get("decision") or "").strip().lower()
        comments = payload.get("comments")
        if not isinstance(comments, list):
            comments = []
        try:
            from pipeline import s5_review

            log("S5 审核：" + folder + " / " + decision)
            outcome = s5_review.decide(folder, reviewer, decision, comments,
                                       signature=signature)
        except FileNotFoundError as exc:
            self.send_error_json(400, str(exc), "")
            return
        except Exception as exc:  # noqa: BLE001
            log("S5 审核失败：" + repr(exc))
            self.send_error_json(500, "S5 审核失败", traceback.format_exc())
            return
        self.send_json(outcome.to_dict())

    def serve_s5_history(self, run_id: str) -> None:
        """列出某个运行的历史版本（新到旧）。"""
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        root = os.path.join(folder, "history")
        items: list[dict] = []
        if os.path.isdir(root):
            for name in sorted(os.listdir(root), reverse=True):
                target = os.path.join(root, name)
                if not os.path.isdir(target):
                    continue
                meta = try_read_json(os.path.join(target, "meta.json"))
                stats = meta.get("stats") if isinstance(meta.get("stats"), dict) else {}
                items.append({
                    "stamp": name,
                    "archived_at": str(meta.get("archived_at") or ""),
                    "state": str(meta.get("state") or ""),
                    "note": str(meta.get("note") or ""),
                    "figures": int(meta.get("figures") or 0),
                    "chars": int(stats.get("chars_body_rendered") or 0),
                })
        self.send_json({"ok": True, "run_id": tag, "items": items})

    def serve_s5_history_detail(self, run_id: str, stamp: str) -> None:
        """读某个历史版本的 draft / manifest / meta。"""
        tag = unquote(run_id)
        stamp = unquote(stamp)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        history = os.path.realpath(os.path.join(folder, "history"))
        target = os.path.realpath(os.path.join(history, stamp))
        try:
            inside = os.path.commonpath([history, target]) == history
        except ValueError:
            inside = False
        if not inside or not os.path.isdir(target):
            self.send_error_json(404, "找不到历史版本：" + stamp)
            return
        draft_path = os.path.join(target, "draft.md")
        if not os.path.isfile(draft_path):
            self.send_error_json(404, "历史版本缺少 draft.md")
            return
        try:
            text = Path(draft_path).read_text(encoding="utf-8")
        except OSError as exc:
            self.send_error_json(500, "读取历史 draft 失败", str(exc))
            return
        manifest = try_read_json(os.path.join(target, "figures_manifest.json"))
        meta = try_read_json(os.path.join(target, "meta.json"))
        base = ("/api/s5/" + quote(tag, safe="") + "/history/"
                + quote(stamp, safe="") + "/figure/")
        figures = []
        for fig in manifest.get("figures") or []:
            if not isinstance(fig, dict):
                continue
            files = fig.get("files") if isinstance(fig.get("files"), dict) else {}
            png = os.path.basename(str(files.get("png") or ""))
            svg = os.path.basename(str(files.get("svg") or ""))
            item = dict(fig)
            item["png"] = base + quote(png, safe="") if png else ""
            item["svg"] = base + quote(svg, safe="") if svg else ""
            figures.append(item)
        self.send_json({"ok": True, "run_id": tag, "stamp": stamp,
                        "draft": text, "manifest": manifest, "meta": meta,
                        "figures": figures})

    def serve_s5_history_figure(self, run_id: str, stamp: str, name: str) -> None:
        """读历史版本里的图片。"""
        tag = unquote(run_id)
        stamp = unquote(stamp)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录")
            return
        name = unquote(name or "")
        ext = os.path.splitext(name)[1].lower()
        if os.path.basename(name) != name or ext not in (".png", ".svg"):
            self.send_error_json(404, "找不到图片：" + name)
            return
        history = os.path.realpath(os.path.join(folder, "history"))
        root = os.path.realpath(os.path.join(history, stamp, "figures"))
        target = os.path.realpath(os.path.join(root, name))
        try:
            inside = os.path.commonpath([root, target]) == root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(target):
            self.send_error_json(404, "找不到图片：" + name)
            return
        with open(target, "rb") as handle:
            body = handle.read()
        self._send(200, body, "image/png" if ext == ".png" else "image/svg+xml")

    def serve_s6_result(self, run_id: str) -> None:
        """S6 交付页：审核记录、状态与交付包。"""
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        intake = try_read_json(os.path.join(folder, "intake.json"))
        run = intake.get("run") if isinstance(intake.get("run"), dict) else {}
        minutes = intake.get("minutes") if isinstance(intake.get("minutes"), dict) else {}
        title = ""
        try:
            for line in Path(folder, "draft.md").read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        except OSError:
            title = ""
        period = (str(minutes.get("period_start") or "") + " 至 "
                  + str(minutes.get("period_end") or "")).strip(" 至")
        self.send_json({
            "ok": True,
            "run_id": tag,
            "dir": str(run.get("dir") or folder),
            "week": str(minutes.get("week") or run.get("period_tag") or tag),
            "period": period,
            "title": title,
            "status": try_read_json(os.path.join(folder, "status.json")),
            "record": try_read_json(os.path.join(folder, "review_record.json")),
            "delivery": s6_delivery_for_api(tag, folder),
        })

    def serve_s5_result(self, run_id: str) -> None:
        """读已落盘的 draft.md / 图表清单 / 状态，不现场重算。"""
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        try:
            from pipeline.s5_write import is_running as s5_is_running

            running = s5_is_running()
        except Exception:  # noqa: BLE001 - 运行标记失败不挡页面
            running = False
        draft_path = os.path.join(folder, "draft.md")
        if not os.path.isfile(draft_path):
            self.send_json({"ok": False, "run_id": tag, "running": running,
                            "status": try_read_json(os.path.join(folder, "status.json")),
                            "error": "这次运行还没扩写"})
            return
        try:
            text = Path(draft_path).read_text(encoding="utf-8")
        except OSError as exc:
            self.send_error_json(500, "读取 draft.md 失败", str(exc))
            return
        manifest = try_read_json(os.path.join(folder, "figures_manifest.json"))
        facts = try_read_json(os.path.join(folder, "s5_facts.json"))
        profile = try_read_json(os.path.join(folder, "profile.json"))
        ledger = try_read_json(os.path.join(folder, "ledger.json"))

        stats: dict = {}
        try:
            from tools.text_stats import analyze, load_banned

            displays = {str(entry.get("key")): str(entry.get("display") or "")
                        for entry in ledger.get("entries") or [] if entry.get("key")}
            stats = analyze(text, load_banned(Path(ROOT) / "规范.md"),
                            stage="draft", ledger_displays=displays)
        except Exception as exc:  # noqa: BLE001 - 统计失败不挡页面
            log("S5 统计失败：" + repr(exc))
        discoveries = facts.get("discoveries") or []
        # 旧「热点」表与统计字段从发现派生，保持前端兼容
        hotspots = facts.get("hotspots") or [
            {"object": str(d.get("id") or ""), "metric": "数据发现",
             "change": d.get("display"), "impact": d.get("reason"),
             "written": True, "section": "数据发现补充"}
            for d in discoveries if isinstance(d, dict)]
        stats["hotspots"] = len(hotspots)
        stats["figures"] = len(manifest.get("figures") or [])

        figures = s5_figures_for_api(manifest, tag, folder)
        record: dict = {}
        materials: dict = {}
        try:
            from pipeline import s5_review

            record = s5_review.load_record(folder)
            materials = s5_review.build_materials(folder)
        except Exception as exc:  # noqa: BLE001 - 审核材料失败不挡正文
            log("读取 S5 审核材料失败：" + repr(exc))

        profile_summary = {}
        if isinstance(profile, dict) and profile:
            coverage = profile.get("coverage") if isinstance(profile.get("coverage"), dict) else {}
            profile_summary = {
                "ok": bool(profile.get("ok")),
                "rows": profile.get("n_rows"), "cols": profile.get("n_cols"),
                "coverage": coverage,
                "warnings": (profile.get("warnings") or [])[:6],
                "meta": profile.get("meta") or {},
            }
        self.send_json({
            "ok": True, "run_id": tag, "running": running,
            "status": try_read_json(os.path.join(folder, "status.json")),
            "qc": try_read_json(os.path.join(folder, "qc_result.json")),
            "draft": text, "stats": stats, "manifest": manifest,
            "figures": figures, "hotspots": hotspots,
            "discoveries": facts.get("discoveries") or [],
            "skeleton": facts.get("skeleton") or [],
            "profile_summary": profile_summary,
            "record": record, "materials": materials,
        })

    def serve_s5_questions(self, run_id: str) -> None:
        """读 S5 问题清单；没有清单时回退上一稿的 discoveries。"""
        tag = unquote(run_id or "")
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        try:
            from pipeline import s5_questions as sq

            data = sq.effective(folder)
        except Exception as exc:  # noqa: BLE001 - 清单损坏时把原因交给页面
            self.send_json({"ok": False, "run_id": tag, "questions": [],
                            "source": "error", "count": 0, "dirty": False,
                            "applied": False, "updated_at": "", "error": str(exc)})
            return
        payload = {"ok": True, "run_id": tag}
        payload.update(data)
        self.send_json(payload)

    def handle_s5_questions(self, run_id: str) -> None:
        """保存整份问题清单（页面编辑）或 reset 恢复自动出题。"""
        payload = self.read_json_body()
        if payload is None:
            return
        tag = unquote(run_id or "")
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        try:
            from pipeline.s5_write import is_running

            if is_running():
                self.send_error_json(409, "S5 正在运行", "等它结束后再改问题。")
                return
        except Exception:  # noqa: BLE001 - 运行标记失败不挡保存
            pass
        try:
            from pipeline import s5_questions as sq

            if payload.get("reset"):
                sq.reset_plan(folder)
                reset = True
            else:
                sq.save_plan(folder, payload.get("questions"),
                             source="user", run_id=tag)
                reset = False
            data = sq.effective(folder)
        except ValueError as exc:
            self.send_error_json(400, "问题清单无效", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log("保存问题清单失败：" + repr(exc))
            self.send_error_json(500, "保存问题清单失败", str(exc))
            return
        result = {"ok": True, "run_id": tag, "reset": reset}
        result.update(data)
        self.send_json(result)

    def serve_s5_figure(self, run_id: str, name: str) -> None:
        folder = resolve_run_dir(unquote(run_id))
        if folder is None:
            self.send_error_json(404, "找不到运行目录")
            return
        name = unquote(name or "")
        ext = os.path.splitext(name)[1].lower()
        if os.path.basename(name) != name or ext not in (".png", ".svg"):
            self.send_error_json(404, "找不到图片：" + name)
            return
        root = os.path.realpath(os.path.join(folder, "figures"))
        target = os.path.realpath(os.path.join(root, name))
        try:
            inside = os.path.commonpath([root, target]) == root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(target):
            self.send_error_json(404, "找不到图片：" + name)
            return
        with open(target, "rb") as handle:
            body = handle.read()
        self._send(200, body, "image/png" if ext == ".png" else "image/svg+xml")

    def handle_s5(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            self.send_error_json(400, "缺少 run_id", "")
            return
        folder = resolve_run_dir(run_id)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + run_id)
            return
        try:
            from pipeline.s5_write import run_s5

            full = bool(payload.get("full", True))
            log("S5 开始：" + folder + ("（全量）" if full else ""))
            outcome = run_s5(folder, full=full)
            result = outcome.to_dict() if hasattr(outcome, "to_dict") else outcome
        except FileNotFoundError as exc:
            self.send_error_json(400, str(exc), "")
            return
        except Exception as exc:  # noqa: BLE001 - 失败原因交给前端
            log("S5 失败：" + repr(exc))
            self.send_error_json(500, "S5 执行失败", traceback.format_exc())
            return
        tag = str(result.get("run_id") or run_id)
        # 让 POST 响应里的图带上 URL，前端「扩写成功后渲染」与 GET 同一形状
        result["figures"] = s5_figures_for_api(result.get("manifest") or {}, tag, folder)
        # discoveries / skeleton：优先用 outcome.to_dict() 带出的字段，缺失则回读 s5_facts.json
        facts = try_read_json(os.path.join(folder, "s5_facts.json"))
        if not isinstance(result.get("discoveries"), list):
            result["discoveries"] = facts.get("discoveries") or []
        if not isinstance(result.get("skeleton"), list):
            result["skeleton"] = facts.get("skeleton") or []
        result["status"] = {"state": result.get("state") or "",
                            "note": result.get("message") or "",
                            "updated_by": "S5"}
        try:
            from pipeline import s5_review

            result["record"] = s5_review.load_record(folder)
            result["materials"] = s5_review.build_materials(folder)
        except Exception as exc:  # noqa: BLE001 - 审核材料失败不挡扩写结果
            log("读取 S5 审核材料失败：" + repr(exc))
        log("S5 完成：" + str(result.get("state") or ""))
        self.send_json(result)

    def serve_pipeline_stages(self, tag: str) -> None:
        """回填对话页流水线：按落盘产物算 S1–S6 状态，不重跑任何阶段。"""
        data = build_pipeline_stages(unquote(tag))
        if data is None:
            self.send_error_json(404, "找不到运行目录：" + unquote(tag))
            return
        self.send_json(data)

    def serve_s3_result(self, run_id: str) -> None:
        """读已落盘的 issues.json / ledger.json，不现场重算。"""
        tag = unquote(run_id)
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        path = os.path.join(folder, "issues.json")
        if not os.path.isfile(path):
            self.send_json({"ok": False, "run_id": tag, "issues": {}, "ledger": {},
                            "error": "这次运行还没对过账"})
            return
        from pipeline.approvals import load as load_approvals

        approvals = load_approvals(folder)
        self.send_json({"ok": True,
                        "issues": try_read_json(path),
                        "ledger": try_read_json(os.path.join(folder, "ledger.json")),
                        "approval": approvals.get("s3", {}),
                        "approvals": approvals})

    def handle_s3(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            self.send_error_json(400, "缺少 run_id", "")
            return
        folder = resolve_run_dir(run_id)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + run_id)
            return
        only = [str(item) for item in (payload.get("only") or []) if str(item)]
        hint = str(payload.get("hint") or "").strip()
        try:
            from pipeline.s3_reconcile import _save_override, run_s3

            if only and hint:
                _save_override(Path(folder), only, hint)
                log("口径建议：" + "、".join(only) + " -> " + hint)
            log("S3 开始：" + folder + ("（只重算 " + "、".join(only) + "）" if only else ""))
            result, logs = run_s3(folder, only=only or None)
        except FileNotFoundError as exc:
            self.send_error_json(404, "前置文件缺失", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log("S3 失败：" + repr(exc))
            self.send_error_json(500, "S3 执行失败", traceback.format_exc())
            return
        log("S3 完成：" + result.message)
        self.send_json({"ok": True, "issues": result.model_dump(mode="json"), "log": logs})

    def serve_s2_cards(self, tag: str) -> None:
        """读某次运行已落盘的 cards.json；没拆过就明确说没有，不现场补跑。"""
        folder = resolve_run_dir(unquote(tag))
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + unquote(tag))
            return
        path = os.path.join(folder, "cards.json")
        if not os.path.isfile(path):
            self.send_json({"ok": False, "run_id": unquote(tag), "cards": {},
                            "error": "这次运行还没拆过卡"})
            return
        self.send_json({"ok": True, "cards": try_read_json(path)})

    def handle_s2(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            self.send_error_json(400, "缺少 run_id", "")
            return
        folder = resolve_run_dir(run_id)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + run_id)
            return
        try:
            from pipeline.s2_cards import run_s2

            log("S2 开始：" + folder)
            result, logs = run_s2(folder, force=bool(payload.get("force")))
        except FileNotFoundError as exc:
            self.send_error_json(404, "前置文件缺失", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 失败原因交给前端
            log("S2 失败：" + repr(exc))
            self.send_error_json(500, "S2 执行失败", traceback.format_exc())
            return
        log("S2 完成：" + result.message)
        self.send_json({"ok": True, "cards": result.model_dump(mode="json"), "log": logs})

    def serve_run(self, tag: str) -> None:
        data = load_run(tag)
        if data is None:
            self.send_error_json(404, "找不到运行目录或缺少 intake.json：" + unquote(tag))
            return
        self.send_json(data)

    def _llm_state(self) -> dict:
        """模型状态：供应商列表脱敏，密钥只报 has_key。"""
        from pipeline import model_config
        from pipeline.llm import DEFAULT_BASE_URL, DEFAULT_MODEL, load_env

        load_env()
        env_model = os.environ.get("GLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        env_base = (os.environ.get("GLM_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        env_key = bool(os.environ.get("GLM_API_KEY", "").strip())
        state = model_config.public_state()
        active = state["active_provider"]
        payload = {
            "providers": state["providers"],
            "env": {"configured": env_key, "model": env_model, "base_url": env_base},
        }
        if active and active["has_key"]:
            payload.update({"ok": True, "configured": True, "source": "file",
                            "id": active["id"], "name": active["name"],
                            "model": active["model"], "base_url": active["base_url"],
                            "has_key": True})
        elif env_key:
            payload.update({"ok": True, "configured": True, "source": "env",
                            "id": "", "name": "环境变量 / .env",
                            "model": env_model, "base_url": env_base, "has_key": True})
        else:
            payload.update({"ok": False, "configured": False, "source": None,
                            "id": "", "name": "", "model": env_model,
                            "base_url": env_base, "has_key": False,
                            "error": "未配置模型：点顶栏「模型」添加供应商，或在 .env 里填写 GLM_API_KEY"})
        return payload

    def serve_llm_info(self) -> None:
        try:
            self.send_json(self._llm_state())
        except Exception as exc:  # noqa: BLE001 - 配置读坏了也要让页面能打开
            self.send_json({"ok": False, "configured": False, "providers": [],
                            "model": "", "error": str(exc)})

    def handle_llm_test(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        payload: dict = {}
        if length:
            if length > MAX_BODY:
                self.send_error_json(400, "请求体过大", "上限 64MB")
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self.send_error_json(400, "请求体不是合法 JSON", "")
                return
            if not isinstance(payload, dict):
                self.send_error_json(400, "请求体必须是 JSON 对象", "")
                return
        try:
            from pipeline import model_config
            from pipeline.llm import LLMConfig, get_config, load_env, ping

            load_env()
            if any(str(payload.get(k) or "").strip()
                   for k in ("base_url", "model", "api_key", "id")):
                conn = model_config.resolve_connection(payload)
                if not conn["model"]:
                    raise model_config.ModelConfigError("测试连接需要填写模型名")
                config = LLMConfig(conn["api_key"], conn["base_url"], conn["model"])
            else:
                config = get_config()
            self.send_json(ping(config))
        except model_config.ModelConfigError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001 - 把失败原因交给前端
            self.send_json({"ok": False, "error": str(exc)}, 502)

    def handle_llm_save(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        from pipeline import model_config

        try:
            provider = model_config.upsert(payload)
            state = self._llm_state()
            state["ok"] = True
            state["saved"] = provider
            self.send_json(state)
        except model_config.ModelConfigError as exc:
            self.send_error_json(400, str(exc), "")
        except Exception as exc:  # noqa: BLE001 - 失败原因交给前端
            log("保存模型配置失败：" + repr(exc))
            self.send_error_json(500, "保存模型配置失败", str(exc))

    def handle_llm_activate(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        from pipeline import model_config

        try:
            provider_id = str(payload.get("id") or "").strip()
            if not provider_id:
                raise model_config.ModelConfigError("缺少供应商 id")
            provider = model_config.activate(provider_id)
            state = self._llm_state()
            state["ok"] = True
            state["saved"] = provider
            self.send_json(state)
        except model_config.ModelConfigError as exc:
            self.send_error_json(400, str(exc), "")
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, "启用供应商失败", str(exc))

    def handle_llm_delete(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        from pipeline import model_config

        try:
            provider_id = str(payload.get("id") or "").strip()
            if not provider_id:
                raise model_config.ModelConfigError("缺少供应商 id")
            model_config.delete(provider_id)
            state = self._llm_state()
            state["ok"] = True
            self.send_json(state)
        except model_config.ModelConfigError as exc:
            self.send_error_json(400, str(exc), "")
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, "删除供应商失败", str(exc))

    def handle_llm_models(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        from pipeline import model_config

        try:
            from pipeline.llm import list_models, load_env

            load_env()
            conn = model_config.resolve_connection(payload)
            models = list_models(conn["base_url"], conn["api_key"])
            self.send_json({"ok": True, "models": models, "base_url": conn["base_url"]})
        except model_config.ModelConfigError as exc:
            self.send_error_json(400, str(exc), "")
        except Exception as exc:  # noqa: BLE001 - 接口错误交给前端
            self.send_json({"ok": False, "error": str(exc)}, 502)

    # -- 对话 ------------------------------------------------------------
    def handle_chat_session(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            self.send_error_json(400, "请至少上传一个文件", "")
            return
        stamp = int(time.time() * 1000)
        folder = os.path.join(CHAT_ROOT, str(stamp))
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self.send_error_json(500, "无法创建会话目录", str(exc))
            return
        saved: list[str] = []
        try:
            for index, item in enumerate(raw_files):
                if not isinstance(item, dict):
                    raise ValueError("files[" + str(index) + "] 必须是对象")
                name = safe_name(item.get("name"))
                blob = decode_base64(item.get("data"))
                target = unique_path(folder, name)
                with open(target, "wb") as handle:
                    handle.write(blob)
                saved.append(target)
        except ValueError as exc:
            self.send_error_json(400, "上传内容不合法", str(exc))
            return
        except OSError as exc:
            self.send_error_json(500, "写入会话目录失败", str(exc))
            return

        try:
            from pipeline.agent import Session

            session = Session(saved, run_root=Path(RUNS_DIR), folder=Path(folder))
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, "无法初始化助手", traceback.format_exc())
            return
        with _SESSION_LOCK:
            _SESSIONS[session.id] = session
        session.save()
        log("对话会话 " + session.id + "，文件 " + "、".join(os.path.basename(p) for p in saved))
        self.send_json({"ok": True, "session_id": session.id,
                        "files": [os.path.basename(p) for p in saved]})

    def handle_chat_files(self) -> None:
        """把补充的文件并入已有会话，保留对话上下文。"""
        payload = self.read_json_body()
        if payload is None:
            return
        session_id = str(payload.get("session_id") or "").strip()
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            self.send_error_json(400, "没有收到文件", "")
            return
        with _SESSION_LOCK:
            session = _SESSIONS.get(session_id)
        if session is None:
            self.send_error_json(404, "会话已失效，请重新上传文件", "")
            return
        folder = session.folder
        if folder is None:
            self.send_error_json(500, "会话缺少目录", "")
            return
        saved: list[str] = []
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for index, item in enumerate(raw_files):
                if not isinstance(item, dict):
                    raise ValueError("files[" + str(index) + "] 必须是对象")
                name = safe_name(item.get("name"))
                blob = decode_base64(item.get("data"))
                target = folder / name
                with open(target, "wb") as handle:
                    handle.write(blob)
                saved.append(str(target))
        except ValueError as exc:
            self.send_error_json(400, "上传内容不合法", str(exc))
            return
        except OSError as exc:
            self.send_error_json(500, "写入会话目录失败", str(exc))
            return
        names = session.add_files([Path(p) for p in saved])
        log("会话 " + session.id + " 追加文件：" + "、".join(os.path.basename(p) for p in saved))
        self.send_json({"ok": True, "files": names})

    def handle_chat_progress(self, query: str) -> None:
        from urllib.parse import parse_qs

        session_id = (parse_qs(query).get("session_id") or [""])[0]
        with _SESSION_LOCK:
            session = _SESSIONS.get(session_id)
        if session is None:
            self.send_error_json(404, "会话已失效，请重新上传文件", "")
            return
        snapshot = session.progress_snapshot()
        snapshot["ok"] = True
        self.send_json(snapshot)

    def handle_chat_restore(self, session_id: str) -> None:
        """恢复一次对话：内存里有就用内存的，没有就从 session.json 重建。"""
        session_id = unquote(session_id).strip()
        with _SESSION_LOCK:
            session = _SESSIONS.get(session_id)
        if session is None:
            folder = find_chat_folder(session_id)
            if folder is None:
                self.send_error_json(404, "找不到这个对话：" + session_id)
                return
            try:
                from pipeline.agent import Session

                session = Session.restore(Path(folder), run_root=Path(RUNS_DIR))
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(500, "恢复对话失败", traceback.format_exc())
                return
            if session is None:
                self.send_error_json(404, "会话文件缺失或损坏：" + session_id)
                return
            with _SESSION_LOCK:
                _SESSIONS[session.id] = session
            log("恢复对话 " + session.id + "（" + folder + "）")
        self.send_json({
            "ok": True,
            "session_id": session.id,
            "files": [f.name for f in session.files],
            "messages": public_messages(session),
            "run_id": session.last_run,
            "created_at": session.created_at,
        })

    def handle_chat_delete(self) -> None:
        """删除一次对话：内存会话和 runs/_chat 下的目录一起清掉。"""
        payload = self.read_json_body()
        if payload is None:
            return
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            self.send_error_json(400, "缺少 session_id", "")
            return

        with _SESSION_LOCK:
            session = _SESSIONS.pop(session_id, None)
        folder = getattr(session, "folder", None) if session is not None else None
        if folder is None:
            folder = find_chat_folder(session_id)

        if folder is not None:
            root = Path(CHAT_ROOT).resolve()
            target = Path(folder).resolve()
            if target.parent != root:
                self.send_error_json(400, "拒绝删除会话目录之外的路径", "")
                return
            manifest = target / "session.json"
            if manifest.is_file():
                data = try_read_json(str(manifest))
                saved_id = str(data.get("id") or "")
                if saved_id and saved_id != session_id:
                    self.send_error_json(400, "会话目录与 id 不匹配", "")
                    return
            if target.is_dir():
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    self.send_error_json(500, "删除会话失败", str(exc))
                    return

        self.send_json({"ok": True, "session_id": session_id})

    def handle_chat(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        session_id = str(payload.get("session_id") or "").strip()
        message = str(payload.get("message") or "").strip()
        if not message:
            self.send_error_json(400, "消息为空", "")
            return
        with _SESSION_LOCK:
            session = _SESSIONS.get(session_id)
        if session is None:
            self.send_error_json(404, "会话已失效，请重新上传文件", "")
            return
        try:
            self.send_stream_headers()
        except ConnectionError:
            return

        def emit(event: dict) -> None:
            self.write_sse(event)

        try:
            result = session.send(message, on_event=emit)
        except ConnectionError:
            log("对话流已断开：" + session_id)
            return
        except Exception as exc:  # noqa: BLE001
            log("对话失败：" + repr(exc))
            try:
                self.write_sse({"type": "error", "ok": False,
                                "error": "助手调用失败：" + str(exc),
                                "detail": traceback.format_exc()})
            except ConnectionError:
                pass
            return
        try:
            self.write_sse({"type": "done", "ok": True,
                            "reply": result.get("reply", ""),
                            "events": result.get("events", []),
                            "run_id": result.get("run_id", "")})
        except ConnectionError:
            log("对话流已断开：" + session_id)

    def discard_body(self) -> None:
        """消费不用的请求体，避免残留字节污染 keep-alive 的下一个请求。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > MAX_BODY:
            self.close_connection = True
            return
        if length > 0:
            self.rfile.read(length)

    def read_optional_json_body(self) -> dict | None:
        """读可省略的 JSON 请求体；空体返回 {}，格式错误返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            self.close_connection = True
            self.send_error_json(400, "请求体过大", "上限 64MB")
            return None
        body = self.rfile.read(length)
        if not body.strip():
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self.send_error_json(400, "请求体不是合法的 UTF-8 JSON", str(exc))
            return None
        if not isinstance(payload, dict):
            self.send_error_json(400, "请求体必须是 JSON 对象", "")
            return None
        return payload

    def read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self.close_connection = True
            self.send_error_json(400, "请求体为空", "")
            return None
        if length > MAX_BODY:
            self.close_connection = True
            self.send_error_json(400, "请求体过大", "上限 64MB")
            return None
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self.send_error_json(400, "请求体不是合法的 UTF-8 JSON", str(exc))
            return None
        if not isinstance(payload, dict):
            self.send_error_json(400, "请求体必须是 JSON 对象", "")
            return None
        return payload

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/llm/test":
            self.handle_llm_test()
            return
        if path == "/api/llm/providers":
            self.handle_llm_save()
            return
        if path == "/api/llm/activate":
            self.handle_llm_activate()
            return
        if path == "/api/llm/delete":
            self.handle_llm_delete()
            return
        if path == "/api/llm/models":
            self.handle_llm_models()
            return
        if path.startswith("/api/runs/") and path.endswith("/waive"):
            self.handle_waive(path[len("/api/runs/"):-len("/waive")])
            return
        if path.startswith("/api/runs/") and path.endswith("/approve"):
            self.handle_approve(path[len("/api/runs/"):-len("/approve")])
            return
        if path == "/api/s4":
            self.handle_s4()
            return
        if path.startswith("/api/s6/") and path.endswith("/deliver"):
            self.handle_s6_deliver(path[len("/api/s6/"):-len("/deliver")])
            return
        if path.startswith("/api/s5/") and path.endswith("/submit"):
            self.handle_s5_submit(path[len("/api/s5/"):-len("/submit")])
            return
        if path.startswith("/api/s5/") and path.endswith("/decision"):
            self.handle_s5_decision(path[len("/api/s5/"):-len("/decision")])
            return
        if path.startswith("/api/s5/") and path.endswith("/questions"):
            self.handle_s5_questions(path[len("/api/s5/"):-len("/questions")])
            return
        if path == "/api/s5":
            self.handle_s5()
            return
        if path == "/api/s3":
            self.handle_s3()
            return
        if path == "/api/s2":
            self.handle_s2()
            return
        if path == "/api/chat/session":
            self.handle_chat_session()
            return
        if path == "/api/chat/files":
            self.handle_chat_files()
            return
        if path == "/api/chat/delete":
            self.handle_chat_delete()
            return
        if path == "/api/chat":
            self.handle_chat()
            return
        if path != "/api/intake":
            self.close_connection = True
            self.send_error_json(404, "未知路径：" + path)
            return
        try:
            self.handle_intake()
        except Exception as exc:  # noqa: BLE001
            log("POST /api/intake 失败：" + repr(exc))
            self.send_error_json(500, "服务器内部错误", traceback.format_exc())

    # -- POST /api/intake ------------------------------------------------
    def handle_intake(self) -> None:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self.close_connection = True
            self.send_error_json(400, "请求体为空", "缺少有效的 Content-Length")
            return
        if length > MAX_BODY:
            self.close_connection = True
            self.send_error_json(400, "请求体过大",
                                 "上限 64MB，本次 " + str(length) + " 字节")
            return

        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self.send_error_json(400, "请求体不是合法的 UTF-8 JSON", str(exc))
            return
        if not isinstance(payload, dict):
            self.send_error_json(400, "请求体必须是 JSON 对象", "")
            return

        prompt = payload.get("prompt")
        prompt = "" if prompt is None else str(prompt)
        raw_files = payload.get("files")
        if raw_files is None:
            raw_files = []
        if not isinstance(raw_files, list):
            self.send_error_json(400, "files 必须是数组", "")
            return

        stamp = int(time.time() * 1000)
        upload_dir = os.path.join(UPLOAD_ROOT, str(stamp))
        try:
            os.makedirs(upload_dir, exist_ok=True)
        except OSError as exc:
            self.send_error_json(500, "无法创建临时目录", str(exc))
            return

        saved: list[str] = []
        try:
            for index, item in enumerate(raw_files):
                if not isinstance(item, dict):
                    raise ValueError("files[" + str(index) + "] 必须是对象")
                name = safe_name(item.get("name"))
                blob = decode_base64(item.get("data"))
                target = unique_path(upload_dir, name)
                with open(target, "wb") as handle:
                    handle.write(blob)
                saved.append(target)
                log("已接收文件 %s（%d 字节）-> %s" % (name, len(blob), rel_to_root(target)))
        except ValueError as exc:
            self.send_error_json(400, "上传内容不合法", str(exc))
            return
        except OSError as exc:
            self.send_error_json(500, "写入临时目录失败", str(exc))
            return

        log("提示词长度 %d 字符，文件 %d 个，临时目录 %s" %
            (len(prompt), len(saved), rel_to_root(upload_dir)))

        logs: list[str] = []
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(_Tee(buffer)):
                intake = call_run_intake(prompt, saved, upload_dir, logs)
        except ModuleNotFoundError as exc:
            self.send_error_json(500, "无法导入 pipeline.s1_intake", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 把后端异常原样交给前端
            log("run_intake 抛错：" + repr(exc))
            detail = traceback.format_exc()
            self.send_json({"ok": False, "error": "准入检查执行失败：" + str(exc),
                            "detail": detail}, 500)
            return

        lines = [line.rstrip() for line in buffer.getvalue().splitlines() if line.strip()]
        status = status_for_intake(intake)
        log("run_intake 完成，state=%s，run.dir=%s" %
            (intake.get("state"), (intake.get("run") or {}).get("dir")))
        self.send_json({"ok": True, "intake": intake, "status": status, "log": lines})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S1 准入检查 Web 界面")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    args = parser.parse_args(argv)

    os.chdir(ROOT)  # 保证 run_intake 写 runs/<tag> 落在项目根
    os.makedirs(UPLOAD_ROOT, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log("项目根：" + ROOT)
    log("服务已启动：http://" + args.host + ":" + str(args.port) + "  （Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，正在关闭")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
