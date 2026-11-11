"""模型供应商配置的本地存储。

配置写在项目根目录 config/models.json，页面顶栏「模型」读写这个文件：

    {
      "active": "供应商 id",
      "providers": [
        {"id": "...", "name": "...", "base_url": "...", "api_key": "...", "model": "..."}
      ]
    }

文件含密钥，已在 .gitignore 里忽略。没有配置文件时，pipeline.llm 继续读 .env。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config" / "models.json"

_LOCK = threading.RLock()


class ModelConfigError(RuntimeError):
    """配置缺失或不合法。message 可直接展示给用户，不含密钥。"""


def _clean_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        raise ModelConfigError("请填写 Base URL")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ModelConfigError("Base URL 需要以 http:// 或 https:// 开头")
    return url


def _clean_model(value: str) -> str:
    model = str(value or "").strip()
    if not model:
        raise ModelConfigError("请填写模型名")
    return model


def _default_name(base_url: str) -> str:
    return urlparse(base_url).netloc or "自定义供应商"


def _env_connection() -> tuple[str, str]:
    key = os.environ.get("GLM_API_KEY", "").strip()
    base = os.environ.get("GLM_BASE_URL", "").strip().rstrip("/")
    return key, base


def _env_key_for(base_url: str) -> str:
    """.env 的密钥只在地址对得上时沿用，避免把 A 家的密钥发给 B 家。"""
    key, env_base = _env_connection()
    if not key:
        return ""
    target = str(base_url or "").strip().rstrip("/")
    if env_base:
        return key if target == env_base else ""
    host = urlparse(target).netloc
    return key if host == "bigmodel.cn" or host.endswith(".bigmodel.cn") else ""


def _load() -> dict:
    if not CONFIG_FILE.exists():
        return {"active": None, "providers": []}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ModelConfigError(f"读取 {CONFIG_FILE.name} 失败：{exc}") from None
    if not isinstance(data, dict):
        raise ModelConfigError("models.json 的顶层必须是对象")
    raw_providers = data.get("providers")
    providers = []
    if isinstance(raw_providers, list):
        for raw in raw_providers:
            if not isinstance(raw, dict):
                continue
            base_url = str(raw.get("base_url") or "").strip().rstrip("/")
            model = str(raw.get("model") or "").strip()
            provider_id = str(raw.get("id") or "").strip()
            if not (base_url and model and provider_id):
                continue
            providers.append({
                "id": provider_id,
                "name": str(raw.get("name") or "").strip() or _default_name(base_url),
                "base_url": base_url,
                "api_key": str(raw.get("api_key") or "").strip(),
                "model": model,
            })
    active = str(data.get("active") or "").strip() or None
    if active and not any(p["id"] == active for p in providers):
        active = providers[0]["id"] if providers else None
    return {"active": active, "providers": providers}


def _write(state: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, CONFIG_FILE)


def _find(state: dict, provider_id: str) -> dict | None:
    return next((p for p in state["providers"] if p["id"] == provider_id), None)


def _public(provider: dict, active_id: str | None) -> dict:
    return {
        "id": provider["id"],
        "name": provider["name"],
        "base_url": provider["base_url"],
        "model": provider["model"],
        "has_key": bool(provider["api_key"]),
        "active": provider["id"] == active_id,
    }


def public_state() -> dict:
    """给前端的状态：不含任何密钥。"""
    state = _load()
    active_id = state["active"]
    providers = [_public(p, active_id) for p in state["providers"]]
    active = next((p for p in providers if p["id"] == active_id), None)
    return {"active": active_id, "active_provider": active, "providers": providers}


def active_provider() -> dict | None:
    """当前供应商的完整配置（含密钥），只给 pipeline.llm 内部用。"""
    state = _load()
    provider = _find(state, state["active"]) if state["active"] else None
    if provider and provider["api_key"]:
        return dict(provider)
    return None


def upsert(payload: dict) -> dict:
    """新增或更新一个供应商；activate=true 时设为当前。返回脱敏后的供应商。"""
    with _LOCK:
        state = _load()
        provider_id = str(payload.get("id") or "").strip()
        existing = _find(state, provider_id) if provider_id else None
        if provider_id and existing is None:
            raise ModelConfigError("找不到这个供应商，可能已被删除，请刷新后重试")
        base_url = _clean_base_url(payload.get("base_url") or (existing or {}).get("base_url", ""))
        model = _clean_model(payload.get("model") or (existing or {}).get("model", ""))
        name = str(payload.get("name") or "").strip() or (existing or {}).get("name") or _default_name(base_url)
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key and existing:
            api_key = existing["api_key"]
        if not api_key:
            api_key = _env_key_for(base_url)
        if not api_key:
            raise ModelConfigError("请填写 API Key（.env 里的密钥只对 .env 里的 Base URL 生效）")

        if existing:
            existing.update({"name": name, "base_url": base_url, "api_key": api_key, "model": model})
            provider = existing
        else:
            provider = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
            }
            state["providers"].append(provider)

        if payload.get("activate", True) or not state["active"]:
            state["active"] = provider["id"]
        _write(state)
        return _public(provider, state["active"])


def activate(provider_id: str) -> dict:
    with _LOCK:
        state = _load()
        provider = _find(state, provider_id)
        if provider is None:
            raise ModelConfigError("找不到这个供应商，可能已被删除")
        state["active"] = provider["id"]
        _write(state)
        return _public(provider, state["active"])


def delete(provider_id: str) -> None:
    with _LOCK:
        state = _load()
        state["providers"] = [p for p in state["providers"] if p["id"] != provider_id]
        if state["active"] == provider_id:
            state["active"] = state["providers"][0]["id"] if state["providers"] else None
        _write(state)


def resolve_connection(payload: dict) -> dict:
    """测试连接 / 拉模型列表用：表单里没填的字段用已保存供应商或 .env 兜底。"""
    with _LOCK:
        state = _load()
        provider_id = str(payload.get("id") or "").strip()
        existing = _find(state, provider_id) if provider_id else None
        if provider_id and existing is None:
            raise ModelConfigError("找不到这个供应商，可能已被删除")
        base_url = _clean_base_url(
            payload.get("base_url") or (existing or {}).get("base_url", ""))
        model = str(payload.get("model") or (existing or {}).get("model", "")).strip()
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key and existing:
            api_key = existing["api_key"]
        if not api_key:
            api_key = _env_key_for(base_url)
        if not api_key:
            raise ModelConfigError("请填写 API Key（.env 里的密钥只对 .env 里的 Base URL 生效）")
        return {"base_url": base_url, "api_key": api_key, "model": model}
