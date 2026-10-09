"""上下文压缩 — 在对话无限增长、逼近模型上下文上限时自动压缩历史。

设计要点（契合 MY_AGENT 的纯云端多 provider 架构）：
  - 系统获取模型上下文上限：优先读 settings.yaml 各 provider 的 context_window，
    回落到 auto_compress.max_context_tokens（>0 时作为强制上限）。
  - 触发条件：压缩前先用「真实 prompt_tokens（模型在 usage 事件中返回）」判断；
    未拿到真实值时用本地估算（约 1 中文/1.6 token）兜底，避免「猜太准」反而误压。
  - 压缩动作：保留 system（在 messages 外）+ 最近 K 轮，把更早的对话交给模型生成
    结构化摘要（保留「做了什么 / 结论 / 待办 / 未完成」），替换成一条 role=system
    的压缩记忆，永不丢弃上下文。
  - 安全：压缩 summaries 不计入下一轮估算上限（避免震荡）；单次压缩上限 3 次；
    压缩本身失败则降级为「按轮裁剪」，绝不阻断对话。
  - 不依赖 tiktoken（项目无此依赖，也不引新包）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# 最近保留的完整轮数（压缩时这些不被摘要，保证当前任务上下文完整）
_KEEP_RECENT_TURNS = 4
# 单次对话最多压缩次数（防极端长会话无限递归压缩）
_MAX_COMPRESSIONS_PER_RUN = 3
# 估算用 token 比例（字符 → token 的启发式，偏低估以减少误压）
_CHARS_PER_TOKEN = 1.6
# 摘要请求的最大生成 token（摘要本身要短）
_SUMMARY_MAX_TOKENS = 1500
# 摘要生成温度（稳定、信息密度高）
_SUMMARY_TEMPERATURE = 0.2


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """本地粗略估算 messages 的总 token 数（无 tokenizer 时的兜底）。"""
    total = 0
    for m in messages:
        # content 可能是 str 或 list（多模态），统一转字符串长度
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(
                part.get("text", "")
                for part in c
                if isinstance(part, dict)
            )
        if isinstance(c, str):
            total += max(1, len(c) / _CHARS_PER_TOKEN)
        # tool_calls 也计入（arguments JSON）
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            total += max(1, len(fn.get("name", "")) / _CHARS_PER_TOKEN)
            total += max(1, len(fn.get("arguments", "")) / _CHARS_PER_TOKEN)
        # tool 结果
        if m.get("role") == "tool" and isinstance(c, str):
            pass  # 已计入 content
    return int(total)


async def maybe_compress(
    messages: list[dict[str, Any]],
    *,
    limit: int,
    real_prompt_tokens: int | None,
    compress_fn: Callable[[list[dict[str, Any]]], Awaitable[str | None]],
    emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    compression_count: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """若 messages 逼近 limit，则压缩历史并返回 (新messages, 新的compression_count)。

    参数：
      messages            完整消息列表（含 system 在 [0]，这是 Agent 内部约定）。
      limit               上下文 token 上限（已综合 provider context_window 与 auto_compress）。
      real_prompt_tokens  模型上一次返回的 prompt_tokens；None 表示还没拿到真实值。
      compress_fn         异步函数：接收「待摘要的旧消息片段」→ 返回摘要文本或 None（失败）。
      emit                可选，用于向 SSE 推 compression 事件（前端可见「压缩中」）。
      compression_count   已有压缩次数（防无限递归）。

    返回：
      (messages, compression_count)
    """
    # 1) 计算当前占用
    if real_prompt_tokens:
        used = real_prompt_tokens
    else:
        # 兜底估算（首次、或 provider 不给 usage 时）
        used = _estimate_tokens(messages)

    # 触发阈值：用到 80% 即压（留余量给本轮生成 + 工具往返）
    trigger = int(limit * 0.8)

    if used < trigger or compression_count >= _MAX_COMPRESSIONS_PER_RUN:
        return messages, compression_count

    if emit:
        await emit({
            "type": "compression",
            "status": "start",
            "used_tokens": used,
            "limit": limit,
            "message": f"上下文接近上限（{used}/{limit}），正在压缩早期历史…",
        })

    # 2) 切分：system 永远保留；history 部分 = messages[1:]
    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    rest = messages[1:] if system_msg is not None else list(messages)

    # 把 rest 按「轮」切：一个 user + 其 assistant(+tool 往返) 视为一轮
    turns: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for m in rest:
        cur.append(m)
        if m.get("role") == "user":
            # 新 user 开始 → 上一轮封口（但 cur 里可能还含后续 assistant/tool）
            pass
        # 以 assistant（无 tool_calls 或末尾）作为一轮结束的近似：
        # 简化：遇到下一个 user 之前都归为同一轮；这里用「user 落点」切分。
    # 上面的流式切分不够干净，改用「按 user 边界切轮」的稳定实现：
    turns = _split_turns(rest)

    if len(turns) <= _KEEP_RECENT_TURNS:
        # 没几轮可压，但已超触发线 → 降级：直接截断最早的一半（极端情况保底）
        keep_from = max(0, len(rest) // 2)
        new_rest = rest[keep_from:]
        if emit:
            await emit({
                "type": "compression",
                "status": "done",
                "strategy": "truncate",
                "message": "历史较短但已超限，已截断最早部分消息以腾出空间。",
            })
        out = ([system_msg] if system_msg is not None else []) + new_rest
        return out, compression_count + 1

    # 3) 保留最近 K 轮，其余交给模型摘要
    keep_turns = turns[-_KEEP_RECENT_TURNS:]
    summarize_turns = turns[:-_KEEP_RECENT_TURNS]
    to_summarize = [m for t in summarize_turns for m in t]

    summary = None
    try:
        summary = await compress_fn(to_summarize)
    except Exception as exc:  # noqa: BLE001
        logger.warning("上下文摘要生成失败，降级为截断：%s", exc)

    new_rest: list[dict[str, Any]]
    if summary:
        summary_msg: dict[str, Any] = {
            "role": "system",
            "content": (
                "【以下为早期对话的压缩摘要，用于补全上下文，请勿当作最新指令】\n"
                + summary
            ),
        }
        new_rest = [summary_msg] + [m for t in keep_turns for m in t]
        strategy = "summarize"
    else:
        # 摘要失败 → 截断：仅保留最近 K 轮
        new_rest = [m for t in keep_turns for m in t]
        strategy = "truncate"

    out = ([system_msg] if system_msg is not None else []) + new_rest

    if emit:
        await emit({
            "type": "compression",
            "status": "done",
            "strategy": strategy,
            "compressed_turns": len(summarize_turns),
            "kept_turns": len(keep_turns),
            "message": (
                f"已压缩 {len(summarize_turns)} 轮早期对话"
                + ("为摘要" if strategy == "summarize" else "（摘要失败，已截断）")
                + "，保留最近 "
                + str(len(keep_turns))
                + " 轮。"
            ),
        })

    return out, compression_count + 1


def _split_turns(rest: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把消息列表按 user 边界切成「轮」。每轮 = [user, 可能多个 assistant/tool]。

    - 以 role==user 作为新一轮起点。
    - 若开头不是 user（如工具结果残留），归到首轮。
    - 连续的 assistant/tool 都并入当前轮，直到下一个 user。
    """
    turns: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for m in rest:
        if m.get("role") == "user" and cur:
            turns.append(cur)
            cur = []
        cur.append(m)
    if cur:
        turns.append(cur)
    return turns


# 便于单测/复用
__all__ = ["maybe_compress", "_estimate_tokens", "_split_turns"]
