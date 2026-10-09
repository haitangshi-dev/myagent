"""事件总线 — 在 agent 工具执行期间把任务/定时更新推回当前 SSE 流。

设计：
  - request_emitter_var：每个 /api/chat 请求在启动时把自己的 emit 协程塞进 contextvar。
    工具处理函数（如 task_board）在请求上下文内可直接 push_to_request(...) 把事件
    注入到正在流动的 SSE 流里（gen() 会把队列里的事件 yield 给前端）。
  - active_conns：全局广播订阅者集合。目前供 scheduler 在后台任务完成时广播用；
    若前端将来订阅全局 SSE，即可实时收到 scheduled_run 事件。
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Emitter = Callable[[dict[str, Any]], Awaitable[None]]

# 当前请求的 SSE emit 协程（由 server/api.py 在 /api/chat 内注入）。
request_emitter_var: contextvars.ContextVar[Emitter | None] = contextvars.ContextVar(
    "req_emit", default=None
)

# 全局广播订阅者（asyncio 回调）。
active_conns: set[Emitter] = set()


def set_request_emitter(emitter: Emitter | None):
    """在当前请求上下文注入 emit 协程，返回 token 供后续 reset。"""
    return request_emitter_var.set(emitter)


def reset_request_emitter(token=None) -> None:
    """清理当前请求的 emit 注入。

    注意：原本实现是 request_emitter_var.reset(token)。但 set 在 chat()
    的 context 创建 token，而 gen() 被 Starlette 放到另一个 task/context
    驱动，token.reset() 会抛「Token was created in a different Context」。
    该异常冒泡导致 gen() 异常、StreamingResponse 连接被异常关闭（未正确发
    送终止 chunk），客户端收到 incomplete chunked read，前端表现为「连接已
    断开」。改用 set(None) 重置当前 context 的注入，彻底规避跨 context 的
    ValueError，且每个请求有独立 context，不会污染其他请求。
    """
    request_emitter_var.set(None)


def get_request_emitter() -> Emitter | None:
    return request_emitter_var.get()


async def push_to_request(ev: dict[str, Any]) -> bool:
    """把事件推入当前请求的 SSE 流。没有活动流时返回 False（调用方自行决定兜底）。"""
    emit = request_emitter_var.get()
    if emit is None:
        return False
    try:
        await emit(ev)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("push_to_request 失败: %s", exc)
        return False


def register(conn: Emitter) -> None:
    active_conns.add(conn)


def unregister(conn: Emitter) -> None:
    active_conns.discard(conn)


async def broadcast(ev: dict[str, Any]) -> None:
    """向所有全局订阅者广播事件（目前前端未订阅，属可选增强）。"""
    for conn in list(active_conns):
        try:
            await conn(ev)
        except Exception:  # noqa: BLE001
            active_conns.discard(conn)
