"""工作区会话 — 按目录隔离的持久记忆 + 沙盒配置。

设计目标（对应前端「工作区会话」）：
  - 每个被前端选为工作区的文件夹，拥有自己独立的记忆文件（.myagent_memory.jsonl），
    与全局 agent_memory.jsonl、其他工作区完全隔离。
  - 每个工作区有一份沙盒配置（.myagent_sandbox.json），控制文件写/删、命令执行权限。
  - 记忆/沙盒文件直接落在工作区目录内，前端可随时增删文件夹，后端零状态。

安全：所有路径解析都先 verify 工作区目录有效（存在且为目录），越界一律拒绝。
"""

from __future__ import annotations

import json as _json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MEMORY_FILENAME = ".myagent_memory.jsonl"
_SANDBOX_FILENAME = ".myagent_sandbox.json"

# 沙盒开关默认值：默认允许写文件、允许执行命令，但禁止删除（删除需用户确认，最危险）
DEFAULT_SANDBOX: dict[str, bool] = {
    "allow_file_write": True,
    "allow_file_delete": False,
    "allow_shell": True,
}

_MAX_MEMORY_ENTRIES = 500  # 单个工作区记忆上限，超出丢弃最旧


# =========================================================================
# 路径辅助
# =========================================================================
def _valid_ws_dir(workspace_dir: str | None) -> Path | None:
    """返回已解析且有效（存在、是目录）的工作区路径；否则 None。"""
    if not workspace_dir:
        return None
    try:
        p = Path(workspace_dir).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return None
    if not p.is_dir():
        return None
    return p


# =========================================================================
# 沙盒配置
# =========================================================================
def load_sandbox(workspace_dir: str | None) -> dict[str, bool]:
    """读取工作区沙盒配置；无效目录或缺失文件则用 DEFAULT_SANDBOX。"""
    p = _valid_ws_dir(workspace_dir)
    if p is None:
        return dict(DEFAULT_SANDBOX)
    sf = p / _SANDBOX_FILENAME
    if not sf.exists():
        return dict(DEFAULT_SANDBOX)
    try:
        data = _json.loads(sf.read_text(encoding="utf-8"))
        cfg = dict(DEFAULT_SANDBOX)
        for k in DEFAULT_SANDBOX:
            if k in data:
                cfg[k] = bool(data[k])
        return cfg
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取沙盒配置失败 %s: %s", sf, exc)
        return dict(DEFAULT_SANDBOX)


def save_sandbox(workspace_dir: str | None, cfg: dict[str, bool]) -> bool:
    """持久化沙盒配置到 <工作区>/.myagent_sandbox.json。成功返回 True。"""
    p = _valid_ws_dir(workspace_dir)
    if p is None:
        return False
    try:
        out = {k: bool(cfg.get(k, DEFAULT_SANDBOX[k])) for k in DEFAULT_SANDBOX}
        (p / _SANDBOX_FILENAME).write_text(
            _json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存沙盒配置失败: %s", exc)
        return False


# =========================================================================
# 工作区记忆（按目录隔离的 jsonl）
# =========================================================================
def add_memory(workspace_dir: str | None, content: str, tags: list | None = None) -> int:
    """追加一条工作区记忆，返回当前总条数。无效目录抛 ValueError。"""
    p = _valid_ws_dir(workspace_dir)
    if p is None:
        raise ValueError("无效的工作区目录")
    mf = p / _MEMORY_FILENAME
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "content": content,
        "tags": list(tags or []),
    }
    try:
        if mf.exists():
            lines = [ln for ln in mf.read_text(encoding="utf-8").splitlines() if ln.strip()]
        else:
            lines = []
        lines.append(_json.dumps(entry, ensure_ascii=False))
        if len(lines) > _MAX_MEMORY_ENTRIES:
            lines = lines[-_MAX_MEMORY_ENTRIES:]
        mf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(lines)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入工作区记忆失败: %s", exc)
        raise


def recall_memory(workspace_dir: str | None, query: str = "", limit: int = 10) -> str:
    """按关键词检索工作区记忆文本；无 query 返回全部（最多 limit 条）。"""
    p = _valid_ws_dir(workspace_dir)
    if p is None:
        return "无效的工作区目录"
    mf = p / _MEMORY_FILENAME
    if not mf.exists():
        return "该工作区尚无记忆"
    try:
        lines = mf.read_text(encoding="utf-8").splitlines()
    except Exception as exc:  # noqa: BLE001
        return f"读取失败: {exc}"
    q = (query or "").lower().strip()
    out: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = _json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        hay = (str(e.get("content", "")) + " " + " ".join(map(str, e.get("tags", [])))).lower()
        if not q or q in hay:
            tagstr = ("  #" + " #".join(map(str, e.get("tags", [])))) if e.get("tags") else ""
            out.append(f"[{e.get('ts', '')}] {e.get('content', '')}{tagstr}")
    if not out:
        return "未找到相关记忆" if q else "（空）"
    return "\n".join(out[-limit:])


def clear_memory(workspace_dir: str | None) -> bool:
    """清空工作区记忆文件。成功返回 True。"""
    p = _valid_ws_dir(workspace_dir)
    if p is None:
        return False
    mf = p / _MEMORY_FILENAME
    try:
        if mf.exists():
            mf.unlink()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("清空记忆失败: %s", exc)
        return False


def memory_count(workspace_dir: str | None) -> int:
    p = _valid_ws_dir(workspace_dir)
    if p is None:
        return 0
    mf = p / _MEMORY_FILENAME
    if not mf.exists():
        return 0
    try:
        return sum(1 for ln in mf.read_text(encoding="utf-8").splitlines() if ln.strip())
    except Exception:  # noqa: BLE001
        return 0


def memory_context(workspace_dir: str | None, query: str = "", limit: int = 8) -> str:
    """生成可注入系统提示的「工作区记忆摘要」；无记忆返回空串。"""
    text = recall_memory(workspace_dir, query=query, limit=limit)
    if not text or text.startswith("该工作区尚无记忆"):
        return ""
    return text


# =========================================================================
# 系统提示注入块
# =========================================================================
def system_block(ctx: dict | None) -> str:
    """生成追加到 system prompt 的工作区说明（含沙盒规则 + 已有记忆）。无工作区返回空。"""
    if not ctx or ctx.get("kind") != "workspace":
        return ""
    wd = ctx.get("workspace_dir")
    p = _valid_ws_dir(wd)
    if p is None:
        return ""
    sandbox = load_sandbox(wd)
    lines = [
        "【工作区会话】你当前处于一个工作区会话中，专用于协作该目录下的代码/文档。",
        f"- 工作区根目录：{p}",
        "- 文件类工具（读/写/编辑/列出/搜索/删除）只允许访问该目录及其子目录，越界会被拒绝。",
        "- 命令执行（shell）的工作目录被限制在该目录，且受下方沙盒开关控制。",
        "- 记忆（memory_remember/recall）只保存在该工作区，与其他会话完全隔离。",
    ]
    rules = []
    rules.append("允许写入文件" if sandbox["allow_file_write"] else "禁止写入文件")
    rules.append("允许删除文件" if sandbox["allow_file_delete"] else "禁止删除文件（删除操作会被拒绝）")
    rules.append("允许执行命令" if sandbox["allow_shell"] else "禁止执行命令")
    lines.append("- 沙盒策略：" + "；".join(rules) + "。")
    mem = memory_context(wd, limit=8)
    if mem:
        lines.append("- 工作区已有记忆（请参考，必要时用 memory_remember 更新）：")
        lines.append(mem)
    return "\n".join(lines)
