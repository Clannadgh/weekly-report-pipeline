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
import inspect
import io
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "web", "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")
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

    def send_json(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=JSON_INDENT).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def send_error_json(self, code: int, error: str, detail: str = "") -> None:
        self.send_json({"ok": False, "error": error, "detail": detail}, code)

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
            elif path == "/api/s3/runs":
                self.send_json({"ok": True, "runs": list_s3_runs()})
            elif path.startswith("/api/s3/"):
                self.serve_s3_result(path[len("/api/s3/"):])
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

    def handle_waive(self, tag: str) -> None:
        """登记人工豁免并重跑 S1。不改判定规则，只是把「人已确认接受」记下来。"""
        payload = self.read_json_body()
        if payload is None:
            return
        folder = resolve_run_dir(tag)
        if folder is None:
            self.send_error_json(404, "找不到运行目录：" + tag)
            return
        items = [str(x).strip() for x in (payload.get("items") or []) if str(x).strip()]
        if not items:
            self.send_error_json(400, "没有给要豁免的冲突关键词", "")
            return
        reason = str(payload.get("reason") or "").strip() or "用户确认可忽略"
        waiver = {
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "by": str(payload.get("by") or "用户"),
            "items": items,
            "reason": reason,
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
        log("人工豁免 " + ",".join(items) + " → " + result.state)
        self.send_json({"ok": True, "intake": to_plain(result), "waiver": waiver, "log": logs})

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
        self.send_json({"ok": True,
                        "issues": try_read_json(path),
                        "ledger": try_read_json(os.path.join(folder, "ledger.json"))})

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
            result, logs = run_s3(folder,
                                  engine=str(payload.get("engine") or "pandasai"),
                                  only=only or None)
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

    def serve_llm_info(self) -> None:
        """只报模型与是否配置，绝不返回密钥。"""
        try:
            from pipeline.llm import get_config

            config = get_config()
            self.send_json({"ok": True, "configured": True,
                            "model": config.model, "base_url": config.base_url})
        except Exception as exc:  # noqa: BLE001 - 未配置是正常状态
            self.send_json({"ok": False, "configured": False,
                            "model": os.environ.get("GLM_MODEL", ""),
                            "error": str(exc)})

    def handle_llm_test(self) -> None:
        length = 0
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length:
            self.rfile.read(length)
        try:
            from pipeline.llm import ping

            self.send_json(ping())
        except Exception as exc:  # noqa: BLE001 - 把失败原因交给前端
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
            result = session.send(message)
        except Exception as exc:  # noqa: BLE001
            log("对话失败：" + repr(exc))
            self.send_json({"ok": False, "error": "助手调用失败：" + str(exc),
                            "detail": traceback.format_exc()}, 500)
            return
        self.send_json({"ok": True, "reply": result.get("reply", ""),
                        "events": result.get("events", []),
                        "thoughts": result.get("thoughts", []),
                        "run_id": result.get("run_id", "")})

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
        if path.startswith("/api/runs/") and path.endswith("/waive"):
            self.handle_waive(path[len("/api/runs/"):-len("/waive")])
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
