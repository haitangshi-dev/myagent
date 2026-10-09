"""会话检查点 — 长会话状态恢复（session_state.json）。

背景（框架改进报告 P0-2）：多次「继续」中断后，Agent 要重新探索环境——
找文件在哪、看脚本叫什么、回忆上一步做到哪。光「重新定位知识库路径」就花好几轮。

机制：
  - 每完成一步（一次工具调用 / 一轮消息），框架自动把
    「当前进度 + 涉及文件路径 + 下一步建议」写入 session_state.json（按 session_id 分文件）。
  - 会话恢复（同一 session_id 再次发消息）时，agent_loop 读取检查点并把
    「现场摘要」注入到 user 消息尾部，让模型秒回现场，不再重新探索。

落盘位置：data_dir（用户数据目录），与看板/定时任务一致，重装不丢。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from agents import data_dir

logger = logging.getLogger(__name__)

# 每个会话的检查点文件：{data}/session_state/{safe_sid}.json
_STATE_ROOT = data_dir.resolve_data_path("session_state")

# 检查点里收集的关键路径（工具调用产物），供恢复时引用
# 注意：部分 Windows 用户目录含撇号或空格，字符类必须兼容
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s\"<>|]{3,}|"
    r"/(?:Users|home|tmp|workspace)[^\s\"<>|]{3,}"
)

_MAX_CHECKPOINT_CHARS = 2000  # 注入给模型的现场摘要上限


def _state_path(sid: str) -> Path:
    safe = "".join(c for c in (sid or "") if c.isalnum() or c in "-_")
    return _STATE_ROOT / f"{safe or 'unknown'}.json"


def save_checkpoint(
    sid: str,
    *,
    step: str,
    done: list[str] | None = None,
    files: list[str] | None = None,
    next_step: str | None = None,
    raw_tool_result: str | None = None,
    ts: int | None = None,
) -> None:
    """写入/追加一条会话检查点。

    step       当前这一步的简述（如「下载崩铁角色 JSON 到 data/roles.json」）
    done       已完成的步骤列表（增量追加，自动去重）
    files      本步涉及的关键文件/路径（自动提取 raw_tool_result 中的路径）
    next_step  下一步建议（可选；模型产出后自然会有）
    raw_tool_result  工具原始返回（用于提取路径等关键信息）
    """
    if not sid:
        return
    try:
        _STATE_ROOT.mkdir(parents=True, exist_ok=True)
        path = _state_path(sid)
        state: dict[str, Any] = {}
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                state = {}
        state.setdefault("session_id", sid)
        state.setdefault("created_at", int(time.time()))
        state.setdefault("steps", [])
        state.setdefault("done", [])
        state.setdefault("files", [])
        state.setdefault("updated_at", int(time.time()))

        # 已完成步骤去重追加
        if step:
            seen = set(state["done"])
            if step not in seen:
                state["done"].append(step)
                state["done"] = state["done"][-30:]
        # 路径提取：显式 files + 工具结果里的路径
        paths: list[str] = list(files or [])
        if raw_tool_result:
            for m in _PATH_RE.findall(raw_tool_result or ""):
                p = m.rstrip(".,;")
                if p not in paths:
                    paths.append(p)
        for p in paths[:20]:
            if p not in state["files"]:
                state["files"].append(p)
                state["files"] = state["files"][-30:]
        # 步骤历史（保留最近 50 条，含时间）
        if step:
            state["steps"].append({"ts": int(time.time()), "step": step})
            state["steps"] = state["steps"][-50:]
        if next_step:
            state["next_step"] = next_step
        state["updated_at"] = int(time.time())
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("会话检查点写入失败: %s", exc)


def load_checkpoint(sid: str) -> dict[str, Any] | None:
    """读取某会话的最新检查点；不存在返回 None。"""
    if not sid:
        return None
    try:
        path = _state_path(sid)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def checkpoint_block(sid: str, limit: int = _MAX_CHECKPOINT_CHARS) -> str:
    """把检查点渲染成可注入的「现场摘要」文本（供会话恢复时拼到 user 消息）。"""
    cp = load_checkpoint(sid)
    if not cp:
        return ""
    lines: list[str] = []
    done = cp.get("done") or []
    files = cp.get("files") or []
    steps = cp.get("steps") or []
    next_step = cp.get("next_step")
    if done:
        lines.append("【已完成步骤】")
        lines.extend(f"- {d}" for d in done[-10:])
    if files:
        lines.append("【涉及文件/路径】")
        lines.extend(f"- {f}" for f in files[-15:])
    if next_step:
        lines.append(f"【上次记录的下一步】{next_step}")
    if steps:
        last = steps[-1].get("step")
        if last and last not in done:
            lines.append(f"【上一步】{last}")
    if not lines:
        return ""
    head = "📌 【会话现场恢复】这是本会话上次进行到一半的状态，直接基于它继续，不要重新探索环境：\n"
    body = "\n".join(lines)
    if len(body) > limit:
        body = body[:limit] + "\n…（检查点过长已截断）"
    return head + body
