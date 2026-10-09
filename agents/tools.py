"""工具系统 — 注册表 + 执行器。

设计（仿 Hermes）：
  - 每个工具有 OpenAI 兼容的 JSON schema（parameters），下发给模型用。
  - 工具执行在服务端完成，结果回灌模型。
  - 前端只看到 build_tool_label / build_tool_preview（见 display.py），原始参数 JSON 永不外传。
"""

from __future__ import annotations

import asyncio
import contextvars
import json as _json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from agents import workspace as workspace_mod
from agents import skills_io as _skills_io
from agents import global_memory as _gmem
from agents import knowledge_base as kb_mod  # 内置知识库（自带空库，零外部依赖）

logger = logging.getLogger(__name__)

_CAP = 12000  # 工具结果回灌模型前的最大字符数

# 工作区上下文（按 asyncio 任务隔离，并发安全）。由 execute_tool 在每次调用前注入。
_WS_CTX: contextvars.ContextVar = contextvars.ContextVar("ws_ctx", default=None)

# 当前请求使用的「接口客户端 + 配置」：由 server/api.py 在每次对话前注入，
# 供 spawn_subagent 派生子智能体时复用同一接口（本项目不内置任何推理后端）。
_AGENT_CTX: contextvars.ContextVar = contextvars.ContextVar("agent_ctx", default=None)


def set_agent_context(client, settings: dict | None = None) -> None:
    """注入当前请求的接口客户端（server/api.py 在对话开始前调用）。"""
    _AGENT_CTX.set({"client": client, "settings": settings or {}})


def current_workspace_ctx() -> dict | None:
    """返回当前请求的工作区上下文（无则 None）。"""
    return _WS_CTX.get()


def _cap(text: str, limit: int = _CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断，共 {len(text)} 字符]"


def _dump_output_to_file(text: str, kind: str = "shell") -> str:
    """把超长输出完整写入临时文件，返回文件路径（供模型后续 read_file 读取）。"""
    try:
        import tempfile

        fd, path = tempfile.mkstemp(prefix=f"myagent_{kind}_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception as exc:  # noqa: BLE001
        return f"（落盘失败：{exc}）"


def _decode_output(raw: bytes) -> str:
    """把子进程原始字节安全解码为 str：保留中文与 emoji，不乱码、不用 ■ 替代表情。

    调用方已通过 chcp 65001 + PYTHONUTF8 强制子进程输出 UTF-8，故直接按 UTF-8 解码即可
    同时正确还原中文与 emoji。仅在（理论上不会出现的）非法 UTF-8 字节时回退 latin-1
    兜底——latin-1 可映射任意字节，绝不抛异常、绝不会产生替换方块 ■。
    全程不调用 .decode(..., "replace")。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _shell_env() -> dict:
    """给子进程注入的环境变量：强制 Python 子进程以 UTF-8 输出，避免 emoji/中文乱码。"""
    env = {**os.environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _win_utf8_prefix() -> str:
    """Windows 上把 cmd 代码页切到 UTF-8（65001），确保命令输出为 UTF-8 而非 GBK。"""
    if sys.platform.startswith("win"):
        return "chcp 65001 >nul 2>&1 && "
    return ""


def _safe_path(p: str) -> Path:
    """展开用户目录并解析绝对路径。"""
    return Path(os.path.expanduser(p)).resolve()


def _resolve_in_workspace(arg_path: str) -> Path:
    """解析工具参数里的路径。

    - 聊天会话：等效 _safe_path（全局，行为与之前一致）。
    - 工作区会话：把路径限制在工作区目录内，相对路径基于工作区根，
      绝对路径必须落在工作区内，否则抛 PermissionError。
    """
    ctx = current_workspace_ctx()
    if ctx and ctx.get("kind") == "workspace" and ctx.get("workspace_dir"):
        ws = workspace_mod._valid_ws_dir(ctx["workspace_dir"])
        if ws is None:
            raise PermissionError("工作区目录无效或不存在")
        raw = os.path.expanduser(arg_path)
        p = Path(raw).resolve() if os.path.isabs(raw) else (ws / arg_path).resolve()
        if p != ws and ws not in p.parents:
            raise PermissionError(f"路径越界：{p} 不在工作区 {ws} 内")
        return p
    return _safe_path(arg_path)


# =========================================================================
# 受保护目录 — 安全边界
# =========================================================================
# 这些目录模型绝不允许「删除 / 写入 / 修改」，即使前端确认也一律拒绝。
# 目的：防止 agent 误删系统、程序目录或项目本体造成不可逆损害。
# 如需让 agent 能改写项目自身代码，把项目根从 _PROTECTED_DIRS 移除即可。
_SYSTEM_DRIVE = (os.environ.get("SystemDrive", "C:") or "C:").rstrip("\\") or "C:"

_PROTECTED_DIRS: frozenset = frozenset(
    p.resolve()
    for p in [
        Path(os.environ.get("SystemRoot", f"{_SYSTEM_DRIVE}\\Windows")),
        Path(os.environ.get("ProgramFiles", f"{_SYSTEM_DRIVE}\\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", f"{_SYSTEM_DRIVE}\\Program Files (x86)")),
        Path(os.environ.get("ProgramData", f"{_SYSTEM_DRIVE}\\ProgramData")),
        Path(__file__).resolve().parent.parent,  # MY_AGENT 项目本体
    ]
)


def _is_protected(path) -> bool:
    """path 是否落在受保护目录内（含等于受保护目录本身）。"""
    try:
        rp = Path(path).resolve()
    except Exception:  # noqa: BLE001
        return False
    return any(rp == d or d in rp.parents for d in _PROTECTED_DIRS)


# python 代码中会删除/移动/覆盖文件的调用模式（首参为路径；支持 r/b/f 前缀的引号字符串）
_PY_FS_DELETE_PATTERNS = (
    re.compile(r"shutil\.rmtree\s*\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]"),
    re.compile(r"shutil\.rm\s*\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]"),
    re.compile(r"os\.remove\s*\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]"),
    re.compile(r"os\.unlink\s*\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]"),
    re.compile(r"os\.rmdir\s*\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]"),
    re.compile(r"os\.removedirs\s*\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]"),
    re.compile(r"Path\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]\s*\)\s*\.\s*(?:unlink|rmdir)\s*\("),
    re.compile(r"pathlib\.Path\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]\s*\)\s*\.\s*(?:unlink|rmdir)\s*\("),
    re.compile(r"os\.rename\s*\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]"),
    re.compile(r"os\.replace\s*\(\s*(?:[rRbBfFuU]{0,2})['\"]([^'\"]+)['\"]"),
)


def _python_code_touches_protected(code: str) -> str | None:
    """扫描 python 代码，检测对受保护目录的删除/移动/覆盖类操作。

    匹配「删除/移动类函数 + 首参为受保护目录路径」即返回拒绝原因。
    注意：这是对字符串字面量的静态检查，变量拼接的动态路径无法覆盖，
    但已足够拦住最常见的直接删除写法（与 shell 命令检查同一强度）。
    """
    for pat in _PY_FS_DELETE_PATTERNS:
        for m in pat.finditer(code or ""):
            p = m.group(1).strip()
            if not p:
                continue
            try:
                target = _resolve_in_workspace(p)
            except PermissionError:
                target = _safe_path(p)
            if _is_protected(target):
                return (
                    f"出于安全策略，python_exec 中禁止对受保护目录执行删除/移动/覆盖操作：{target}。"
                    "请改用 delete_file（会向你确认）或指定非受保护路径。"
                )
    return None


# -------------------------------------------------------------------------
# 删除确认目录（2026-08-08 新增）：用户在前端通过资源管理器多选配置。
# 命中这些目录的删除操作，即使权限档位为 high 也强制前端确认。
# 系统保护目录（_PROTECTED_DIRS）永远硬拦截，不在此列（不可被配置解除）。
# -------------------------------------------------------------------------
def delete_confirm_dirs() -> list[str]:
    """返回用户配置的「删除需确认」目录列表（settings.security.delete_confirm_dirs）。"""
    try:
        from config import load_settings

        s = load_settings().get("security") or {}
        lst = s.get("delete_confirm_dirs") or []
        return [str(x) for x in lst if str(x).strip()]
    except Exception:  # noqa: BLE001
        return []


def needs_delete_confirm(path: str) -> bool:
    """path 是否落在用户指定的「删除需确认」目录内（不含系统保护目录——那永远硬拦截）。"""
    if not path:
        return False
    try:
        rp = Path(path).resolve()
    except Exception:  # noqa: BLE001
        return False
    if _is_protected(rp):
        # 系统保护目录：永远硬拦截，不需要走确认（确认也拦不住），
        # 由 protected_block_reason 在更早阶段拒绝。
        return False
    for d in delete_confirm_dirs():
        try:
            dp = Path(d).resolve()
        except Exception:  # noqa: BLE001
            continue
        if rp == dp or dp in rp.parents:
            return True
    return False


def _tokenize_light(command: str) -> list[str]:
    """极简分词：按空白/管道/重定向切分并去引号，足以判断删除动词。"""
    out: list[str] = []
    for part in re.split(r"[\s|&;<>]+", command or ""):
        part = part.strip().strip('"').strip("'")
        if part:
            out.append(part)
    return out


# shell 中禁止直接出现的「删除/格式化」文件系统动词（大小写不敏感）
_DELETE_VERBS = frozenset({"del", "rmdir", "rd", "erase", "format", "rm"})
# 这些命令前的同名动词视为非文件系统删除（容器/包管理类），放行
_DELETE_EXEMPT_PREV = frozenset(
    {"docker", "git", "kubectl", "npm", "npx", "yarn", "pnpm", "podman", "helm"}
)


# shell 中禁止的「进程终止」动词（按名/映像批量杀会误杀托管本 agent 的后端）
_PROCESS_KILL_VERBS = frozenset({"stop-process", "taskkill", "tskill", "kill"})


def _shell_is_process_kill(command: str) -> tuple[bool, bool, set[int]]:
    """检测命令是否含进程终止意图。

    返回 (is_kill, by_name, pids)：
      is_kill -> 是否含终止进程动词
      by_name -> 是否按进程名/映像批量杀（高危，应拦截）；
                 False 表示按 PID 精确杀（相对安全，但需排除后端自身）
      pids    -> 命令中出现的纯数字 PID 集合（用于自保护校验）

    背景：之前模型在 workspace 执行「Get-Process python | Stop-Process -Force」
    按进程名批量杀，误杀了托管当前会话的后端进程，导致 SSE 断连、前端报
    network error。故按名/映像杀一律拦截，按 PID 杀仅放行且不许杀后端自身。
    """
    tokens = _tokenize_light(command)
    idx = 0
    while idx < len(tokens) and re.match(r"^[A-Za-z_]\w*=", tokens[idx]):
        idx += 1
    for i, tok in enumerate(tokens[idx:], start=idx):
        base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if base in _PROCESS_KILL_VERBS:
            rest = tokens[i + 1 :]
            rest_lower = [t.lower() for t in rest]
            pids = {int(t) for t in rest if re.fullmatch(r"\d+", t)}
            # 按映像名杀的明确标志：/IM, -Name, -Image，或管道 Get-Process 上游按名过滤
            by_image = (
                "/im" in rest_lower
                or "-name" in rest_lower
                or "-imagename" in rest_lower
                or ("get-process" in command.lower() and "|" in command)
            )
            # 带 PID 标志（-Id / /PID / -PID）或存在纯数字 PID 视为精确杀
            has_pid = (
                "-id" in rest_lower
                or "/pid" in rest_lower
                or "-pid" in rest_lower
                or bool(pids)
            )
            by_name = by_image or (not has_pid)
            return True, by_name, pids
    return False, False, set()


def _shell_is_destructive_delete(command: str) -> bool:
    """命令是否直接执行文件系统删除/格式化（忽略 docker rm / git clean 等）。"""
    tokens = _tokenize_light(command)
    idx = 0
    # 跳过开头的 VAR= 赋值（如 PATH=c:\x 命令 ...）
    while idx < len(tokens) and re.match(r"^[A-Za-z_]\w*=", tokens[idx]):
        idx += 1
    prev = None
    for tok in tokens[idx:]:
        base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if base in _DELETE_VERBS:
            if prev in _DELETE_EXEMPT_PREV:
                prev = base
                continue
            return True
        prev = base
    return False


def protected_block_reason(name: str, args: dict) -> str | None:
    """若操作触及受保护目录 / shell 危险命令，返回拒绝原因；否则 None。

    用于 agent_loop 在触发审批前直接拦截，也作为工具内部的二次防护。
    """
    if name == "python_exec":
        # ★ 补洞（2026-08-08）：python_exec 的执行体可以绕过 shell 字符串检查，
        # 用 shutil.rmtree / os.remove 直接删受保护目录。这里检测代码里的
        # 删除类调用 + 路径字符串，命中受保护目录即拒绝。
        code = str(args.get("code", ""))
        hit = _python_code_touches_protected(code)
        if hit:
            return hit
        return None
    if name == "shell":
        cmd = str(args.get("command", ""))
        if _shell_is_destructive_delete(cmd):
            return (
                "出于安全策略，shell 中禁止直接执行删除/格式化命令"
                "（del / rm / rmdir / rd / erase / format）。"
                "请改用 delete_file（删除文件，会向你确认）"
                "或 uninstall_software（卸载软件）工具。"
            )
        # 进程终止防护：禁止按名/映像批量杀（会误杀后端）；按 PID 杀不许杀后端自身
        is_kill, by_name, pids = _shell_is_process_kill(cmd)
        if is_kill and by_name:
            return (
                "出于安全策略，shell 中禁止按进程名/映像批量终止进程"
                "（如 Stop-Process -Name / Get-Process x | Stop-Process / taskkill /IM）。"
                "这会误杀托管本 agent 的后端进程，导致连接断开。"
                "如需结束某个进程，请先用精确 PID："
                "Stop-Process -Id <PID> 或 taskkill /PID <PID>。"
            )
        if is_kill and os.getpid() in pids:
            return "出于安全策略，禁止终止托管本 agent 的后端进程自身（PID 等于自身）。"
        return None
    if name in ("delete_file", "write_file", "edit_file"):
        p = args.get("path")
        if not p:
            return None
        try:
            target = _resolve_in_workspace(str(p))
        except PermissionError:
            return None
        if _is_protected(target):
            action = "删除" if name == "delete_file" else "写入/修改"
            return f"路径位于受保护目录，禁止{action}：{target}"
    return None


# =========================================================================
# 工具定义与执行器
# =========================================================================
TOOLS: dict[str, dict[str, Any]] = {}

# -------------------------------------------------------------------------
# 模型创建文件注册表 + 备份机制
# -------------------------------------------------------------------------
# 备份根目录：<项目根>/backups/model_files/
_BACKUP_ROOT = Path(__file__).resolve().parent.parent / "backups" / "model_files"
_MANIFEST_PATH = _BACKUP_ROOT / "manifest.json"

# 当前进程内「模型创建的文件」集合（存绝对路径字符串）
MODEL_CREATED: set[str] = set()


def _load_manifest() -> None:
    """启动时把持久化清单读入 MODEL_CREATED。"""
    global MODEL_CREATED
    if not _MANIFEST_PATH.exists():
        return
    try:
        data = _json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        MODEL_CREATED = set(data.get("created", []))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取模型创建文件清单失败: %s", exc)


def _persist_manifest() -> None:
    """把 MODEL_CREATED 持久化到 manifest.json。"""
    try:
        _BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        _MANIFEST_PATH.write_text(
            _json.dumps({"created": sorted(MODEL_CREATED)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("持久化模型创建文件清单失败: %s", exc)


def add_created(path: str) -> None:
    """登记一个由模型创建的文件（绝对路径）。"""
    MODEL_CREATED.add(str(Path(path).resolve()))
    _persist_manifest()


def is_created_by_model(path: str) -> bool:
    return str(Path(path).resolve()) in MODEL_CREATED


def _backup_file(path: str) -> str | None:
    """把 path 复制到 <备份根>/<时间戳>__<原名>。失败返回 None。"""
    src = Path(path).resolve()
    if not src.exists():
        return None
    try:
        _BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest = _BACKUP_ROOT / f"{ts}__{src.name}"
        # 同名冲突则加序号
        n = 1
        while dest.exists():
            dest = _BACKUP_ROOT / f"{ts}_{n}__{src.name}"
            n += 1
        if src.is_dir():
            import shutil

            shutil.copytree(src, dest)
        else:
            import shutil

            shutil.copy2(src, dest)
        return str(dest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("备份文件失败 %s: %s", path, exc)
        return None


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler,
    require_approval: bool = False,
    kind: str = "tool",
    min_permission: str = "medium",
):
    """注册一个工具。

    kind:
      - "tool"  普通内置工具（默认）
      - "skill" 由「技能市场」安装/内置的技能型工具，前端会以技能卡片样式突出显示

    min_permission（权限档位，2026-08-07 新增）:
      - "low"    纯聊天/只读类（web_search/read_file/kb/memory 等）
      - "medium" 写文件 + shell + 常规工具（默认档；危险操作仍走 require_approval）
      - "high"   接管电脑类（系统级修改/安装软件/桌面自动化等）——仅在高权限档可见可用
    """
    TOOLS[name] = {
        "name": name,
        "description": description,
        "parameters": parameters,
        "handler": handler,
        "require_approval": require_approval,
        "kind": kind,
        "min_permission": min_permission,
    }


# -------------------------------------------------------------------------
# think — 内部思考
# -------------------------------------------------------------------------
async def _think(args: dict) -> str:
    # 思考不回显给用户，仅作为模型的自言自语锚点
    return "已记录思考。"


register_tool(
    "think",
    "把你的计划/推理写下来，帮助理清多步任务。不构成对用户的输出。",
    {
        "type": "object",
        "properties": {
            "thought": {"type": "string", "description": "你的思考内容"}
        },
        "required": ["thought"],
    },
    _think,
    min_permission="low",  # ★ 只读工具：低权限档可见
)


# -------------------------------------------------------------------------
# Linux → Windows shell 命令翻译层（仅 Windows 主机生效）
# -------------------------------------------------------------------------
# 小本地模型（如 1B 级）常在 Windows 上发 Linux 命令（cat/ls/grep），Windows 无这些内建
# 命令，会报 "不是内部或外部命令" 的 GBK 乱码错误，模型难以理解。这里拦截已知 Linux 命令
# 自动替换为 Windows 等价物，降低小模型在 Windows 上踩坑的概率。翻译不改变命令语义，仅换壳。
_SHELL_LINUX_TO_WIN = {
    "cat": "type",
    "ls": "dir",
    "pwd": "cd",
    "grep": "findstr",
    "cp": "copy",
    "mv": "move",
    "rm": "del",
    "which": "where",
    "clear": "cls",
    "uname": "ver",
    "ps": "tasklist",
    "wget": "curl",
}
# 这些命令翻译后需剥掉 Unix 风格 -xxx 开关（Windows 用 /xxx 或不需要），避免把 -la 当文件名
_SHELL_STRIP_FLAGS = {"cat", "ls", "grep", "cp", "mv", "rm"}


def _translate_shell_to_windows(command: str) -> tuple[str, bool]:
    """把 Linux 习惯命令翻译成 Windows 等价命令。返回 (新命令, 是否发生翻译)。

    仅 Windows (os.name=='nt') 生效；不在 Windows 或非已知命令时原样返回。
    仅替换命令名 + 剥 Unix 开关 + 翻译 $VAR→%VAR%；不改动命令真实意图。
    """
    if os.name != "nt" or not command or not command.strip():
        return command, False
    changed = False
    # 按管道/链式分隔符切分，保留分隔符
    segs = re.split(r"(\s*(?:\|\||&&|\||;)\s*)", command)
    out: list[str] = []
    for seg in segs:
        if re.match(r"^\s*(?:\|\||&&|\||;)\s*$", seg):
            out.append(seg)
            continue
        parts = seg.split()
        if not parts:
            out.append(seg)
            continue
        cmd0 = parts[0]
        if cmd0 not in _SHELL_LINUX_TO_WIN:
            # 非已知命令：仅做环境变量 $VAR → %VAR% 翻译
            seg2 = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", r"%\1%", seg)
            if seg2 != seg:
                changed = True
            out.append(seg2)
            continue
        mapped = _SHELL_LINUX_TO_WIN[cmd0]
        rest = parts[1:]
        if cmd0 in _SHELL_STRIP_FLAGS:
            rest = [p for p in rest if not p.startswith("-")]
        rest = [re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", r"%\1%", p) for p in rest]
        out.append(" ".join([mapped] + rest))
        changed = True
    return "".join(out), changed


# -------------------------------------------------------------------------
# shell — 执行命令
# -------------------------------------------------------------------------
async def _shell(args: dict) -> str:
    command = args.get("command", "")
    if not command:
        return "错误：缺少 command"
    # Windows 主机：把小模型误发的 Linux 命令翻译成 Windows 等价命令
    command, _translated = _translate_shell_to_windows(command)
    block = protected_block_reason("shell", args)
    if block:
        return f"错误：{block}"
    ctx = current_workspace_ctx()
    cwd = None
    if ctx and ctx.get("kind") == "workspace":
        sandbox = ctx.get("sandbox") or {}
        if not sandbox.get("allow_shell", True):
            return "错误：当前工作区禁止执行命令（沙盒未开启 allow_shell）"
        ws = workspace_mod._valid_ws_dir(ctx.get("workspace_dir"))
        cwd = str(ws) if ws is not None else None
    timeout = float(args.get("timeout") or 120)
    if timeout <= 0 or timeout > 3600:
        timeout = 120
    output_limit = int(args.get("output_limit") or _CAP)
    output_limit = max(500, min(output_limit, 50000))
    try:
        proc = await asyncio.create_subprocess_shell(
            _win_utf8_prefix() + command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_shell_env(),
        )
        try:
            so, se = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
            return f"错误：命令执行超时（{int(timeout)}s）。可传 timeout 参数调大（如 timeout=600），或改用后台任务分批执行。"
        out = _decode_output(so)
        err = _decode_output(se)
        prefix = f"[cwd: {cwd}]\n" if cwd else ""
        # stdout / stderr 分离显示
        body = ""
        if out:
            body += f"【标准输出】\n{out}\n"
        if err:
            body += f"【标准错误】\n{err}"
        if not body:
            body = "(无输出)"
        full = prefix + f"[exit {proc.returncode}]\n{body}"
        if _translated:
            full = (
                "[提示] 你发出的命令被识别为 Linux 语法，已在 Windows 上自动翻译为等价命令。"
                "后续请直接用 Windows 命令（type/dir/findstr/copy/move/del 等）。\n"
                + full
            )
        # 长输出自动落盘：返回文件路径，不截断在 JSON 里
        if len(full) > output_limit:
            dump = _dump_output_to_file(full, "shell")
            return (
                f"[exit {proc.returncode}] 输出共 {len(full)} 字符，超过 {output_limit}，"
                f"已完整落盘：{dump}\n"
                + _cap(full, output_limit)
            )
        return full
    except Exception as exc:  # noqa: BLE001
        return f"执行失败: {exc}"


register_tool(
    "shell",
    "在用户机器上执行 shell 命令，返回合并的 stdout/stderr。用于真实完成任务。"
    "长输出（超过 output_limit）会自动完整落盘到临时文件并返回路径，可再 read_file 读取全文。"
    "命令超时默认 120s，耗时任务可传 timeout（最大 3600）。",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "number", "description": "超时秒数（默认 120，最大 3600）"},
            "output_limit": {"type": "number", "description": "回灌字符上限（默认 12000，超出自动落盘）"},
        },
        "required": ["command"],
    },
    _shell,
)


# -------------------------------------------------------------------------
# read_file
# -------------------------------------------------------------------------
async def _read_file(args: dict) -> str:
    try:
        path = _resolve_in_workspace(args.get("path", ""))
    except PermissionError as exc:
        return f"错误：{exc}"
    if not path.exists():
        return f"错误：文件不存在 {path}"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except IsADirectoryError:
        return f"错误：{path} 是目录"
    return _cap(text)


register_tool(
    "read_file",
    "读取文本文件内容。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "limit": {"type": "integer", "description": "最多读取行数（可选）"},
        },
        "required": ["path"],
    },
    _read_file,
    min_permission="low",  # ★ 只读工具：低权限档可见
)


# -------------------------------------------------------------------------
# write_file
# -------------------------------------------------------------------------
async def _write_file(args: dict) -> str:
    ctx = current_workspace_ctx()
    if ctx and ctx.get("kind") == "workspace":
        sandbox = ctx.get("sandbox") or {}
        if not sandbox.get("allow_file_write", True):
            return "错误：当前工作区禁止写入文件（沙盒未开启 allow_file_write）"
    try:
        path = _resolve_in_workspace(args.get("path", ""))
    except PermissionError as exc:
        return f"错误：{exc}"
    if _is_protected(path):
        return f"错误：路径位于受保护目录，禁止写入：{path}"
    content = args.get("content", "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        # 登记为「模型创建的文件」，删除前需备份
        add_created(str(path))
        return f"已写入 {len(content)} 字符到 {path}"
    except Exception as exc:  # noqa: BLE001
        return f"写入失败: {exc}"


register_tool(
    "write_file",
    "创建或覆盖写入文本文件。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "要写入的内容"},
        },
        "required": ["path", "content"],
    },
    _write_file,
)


# -------------------------------------------------------------------------
# list_files
# -------------------------------------------------------------------------
async def _list_files(args: dict) -> str:
    try:
        path = _resolve_in_workspace(args.get("path", "."))
    except PermissionError as exc:
        return f"错误：{exc}"
    if not path.is_dir():
        return f"错误：不是目录 {path}"
    try:
        entries = sorted(
            (f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in path.iterdir())
        )
        return _cap("\n".join(entries) or "（空目录）")
    except Exception as exc:  # noqa: BLE001
        return f"列出失败: {exc}"


register_tool(
    "list_files",
    "列出目录下的文件与子目录。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，默认当前目录"}
        },
    },
    _list_files,
    min_permission="low",  # ★ 只读工具：低权限档可见
)


# -------------------------------------------------------------------------
# web_fetch
# -------------------------------------------------------------------------
async def _web_fetch(args: dict) -> str:
    url = args.get("url", "")
    if not url:
        return "错误：缺少 url"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 HermesAgent/1.0"})
            resp.raise_for_status()
            text = resp.text
    except Exception as exc:  # noqa: BLE001
        return f"抓取失败: {exc}"
    # 粗略去 html 噪声
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _cap(text)


register_tool(
    "web_fetch",
    "抓取网页 URL 并返回其纯文本内容。",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的网页地址"}
        },
        "required": ["url"],
    },
    _web_fetch,
    min_permission="low",  # ★ 只读工具：低权限档可见
)


# -------------------------------------------------------------------------
# web_search — 多引擎联网搜索（SearXNG 聚合中文 / Google CSE / DuckDuckGo 兜底）
# 纯 httpx 实现，不引入新依赖；引擎按 settings.search.engine_chain 顺序尝试。
# -------------------------------------------------------------------------
_SEARCH_CFG_CACHE: dict | None = None


def _search_config() -> dict:
    """读取 settings.yaml 的 search: 段，带默认值，模块级缓存一次（改配置需重启后端）。"""
    global _SEARCH_CFG_CACHE
    if _SEARCH_CFG_CACHE is not None:
        return _SEARCH_CFG_CACHE
    try:
        from config import load_settings

        s = load_settings().get("search") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 search 配置失败，使用默认值: %s", exc)
        s = {}
    chain = s.get("engine_chain") or ["searxng", "ddg"]
    if isinstance(chain, str):
        chain = [chain]
    cfg = {
        "chain": chain,
        "searxng_url": (s.get("searxng_url") or "https://searx.be").rstrip("/"),
        "google_api_key": (s.get("google_api_key") or "").strip(),
        "google_cx": (s.get("google_cx") or "").strip(),
        "region": s.get("region") or "cn-zh",
    }
    _SEARCH_CFG_CACHE = cfg
    return cfg


def _fmt_search_items(items: list[dict], max_results: int) -> str:
    """把 [{title,url,snippet}] 格式化成可读列表，去空白。无有效项返回空串。"""
    items = [i for i in items if i.get("title") and i.get("url")][:max_results]
    if not items:
        return ""
    lines: list[str] = []
    for i, it in enumerate(items, 1):
        title = re.sub(r"\s+", " ", it["title"]).strip()
        url = it["url"].strip()
        snippet = re.sub(r"\s+", " ", it.get("snippet", "")).strip()
        line = f"{i}. {title}\n   {url}"
        if snippet:
            line += f"\n   {snippet}"
        lines.append(line)
    return "\n".join(lines)


async def _search_ddg(query: str, max_results: int, cfg: dict) -> str:
    """DuckDuckGo HTML 搜索（零配置兜底）。设 kl=cn-zh 偏向中文结果。"""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": cfg.get("region", "cn-zh")},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"DuckDuckGo 请求失败: {exc}")
    items: list[dict] = []
    # 结果块：标题+链接在 result__a，摘要在 result__snippet
    for m in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
        html, re.S,
    ):
        items.append(
            {
                "title": re.sub(r"<[^>]+>", "", m.group(2)),
                "url": m.group(1),
                "snippet": re.sub(r"<[^>]+>", "", m.group(3)),
            }
        )
    # 兜底：只抓标题链接（无 snippet）
    if not items:
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            items.append(
                {
                    "title": re.sub(r"<[^>]+>", "", m.group(2)),
                    "url": m.group(1),
                    "snippet": "",
                }
            )
    out = _fmt_search_items(items, max_results)
    return out or "未找到结果（可能触发了反爬，稍后重试或切换引擎）。"


def _parse_searxng_html(html: str) -> list[dict]:
    """解析 SearXNG 结果页 HTML（article.result 结构）。"""
    items: list[dict] = []
    for m in re.finditer(r'<article[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</article>', html, re.S):
        block = m.group(1)
        a = re.search(
            r'<a[^>]*class="[^"]*url_header[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S
        )
        if not a:
            a = re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        snip = re.search(r'<p[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</p>', block, re.S)
        items.append(
            {
                "title": re.sub(r"<[^>]+>", "", a.group(2)),
                "url": a.group(1),
                "snippet": re.sub(r"<[^>]+>", "", snip.group(1)) if snip else "",
            }
        )
    return items


async def _search_searxng(query: str, max_results: int, cfg: dict) -> str:
    """SearXNG 元搜索（聚合百度/搜狗/360/Bing 等，中文结果好，免费、无需 key）。"""
    base = cfg.get("searxng_url") or "https://searx.be"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            # 先试 JSON（多数实例支持）
            resp = await client.get(
                f"{base}/search",
                params={"q": query, "format": "json", "language": "zh"},
                headers={"User-Agent": "Mozilla/5.0 MY_AGENT/1.0", "Accept": "application/json"},
            )
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                data = resp.json()
                items = [
                    {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
                    for r in (data.get("results") or [])
                ]
                out = _fmt_search_items(items, max_results)
                if out:
                    return out
            # 回落 HTML 解析
            resp2 = await client.get(
                f"{base}/search",
                params={"q": query, "language": "zh"},
                headers={"User-Agent": "Mozilla/5.0 MY_AGENT/1.0", "Accept": "text/html"},
            )
            resp2.raise_for_status()
            html = resp2.text
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"SearXNG 请求失败 ({base}): {exc}")
    out = _fmt_search_items(_parse_searxng_html(html), max_results)
    return out or "SearXNG 未返回结果。"


async def _search_google(query: str, max_results: int, cfg: dict) -> str:
    """Google 自定义搜索（CSE）：中文最稳，免费 100 次/天，需申请 key。"""
    key = cfg.get("google_api_key")
    cx = cfg.get("google_cx")
    if not key or not cx:
        raise RuntimeError("未配置 google_api_key / google_cx")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": key,
                    "cx": cx,
                    "q": query,
                    "hl": "zh-CN",
                    "gl": "cn",
                    "num": min(max_results, 10),
                },
                headers={"User-Agent": "Mozilla/5.0 MY_AGENT/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Google CSE 请求失败: {exc}")
    items = [
        {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
        for r in (data.get("items") or [])
    ]
    out = _fmt_search_items(items, max_results)
    return out or "Google 未返回结果。"


async def _web_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "错误：缺少 query"
    max_results = int(args.get("max_results") or 8)
    cfg = _search_config()
    chain = list(cfg["chain"])
    # 配了 Google key 则自动优先用 Google（中文最稳）
    if cfg.get("google_api_key") and cfg.get("google_cx") and "google" not in chain:
        chain = ["google"] + chain
    tried: list[str] = []
    last_err: Exception | None = None
    for eng in chain:
        tried.append(eng)
        try:
            if eng == "google":
                res = await _search_google(query, max_results, cfg)
            elif eng == "searxng":
                res = await _search_searxng(query, max_results, cfg)
            else:
                res = await _search_ddg(query, max_results, cfg)
            if res:
                return _cap(res)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("web_search 引擎 %s 失败: %s", eng, exc)
            continue
    return f"搜索失败（已尝试 {tried}）：{type(last_err).__name__}: {last_err}"


register_tool(
    "web_search",
    "联网搜索网页，返回相关结果的标题、链接与摘要。默认先用 SearXNG（聚合百度/搜狗/360 等中文引擎，"
    "中文结果好），失败回落 DuckDuckGo；若配置了 Google CSE key 则优先用 Google（中文最稳）。"
    "适合查资料、查文档、核实事实。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词（中文也支持）"},
            "max_results": {"type": "integer", "description": "最多返回条数（默认 8）"},
        },
        "required": ["query"],
    },
    _web_search,
    min_permission="low",  # ★ 只读工具：低权限档可见
)


# -------------------------------------------------------------------------
# python_exec
# -------------------------------------------------------------------------
async def _python_exec(args: dict) -> str:
    code = args.get("code", "")
    if not code:
        return "错误：缺少 code"
    # ★ 内部二次防护：与 shell 同强度，即使绕过 agent_loop 直接调工具也拦截
    block = protected_block_reason("python_exec", {"code": code})
    if block:
        return "操作被安全策略阻止：" + block
    runner = (
        "import sys, json, io\n"
        "import contextlib\n"
        "_out = io.StringIO()\n"
        "try:\n"
        "    with contextlib.redirect_stdout(_out), contextlib.redirect_stderr(_out):\n"
        "        exec(compile(sys.stdin.read(), '<user>', 'exec'))\n"
        "except Exception as _e:\n"
        "    print('ERROR:', repr(_e), file=sys.stderr)\n"
        "finally:\n"
        "    sys.stdout.write(_out.getvalue())\n"
    )
    try:
        # ★ 用 sys.executable 显式指定解释器（避免 PATH 里的 python 指向错误版本，
        #   或含撇号用户目录下 create_subprocess 解析歧义导致 OSError(22)）
        py = sys.executable or "python"
        proc = await asyncio.create_subprocess_exec(
            py, "-c", runner,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_shell_env(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(code.encode("utf-8")), timeout=60)
        return _cap(_decode_output(stdout))
    except asyncio.TimeoutError:
        return "错误：Python 执行超时（60s）"
    except Exception as exc:  # noqa: BLE001
        return f"执行失败: {exc}"


register_tool(
    "python_exec",
    "在隔离子进程里运行 Python 代码并返回 stdout/stderr。适合计算、数据处理。",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"}
        },
        "required": ["code"],
    },
    _python_exec,
)


# =========================================================================
# edit_file — 字符串替换式编辑（避免整文件覆盖）
# =========================================================================
async def _edit_file(args: dict) -> str:
    try:
        path = _resolve_in_workspace(args.get("path", ""))
    except PermissionError as exc:
        return f"错误：{exc}"
    if _is_protected(path):
        return f"错误：路径位于受保护目录，禁止修改：{path}"
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    if not path.exists():
        return f"错误：文件不存在 {path}"
    if not old:
        return "错误：缺少 old_string"
    # 若是模型创建的文件，编辑前先备份，避免改坏无法还原
    if is_created_by_model(str(path)):
        _backup_file(str(path))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return f"读取失败: {exc}"
    if old not in text:
        return "错误：未在文件中找到 old_string（请确认内容完全一致，含空白）"
    replace_all = bool(args.get("replace_all", False))
    if replace_all:
        count = text.count(old)
        text = text.replace(old, new)
    else:
        count = 1
        text = text.replace(old, new, 1)
    try:
        path.write_text(text, encoding="utf-8")
        return f"已替换 {count} 处 -> {path}"
    except Exception as exc:  # noqa: BLE001
        return f"写入失败: {exc}"


register_tool(
    "edit_file",
    "在文件中把一段文本(old_string)替换成新文本(new_string)。用于精确修改文件，避免整文件覆盖。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原始文本（需与文件内容完全一致）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换全部匹配（默认只换第一处）"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    _edit_file,
)


# =========================================================================
# delete_file — 删除文件/目录（需前端确认）
# =========================================================================
async def _delete_file(args: dict) -> str:
    ctx = current_workspace_ctx()
    if ctx and ctx.get("kind") == "workspace":
        sandbox = ctx.get("sandbox") or {}
        if not sandbox.get("allow_file_delete", False):
            return "错误：当前工作区禁止删除文件（沙盒未开启 allow_file_delete）"
    try:
        path = _resolve_in_workspace(args.get("path", ""))
    except PermissionError as exc:
        return f"错误：{exc}"
    if _is_protected(path):
        return f"错误：路径位于受保护目录，禁止删除：{path}"
    if not path.exists():
        return f"错误：文件/目录不存在 {path}"
    # 若是模型创建的文件，删除前先备份，避免误删无法还原
    if is_created_by_model(str(path)):
        _backup_file(str(path))
        MODEL_CREATED.discard(str(path))
        _persist_manifest()
    try:
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink()
        return f"已删除 {path}"
    except Exception as exc:  # noqa: BLE001
        return f"删除失败: {exc}"


def build_approval_info(name: str, args: dict) -> dict[str, Any] | None:
    """构造「需要审批」事件附带的净化信息（不含原始参数 JSON）。"""
    if name == "uninstall_software":
        return {"target": args.get("name", ""), "kind": "uninstall"}
    if name != "delete_file":
        return None
    try:
        path = _resolve_in_workspace(args.get("path", ""))
    except PermissionError:
        path = _safe_path(args.get("path", ""))
    created = is_created_by_model(str(path))
    return {
        "path": str(path),
        "created_by_model": created,
        "will_backup": created,  # 模型创建的文件删除前会先备份
    }


register_tool(
    "delete_file",
    "删除一个文件或目录。注意：此操作需用户在前端确认（尤其是模型创建的文件会先自动备份再删除）。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要删除的文件或目录路径"}
        },
        "required": ["path"],
    },
    _delete_file,
    require_approval=True,  # ★ 触发前端确认闸门
)


# =========================================================================
# search_content — 正则检索文件内容（grep）
# =========================================================================
_SEARCH_EXCLUDE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".workbuddy", "venv", ".venv",
    "backups", "logs", ".idea", ".vscode", "dist", "build", ".next",
})


async def _search_content(args: dict) -> str:
    pattern = args.get("pattern", "")
    if not pattern:
        return "错误：缺少 pattern"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"错误：正则无效 {exc}"
    try:
        root = _resolve_in_workspace(args.get("path", "."))
    except PermissionError as exc:
        return f"错误：{exc}"
    glob = args.get("glob", "*")
    max_results = int(args.get("max_results", 60))
    try:
        files = [root] if root.is_file() else [p for p in root.rglob(glob) if p.is_file()]
    except Exception as exc:  # noqa: BLE001
        return f"遍历失败: {exc}"
    results: list[str] = []
    scanned = 0
    for p in files:
        if any(part in _SEARCH_EXCLUDE_DIRS for part in p.parts):
            continue
        scanned += 1
        if scanned > 4000:
            results.append("…[已停止：扫描文件过多]")
            break
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                rel = str(p)
                results.append(f"{rel}:{i}: {line.strip()}")
                if len(results) >= max_results:
                    results.append(f"…[已截断，最多 {max_results} 条]")
                    return _cap("\n".join(results))
    return _cap("\n".join(results)) if results else "未找到匹配"


register_tool(
    "search_content",
    "在文件/目录中按正则搜索内容，返回 文件:行号:匹配行。用于定位代码、配置、日志。",
    {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索根目录或文件，默认当前目录"},
            "glob": {"type": "string", "description": "文件名匹配，如 '*.py'（可选）"},
            "max_results": {"type": "integer", "description": "最多返回条数（默认 60）"},
        },
        "required": ["pattern"],
    },
    _search_content,
)


# =========================================================================
# time_now — 当前时间
# =========================================================================
async def _time_now(args: dict) -> str:
    utc = datetime.now(__import__("datetime").timezone.utc)
    local = datetime.now()
    return "\n".join([
        f"UTC:    {utc.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"本地:   {local.strftime('%Y-%m-%d %H:%M:%S')}",
        f"ISO8601:{local.isoformat()}",
        f"Unix:   {int(local.timestamp())}",
    ])


register_tool(
    "time_now",
    "返回当前 UTC / 本地时间、ISO8601 与时间戳。用于需要“现在”时间的任务。",
    {
        "type": "object",
        "properties": {
            "unused": {"type": "string", "description": "无需参数"}
        },
    },
    _time_now,
    min_permission="low",  # ★ 只读工具：低权限档可见
)


# =========================================================================
# download — 下载 URL 到本地文件
# =========================================================================
async def _download(args: dict) -> str:
    url = args.get("url", "")
    dest = args.get("path", "")
    if not url:
        return "错误：缺少 url"
    if not dest:
        return "错误：缺少 path（保存路径）"
    ctx = current_workspace_ctx()
    if ctx and ctx.get("kind") == "workspace":
        sandbox = ctx.get("sandbox") or {}
        if not sandbox.get("allow_file_write", True):
            return "错误：当前工作区禁止写入文件（沙盒未开启 allow_file_write）"
    try:
        path = _resolve_in_workspace(dest)
    except PermissionError as exc:
        return f"错误：{exc}"
    timeout = float(args.get("timeout") or 60)
    retries = int(args.get("retries") or 3)
    delay = args.get("delay") or 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ★ 统一网络层：浏览器头 + 指数退避重试 + 封禁识别（下载也走同一套反爬博弈）
        import random

        headers = _browser_headers(args.get("headers") or {})
        max_retries = max(0, int(retries))
        if isinstance(delay, (list, tuple)) and len(delay) == 2:
            base_delay = random.uniform(float(delay[0]), float(delay[1]))
        else:
            base_delay = float(delay or 0)
        last_err: Exception | None = None
        total = 0
        for attempt in range(max_retries + 1):
            if base_delay > 0:
                await asyncio.sleep(base_delay)
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    async with client.stream("GET", url, headers=headers) as resp:
                        status = resp.status_code
                        if status in _ANTI_BOT_CODES or status >= 500:
                            if attempt < max_retries:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            note = ""
                            if status == 567:
                                note = "（站点反爬拦截，如 BWIKI EdgeOne）"
                            elif status == 429:
                                note = "（请求过于频繁被限流）"
                            return f"下载失败 HTTP {status}{note}"
                        resp.raise_for_status()
                        total = 0
                        with open(path, "wb") as f:
                            async for chunk in resp.aiter_bytes(8192):
                                f.write(chunk)
                                total += len(chunk)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        return f"已下载 {total} 字节 -> {path}"
    except Exception as exc:  # noqa: BLE001
        return f"下载失败: {exc}"



register_tool(
    "download",
    "把网页/文件 URL 下载并保存到本地路径。框架层自动附加浏览器头、对网络错误/反爬/5xx 指数退避重试，"
    "遇到 HTTP 567/429/403/503 会给出友好提示（反爬/限流）。",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要下载的 URL"},
            "path": {"type": "string", "description": "本地保存路径"},
            "timeout": {"type": "number", "description": "超时秒数（默认 60）"},
            "retries": {"type": "number", "description": "失败重试次数（默认 3，指数退避）"},
            "delay": {
                "anyOf": [{"type": "number"}, {"type": "array", "items": {"type": "number"}}],
                "description": "请求前随机等待秒数：单值或 [min,max] 区间，防高频限流（可选）",
            },
            "headers": {"type": "object", "description": "附加请求头（可选，如 Referer/Cookie 反爬）"},
        },
        "required": ["url", "path"],
    },
    _download,
)


# =========================================================================
# 统一网络请求层 — 浏览器 UA + 指数退避重试 + 反爬拦截识别
# 把「反爬博弈」封装进框架层：模型只传 url/method/body，其余由这里兜底。
# =========================================================================
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 反爬拦截状态码（EdgeOne/CF 等 WAF 常见）：HTTP 567 是 BWIKI EdgeOne 的拦截码
_ANTI_BOT_CODES = {403, 429, 503, 567}


def _browser_headers(headers: dict | None = None, accept_json: bool = False) -> dict:
    """默认浏览器请求头；调用方传的 headers 优先覆盖。"""
    base = {
        "User-Agent": _BROWSER_UA,
        "Accept": (
            "application/json, text/html, application/xhtml+xml, */*;q=0.8"
            if accept_json
            else "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if headers:
        base.update({str(k): str(v) for k, v in headers.items()})
    return base


def _url_variants(url: str) -> list[str]:
    """换出口变体：原 URL + 协议互换（https↔http）+ 加/去 www。
    用于反爬拦截时自动切换访问入口（站点常按协议/子域/IP 模式拦截）。"""
    variants = [url]
    lowered = url.strip().lower()
    try:
        if lowered.startswith("https://"):
            variants.append("http://" + url[len("https://"):])
        elif lowered.startswith("http://"):
            variants.append("https://" + url[len("http://"):])
        # www 子域切换（仅当主机名不含 www 时加，含 www 时去）
        if "://" in url:
            scheme, rest = url.split("://", 1)
            host, _, tail = rest.partition("/")
            if host.startswith("www."):
                variants.append(f"{scheme}://{host[4:]}/{tail}" if tail else f"{scheme}://{host[4:]}")
            elif host and "." in host and not host.startswith("www."):
                variants.append(f"{scheme}://www.{host}/{tail}" if tail else f"{scheme}://www.{host}")
    except Exception:  # noqa: BLE001
        pass
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def _http_fetch(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: str | None = None,
    timeout: float = 30,
    retries: int = 3,
    delay: float = 0,
) -> tuple[int, dict, str]:
    """统一请求内核：浏览器头 + 随机间隔 + 指数退避重试。

    返回 (status_code, resp_headers, text)。
    - 网络错误 / 5xx / 反爬拦截码：指数退避重试（1s→2s→4s），retries 次后放弃。
    - 随机间隔 delay（秒）：可传区间 [min,max] 或单值，防高频请求被限流。
    """
    import random

    headers = _browser_headers(headers)
    max_retries = max(0, int(retries))
    if isinstance(delay, (list, tuple)) and len(delay) == 2:
        base_delay = random.uniform(float(delay[0]), float(delay[1]))
    else:
        base_delay = float(delay or 0)
    last_exc: Exception | None = None
    # ★ R-05 换出口增强：同一 URL 连续被反爬拦截时，自动尝试协议/子域变体
    #   （https↔http、加/去 www），应对站点按 UA/IP/路径模式拦截的场景。
    url_variants = _url_variants(url)
    for attempt in range(max_retries + 1):
        try_url = url_variants[min(attempt, len(url_variants) - 1)]
        if base_delay > 0:
            await asyncio.sleep(base_delay)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.request(method, try_url, headers=headers, content=body)
            status = resp.status_code
            # 反爬拦截 / 服务端错误 → 重试（注意：403 也可能是真权限问题，仍重试一次再报）
            if status in _ANTI_BOT_CODES or status >= 500:
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)  # 1s → 2s → 4s
                    continue
            return status, dict(resp.headers), resp.text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
                continue
    raise last_exc or RuntimeError("网络请求失败")


async def _http_request(args: dict) -> str:
    method = str(args.get("method") or "GET").upper()
    url = args.get("url", "")
    if not url:
        return "错误：缺少 url"
    headers = args.get("headers") or {}
    body = args.get("body")
    timeout = float(args.get("timeout") or 30)
    retries = int(args.get("retries") or 3)
    delay = args.get("delay") or 0
    try:
        status, resp_headers, text = await _http_fetch(
            method,
            url,
            headers=headers,
            body=body,
            timeout=timeout,
            retries=retries,
            delay=delay,
        )
        head = "\n".join(f"{k}: {v}" for k, v in list(resp_headers.items())[:15])
        # 反爬拦截友好提示：让模型知道是 WAF 挡了，而不是代码问题
        note = ""
        if status == 567:
            note = "\n⚠️ HTTP 567：站点反爬拦截（如 BWIKI EdgeOne）。可重试、换数据源，或带完整浏览器 Cookie/Referer 再试。"
        elif status == 429:
            note = "\n⚠️ HTTP 429：请求过于频繁被限流。可稍等重试，或加 delay 参数（如 delay=[2,5]）降低频率。"
        elif status in (403, 503):
            note = f"\n⚠️ HTTP {status}：疑似被站点拦截或服务暂时不可用。可重试、换源，或补充 Referer/Cookie。"
        full = f"HTTP {status}{note}\n{head}\n\n{text}"
        # 超长输出（>50KB）自动完整落盘 + 返回路径与摘要，不截断塞回 JSON
        if len(full) > 50 * 1024:
            dump = _dump_output_to_file(full, "http")
            return (
                f"HTTP {status}{note}\n\n⚠️ 响应共 {len(full)} 字符（>{50 * 1024}），"
                f"已完整落盘：{dump}\n\n"
                + _cap(full, 12000)
            )
        return _cap(full)
    except Exception as exc:  # noqa: BLE001
        return f"请求失败（已自动重试）: {exc}"


register_tool(
    "http_request",
    "发起任意 HTTP 请求（GET/POST/...）访问 API，返回状态码+响应头+正文。"
    "框架层自动附加浏览器 User-Agent 与常用请求头、对网络错误/5xx/反爬拦截做指数退避重试，"
    "遇到 HTTP 567/429/403/503 会附带友好提示（反爬/限流/换源）。用于调用外部服务、抓取网页数据。",
    {
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "HTTP 方法，如 GET/POST/PUT/DELETE（默认 GET）"},
            "url": {"type": "string", "description": "请求地址"},
            "headers": {"type": "object", "description": "请求头（可选，覆盖默认浏览器头；可传 Referer/Cookie 反爬）"},
            "body": {"type": "string", "description": "请求体（可选）"},
            "timeout": {"type": "number", "description": "超时秒数（默认 30）"},
            "retries": {"type": "number", "description": "失败重试次数（默认 3，指数退避）"},
            "delay": {
                "anyOf": [{"type": "number"}, {"type": "array", "items": {"type": "number"}}],
                "description": "请求前随机等待秒数：单值或 [min,max] 区间，防高频限流（可选）",
            },
        },
        "required": ["url"],
    },
    _http_request,
    min_permission="low",  # ★ 只读工具：低权限档可见
)


# =========================================================================
# memory — 跨会话持久记忆（委托 agents/global_memory，BM25 召回 + 两档去重）
# =========================================================================
# 旧 _MEMORY_FILE 实现已迁移到 agents/global_memory（同路径 agent_memory.jsonl），
# 这里只保留工具壳 + 工作区隔离分支；全局记忆的增删改查全部委托 _gmem。
#
# 两档去重（2026-08-05 用户定）：
#   档1 — 无强相似候选（BM25 < 3.0）→ 直接保存。
#   档2 — 有强相似候选（BM25 ≥ 3.0）→ 不再一刀切「判无效」，而是注入 prompt 让
#         LLM 对比确认（new / duplicate / update），按真实结果执行并返回真实结果。


async def _memory_remember(args: dict) -> str:
    content = args.get("content", "")
    if not content:
        return "错误：缺少 content"
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    # 工作区会话：记忆隔离到工作区目录
    ctx = current_workspace_ctx()
    if ctx and ctx.get("kind") == "workspace" and ctx.get("workspace_dir"):
        try:
            total = workspace_mod.add_memory(ctx["workspace_dir"], content, tags)
            return f"已记忆到工作区（当前共 {total} 条）"
        except Exception as exc:  # noqa: BLE001
            return f"记忆失败: {exc}"
    # 否则走全局记忆（两档去重）
    try:
        r = await _gmem.smart_add(content, tags=list(tags), source="manual")
        if not r.get("saved"):
            return f"未添加：{r.get('reason') or '与已有记忆重复'}"
        if r.get("verdict") == "update":
            return f"已更新记忆并保存（{r.get('reason') or ''}，新 id={r['entry']['id'][:8]}）"
        return f"已记忆（id={r['entry']['id'][:8]}）"
    except Exception as exc:  # noqa: BLE001
        return f"记忆失败: {exc}"


async def _memory_recall(args: dict) -> str:
    query = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 10)
    # 工作区会话：只检索该工作区记忆
    ctx = current_workspace_ctx()
    if ctx and ctx.get("kind") == "workspace" and ctx.get("workspace_dir"):
        return _cap(workspace_mod.recall_memory(ctx["workspace_dir"], query=query, limit=limit))
    try:
        hits = _gmem.search(query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return f"读取失败: {exc}"
    if not hits:
        return "未找到相关记忆" if query else "（空）"
    lines = []
    for h in hits:
        tagstr = ("  #" + " #".join(h.get("tags", []))) if h.get("tags") else ""
        lines.append(f"[{h.get('ts', '')}] {h['content']}{tagstr}")
    return _cap("\n".join(lines))


register_tool(
    "memory_remember",
    "把一条信息写入持久记忆（跨会话保留），可带 tags 便于检索。",
    {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "标签（可选）"},
        },
        "required": ["content"],
    },
    _memory_remember,
)

register_tool(
    "memory_recall",
    "按关键词检索持久记忆，返回最近的若干条（BM25 相关度排序）。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词（留空则返回全部）"},
            "limit": {"type": "integer", "description": "最多返回条数（默认 10）"},
        },
    },
    _memory_recall,
    min_permission="low",  # ★ 只读工具：低权限档可见
)


# --------------------------------------------------------------------------
# 全局记忆可视化管理（供前端「记忆」面板调用，委托 agents/global_memory）
# --------------------------------------------------------------------------
def memory_list(query: str = "", limit: int = 200) -> list[dict]:
    """返回全局记忆的结构化列表（含 id / source / status），供前端渲染/搜索/编辑/删除。"""
    try:
        return _gmem.list_all(query=query, limit=limit)
    except Exception:  # noqa: BLE001
        return []


def memory_delete(
    index: int | None = None,
    content_substr: str | None = None,
    mid: str | None = None,
) -> dict:
    """删除全局记忆：优先按 mid（新前端用）；兼容旧的 index / content_substr。"""
    try:
        if mid:
            ok = _gmem.delete(mid)
            if ok:
                return {"ok": True, "removed_id": mid}
            return {"ok": False, "reason": f"id {mid} 未找到"}
        # 旧兼容路径：先拉全量，再按 index / content_substr 定位 id 删除
        entries = _gmem.list_all(limit=10**9)
        if index is not None:
            if index < 0 or index >= len(entries):
                return {"ok": False, "reason": f"index {index} 越界（共 {len(entries)} 条）"}
            mid = entries[index].get("id")
            _gmem.delete(mid)
            return {"ok": True, "removed_index": index, "removed_id": mid}
        if content_substr:
            sub = content_substr.lower()
            for e in entries:
                if sub in str(e.get("content", "")).lower():
                    _gmem.delete(e["id"])
                    return {"ok": True, "removed_id": e["id"]}
            return {"ok": False, "reason": "未找到匹配的记忆"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}
    return {"ok": False, "reason": "需提供 mid / index / content_substr"}


def memory_clear_all() -> dict:
    """清空全部全局记忆。"""
    try:
        n = _gmem.clear_all()
        return {"ok": True, "count": n}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}


# =========================================================================
# find_skill — 在 SkillHub 技能市场检索可用技能
# =========================================================================
_SKILLHUB_API = "https://lightmake.site/api/skills"


async def _find_skill(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "错误：缺少 query（检索关键词）"
    page = max(1, int(args.get("page") or 1))
    page_size = min(30, max(1, int(args.get("page_size") or 10)))
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                _SKILLHUB_API,
                params={"keyword": query, "page": page, "pageSize": page_size},
                headers={"User-Agent": "Mozilla/5.0 MY_AGENT/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return f"技能市场检索失败: {exc}"

    if not isinstance(data, dict) or data.get("code") != 0:
        return f"技能市场返回异常: {data.get('msg') or data}"
    skills = (data.get("data") or {}).get("skills") or []
    total = (data.get("data") or {}).get("total") or 0
    if not skills:
        return f"未找到与「{query}」相关的技能（市场共 {total} 个技能）。"

    lines: list[str] = [f"在技能市场找到 {len(skills)} 个相关技能（共 {total} 个）：", ""]
    for i, s in enumerate(skills, 1):
        name = s.get("name") or s.get("slug") or "?"
        slug = s.get("slug") or ""
        desc = (s.get("description_zh") or s.get("description") or "").strip()
        cat = s.get("category") or ""
        installs = s.get("installs") or 0
        src = s.get("source") or ""
        home = s.get("homepage") or ""
        lines.append(f"{i}. {name}  (slug: {slug})")
        if desc:
            lines.append(f"   描述: {desc}")
        meta = []
        if cat:
            meta.append(f"分类:{cat}")
        if installs:
            meta.append(f"安装:{installs}")
        if src:
            meta.append(f"来源:{src}")
        if meta:
            lines.append(f"   {' | '.join(meta)}")
        if home:
            lines.append(f"   主页: {home}")
        lines.append("")
    return _cap("\n".join(lines))


register_tool(
    "find_skill",
    "在 SkillHub 技能市场按关键词检索可用的 AI 技能，返回技能名称、简介、分类、安装量与主页。用于发现并安装新能力。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词，例如『pdf』『翻译』『爬虫』"},
            "page": {"type": "integer", "description": "页码（默认 1）"},
            "page_size": {"type": "integer", "description": "每页数量（默认 10，最大 30）"},
        },
        "required": ["query"],
    },
    _find_skill,
    min_permission="low",  # ★ 只读工具：低权限档可见
    kind="skill",  # ★ 标记为技能型工具，前端以技能卡片突出显示
)


# =========================================================================
# list_skills / use_skill — 运行时加载并执行已安装的技能（Hermes 风格）
# =========================================================================
# 已安装技能存于「用户数据目录」（由 agents.skills_io 解析 MY_AGENT_SKILLS_DIR），
# 更新 exe 不丢、无需重新打包即可使用。use_skill 只回传说明文本，真正执行由模型
# 按说明用现有 shell 工具完成（受保护目录硬拦截），不自动执行任何脚本。


async def _list_skills(args: dict) -> str:
    installed = _skills_io.read_installed()
    if not installed:
        return "当前未安装任何技能。可在「技能市场」标签页点安装，或让我用 find_skill 检索合适技能后安装。"
    lines = [f"已安装 {len(installed)} 个技能：", ""]
    for i, (slug, meta) in enumerate(installed.items(), 1):
        name = meta.get("name") or slug
        desc = (meta.get("description") or "").strip()
        real = "✅ 真实技能包（含完整说明）" if meta.get("real") else "⚠️ 元数据存根（未拿到正文，可能需重装）"
        cat = meta.get("category") or ""
        lines.append(f"{i}. {name}  (slug: {slug})")
        if cat:
            lines.append(f"   分类: {cat}")
        lines.append(f"   类型: {real}")
        if desc:
            d = desc if len(desc) <= 120 else desc[:120] + "…"
            lines.append(f"   简介: {d}")
        lines.append("")
    lines.append("使用某技能：对我说「用 <slug> 技能」或直接调用 use_skill(slug)。")
    return _cap("\n".join(lines))


async def _use_skill(args: dict) -> str:
    slug = (args.get("slug") or "").strip()
    if not slug:
        return "错误：缺少 slug（技能标识）。请先调用 list_skills 查看已装技能。"
    skill_dir, markdown, extra_files = _skills_io.load_skill(slug)
    if not skill_dir.exists():
        return (
            f"未找到技能「{slug}」。请先在「技能市场」安装（或让我用 find_skill 检索后安装）。\n"
            f"已安装列表见 list_skills。"
        )
    if not markdown:
        return (
            f"技能「{slug}」已安装，但 SKILL.md 为空或缺失（可能是元数据存根）。\n"
            f"技能目录: {skill_dir}\n"
            f"建议在技能市场重新安装以获取完整技能说明，或到其 homepage 查看。"
        )
    head = [
        f"# 技能已加载：{slug}",
        f"技能目录(绝对路径): {skill_dir}",
    ]
    if extra_files:
        head.append("附带文件(可用现有 shell 工具执行): " + ", ".join(extra_files))
    head.append("")
    head.append("下面是该技能的完整说明，请严格按其中的步骤执行：")
    return _cap("\n".join(head) + "\n\n" + markdown)


register_tool(
    "list_skills",
    "列出当前已安装的技能（来自技能市场）。返回每个技能的名称、slug、分类、是否真实技能包与简介。用于在调用 use_skill 前确认可用技能。",
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
    _list_skills,
    min_permission="low",  # ★ 只读工具：低权限档可见
    kind="skill",  # ★ 技能型工具，前端以技能卡片突出显示
)

register_tool(
    "use_skill",
    "加载并执行一个已安装的技能：读取其 SKILL.md 全文与技能目录绝对路径回灌给你，并列出附带文件，由你按说明用 shell 等现有工具执行。技能需先在技能市场安装。注意：本工具只读取并返回说明，不自动执行任何脚本。",
    {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "技能标识，例如『pdf』『translate』，见 list_skills"},
        },
        "required": ["slug"],
    },
    _use_skill,
    kind="skill",  # ★ 技能型工具，前端以技能卡片突出显示
)


# =========================================================================
# list_software — 列出本机已安装软件（注册表 + winget）
# =========================================================================
_UNINSTALL_ROOTS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]


def _reg_get(key, name: str) -> str:
    try:
        import winreg

        val, _ = winreg.QueryValueEx(key, name)
        return str(val)
    except Exception:  # noqa: BLE001
        return ""


async def _list_software(args: dict) -> str:
    try:
        import winreg
    except ImportError:
        return "错误：本机不是 Windows，无法读取已安装软件注册表。"
    items: list[tuple[str, str, str]] = []
    for hive_name, sub in _UNINSTALL_ROOTS:
        hive = getattr(winreg, hive_name)
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
        except OSError:
            continue
        for i in range(count):
            try:
                sk = winreg.OpenKey(key, winreg.EnumKey(key, i))
                dn = _reg_get(sk, "DisplayName")
                if not dn:
                    continue
                ver = _reg_get(sk, "DisplayVersion")
                pub = _reg_get(sk, "Publisher")
                items.append((dn, ver, pub))
            except OSError:
                continue
    if not items:
        return "未找到已安装软件（或当前系统不支持）。"
    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    for name, ver, pub in sorted(items, key=lambda x: x[0].lower()):
        k = (name, ver)
        if k in seen:
            continue
        seen.add(k)
        line = f"- {name}"
        if ver:
            line += f"  (v{ver})"
        if pub:
            line += f"  [{pub}]"
        out.append(line)
    return _cap("\n".join(out))


register_tool(
    "list_software",
    "列出本机已安装的软件（来自 Windows 注册表），返回 软件名/版本/发布者。用于卸载前确认目标。",
    {
        "type": "object",
        "properties": {
            "unused": {"type": "string", "description": "无需参数"}
        },
    },
    _list_software,
)


# =========================================================================
# uninstall_software — 卸载软件（高危，需前端确认）
# =========================================================================
def _parse_winget_id(text: str, name: str) -> str | None:
    """从 `winget list` 输出里解析最匹配的 Id（形如 Publisher.App）。"""
    name_l = name.lower()
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if set(s) <= set("-= "):
            continue
        if "名称" in s or ("Name" in s and "Id" in s):  # 表头
            continue
        if name_l not in ln.lower():
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        for p in parts:
            # winget id 形如 Publisher.App：含点且含字母；
            # 排除纯数字版本号（如 23.01）与非 id 的含点词（如 7-Zip 用连字符）
            if "." in p and any(c.isalpha() for c in p):
                return p
        return parts[1]  # 退而求其次取第 2 列
    return None


async def _uninstall_via_registry(name: str, extra: str = "") -> str:
    """注册表回落：按 DisplayName 模糊匹配，执行其 UninstallString。"""
    try:
        import winreg
    except ImportError:
        return (extra + "\n且本机不支持注册表卸载（非 Windows）。").strip()
    found: str | None = None
    for hive_name, sub in _UNINSTALL_ROOTS:
        hive = getattr(winreg, hive_name)
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
        except OSError:
            continue
        for i in range(count):
            try:
                sk = winreg.OpenKey(key, winreg.EnumKey(key, i))
                dn = _reg_get(sk, "DisplayName")
                if dn and name.lower() in dn.lower():
                    found = _reg_get(sk, "UninstallString")
                    break
            except OSError:
                continue
        if found:
            break
    if not found:
        return (extra + f"\n注册表中也未找到包含「{name}」的卸载项。").strip()
    cmd = found
    low = cmd.lower()
    if "msiexec" in low:
        if "/qn" not in low and "/quiet" not in low:
            cmd += " /qn"
    elif "/s" not in low and "/silent" not in low and "/verysilent" not in low:
        cmd += " /S"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
        return _cap((extra + f"\n已用注册表卸载字符串卸载 {name}：\n{_decode_output(out)}").strip())
    except Exception as exc:  # noqa: BLE001
        return (extra + f"\n执行注册表卸载字符串失败: {exc}").strip()


async def _uninstall_software(args: dict) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return "错误：缺少 name（要卸载的软件名）"
    try:
        # 1) 先用 winget list 解析精确 id
        list_proc = await asyncio.create_subprocess_exec(
            "winget", "list", "--name", name, "--accept-source-agreements",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        lout, _ = await asyncio.wait_for(list_proc.communicate(), timeout=60)
        list_text = _decode_output(lout)
        wid = _parse_winget_id(list_text, name)
        if wid:
            uproc = await asyncio.create_subprocess_exec(
                "winget", "uninstall", "--id", wid, "--silent",
                "--accept-source-agreements", "--disable-interactivity",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            uout, _ = await asyncio.wait_for(uproc.communicate(), timeout=240)
            if uproc.returncode == 0:
                return _cap(f"已卸载 {name}（winget id: {wid}）：\n{_decode_output(uout)}")
            return await _uninstall_via_registry(
                name, extra=f"winget 卸载返回码 {uproc.returncode}：\n{_decode_output(uout)}"
            )
        # 2) 没解析出 id，直接尝试 winget uninstall --name
        uproc = await asyncio.create_subprocess_exec(
            "winget", "uninstall", "--name", name, "--silent",
            "--accept-source-agreements", "--disable-interactivity",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        uout, _ = await asyncio.wait_for(uproc.communicate(), timeout=240)
        if uproc.returncode == 0:
            return _cap(f"已卸载 {name}（winget）：\n{_decode_output(uout)}")
        return await _uninstall_via_registry(
            name, extra=f"winget 未找到可卸载项：\n{_decode_output(uout)}"
        )
    except Exception as exc:  # noqa: BLE001
        return await _uninstall_via_registry(name, extra=f"winget 调用异常: {exc}")


register_tool(
    "uninstall_software",
    "卸载一个已安装的软件。优先用 winget 精确卸载，失败则回落注册表卸载字符串。⚠️ 高危操作，需用户在前端确认。",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "要卸载的软件名（可模糊，如『微信』『7-Zip』；也能直接给 winget id，如『Git.Git』）",
            }
        },
        "required": ["name"],
    },
    _uninstall_software,
    require_approval=True,  # ★ 触发前端确认闸门
    kind="skill",           # ★ 技能型工具，前端以技能卡片突出显示
    min_permission="high",  # ★ 卸载软件=接管电脑级操作，仅高权限档可见
)


# =========================================================================
# organize_downloads — 下载夹自动整理（按类型归类到子文件夹）
# 数据嵌入：把散落的下载文件规整到 Images/Videos/Documents/... 子目录。
# 默认只整理「下载」文件夹；也可指定任意文件夹。仅移动、不删除。
# =========================================================================
_ORG_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Images": ("jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "svg", "tiff", "ico", "avif"),
    "Videos": ("mp4", "mov", "avi", "mkv", "webm", "flv", "wmv", "m4v", "mpg", "mpeg"),
    "Audio": ("mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus"),
    "Documents": (
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv",
        "rtf", "odt", "ods", "odp", "epub", "mobi", "azw3", "pages", "key", "numbers",
    ),
    "Archives": ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz", "iso", "zst"),
    "Code": (
        "py", "js", "ts", "tsx", "jsx", "cpp", "c", "h", "hpp", "java", "go", "rs",
        "rb", "php", "html", "css", "scss", "json", "yaml", "yml", "sh", "bat", "ps1",
        "ipynb", "sql", "xml", "toml", "lua", "kt", "swift",
    ),
    "Installers": ("exe", "msi", "appx", "appxbundle", "dmg", "pkg", "deb", "rpm"),
}
# 整理时要跳过的临时/进行中文件（下载到一半、浏览器临时锁文件等）
_ORG_SKIP_EXT = {".tmp", ".part", ".crdownload", ".downloading"}
_ORG_RESERVED = set(_ORG_CATEGORIES.keys())  # 已经是我们建的子目录，不要二次移动


def _org_default_folder() -> str:
    try:
        return os.path.join(os.path.expanduser("~"), "Downloads")
    except Exception:  # noqa: BLE001
        return ""


def _org_category(name: str) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if not ext:
        return None
    for cat, exts in _ORG_CATEGORIES.items():
        if ext in exts:
            return cat
    return "Misc"


def _organize_scan(folder: str) -> dict[str, list[str]]:
    """扫描顶层文件，按类别归集（不进入已有子目录）。返回 {类别: [文件名]}。"""
    plan: dict[str, list[str]] = {}
    try:
        entries = os.listdir(folder)
    except Exception:  # noqa: BLE001
        return plan
    for name in entries:
        full = os.path.join(folder, name)
        if os.path.isdir(full):
            continue  # 跳过目录（含已建的分类子目录）
        if name.startswith("."):
            continue  # 跳过隐藏文件
        ext = os.path.splitext(name)[-1].lower()
        if ext in _ORG_SKIP_EXT:
            continue  # 跳过下载中的临时文件
        cat = _org_category(name)
        plan.setdefault(cat, []).append(name)
    return plan


async def _organize_downloads(args: dict) -> str:
    import shutil  # 仅本函数用到，局部导入避免顶层污染

    mode = (args.get("mode") or "preview").lower()
    folder = (args.get("folder") or "").strip()
    if not folder:
        folder = _org_default_folder()
    if not folder or not os.path.isdir(folder):
        return f"错误：文件夹不存在或无法访问：{folder or '(空)'}"

    # 安全闸门：禁止整理受保护目录（系统目录/项目自身）
    if _is_protected(folder):
        return f"已阻止：{folder} 属于受保护目录，出于安全不自动整理。"

    plan = _organize_scan(folder)
    total = sum(len(v) for v in plan.values())
    if total == 0:
        return f"文件夹已是整齐状态（无顶层散落文件可归类）：{folder}"

    if mode == "preview":
        lines = [f"【整理预览】{folder}", f"共 {total} 个文件将被归类到以下子目录："]
        for cat, names in sorted(plan.items(), key=lambda x: -len(x[1])):
            lines.append(f"\n📁 {cat}/  ({len(names)} 个)")
            for n in names[:8]:
                lines.append(f"   - {n}")
            if len(names) > 8:
                lines.append(f"   … 还有 {len(names) - 8} 个")
        lines.append(
            "\n确认无误后，以 mode=\"apply\" 再次调用本工具即可实际移动（仅移动、不删除）。"
        )
        return _cap("\n".join(lines))

    # apply：建子目录并移动（仅移动，不删除；失败逐个跳过）
    moved = 0
    failed: list[str] = []
    for cat, names in plan.items():
        target = os.path.join(folder, cat)
        try:
            os.makedirs(target, exist_ok=True)
        except Exception:  # noqa: BLE001
            failed.extend(names)
            continue
        for n in names:
            src = os.path.join(folder, n)
            dst = os.path.join(target, n)
            if os.path.exists(dst):
                failed.append(n)
                continue
            try:
                shutil.move(src, dst)
                moved += 1
            except Exception:  # noqa: BLE001
                failed.append(n)
    summary = [f"✅ 整理完成：{folder}", f"已移动 {moved} 个文件到对应子目录。"]
    if failed:
        summary.append(f"⚠️ {len(failed)} 个未能移动（同名冲突或权限不足）：" + "、".join(failed[:10]))
    return _cap("\n".join(summary))


register_tool(
    "organize_downloads",
    "整理下载文件夹（或指定文件夹）：按文件类型（图片/视频/音频/文档/压缩包/代码/安装包）归类到子目录。"
    "mode=preview 仅生成整理方案不移动；mode=apply 才实际移动文件（仅移动、不删除）。默认整理『下载』文件夹。",
    {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "description": "preview=只生成方案不移动（默认）；apply=实际移动文件到子目录",
                "enum": ["preview", "apply"],
            },
            "folder": {
                "type": "string",
                "description": "要整理的文件夹路径；留空则默认『下载』文件夹",
            },
        },
    },
    _organize_downloads,
    require_approval=True,  # ★ 移动用户文件，需前端确认
    kind="skill",           # ★ 技能型工具，前端以技能卡片突出显示
)


# =========================================================================
# desktop_control — 桌面/GUI 自动化（截图·鼠标·键盘·窗口）
# 补齐「完整流程控制电脑」的 GUI 层：无命令行的程序也能操作。
# 依赖 pyautogui（鼠标/键盘/截图）+ pygetwindow（窗口）；OCR 走 pytesseract（可选）。
# =========================================================================
def _dt_box(args: dict):
    region = args.get("region")
    if isinstance(region, (list, tuple)) and len(region) == 4:
        try:
            return tuple(int(x) for x in region)
        except (TypeError, ValueError):
            return None
    return None


def _desktop_ocr(path: str, lang: str | None = None) -> str:
    """对图片做 OCR 识别文字（最佳努力）。需要 Tesseract OCR 二进制。"""
    try:
        import pytesseract
        from PIL import Image
        from pytesseract import TesseractError
    except Exception as exc:  # noqa: BLE001
        return f"（OCR 不可用：未安装 pytesseract；需先 `pip install pytesseract`）"
    try:
        lang = lang or "chi_sim+eng"
        return pytesseract.image_to_string(Image.open(path), lang=lang).strip()
    except TesseractError:
        # 中文训练数据可能没装，回落英文
        try:
            return pytesseract.image_to_string(Image.open(path), lang="eng").strip()
        except Exception:  # noqa: BLE001
            return "（OCR 失败：未安装 Tesseract OCR 二进制，或缺少中文训练数据）"
    except Exception as exc:  # noqa: BLE001
        return f"（OCR 失败：{exc}）"


async def _desktop_screenshot(args: dict) -> str:
    import pyautogui
    import tempfile
    import time

    from pathlib import Path

    box = _dt_box(args)
    img = pyautogui.screenshot(region=box)
    save_path = args.get("save_path") or ""
    if save_path:
        p = Path(save_path)
    else:
        p = Path(tempfile.gettempdir()) / f"myagent_desktop_{int(time.time() * 1000)}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(p))
    w, h = img.size
    out = [
        f"已截图，保存至：{p}",
        f"尺寸：{w}x{h}" + (f"（区域 {box}）" if box else "（全屏）"),
    ]
    if args.get("vision"):
        # 多模态视觉理解（交给 supports_vision 的 provider，如 MiniMax M3），与 ocr 互斥
        desc = await _describe_vision(str(p))
        out.append("屏幕视觉描述（多模态模型）：\n" + (desc or "（未返回内容）"))
    elif args.get("ocr"):
        txt = _desktop_ocr(str(p), args.get("lang"))
        out.append("屏幕文字（OCR）：\n" + (txt or "（未识别到文字）"))
    return _cap("\n".join(out))


async def _describe_vision(path: str) -> str:
    """用多模态模型把截图转成文字描述（视觉后端未配置时返回说明）。"""
    from agents import vision as _vis

    return await _vis.describe_image(path)


def _desktop_mouse(args: dict) -> str:
    import pyautogui

    pyautogui.FAILSAFE = True  # 把鼠标甩到屏幕左上角可紧急中断
    op = (args.get("op") or "click").lower()
    x = args.get("x")
    y = args.get("y")
    button = (args.get("button") or "left").lower()
    dur = args.get("duration", 0.2)
    if op == "move":
        if x is None or y is None:
            return "错误：move 需要 x,y"
        pyautogui.moveTo(x, y, duration=dur)
        return f"已移动鼠标到 ({x},{y})"
    if op in ("click", "double", "right"):
        if x is not None and y is not None:
            pyautogui.moveTo(x, y, duration=dur)
        clicks = 2 if op == "double" else 1
        pyautogui.click(button=button, clicks=clicks)
        where = f"({x},{y}) " if x is not None and y is not None else ""
        return f"已在 {where}执行 {op} 点击（按钮 {button}）"
    if op == "down":
        pyautogui.mouseDown(button=button)
        return f"鼠标已按下（{button}）"
    if op == "up":
        pyautogui.mouseUp(button=button)
        return f"鼠标已抬起（{button}）"
    if op == "drag":
        dx = args.get("dx")
        dy = args.get("dy")
        if dx is None or dy is None:
            return "错误：drag 需要 dx,dy"
        pyautogui.drag(dx, dy, duration=dur, button=button)
        return f"已拖拽相对位移 ({dx},{dy})"
    if op == "scroll":
        amt = int(args.get("scroll_amount") or 0)
        if x is not None and y is not None:
            pyautogui.moveTo(x, y)
        pyautogui.scroll(amt, x=x, y=y)
        return f"已滚动 {amt}（正=上，负=下）"
    return f"未知鼠标操作：{op}（支持 move/click/double/right/down/up/drag/scroll）"


def _desktop_keyboard(args: dict) -> str:
    import pyautogui

    pyautogui.FAILSAFE = True
    op = (args.get("op") or "type").lower()
    if op == "type":
        text = args.get("text")
        if not text:
            return "错误：type 需要 text"
        pyautogui.write(text, interval=args.get("interval", 0.02))
        preview = text if len(text) <= 60 else text[:60] + "…"
        return f"已输入文本：{preview}"
    if op in ("press", "hotkey"):
        keys = args.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            return "错误：press/hotkey 需要 keys（如 ['ctrl','c']）"
        if op == "press":
            for k in keys:
                pyautogui.press(k)
            return f"已按下：{','.join(keys)}"
        pyautogui.hotkey(*keys)
        return f"已执行快捷键：{'+'.join(keys)}"
    return f"未知键盘操作：{op}（支持 type/press/hotkey）"


def _desktop_window(args: dict) -> str:
    try:
        import pygetwindow as gw
    except ImportError:
        return "错误：缺少 pygetwindow（pyautogui 依赖），无法操作窗口"
    op = (args.get("op") or "list").lower()
    title = args.get("title") or ""
    try:
        if op == "list":
            wins = gw.getAllWindows()
            items = [
                f"- {w.title}  [{int(w.left)},{int(w.top)} {int(w.width)}x{int(w.height)}]"
                for w in wins
                if getattr(w, "title", "")
            ]
            return _cap("可见窗口：\n" + ("\n".join(items) if items else "（无标题窗口）"))
        if op == "active":
            w = gw.getActiveWindow()
            return f"当前活动窗口：{getattr(w, 'title', '') or '(无)'}" if w else "（无活动窗口）"
        if op in ("focus", "minimize", "maximize", "restore", "close"):
            if not title:
                return "错误：该操作需要 title（窗口标题，模糊匹配）"
            matches = [w for w in gw.getAllWindows() if title.lower() in (w.title or "").lower()]
            if not matches:
                return f"未找到包含「{title}」的窗口"
            w = matches[0]
            if op == "focus":
                w.activate(); return f"已聚焦：{w.title}"
            if op == "minimize":
                w.minimize(); return f"已最小化：{w.title}"
            if op == "maximize":
                w.maximize(); return f"已最大化：{w.title}"
            if op == "restore":
                w.restore(); return f"已还原：{w.title}"
            if op == "close":
                w.close(); return f"已关闭窗口：{w.title}（程序可能仍在后台运行）"
    except Exception as exc:  # noqa: BLE001
        return f"窗口操作失败：{exc}"
    return f"未知窗口操作：{op}（支持 list/active/focus/minimize/maximize/restore/close）"


async def _desktop_control(args: dict) -> str:
    action = (args.get("action") or "").lower()
    try:
        if action == "screenshot":
            return await _desktop_screenshot(args)
        if action == "ocr":
            # 截图并识别文字的便捷入口
            return await _desktop_screenshot({**args, "ocr": True})
        if action == "vision":
            # 截图并用多模态模型理解画面（看图）
            return await _desktop_screenshot({**args, "vision": True})
        if action == "mouse":
            return _desktop_mouse(args)
        if action == "keyboard":
            return _desktop_keyboard(args)
        if action == "window":
            return _desktop_window(args)
    except Exception as exc:  # noqa: BLE001
        return f"desktop_control 执行失败：{exc}（本机需有图形界面；无显示器/远程会话可能失败）"
    return "错误：缺少或未知 action（screenshot/ocr/vision/mouse/keyboard/window）"


register_tool(
    "desktop_control",
    "桌面/GUI 自动化：截图（可选 OCR 识别文字，或 vision 用多模态模型直接看懂画面）、控制鼠标（移动/点击/双击/拖拽/滚动）、"
    "键盘（输入文本/按键/快捷键）、窗口（列出/聚焦/最小化/最大化/关闭）。"
    "用于操作没有命令行界面的图形程序，实现完整的电脑控制。",
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "子操作：screenshot(截图) / ocr(截图并OCR识别文字) / vision(截图并用多模态模型理解画面) / mouse(鼠标) / keyboard(键盘) / window(窗口)",
            },
            "region": {
                "type": "array",
                "description": "截图区域 [x,y,w,h]，省略则截全屏",
                "items": {"type": "number"},
            },
            "save_path": {"type": "string", "description": "截图保存路径，省略则存到系统临时目录"},
            "ocr": {"type": "boolean", "description": "screenshot 时是否附带 OCR 文字识别"},
            "vision": {"type": "boolean", "description": "screenshot 时是否用多模态模型（如 MiniMax M3）直接看懂截图；与 ocr 互斥，vision 优先"},
            "lang": {"type": "string", "description": "OCR 语言，默认 chi_sim+eng（需装对应训练数据）"},
            "op": {
                "type": "string",
                "description": "mouse: move/click/double/right/down/up/drag/scroll；"
                               "keyboard: type/press/hotkey；window: list/active/focus/minimize/maximize/restore/close",
            },
            "x": {"type": "number", "description": "鼠标 x 坐标"},
            "y": {"type": "number", "description": "鼠标 y 坐标"},
            "dx": {"type": "number", "description": "drag 相对位移 x"},
            "dy": {"type": "number", "description": "drag 相对位移 y"},
            "button": {"type": "string", "description": "鼠标按键 left/right/middle"},
            "duration": {"type": "number", "description": "移动/拖拽动画时长（秒）"},
            "scroll_amount": {"type": "integer", "description": "滚动量，正=上滚，负=下滚"},
            "text": {"type": "string", "description": "keyboard type 要输入的文本"},
            "keys": {
                "type": "array",
                "description": "press/hotkey 的按键列表，如 ['ctrl','c']",
                "items": {"type": "string"},
            },
            "title": {"type": "string", "description": "window 操作的窗口标题（模糊匹配）"},
        },
        "required": ["action"],
    },
    _desktop_control,
    kind="tool",  # 内置控制工具，前端以普通工具卡显示
    min_permission="high",  # ★ 桌面自动化=接管电脑级操作，仅高权限档可见
)


# -------------------------------------------------------------------------
# 视觉 skill — 截图 + 多模态模型看懂画面（主眼睛 + 备用眼睛，共用 fallback 链）
# 由 desktop_control(vision) 内部复用同一逻辑；此处独立成 skill 让模型可主动调用。
# -------------------------------------------------------------------------
async def _vision_see(args: dict) -> str:
    """截图并用多模态模型看懂画面（需自行配置 supports_vision 的接口）。"""
    from agents import vision as _vis

    # 复用 desktop_control 的截图+vision 逻辑
    out = await _desktop_screenshot({**args, "vision": True})
    if not _vis.is_vision_ready():
        return out + "\n（视觉后端未启用）"
    return out


register_tool(
    "vision_see",
    "用多模态模型「看」屏幕：先截图，再交给视觉后端（需自行配置 supports_vision 的接口）"
    "把画面转成中文文字描述回灌给你。当你需要理解屏幕上的窗口、按钮、文字、弹窗或图形界面时用它。"
    "主模型（纯文本大脑）通过此技能获得「视觉」。",
    {
        "type": "object",
        "properties": {
            "region": {
                "type": "array",
                "description": "截图区域 [x,y,w,h]，省略则截全屏",
                "items": {"type": "number"},
            },
            "save_path": {"type": "string", "description": "截图保存路径，省略则存到系统临时目录"},
            "prompt": {"type": "string", "description": "可选的自定义观察指令（如「只看右上角的弹窗」）"},
        },
        "required": [],
    },
    _vision_see,
    kind="skill",  # ★ 技能型工具，前端以紫色技能卡突出显示
)


# -------------------------------------------------------------------------
# task_board — 任务看板（技能型）
# -------------------------------------------------------------------------
def _board_snapshot(sid: str, prefix: str) -> str:
    """返回看板当前快照文本：序号 + 状态 + 标题 + ID（供模型寻址与自愈）。"""
    from agents import board

    tasks = board.list_tasks(sid)
    if not tasks:
        return "当前任务清单为空。"
    lines = []
    for i, t in enumerate(tasks):
        tid = t.get("id", "?")
        note = f" — {t['note']}" if t.get("note") else ""
        lines.append(f"{i + 1}. [{t.get('status', 'pending')}] {t['title']} (id={tid}){note}")
    return prefix + "\n" + "\n".join(lines)


async def _task_board(args: dict) -> str:
    """把复杂多步需求拆成子任务清单并跟踪进度。模型在处理复杂需求时应主动调用。"""
    from agents import board

    ctx = current_workspace_ctx()
    sid = (ctx or {}).get("session_id") if ctx else None
    if not sid:
        return "错误：无法获取当前会话 ID，任务看板需要绑定会话上下文。"
    action = (args.get("action") or "list").lower()

    if action == "add":
        tasks = args.get("tasks") or []
        if isinstance(tasks, str):
            tasks = [tasks]
        added = board.add(sid, tasks)
        if added:
            await board.push_update(sid)
            titles = "、".join(t["title"] for t in added)
            return f"已将 {len(added)} 个子任务注入任务清单：{titles}"
        return "没有有效的子任务可注入（tasks 需为非空字符串数组）。"

    if action == "update":
        task_id = args.get("task_id") or args.get("id") or args.get("title")
        if not task_id:
            return "错误：action=update 需要提供 task_id（可用 list 看到的序号/标题/ID）。"
        ok, matched = board.update(
            sid,
            task_id,
            status=args.get("status"),
            note=args.get("note"),
            title=args.get("title"),
        )
        if ok:
            await board.push_update(sid)
            t = matched[0]
            return f"已更新任务「{t['title']}」→ {t.get('status', 'pending')}"
        # 未命中：把当前快照回给模型，便于按正确引用重试（自愈）
        return _board_snapshot(sid, f"未找到任务（引用：{task_id}），当前任务清单如下：")

    if action == "remove":
        task_id = args.get("task_id") or args.get("id") or args.get("title")
        if not task_id:
            return "错误：action=remove 需要提供 task_id。"
        ok, matched = board.remove(sid, task_id)
        if ok:
            await board.push_update(sid)
            return f"已移除任务「{matched[0]['title']}」。"
        return _board_snapshot(sid, f"未找到任务（引用：{task_id}），当前任务清单如下：")

    if action == "clear":
        board.clear(sid)
        await board.push_update(sid)
        return "已清空任务清单。"

    # list（默认）
    return _board_snapshot(sid, "当前任务清单：")


register_tool(
    "task_board",
    "任务看板：把复杂多步需求拆成子任务清单并跟踪进度。当你收到一个复杂/多步的用户需求时，"
    "先用 action=add 把子任务批量注入任务清单（tasks 为字符串数组，如 ['步骤1…','步骤2…']），"
    "完成某步用 action=update 把对应任务标记为 doing/done（task_id 可用 list 看到的序号 1/2/3、"
    "标题、或 id 任意一种寻址），移除用 action=remove，清空用 action=clear，查看用 action=list。"
    "任务清单会实时显示在前端输入框上方。",
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "update", "remove", "clear", "list"],
                "description": "操作类型",
            },
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "action=add 时，要注入的子任务标题列表",
            },
            "task_id": {
                "type": "string",
                "description": "action=update/remove 时的任务引用：支持序号（1 起，list 可看到）、标题或真实 ID",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "doing", "done"],
                "description": "action=update 时的新状态",
            },
            "note": {"type": "string", "description": "action=update 时的备注（可选）"},
        },
        "required": ["action"],
    },
    _task_board,
    kind="skill",
)


# -------------------------------------------------------------------------
# schedule_task — 创建定时任务（技能型）
# -------------------------------------------------------------------------
async def _schedule_task(args: dict) -> str:
    """创建定时任务：让助手在未来自动执行某段提示词。"""
    from agents import scheduler as _sched

    name = args.get("name") or args.get("title") or "定时任务"
    prompt = args.get("prompt") or args.get("task") or ""
    when = args.get("when") or ""
    if not prompt or not when:
        return (
            "错误：schedule_task 需要提供 prompt（要执行的任务）与 when（触发规则）。"
            "when 支持：'in 30m'（30 分钟后一次性）、'every 1h'（每小时）、"
            "'daily 09:00'（每天 9 点）、'weekly mon 09:00'（每周一 9 点）、"
            "'cron 0 9 * * *'（标准 cron）。"
        )
    try:
        job = _sched.get_scheduler().add_job(
            {
                "name": name,
                "prompt": prompt,
                "when": when,
                "provider": args.get("provider"),
                "model": args.get("model"),
            }
        )
    except ValueError as exc:
        return f"错误：{exc}"
    return (
        f"已创建定时任务「{job.name}」（ID {job.id}），触发规则：{job.when}，"
        f"下次执行：{job.next_run or '—'}。可在右侧「定时任务」面板查看与管理。"
    )


register_tool(
    "schedule_task",
    "创建定时任务：让助手在未来的指定时间自动执行某段提示词（prompt）。"
    "when 支持：'in 30m'（30 分钟后一次性）、'every 1h'（每小时）、"
    "'daily 09:00'（每天 9 点）、'weekly mon 09:00'（每周一 9 点）、"
    "'cron 0 9 * * *'（标准 cron）。可选项 provider/model 指定执行用的模型。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "任务名称"},
            "prompt": {"type": "string", "description": "到点要自动执行的任务提示词"},
            "when": {
                "type": "string",
                "description": "触发规则，如 'daily 09:00' / 'every 1h' / 'in 30m' / 'weekly mon 09:00' / 'cron 0 9 * * *'",
            },
            "provider": {
                "type": "string",
                "description": "可选：指定 provider（默认用全局默认）",
            },
            "model": {"type": "string", "description": "可选：指定模型"},
        },
        "required": ["prompt", "when"],
    },
    _schedule_task,
    kind="skill",
)


# =========================================================================
# 对外接口
# =========================================================================
def get_tool_kind(name: str) -> str:
    """返回工具类型：'tool' 或 'skill'。未知工具回落 'tool'。"""
    return TOOLS.get(name, {}).get("kind", "tool")


def get_tool_schemas(
    allowed: list[str] | None = None,
    permission_level: str | None = None,
) -> list[dict[str, Any]]:
    """把注册表转成 OpenAI tools 格式。

    allowed 语义：
      - None        → 返回全部工具 schema（云端默认）。
      - 非空列表     → 仅返回白名单内的工具（模型看不到其余工具）。
      - 空列表 []    → 返回空列表，即不挂载任何工具（本地纯聊天模式）。
    注意区分 None 与 []：空列表是「零工具」而非「全部」。

    permission_level（权限档位，2026-08-07 新增）：
      - None/medium → 不额外过滤（默认）
      - "low"       → 只暴露 min_permission=low 的只读工具（纯聊天/只读模式）
      - "high"      → 全部工具（含 min_permission=high 的接管电脑类）
      档位由用户在前端滑动条设置；模型看不到被过滤掉的工具。
    """
    if allowed is None:
        names = None
    else:
        names = set(allowed)
    level = (permission_level or "medium").lower()
    # 档位层级：low < medium < high
    level_order = {"low": 0, "medium": 1, "high": 2}
    need = level_order.get(level, 1)
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOLS.values()
        if (names is None or t["name"] in names)
        and level_order.get(t.get("min_permission", "medium"), 1) <= need
    ]


def permission_level_of_tool(name: str) -> str:
    """返回工具所需的最低权限档位（默认 medium）。"""
    return TOOLS.get(name, {}).get("min_permission", "medium")


async def execute_tool(name: str, args: dict, ctx: dict | None = None) -> str:
    """执行工具，返回回灌模型的文本结果。

    ctx 为工作区上下文（聊天会话为 None）。通过 contextvars 注入，使同一请求内
    的多个工具调用共享上下文，且不同并发请求互不干扰。
    """
    tool = TOOLS.get(name)
    if not tool:
        return f"错误：未知工具 {name}"
    token = _WS_CTX.set(ctx) if ctx is not None else None
    try:
        return await tool["handler"](args or {})
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool %s failed", name)
        return f"工具 {name} 执行异常: {exc}"
    finally:
        if token is not None:
            _WS_CTX.reset(token)


# -------------------------------------------------------------------------
# email_send — 一步发送邮件（P0-1：免两步令牌，正文走文件防中文/撇号失配）
# -------------------------------------------------------------------------
async def _email_send(args: dict) -> str:
    """通过邮箱一步发送邮件（自动完成两阶段确认，正文 UTF-8 文件传递）。

    注意：这是 agent 邮箱（<你的 agent 邮箱>）直发，收件人必须是
    真实外部邮箱；给自己发信会触发邮件网关的自答过滤，不会造成死循环。
    """
    to = args.get("to") or []
    if isinstance(to, str):
        to = [to]
    to = [str(x).strip() for x in to if str(x).strip()]
    if not to:
        return "错误：缺少收件人（to 至少一个邮箱地址）"
    subject = str(args.get("subject") or "")
    body = str(args.get("body") or "")
    if not body:
        return "错误：缺少邮件正文（body）"
    try:
        from agents.email_gateway import AgentlyCLI, resolve_cli

        resolved = resolve_cli()
        if not resolved:
            return "错误：未找到 agently-cli（邮件发送不可用），请先 npm install -g @tencent-qqmail/agently-cli 并完成授权。"
        node, entry = resolved
        cli = AgentlyCLI(node, entry)
        ok, err = await cli.send(
            to=to,
            subject=subject,
            body=body,
            cc=args.get("cc") or None,
            bcc=args.get("bcc") or None,
            attachments=args.get("attachments") or None,
        )
        if ok:
            return f"邮件已发送 ✓ → {', '.join(to)}" + (f"（主题：{subject}）" if subject else "")
        return f"邮件发送失败：{err}"
    except Exception as exc:  # noqa: BLE001
        return f"邮件发送异常: {exc}"


register_tool(
    "email_send",
    "一步发送邮件（自动完成授权确认，正文支持中文与特殊字符，无需手工两阶段令牌流程）。"
    "适合：任务完成后的结果汇报、通知用户、把整理好的数据/报告发给指定邮箱。"
    "参数：to（收件人，可多个）、subject（主题，可选）、body（正文，必填）、"
    "cc/bcc（可选）、attachments（附件绝对路径列表，可选）。",
    {
        "type": "object",
        "properties": {
            "to": {
                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                "description": "收件人邮箱（可传单个字符串或数组）",
            },
            "subject": {"type": "string", "description": "邮件主题（可选）"},
            "body": {"type": "string", "description": "邮件正文（必填，支持中文与特殊字符）"},
            "cc": {"type": "array", "items": {"type": "string"}, "description": "抄送（可选）"},
            "bcc": {"type": "array", "items": {"type": "string"}, "description": "密送（可选）"},
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "附件绝对路径列表（可选）",
            },
        },
        "required": ["to", "body"],
    },
    _email_send,
    kind="tool",
)


# =========================================================================
# kb_search / kb_save / kb_list — 内置知识库工具
#
# ★ 本项目自带一个空知识库：首次运行自动创建，位置在用户数据目录
#   （Windows: %APPDATA%/MY_AGENT/knowledge），与任何外部目录无关。
#   检索/保存逻辑内联在 agents/knowledge_base.py，零外部依赖、零硬编码路径。
# =========================================================================
def _kb_intro() -> str:
    root = kb_mod.resolve_kb_dir()
    return f"知识库位置：{root}"


async def _kb_search(args: dict) -> str:
    """检索内置知识库（所有 md/json/txt）。"""
    query = args.get("query") or ""
    if isinstance(query, list):
        terms = [str(q).strip() for q in query if str(q).strip()]
    else:
        terms = [t.strip() for t in str(query).replace("，", " ").replace(",", " ").split() if t.strip()]
    if not terms:
        return "错误：缺少 query（搜索关键词，可多个）"
    use_or = bool(args.get("or"))
    limit = int(args.get("limit") or 20)
    try:
        results = kb_mod.search(terms, use_or=use_or, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return f"知识库检索失败: {exc}"
    if not results:
        return (
            f"知识库无匹配（{(' / '.join(terms))}）。"
            "知识库当前可能为空——可用 kb_save 沉淀内容后再检索。"
        )
    lines = [f"🔍 知识库匹配 {len(results)} 处：\n"]
    for rel, ln, line in results:
        lines.append(f"  {rel}:{ln}  {line[:140]}")
    lines.append("\n提示：需要全文时用 kb_read 读取对应文件（路径为知识库内相对路径）。")
    return _cap("\n".join(lines))


async def _kb_save(args: dict) -> str:
    """保存一条新知识到内置知识库（追加到 <分类>/<标题>.md）。"""
    title = str(args.get("title") or "").strip()
    content = str(args.get("content") or "").strip()
    category = str(args.get("category") or "默认").strip()
    if not title or not content:
        return "错误：需要 title 与 content"
    try:
        path = kb_mod.save_knowledge(title, content, category=category)
    except ValueError as exc:
        return f"错误：{exc}，请移除或替换后再保存（可用空格、下划线、全角字符代替）。"
    except Exception as exc:  # noqa: BLE001
        return f"知识保存失败: {exc}"
    rel = path.relative_to(kb_mod.resolve_kb_dir()).as_posix()
    return f"✅ 知识已保存到知识库（{rel}）"


async def _kb_read(args: dict) -> str:
    """读取知识库内某个文件（相对路径）。"""
    rel = str(args.get("path") or "").strip()
    if not rel:
        return "错误：缺少 path（知识库内相对路径）"
    res = kb_mod.read_file(rel, max_chars=int(args.get("max_chars") or 4000))
    if not res.get("ok"):
        return f"错误：{res.get('error')}"
    tail = f"\n…[已截断，全文 {res['total_chars']} 字符]" if res.get("truncated") else ""
    return f"【{res['path']}】\n{res['content']}{tail}"


async def _kb_list(args: dict) -> str:
    """列出知识库结构与各分类文件数。"""
    tree = kb_mod.list_tree()
    if not tree["file_count"]:
        return f"知识库当前为空（{tree['root']}）。可用 kb_save 保存第一条知识。"
    lines = [f"📚 知识库（{tree['file_count']} 个文件）位于 {tree['root']}：\n"]
    for cat, n in sorted(tree["category_counts"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {cat}  ({n})")
    for f in tree["files"][:40]:
        lines.append(f"    {f['file']}")
    if tree["file_count"] > 40:
        lines.append(f"    … 共 {tree['file_count']} 个文件")
    return _cap("\n".join(lines))


register_tool(
    "kb_search",
    "检索内置知识库（随程序自带，初始为空，内容由你自己沉淀）。"
    "当需要：回忆以前整理过的资料、复用成功案例、查项目历史时，先搜这里而不是重新探索。"
    "支持多关键词（默认 AND；传 or=true 为 OR）、limit 控制条数。",
    {
        "type": "object",
        "properties": {
            "query": {
                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                "description": "搜索关键词（多个词默认 AND 匹配）",
            },
            "or": {"type": "boolean", "description": "true=多词 OR 匹配（默认 false=AND）"},
            "limit": {"type": "integer", "description": "最多返回条数（默认 20）"},
        },
        "required": ["query"],
    },
    _kb_search,
    min_permission="low",  # ★ 只读工具：低权限档可见
    kind="skill",
)

register_tool(
    "kb_save",
    "保存一条新知识到内置知识库（写入 <知识库>/<category>/<title>.md，追加模式）。"
    "适用：沉淀成功案例、项目成果、踩坑经验、整理好的数据结论，供以后检索复用。"
    "注意：保存后可再调用 memory 工具记一条索引，双保险。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "知识标题"},
            "content": {"type": "string", "description": "知识内容（可多行）"},
            "category": {"type": "string", "description": "分类目录名（默认「默认」）"},
        },
        "required": ["title", "content"],
    },
    _kb_save,
    kind="skill",
)

register_tool(
    "kb_read",
    "读取内置知识库内某个文件的全文（path 为知识库内相对路径，从 kb_search / kb_list 结果中获得）。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "知识库内相对路径，如 案例库/xxx.md"},
            "max_chars": {"type": "integer", "description": "最多返回字符数（默认 4000）"},
        },
        "required": ["path"],
    },
    _kb_read,
    min_permission="low",
    kind="skill",
)

register_tool(
    "kb_list",
    "列出内置知识库的结构与各分类文件数（用于了解库里沉淀了什么）。",
    {"type": "object", "properties": {}, "required": []},
    _kb_list,
    min_permission="low",
    kind="skill",
)


# -------------------------------------------------------------------------
# spawn_subagent —— 子智能体委派（真·多智能体）
# -------------------------------------------------------------------------
# 子智能体默认角色（全面、静态）：不随子任务变化，统一由这段 system prompt 驱动。
# 设计目标：让小本地模型在子任务执行中表现稳定、可预期，不依赖运行时再生成。
# 如需覆盖，可在 spawn_subagent 调用时传 role 指定角色。
_SUBAGENT_STATIC_ROLE = """你是一个运行在本机的「本地子智能体」（子任务执行专家），由主智能体在需要时派生，
专门独立负责完成主智能体指派给你的某一个自包含子任务。请严格遵守以下准则：

# 身份与语言
- 你的身份是任务执行专家，不是聊天对象；你只在执行子任务期间存在，子任务结束即退出。
- 必须始终用中文回复，全程不得夹杂英文句子（专业术语可保留英文单词，如 API / KV cache）。

# 任务边界
- 只聚焦于主智能体交给你的 task，不要自行扩大范围，也不要去处理与 task 无关的事情。
- 如果 task 描述不清或缺少关键信息，先用一句话说明你缺什么，然后基于已有信息尽力推进；
  不要反复向主智能体提问（主智能体不会即时回复你，等待会卡死整个流程）。
- 绝对不要再调用 spawn_subagent 派生子任务（避免无限递归）；一切靠你自己的工具完成。

# 工具使用
你可以使用除 spawn_subagent 外的全部工具，包括：read_file / write_file / edit_file / list_files /
shell / python_exec / search_content / web_search / web_fetch / http_request / memory_remember /
memory_recall / download / task_board 等。
- 优先用工具获取真实信息，不要凭空编造文件内容、命令输出、网页数据或数值。
- 写文件/改文件前先确认路径正确，绝不触碰受保护目录（系统目录、本 Agent 项目本体）。
- 涉及删除（delete_file 等）要谨慎，先明确告知主智能体会删除/改动什么，必要时等待确认。
- 执行 shell / python 命令时优先用绝对路径，命令失败就读懂报错再修正，不要盲目重试。

# 工作风格与输出
- 直接产出 task 要求的结果，不要客套、不要寒暄、不要加「作为子智能体…」「好的，我来…」之类的开场白。
- 复杂结果用清晰的列表 / 分段呈现；需要引用时标注来源（文件路径或 URL）。
- 如果 task 是审查 / 总结 / 提取类，给出「结论先行 + 关键依据」，不要把原始材料整段堆回去。
- 遇到错误：说明你做了什么、报了什么错、下一步建议，便于主智能体接管，不要假装成功。

# 返回格式
你的最终输出会被主智能体直接拼接进它的回答，因此：
- 只输出 task 的交付物本身（结论 / 产物 / 数据），不要输出元叙事（如「我已完成任务」）。
- 若子任务确实没有实质产物，输出一句简短的状态说明即可。
- 保持简洁，但信息完整：宁可多给一个关键事实，也不要让主智能体为缺失信息再次派生子任务。"""


async def _spawn_subagent(args: dict) -> str:
    """派生子智能体完成一个子任务，返回其结果文本。

    子智能体复用「当前对话所用的那个接口」（同一 client 与配置），带独立角色 prompt，
    可使用除自身外的全部工具（防止无限递归）。结果回传主智能体。
    角色 prompt 默认使用内置全面静态角色（_SUBAGENT_STATIC_ROLE），也可用 role 显式覆盖。
    """
    task = str(args.get("task") or "").strip()
    role = str(args.get("role") or "").strip()
    if not task:
        return "错误：缺少 task（子任务描述）"

    ctx_data = _AGENT_CTX.get() or {}
    client = ctx_data.get("client")
    settings = ctx_data.get("settings") or {}
    if client is None:
        return "错误：当前没有可用的接口客户端，无法派生子智能体。"

    # 角色未显式指定 → 使用内置全面静态角色（行为稳定可预期）
    if not role:
        role = _SUBAGENT_STATIC_ROLE

    # 子智能体工具：排除 spawn_subagent 自身，防止无限递归
    child_tools = [t for t in TOOLS.keys() if t != "spawn_subagent"]

    try:
        from agents.agent_loop import Agent
        agent = Agent(client, system_prompt=role, settings=settings, allowed_tools=child_tools)
        collected: list[str] = []

        async def _emit(ev: dict) -> None:
            if not isinstance(ev, dict):
                return
            t = ev.get("type")
            if t == "content":
                collected.append(ev.get("text", ""))
            elif t == "error":
                collected.append(f"\n[子智能体执行错误] {ev.get('message', '')}")

        ctx = current_workspace_ctx()
        await agent.run([], task, _emit, ctx=ctx, session_id=None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("spawn_subagent 失败")
        return f"子智能体执行异常：{exc}"

    result = "".join(collected).strip()
    return result or "（子智能体无文本输出）"


register_tool(
    "spawn_subagent",
    "派生子智能体（子任务专家）独立完成一个子任务，并把结果返回给你。\n"
    "用途：任务可拆成独立子任务、或需要不同专家视角（如「代码审查」「长文总结」「数据提取」）时使用。\n"
    "子智能体复用当前对话所用的接口、带独立角色设定，可使用除自身外的全部工具（文件/Shell/记忆等）。\n"
    "参数：task（子任务描述，必填）、role（子智能体角色/人设，可选；不填则使用内置全面静态默认角色）。",
    {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "要派给子智能体的子任务描述（清晰、自包含）"},
            "role": {"type": "string", "description": "子智能体角色/人设（可选）；不填则用内置通用默认"},
        },
        "required": ["task"],
    },
    _spawn_subagent,
    min_permission="low",
)
