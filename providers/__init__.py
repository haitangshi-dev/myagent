"""Provider registry — 仿 Hermes providers 模块。

ProviderProfile 是声明式的：把某个推理 provider 的鉴权、端点、 quirks 集中在一处，
transport（openai_compat.OpenAICompatClient）读取它而不是收一堆布尔开关。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from pathlib import Path

from providers.base import OMIT_TEMPERATURE, ProviderProfile  # noqa: F401

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ProviderProfile] = {}
_ALIASES: dict[str, str] = {}
_discovered = False


def register_provider(profile: ProviderProfile) -> None:
    """按 name + aliases 注册 provider，后注册的覆盖先注册的。"""
    _REGISTRY[profile.name] = profile
    for alias in profile.aliases:
        _ALIASES[alias] = profile.name


def get_provider_profile(name: str) -> ProviderProfile | None:
    if not _discovered:
        _discover_providers()
    canonical = _ALIASES.get(name, name)
    return _REGISTRY.get(canonical)


def list_providers() -> list[ProviderProfile]:
    if not _discovered:
        _discover_providers()
    seen: set[int] = set()
    result: list[ProviderProfile] = []
    for profile in _REGISTRY.values():
        pid = id(profile)
        if pid not in seen:
            seen.add(pid)
            result.append(profile)
    return result


def _discover_providers() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    try:
        import providers as _pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(_pkg.__path__):
            if modname.startswith("_") or modname == "base":
                continue
            try:
                importlib.import_module(f"providers.{modname}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载 provider 模块 %s 失败: %s", modname, exc)
    except Exception:  # noqa: BLE001
        pass
