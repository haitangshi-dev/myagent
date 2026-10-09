"""Provider profile base class — 仿 Hermes providers/base.py（声明式）。

ProviderProfile 只描述 provider 的行为（鉴权、端点、quirks），不负责实际的流式传输。
传输层（openai_compat.OpenAICompatClient）读取它。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 省缺温度哨兵（某些 provider 由服务端管理温度）
OMIT_TEMPERATURE = object()


@dataclass
class ProviderProfile:
    """基础 provider 档案 —— 可子类化或用字段覆盖实例化。"""

    # ── 身份 ─────────────────────────────────────────────
    name: str
    api_mode: str = "chat_completions"
    aliases: tuple = ()

    # ── 人类可读元数据 ───────────────────────────────
    display_name: str = ""
    description: str = ""
    signup_url: str = ""

    # ── 鉴权与端点 ───────────────────────────────────
    env_vars: tuple = ()
    base_url: str = ""
    models_url: str = ""
    auth_type: str = "api_key"
    supports_health_check: bool = True

    # ── 能力 ─────────────────────────────────────────
    supports_vision: bool = False
    supports_vision_tool_messages: bool = True

    # ── 模型目录 ─────────────────────────────────────
    fallback_models: tuple = ()
    hostname: str = ""

    # ── 客户端级 quirks ─────────────────────────────
    default_headers: dict[str, str] = field(default_factory=dict)

    # ── 请求级 quirks ───────────────────────────────
    fixed_temperature: Any = None
    default_max_tokens: int | None = None
    default_aux_model: str = ""

    def get_hostname(self) -> str:
        if self.hostname:
            return self.hostname
        if self.base_url:
            from urllib.parse import urlparse

            return urlparse(self.base_url).hostname or ""
        return ""

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """provider 特定的消息预处理，默认透传。"""
        return messages

    def build_extra_body(self, *, session_id: str | None = None, **context: Any) -> dict[str, Any]:
        """provider 特定的 extra_body 字段，默认空。"""
        return {}
