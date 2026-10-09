"""Provider 管理器 — 按配置懒构建 OpenAICompatClient，并枚举可用 provider。"""

from __future__ import annotations

import logging
from typing import Any

from providers.openai_compat import OpenAICompatClient

logger = logging.getLogger(__name__)


class ProviderManager:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self._clients: dict[str, OpenAICompatClient] = {}

    def list_providers(self) -> list[dict[str, Any]]:
        """返回前端可用的 provider 摘要（不含密钥）。"""
        out: list[dict[str, Any]] = []
        for name, cfg in self.settings["providers"].items():
            # 可用模型 = models 列表 ∪ {default_model}
            # （本项目只提供一个通用接口槽位，使用者常只填一个模型名，故并入列表供前端选择）
            _models = [m for m in (cfg.get("models") or []) if m]
            _dm = (cfg.get("default_model") or "").strip()
            if _dm and _dm not in _models:
                _models.append(_dm)
            out.append(
                {
                    "id": name,
                    "display_name": cfg.get("display_name", name),
                    "description": cfg.get("description", ""),
                    "models": _models,
                    "default_model": cfg.get("default_model", ""),
                    "base_url": cfg.get("base_url", "") or "",
                    "supports_vision": bool(cfg.get("supports_vision", False)),
                    # 上下文窗口（token 数），用于前端「上下文占用」可视化；
                    # 缺省 None 表示不限制（前端不显示占用条）
                    "context_window": int(cfg["context_window"]) if cfg.get("context_window") else None,
                }
            )
        return out

    def get_client(self, name: str, model: str | None = None) -> OpenAICompatClient:
        """获取（或构建）某 provider 的客户端。"""
        cfg = self.settings["providers"].get(name)
        if not cfg:
            raise KeyError(f"未知 provider: {name}")

        rl = cfg.get("rate_limit", {}) or {}
        # 模型：调用方指定 > provider 默认 > 列表首个
        effective_model = model or cfg.get("default_model") or (cfg.get("models") or [None])[0]
        if not effective_model:
            raise ValueError(f"provider {name} 没有可用 model")

        cache_key = f"{name}:{effective_model}"
        client = self._clients.get(cache_key)
        if client is None:
            client = OpenAICompatClient(
                base_url=cfg["base_url"],
                api_key=cfg.get("api_key", ""),
                model=effective_model,
                max_rpm=int(rl.get("max_rpm", 0) or 0),
                min_interval=float(rl.get("min_interval", 0.0) or 0.0),
                max_concurrency=int(rl.get("max_concurrency", 0) or 0),
                timeout=float(cfg.get("timeout", 120.0)),
                context_limit=int(cfg["context_window"]) if cfg.get("context_window") else None,
            )
            self._clients[cache_key] = client
        else:
            # 允许运行时切换 model（同一 provider 不同 model 走不同缓存键）
            if client.model != effective_model:
                client = OpenAICompatClient(
                    base_url=cfg["base_url"],
                    api_key=cfg.get("api_key", ""),
                    model=effective_model,
                    max_rpm=int(rl.get("max_rpm", 0) or 0),
                    min_interval=float(rl.get("min_interval", 0.0) or 0.0),
                    max_concurrency=int(rl.get("max_concurrency", 0) or 0),
                    context_limit=int(cfg["context_window"]) if cfg.get("context_window") else None,
                )
                self._clients[cache_key] = client
        return client

    async def close_all(self) -> None:
        for c in self._clients.values():
            await c.close()
        self._clients.clear()
