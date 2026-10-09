"""内置知识库 — 随程序自带，首次运行自动创建空库。

设计要点
--------
1. **不依赖任何外部路径**：早期版本把检索委托给 `<用户文档>/AiKnowledgeBase/工具/kb_search.py`，
   那既是硬编码路径，也让「装了程序却没有那个目录」的用户直接报错。现在检索/保存逻辑内联在本模块，
   零外部依赖。
2. **安装即空库**：库根目录位于用户数据目录（重装/卸载不丢，且与开发者的知识库完全隔离）：
       - 环境变量 MY_AGENT_KB_DIR（显式覆盖，测试用）
       - Windows: %APPDATA%/MY_AGENT/knowledge
       - XDG:     $XDG_DATA_HOME/my-agent/knowledge
       - 兜底:    ~/.myagent/knowledge
   首次访问时自动 mkdir，并写入一份说明用 README.md（不含任何个人内容）。
3. **格式约定**：库内按分类建子目录，每条知识是一个 Markdown 文件；检索范围 .md/.json/.txt。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

EXTS = (".md", ".json", ".txt")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}

_README = """# 知识库

这是 MY_AGENT 自带的知识库，目前是**空的**。

## 怎么用

- **保存知识**：对助手说「把 XXX 记到知识库」，或用工具 `kb_save`（指定 `category` 分类与 `title`）。
- **检索知识**：问助手以前整理过的资料，助手会调用 `kb_search` 在本目录内检索。

## 目录约定

```
knowledge/
├── README.md          ← 本文件
├── 默认/               ← 未指定分类时写入这里
└── <你的分类>/<标题>.md
```

检索范围：本目录下所有 `.md` / `.json` / `.txt` 文件。
"""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_kb_dir(create: bool = True) -> Path:
    """返回知识库根目录；create=True 时确保目录存在并补齐 README。"""
    env = os.environ.get("MY_AGENT_KB_DIR")
    if env:
        root = Path(env)
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = Path(base) / "MY_AGENT" / "knowledge" if base else _project_root() / "knowledge"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            root = Path(xdg) / "my-agent" / "knowledge"
        else:
            home = os.environ.get("HOME") or os.path.expanduser("~")
            root = Path(home) / ".myagent" / "knowledge" if home and str(home) != "~" else _project_root() / "knowledge"

    if create:
        try:
            root.mkdir(parents=True, exist_ok=True)
            readme = root / "README.md"
            if not readme.exists():
                readme.write_text(_README, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建知识库目录失败: %s", exc)
    return root


def _walk(kb: Path, sub: str | None = None):
    base = kb / sub if sub else kb
    if not base.is_dir():
        return
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            if f.lower().endswith(EXTS):
                yield Path(root) / f


def search(terms: list[str], use_or: bool = False, limit: int = 20) -> list[tuple[str, int, str]]:
    """在全库内检索关键词，返回 [(相对路径, 行号, 命中行)]（最多 limit 条）。"""
    kb = resolve_kb_dir()
    out: list[tuple[str, int, str]] = []
    if not terms:
        return out
    lowered = [t.lower() for t in terms]
    for path in _walk(kb):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        rel = path.relative_to(kb).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            ll = line.lower()
            hit = any(t in ll for t in lowered) if use_or else all(t in ll for t in lowered)
            if hit:
                out.append((rel, i, line.strip()[:200]))
                if len(out) >= limit:
                    return out
    return out


def list_tree(kb: Path | None = None) -> dict:
    """列出知识库结构与各顶层分类的文件数。"""
    kb = kb or resolve_kb_dir()
    counts: dict[str, int] = {}
    entries: list[dict] = []
    for path in _walk(kb):
        rel = path.relative_to(kb).as_posix()
        top = rel.split("/")[0] if "/" in rel else "(根目录)"
        counts[top] = counts.get(top, 0) + 1
        entries.append({"file": rel, "size": path.stat().st_size if path.exists() else 0})
    return {"root": str(kb), "file_count": len(entries), "category_counts": counts, "files": entries}


def read_file(rel_path: str, max_chars: int = 4000) -> dict:
    """读取知识库内某个文件（相对路径）。"""
    kb = resolve_kb_dir()
    target = (kb / rel_path).resolve()
    try:
        target.relative_to(kb.resolve())
    except ValueError:
        return {"ok": False, "error": "路径越界（只允许读取知识库内文件）"}
    if not target.is_file():
        return {"ok": False, "error": f"文件不存在: {rel_path}"}
    data = target.read_text(encoding="utf-8", errors="replace")
    return {
        "ok": True,
        "path": rel_path,
        "total_chars": len(data),
        "truncated": len(data) > max_chars,
        "content": data[:max_chars],
    }


def save_knowledge(title: str, content: str, category: str = "默认") -> Path:
    """保存一条知识到 <kb>/<category>/<title>.md（追加模式）。

    标题含 Windows 非法字符时抛 ValueError，由调用方转成给用户的提示。
    """
    bad = re.findall(r'[\\/:*?"<>|]', title)
    if bad:
        raise ValueError("标题含非法字符: " + " ".join(dict.fromkeys(bad)))
    kb = resolve_kb_dir()
    folder = kb / (category or "默认")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{title}.md"
    is_new = not path.exists()
    with path.open("a", encoding="utf-8") as fp:
        if is_new:
            fp.write(f"# {title}\n\n> 归档时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        fp.write(content.rstrip() + "\n\n")
    return path
