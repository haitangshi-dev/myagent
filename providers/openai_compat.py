"""统一 OpenAI 兼容流式客户端 — 任何 OpenAI 兼容接口通用（地址/密钥/模型名由使用者填写）。

所有四个 provider 都暴露 OpenAI Chat Completions 接口，因此共用一个 transport：
  - 流式增量解析（content / reasoning / tool_calls）
  - 每 provider 实例带限流器（NIM 40/min、相邻 ≥10s）
  - tool_calls 的 arguments 是分片下发的 JSON，这里攒齐后再整体抛出
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# 持久错误标记：命中则不重试，直接放弃（订阅墙 / 鉴权失败 / 用量或配额耗尽属此类）。
# 注意：ResourceExhausted / 429 限流（Worker 节点突发限流 / 并发超限 / RPM 超限）
# **不在**此列，视为瞬时错误，自动退避重试（稍后节点恢复即可正常）。
# ⚠️ 历史坑：曾用裸 "quota" 子串，会与下方限流标记 "quota exceeded" 冲突——
# 永久检查先命中 "quota" 导致限流重试分支永远不可达。故移除裸 "quota"，
# 改用更具体的短语（"usage limit" / "daily limit" / "limit reached" 等）判持久错误。
_PERMANENT_MARKERS = (
    "requires a subscription",
    "subscription required",
    "subscription",
    "403",
    "401",
    "402",
    "forbidden",
    "unauthorized",
    "invalid api key",
    "incorrect api key",
    "payment required",
    "insufficient",          # insufficient quota / insufficient credits
    "credit",                # no credits / out of credits
    "balance",               # insufficient balance
    "usage limit",           # FreeModel: "Usage limit reached, will reset ..."
    "daily limit",           # 日额度耗尽
    "out of credits",
    "no credits",
)


def _is_permanent_error(msg: str) -> bool:
    """判断错误是否为持久错误（重试无意义，应直接放弃）。"""
    low = (msg or "").lower()
    return any(k in low for k in _PERMANENT_MARKERS)


class RateLimiter:
    """滑动窗口 + 最小间隔 + 并发限制 限流器（异步安全）。

    max_rpm=0 / min_interval=0 表示不限流。
    max_concurrency=0 表示不限并发；≥1 时用 asyncio.Semaphore 保证同时最多
    max_concurrency 个请求在飞（NIM 免费版要求并发=1）。
    """

    def __init__(
        self,
        max_rpm: int = 0,
        min_interval: float = 0.0,
        max_concurrency: int = 0,
    ):
        self.max_rpm = max_rpm
        self.min_interval = min_interval
        self.max_concurrency = max_concurrency
        self._lock = asyncio.Lock()
        self._timestamps: list[float] = []
        self._last_call = 0.0
        # 并发信号量：0 = 不限；≥1 = 同时最多 N 个请求在飞
        self._sem: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        )

    async def acquire(self) -> None:
        # 先拿并发槽位（如果有限制）
        if self._sem:
            await self._sem.acquire()
        try:
            if self.max_rpm <= 0 and self.min_interval <= 0:
                return
            async with self._lock:
                while True:
                    now = time.monotonic()
                    # 最小间隔
                    wait = self.min_interval - (now - self._last_call)
                    # 滑动窗口（最近 60s 内请求数）
                    cutoff = now - 60.0
                    self._timestamps = [t for t in self._timestamps if t > cutoff]
                    if self.max_rpm > 0 and len(self._timestamps) >= self.max_rpm:
                        wait = max(wait, self._timestamps[0] + 60.0 - now)
                    if wait <= 0:
                        break
                    await asyncio.sleep(min(wait, 5.0))
                self._last_call = time.monotonic()
                self._timestamps.append(self._last_call)
        except BaseException:
            # acquire 失败时释放并发槽位
            if self._sem:
                self._sem.release()
            raise

    def release(self) -> None:
        """释放并发槽位（请求完成后调用）。"""
        if self._sem:
            self._sem.release()


class OpenAICompatClient:
    """一个 provider 一个实例，持有 base_url / api_key / model 与限流器。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        max_rpm: int = 0,
        min_interval: float = 0.0,
        max_concurrency: int = 0,   # 并发限制（NIM 免费版=1）
        timeout: float = 120.0,
        extra_headers: dict[str, str] | None = None,
        context_limit: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.limiter = RateLimiter(
            max_rpm=max_rpm,
            min_interval=min_interval,
            max_concurrency=max_concurrency,
        )
        self.context_limit = context_limit
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        max_retries: int = 0,
        # ★ 防重复参数（多数 OpenAI 兼容 API 支持）
        repetition_penalty: float = 1.0,    # >1 惩罚重复 token（推荐 1.1~1.2）
        frequency_penalty: float = 0.0,     # 出现过的 token 惩罚
        presence_penalty: float = 0.0,      # 已出现 topic 的 token 激励
        stop: list[str] | None = None,      # 停止序列列表
        # ★ 思考链开关（思考型模型专用）：
        #   None = 不注入该字段（保持 provider 默认行为，兼容不支持的 API）
        #   False = 显式禁用思考链（避免思考 token 吃光 max_tokens、拖慢首字）
        #   True  = 显式开启
        thinking: bool | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式对话。产出事件字典：

        content_delta / reasoning_delta / tool_call / usage / done / error

        全自动重试（对所有 OpenAI 兼容接口生效）：
          - **按错误类型动态退避**：瞬时错误（DNS 抖动 / 超时 / 连接断开）基础 1.5s 指数退避封顶 10s；
            限流错误（429 / ResourceExhausted / rate limit / quota exceeded）基础 3s 指数退避封顶 30s，
            给服务端恢复配额的时间，避免固定 1.5s 狂轰导致永远连不上。
          - 持久错误（订阅墙 / 鉴权失败 / 用量或配额耗尽 / 余额不足）→ 直接放弃，不重试。
          - max_retries=0 表示对瞬时错误「一直不行就一直重试」（无限重试）；
            建议调用方传有限值（如 8）以便最终给出干净错误而非无限重连。
        """
        # ★ 限流错误关键词（用于区分「普通瞬时错误」和「被限流了」）
        _RATE_LIMIT_MARKERS = (
            "429", "rate limit", "ratelimit", "too many requests",
            "resourceexhausted", "resource exhausted", "exceeded",
            "quota exceeded", "requests exceeded", "throttl",
        )

        def _is_rate_limit_error(msg: str) -> bool:
            low = (msg or "").lower()
            return any(k in low for k in _RATE_LIMIT_MARKERS)

        def _backoff_delay(is_rate_limit: bool, attempt: int) -> float:
            """按错误类型计算退避等待（秒）：

            - 限流（429 / ResourceExhausted / quota exceeded 等）：基础 3s、指数增长、封顶 30s，
              给服务端恢复配额/并发槽的时间，避免 1.5s 狂轰导致永远连不上。
            - 瞬时/连接错误（DNS 抖动 / 超时 / 连接断开）：基础 1.5s、指数增长、封顶 10s。
            均随重试次数指数退避，杜绝「固定 1.5s 无限重连」的「重连速度不对」问题。
            """
            base = 3.0 if is_rate_limit else 1.5
            cap = 30.0 if is_rate_limit else 10.0
            return min(base * (2 ** (attempt - 1)), cap)

        # ★ 重试间隔：按错误类型动态退避（见 _backoff_delay），不再固定 1.5s。
        last_err = "（未知错误）"

        await self.limiter.acquire()
        try:

            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": True,
            }
            # 温度：None/0.0 特殊处理 —— 0.0 是合法低温度（贪心），照传；
            # None 表示「不传温度」，让 provider 用服务端默认（对齐 Hermes 主模型行为）。
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens:
                payload["max_tokens"] = max_tokens
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = tool_choice
            # ★ 请求 usage（前端「上下文占用」可视化需要 prompt_tokens / completion_tokens）。
            # OpenAI 兼容协议标准字段，主流接口均支持；
            # 末尾会多推一个仅含 usage、choices 为空的 chunk，下方解析已兼容。
            payload["stream_options"] = {"include_usage": True}
            # ★ 防重复参数：仅当非默认值时才写入（避免破坏不支持的 provider）
            if repetition_penalty != 1.0:
                payload["repetition_penalty"] = repetition_penalty
            if frequency_penalty != 0.0:
                payload["frequency_penalty"] = frequency_penalty
            if presence_penalty != 0.0:
                payload["presence_penalty"] = presence_penalty
            if stop:
                payload["stop"] = stop
            # ★ 思考链开关：仅当调用方显式指定时才注入，避免破坏不支持该字段的 provider。
            # 思考型模型默认开思考链会吃光 max_tokens 导致返回空串。
            if thinking is not None:
                payload["thinking"] = {"type": "enabled" if thinking else "disabled"}

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            headers.update(self.extra_headers)

            # ★ 真实重试循环：本轮（瞬时错误 / 限流）失败后回到这里重发请求。
            # max_retries=0 表示对瞬时错误「一直不行就一直重试」（无限重试）；
            # max_retries>0 时达到上限才放弃。重试间隔按错误类型动态退避
            # （见 _backoff_delay：限流 3→6→12→24→30s 封顶，瞬时 1.5→3→6→10s 封顶），
            # 不再固定 1.5s 狂轰。
            attempt = 0
            while True:
                attempt += 1
                # 每轮重置累积状态（避免上一轮半成品污染下一轮）
                tool_acc: dict[int, dict[str, Any]] = {}
                tool_emitted: dict[int, bool] = {}  # idx -> 是否已下发 partial
                got_error = False
                finish_reason: str | None = None

                try:
                    async with self._client.stream(
                        "POST", f"{self.base_url}/chat/completions", json=payload, headers=headers
                    ) as resp:
                        if resp.status_code != 200:
                            body = await resp.aread()
                            msg = f"provider {resp.status_code}: {body.decode('utf-8', 'replace')[:500]}"
                            if _is_permanent_error(msg):
                                # 持久错误：直接放弃
                                yield {"type": "error", "message": msg}
                                return
                            last_err = msg
                            got_error = True
                        else:
                            async for line in resp.aiter_lines():
                                if not line:
                                    continue
                                if line.startswith(":"):
                                    continue
                                if not line.startswith("data:"):
                                    continue
                                data = line[len("data:"):].strip()
                                if data == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data)
                                except json.JSONDecodeError:
                                    continue

                                # ★ 流式块内错误：部分 provider（如 NIM）限流/超限时返回
                                # HTTP 200 但 body 里夹带 {"error": {...}}（code 常为 500）。
                                # 不拦截会让前端「什么也不输出」且无报错，必须在此兜底。
                                if chunk.get("error"):
                                    err_payload = chunk["error"]
                                    err_msg = (
                                        err_payload.get("message")
                                        if isinstance(err_payload, dict)
                                        else str(err_payload)
                                    )
                                    msg = f"provider error: {err_msg}"
                                    if _is_permanent_error(msg):
                                        # 持久错误：直接放弃
                                        yield {"type": "error", "message": msg}
                                        return
                                    last_err = msg
                                    got_error = True
                                    break

                                for choice in chunk.get("choices", []):
                                    delta = choice.get("delta", {})
                                    fr = choice.get("finish_reason")
                                    if fr:
                                        finish_reason = fr
                                    if delta.get("content"):
                                        yield {"type": "content_delta", "text": delta["content"]}
                                    reason = delta.get("reasoning_content") or delta.get("reasoning")
                                    if reason:
                                        yield {"type": "reasoning_delta", "text": reason}
                                    for tc in delta.get("tool_calls", []) or []:
                                        idx = tc.get("index", 0)
                                        slot = tool_acc.setdefault(
                                            idx, {"id": "", "name": "", "arguments": ""}
                                        )
                                        if tc.get("id"):
                                            slot["id"] = tc["id"]
                                        fn = tc.get("function", {})
                                        if fn.get("name"):
                                            slot["name"] = fn["name"]
                                        if fn.get("arguments"):
                                            slot["arguments"] += fn["arguments"]
                                        if slot["name"] and not tool_emitted.get(idx):
                                            tool_emitted[idx] = True
                                            yield {
                                                "type": "tool_call",
                                                "id": slot["id"] or f"call_{idx}",
                                                "name": slot["name"],
                                                "arguments": slot["arguments"] or "{}",
                                                "partial": True,
                                            }
                                        elif tool_emitted.get(idx) and slot["arguments"]:
                                            yield {
                                                "type": "tool_call",
                                                "id": slot["id"] or f"call_{idx}",
                                                "name": slot["name"],
                                                "arguments": slot["arguments"],
                                                "partial": True,
                                            }

                                if chunk.get("usage"):
                                    u = chunk["usage"]
                                    yield {
                                        "type": "usage",
                                        "prompt_tokens": u.get("prompt_tokens", 0),
                                        "completion_tokens": u.get("completion_tokens", 0),
                                    }
                                    # 前缀缓存命中统计（部分兼容接口在 usage 中返回）。
                                    # 缺失字段则不发事件，避免对不支持缓存的 provider 产生噪音。
                                    hit = u.get("prompt_cache_hit_tokens")
                                    miss = u.get("prompt_cache_miss_tokens")
                                    if hit is not None or miss is not None:
                                        yield {
                                            "type": "cache_usage",
                                            "hit": int(hit or 0),
                                            "miss": int(miss or 0),
                                        }

                            # 流正常结束（未出错）→ 抛出完整工具调用并收尾
                            if not got_error:
                                for idx in sorted(tool_acc):
                                    slot = tool_acc[idx]
                                    raw = slot["arguments"] or "{}"
                                    try:
                                        args = json.loads(raw)
                                    except json.JSONDecodeError:
                                        args = {"__raw__": raw}
                                    if tool_emitted.get(idx):
                                        yield {
                                            "type": "tool_call",
                                            "id": slot["id"] or f"call_{idx}",
                                            "name": slot["name"],
                                            "arguments": args,
                                        }
                                    else:
                                        yield {
                                            "type": "tool_call",
                                            "id": slot["id"] or f"call_{idx}",
                                            "name": slot["name"],
                                            "arguments": args,
                                        }
                                yield {"type": "done", "finish_reason": finish_reason}
                                return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("stream_chat error")
                    msg = str(exc)
                    if _is_permanent_error(msg):
                        # 持久错误：直接放弃
                        yield {"type": "error", "message": msg}
                        return
                    last_err = msg
                    got_error = True

                # ---- 到达此处说明本轮失败（got_error=True） ----
                # 瞬时/限流错误 → 按类型动态退避后进入下一轮循环重发请求。
                is_rate_limited = _is_rate_limit_error(last_err)
                wait = _backoff_delay(is_rate_limited, attempt)
                # max_retries>0 且已达上限 → 放弃（默认 0 = 无限重试）
                if max_retries and attempt >= max_retries:
                    yield {
                        "type": "error",
                        "message": f"重试 {attempt} 次仍失败（{'限流' if is_rate_limited else '瞬时错误'}）：{last_err}",
                    }
                    return
                # ★ 透传「模型连接重试」事件给前端：重试等待期间让用户看到
                # 「连接模型中…」而非一直显示「思考中」，明确区分「真在思考（流式推理链）」
                # 与「在重试 / 重连模型（被限流或网络抖动）」。delay 为真实退避秒数。
                yield {
                    "type": "retry",
                    "attempt": attempt,
                    "max": max_retries if max_retries else 0,  # 0 = 无限重试
                    "delay": wait,
                    "reason": last_err,
                    "is_rate_limit": is_rate_limited,
                }
                logger.warning(
                    "%s 第 %d 次请求未成功（%s），%.1fs 后重试：%s",
                    self.model, attempt,
                    "限流" if is_rate_limited else "瞬时错误",
                    wait, last_err[:200],
                )
                await asyncio.sleep(wait)
                # 注意：并发槽位 / RPM 已在函数开头 acquire 一次、finally 释放一次，
                # 这里不再重复 acquire（否则 max_concurrency=1 时会自锁）。
        finally:
            # ★ 确保并发槽位被释放（无论成功/失败/取消）
            self.limiter.release()
