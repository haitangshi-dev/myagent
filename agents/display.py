"""Hermes 风格工具显示协议 — 仿 hermes agent/display.py 的 build_tool_label / build_tool_preview。

核心原则（与 Hermes 一致）：
  - 工具调用绝不下发原始 JSON 参数到聊天前端。
  - 改用「人话动词 + 主参数预览」渲染工具卡片，例如：
        "Running npm install"  /  "Reading docs/api.md"  /  "Searching the web for weather"
  - 凭据类参数在预览阶段即被脱敏（redact），杜绝密钥泄露到 UI/日志。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# 工具预览最大长度（字符），None 取全局值，0 表示不限
_tool_preview_max_len: int = 120

# 人话动词表（现在分词），仅内置工具可映射；插件/自定义工具回落到原始预览
_TOOL_VERBS: dict[str, str] = {
    "web_search": "Searching the web",
    "web_fetch": "Reading",
    "shell": "Running",
    "python_exec": "Running code",
    "read_file": "Reading",
    "write_file": "Writing",
    "list_files": "Listing",
    "think": "Thinking",
    "patch": "Editing",
    "edit_file": "Editing",
    "search_content": "Searching files",
    "time_now": "Checking time",
    "download": "Downloading",
    "http_request": "Requesting",
    "memory_remember": "Saving memory",
    "memory_recall": "Recalling memory",
    "find_skill": "Searching SkillHub for",
    "list_software": "Listing installed software",
    "uninstall_software": "Uninstalling",
    "organize_downloads": "Organizing downloads",
    "desktop_control": "Controlling desktop",
    "task_board": "Updating task board",
    "schedule_task": "Scheduling",
}

# 子操作动词覆盖：当工具带 action 参数时，用更贴切的动词取代通用动词。
# 例：desktop_control 的 action=vision 显示 "Seeing screen"，而非笼统的 "Controlling desktop"。
_ACTION_VERB_OVERRIDES: dict[tuple[str, str], str] = {
    ("desktop_control", "vision"): "Seeing screen",
    ("desktop_control", "screenshot"): "Capturing screen",
    ("desktop_control", "ocr"): "Reading screen text",
    ("desktop_control", "mouse"): "Moving mouse",
    ("desktop_control", "keyboard"): "Typing",
    ("desktop_control", "window"): "Managing window",
}

# 这些工具读起来不需要拼接参数预览
_TOOL_VERBS_NO_PREVIEW: frozenset[str] = frozenset({"think"})

# 这些工具用 "for" 连接词更自然（搜索类）
_TOOL_VERBS_FOR_CONNECTOR: frozenset[str] = frozenset({"web_search", "list_files", "search_content", "memory_recall", "find_skill", "schedule_task"})

_friendly_tool_labels: bool = True


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _oneline(text: str) -> str:
    """把空白（含换行）压缩成单空格。"""
    return " ".join(text.split())


def _truncate_preview(text: str, max_len: int | None) -> str:
    if max_len and max_len > 0 and len(text) > max_len:
        if max_len <= 3:
            return "." * max_len
        return text[: max_len - 3] + "..."
    return text


# ---------------------------------------------------------------------------
# 简易 shell 摘要（保留可读命令，去掉管道/重定向噪声）
# ---------------------------------------------------------------------------
_SHELL_SILENT_HEADS = {"cd", "pushd", "popd", "export", "set", "unset", "source", ".", "true", "false", ":"}
_SHELL_PIPE_TAIL_HEADS = {"head", "tail", "wc", "sort", "uniq"}


def _shell_basename(head: str) -> str:
    return head.rsplit("/", 1)[-1] if head else ""


def _split_shell_words(segment: str) -> list[str]:
    words: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(segment):
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or segment[i - 1] != "\\"):
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            continue
        if ch.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def _strip_shell_pipe_tail(segment: str) -> str:
    words = _split_shell_words(segment)
    out: list[str] = []
    for i, word in enumerate(words):
        if word == "|" and _shell_basename(words[i + 1] if i + 1 < len(words) else "") in _SHELL_PIPE_TAIL_HEADS:
            break
        out.append(word)
    return " ".join(out).strip()


def _split_shell_compound(command: str) -> list[str]:
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or command[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        op_len = 2 if command.startswith("&&", i) or command.startswith("||", i) else 1 if ch in {";", "\n"} else 0
        if op_len:
            segment = _strip_shell_pipe_tail("".join(buf).strip())
            if segment:
                segments.append(segment)
            buf = []
            i += op_len
            continue
        buf.append(ch)
        i += 1
    segment = _strip_shell_pipe_tail("".join(buf).strip())
    if segment:
        segments.append(segment)
    return segments


def _shell_head_word(segment: str) -> str:
    words = _split_shell_words(segment)
    index = 0
    while index < len(words) and re.match(r"^[A-Za-z_]\w*=", words[index]):
        index += 1
    return _shell_basename(words[index] if index < len(words) else "")


def _clean_shell_segment(segment: str) -> str:
    words = _split_shell_words(segment)
    out: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if re.match(r"^\d*(?:>>?|<)$", word):
            i += 2
            continue
        if re.match(r"^\d*(?:>&|<&)\d+$", word) or re.match(r"^\d*>&\d+$", word):
            i += 1
            continue
        out.append(word)
        i += 1
    return " ".join(out).strip()


def summarize_shell_command(command: str) -> str:
    """为显示压缩 shell 包装/管道，原始命令在别处保持不变。"""
    original = _oneline(command)
    if not original:
        return ""
    segments = _split_shell_compound(original)
    if len(segments) <= 1:
        return _clean_shell_segment(segments[0] if segments else original) or original
    core: list[str] = []
    for segment in segments:
        cleaned = _clean_shell_segment(segment)
        head = _shell_head_word(cleaned)
        if cleaned and head not in _SHELL_SILENT_HEADS:
            core.append(cleaned)
    if not core:
        return original
    if len(core) == 1:
        return core[0]
    count = len(core) - 1
    return f"{core[0]} + {count} {'command' if count == 1 else 'commands'}"


# ---------------------------------------------------------------------------
# 凭据脱敏（显示前必过）
# ---------------------------------------------------------------------------
_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*)\S+"
    r"|"
    r"\b(?:sk-[a-z0-9]{20,}|nvapi-[a-z0-9]{20,}|ghp_[a-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
)


def redact_sensitive_text(text: str) -> str:
    """把看起来像密钥的字符串替换成 [REDACTED]。"""
    return _SECRET_RE.sub(lambda m: (m.group(1) + "[REDACTED]") if m.group(1) else "[REDACTED]", text)


def redact_tool_args_for_display(tool_name: str, args: dict | None) -> dict | None:
    """返回一份可安全用于日志/进度 UI 的工具参数副本。"""
    if not isinstance(args, dict):
        return args
    safe = dict(args)
    for key, value in safe.items():
        if isinstance(value, str):
            safe[key] = redact_sensitive_text(value)
    return safe


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build_tool_preview(tool_name: str, args: dict, max_len: int | None = None) -> str | None:
    """构建工具主参数的简短预览（不下发原始 JSON）。"""
    if max_len is None:
        max_len = _tool_preview_max_len
    if not args:
        return None
    args = redact_tool_args_for_display(tool_name, args) or args

    primary_args = {
        "shell": "command",
        "read_file": "path",
        "write_file": "path",
        "list_files": "path",
        "web_fetch": "url",
        "web_search": "query",
        "python_exec": "code",
        "patch": "path",
        "edit_file": "path",
        "search_content": "pattern",
        "download": "url",
        "http_request": "url",
        "memory_remember": "content",
        "memory_recall": "query",
        "think": "thought",
        "find_skill": "query",
        "uninstall_software": "name",
        "desktop_control": "action",
        "task_board": "action",
        "schedule_task": "when",
    }

    if tool_name == "shell":
        command = args.get("command")
        if command is None:
            return None
        preview = summarize_shell_command(str(command))
        return _truncate_preview(preview, max_len) if preview else None

    if tool_name == "read_file":
        path = args.get("path") or args.get("file") or args.get("filepath")
        if path is None:
            return None
        label = Path(str(path).replace("\\", "/")).name or str(path)
        return _truncate_preview(label, max_len)

    if tool_name == "think":
        thought = args.get("thought") or args.get("text")
        if thought is None:
            return "planning"
        return _truncate_preview(_oneline(str(thought)), max_len)

    if tool_name == "list_files":
        path = args.get("path") or "."
        return _truncate_preview(str(path), max_len)

    key = primary_args.get(tool_name)
    if not key:
        for fallback_key in ("query", "text", "command", "path", "name", "url", "prompt", "code", "goal"):
            if fallback_key in args:
                key = fallback_key
                break
    if not key or key not in args:
        return None

    value = args[key]
    if isinstance(value, list):
        value = value[0] if value else ""
    preview = _oneline(str(value))
    if not preview:
        return None
    if max_len and max_len > 0 and len(preview) > max_len:
        preview = preview[: max_len - 3] + "..."
    return preview


def build_tool_command(tool_name: str, args: Any) -> str:
    """暴露模型实际下发的“命令”（人话可读、脱敏），用于工具卡片 running 阶段展示。

    args 可为：
      - dict：解析后的参数（最终事件）
      - str：流式过程中累积的原始 arguments 片段（partial 事件）
    返回形如 ``shell: ls -la`` 或 ``web_search: weather in Shenzhen``。
    """
    if isinstance(args, str):
        raw = args.strip()
    elif isinstance(args, dict):
        # 优先取主参数让命令更可读；否则整包
        if "command" in args:
            raw = args["command"]
        elif "code" in args:
            raw = args["code"]
        else:
            raw = json.dumps(args, ensure_ascii=False)
    else:
        raw = json.dumps(args, ensure_ascii=False)
    raw = _oneline(str(raw))
    raw = redact_sensitive_text(raw)
    if len(raw) > 220:
        raw = raw[:217] + "..."
    return f"{tool_name}: {raw}"


def build_tool_label(tool_name: str, args: dict, max_len: int | None = None) -> str | None:
    """构建人话工具状态标签（动词 + 预览）。无动词的工具回落到原始预览。"""
    if not _friendly_tool_labels:
        return build_tool_preview(tool_name, args, max_len=max_len)
    verb = _TOOL_VERBS.get(tool_name)
    action_override = None
    if args:
        action = args.get("action")
        if action:
            action_override = _ACTION_VERB_OVERRIDES.get((tool_name, str(action).lower()))
            if action_override:
                verb = action_override
    if not verb:
        return build_tool_preview(tool_name, args, max_len=max_len)
    if action_override or tool_name in _TOOL_VERBS_NO_PREVIEW:
        # 动词已完整表达动作（如 Seeing screen），不再附加原始 action 名
        return verb
    preview = build_tool_preview(tool_name, args, max_len=max_len)
    if not preview:
        return verb
    if tool_name in _TOOL_VERBS_FOR_CONNECTOR:
        return f"{verb} for {preview}"
    return f"{verb} {preview}"


def build_status_phrase(tool_name: str, args: dict | None, max_len: int = 49) -> str | None:
    """构建当前正在做什么的短语（用于状态条）。"""
    if not tool_name or tool_name == "_thinking":
        return None
    if not _friendly_tool_labels:
        return None
    verb = _TOOL_VERBS.get(tool_name)
    action_override = None
    if args:
        action = args.get("action")
        if action:
            action_override = _ACTION_VERB_OVERRIDES.get((tool_name, str(action).lower()))
            if action_override:
                verb = action_override
    head = f"is {verb[0].lower()}{verb[1:]}" if verb else f"is using {tool_name}"
    phrase = head
    if action_override:
        # 动词已表达动作，状态条只显示动词
        if len(phrase) > max_len - 1:
            phrase = phrase[: max_len - 2].rstrip() + "…"
        else:
            phrase = phrase + "…"
        return phrase
    if args and verb and tool_name not in _TOOL_VERBS_NO_PREVIEW:
        preview = build_tool_preview(tool_name, args, max_len=None)
        if preview:
            preview = preview.splitlines()[0].strip()
            phrase = f"{head}{' for ' if tool_name in _TOOL_VERBS_FOR_CONNECTOR else ' '}{preview}"
    if len(phrase) > max_len - 1:
        phrase = phrase[: max_len - 2].rstrip() + "…"
    else:
        phrase = phrase + "…"
    return phrase
