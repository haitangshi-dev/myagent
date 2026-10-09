"""全局持久记忆 — 跨会话、BM25 召回、自动抽取 / 去重 / 压缩。

零第三方依赖：
  - BM25（Okapi, k1=1.5, b=0.75）用纯标准库实现。
  - 中文按字 uni + bigram 切分（保留 tokenize 单点替换，未来可换 jieba / 本地 embedding）。
  - 记忆落盘为 agent_memory.jsonl（每行一个 JSON 条目），位置在用户数据目录
    （%APPDATA%/MY_AGENT 等），重装 / 卸载不会被清空；旧安装目录内的记忆会在启动时迁移过来。

条目 schema:
  { id, ts, content, tags, source, status }
    id      : uuid4().hex（旧数据迁移时自动补）
    ts      : ISO 时间字符串
    content : 记忆正文
    tags    : list[str]
    source  : auto | manual | import
    status  : active | superseded（冲突时旧条标 superseded，不直接删，保留可追溯）
"""

from __future__ import annotations

import json as _json
import logging
import math
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# =========================================================================
# 记忆落盘位置：必须放在安装目录之外，否则重装 / 卸载（NSIS 删整个安装目录）会清空。
# 优先级：
#   1) 环境变量 MY_AGENT_MEMORY_DIR（由 electron 主进程注入，指向 userData/memory）
#   2) 平台用户数据目录：Windows %APPDATA%/MY_AGENT，Linux $XDG_DATA_HOME/my-agent，macOS ~/.myagent
#   3) 兜底：__file__ 所在项目根（极端环境兼容，非默认路径）
# 启动时会把旧位置（安装目录内的 agent_memory.jsonl）的记忆一次性并入新位置。
# =========================================================================
def _resolve_memory_dir() -> Path:
    env = os.environ.get("MY_AGENT_MEMORY_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "MY_AGENT"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "my-agent"
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if home and str(home) not in ("~", ""):
        return Path(home) / ".myagent"
    return Path(__file__).resolve().parent.parent


def _legacy_memory_file() -> Path:
    """旧位置：安装目录内的 agent_memory.jsonl（升级时迁移用，重装会被删）。"""
    return Path(__file__).resolve().parent.parent / "agent_memory.jsonl"


# 记忆主文件（位于用户数据目录，重装安全）
MEMORY_FILE = _resolve_memory_dir() / "agent_memory.jsonl"

# --- BM25 参数 ---
_K1 = 1.5
_B = 0.75

# --- 轻量停用词（中英文常见无意义词）---
_STOPWORDS = set(
    (
        "的 了 和 是 在 我 你 他 她 它 们 这 那 有 个 啊 吗 呢 吧 把 被 让 给 "
        "就 也 都 还 很 不 没 又 再 要 会 能 可 以 对 与 及 或 但 而 如果 因为 所以 "
        "我们 你们 他们 自己 什么 怎么 怎样 如何 这个 那个 一个 没有 不是 就是 这样 那样 "
        "the a an of to in on for and or but is are was were be been being this that "
        "it its as at by from we you they he she do does did has have had i my your our "
        "with about into over under than then so out up down off"
    ).split()
)

# 文件读写锁（FastAPI 同步端点跑在线程池，用线程锁保护 read-modify-write）
_file_lock = threading.Lock()

# 记忆抽取 / 压缩用的 LLM（由 server/api.py 在启动时注入，取默认 provider 客户端）
_LLM_FN: Callable[[str, str], Any] | None = None


# =========================================================================
# 分词 + BM25
# =========================================================================
def tokenize(text: str) -> list[str]:
    """中文 uni + bigram 切分；拉丁 / 数字连续段小写成词；去停用词与空白。"""
    if not text:
        return []
    text = text.lower()
    tokens: list[str] = []
    # 拉丁 / 数字连续段 → 词
    for m in re.findall(r"[a-z0-9]+", text):
        if m not in _STOPWORDS:
            tokens.append(m)
    # CJK 段 → uni + bigram
    cjk = "".join(re.findall(r"[一-鿿]", text))
    for ch in cjk:
        if ch not in _STOPWORDS:
            tokens.append(ch)
    for i in range(len(cjk) - 1):
        bg = cjk[i : i + 2]
        if bg not in _STOPWORDS:
            tokens.append(bg)
    return tokens


def _bm25_scores(query_tokens: list[str], docs: list[list[str]]) -> list[float]:
    """对每条已 tokenize 的文档算 BM25 分数，返回等长分数列表。"""
    n = len(docs)
    if n == 0 or not query_tokens:
        return [0.0] * n
    # 文档频率 df
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    # idf
    avgdl = (sum(len(d) for d in docs) / n) if n else 0.0
    idf: dict[str, float] = {}
    for t, f in df.items():
        idf[t] = math.log((n - f + 0.5) / (f + 0.5) + 1.0)
    scores: list[float] = []
    for d in docs:
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        dl = len(d)
        denom_dl = dl / avgdl if avgdl else 1.0
        score = 0.0
        for qt in query_tokens:
            f = tf.get(qt)
            if not f:
                continue
            denom = f + _K1 * (1 - _B + _B * denom_dl)
            score += idf.get(qt, 0.0) * (f * (_K1 + 1)) / denom
        scores.append(score)
    return scores


# =========================================================================
# 落盘读写（带原子写 + 旧数据迁移）
# =========================================================================
def _read_entries() -> list[dict]:
    """读取全部条目；对缺 id 的旧条目自动补 id（并落盘一次）。"""
    if not MEMORY_FILE.exists():
        return []
    try:
        raw = MEMORY_FILE.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取记忆失败: %s", exc)
        return []
    out: list[dict] = []
    need_migrate = False
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = _json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if not e.get("id"):
            e["id"] = uuid.uuid4().hex
            need_migrate = True
        if "status" not in e:
            e["status"] = "active"
        out.append(e)
    if need_migrate:
        _write_entries(out)
    return out


def _write_entries(entries: list[dict]) -> None:
    """原子写：先写临时文件再 replace，避免半截写入损坏。"""
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMORY_FILE.with_name(MEMORY_FILE.name + ".tmp")
    lines = [_json.dumps(e, ensure_ascii=False) for e in entries]
    tmp.write_text(
        ("\n".join(lines) + "\n") if lines else "",
        encoding="utf-8",
    )
    tmp.replace(MEMORY_FILE)


def _strip_score(entries: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k != "score"} for e in entries]


# =========================================================================
# 公开 API
# =========================================================================
def add(content: str, tags: list[str] | None = None, source: str = "manual") -> dict:
    """新增一条记忆，返回写入的条目（自动补 id / ts）。"""
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    if source not in ("auto", "manual", "import"):
        source = "manual"
    entry = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "content": content,
        "tags": list(tags or []),
        "source": source,
        "status": "active",
    }
    with _file_lock:
        entries = _read_entries()
        entries.append(entry)
        _write_entries(entries)
    return entry


def search(query: str, limit: int = 8) -> list[dict]:
    """BM25 召回：返回活跃条目（按分数降序），每条带 score 字段。

    查询为空时返回最近 limit 条活跃记忆（会话开始自动召回用）。
    """
    q = (query or "").strip()
    with _file_lock:
        active = [e for e in _read_entries() if e.get("status", "active") == "active"]
    if not active:
        return []
    if not q:
        recent = active[-limit:] if limit else active
        return [dict(e, score=0.0) for e in recent]
    qtokens = tokenize(q)
    if not qtokens:
        return []
    docs = [
        tokenize(str(e.get("content", "")) + " " + " ".join(map(str, e.get("tags", []))))
        for e in active
    ]
    scores = _bm25_scores(qtokens, docs)
    ranked = [(e, s) for e, s in zip(active, scores) if s > 0]
    ranked.sort(key=lambda x: x[1], reverse=True)
    ranked = ranked[:limit]
    return [dict(e, score=round(s, 4)) for e, s in ranked]


def recall_text(query: str, limit: int = 8) -> str:
    """格式化供注入 system prompt 的回忆文本（无则空字符串）。"""
    try:
        hits = search(query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("recall_text 失败: %s", exc)
        return ""
    if not hits:
        return ""
    lines = []
    for h in hits:
        tagstr = ("  #" + " #".join(h.get("tags", []))) if h.get("tags") else ""
        lines.append(f"- {h['content']}{tagstr}")
    return "\n".join(lines)


def build_index(limit: int = 30) -> str:
    """记忆索引：把全部活跃记忆压成一行短描述（tag + 内容前 40 字）。

    供「双层记忆」第一层注入（2026-08-08）——模型看到索引才知道完整记忆里
    有什么，需要细节时再调 memory_recall 检索第二层。索引很轻：
    30 条约 300-400 token，远小于全量注入。
    """
    try:
        with _file_lock:
            active = [e for e in _read_entries() if e.get("status", "active") == "active"]
    except Exception:  # noqa: BLE001
        return ""
    if not active:
        return ""
    recent = active[-limit:]
    lines = []
    for e in recent:
        tags = e.get("tags") or []
        tagstr = (" #" + " #".join(str(t) for t in tags)) if tags else ""
        content = " ".join(str(e.get("content", "")).split())[:40]
        if not content:
            continue
        lines.append(f"- {content}{tagstr}")
    return "\n".join(lines)


def list_all(query: str = "", limit: int = 200) -> list[dict]:
    """返回全局记忆结构化列表（含 id / source / status），供前端渲染 / 搜索 / 编辑 / 删除。"""
    with _file_lock:
        entries = _read_entries()
    if (query or "").strip():
        q = (query or "").strip()
        qtokens = tokenize(q)
        docs = [
            tokenize(str(e.get("content", "")) + " " + " ".join(map(str, e.get("tags", []))))
            for e in entries
        ]
        scores = _bm25_scores(qtokens, docs)
        ranked = [(e, s) for e, s in zip(entries, scores) if s > 0]
        ranked.sort(key=lambda x: x[1], reverse=True)
        entries = [e for e, _ in ranked]
    else:
        # 无查询：按时间倒序（最新在前）
        entries = sorted(entries, key=lambda e: e.get("ts", ""), reverse=True)
    if limit and limit > 0:
        entries = entries[:limit]
    return _strip_score(entries)


def get_by_id(mid: str) -> dict | None:
    with _file_lock:
        for e in _read_entries():
            if e.get("id") == mid:
                return dict(e)
    return None


def update(mid: str, content: str | None = None, tags: list[str] | None = None) -> dict | None:
    """按 id 更新 content / tags（任一为 None 则不改）。返回更新后的条目或 None。"""
    with _file_lock:
        entries = _read_entries()
        for e in entries:
            if e.get("id") == mid:
                if content is not None:
                    e["content"] = content.strip()
                if tags is not None:
                    e["tags"] = list(tags)
                e["ts"] = datetime.now().isoformat(timespec="seconds")
                _write_entries(entries)
                return dict(e)
    return None


def delete(mid: str) -> bool:
    """按 id 物理删除一条记忆（用户主动删除用）。返回是否删除成功。"""
    with _file_lock:
        entries = _read_entries()
        new = [e for e in entries if e.get("id") != mid]
        if len(new) == len(entries):
            return False
        _write_entries(new)
    return True


def clear_all() -> int:
    """清空全部全局记忆，返回原条目数。"""
    with _file_lock:
        entries = _read_entries()
        n = len(entries)
        try:
            MEMORY_FILE.unlink()
        except FileNotFoundError:
            pass
    return n


# =========================================================================
# LLM 接入（自动抽取 / 压缩）
# =========================================================================
def set_llm(fn: Callable[[str, str], Any]) -> None:
    """注入一个 async fn(system, user) -> str（非对话轮次的记忆抽取 / 压缩用）。

    通常由 server/api.py 注入：取默认 provider 客户端收集文本。
    """
    global _LLM_FN
    _LLM_FN = fn


async def _call_llm(system: str, user: str, client: Any = None) -> str:
    """统一的 LLM 文本收集：优先用传入 client（有 stream_chat），否则用 _LLM_FN。

    任何异常都返回空串（best-effort，绝不抛）。
    """
    fn = client if client is not None else _LLM_FN
    if fn is None:
        return ""
    # OpenAICompatClient → 走 stream_chat 收集
    if hasattr(fn, "stream_chat"):
        collected: list[str] = []
        try:
            async for ev in fn.stream_chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=1024,
                repetition_penalty=1.1,
            ):
                if ev.get("type") == "content_delta":
                    collected.append(ev["text"])
                elif ev.get("type") == "error":
                    return ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆 LLM 调用出错: %s", exc)
            return ""
        return "".join(collected).strip()
    # 否则当成 async fn(system, user) -> str
    try:
        out = await fn(system, user)
        return (out or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆 LLM 调用出错: %s", exc)
        return ""


def _parse_facts(text: str) -> list[dict]:
    """从 LLM 输出里抽取 JSON 数组（兼容 ```json 围栏）。非数组 / 解析失败返回 []。"""
    text = (text or "").strip()
    if not text:
        return []
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    s = text.find("[")
    e = text.rfind("]")
    if s == -1 or e == -1 or e < s:
        return []
    try:
        data = _json.loads(text[s : e + 1])
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _mark_superseded(mid: str) -> bool:
    """把某条活跃记忆标为 superseded（冲突旧条保留可追溯）。"""
    with _file_lock:
        entries = _read_entries()
        changed = False
        for e in entries:
            if e.get("id") == mid and e.get("status", "active") == "active":
                e["status"] = "superseded"
                changed = True
        if changed:
            _write_entries(entries)
            return True
    return False


# =========================================================================
# 两档去重写入（smart_add）：档1 直接保存 / 档2 LLM 对比确认后按真实结果执行
# =========================================================================
_DEDUP_HARD_THRESHOLD = 3.0  # BM25 分数 ≥ 此值进入档2（LLM 复核），而非一刀切拒绝
_DEDUP_REVIEW_LIMIT = 5      # 档2 最多带多少条候选旧记忆给 LLM 对比


async def _judge_memory_relation(content: str, related: list[dict], client: Any = None) -> tuple[str, str | None]:
    """让 LLM 判断新记忆与候选旧记忆的关系。

    返回 (verdict, supersede_id)：
      verdict ∈ new / duplicate / update；supersede_id 仅 update 时可能有。
    任何异常/解析失败 → ("new", None)（宁可多存，不误杀新信息）。
    """
    if not related:
        return "new", None
    block = "".join(f"- [id={r.get('id')}] {r.get('content')}\n" for r in related)
    sys_prompt = (
        "你是一个记忆管理员。用户提交了一条「新记忆」，同时给出现有记忆库中与它最相关的候选条目。\n"
        "请判断新记忆相对这些候选是：\n"
        "1. new —— 新记忆表达了**新的、不与候选重复的事实**（即使关键词相近，但说的是不同内容/不同角度/补充了新细节）→ 应保存。\n"
        "2. duplicate —— 新记忆与某条候选**说的是同一件事**（仅措辞不同）→ 应跳过，不重复保存。\n"
        "3. update —— 新记忆是对某条候选的**修正/更新**（信息更准确、更完整，旧条已被取代）→ 应保存新记忆并让旧条失效。\n"
        "只输出 JSON，不要解释：{\"verdict\": \"new\"|\"duplicate\"|\"update\", \"supersede_id\"?: str}\n"
        "supersede_id 仅在 verdict=update 时给出（必须是候选旧记忆里出现的真实 id）。"
    )
    user_prompt = (
        f"【候选旧记忆】\n{block}\n"
        f"【新记忆】\n{content}\n\n"
        "请输出判定 JSON："
    )
    text = await _call_llm(sys_prompt, user_prompt, client)
    if not text:
        return "new", None
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if not m:
        return "new", None
    try:
        obj = _json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return "new", None
    verdict = str(obj.get("verdict") or "new").lower()
    if verdict not in ("new", "duplicate", "update"):
        return "new", None
    sid = str(obj.get("supersede_id") or "").strip() or None
    return verdict, sid


async def smart_add(
    content: str,
    tags: list[str] | None = None,
    source: str = "manual",
    client: Any = None,
) -> dict:
    """两档去重写入：

    档1 —— 没有强相似候选（BM25 < _DEDUP_HARD_THRESHOLD）：直接保存。
    档2 —— 存在强相似候选：注入 prompt 让 LLM 对比确认，按真实结果执行——
          new → 保存；duplicate → 跳过；update → 旧条标 superseded + 新记忆保存。

    返回 {"saved": bool, "verdict": str, "entry"?: dict, "reason"?: str, "supersede_id"?: str}
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    related = search(content, limit=_DEDUP_REVIEW_LIMIT)
    strong = [r for r in related if r.get("score", 0) >= _DEDUP_HARD_THRESHOLD]

    # 档1：无强命中 → 直接保存
    if not strong:
        entry = add(content, tags=tags, source=source)
        return {"saved": True, "verdict": "new", "entry": entry}

    # 档2：LLM 复核
    verdict, sid = await _judge_memory_relation(content, strong, client=client)
    if verdict == "duplicate":
        top = strong[0]
        return {
            "saved": False,
            "verdict": "duplicate",
            "reason": f"与已有记忆重复（未重复添加）：{top.get('content', '')[:50]}",
            "related": [r.get("id") for r in strong],
        }
    if verdict == "update":
        # 优先采用 LLM 点名的 supersede_id（必须是候选里的真实 id），否则回退最强候选
        valid_ids = {r.get("id") for r in strong}
        target = sid if sid in valid_ids else strong[0].get("id")
        if target:
            _mark_superseded(target)
        entry = add(content, tags=tags, source=source)
        return {
            "saved": True,
            "verdict": "update",
            "entry": entry,
            "supersede_id": target,
            "reason": f"已更新旧记忆（{target[:8] if target else '?'} 已失效）",
        }
    # new
    entry = add(content, tags=tags, source=source)
    return {"saved": True, "verdict": "new", "entry": entry}


async def extract_and_store(
    client: Any = None,
    user_text: str = "",
    assistant_text: str = "",
    settings: dict | None = None,
) -> int:
    """从一轮对话抽取关键事实写入全局记忆（自动去重 + 冲突标记）。

    返回本次新增 / 更新的条目数。全程 try/except 包住，失败不影响主对话。
    client：对话用客户端（优先）；为 None 时回退 _LLM_FN。
    """
    try:
        if not user_text and not assistant_text:
            return 0
        # 候选旧记忆（供 LLM 判断 new / update / duplicate）
        related = search(user_text + " " + assistant_text, limit=6)
        related_block = ""
        for r in related:
            related_block += f"- [id={r['id']}] {r['content']}\n"
        if not related_block:
            related_block = "（暂无相关旧记忆）"

        sys_prompt = (
            "你是一个记忆管理员。阅读下面「已有的相关记忆」和「本轮对话」，"
            "提炼出值得长期记住的、稳定且可复用的**事实性**信息（用户的偏好、"
            "个人情况、项目约定、常用环境 / 工具、长期任务状态等）。\n"
            "规则：\n"
            "1. 只输出 JSON 数组，不要任何解释。若没有值得记忆的新信息，输出 []。\n"
            "2. 每条格式：{\"content\": str, \"tags\": [str], "
            "\"verdict\": \"new\"|\"update\"|\"duplicate\", \"supersede_id\"?: str}\n"
            "3. verdict=new：全新事实 → 直接写入。\n"
            "4. verdict=update：修正 / 补充了某条旧记忆 → 提供 supersede_id（旧记忆 id），"
            "旧记忆会被标记失效，新事实写入。\n"
            "5. verdict=duplicate：与已有记忆重复 → 跳过。\n"
            "6. 不要记忆一次性任务步骤、临时报错、闲聊。内容要简洁（一句事实）。"
        )
        user_prompt = (
            f"【已有的相关记忆】\n{related_block}\n"
            f"【本轮对话】\n用户：{user_text}\n助手：{assistant_text}\n\n"
            "请输出抽取结果 JSON 数组："
        )
        text = await _call_llm(sys_prompt, user_prompt, client)
        if not text:
            return 0
        facts = _parse_facts(text)
        if not facts:
            return 0
        count = 0
        for f in facts:
            verdict = (f.get("verdict") or "new").lower()
            content = (f.get("content") or "").strip()
            if not content:
                continue
            tags = [str(t) for t in (f.get("tags") or [])][:8]
            if verdict == "duplicate":
                continue
            if verdict == "update":
                sid = f.get("supersede_id")
                if sid:
                    _mark_superseded(sid)
                add(content, tags=tags, source="auto")
                count += 1
            else:  # new
                add(content, tags=tags, source="auto")
                count += 1
        return count
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_and_store 失败: %s", exc)
        return 0


async def maybe_auto_summarize(settings: dict | None = None) -> int:
    """活跃条目超过阈值时，把最旧的超出部分压缩成少量概括条目（best-effort）。

    返回新增的概括条目数。需要 _LLM_FN（对话外场景）；未注入则直接跳过。
    """
    try:
        threshold = 300
        if settings:
            threshold = int(
                (settings.get("agent", {}) or {}).get("memory_auto_summarize_threshold", 300) or 300
            )
        with _file_lock:
            entries = _read_entries()
        active = [e for e in entries if e.get("status", "active") == "active"]
        if len(active) <= threshold:
            return 0
        overflow = len(active) - threshold
        active_sorted = sorted(active, key=lambda e: e.get("ts", ""))
        to_compress = active_sorted[:overflow]
        if not to_compress:
            return 0
        compress_block = "\n".join(f"- {e['content']}" for e in to_compress)
        sys_p = (
            "你是一个记忆压缩器。把下面的多条记忆压缩成 1~3 条更精炼的概括性记忆，"
            "保留关键事实（偏好、环境、长期约定、项目状态）。只输出 JSON 数组，"
            "每项 {\"content\": str, \"tags\": [str]}。不要解释。"
        )
        user_p = f"【待压缩记忆】\n{compress_block}\n\n输出压缩结果："
        out = await _call_llm(sys_p, user_p)  # 无 client → 用 _LLM_FN
        facts = _parse_facts(out) if out else []
        summary_ids: list[str] = []
        for f in facts:
            content = (f.get("content") or "").strip()
            if not content:
                continue
            tags = [str(t) for t in (f.get("tags") or [])][:8]
            add(content, tags=tags, source="auto")
            summary_ids.append(content)
        # 标记被压缩的旧条为 superseded
        with _file_lock:
            entries = _read_entries()
            comp_ids = {e["id"] for e in to_compress}
            changed = False
            for e in entries:
                if e.get("id") in comp_ids and e.get("status", "active") == "active":
                    e["status"] = "superseded"
                    changed = True
            if changed:
                _write_entries(entries)
        return len(summary_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("maybe_auto_summarize 失败: %s", exc)
        return 0


# =========================================================================
# 启动迁移：首次导入时把旧位置（安装目录内）的记忆并入新用户数据目录。
# 目的：旧版用户升级后，记忆不再躺在安装目录、重装被清空。幂等 + 异常安全。
# =========================================================================
def _migrate_legacy_memory() -> None:
    legacy = _legacy_memory_file()
    # 新位置与旧位置相同（极端环境）时无需迁移
    if legacy == MEMORY_FILE or not legacy.exists():
        return
    try:
        raw = legacy.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取旧记忆文件失败（跳过迁移）: %s", exc)
        return
    legacy_entries: list[dict] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            legacy_entries.append(_json.loads(ln))
        except Exception:  # noqa: BLE001
            continue
    if not legacy_entries:
        # 旧文件为空也改名，避免反复探测
        try:
            legacy.replace(legacy.with_name("agent_memory.jsonl.migrated"))
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        with _file_lock:
            cur = _read_entries()
            seen = {e.get("id") for e in cur}
            merged = list(cur)
            added = 0
            for e in legacy_entries:
                if e.get("id") and e["id"] in seen:
                    continue
                if not e.get("id"):
                    e["id"] = uuid.uuid4().hex
                if "status" not in e:
                    e["status"] = "active"
                merged.append(e)
                seen.add(e.get("id"))
                added += 1
            if added:
                _write_entries(merged)
        logger.info("记忆迁移：从旧位置并入 %d 条到 %s", added, MEMORY_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆迁移失败（不影响新位置写入）: %s", exc)
        return
    # 迁移成功后把旧文件改名，避免下次启动重复迁移
    try:
        legacy.replace(legacy.with_name("agent_memory.jsonl.migrated"))
    except Exception:  # noqa: BLE001
        pass


_migrate_legacy_memory()
