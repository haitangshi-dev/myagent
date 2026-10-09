"""任务看板 — 按会话维度管理与持久化任务清单，并推送更新到前端。

任务清单用于：当用户发送复杂多步需求时，模型先用 task_board 工具把需求拆成子任务注入，
前端在输入框上方以可折叠列表展示，模型每完成一步就更新状态（doing / done）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import data_dir, events

logger = logging.getLogger(__name__)

# 看板落盘位置：必须放在安装目录之外，否则重装 / 卸载（NSIS 删整个安装目录）会清空。
# 优先级见 data_dir._resolve_data_base（env MY_AGENT_DATA_DIR → 用户数据目录 → 项目根兜底）。
_DATA_ROOT = data_dir.resolve_data_path("boards")


def _merge_boards(new: dict, old: dict) -> dict:
    """合并两个看板对象：以任务 id 去重，旧数据补全新位置缺失的任务。"""
    new_tasks = {t.get("id"): t for t in (new.get("tasks") or []) if t.get("id")}
    for t in old.get("tasks") or []:
        tid = t.get("id")
        if tid and tid not in new_tasks:
            new_tasks[tid] = t
    return {"tasks": list(new_tasks.values())}


def _migrate_legacy_boards() -> None:
    """首次导入时把旧安装目录内的 data/boards 并入用户数据目录（幂等 + 异常安全）。"""
    old_root = data_dir._legacy_data_root() / "boards"
    data_dir._maybe_migrate_dir(old_root, _DATA_ROOT, _merge_boards)


_migrate_legacy_boards()


def _board_path(sid: str) -> Path:
    # 会话 id 已为 hex，仍做基本清洗避免路径穿越
    safe = "".join(c for c in (sid or "") if c.isalnum() or c in "-_")
    return _DATA_ROOT / f"{safe or 'unknown'}.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load(sid: str) -> list[dict[str, Any]]:
    p = _board_path(sid)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("tasks", []) if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001
        return []


def _save(sid: str, tasks: list[dict[str, Any]]) -> None:
    _DATA_ROOT.mkdir(parents=True, exist_ok=True)
    _board_path(sid).write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add(sid: str, tasks: list[Any]) -> list[dict[str, Any]]:
    """批量新增任务。tasks 项可为字符串或 {title,status,note}。返回新增的规范化任务。"""
    existing = _load(sid)
    added: list[dict[str, Any]] = []
    for t in tasks or []:
        if isinstance(t, str):
            title = t.strip()
            if not title:
                continue
            item: dict[str, Any] = {"title": title, "status": "pending"}
        elif isinstance(t, dict):
            title = str(t.get("title") or "").strip()
            if not title:
                continue
            item = {"title": title, "status": str(t.get("status") or "pending")}
            if t.get("note"):
                item["note"] = str(t["note"])
        else:
            continue
        item["id"] = f"t{len(existing) + len(added) + 1}_{_now().replace(':', '').replace('-', '')}"
        item["created_at"] = _now()
        item["updated_at"] = item["created_at"]
        added.append(item)
    existing.extend(added)
    _save(sid, existing)
    return added


def _match_task(tasks: list[dict[str, Any]], ref: str | None) -> list[dict[str, Any]]:
    """按引用寻址任务：支持 id 精确 / 序号（1 起）/ 标题精确 / 标题包含。

    返回匹配列表（0 或多个）。序号是任务在清单里的展示位置（1 起），
    标题匹配大小写不敏感、去除首尾空白后精确或包含匹配。
    """
    if not ref or not str(ref).strip():
        return []
    ref = str(ref).strip()
    # 1) id 精确
    hit = [t for t in tasks if t.get("id") == ref]
    if hit:
        return hit
    # 2) 纯数字 → 序号（1 起）
    if ref.isdigit():
        idx = int(ref) - 1
        if 0 <= idx < len(tasks):
            return [tasks[idx]]
        return []
    # 3) 标题精确（忽略大小写与首尾空白）
    low = ref.lower()
    hit = [t for t in tasks if str(t.get("title", "")).strip().lower() == low]
    if hit:
        return hit
    # 4) 标题包含
    return [t for t in tasks if low in str(t.get("title", "")).lower()]


def update(
    sid: str,
    task_id: str,
    status: str | None = None,
    note: str | None = None,
    title: str | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """更新任务。task_id 可为真实 id 或标题/序号（见 _match_task）。

    返回 (changed, matched)：changed 是否更新成功；matched 是命中的任务列表
    （供调用方在失败/歧义时把快照回给模型自愈）。
    """
    tasks = _load(sid)
    matched = _match_task(tasks, task_id)
    if not matched:
        return False, matched
    # 命中多条时只更新第一条（避免歧义误改；调用方可用快照消歧）
    t = matched[0]
    if status:
        t["status"] = status
    if note is not None:
        t["note"] = note
    t["updated_at"] = _now()
    _save(sid, tasks)
    return True, matched


def remove(sid: str, task_id: str) -> tuple[bool, list[dict[str, Any]]]:
    """移除任务。task_id 可为真实 id 或标题/序号（见 _match_task）。

    返回 (removed, matched)。
    """
    tasks = _load(sid)
    matched = _match_task(tasks, task_id)
    if not matched:
        return False, matched
    t = matched[0]
    new = [x for x in tasks if x.get("id") != t.get("id")]
    if len(new) != len(tasks):
        _save(sid, new)
        return True, matched
    return False, matched


def clear(sid: str) -> None:
    _save(sid, [])


def list_tasks(sid: str) -> list[dict[str, Any]]:
    return _load(sid)


async def push_update(sid: str) -> None:
    """把当前任务清单作为 task_update 事件推入当前 SSE 流（前端据此刷新看板）。"""
    tasks = _load(sid)
    await events.push_to_request(
        {"type": "task_update", "session_id": sid, "tasks": tasks}
    )
