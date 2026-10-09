"""Agent 循环 — 流式对话 + 工具执行（仿 Hermes conversation loop 的核心骨架）。

关键约束（与 Hermes 一致）：
  - 工具调用的原始 JSON 参数绝不下发前端，只下发 build_tool_label / build_tool_preview。
  - 思考链（reasoning）与正文（content）分两条流，前端分别渲染。
  - 工具在服务端执行，结果回灌模型继续下一轮，直到模型不再调用工具。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Callable, Awaitable

from agents.display import build_tool_label, build_tool_preview, build_tool_command
from agents.tools import execute_tool, get_tool_schemas, TOOLS, build_approval_info, get_tool_kind, protected_block_reason, needs_delete_confirm
from agents.context_compress import maybe_compress
from agents import global_memory as _gmem
from agents import checkpoint as _checkpoint

logger = logging.getLogger(__name__)

# 工具结果给前端的预览上限
_TOOL_PREVIEW_CAP = 600

# =========================================================================
# 主模型输出质量防护 — 应对「模型偶尔发神经」类低概率异常输出
# =========================================================================
# 判定为「噪声/无效输出」的阈值
_NOISE_MIN_LEN = 8          # 低于此长度视为无意义碎片
_GIBBERISH_RATIO = 0.6      # 不可打印字符占比超此值视为乱码
_REPEAT_BLOCK_MAX = 4       # 同一重复块出现次数上限（短块循环刷屏）


def _is_noise_output(text: str) -> tuple[bool, str]:
    """粗略判断一段正文是否为「模型发神经」式的无效输出。

    返回 (is_noise, reason)。仅做轻量启发式，避免误杀正常输出：
      - 乱码：大量替换方块(■)/控制字符/无意义符号字符（非中文、非 ASCII 字母数字、非常规标点）；
      - 重复碎片：同一短块（>=2 字符）高频重复（如「啊啊啊啊」或「...」×N）；
      - 极短空话：< _NOISE_MIN_LEN 且非工具调用。
    """
    if not text:
        return False, ""
    stripped = text.strip()
    if len(stripped) < _NOISE_MIN_LEN:
        return False, ""
    # 乱码检测：统计「异常字符」——既非 CJK、也非 ASCII 字母数字、也非常规标点空白
    _NORMAL_PUNCT = set("，。、；：！？“”‘’（）《》【】…—·.,;:!?\"'()[]{}<>-_=+*/\\|@#$%^&~` \n\t")
    bad = 0
    for ch in stripped:
        if "一" <= ch <= "鿿":
            continue  # 中文正常
        if ch.isascii() and (ch.isalnum() or ch in _NORMAL_PUNCT):
            continue
        if ch in _NORMAL_PUNCT:
            continue
        bad += 1
    if stripped and bad / len(stripped) > _GIBBERISH_RATIO:
        return True, "输出含大量乱码/异常字符"
    # 重复碎片检测：相邻 2~4 字符块出现次数过多（整段由少量字符循环构成）
    for blen in (2, 3, 4):
        if len(stripped) >= blen * 3:
            block = stripped[:blen]
            if block.strip() and stripped.count(block) * blen > len(stripped) * 0.8:
                return True, "输出为重复字符碎片"
    return False, ""


def _looks_like_bare_json_or_code(text: str) -> bool:
    """判断正文是否几乎整段是裸 JSON / 代码块却未走工具调用（模型「发神经」典型表现）。

    仅当正文绝大部分被 ``` 代码围栏 或 首字符 {/[ 包围的 JSON 占据，且明显不是自然语言回答时触发。
    """
    s = text.strip()
    if len(s) < 12:
        return False
    # 整体被 ``` 代码块包裹且前面没有自然语言解释
    if s.startswith("```") and s.count("```") >= 2:
        head = s[: s.find("```")]
        if not any(kw in head for kw in ("根据", "以下是", "我", "请", "已", "：", ":", "。")):
            return True
    # 首字符为 { 或 [ 且整段像 JSON（括号配平、几乎无中文叙述）
    if s[0] in "{[":
        cn = sum(1 for ch in s if "一" <= ch <= "鿿")
        opens = s.count("{") + s.count("[")
        closes = s.count("}") + s.count("]")
        if opens > 2 and opens == closes and cn < 3:
            return True
    return False


Emitter = Callable[[dict[str, Any]], Awaitable[None]]


# =========================================================================
# 审批闸门 — 需确认的工具（如 delete_file）在跑前阻塞，等前端回传决策
# =========================================================================
PENDING_APPROVALS: dict[str, asyncio.Future] = {}


# =========================================================================
# 取消信号 — 前端“停止”时由 /api/cancel 或 SSE 断连设置，agent 在关键点检测并提前退出
# =========================================================================
CANCEL_EVENTS: dict[str, asyncio.Event] = {}


def request_cancel(session_id: str) -> None:
    """标记某个会话的当前对话应被取消。幂等：重复调用安全。"""
    ev = CANCEL_EVENTS.get(session_id)
    if ev is None:
        ev = asyncio.Event()
        CANCEL_EVENTS[session_id] = ev
    ev.set()


def cancel_requested(session_id: str) -> bool:
    ev = CANCEL_EVENTS.get(session_id)
    return ev is not None and ev.is_set()


def clear_cancel(session_id: str) -> None:
    CANCEL_EVENTS.pop(session_id, None)


# =========================================================================
# 自动记忆抽取 — 节流 + 单并发锁（避免每轮都打 LLM，也避免并发写冲突）
# =========================================================================
_AUTO_MEM_LAST = 0.0  # 上次抽取的时间戳（time.monotonic）
_AUTO_MEM_INTERVAL = 15.0  # 节流间隔（秒）
_AUTO_MEM_LOCK = asyncio.Lock()


async def _schedule_auto_memory(client, user_input: str, assistant_text: str, settings: dict) -> None:
    """后台抽取本轮对话的关键事实写入全局记忆（不阻塞主回答）。

    - 节流：距上次抽取不足 15s 跳过（避免刷屏式调用 LLM）。
    - 单并发：asyncio.Lock 防并发写冲突。
    - 全量 try/except 包住，失败仅记日志，不影响主对话。
    """
    global _AUTO_MEM_LAST
    try:
        async with _AUTO_MEM_LOCK:
            now = time.monotonic()
            if now - _AUTO_MEM_LAST < _AUTO_MEM_INTERVAL:
                return
            _AUTO_MEM_LAST = now
            n = await _gmem.extract_and_store(
                client=client,
                user_text=user_input,
                assistant_text=assistant_text,
                settings=settings,
            )
            # 抽取后尝试压缩（best-effort，内部有阈值 + 异常保护）
            await _gmem.maybe_auto_summarize(settings)
            if n:
                logger.info("自动记忆抽取：新增/更新 %d 条", n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动记忆抽取调度失败: %s", exc)


async def wait_for_approval(approval_id: str, timeout: float = 300.0) -> str:
    """阻塞等待前端对 approval_id 的决策。超时（默认 5 分钟）或取消一律视为 deny。"""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    PENDING_APPROVALS[approval_id] = fut
    try:
        # shield：超时取消的是外层 waiter，内部 fut 不被取消，仍可被后续 resolve 设置
        decision = await asyncio.wait_for(asyncio.shield(fut), timeout)
        return decision if decision in ("approve", "deny") else "deny"
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return "deny"  # 默认拒绝（超时未处理）
    finally:
        PENDING_APPROVALS.pop(approval_id, None)


def resolve_approval(approval_id: str, decision: str) -> bool:
    """由 /api/approve 调用来放行/拒绝某个待审批操作。返回是否成功唤醒。"""
    fut = PENDING_APPROVALS.get(approval_id)
    if fut is None or fut.done():
        return False
    fut.set_result("approve" if decision == "approve" else "deny")
    return True


def _tool_result_preview(result: str) -> str:
    from agents.display import redact_sensitive_text

    text = redact_sensitive_text(result)
    if len(text) > _TOOL_PREVIEW_CAP:
        return text[:_TOOL_PREVIEW_CAP] + "…"
    return text


# ★ 工具结果回灌截断（2026-08-08）：模型上下文里的工具结果「留头留尾」。
# 头尾各保留 _TRUNC_HEAD/_TRUNC_TAIL 字符，中间省略，并明确告知模型
# 「内容已截断、可要求继续读」——避免大文件/长输出全量塞回烧 token。
# 截断只发生在回灌模型的消息上；检查点 raw_tool_result 仍存完整结果。
_TRUNC_HEAD = 4000    # 保留头部字符数
_TRUNC_TAIL = 1500    # 保留尾部字符数
_TRUNC_MIN = 8000     # 低于此长度不截断（小结果全量给，保证小任务质量）


def _truncate_for_model(result: str) -> str:
    """把工具结果压缩成适合回灌模型的形态：小结果原样，大结果留头留尾。"""
    text = str(result or "")
    if len(text) <= _TRUNC_MIN:
        return text
    head = text[:_TRUNC_HEAD]
    tail = text[-_TRUNC_TAIL:]
    omitted = len(text) - _TRUNC_HEAD - _TRUNC_TAIL
    return (
        f"{head}\n\n"
        f"……[中间 {omitted} 字符已省略；如需查看被截断的部分，告诉我你想看哪一段，"
        f"我可以重新读取/分页提供]……\n\n{tail}"
    )


def _checkpoint_step_desc(tc: dict, result: str) -> str:
    """把一次工具调用转成一句检查点进度描述（写入 session_state.json）。"""
    name = tc.get("name", "?")
    args = tc.get("arguments") or {}
    if name == "shell" and isinstance(args, dict):
        cmd = str(args.get("command") or "")[:80]
        return f"执行命令：{cmd}"
    if name == "write_file" and isinstance(args, dict):
        return f"写入文件：{args.get('path')}"
    if name == "read_file" and isinstance(args, dict):
        return f"读取文件：{args.get('path')}"
    if name in ("http_request", "download") and isinstance(args, dict):
        return f"网络请求：{args.get('method', 'GET')} {str(args.get('url') or '')[:100]}"
    if name in ("email_send",) and isinstance(args, dict):
        to = args.get("to")
        return f"发送邮件给：{to if isinstance(to, str) else (', '.join(to) if isinstance(to, list) else to)}"
    if name == "task_board" and isinstance(args, dict):
        return f"任务看板 {args.get('action')}：{str(args.get('tasks') or args.get('task_id') or '')[:60]}"
    if name == "kb_search":
        return f"检索知识库：{str(args.get('query') or '')[:60]}"
    # 默认：工具名 + 结果摘要（取结果第一行）
    first_line = (result or "").strip().splitlines()
    summary = (first_line[0] if first_line else "")[:80]
    return f"调用 {name}：{summary}"


def _validate_tool_args(name: str, args: Any) -> str | None:
    """执行前校验工具参数。

    返回错误字符串表示「应跳过执行并向前端报错」；返回 None 表示可正常执行。
    覆盖两类畸形：
      1. 参数 JSON 解析失败被塞进 {"__raw__": ...}（不该进入执行）；
      2. 缺少工具 schema 的必填字段（如 shell 缺 command），避免执行时崩出
         “错误：缺少 command” 这类裸错。
    """
    if isinstance(args, str):
        return (
            f"模型调用参数解析失败：工具 {name} 收到的参数不是合法 JSON，"
            "无法执行。"
        )
    if not isinstance(args, dict):
        return f"模型调用参数格式异常：工具 {name} 收到非字典参数，无法执行。"
    if "__raw__" in args and len(args) == 1:
        snippet = str(args.get("__raw__") or "")[:80]
        return (
            f"模型调用参数解析失败：工具 {name} 收到的参数不是合法 JSON"
            f"（{snippet}），无法执行。"
        )
    meta = TOOLS.get(name, {})
    required = (meta.get("parameters") or {}).get("required") or []
    missing = [r for r in required if not args.get(r)]
    if missing:
        return (
            f"模型调用参数不完整：工具 {name} 缺少必填字段 {missing}。"
            "请检查模型是否按工具 schema 传参（例如 shell 必须包含 command 字段）。"
        )
    return None


# =========================================================================
# 模型连接重试 — 连接/超时/限流等瞬时错误自动重试，并把重试状态渲染到前端
# =========================================================================
_RETRY_BACKOFF = [1.5, 1.5, 1.5]  # 第 1/2/3 次重试前的等待秒数（固定 1.5s，不分级）
_MAX_ATTEMPTS = 3


def _is_retryable_error(err: dict[str, Any]) -> bool:
    """判断模型返回的错误是否值得重试。

    - 401/403、鉴权失败、API key 无效：永久错误，绝不重试（重试无意义且浪费额度）。
    - 429 限流 / 5xx / 超时 / 连接中断 / 服务过载：瞬时错误，应重试。
    - 其余未知错误：默认重试（更保险，避免偶发网络抖动直接失败）。
    """
    msg = (err.get("message") or "").lower()
    # 限流（429）一律视为瞬时错误，应重试（即使文案里含 "api key" 等字样）
    if "429" in msg or "too many requests" in msg or "rate limit" in msg:
        return True
    # 以下为永久错误，绝不重试（重试无意义且会反复重连刷屏）：
    #  402 额度/余额不足、401/403 鉴权失败、api key 无效
    permanent = (
        " 401", "401 ", " 403", "403 ", " 402", "402 ",
        "unauthorized", "forbidden", "invalid api", "incorrect api",
        "authentication failed", "api key", "insufficient", "payment",
        "credit", "balance", "quota",
    )
    if any(k in msg for k in permanent):
        return False
    # 其余未知错误默认重试（避免偶发网络抖动直接失败）
    return True


async def _call_model_with_retry(
    client,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    # ★ 防重复
    repetition_penalty: float = 1.15,   # 主流接口推荐 1.1~1.2，显著减少循环
    frequency_penalty: float = 0.3,     # 已出现 token 轻度惩罚
    stop: list[str] | None = None,      # 停止序列（常见中文重复模式）
    thinking: bool | None = None,       # 思考链开关；None=不注入（保持 provider 默认）
) -> AsyncIterator[dict[str, Any]]:
    """直接透传 client.stream_chat 的事件。

    重试逻辑已下沉到 OpenAICompatClient.stream_chat 内部（统一固定 1.5s、
    所有 provider 通用、max_retries=0 表示瞬时错误无限重试）。这里**不再二次重试**，
    否则会与 stream_chat 的重试循环打架，导致「只显示一次连接中却不真正重发请求」。
    """
    kwargs: dict[str, Any] = {}
    if thinking is not None:
        kwargs["thinking"] = thinking
    async for ev in client.stream_chat(
        messages, tools=tools, temperature=temperature, max_tokens=max_tokens,
        repetition_penalty=repetition_penalty,
        frequency_penalty=frequency_penalty,
        stop=stop,
        max_retries=8,
        **kwargs,
    ):
        yield ev


class Agent:
    def __init__(
        self,
        client,  # OpenAICompatClient
        system_prompt: str,
        *,
        context_limit: int | None = None,
        settings: dict | None = None,
        fallback_client=None,  # 主 provider 连续异常/噪声时的稳定兜底大脑
        allowed_tools: list[str] | None = None,  # 工具白名单：非空时只允许这些工具（极简模式用）
        eager_memory: bool = True,  # 是否每轮主动把长期记忆注入上下文；关闭则改为按需检索
        auto_approve: bool = False,  # 无人值守场景（邮件网关等）：跳过前端审批弹窗，直接放行
        thinking: bool | None = None,  # 思考链开关，透传给 stream_chat；None=不注入
        temperature: float | None = None,  # 采样温度；None=从 settings.agent.temperature 读（默认 0.1）
    ):
        self.client = client
        self.system_prompt = system_prompt
        self.context_limit = context_limit
        self._settings = settings or {}
        self._fallback_client = fallback_client
        self._fallback_used = False      # 本次对话是否已切到兜底
        self._consecutive_failures = 0   # 主 provider 连续错误/噪声计数
        # 工具白名单：None=全部工具；set()（由空列表 [] 传入）=零工具；
        # 非空 set=仅白名单。注意区分 None 与空集合（空集合是「不给任何工具」）。
        self._allowed_tools = None if allowed_tools is None else set(allowed_tools)
        # 主动记忆注入开关：False 时不每轮把长期记忆喂给模型，
        # 改由模型在需要时通过 memory_recall 工具自行检索。
        self._eager_memory = eager_memory
        # ★ 无人值守自动批准：邮件网关等后台场景没有前端可弹窗，等待审批会永久挂起。
        # 开启后跳过 wait_for_approval，但**受保护目录硬安全网（protected_block_reason）仍然生效**。
        self._auto_approve = bool(auto_approve)
        # 思考链开关（思考型模型需显式关闭，否则思考 token 吃光 max_tokens）
        self._thinking = thinking
        # 采样温度：统一 0.15（用户指定）。
        #  - 显式传入 → 用传入值
        #  - 否则读 settings.agent.temperature（默认 0.15）
        _agent_cfg = (self._settings.get("agent", {}) or {})
        _cfg_temp = _agent_cfg.get("temperature", 0.15)
        self._temperature = temperature if temperature is not None else float(_cfg_temp or 0.15)
        # 前缀缓存优化开关：开启时把「长期记忆」注入最新 user 消息尾部（而非 system 头部），
        # 以冻结 system+history 前缀、提升接口前缀缓存命中率；关闭则回退旧行为。
        self._prefix_cache_optimize = bool(
            (self._settings.get("agent", {}) or {}).get("prefix_cache_optimize", True)
        )

    def _resolve_context_limit(self) -> int | None:
        """系统获取模型上下文上限，优先级：注入值 > provider.context_window > auto_compress > 客户端默认。

        返回值单位 token。None 表示「不限制」（如 V4-Flash 1M 且 auto_compress=0）。
        """
        # 1) 调用方显式注入（server/api.py / scheduler 已解析好）
        if self.context_limit:
            return int(self.context_limit)
        # 2) auto_compress.max_context_tokens > 0 作为强制上限
        ac = self._settings.get("auto_compress", {}) or {}
        ac_val = int(ac.get("max_context_tokens", 0) or 0)
        if ac_val > 0:
            return ac_val
        # 3) provider 配置里的 context_window
        prov = self._settings.get("providers", {}) or {}
        for cfg in prov.values():
            cw = cfg.get("context_window")
            if cw:
                return int(cw)
        # 4) 客户端默认值（OpenAICompatClient.context_limit，构造时可选）
        cl = getattr(self.client, "context_limit", None)
        if cl:
            return int(cl)
        # 全部缺失 → 不自动压缩（避免误伤 1M 模型）
        return None

    async def run(
        self,
        history: list[dict[str, Any]],
        user_input: str,
        emit: Emitter,
        ctx: dict | None = None,
        session_id: str | None = None,
        images: list[str] | None = None,
    ) -> None:
        """执行一轮对话（可能包含多轮工具调用）。事件通过 emit 推给调用方。

        ctx 为工作区上下文（聊天会话为 None），透传给 execute_tool 以施加沙盒/记忆隔离。
        session_id 用于“停止”信号：前端 /api/cancel 或 SSE 断连会设置取消标志，
        本方法在流式输出与工具执行的关键点检测并提前退出。
        images 为内联图片列表（data URL），非空时把 user 消息构造成多模态 content 数组。
        """
        # ★ 权限档位（2026-08-07）：从 ctx 读取前端滑动条设置（low/medium/high）
        #   档位决定模型可见/可用的工具集；模型可经 request_permission 申请临时提权。
        self._permission_level = str((ctx or {}).get("permission_level") or "medium").lower()
        if self._permission_level not in ("low", "medium", "high"):
            self._permission_level = "medium"
        # 每轮临时提权（用户确认后置位，本轮结束重置）
        self._escalated_level: str | None = None
        message_id = uuid.uuid4().hex
        await emit({"type": "message_start", "message_id": message_id})

        sid = session_id
        last_finish_reason: str | None = None

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        messages.extend(history)

        # 多模态：当存在内联图片时，user 消息用 content 数组（文本 + 图片）构造；
        # 否则保持纯文本字符串（兼容旧逻辑与 history 中既有纯文本消息）。
        imgs = images or []
        # ★ 会话检查点恢复：同一会话「继续」时注入上次现场摘要，秒回现场
        cp_block = ""
        if sid:
            try:
                cp_block = _checkpoint.checkpoint_block(sid)
            except Exception:  # noqa: BLE001
                cp_block = ""
        if imgs:
            content: Any = [{"type": "text", "text": user_input or ""}]
            if cp_block:
                content.append({"type": "text", "text": "\n\n" + cp_block})
            for img in imgs:
                content.append(
                    {"type": "image_url", "image_url": {"url": img}}
                )
            messages.append({"role": "user", "content": content})
        else:
            merged_input = (user_input or "") + (("\n\n" + cp_block) if cp_block else "")
            messages.append({"role": "user", "content": merged_input})

        # ★ 自动记忆召回（MemGPT 双层，2026-08-08）：
        #   第一层=完整记忆索引（build_index，很轻）→ 模型知道库里有什么；
        #   第二层=BM25 相关召回（recall_text）→ 当前输入直接命中内容。
        #   命中足够时不再重复注入索引（省 token）；命中少时补索引引导
        #   模型主动调 memory_recall 检索细节。
        mem_block = ""
        index_block = ""
        if self._eager_memory:
            try:
                mem_block = _gmem.recall_text(user_input, limit=8)
                # 命中少于 3 条 → 附上索引（让模型知道还有可查的记忆）
                if len([l for l in mem_block.splitlines() if l.strip().startswith("-")]) < 3:
                    index_block = _gmem.build_index(limit=30)
            except Exception as exc:  # noqa: BLE001
                mem_block = ""
                index_block = ""
                logger.warning("记忆召回失败: %s", exc)

        mem_block = mem_block or ""
        index_block = index_block or ""
        if mem_block or index_block:
            parts = []
            if mem_block:
                parts.append("【与当前话题相关的长期记忆】\n" + mem_block)
            if index_block:
                parts.append(
                    "【全部长期记忆索引（需要更多细节时，可调 memory_recall 检索具体条目）】\n"
                    + index_block
                )
            mem_suffix = "\n\n" + "\n\n".join(parts)
            if self._prefix_cache_optimize:
                # 优化路径：拼到【最新 user 消息尾部】，永不改写 system 头部，
                # 保 system+history 前缀字节级稳定 → 命中接口前缀缓存。
                last = messages[-1]
                if isinstance(last.get("content"), list):
                    # 多模态 content 数组：追加一个 text part 到末尾
                    last["content"].append(
                        {"type": "text", "text": mem_suffix}
                    )
                else:
                    last["content"] = (last.get("content") or "") + mem_suffix
            else:
                # 兼容/回退路径：旧行为，注入 system 头部（前缀会随记忆变化而断裂）
                messages[0] = {
                    "role": "system",
                    "content": self.system_prompt + mem_suffix,
                }

        schemas = get_tool_schemas(self._allowed_tools, permission_level=self._permission_level)

        # ---- 上下文压缩：系统获取模型上下文上限 ----
        # 优先 provider 配置的 context_window（在 server/api.py 或 scheduler 注入），
        # 回落到 auto_compress.max_context_tokens（>0 强制上限），
        # 再回落到客户端自带默认值（OpenAICompatClient 构造时未带则 None）。
        limit = self._resolve_context_limit()
        # 模型真实返回的 prompt_tokens（usage 事件），用于精确判断是否超阈值
        _real_prompt_tokens: int | None = None
        _compression_count = 0

        async def _compress(old_msgs: list[dict[str, Any]]) -> str | None:
            """把待摘要片段交给当前模型生成结构化摘要。返回文本或 None（失败）。"""
            summ_sys = (
                "你是一个对话历史压缩器。请把下面的多轮对话压缩成一段简洁的"
                "结构化中文摘要，必须保留：1) 已完成的任务与关键结论；2) 关键文件"
                "路径 / 命令 / 数据；3) 待办与未完成事项；4) 用户的偏好与约束。"
                "不要复述闲聊，不要遗漏工具执行的关键结果。控制在 400 字以内。"
            )
            payload_msgs = [
                {"role": "system", "content": summ_sys},
                *old_msgs,
                {"role": "user", "content": "请输出压缩摘要。"},
            ]
            collected = ""
            try:
                async for ev in self.client.stream_chat(
                    payload_msgs,
                    temperature=0.15,
                    max_tokens=1500,
                    repetition_penalty=1.1,   # 压缩任务也防重复
                ):
                    if ev.get("type") == "content_delta":
                        collected += ev["text"]
                    elif ev.get("type") == "error":
                        logger.warning("压缩摘要生成出错：%s", ev.get("message"))
                        return None
                return collected.strip() or None
            except Exception as exc:  # noqa: BLE001
                logger.warning("压缩摘要生成异常：%s", exc)
                return None

        while True:
            if cancel_requested(sid):
                await emit({"type": "cancelled"})
                await emit({"type": "message_end"})
                return

            # ★ 上下文压缩：逼近模型上限时自动压缩早期历史（limit=None 表示不限制）
            if limit:
                messages, _compression_count = await maybe_compress(
                    messages,
                    limit=limit,
                    real_prompt_tokens=_real_prompt_tokens,
                    compress_fn=_compress,
                    emit=emit,
                    compression_count=_compression_count,
                )

            assistant_text = ""
            assistant_reasoning = ""
            tool_calls: list[dict[str, Any]] = []
            _switched_to_fallback = False  # 本轮主模型报错已切兜底 → 跳过本轮收尾直接重开
            # ★ 重复输出检测：防止模型死循环刷同一句话（如截图中的「好的,让我读取...」×15）
            _repeat_detect_window = 60  # 检测窗口（约一句中文）
            _repeat_max = 2            # 允许重复次数（从 3 降到 2，更早截断）

            # ★ 停止序列：常见中文重复/无意义模式，命中后 API 立即停止生成
            _stop_seqs = [
                "\n\n好的，让我",      # 反复"好的，让我..."循环
                "\n\n好的,让我",
                "让我检查", "让我读取", "让我先检查",
                "让我直接", "让我来",
                "以下是重复的", "我重复一遍",
                "如上所述", "同上",
                "（重复）", "(repeat)",
            ]

            async for ev in _call_model_with_retry(
            self.client, messages, schemas,
            temperature=self._temperature,   # 温度来自 settings（默认 0.1）→ 工具调用更确定，减少发散
            max_tokens=8192,   # 上限防死循环刷屏（V4-Flash 1M 上下文但单轮不需无限）
            repetition_penalty=1.15,   # 惩罚重复 token（多数 OpenAI 兼容接口支持）
            frequency_penalty=0.3,     # 已出现 token 轻度惩罚
            # stop 序列上限 3：NIM/vLLM 要求 stop 数量 <4，超了直接 500；
            # 主流 OpenAI 兼容接口同样支持 ≤3。重复抑制主要靠 penalty，stop 仅兜底最典型循环模式。
            stop=_stop_seqs[:3],
            thinking=self._thinking,
        ):
                etype = ev.get("type")
                if etype == "content_delta":
                    new_text = ev["text"]
                    assistant_text += new_text
                    # 重复检测：取末尾窗口，在全文中计数
                    if len(assistant_text) > _repeat_detect_window * 2:
                        tail = assistant_text[-_repeat_detect_window:]
                        # 简单子串计数（O(n) 但文本不长）
                        count = assistant_text.count(tail)
                        if count >= _repeat_max:
                            await emit({
                                "type": "error",
                                "message": f"模型输出重复（已出现 {count} 次），已自动停止。"
                            })
                            await emit({"type": "message_end"})
                            return
                    await emit({"type": "content", "text": new_text})
                elif etype == "reasoning_delta":
                    assistant_reasoning += ev["text"]
                    await emit({"type": "reasoning", "text": ev["text"]})
                elif etype == "tool_call":
                    # ★ 参数归一：客户端对 partial（分片）下发的是 arguments 字符串，
                    # 完整事件已是 dict；统一 coerce 成 dict，避免 build_tool_label 内部 .get 崩溃。
                    raw_args = ev.get("arguments")
                    if isinstance(raw_args, str):
                        try:
                            ev["arguments"] = json.loads(raw_args) if raw_args.strip() else {}
                        except json.JSONDecodeError:
                            ev["arguments"] = {"__raw__": raw_args}
                    # partial 事件：name 出现即建卡，arguments 片段逐步刷新「模型输入命令」
                    if ev.get("partial"):
                        label = build_tool_label(ev["name"], ev["arguments"]) or ev["name"]
                        preview = build_tool_preview(ev["name"], ev["arguments"])
                        command = build_tool_command(ev["name"], ev["arguments"])
                        await emit(
                            {
                                "type": "tool_call",
                                "id": ev["id"],
                                "name": ev["name"],
                                "kind": get_tool_kind(ev["name"]),  # 'tool' | 'skill'
                                "label": label,
                                "preview": preview,
                                "command": command,  # 模型原始输入（脱敏）
                                "status": "running",
                                "partial": True,
                            }
                        )
                        continue
                    # 完整事件：定型卡片（带完整 arguments）
                    tool_calls.append(ev)
                    label = build_tool_label(ev["name"], ev["arguments"]) or ev["name"]
                    preview = build_tool_preview(ev["name"], ev["arguments"])
                    command = build_tool_command(ev["name"], ev["arguments"])
                    await emit(
                        {
                            "type": "tool_call",
                            "id": ev["id"],
                            "name": ev["name"],
                            "kind": get_tool_kind(ev["name"]),  # 'tool' | 'skill'
                            "label": label,
                            "preview": preview,
                            "command": command,  # 模型原始输入（脱敏）
                            "status": "running",
                        }
                    )
                elif etype == "usage":
                    # 累积真实 prompt_tokens（用于下一轮压缩阈值判断）
                    pt = ev.get("prompt_tokens", 0)
                    if pt:
                        _real_prompt_tokens = pt
                    await emit({"type": "usage", "prompt_tokens": pt,
                                "completion_tokens": ev.get("completion_tokens", 0)})
                elif etype == "retry":
                    # ★ 连接重试：清空上一轮（可能已部分输出）的累积，前端据此重置消息气泡
                    assistant_text = ""
                    assistant_reasoning = ""
                    tool_calls = []
                    await emit(ev)
                elif etype == "error":
                    self._consecutive_failures += 1
                    # 主 provider 报错且配置了兜底 → 切到稳定模型后重试本轮
                    if self._fallback_client is not None and not self._fallback_used:
                        self._fallback_used = True
                        self.client = self._fallback_client
                        logger.warning("主模型报错（%s），切换到兜底模型重试", ev.get("message", ""))
                        await emit({
                            "type": "info",
                            "message": "主模型请求失败，已自动切换到稳定模型继续。",
                        })
                        # 重置本轮累积并跳出流循环，进入外层 while 用兜底客户端重新生成
                        # ★ 必须 break 而非 continue：continue 只跳到下一个流事件（内层 for），
                        # 兜底模型永远不会被真正调用 → 表现为空回复收尾。
                        assistant_text = ""
                        assistant_reasoning = ""
                        tool_calls = []
                        _switched_to_fallback = True
                        break
                    await emit({"type": "error", "message": ev.get("message", "未知错误")})
                    await emit({"type": "message_end"})
                    return
                elif etype == "done":
                    # 捕获模型停止原因，正常收尾时透传给前端（截断/过滤可见）
                    last_finish_reason = ev.get("finish_reason")
                    continue
                # 取消检查：流式输出过程中用户点了停止，立即中断本轮生成
                if cancel_requested(sid):
                    await emit({"type": "cancelled"})
                    await emit({"type": "message_end"})
                    return

            # 兜底切换：本轮被主模型 error 中断且无有效输出 → 跳过本轮收尾，直接重开一轮（用兜底 client）
            if _switched_to_fallback:
                continue

            # 组装 assistant 消息（保留 tool_calls 以便多轮）
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": assistant_text}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                # ★ 主模型「发神经」防护：无工具调用时检测纯文本噪声 / 裸 JSON 误发
                noise, reason = _is_noise_output(assistant_text)
                if noise:
                    self._consecutive_failures += 1
                    logger.warning("检测到主模型噪声输出（%s），丢弃本轮并重试", reason)
                    # 丢弃本轮 assistant 消息，注入修正提示让模型重答
                    messages.pop()
                    messages.append({
                        "role": "system",
                        "content": (
                            "（系统提示：你上一轮输出被判定为无效/乱码内容，"
                            "请忽略并重用正常中文自然语言回答，或直接调用合适的工具。"
                            "不要输出无意义字符或裸 JSON/代码块。）"
                        ),
                    })
                    # 连续 2 次噪声 → 切到稳定兜底模型（若配置），并提示前端
                    if self._fallback_client is not None and not self._fallback_used:
                        self._fallback_used = True
                        self.client = self._fallback_client
                        await emit({
                            "type": "info",
                            "message": "主模型输出异常，已自动切换到稳定模型继续。",
                        })
                        logger.info("主模型连续异常，已切换到兜底模型")
                    # 防止无限重试：最多重答 3 次
                    if self._consecutive_failures >= 3:
                        await emit({
                            "type": "error",
                            "message": f"主模型连续输出异常（{reason}），已停止以避免刷屏。",
                        })
                        await emit({"type": "message_end"})
                        return
                    continue

                if _looks_like_bare_json_or_code(assistant_text):
                    # 裸 JSON/代码块误发：截断本轮，提示用自然语言或走工具
                    logger.warning("检测到主模型裸 JSON/代码块误发，提示重答")
                    messages.pop()
                    messages.append({
                        "role": "system",
                        "content": (
                            "（系统提示：检测到你直接输出了 JSON 或代码块而非调用工具。"
                            "请改用自然语言回答，或在需要时通过 tool_calls 调用对应工具，"
                            "不要以裸文本形式返回 JSON/代码。）"
                        ),
                    })
                    if self._consecutive_failures < 3:
                        self._consecutive_failures += 1
                        continue
                    # 已达上限：把裸内容当作正文正常返回（避免彻底卡死）
                    await emit({"type": "message_end", "finish_reason": last_finish_reason})
                    return

                # ★ 自动记忆抽取：fire-and-forget，绝不阻塞 / 失败主回答
                if self._settings.get("agent", {}).get("memory_autosave", True):
                    asyncio.create_task(
                        _schedule_auto_memory(
                            self.client, user_input, assistant_text, self._settings
                        )
                    )

                # 正常收尾：携带 finish_reason，使「截断/被过滤」可见
                await emit({"type": "message_end", "finish_reason": last_finish_reason})
                return

            # 执行工具，回灌结果
            for tc in tool_calls:
                # ★ 白名单双拦截：极简模式等受限场景下，不在白名单的工具直接拒绝，
                # 回灌模型让其停止尝试（schema 已过滤使其「看不见」，此为正交兜底）。
                if self._allowed_tools is not None and tc["name"] not in self._allowed_tools:
                    deny = (
                        f"工具 {tc['name']} 不在当前模式允许的白名单中，已被禁用。"
                        "请只使用允许的工具，不要调用它。"
                    )
                    await emit({
                        "type": "tool_result",
                        "id": tc["id"],
                        "status": "error",
                        "preview": _tool_result_preview(deny),
                    })
                    messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": deny}
                    )
                    continue

                if cancel_requested(sid):
                    await emit({"type": "cancelled"})
                    await emit({"type": "message_end"})
                    return

                # ★ 权限申请工具：模型申请提权 → 发审批事件等待前端确认
                if tc["name"] == "request_permission":
                    target = str((tc["arguments"] or {}).get("target_level") or "").lower()
                    reason = str((tc["arguments"] or {}).get("reason") or "")
                    level_order = {"low": 0, "medium": 1, "high": 2}
                    cur = level_order.get(self._permission_level, 1)
                    tgt = level_order.get(target, 1)
                    if target not in ("medium", "high") or tgt <= cur:
                        deny = "权限申请无效：target_level 必须是当前档位之上的档位（medium/high）。"
                        await emit({"type": "tool_result", "id": tc["id"], "status": "error",
                                    "preview": _tool_result_preview(deny)})
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": deny})
                        continue
                    aid = uuid.uuid4().hex
                    await emit({
                        "type": "approval_required",
                        "approval_id": aid,
                        "tool": "permission_escalate",
                        "target": target,
                        "description": f"模型申请将权限从「{self._permission_level}」提升到「{target}」"
                                       + (f"：{reason}" if reason else ""),
                    })
                    if self._auto_approve:
                        decision = "approve"
                    else:
                        decision = await wait_for_approval(aid)
                    if decision == "approve":
                        self._escalated_level = target
                        ok = f"✅ 用户已批准临时提权到「{target}」。本轮内可执行对应档位的操作。"
                        await emit({"type": "approval_auto", "approval_id": aid,
                                    "tool": "permission_escalate", "target": target})
                    else:
                        ok = f"❌ 用户拒绝了提权申请（保持「{self._permission_level}」档）。请改用当前档位允许的方案。"
                    await emit({"type": "tool_result", "id": tc["id"], "status": "ok",
                                "preview": _tool_result_preview(ok)})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": ok})
                    continue

                # ★ 权限档位拦截：工具所需档位 > 当前档位（含临时提权）→ 拒绝并提示申请提权
                from agents.tools import permission_level_of_tool
                need = permission_level_of_tool(tc["name"])
                level_order = {"low": 0, "medium": 1, "high": 2}
                cur_level = self._escalated_level or self._permission_level
                if level_order.get(need, 1) > level_order.get(cur_level, 1):
                    deny = (
                        f"工具 {tc['name']} 需要「{need}」权限档位，当前为「{cur_level}」。"
                        "请先调用 request_permission 申请提权（target_level 填目标档位），"
                        "用户确认后再执行；或改用当前档位允许的替代方案。"
                    )
                    await emit({
                        "type": "tool_result",
                        "id": tc["id"],
                        "status": "error",
                        "preview": _tool_result_preview(deny),
                    })
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": deny})
                    continue

                # ★ 安全策略：受保护目录 / shell 危险命令在触发审批前直接拦截
                block = protected_block_reason(tc["name"], tc["arguments"])
                if block:
                    await emit({
                        "type": "tool_result",
                        "id": tc["id"],
                        "status": "error",
                        "preview": _tool_result_preview(block),
                    })
                    messages.append(
                        {"role": "tool", "tool_call_id": tc["id"],
                         "content": "操作被安全策略阻止：" + block + " 请勿再尝试。"}
                    )
                    continue

                # ★ 审批闸门：需确认的工具先发 approval_required 事件并阻塞等待前端决策
                meta = TOOLS.get(tc["name"], {})
                need_approval = bool(meta.get("require_approval"))
                # ★ 删除确认目录（2026-08-08）：high 权限下，delete_file 命中用户配置的
                # 「删除需确认目录」时仍强制确认；未命中则 high 直接放行（不再弹窗）。
                # 系统保护目录永远硬拦截（protected_block_reason 已在上方处理），不在此列。
                if tc["name"] == "delete_file":
                    del_path = str((tc.get("arguments") or {}).get("path", ""))
                    if self._permission_level == "high" and not needs_delete_confirm(del_path):
                        need_approval = False
                if need_approval:
                    aid = uuid.uuid4().hex
                    info = build_approval_info(tc["name"], tc["arguments"]) or {}
                    if self._auto_approve:
                        # 无人值守（邮件网关等）：无前端可弹窗，等待会永久挂起。
                        # 直接放行并发一条可观测事件；受保护目录已在上方 protected_block_reason 拦截。
                        await emit({
                            "type": "approval_auto",
                            "approval_id": aid,
                            "tool": tc["name"],
                            **info,
                        })
                        decision = "approve"
                    else:
                        await emit({
                            "type": "approval_required",
                            "approval_id": aid,
                            "tool": tc["name"],
                            **info,
                        })
                        decision = await wait_for_approval(aid)
                    if decision != "approve":
                        # 用户拒绝：把拒绝结果回灌模型，让它调整行为，并跳过执行
                        target_desc = info.get("path") or info.get("target") or "需确认的操作"
                        denial = (
                            f"操作被用户拒绝：{tc['name']}"
                            f"（{target_desc}）。"
                            "请勿重复该请求。"
                        )
                        await emit({
                            "type": "tool_result",
                            "id": tc["id"],
                            "status": "error",
                            "preview": _tool_result_preview(denial),
                        })
                        messages.append(
                            {"role": "tool", "tool_call_id": tc["id"], "content": denial}
                        )
                        continue

                # ★ 执行前校验：畸形/不完整参数不真正执行，避免 __raw__ 漏进执行
                # 或 shell 缺 command 崩出裸错；改为清晰报错并回灌模型。
                val_err = _validate_tool_args(tc["name"], tc["arguments"])
                if val_err:
                    await emit({
                        "type": "tool_result",
                        "id": tc["id"],
                        "status": "error",
                        "preview": _tool_result_preview(val_err),
                    })
                    messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": val_err}
                    )
                    continue
                result = await execute_tool(tc["name"], tc["arguments"], ctx=ctx)
                preview = _tool_result_preview(result)
                await emit(
                    {
                        "type": "tool_result",
                        "id": tc["id"],
                        "status": "success",
                        "preview": preview,
                    }
                )
                # ★ 回灌模型前截断：大结果留头留尾，省 token 且不丢关键信息
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": _truncate_for_model(result)}
                )
                # ★ 会话检查点：每完成一步工具调用就记录进度/路径/结果
                # （供「继续」时恢复现场，避免重新探索环境）
                try:
                    _checkpoint.save_checkpoint(
                        sid,
                        step=_checkpoint_step_desc(tc, result),
                        raw_tool_result=result,
                        next_step=None,
                    )
                except Exception:  # noqa: BLE001
                    pass
