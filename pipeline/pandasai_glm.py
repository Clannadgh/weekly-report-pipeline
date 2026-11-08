"""让 pandasai 的 OpenAI 适配器指向 GLM。

pandasai 2.3.2 内置了模型名白名单，glm-5.3-flash 不在里面会直接抛
UnsupportedModelError。这个模块只做两件事：把模型名放进白名单，
把 api_base 指到智谱的 OpenAI 兼容端点。不改 pandasai 的执行逻辑。
"""
from __future__ import annotations

from typing import Any

from .llm import get_config


def make_llm(model: str | None = None, api_key: str | None = None,
             base_url: str | None = None) -> Any:
    from pandasai.llm import OpenAI  # type: ignore

    config = get_config()
    name = model or config.model
    if name not in OpenAI._supported_chat_models:  # type: ignore[attr-defined]
        OpenAI._supported_chat_models = list(  # type: ignore[attr-defined]
            OpenAI._supported_chat_models) + [name]
    return OpenAI(model=name,
                  api_token=api_key or config.api_key,
                  api_base=base_url or config.base_url)


def make_qa(frames: dict, cache_dir: Any = None) -> Any:
    """构造 DataQA，llm 指向 GLM。"""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.ask_data import DataQA  # type: ignore

    return DataQA(frames, llm=make_llm(), cache_dir=cache_dir)
