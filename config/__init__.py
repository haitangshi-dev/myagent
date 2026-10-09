"""配置加载 — 读取 settings.yaml，解析 ${ENV:default} 占位符。

重装安全：除安装目录内的 settings.yaml 外，还支持一份位于用户数据目录
（%APPDATA%/MY_AGENT 或 XDG ~/.myagent 等）的 settings.override.yaml。
override 经深度合并覆盖默认配置，使前端可编辑并持久化 apikey / provider
等字段，且更新 / 重装 exe 不丢。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path(__file__).resolve().parent / "settings.yaml"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _resolve_env(node: Any) -> Any:
    """递归把字符串里的 ${VAR:default} 替换成环境变量值。"""
    if isinstance(node, str):
        def repl(m: re.Match) -> str:
            var, default = m.group(1), m.group(2) or ""
            return os.environ.get(var, default)

        return _ENV_RE.sub(repl, node)
    if isinstance(node, dict):
        return {k: _resolve_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_env(v) for v in node]
    return node


def get_override_path() -> Path:
    """返回用户数据目录下的 override 配置路径（重装安全）。

    优先级：
      1. 环境变量 MY_AGENT_SETTINGS_OVERRIDE（显式指定，由 electron 注入 userData 路径）
      2. Windows: %APPDATA%/MY_AGENT/settings.override.yaml
      3. XDG_DATA_HOME/my-agent/settings.override.yaml
      4. ~/.myagent/settings.override.yaml
      5. 兜底：安装目录内 settings.override.yaml
    """
    env = os.environ.get("MY_AGENT_SETTINGS_OVERRIDE")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "MY_AGENT" / "settings.override.yaml"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "my-agent" / "settings.override.yaml"
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if home and str(home) not in ("~", ""):
        return Path(home) / ".myagent" / "settings.override.yaml"
    return _SETTINGS_PATH.parent / "settings.override.yaml"


def _deep_merge(base: Any, override: Any) -> Any:
    """递归深度合并（override 优先），仅对 dict 做递归，list 直接覆盖。"""
    if not isinstance(override, dict):
        return override
    result: dict[str, Any] = dict(base) if isinstance(base, dict) else {}
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    """加载并解析配置，再深度合并用户数据目录的 override（如存在）。

    override 经 ${ENV:default} 解析后合并，使默认值与覆盖值里的环境变量占位符
    都能正确展开。
    """
    p = Path(path) if path else _SETTINGS_PATH
    if not p.exists():
        raise FileNotFoundError(f"找不到配置文件: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw = _resolve_env(raw)

    ov = get_override_path()
    if ov.exists() and ov.resolve() != p.resolve():
        try:
            ov_raw = yaml.safe_load(ov.read_text(encoding="utf-8")) or {}
            ov_raw = _resolve_env(ov_raw)
            raw = _deep_merge(raw, ov_raw)
        except Exception as exc:  # 容错：override 损坏不阻塞启动
            logger.warning("读取 override 配置失败，已忽略: %s", exc)
    return raw


def write_settings_override(partial: dict[str, Any], merge: bool = True) -> dict[str, Any]:
    """将 partial 写入用户数据目录的 override 配置（前端持久化 apikey 用）。

    merge=True 时与已有 override 深度合并，避免多次写入互相覆盖；
    返回合并后的完整 override 字典。
    """
    p = get_override_path()
    existing: dict[str, Any] = {}
    if merge and p.exists():
        try:
            existing = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
    merged = _deep_merge(existing, partial) if merge else partial
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(merged, allow_unicode=True), encoding="utf-8")
    return merged


def get_provider_config(settings: dict[str, Any], name: str) -> dict[str, Any]:
    return settings["providers"][name]
