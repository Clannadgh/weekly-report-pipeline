"""智谱 GLM 的极简客户端（OpenAI 兼容协议，只用标准库）。

密钥从环境变量或项目根目录的 .env 读取，代码里不出现任何密钥。

    from pipeline.llm import chat, ping

    print(ping())
    print(chat([{"role": "user", "content": "你好"}]))

命令行自检：

    python -m pipeline.llm --ping
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-5.3-flash"
ENV_FILE = PROJECT_ROOT / ".env"


class LLMError(RuntimeError):
    """调用失败。message 里不含密钥。"""


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: int = 60


def load_env(path: Path | None = None) -> None:
    """把 .env 读进 os.environ，已存在的变量不覆盖。"""
    path = path or ENV_FILE
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in ("'", '"') and len(value) >= 2:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def get_config() -> LLMConfig:
    load_env()
    key = os.environ.get("GLM_API_KEY", "").strip()
    if not key:
        raise LLMError("未配置 GLM_API_KEY：请在项目根目录的 .env 里填写，或设置同名环境变量")
    return LLMConfig(
        api_key=key,
        base_url=os.environ.get("GLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        model=os.environ.get("GLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
    )


def _post(cfg: LLMConfig, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg.base_url + "/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg.api_key,
            "Accept": "application/json",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise LLMError(f"接口返回 HTTP {exc.code}：{detail}") from None
    except urllib.error.URLError as exc:
        raise LLMError(f"连不上 {cfg.base_url}：{exc.reason}") from None
    except TimeoutError:
        raise LLMError(f"请求超时（{cfg.timeout}s）") from None

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise LLMError(f"返回不是 JSON：{raw[:300]}") from None
    if status >= 400 or "error" in data:
        raise LLMError(f"接口报错：{json.dumps(data.get('error', data), ensure_ascii=False)[:600]}")
    data["_elapsed_ms"] = elapsed_ms
    return data


def chat(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    response_format: dict | None = None,
    timeout: int | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    thinking: str | None = None,
) -> dict:
    """返回 {text, message, tool_calls, model, usage, elapsed_ms, finish_reason}。"""
    cfg = get_config()
    payload: dict = {
        "model": model or cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if response_format:
        payload["response_format"] = response_format
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    if thinking in ("enabled", "disabled"):
        payload["thinking"] = {"type": thinking}
    if timeout:
        cfg = LLMConfig(cfg.api_key, cfg.base_url, cfg.model, timeout)

    data = _post(cfg, payload)
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"返回里没有 choices：{json.dumps(data, ensure_ascii=False)[:300]}")
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return {
        "text": text.strip(),
        "reasoning": (message.get("reasoning_content") or message.get("reasoning") or "").strip(),
        "message": message,
        "tool_calls": message.get("tool_calls") or [],
        "model": data.get("model") or payload["model"],
        "usage": data.get("usage") or {},
        "finish_reason": choices[0].get("finish_reason"),
        "elapsed_ms": data.get("_elapsed_ms", 0),
    }


def ping() -> dict:
    """连通性自检：短问题、短回答。"""
    cfg = get_config()
    out = chat(
        [{"role": "user", "content": "只回复两个字：可用"}],
        temperature=0.0,
        max_tokens=256,
    )
    return {"ok": bool(out["text"]), "model": out["model"], "reply": out["text"],
            "finish_reason": out["finish_reason"],
            "elapsed_ms": out["elapsed_ms"], "usage": out["usage"]}


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(ping(), ensure_ascii=False, indent=2))
    except LLMError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        sys.exit(1)
