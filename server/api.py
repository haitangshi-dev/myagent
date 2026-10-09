"""FastAPI 服务 — SSE 流式对话 + 静态托管 Web UI。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.agent_loop import Agent, resolve_approval, request_cancel, clear_cancel
from agents import workspace as workspace_mod
from agents import events as agent_events
from agents import tools as tools_mod
from agents import global_memory as _gmem
from agents.skills_io import (
    skill_install_root,
    read_installed,
    write_installed,
)
from config import load_settings
from providers.manager import ProviderManager

logger = logging.getLogger(__name__)

settings = load_settings()
pm = ProviderManager(settings)
SYSTEM_PROMPT = settings.get("agent", {}).get("system_prompt", "你是一个 AI Agent。")

# 极简模式（minimal）专用精简 prompt + 工具白名单。
# 极简模式 = 短提示词 + 受限工具集，省 token、降低小模型乱调工具的概率。
# ★ 注意：与「本地 / 云端」无关，本项目不区分推理来源。
MINIMAL_SYSTEM_PROMPT = settings.get("agent", {}).get("minimal_system_prompt") or SYSTEM_PROMPT
MINIMAL_TOOLS = settings.get("agent", {}).get("minimal_tools", None)  # list[str] | None

# 极简模式默认精简 prompt（短、直接、不啰嗦）
_DEFAULT_MINIMAL_PROMPT = """你是一个电脑智能助手。你必须始终用中文回复，全程不得夹杂英文句子（专业术语可保留英文单词，如 JSON、API、GPU）。

可用工具：
- read_file / write_file / edit_file / list_files：读写文件
- shell：执行命令
- memory_remember / memory_recall：记住和回忆事实

原则：
1. 需要读文件/跑命令就直接调工具，不要描述不动作。
2. 不知道就说不猜，不编造工具调用。
3. 回答简短，一句能说清就不写两句。
4. 代码注释、命令说明、变量命名解释，一切文字都用中文。
5. 关键：你内部推理（thinking/reasoning）允许用英文思考，但**最终输出给用户的「总结性回复 / 任务收尾结论」必须始终用中文书写，不得出现整段英文**。内部怎么想都行，给用户看的答案、总结、结论，全用中文。"""

# 运行时模式：full（完整）或 minimal（极简）
# 前端通过 /api/mode 读写，chat 端点据此选择提示词与工具集。
_runtime_mode: str = settings.get("runtime_mode", "full")  # "full" | "minimal"

# --------------------------------------------------------------------------
# 记忆模块 LLM：自动抽取 / 压缩时取默认 provider 客户端（捕获异常返回空串）
# --------------------------------------------------------------------------
_DEFAULT_PROVIDER = settings.get("default_provider", "custom")
_DEFAULT_MODEL = settings.get("default_model", "")


def _thinking_flag(provider: str) -> bool | None:
    """该 provider 是否应关闭思考链：settings 里 reasoning:"off" → False，其余 → None（不注入）。

    用于思考型模型：不关思考链时 token 全耗在 reasoning_content 上，
    content 为空 → 前端报 "empty stream with no finish_reason"。
    provider 配了 reasoning:"off" 时返回 False，让 OpenAICompatClient 注入
    thinking={"type":"disabled"}；其它支持思考的接口保持 None，按 provider 默认行为。
    """
    cfg = (settings.get("providers", {}) or {}).get(provider) or {}
    if cfg.get("reasoning") == "off":
        return False
    return None


async def _memory_llm(system: str, user: str) -> str:
    """记忆抽取 / 压缩用的 LLM 文本收集（统一经默认 provider 客户端）。"""
    try:
        client = pm.get_client(_DEFAULT_PROVIDER, _DEFAULT_MODEL)
        collected: list[str] = []
        async for ev in client.stream_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.15,
            max_tokens=1024,
            repetition_penalty=1.1,
            thinking=_thinking_flag(_DEFAULT_PROVIDER),
        ):
            if ev.get("type") == "content_delta":
                collected.append(ev["text"])
            elif ev.get("type") == "error":
                return ""
        return "".join(collected).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆 LLM 调用失败: %s", exc)
        return ""


_gmem.set_llm(_memory_llm)


def _resolve_context_limit(pmanager: ProviderManager, provider: str, model: str | None) -> int | None:
    """计算某 provider/model 的上下文压缩上限（token）。

    优先级：auto_compress.max_context_tokens(>0) > provider.context_window > 客户端默认。
    返回 None 表示「不限制」（如 V4-Flash 1M 且 auto_compress=0 且无 context_window）。
    """
    cfg = (settings.get("providers", {}) or {}).get(provider)
    # 1) auto_compress 强制上限优先
    ac_val = int((settings.get("auto_compress", {}) or {}).get("max_context_tokens", 0) or 0)
    if ac_val > 0:
        return ac_val
    # 2) provider 配置的 context_window
    if cfg:
        cw = cfg.get("context_window")
        if cw:
            return int(cw)
    # 3) 客户端默认值
    try:
        client = pmanager.get_client(provider, model)
        cl = getattr(client, "context_limit", None)
        if cl:
            return int(cl)
    except Exception:  # noqa: BLE001
        pass
    return None


def _build_fallback_client(pmanager: ProviderManager, primary_provider: str, primary_model: str | None):
    """构建兜底客户端：主接口连续异常/噪声时自动切换。

    本项目不内置任何服务商，因此兜底策略是「从用户配置的其它接口里挑一个可用的」：
    遍历 providers，跳过主接口与未配置地址的接口，取第一个有可用模型的接口。
    若用户只配了一个接口（最常见），则返回 None——仍保留单接口自身的重试。
    """
    try:
        for name, cfg in (settings.get("providers", {}) or {}).items():
            if name == primary_provider:
                continue
            if not (cfg.get("base_url") or "").strip():
                continue
            model = cfg.get("default_model") or (cfg.get("models") or [None])[0]
            if not model:
                continue
            return pmanager.get_client(name, model)
    except Exception as exc:  # noqa: BLE001
        logger.warning("构建兜底客户端失败：%s", exc)
    return None
# 本程序不内置服务商：后端 = 使用者配置中 supports_vision 为 true 的接口，按声明顺序。
# 任一 provider 配 supports_vision: true 即进入候选池，按 provider 在 settings 中的声明顺序排序。
from agents import vision as _vision

try:
    _vision_backends: list[dict[str, Any]] = []
    for _pn, _pc in settings.get("providers", {}).items():
        if _pc.get("supports_vision"):
            try:
                _vcl = pm.get_client(_pn)
                _vision_backends.append({
                    "client": _vcl,
                    "model": _pc.get("default_model"),
                    "name": _pc.get("display_name", _pn),
                })
            except Exception as _e:  # noqa: BLE001
                logger.warning("视觉后端 %s 客户端构建失败，跳过：%s", _pn, _e)
    if _vision_backends:
        _vision.configure_vision(_vision_backends)
        logger.info(
            "视觉后端已启用（按优先级）：%s",
            " → ".join(b["name"] for b in _vision_backends),
        )
    else:
        logger.info("未找到 supports_vision 的 provider，desktop_control 视觉模式将不可用")
except Exception as exc:  # noqa: BLE001
    logger.warning("视觉后端初始化失败（desktop_control 视觉模式不可用）：%s", exc)

app = FastAPI(title="Hermes 高仿版")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------
# 访问令牌鉴权（生态互联：手机/远程客户端经公网访问时必备）
# token 取自 settings.yaml 的 server.auth_token（或环境变量 MYAGENT_AUTH_TOKEN）。
# 支持 Authorization: Bearer <token> 或 ?token=<token>。
# 仅对 /api/* 生效，/api/health 豁免（仅供存活探测，无敏感信息）。
# 前端由 lib/auth.ts 的 fetch 拦截器自动带头；远程端首次打开会弹「输入访问令牌」。
# --------------------------------------------------------------------------
import hmac as _hmac

_AUTH_TOKEN = (settings.get("server", {}) or {}).get("auth_token") or os.environ.get(
    "MYAGENT_AUTH_TOKEN", ""
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _AUTH_TOKEN:
        path = request.url.path
        # 仅对公网 /api/* 鉴权：本机回环（桌面端/Electron）信任，免令牌
        if path.startswith("/api/") and path != "/api/health":
            client = (request.client.host if request.client else "") or ""
            is_local = client in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")
            if not is_local:
                auth = request.headers.get("Authorization", "")
                token = auth[7:].strip() if auth.startswith("Bearer ") else request.query_params.get("token", "")
                if not token or not _hmac.compare_digest(token, _AUTH_TOKEN):
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
async def _startup():
    """启动后台定时任务调度器（独立于任何聊天会话，按 when 自动触发 Agent）。"""
    try:
        from agents import scheduler as _sched

        _sched.get_scheduler().init()
        await _sched.get_scheduler().start()
        logger.info("定时任务调度器已启动")
    except Exception as exc:  # noqa: BLE001
        logger.warning("定时任务调度器启动失败：%s", exc)

    # 邮件网关：邮箱来信 → 唤醒助手 → 自动回信（仅在 settings.email.enabled 时拉起）
    try:
        gw = _get_email_gateway()
        if gw.enabled:
            gw.start()
            logger.info("邮件网关已启动")
        else:
            logger.info("邮件网关未启用（config/settings.yaml → email.enabled）")
    except Exception as exc:  # noqa: BLE001
        logger.warning("邮件网关启动失败：%s", exc)


@app.on_event("shutdown")
async def _shutdown_email():
    try:
        if _email_gw is not None:
            await _email_gw.stop()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 邮件网关（QQ 邮箱 MCP）
# --------------------------------------------------------------------------
_email_gw = None


def _get_email_gateway():
    """惰性构造邮件网关单例（复用全局 pm / SYSTEM_PROMPT / 事件广播总线）。"""
    global _email_gw
    if _email_gw is None:
        from agents.email_gateway import EmailGateway

        _email_gw = EmailGateway(
            settings,
            pm,
            SYSTEM_PROMPT,
            broadcaster=agent_events.broadcast,
        )
    return _email_gw


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    provider: str = "custom"
    model: str | None = None
    history: list[dict[str, Any]] = []
    # 多模态：图片以 data URL（data:image/...;base64,...）内联；允许纯图片无文字
    images: list[str] = []
    # 会话/工作区扩展
    session_id: str | None = None
    session_name: str | None = None
    kind: str = "chat"            # "chat" | "workspace"
    workspace_dir: str | None = None
    sandbox: dict[str, bool] | None = None  # {allow_file_write, allow_file_delete, allow_shell}
    # 运行时模式：full（完整）/ minimal（极简）。仅影响提示词与工具集，与推理来源无关。
    mode: str | None = None
    # ★ 权限档位（2026-08-07）：low/medium/high，前端滑动条设置，决定工具集
    permission_level: str | None = None


class ApproveRequest(BaseModel):
    approval_id: str
    decision: str  # "approve" | "deny"


class WorkspaceSandboxRequest(BaseModel):
    dir: str
    allow_file_write: bool = True
    allow_file_delete: bool = False
    allow_shell: bool = True


class WorkspaceMemoryRequest(BaseModel):
    dir: str
    content: str
    tags: list[str] | None = None


# 定时任务创建 / 更新
class ScheduleCreateRequest(BaseModel):
    name: str = "定时任务"
    prompt: str
    when: str
    provider: str | None = None
    model: str | None = None


class ScheduleUpdateRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    when: str | None = None
    provider: str | None = None
    model: str | None = None
    enabled: bool | None = None


class SettingsOverrideRequest(BaseModel):
    # 任意深度覆盖片段，典型形如 {"providers": {"nim": {"api_key": "..."}}}
    override: dict[str, Any]


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"status": "ok", "providers": [p["id"] for p in pm.list_providers()]}


# 运行时模式：full（完整模式）/ minimal（极简模式）
# ★ 与「本地 / 云端」无关：本项目不提供推理后端，只区分助手的行为复杂度。
@app.get("/api/mode")
async def get_mode():
    return {"mode": _runtime_mode}


class ModeRequest(BaseModel):
    mode: str  # "full" | "minimal"


@app.post("/api/mode")
async def set_mode(req: ModeRequest):
    global _runtime_mode
    if req.mode not in ("full", "minimal"):
        raise HTTPException(400, "mode 必须是 full 或 minimal")
    _runtime_mode = req.mode
    logger.info("运行时模式切换为 %s", req.mode)
    return {"mode": _runtime_mode}


@app.get("/api/ambient/state")
async def ambient_state():
    """环境感知状态：当前激活窗口标题（主动感知层 / Phase 4 基础）。

    后端通过 pygetwindow 读取前台窗口标题；依赖缺失则返回 null。
    前端据此判断「用户正在使用什么」，为后续主动插话 / 上下文感知提供信号。
    """
    state: dict[str, Any] = {
        "active_window": None,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        import pygetwindow as gw

        win = gw.getActiveWindow()
        if win and getattr(win, "title", ""):
            state["active_window"] = {"title": win.title, "app": _guess_app(win.title)}
    except Exception as exc:  # noqa: BLE001
        logger.debug("ambient 激活窗口读取失败（依赖缺失？）：%s", exc)
    return state


def _guess_app(title: str) -> str:
    """从窗口标题粗判应用名（best-effort，不依赖进程枚举）。"""
    mapping = {
        "Visual Studio Code": "VSCode",
        "Microsoft Edge": "Edge",
        "Google Chrome": "Chrome",
        "微信": "WeChat",
        "钉钉": "DingTalk",
        "记事本": "Notepad",
        "Photoshop": "Photoshop",
        "Excel": "Excel",
        "Word": "Word",
        "PowerPoint": "PowerPoint",
        "Windows Terminal": "Terminal",
        "Windows 资源管理器": "Explorer",
        "腾讯会议": "VooV",
    }
    for key, name in mapping.items():
        if key in title:
            return name
    return ""


class ProactiveRequest(BaseModel):
    context: dict[str, Any] | None = None  # 可选：前端传当前 ambient state


@app.post("/api/proactive/suggest")
async def proactive_suggest(req: ProactiveRequest):
    """主动搭话（Phase 4 自治感知）：根据用户当前在用的程序，生成一句贴心中文搭话。

    走当前配置的默认接口，单次非流式补全，轻量、可控速率。
    任何异常都回退到内置兜底建议，保证前端永远有内容、不报错。
    """
    aw = (req.context or {}).get("active_window") if req.context else None
    title = (aw or {}).get("title") or "未知"
    app_name = (aw or {}).get("app") or ""

    system = (
        "你是一位贴心、能干的电脑智能助手。"
        "根据用户当前正在使用的程序，给一句自然、贴心、不超过 40 字的中文搭话"
        "（可以是轻提醒、小技巧、或轻松互动）。只输出这句话本身，不要解释、"
        "不要引号、不要问号堆叠。若用户看起来在忙，就给个轻松的；若空闲，"
        "可以主动提议帮点小忙。"
    )
    user = f"用户当前前台窗口：{app_name + ' — ' if app_name else ''}{title}"

    fallback = "需要我帮忙整理下桌面，或者查点什么吗？"
    try:
        client = pm.get_client(_DEFAULT_PROVIDER, None)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        text = ""
        async for ev in client.stream_chat(
            messages, tools=None, temperature=0.15, max_tokens=120,
            thinking=_thinking_flag(_DEFAULT_PROVIDER),
        ):
            if ev.get("type") == "content_delta":
                text += ev.get("text", "")
            elif ev.get("type") == "error":
                logger.warning("proactive suggest 模型错误：%s", ev.get("message"))
                break
        text = (text or "").strip()
        # 清掉模型可能夹带的引号/前缀
        text = text.strip("\"'「」『』 \n")
        if not text:
            text = fallback
        return {"suggestion": text, "context": aw}
    except Exception as exc:  # noqa: BLE001
        logger.warning("proactive suggest 失败，回退：%s", exc)
        return {"suggestion": fallback, "context": aw}


class DailySummaryRequest(BaseModel):
    session_id: str | None = None  # 可选：拉取该会话的任务看板进度


@app.post("/api/daily/summary")
async def daily_summary(req: DailySummaryRequest):
    """每日摘要（Phase 4 自治）：汇总当日日期、当前在用的程序、任务看板进度，
    由默认大脑生成一段暖心、精炼（≤180 字）的中文今日摘要 + 2~3 条轻建议。
    任何异常都回退到内置兜底摘要，保证前端永远有内容、不报错。
    """
    from agents import board as _board

    now = datetime.now()
    _wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    date_cn = f"{now.year}年{now.month}月{now.day}日 {_wd}"

    # 环境感知（复用 ambient 端点逻辑）
    aw_title = aw_app = ""
    try:
        st = await ambient_state()
        aw = (st or {}).get("active_window")
        if aw:
            aw_title = aw.get("title") or ""
            aw_app = aw.get("app") or ""
    except Exception:  # noqa: BLE001
        pass

    # 任务看板进度
    tasks_brief = "暂无"
    try:
        sid = req.session_id or "default"
        tasks = _board.list_tasks(sid)
        if tasks:
            done = [t for t in tasks if (t.get("status") or "").lower() in ("done", "completed", "finished")]
            doing = [t for t in tasks if (t.get("status") or "").lower() == "doing"]
            pending = [t for t in tasks if (t.get("status") or "").lower() in ("pending", "todo", "open")]
            parts = []
            if pending:
                parts.append(f"待办 {len(pending)}")
            if doing:
                parts.append(f"进行中 {len(doing)}")
            if done:
                parts.append(f"已完成 {len(done)}")
            tasks_brief = "；".join(parts) or "暂无"
    except Exception:  # noqa: BLE001
        tasks_brief = "暂无"

    system = (
        "你是一位贴心、能干的电脑智能助手。"
        "根据以下今日上下文，为用户生成一段精炼的「今日摘要」："
        "1) 用一句暖心开场点出今天日期；2) 简述用户当前在做什么、以及任务看板进度；"
        "3) 给出 2~3 条具体、可执行的小建议（如整理、休息、继续某项任务）。"
        "整体不超过 180 字，中文、口语化、不要标题、不要编号前缀、不要引号。"
        "若信息不足，就给个轻松的鼓励。"
    )
    user = (
        f"今日：{date_cn}\n"
        f"用户当前窗口：{aw_app + ' — ' if aw_app else ''}{aw_title or '未知'}\n"
        f"任务看板：{tasks_brief}"
    )

    generated_at = now.isoformat(timespec="seconds")
    fallback = f"今天是{date_cn}，用户辛苦了。先深呼吸一下，把最要紧的一件事做了，其余慢慢来～"
    try:
        client = pm.get_client(_DEFAULT_PROVIDER, None)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        text = ""
        async for ev in client.stream_chat(
            messages, tools=None, temperature=0.15, max_tokens=220,
            thinking=_thinking_flag(_DEFAULT_PROVIDER),
        ):
            if ev.get("type") == "content_delta":
                text += ev.get("text", "")
            elif ev.get("type") == "error":
                logger.warning("daily summary 模型错误：%s", ev.get("message"))
                break
        text = (text or "").strip().strip("\"'「」『』 \n")
        if not text:
            text = fallback
        return {"summary": text, "date": date_cn, "generated_at": generated_at}
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily summary 失败，回退：%s", exc)
        return {"summary": fallback, "date": date_cn, "generated_at": generated_at}


@app.get("/api/providers")
async def providers():
    return {"providers": pm.list_providers()}


@app.get("/api/tools")
async def tools():
    from agents.tools import get_tool_schemas
    return {"tools": get_tool_schemas()}


class CancelRequest(BaseModel):
    session_id: str


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None  # 不填则用设置里的最佳云端女声（默认 zh-CN-XiaoxiaoNeural）
    rate: float | None = None  # 语速倍率（0.5~3.0，1.0 正常）
    pitch: float | None = None  # 音调倍率（0.5~2.0，1.0 正常；映射为 Azure 相对 Hz）


@app.post("/api/approve")
async def approve(req: ApproveRequest):
    """前端用户在弹窗里确认/拒绝一个需要审批的工具操作（如删除文件）。"""
    ok = resolve_approval(req.approval_id, req.decision)
    return {"ok": ok}


@app.post("/api/cancel")
async def cancel(req: CancelRequest):
    """前端点“停止”时调用：标记该会话的当前对话应立即中断。"""
    request_cancel(req.session_id)
    return {"ok": True}


@app.post("/api/tts")
async def tts(req: TTSRequest):
    """云端 TTS。

    默认主引擎 = **Edge TTS**（微软在线神经语音，免密钥、免费）→ 默认最佳中文女声
    zh-CN-XiaoxiaoNeural。若配置了 Azure 密钥（settings.yaml → azure_speech），则 Edge
    失败时回退 Azure。两者都不可用（无网络 / 未装 edge-tts）→ 502，前端回退本地
    SpeechSynthesis。绝不让密钥出现在前端。
    """
    voice = (req.voice or "zh-CN-XiaoxiaoNeural").strip()
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "缺少 text")

    # 语速 / 音调：前端 multiplier → SSML 相对值（Edge TTS 与 Azure 通用）
    rate = req.rate if req.rate is not None else 1.0
    rate = max(0.5, min(3.0, float(rate)))
    rate_ssml = f"{(rate - 1) * 100:+.0f}%"   # 1.05 → +5%，0.9 → -10%
    pitch = req.pitch if req.pitch is not None else 1.0
    pitch = max(0.5, min(2.0, float(pitch)))
    pitch_hz = max(-50.0, min(50.0, (pitch - 1.0) * 50.0))  # 1.0→0Hz，±50Hz
    pitch_ssml = f"{pitch_hz:+.0f}Hz"

    # ---- 主引擎：Edge TTS（免密钥）----
    try:
        audio = await _edge_tts_synth(text, voice, rate_ssml, pitch_ssml)
        if audio:
            return Response(
                content=audio,
                media_type="audio/mpeg",
                headers={"Content-Disposition": 'inline; filename="tts.mp3"'},
            )
    except Exception as e:
        logger.warning("Edge TTS 失败，尝试 Azure 回退：%s", e)

    # ---- 回退：Azure（仅当已配置密钥）----
    az = settings.get("azure_speech", {}) or {}
    key = az.get("key") or os.environ.get("AZURE_SPEECH_KEY")
    region = az.get("region") or os.environ.get("AZURE_SPEECH_REGION")
    if key and region:
        try:
            audio = await _azure_tts_synth(text, voice, rate_ssml, pitch_ssml, key, region)
            if audio:
                return Response(
                    content=audio,
                    media_type="audio/mpeg",
                    headers={"Content-Disposition": 'inline; filename="tts.mp3"'},
                )
        except Exception as e:
            logger.warning("Azure TTS 也失败：%s", e)

    raise HTTPException(502, "云端 TTS 不可用（检查网络 / 确认已 pip install edge-tts）")


async def _edge_tts_synth(text: str, voice: str, rate_ssml: str, pitch_ssml: str) -> bytes:
    """Edge TTS（微软在线神经语音，免密钥）。返回 mp3 字节。"""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate_ssml, pitch=pitch_ssml)
    data = b""
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            data += chunk["data"]
    if not data:
        raise RuntimeError("Edge TTS 返回空音频")
    return data


async def _azure_tts_synth(
    text: str, voice: str, rate_ssml: str, pitch_ssml: str, key: str, region: str
) -> bytes:
    """Azure 神经语音（需密钥）。SSML 兼容 Edge 的相对值。"""
    # SSML 转义，避免特殊字符破坏合成
    safe = (
        text.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;")
    )
    # Edge 的 rate_ssml 形如 "+5%"，转成 Azure 的倍率数值
    rate_num = 1.0
    try:
        if rate_ssml.endswith("%"):
            rate_num = 1.0 + float(rate_ssml[:-1]) / 100.0
    except ValueError:
        rate_num = 1.0
    ssml = (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        "xml:lang='zh-CN'>"
        f"<voice name='{voice}'>"
        f"<prosody rate='{rate_num:.2f}' pitch='{pitch_ssml}'>{safe}</prosody>"
        "</voice></speak>"
    )
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-16khz-32kbitrate-mono-mp3",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.post(url, content=ssml, headers=headers)
        resp.raise_for_status()
        return resp.content


# --------------------------------------------------------------------------
# 记忆快捷指令（模式无关，不调模型，直接落库/查询）
# 让本地小模型也能「注入/查询/删除记忆」而无需理解复杂工具调用。
# 支持前缀：记：/记:/记住：/记住:/ /记 /remember  → 写入
#           回忆：/回忆:/回想：/回想:/ /回忆 /recall → 查询
#           忘：/忘:/忘记：/忘记:/ /忘 /forget /删除记忆： → 删除
# --------------------------------------------------------------------------
_MEMORY_CMD_PREFIXES = [
    (("记：", "记:", "记住：", "记住:", "/记", "/remember"), "remember"),
    (("回忆：", "回忆:", "回想：", "回想:", "/回忆", "/recall"), "recall"),
    (("忘：", "忘:", "忘记：", "忘记:", "/忘", "/forget", "删除记忆：", "删除记忆:"), "forget"),
]


def parse_memory_command(message: str):
    """解析记忆快捷指令，返回 (kind, content) 或 None。"""
    m = (message or "").strip()
    if not m:
        return None
    low = m.lower()
    for prefixes, kind in _MEMORY_CMD_PREFIXES:
        for p in prefixes:
            if low.startswith(p.lower()):
                content = m[len(p):].strip().lstrip("：: ").strip()
                if content:
                    return kind, content
    return None


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


async def _handle_memory_command(kind: str, content: str, req: ChatRequest, request: Request):
    """把「记/回忆/忘」快捷指令直接变成 SSE 流返回，完全不经过模型。"""
    message_id = uuid.uuid4().hex

    async def gen():
        yield f"data: {json.dumps({'type': 'message_start', 'message_id': message_id}, ensure_ascii=False)}\n\n"
        try:
            if kind == "remember":
                _gmem.add(content, tags=None, source="manual")
                text = f"✅ 已记住：{content}"
            elif kind == "recall":
                block = _gmem.recall_text(content, limit=8)
                text = ("🧠 相关记忆：\n" + block) if block else "（没有找到相关记忆）"
            else:  # forget
                hits = _gmem.search(content, limit=50)
                n = 0
                for h in hits:
                    try:
                        if _gmem.delete(h.get("id")):
                            n += 1
                    except Exception:  # noqa: BLE001
                        pass
                text = f"🗑️ 已删除 {n} 条相关记忆。" if n else "（没有找到需要删除的记忆）"
        except Exception as exc:  # noqa: BLE001
            text = f"记忆操作失败：{exc}"
            logger.exception("memory command failed")
        yield f"data: {json.dumps({'type': 'content', 'text': text}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'message_end', 'finish_reason': 'stop'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    if not req.message.strip() and not req.images:
        raise HTTPException(400, "message 不能为空")

    # 记忆快捷指令（记：/回忆：/忘：等）：模式无关，直接落库/查询，不经过模型。
    # 对本地小模型尤其重要——它看不懂复杂工具调用，但这类指令能直接生效。
    mem_cmd = parse_memory_command(req.message)
    if mem_cmd:
        return await _handle_memory_command(mem_cmd[0], mem_cmd[1], req, request)

    # 模式：full（完整）/ minimal（极简）—— 只影响提示词与工具集，与推理来源无关。
    effective_mode = req.mode or _runtime_mode
    if effective_mode not in ("full", "minimal"):
        effective_mode = "full"
    effective_provider = req.provider
    effective_model = req.model
    # 接口兜底：若前端传来的 provider 未配置（或地址为空），自动选第一个「配了地址」的接口。
    _providers = settings.get("providers", {}) or {}
    _cfg = _providers.get(effective_provider) or {}
    if not (_cfg.get("base_url") or "").strip():
        fallback = next(
            (pid for pid, pc in _providers.items() if (pc.get("base_url") or "").strip()),
            None,
        )
        if fallback:
            if effective_provider and effective_provider != fallback:
                logger.info("接口 '%s' 未配置地址，改用 '%s'", effective_provider, fallback)
            effective_provider = fallback
        else:
            raise HTTPException(
                400,
                "尚未配置任何可用接口。请在「设置 → 接口」填入 OpenAI 兼容的地址 / 密钥 / 模型名。",
            )

    try:
        client = pm.get_client(effective_provider, effective_model)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))

    # 组装请求上下文：始终带 session_id（让 task_board 等技能能绑定会话），
    # 工作区会话额外带目录与沙盒约束。
    # ★ 权限档位：只降不升（会话级状态约束，防止请求体直接提权）
    ctx: dict | None = {
        "kind": req.kind or "chat",
        "session_id": req.session_id,
        "session_name": req.session_name,
        "permission_level": _resolve_permission_level(req.session_id, req.permission_level),
    }
    # ★ 极简模式（minimal）：精简工具集 + 短提示词（省 token，适合小模型 / 轻量接口）
    # 权限 3 档保留：low=只读 / medium=常规 / high=完全控制
    allowed_tools = None  # None = 全部工具（完整模式默认）
    if effective_mode == "minimal":
        # ⚠️ settings.yaml 的 minimal_system_prompt: null 表示「用内置默认」，
        #   但 dict.get('key', default) 在键存在且值为 None 时返回 None（不是 default）！
        #   必须用 `or` 兜底，不能用 get 的第二个参数。
        _raw_prompt = settings.get("agent", {}).get("minimal_system_prompt")
        system_prompt = _raw_prompt or _DEFAULT_MINIMAL_PROMPT
        # 极简模式白名单工具（可由用户在 settings.yaml 的 agent.minimal_tools 配置扩展）
        tool_whitelist = settings.get("agent", {}).get("minimal_tools", None)
        if tool_whitelist is not None:
            # 用户自定义了工具白名单 → 只挂载这些
            allowed_tools = tool_whitelist
        else:
            # 默认精简工具集：文件管理 + 记忆 + Shell + 子智能体委派
            allowed_tools = [
                "read_file", "write_file", "edit_file", "list_files",
                "shell", "memory_remember", "memory_recall", "spawn_subagent",
            ]
            logger.info("极简模式使用默认精简工具集: %s", allowed_tools)
    else:
        # 完整模式：全部工具 + 完整提示词
        system_prompt = SYSTEM_PROMPT if not req.workspace_dir else SYSTEM_PROMPT
    if req.kind == "workspace" and req.workspace_dir:
        sandbox = req.sandbox or workspace_mod.load_sandbox(req.workspace_dir)
        ctx["workspace_dir"] = req.workspace_dir
        ctx["sandbox"] = sandbox
        ws_block = workspace_mod.system_block(ctx)
        if ws_block:
            system_prompt = SYSTEM_PROMPT + "\n\n" + ws_block

    # 把当前接口客户端注入工具上下文：spawn_subagent 派生子智能体时复用同一接口
    tools_mod.set_agent_context(client, settings)

    agent = Agent(
        client,
        system_prompt,
        context_limit=_resolve_context_limit(pm, effective_provider, effective_model),
        settings=settings,
        fallback_client=_build_fallback_client(pm, effective_provider, effective_model),
        allowed_tools=allowed_tools,
        eager_memory=True,
        thinking=_thinking_flag(effective_provider),
    )
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(ev: dict) -> None:
        # 前缀缓存命中率可观测性：接口返回 cache_usage 时打印日志
        if ev.get("type") == "cache_usage":
            hit = ev.get("hit", 0)
            miss = ev.get("miss", 0)
            total = hit + miss
            rate = (hit / total * 100.0) if total else 0.0
            logger.info(
                "[Cache] 前缀缓存命中率 %.1f%% (hit=%d, miss=%d)", rate, hit, miss
            )
        await queue.put(ev)

    # 把本请求的 emit 注入事件总线，供 task_board 等工具在请求上下文内推事件回 SSE 流
    emit_token = agent_events.set_request_emitter(emit)

    async def runner() -> None:
        try:
            await agent.run(
                req.history, req.message, emit, ctx=ctx, session_id=req.session_id,
                images=req.images,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent run failed")
            await emit({"type": "error", "message": str(exc)})
        finally:
            await queue.put(None)  # 哨兵：流结束
            clear_cancel(req.session_id)  # 清理取消标志，避免污染下次对话

    task = asyncio.create_task(runner())

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # 客户端（前端）断开连接：视为停止，触发后端中断并结束流
                    if await request.is_disconnected():
                        request_cancel(req.session_id)
                        break
                    continue
                if ev is None:
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            await task
        finally:
            # 收尾：清理当前请求的 emit 注入，避免泄漏到后续请求。
            # try/except 兜底：任何异常都不应导致 SSE 连接被异常关闭
            # （否则前端会误报「连接已断开」）。
            try:
                agent_events.reset_request_emitter(emit_token)
            except Exception as exc:  # noqa: BLE001
                logger.warning("reset_request_emitter 失败（已忽略）: %s", exc)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --------------------------------------------------------------------------
# 工作区：沙盒配置 / 记忆 / 信息
# --------------------------------------------------------------------------
@app.get("/api/workspace/info")
async def workspace_info(dir: str):
    p = workspace_mod._valid_ws_dir(dir)
    if p is None:
        return {"exists": False, "dir": dir, "sandbox": workspace_mod.DEFAULT_SANDBOX,
                "memory_count": 0}
    return {
        "exists": True,
        "dir": str(p),
        "sandbox": workspace_mod.load_sandbox(dir),
        "memory_count": workspace_mod.memory_count(dir),
    }


@app.put("/api/workspace/sandbox")
async def workspace_set_sandbox(req: WorkspaceSandboxRequest):
    ok = workspace_mod.save_sandbox(req.dir, {
        "allow_file_write": req.allow_file_write,
        "allow_file_delete": req.allow_file_delete,
        "allow_shell": req.allow_shell,
    })
    if not ok:
        raise HTTPException(400, "无效的工作区目录")
    return {"ok": True, "sandbox": workspace_mod.load_sandbox(req.dir)}


@app.get("/api/workspace/memory")
async def workspace_get_memory(dir: str, query: str = "", limit: int = 10):
    return {"memory": workspace_mod.recall_memory(dir, query=query, limit=limit)}


@app.post("/api/workspace/memory")
async def workspace_add_memory(req: WorkspaceMemoryRequest):
    if not req.content.strip():
        raise HTTPException(400, "content 不能为空")
    try:
        total = workspace_mod.add_memory(req.dir, req.content, req.tags)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "count": total}


@app.delete("/api/workspace/memory")
async def workspace_clear_memory(dir: str):
    ok = workspace_mod.clear_memory(dir)
    return {"ok": ok}


# --------------------------------------------------------------------------
# 全局记忆（agent_memory.jsonl）可视化：list / add / update / delete / clear
# 统一按 id 操作（记忆条目含 uuid id），前端主权交还用户。
# --------------------------------------------------------------------------
class MemoryCreate(BaseModel):
    content: str
    tags: list[str] = []


class MemoryUpdate(BaseModel):
    content: str | None = None
    tags: list[str] | None = None


@app.get("/api/memory")
async def memory_get(query: str = "", limit: int = 300):
    """返回全局记忆结构化列表（含 id / source / status），供前端渲染/搜索/编辑/删除。"""
    try:
        items = _gmem.list_all(query=query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))
    return {"items": items}


@app.post("/api/memory")
async def memory_create(req: MemoryCreate):
    """新增一条记忆（前端「+ 新增记忆」用）。

    走两档去重：无强相似直接保存；有强相似则由 LLM 对比确认
    （new=保存 / duplicate=跳过 / update=旧条失效+新条保存），返回真实结果。
    """
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    try:
        r = await _gmem.smart_add(content, tags=req.tags or [], source="manual")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))
    return {"ok": r.get("saved"), **r}


@app.put("/api/memory/{mid}")
async def memory_update(mid: str, req: MemoryUpdate):
    """按 id 更新一条记忆（前端内联编辑用）。"""
    try:
        entry = _gmem.update(mid, content=req.content, tags=req.tags)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))
    if entry is None:
        raise HTTPException(404, f"记忆 {mid} 不存在")
    return {"ok": True, "entry": entry}


@app.delete("/api/memory/{mid}")
async def memory_delete_id(mid: str):
    """按 id 删除一条记忆（取代旧的按 index 删除）。"""
    try:
        ok = _gmem.delete(mid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))
    if not ok:
        raise HTTPException(404, f"记忆 {mid} 不存在")
    return {"ok": True, "removed_id": mid}


@app.delete("/api/memory/clear")
async def memory_clear():
    """清空全部全局记忆。"""
    try:
        n = _gmem.clear_all()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))
    return {"ok": True, "count": n}


# --------------------------------------------------------------------------
# 技能市场（SkillHub）— 检索 / 分类 / 安装
# --------------------------------------------------------------------------
_SKILLHUB_API = "https://lightmake.site/api/skills"
# 真正的「下载技能包」接口：返回 302 -> COS 上的真实 zip（含 SKILL.md / 脚本 / WORKFLOW.md）
_SKILLHUB_DOWNLOAD = "https://lightmake.site/api/v1/download"
_SKILL_MARKET_CATEGORIES = [
    "ai-agent", "business-ops", "content-creation", "data-analysis",
    "design-media", "dev-programming", "education", "it-ops-security",
    "knowledge-management", "life-service", "office-efficiency", "professional",
]
_SKILLHUB_CATEGORY_LABELS = {
    "ai-agent": "AI 智能体",
    "business-ops": "商业运营",
    "content-creation": "内容创作",
    "data-analysis": "数据分析",
    "design-media": "设计媒体",
    "dev-programming": "开发编程",
    "education": "教育",
    "it-ops-security": "IT 运维安全",
    "knowledge-management": "知识管理",
    "life-service": "生活服务",
    "office-efficiency": "办公效率",
    "professional": "专业服务",
}
# 技能安装根目录改由 agents.skills_io.skill_install_root() 决定：
#   - 打包态：Electron 壳注入 MY_AGENT_SKILLS_DIR=AppData/Roaming/MY_AGENT/skills
#     （更新 exe 不丢，且不会往安装目录写脏数据）
#   - dev 态：回落 <项目根>/skills，行为同原实现


@app.get("/api/skill-market/categories")
async def skill_market_categories():
    """返回技能市场的分类列表（含中英标签）。"""
    return {
        "categories": [
            {"id": c, "label": _SKILLHUB_CATEGORY_LABELS.get(c, c)}
            for c in _SKILL_MARKET_CATEGORIES
        ]
    }


@app.get("/api/skill-market/search")
async def skill_market_search(keyword: str = "", page: int = 1, pageSize: int = 12):
    """代理 SkillHub 检索接口，返回技能列表与总数。"""
    page = max(1, int(page))
    pageSize = min(50, max(1, int(pageSize)))
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                _SKILLHUB_API,
                params={"keyword": keyword, "page": page, "pageSize": pageSize},
                headers={"User-Agent": "Mozilla/5.0 MY_AGENT/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"技能市场请求失败: {exc}")
    if not isinstance(data, dict) or data.get("code") != 0:
        raise HTTPException(502, f"技能市场返回异常: {data.get('msg') or data}")
    return {
        "skills": data.get("data", {}).get("skills", []),
        "total": data.get("data", {}).get("total", 0),
        "page": page,
        "pageSize": pageSize,
    }


class SkillInstallRequest(BaseModel):
    slug: str
    name: str | None = None
    description: str | None = None
    description_zh: str | None = None
    category: str | None = None
    source: str | None = None
    homepage: str | None = None
    version: str | None = None
    icon_url: str | None = None


# 以下原 _skill_installed_path / _read_installed / _write_installed 已统一迁移到
# agents.skills_io（read_installed / write_installed / installed_path），
# 以共享「用户数据目录」逻辑。调用点直接改用 skills_io 同名函数。


@app.post("/api/skill-market/install")
async def skill_market_install(req: SkillInstallRequest):
    """安装一个技能。

    优先通过 SkillHub 的下载接口拉取真实技能包（含 SKILL.md / 脚本 / WORKFLOW.md 等），
    解压到 skills/<slug>/，使其成为「可用技能」。
    若下载失败（网络/限流/接口异常），则降级为写入元数据存根，
    保证前端仍能显示「已安装」（best-effort 兜底）。
    """
    slug = (req.slug or "").strip()
    if not slug:
        raise HTTPException(400, "缺少 slug")
    desc = req.description_zh or req.description or ""
    meta = {
        "slug": slug,
        "name": req.name or slug,
        "description": desc,
        "category": req.category or "",
        "source": req.source or "",
        "homepage": req.homepage or "",
        "version": req.version or "",
        "icon_url": req.icon_url or "",
        "installed_at": datetime.now().isoformat(timespec="seconds"),
        "real": False,  # 是否装到了真实技能包（含正文）
    }
    skill_dir = skill_install_root() / slug

    # 1) 尝试拉取并解压真实技能包
    real_installed = False
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(_SKILLHUB_DOWNLOAD, params={"slug": slug})
            resp.raise_for_status()
            data = resp.content
        if data[:2] == b"PK":  # zip 魔数
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
            skill_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(BytesIO(data)) as zf:
                zf.extractall(skill_dir)
            real_installed = True
            meta["real"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("下载技能包失败 %s: %s，降级为元数据存根", slug, exc)

    # 2) 未拿到真实包 -> 写元数据存根（保证 SKILL.md 存在，前端仍可显示）
    if not real_installed:
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            stub = (
                f"---\n"
                f"name: {meta['name']}\n"
                f"slug: {slug}\n"
                f"category: {meta['category']}\n"
                f"source: {meta['source']}\n"
                f"homepage: {meta['homepage']}\n"
                f"version: {meta['version']}\n"
                f"installed_at: {meta['installed_at']}\n"
                f"---\n\n"
                f"# {meta['name']}\n\n"
                f"{desc}\n\n"
                f"> 由 SkillHub 技能市场安装（下载技能包失败，已降级为元数据存根）。\n"
                f"> 可在上方 homepage 查看完整技能。\n"
            )
            (skill_dir / "SKILL.md").write_text(stub, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入技能存根失败 %s: %s", slug, exc)
            raise HTTPException(500, f"写入技能存根失败: {exc}")

    # 3) 登记到 installed.json
    installed = read_installed()
    installed[slug] = meta
    write_installed(installed)
    return {
        "ok": True,
        "slug": slug,
        "real": real_installed,
        "path": str(skill_dir / "SKILL.md"),
        "installed": meta,
    }


@app.get("/api/skill-market/installed")
async def skill_market_installed():
    """返回已安装的技能清单。"""
    return {"installed": read_installed()}


# --------------------------------------------------------------------------
# 任务看板 + 定时任务
# --------------------------------------------------------------------------
@app.get("/api/tasks")
async def get_tasks(session_id: str = ""):
    """返回某会话当前的任务清单（前端切换会话时拉取，刷新看板）。"""
    from agents import board

    return {"session_id": session_id, "tasks": board.list_tasks(session_id)}


@app.get("/api/schedules")
async def list_schedules():
    """返回所有定时任务（含状态与最近执行历史）。"""
    from agents import scheduler as _sched

    jobs = [j.to_dict() for j in _sched.get_scheduler().list_jobs()]
    return {"jobs": jobs}


@app.post("/api/schedules")
async def create_schedule(req: ScheduleCreateRequest):
    from agents import scheduler as _sched

    try:
        job = _sched.get_scheduler().add_job(
            {
                "name": req.name,
                "prompt": req.prompt,
                "when": req.when,
                "provider": req.provider,
                "model": req.model,
            }
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "job": job.to_dict()}


@app.put("/api/schedules/{job_id}")
async def update_schedule(job_id: str, req: ScheduleUpdateRequest):
    from agents import scheduler as _sched

    job = _sched.get_scheduler().update_job(
        job_id,
        {
            "name": req.name,
            "prompt": req.prompt,
            "when": req.when,
            "provider": req.provider,
            "model": req.model,
            "enabled": req.enabled,
        },
    )
    if job is None:
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "job": job.to_dict()}


@app.delete("/api/schedules/{job_id}")
async def delete_schedule(job_id: str):
    from agents import scheduler as _sched

    ok = _sched.get_scheduler().delete_job(job_id)
    if not ok:
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@app.post("/api/schedules/{job_id}/run")
async def run_schedule_now(job_id: str):
    """立即手动触发一次定时任务（忽略调度时间）。"""
    from agents import scheduler as _sched

    result = await _sched.get_scheduler().run_now(job_id)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "任务不存在"))
    return result


# --------------------------------------------------------------------------
# 邮件网关 REST：状态 / 手动检查 / 写 token / 改配置 / 事件流
# --------------------------------------------------------------------------
class EmailTokenRequest(BaseModel):
    token: str


class EmailConfigRequest(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    poll_interval: int | None = None
    auto_reply: bool | None = None
    auto_approve: bool | None = None
    my_address: str | None = None
    allow_senders: list[str] | None = None
    max_process_per_poll: int | None = None


@app.get("/api/email/status")
async def email_status():
    """邮件网关状态：是否启用/运行中、是否已拿到凭据、最近处理的邮件。"""
    return _get_email_gateway().status()


@app.post("/api/email/check")
async def email_check():
    """立即手动拉取一次新邮件并处理（不等轮询间隔）。结果同时经 SSE 推给前端。"""
    gw = _get_email_gateway()
    result = await gw.poll_once(trigger="manual")
    result["status"] = gw.status()
    return result


@app.post("/api/email/set-token")
async def email_set_token(req: EmailTokenRequest):
    """手动写入 QQ 邮箱 MCP token（自动扫描找不到凭据时的兜底）。"""
    from agents.email_gateway import save_token

    tok = (req.token or "").strip()
    if not tok:
        raise HTTPException(400, "token 不能为空")
    save_token(tok)
    return {"ok": True, "has_token": True}


@app.post("/api/email/config")
async def email_config(req: EmailConfigRequest):
    """运行时改邮件网关配置（内存生效；enabled 变化会立即起停轮询）。"""
    gw = _get_email_gateway()
    cfg = settings.setdefault("email", {})
    changed = req.model_dump(exclude_none=True)
    cfg.update(changed)

    if "enabled" in changed:
        if changed["enabled"]:
            gw.start()
        else:
            await gw.stop()
    return {"ok": True, "config": cfg, "status": gw.status()}


@app.get("/api/email/stream")
async def email_stream(request: Request):
    """SSE：把邮件网关的事件（收信/已回复/工具活动）实时推给 App。"""
    queue: asyncio.Queue = asyncio.Queue()

    async def _conn(ev: dict[str, Any]) -> None:
        if str(ev.get("type", "")).startswith("email_"):
            await queue.put(ev)

    agent_events.register(_conn)

    async def gen():
        try:
            yield "data: " + json.dumps(
                {"type": "email_stream_open"}, ensure_ascii=False
            ) + "\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            agent_events.unregister(_conn)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# --------------------------------------------------------------------------
# 设置：provider / apikey 等（重装安全 — 写用户数据目录的 override）
# --------------------------------------------------------------------------
def _mask_api_key(key: str) -> str:
    """把 apikey 掩码成 ****xxxx，仅保留末 4 位用于辨识是否已设置。"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


def _mask_providers(settings: dict[str, Any]) -> dict[str, Any]:
    """返回 provider 配置，api_key 掩码并附带 has_api_key 标记。"""
    providers = settings.get("providers", {}) or {}
    out: dict[str, Any] = {}
    for name, cfg in providers.items():
        cfg = dict(cfg) if isinstance(cfg, dict) else {}
        raw_key = cfg.get("api_key", "")
        cfg["api_key"] = _mask_api_key(raw_key)
        cfg["has_api_key"] = bool(raw_key)
        out[name] = cfg
    return out


def _reload_settings() -> None:
    """重写 override 后，热更新模块级 settings / pm 及 scheduler 中的引用。"""
    global settings, pm
    settings = load_settings()
    pm = ProviderManager(settings)
    try:
        from agents import scheduler as _sched

        s = _sched.get_scheduler()
        s._settings = settings
        s._pm = ProviderManager(settings)
    except Exception as exc:  # 不影响主配置热更新
        logger.warning("热更新 scheduler 配置引用失败: %s", exc)


@app.get("/api/settings")
async def get_settings():
    """返回当前生效配置（api_key 已掩码），供前端设置面板展示。"""
    from config import get_override_path

    return {
        "default_provider": settings.get("default_provider"),
        "default_model": settings.get("default_model"),
        "providers": _mask_providers(settings),
        "override_path": str(get_override_path()),
    }


@app.post("/api/settings/override")
async def save_settings_override(req: SettingsOverrideRequest):
    """将覆盖片段写入用户数据目录的 settings.override.yaml（重装不丢）。"""
    from config import write_settings_override

    # 简单校验：只允许覆盖 providers / default_provider / default_model
    allowed = {"providers", "default_provider", "default_model"}
    cleaned = {k: v for k, v in req.override.items() if k in allowed}
    if not cleaned:
        raise HTTPException(400, "只允许覆盖 providers / default_provider / default_model")

    # 进一步校验 providers 内部只接受已知可改字段
    if "providers" in cleaned:
        prov_allowed = {"api_key", "base_url", "default_model", "display_name"}
        cleaned_providers: dict[str, Any] = {}
        for pname, pcfg in (cleaned["providers"] or {}).items():
            if not isinstance(pcfg, dict):
                continue
            cleaned_providers[pname] = {
                k: v for k, v in pcfg.items() if k in prov_allowed
            }
        cleaned["providers"] = cleaned_providers

    try:
        write_settings_override(cleaned, merge=True)
    except Exception as exc:
        logger.exception("写入 override 配置失败")
        raise HTTPException(500, f"写入失败: {exc}")

    _reload_settings()
    return {"ok": True, "settings": _mask_providers(settings)}


# ★ 会话权限状态（2026-08-08）：权限只能「降级」不能「升级」。
#   聊天请求体里的 permission_level 若高于已确认档位，一律降级，
#   杜绝「直接调 API 传 high 即提权」。提权必须走 POST /api/permission 端点
#   （前端滑动条调用；服务端记录，重启后回落默认 medium）。
_SESSION_PERM: dict[str, str] = {}
# 全局默认档位（无 session 的请求用它约束；前端滑动条设置时更新）
_GLOBAL_PERM: str = "medium"
_PERM_ORDER = {"low": 0, "medium": 1, "high": 2}


def _resolve_permission_level(session_id: str | None, requested: str | None) -> str:
    """按已确认档位约束请求档位：只降不升。"""
    want = str(requested or "medium").lower()
    if want not in _PERM_ORDER:
        want = "medium"
    base = "medium"
    if session_id:
        base = _SESSION_PERM.get(session_id) or _GLOBAL_PERM
    else:
        base = _GLOBAL_PERM
    if _PERM_ORDER.get(want, 1) > _PERM_ORDER.get(base, 1):
        return base  # 请求档位高于已确认档位 → 降级
    return want


class PermissionRequest(BaseModel):
    session_id: str | None = None
    level: str = "medium"  # low / medium / high


@app.post("/api/permission")
async def set_permission(req: PermissionRequest):
    """前端滑动条设置权限档位（服务端记录；session_id 为空则设全局档位）。

    安全意义：聊天请求体不能顺带提权（见 _resolve_permission_level）；
    无人值守场景（邮件网关）不经过此端点，始终默认 medium。
    """
    level = str(req.level or "medium").lower()
    if level not in _PERM_ORDER:
        raise HTTPException(400, "level 必须是 low / medium / high")
    global _GLOBAL_PERM
    if req.session_id:
        _SESSION_PERM[req.session_id] = level
    else:
        _GLOBAL_PERM = level
    return {"ok": True, "session_id": req.session_id, "level": level}


# --------------------------------------------------------------------------
# 删除确认目录（2026-08-08）：用户在前端通过资源管理器多选配置。
# 命中这些目录的 delete_file 即使 high 权限也强制确认；系统保护目录永远硬拦截。
# --------------------------------------------------------------------------
@app.get("/api/security/delete_confirm_dirs")
async def get_delete_confirm_dirs():
    """返回当前「删除需确认」目录列表 + 系统保护目录（只读展示）。"""
    from agents.tools import _PROTECTED_DIRS, delete_confirm_dirs

    return {
        "dirs": delete_confirm_dirs(),
        "protected": sorted(str(d) for d in _PROTECTED_DIRS),
    }


class DeleteConfirmDirsRequest(BaseModel):
    dirs: list[str] = []


@app.post("/api/security/delete_confirm_dirs")
async def save_delete_confirm_dirs(req: DeleteConfirmDirsRequest):
    """保存「删除需确认」目录列表到 override 配置（重装不丢）。

    安全约束：系统保护目录（Windows/Program Files/ProgramData/项目本体）
    永远硬拦截，即使被误写入本列表也不会解除保护（tools._is_protected 优先）。
    这里仍做一次过滤，避免把保护目录塞进列表造成误导。
    """
    from config import write_settings_override
    from agents.tools import _PROTECTED_DIRS, _is_protected

    cleaned: list[str] = []
    for p in req.dirs:
        s = str(p).strip()
        if not s:
            continue
        try:
            rp = Path(s).resolve()
        except Exception:  # noqa: BLE001
            cleaned.append(s)
            continue
        if _is_protected(rp):
            # 系统保护目录不需要（也不能）加入确认列表——永远硬拦截
            continue
        cleaned.append(s)

    try:
        write_settings_override({"security": {"delete_confirm_dirs": cleaned}}, merge=True)
    except Exception as exc:
        logger.exception("写入删除确认目录失败")
        raise HTTPException(500, f"写入失败: {exc}")

    _reload_settings()
    return {"ok": True, "dirs": cleaned}


# --------------------------------------------------------------------------
# 静态文件（Web UI）— 必须在所有路由之后，否则 StaticFiles 会拦截 /api/* 请求
# --------------------------------------------------------------------------
def _resolve_static_dir() -> Path:
    """定位前端构建产物 web/dist。

    优先级：
      1. 环境变量 MY_AGENT_STATIC_DIR（显式覆盖）
      2. 开发态/打包态：<backend根>/web/dist
         （打包后 backend 位于 resources/backend，故为 resources/backend/web/dist）
      3. 兜底：当前工作目录下的 web/dist（防止 cwd 被改动时找不到）
    选定后还会校验目录内确实存在 index.html，缺失则记录 CRITICAL 日志，
    便于「打包后前端空白」时快速定位（而不是静默 404 白屏）。
    """
    env = os.environ.get("MY_AGENT_STATIC_DIR")
    backend_root = Path(__file__).resolve().parent.parent
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(backend_root / "web" / "dist")
    candidates.append(Path.cwd() / "web" / "dist")

    for d in candidates:
        try:
            if d.is_dir() and (d / "index.html").is_file():
                return d
        except OSError:
            continue
    # 都没命中：返回首个候选（通常是 backend_root/web/dist），让下面的告警给出清晰信息
    return candidates[1] if len(candidates) > 1 else candidates[0]


_STATIC_DIR = _resolve_static_dir()
_STATIC_ENV = os.environ.get("MY_AGENT_STATIC_DIR")

if _STATIC_DIR.is_dir() and (_STATIC_DIR / "index.html").is_file():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
    logger.info("静态前端已挂载：%s", _STATIC_DIR)
else:
    logger.critical(
        "⚠️ 前端 web/dist/index.html 未找到（最后尝试路径：%s）。"
        "打包后前端空白通常是此处未命中：请确认 electron-builder 的 extraResources"
        " 已将 web/dist 完整拷贝到 resources/backend/web/dist，"
        "或设置环境变量 MY_AGENT_STATIC_DIR 指向含 index.html 的目录。",
        _STATIC_DIR,
    )


# 禁止浏览器/Electron 缓存前端静态资源。安装包升级后若缓存命中旧 index.html，
# 会引用已不存在的旧 assets → 404 → 白屏。对 / 和 /assets/* 强制无缓存头。
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response
