"""视觉理解后端 — 把截图交给多模态模型转成文字描述，供主模型（纯文本）阅读。

设计：纯文本模型当「大脑」，单独开一条「视觉通路」。
由 server/api.py 启动时调用 configure_vision(backends) 注入**有序多模态后端列表**：
  - 本程序不内置服务商：后端取自使用者配置中 supports_vision: true 的接口，
  - 按配置声明顺序优先使用，任一可用即可。
desktop_control 截图后把图片 base64 编码塞进 image_url 内容块，流式拿回文字描述。
视觉模型返回的文字作为工具结果回灌主对话，主模型据此「看」屏幕。

★ 多后端 fallback：首选后端报「配额/订阅/额度耗尽」这类持久错误时，自动切到下一个
后端，对用户透明（不抛异常、不刷屏），任一成功即返回描述。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from typing import Any

logger = logging.getLogger(__name__)

# 触发「切备用后端」的持久错误标记（与 providers/openai_compat.py 的 _PERMANENT_MARKERS 对齐）
_QUOTA_MARKERS = (
    "requires a subscription", "subscription", "403", "401", "402",
    "forbidden", "unauthorized", "invalid api key", "incorrect api key",
    "payment required", "insufficient", "credit", "balance", "quota",
)

_VISION_CLIENTS: list[dict[str, Any]] = []  # [{"client":..., "model":..., "name":...}, ...]


def configure_vision(backends: list[dict[str, Any]]) -> None:
    """注入有序多模态后端列表：[{client, model, name}, ...]。

    按配置声明顺序排序多模态后端。
    """
    global _VISION_CLIENTS
    _VISION_CLIENTS = [b for b in backends if b.get("client")]


def is_vision_ready() -> bool:
    return len(_VISION_CLIENTS) > 0


async def describe_image(image_path: str, prompt: str | None = None) -> str:
    """把一张图片交给多模态模型，返回其文字描述。失败返回说明文本（不抛异常）。

    多后端 fallback：首个后端成功即用；若首个报「配额/订阅/额度耗尽」类持久错误，
    自动切下一个后端重试；全部失败或不可用则返回说明文本（含已尝试的后端名）。
    """
    if not _VISION_CLIENTS:
        return (
            "（视觉后端未启用：当前没有可用的多模态 provider，"
            "无法看图；可在一个 provider 上设置 supports_vision: true，或改用 desktop_control 的 ocr 模式）"
        )

    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    try:
        with open(image_path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        return f"（读取截图失败：{exc}）"
    b64 = base64.b64encode(raw).decode("ascii")

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": prompt
            or (
                "请详细描述这张电脑截图，帮助一个无法直接看图的助手理解屏幕内容：\n"
                "1) 当前最前面的窗口/应用叫什么；\n"
                "2) 屏幕上可见的主要文字（保留原文，含按钮、菜单、标签、输入框提示）；\n"
                "3) 关键可点击元素的位置与外观（按钮、图标、链接、输入框）；\n"
                "4) 任何弹窗、报错或异常状态；\n"
                "5) 若有代码/终端，简述其内容。\n"
                "用中文、条理清晰、抓住重点。"
            ),
        },
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    messages = [{"role": "user", "content": content}]

    last_err = ""
    tried: list[str] = []
    for backend in _VISION_CLIENTS:
        client = backend["client"]
        model = backend.get("model")
        name = backend.get("name", model or "unknown")
        tried.append(name)
        parts: list[str] = []
        try:
            async for ev in client.stream_chat(messages, tools=None):
                t = ev.get("type")
                if t == "content_delta":
                    parts.append(ev.get("text", ""))
                elif t == "error":
                    msg = ev.get("message", "")
                    last_err = f"{name}: {msg}"
                    # 持久错误（配额/订阅/额度耗尽）→ 切下一个后端
                    if any(k in msg.lower() for k in _QUOTA_MARKERS):
                        logger.warning("视觉后端 %s 配额/订阅类错误，切备用：%s", name, msg)
                        break
                    # 其他错误也切下一个后端（避免同一坏后端反复重试）
                    break
            else:
                # 流式正常结束（没 break）→ 收集成功
                pass
            text = "".join(parts).strip()
            if text:
                return text
            # 没有内容但也没显式错误：视为该后端不可用，继续下一个
            last_err = last_err or f"{name}: 未返回任何内容"
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = f"{name}: {exc}"
            logger.warning("视觉后端 %s 异常，切备用：%s", name, exc)
            continue

    return (
        f"（视觉理解失败：已尝试 { ' → '.join(tried) }，均不可用。"
        f"{('最后错误：' + last_err) if last_err else ''}）"
    )
