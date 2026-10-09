"""共享数据目录解析 — 让重装 / 卸载（NSIS 删整个安装目录）不会清空用户数据。

本模块为 board.py（任务看板）、scheduler.py（定时任务）等"落盘在安装目录外"的
模块提供统一的路径解析与旧位置迁移工具，与 global_memory.py 的 _resolve_memory_dir
同源思路，只是这里统一管理 data/ 下的多个文件。

解析优先级（_resolve_data_base）：
  1) 环境变量 MY_AGENT_DATA_DIR（由 electron 主进程注入，指向 userData/data）
  2) 平台用户数据目录：Windows %APPDATA%/MY_AGENT/data，
     Linux $XDG_DATA_HOME/my-agent/data，macOS ~/.myagent/data
  3) 兜底：__file__ 所在项目根 / "data"（极端环境兼容，非默认路径）

安全阀门：
  - 若设置了环境变量 MY_AGENT_NO_MIGRATE，则跳过启动时的旧位置迁移
    （防止测试 / 特殊场景下误搬真实数据）。

迁移（_maybe_migrate_file / _maybe_migrate_dir）均为幂等 + 异常安全：
  - 只在新位置缺失时才并入旧内容；迁移成功后把旧文件改名 .migrated，
    下次启动不会重复迁移。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 安全阀门：开启后跳过所有旧位置迁移（默认 False，即正常迁移）
NO_MIGRATE = os.environ.get("MY_AGENT_NO_MIGRATE", "").lower() in ("1", "true", "yes")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_data_base() -> Path:
    """返回用户数据根目录（其下再放 boards/、schedules.json 等）。"""
    env = os.environ.get("MY_AGENT_DATA_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "MY_AGENT" / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "my-agent" / "data"
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if home and str(home) not in ("~", ""):
        return Path(home) / ".myagent" / "data"
    return _PROJECT_ROOT / "data"


def resolve_data_path(*parts: str) -> Path:
    """返回 _resolve_data_base() 下拼接 parts 的路径，并确保父目录存在。"""
    p = _resolve_data_base().joinpath(*parts)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("创建数据目录失败（将尝试直接写入）: %s", exc)
    return p


# =========================================================================
# 旧位置迁移工具（幂等 + 异常安全）
# =========================================================================
def _legacy_data_root() -> Path:
    """旧位置：安装目录内的 data/（升级时迁移用，重装会被删）。"""
    return _PROJECT_ROOT / "data"


def _maybe_migrate_file(old: Path, new: Path, merge: Any) -> None:
    """把单个旧 JSON 文件并入新位置（merge(new_obj, old_obj) -> merged 写回新位置）。

    - 旧文件不存在或新文件已存在 → 跳过
    - 迁移成功后旧文件改名 .migrated，避免重复迁移
    - 任意异常都只告警不抛出，绝不影响新位置写入
    """
    if NO_MIGRATE:
        return
    if not old.exists() or new.exists():
        return
    try:
        old_obj = json.loads(old.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取旧数据失败（跳过迁移 %s）: %s", old, exc)
        return
    try:
        if new.exists():
            cur = json.loads(new.read_text(encoding="utf-8"))
        else:
            cur = {}
        merged = merge(cur, old_obj)
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("迁移数据失败（不影响新位置）: %s", exc)
        return
    try:
        old.replace(old.with_name(old.name + ".migrated"))
    except Exception:  # noqa: BLE001
        pass
    logger.info("数据迁移：%s -> %s", old, new)


def _maybe_migrate_dir(old_root: Path, new_root: Path, merge: Any) -> None:
    """把旧目录里的同名文件逐个并入新目录（merge(new_obj, old_obj) -> merged）。

    - 旧目录不存在则跳过；已存在的新文件跳过（不覆盖用户在新位置的修改）
    - 迁移成功的旧文件改名 .migrated
    """
    if NO_MIGRATE:
        return
    if not old_root.is_dir():
        return
    try:
        new_root.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    for old_file in old_root.glob("*.json"):
        new_file = new_root / old_file.name
        _maybe_migrate_file(old_file, new_file, merge)
