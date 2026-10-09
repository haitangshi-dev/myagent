"""邮件网关：QQ 邮箱来信 → 唤醒助手（Agent）→ 自动回信。

设计要点
--------
1. **底层用官方 agently-cli**：<你的 agent 邮箱> 这枚「平台 agent 邮箱」的官方客户端
   是腾讯 @tencent-qqmail 出品的 `agently-cli`（与 MY_AGENT 里装的 agently-mail skill 同款）。
   鉴权走 agent.qq.com 的 device-code OAuth，凭据由 CLI 存于 Windows DPAPI 并自动续期——
   MY_AGENT 完全不需要自己管 token，也不依赖 WorkBuddy 连接器。
2. **调用方式**：直接用 `node <agently-cli 入口> <子命令>` 子进程，不经过 cmd /c，
   避免邮件正文里的 & | > 等字符被 shell 误解析（已实测：带 & | 的正文能原样送达）。
3. **两阶段确认自动完成**：reply 需两步（先拿 confirmation_token，再带 token 重发）。网关在无人
   值守下程序化自动确认——第一阶段取 ctk，第二阶段带 ctk 重发（用户已在网关开关里预授权 auto_reply）。
4. **速率自觉**：CLI 受 10 次/分钟、200 次/小时、发信 50 封/天 限制。轮询 30s（=120 次/小时 list）
   已接近小时配额，故内置**自适应退避**：一旦识别到限频（429/rate limit/exit 7），自动拉长间隔，
   恢复后回落，避免把配额打爆。
5. **幂等**：已处理邮件 id 落盘 data/email_gateway.json，重启不重复回信。
6. **无人值守**：调 Agent 强制 auto_approve；但 agent_loop 的 protected_block_reason 硬安全网仍生效。
7. **可观测**：每一轮轮询都产出真实反馈——`email_poll_start` / `email_poll_done` 事件经 SSE 推给前端，
   含序号、拉取数、新邮件数、处理数、耗时、错误、下次轮询时间；status() 同时回吐最近 20 轮历史。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from agents.data_dir import resolve_data_path

logger = logging.getLogger(__name__)

STATE_FILE = "email_gateway.json"
_MAX_SEEN = 500
AGENTLY_PKG = "@tencent-qqmail/agently-cli"


# =========================================================================
# agently-cli 定位
# =========================================================================
def _find_node() -> str | None:
    p = shutil.which("node")
    if p:
        return p
    for cand in (
        "C:/Program Files/nodejs/node.exe",
        "C:/Program Files (x86)/nodejs/node.exe",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def resolve_cli() -> tuple[str, str] | None:
    """定位 agently-cli 的 (node 路径, 入口 js 路径)。找不到返回 None。

    优先级：全局 npm 包目录（npm install -g 的落点）→ PATH 上的 agently-cli(.cmd)。
    走 node 直调入口 scripts/run.js，完全避开 cmd /c，正文特殊字符安全。
    """
    node = _find_node()
    if not node:
        return None

    roots: list[Path] = []
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if base:
            roots.append(Path(base) / "npm" / "node_modules" / AGENTLY_PKG)
    for root in roots:
        pkg = root / "package.json"
        if not pkg.is_file():
            continue
        try:
            meta = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        bin_rel = (meta.get("bin") or {}).get("agently-cli") or meta.get("main") or "scripts/run.js"
        entry = root / bin_rel
        if entry.is_file():
            return node, str(entry)

    # 回退：PATH 上的 agently-cli（.cmd 或无后缀）
    for name in ("agently-cli.cmd", "agently-cli"):
        p = shutil.which(name)
        if p:
            return node, p
    return None


def _extract_json(text: str) -> dict | list | None:
    """从 CLI 混合输出（JSON + 末尾 tip: 行）里抠出 JSON 对象/数组。"""
    s = text.find("{")
    if s == -1:
        s = text.find("[")
    e = max(text.rfind("}"), text.rfind("]"))
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        return json.loads(text[s : e + 1])
    except Exception:  # noqa: BLE001
        return None


# =========================================================================
# agently-cli 封装（node 直调）
# =========================================================================
def _write_body_file(body: str) -> str:
    """把邮件正文写入临时 UTF-8 文件，返回绝对路径。

    为什么必须走文件：agently-cli 的 --body 参数一旦含中文/撇号/特殊字符，
    两阶段确认（先拿 confirmation_token 再重发）时 body 不一致会导致令牌
    匹配失败。正文写入 UTF-8 文件、两阶段都引用同一文件，彻底规避该问题。
    """
    import tempfile

    fd, path = tempfile.mkstemp(prefix="myagent_mail_body_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body or "")
    return path


class AgentlyCLI:
    """封装 agently-cli 子进程调用。所有参数以真实 argv 传给 node，无 shell 解释。

    正文一律走 --body-file（UTF-8 临时文件），两阶段确认自动完成——
    对外表现为「一步发送」，模型/网关无需关心 confirmation_token。
    """

    def __init__(self, node: str, entry: str, timeout: float = 60.0):
        self.node = node
        self.entry = entry
        self.timeout = timeout

    async def _run(self, args: list[Any]) -> dict:
        cmd = [self.node, self.entry] + [str(a) for a in args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError("agently-cli 调用超时")
        except FileNotFoundError as exc:
            raise RuntimeError(f"找不到 node/CLI：{exc}")
        text = (stdout or b"").decode("utf-8", "replace")
        data = _extract_json(text)
        if not isinstance(data, dict):
            err = (stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"agently-cli 输出无法解析：{err[:200]}")
        return data

    async def me(self) -> dict:
        out = await self._run(["+me"])
        if not out.get("ok"):
            raise RuntimeError("+me 失败")
        return out.get("data") or {}

    async def list_messages(self, limit: int = 10) -> list[dict]:
        out = await self._run(["message", "+list", "--dir", "inbox", "--limit", str(limit)])
        if not out.get("ok"):
            raise RuntimeError(f"list 失败：{out.get('error') or (out.get('data') or {}).get('message')}")
        return (out.get("data") or {}).get("data") or []

    async def read(self, mid: str) -> dict:
        out = await self._run(["message", "+read", "--id", mid])
        if not out.get("ok"):
            raise RuntimeError(f"read 失败：{out.get('error') or (out.get('data') or {}).get('message')}")
        return out.get("data") or {}

    async def _send_two_phase(self, args: list[Any], body: str, what: str) -> tuple[bool, str]:
        """两阶段确认自动完成（一步发送）：正文写 UTF-8 文件，两阶段引用同一文件。

        args 为已组装好的基础参数（不含 body/token）；返回 (ok, error)。
        """
        body_file = _write_body_file(body)
        # agently-cli 的 --body-file 只接受相对路径（绝对路径直接报
        # "--body-file must be a relative path"）。子进程继承父进程 cwd，
        # 故相对 os.getcwd() 的路径能被 CLI 正确解析。
        try:
            body_arg = os.path.relpath(body_file, os.getcwd())
        except ValueError:
            body_arg = body_file  # 跨盘符极端情况兜底（同盘场景不会触发）
        base = list(args) + ["--body-file", body_arg]
        try:
            out1 = await self._run(base)
            d1 = (out1.get("data") or {}) if isinstance(out1, dict) else {}
            ctk = d1.get("confirmation_token")
            if not ctk:
                if isinstance(out1, dict) and out1.get("ok"):
                    return True, ""
                return False, str(out1.get("error") or d1.get("message") or f"{what}未返回确认令牌")
            out2 = await self._run(base + ["--confirmation-token", ctk])
            if isinstance(out2, dict) and out2.get("ok"):
                return True, ""
            d2 = (out2.get("data") or {}) if isinstance(out2, dict) else {}
            return False, str(out2.get("error") or d2.get("message") or f"{what}确认失败")
        finally:
            try:
                os.remove(body_file)
            except OSError:
                pass

    async def reply(self, mid: str, body: str, reply_all: bool = False) -> tuple[bool, str]:
        """一步回复（两阶段自动确认，正文走文件防中文/撇号令牌失配）。"""
        base = ["message", "+reply", "--id", mid]
        if reply_all:
            base.append("--reply-all")
        return await self._send_two_phase(base, body, "回复")

    async def send(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str] | None = None,
    ) -> tuple[bool, str]:
        """一步发送新邮件（两阶段自动确认，正文走文件）。"""
        if not to:
            return False, "缺少收件人"
        base = ["message", "+send"]
        for addr in to:
            base += ["--to", addr]
        if cc:
            for addr in cc:
                base += ["--cc", addr]
        if bcc:
            for addr in bcc:
                base += ["--bcc", addr]
        if subject:
            base += ["--subject", subject]
        if attachments:
            for p in attachments:
                base += ["--attachment", p]
        return await self._send_two_phase(base, body, "发送")


# =========================================================================
# 状态持久化
# =========================================================================
def _state_path() -> Path:
    return resolve_data_path(STATE_FILE)


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {"seen": [], "token": "", "last_poll": 0, "log": [], "messages": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("seen", [])
            data.setdefault("token", "")
            data.setdefault("last_poll", 0)
            data.setdefault("log", [])
            data.setdefault("messages", [])
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("邮件网关状态读取失败：%s", exc)
    return {"seen": [], "token": "", "last_poll": 0, "log": [], "messages": []}


def _save_state(state: dict[str, Any]) -> None:
    try:
        state["seen"] = list(state.get("seen", []))[-_MAX_SEEN:]
        state["log"] = list(state.get("log", []))[-50:]
        state["messages"] = list(state.get("messages", []))[-200:]
        _state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("邮件网关状态写入失败：%s", exc)


def save_token(token: str) -> None:
    """保留接口兼容 /api/email/set-token；当前网关用 agently-cli 自管凭据，此值不再被使用。"""
    st = _load_state()
    st["token"] = (token or "").strip()
    _save_state(st)


# =========================================================================
# 字段提取（CLI 返回里 from 是 {email,name} 结构，做宽松取值）
# =========================================================================
def _sender_of(m: dict) -> str:
    frm = m.get("from")
    if isinstance(frm, dict):
        return str(frm.get("email", "") or "")
    if isinstance(frm, str):
        return frm
    return ""


def _msg_id(m: dict) -> str:
    return str(m.get("message_id") or m.get("id") or "")


def _pick(d: dict, *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


# =========================================================================
# 网关主体
# =========================================================================
class EmailGateway:
    """轮询 <你的 agent 邮箱> → 新邮件唤醒 Agent → 自动回信 + 推送到 App。"""

    def __init__(
        self,
        settings: dict[str, Any],
        provider_manager,
        system_prompt: str,
        broadcaster: Callable[[dict], Any] | None = None,
    ):
        self._settings = settings or {}
        self._pm = provider_manager
        self._system_prompt = system_prompt
        self._broadcast = broadcaster
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_error: str = ""
        self._me: dict[str, Any] = {}
        # —— 实时反馈用的运行时状态（内存，重启清零；last_poll 仍落盘） ——
        self._lock = asyncio.Lock()          # 防「轮询」与「立即检查」并发重入
        self._poll_seq = 0                   # 轮询序号（本次进程内自增）
        self._polls: list[dict[str, Any]] = []  # 最近 N 轮真实结果
        self._last_result: dict[str, Any] = {}
        self._next_poll_at = 0.0             # 下一轮预计开始（epoch 秒），供前端倒计时
        self._polling = False                # 当前是否正在跑一轮
        self._backoff_until = 0.0            # 限频退避截止时间
        self._rate_hits = 0                  # 连续命中限频次数

    # ---------------- 配置 ----------------
    @property
    def cfg(self) -> dict[str, Any]:
        return self._settings.get("email", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    def _cli(self) -> AgentlyCLI | None:
        resolved = resolve_cli()
        if not resolved:
            self._last_error = (
                "未找到 agently-cli：请先 `npm install -g @tencent-qqmail/agently-cli` 并完成 "
                "`agently-cli auth login` 授权（device-code 走 agent.qq.com）。"
            )
            return None
        node, entry = resolved
        return AgentlyCLI(node, entry, timeout=float(self.cfg.get("timeout", 60)))

    # ---------------- 生命周期 ----------------
    @property
    def interval(self) -> int:
        """当前轮询间隔（秒）。每轮实时读配置，运行时改 poll_interval 立刻生效。"""
        return max(15, int(self.cfg.get("poll_interval", 30) or 30))

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("邮件网关已启动（轮询间隔 %ss）", self.interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(3)
        while self._running:
            interval = self.interval
            # 限频退避：命中过 429/rate limit 时按退避时间等待，不打爆小时配额
            now = time.time()
            if self._backoff_until > now:
                wait = max(1.0, self._backoff_until - now)
                self._next_poll_at = self._backoff_until
                await asyncio.sleep(wait)
                continue
            try:
                await self.poll_once(trigger="auto")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("邮件网关轮询异常：%s", exc)
            self._next_poll_at = time.time() + interval
            await asyncio.sleep(interval)

    # ---------------- 轮询包装（负责实时反馈） ----------------
    @staticmethod
    def _is_rate_limited(text: str) -> bool:
        t = (text or "").lower()
        return any(
            k in t
            for k in ("rate limit", "ratelimit", "429", "too many requests", "限频", "频繁", "exit 7")
        )

    async def poll_once(self, trigger: str = "manual") -> dict[str, Any]:
        """跑一轮轮询，并把真实结果（含耗时/数量/错误）推给前端。

        trigger: auto=定时轮询 / manual=前端「立即检查」。
        """
        if self._lock.locked():
            return {"ok": False, "error": "上一轮检查尚未结束", "processed": 0, "busy": True}

        async with self._lock:
            self._poll_seq += 1
            seq = self._poll_seq
            started = time.time()
            self._polling = True
            await self._emit({
                "type": "email_poll_start",
                "seq": seq,
                "trigger": trigger,
                "ts": int(started),
            })
            try:
                result = await self._poll_core()
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc), "processed": 0}
            finally:
                self._polling = False

            elapsed = int((time.time() - started) * 1000)
            err = str(result.get("error") or "")

            # 限频自适应：命中就指数退避（最长 10 分钟），成功则复位
            if err and self._is_rate_limited(err):
                self._rate_hits = min(self._rate_hits + 1, 5)
                backoff = min(600, self.interval * (2 ** self._rate_hits))
                self._backoff_until = time.time() + backoff
                result["rate_limited"] = True
                result["backoff"] = backoff
                logger.warning("邮件网关命中限频，退避 %ss", backoff)
            elif result.get("ok"):
                self._rate_hits = 0
                self._backoff_until = 0.0

            if self._running:
                # 立刻重排下一轮时间（手动检查也顺延），保证推给前端的倒计时是准的
                self._next_poll_at = (
                    self._backoff_until
                    if self._backoff_until > time.time()
                    else time.time() + self.interval
                )

            entry = {
                "seq": seq,
                "trigger": trigger,
                "ts": int(started),
                "elapsed_ms": elapsed,
                "ok": bool(result.get("ok")),
                "total": int(result.get("total") or 0),
                "fresh": int(result.get("fresh") or 0),
                "processed": int(result.get("processed") or 0),
                "error": err,
                "note": str(result.get("note") or ""),
                "rate_limited": bool(result.get("rate_limited")),
            }
            self._polls.append(entry)
            self._polls = self._polls[-20:]
            self._last_result = entry

            await self._emit({
                "type": "email_poll_done",
                **entry,
                "next_poll": int(self._next_poll_at or 0),
            })
            result["seq"] = seq
            result["elapsed_ms"] = elapsed
            return result

    # ---------------- 核心轮询 ----------------
    async def _poll_core(self) -> dict[str, Any]:
        cli = self._cli()
        if not cli:
            return {"ok": False, "error": self._last_error, "processed": 0}

        state = _load_state()
        seen_list: list[str] = [str(x) for x in state.get("seen", [])]
        seen: set[str] = set(seen_list)

        def _mark(mid: str) -> None:
            if mid and mid not in seen:
                seen.add(mid)
                seen_list.append(mid)

        # 首次需要 +me 拿 alias 信息
        if not self._me:
            try:
                self._me = await cli.me() or {}
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"+me 失败：{exc}"
                return {"ok": False, "error": self._last_error, "processed": 0}

        limit = int(self.cfg.get("fetch_limit", 10) or 10)
        try:
            msgs = await cli.list_messages(limit)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"list 失败：{exc}"
            return {"ok": False, "error": self._last_error, "processed": 0}

        state["last_poll"] = int(time.time())

        # 首次运行：只记录基线，不回历史邮件
        if not state.get("seen") and not state.get("bootstrapped"):
            state["seen"] = [_msg_id(m) for m in msgs if _msg_id(m)]
            state["bootstrapped"] = True
            _save_state(state)
            self._last_error = ""
            return {
                "ok": True,
                "processed": 0,
                "fresh": 0,
                "note": f"首次运行已建立基线（记录 {len(msgs)} 封历史邮件，不回信）",
                "total": len(msgs),
            }

        my_addr = (self.cfg.get("my_address") or "").strip().lower()
        allow = [a.strip().lower() for a in (self.cfg.get("allow_senders") or []) if a.strip()]
        max_n = int(self.cfg.get("max_process_per_poll", 2) or 2)

        fresh = []
        for m in msgs:
            mid = _msg_id(m)
            if not mid or mid in seen:
                continue
            sender = _sender_of(m).lower()
            if my_addr and my_addr in sender:
                _mark(mid)  # 自己发的（含自动回信）不触发，防自问自答死循环
                continue
            if allow and not any(a in sender for a in allow):
                _mark(mid)
                continue
            fresh.append(m)

        fresh_total = len(fresh)
        fresh = fresh[:max_n]
        processed = 0
        for m in fresh:
            mid = _msg_id(m)
            try:
                await self._handle_message(cli, m)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("处理邮件 %s 失败：%s", mid, exc)
                self._last_error = f"处理邮件失败：{exc}"
            finally:
                _mark(mid)

        latest = _load_state()
        latest["seen"] = seen_list
        latest["last_poll"] = state["last_poll"]
        latest["bootstrapped"] = True
        _save_state(latest)
        self._last_error = "" if processed or not fresh else self._last_error
        return {
            "ok": True,
            "processed": processed,
            "fresh": fresh_total,
            "total": len(msgs),
        }

    # ---------------- 单封邮件处理 ----------------
    async def _handle_message(self, cli: AgentlyCLI, msg: dict[str, Any]) -> None:
        mid = _msg_id(msg)
        detail: dict[str, Any] = msg
        try:
            got = await cli.read(mid)
            if isinstance(got, dict):
                detail = {**msg, **got}
        except Exception as exc:  # noqa: BLE001
            logger.debug("read(%s) 失败，退化用列表摘要：%s", mid, exc)

        subject = str(_pick(detail, "subject", "title", default="(无主题)"))
        sender = _sender_of(detail) or str(_pick(detail, "sender", "from_address", default="(未知发件人)"))
        body = str(_pick(detail, "body", "text", "content", "snippet", "preview", default=""))
        body = body.strip()[:8000]

        await self._emit({
            "type": "email_received",
            "message_id": mid,
            "from": sender,
            "subject": subject,
            "preview": body[:200],
        })

        prompt = (
            "【邮件唤醒】你收到了用户通过邮件发来的指令。\n"
            f"发件人：{sender}\n"
            f"主题：{subject}\n"
            f"正文：\n{body or '(空)'}\n\n"
            "请理解并执行用户的意图（可以使用你的全部工具：读写文件、执行命令、联网搜索等）。"
            "完成后用中文写一段简洁的结果回复，这段回复会作为邮件正文自动回给用户，"
            "所以请写成完整可读的答复，不要只写「好的」。"
        )

        # 通知前端：邮件任务开始 → 前端在 chat 界面创建「📧 邮件会话」，
        # 把邮件正文作为 user 消息、模型输出流式渲染为 assistant 消息（与用户自己输入一致）。
        await self._emit({
            "type": "email_agent_start",
            "message_id": mid,
            "session_id": f"email:{mid}",
            "from": sender,
            "subject": subject,
            "body": body,
        })

        reply_text = await self._run_agent(prompt, mid)

        replied = False
        reply_err = ""
        if self.cfg.get("auto_reply", True) and reply_text.strip():
            try:
                ok, err = await cli.reply(mid, reply_text, reply_all=False)
                if ok:
                    replied = True
                    await self._emit({"type": "email_replied", "message_id": mid})
                else:
                    reply_err = err
                    logger.warning("自动回信失败（%s）：%s", mid, err)
                    await self._emit({"type": "email_reply_failed", "message_id": mid, "error": err})
            except Exception as exc:  # noqa: BLE001
                reply_err = str(exc)
                logger.warning("自动回信异常（%s）：%s", mid, exc)
                await self._emit({"type": "email_reply_failed", "message_id": mid, "error": str(exc)})

        # 通知前端：邮件任务结束（含回信结果）→ 前端把 assistant 消息标 done + 附回信状态
        await self._emit({
            "type": "email_agent_end",
            "message_id": mid,
            "session_id": f"email:{mid}",
            "reply": reply_text[:2000],
            "replied": replied,
            "reply_error": reply_err,
        })

        st = _load_state()
        st.setdefault("log", []).append({
            "ts": int(time.time()),
            "id": mid,
            "from": sender,
            "subject": subject,
            "reply": reply_text[:500],
        })
        # 完整邮件消息归档（供前端「邮箱消息列表」展示）
        st.setdefault("messages", [])
        st["messages"] = [m for m in st.get("messages", []) if m.get("id") != mid]
        st["messages"].append({
            "ts": int(time.time()),
            "id": mid,
            "from": sender,
            "subject": subject,
            "body": body,
            "status": "replied" if replied else ("failed" if reply_err else "processed"),
            "reply": reply_text[:2000],
            "reply_error": reply_err,
        })
        _save_state(st)

    # ---------------- 调 Agent ----------------
    async def _run_agent(self, prompt: str, mid: str) -> str:
        from agents.agent_loop import Agent

        provider = self.cfg.get("provider") or self._settings.get("default_provider", "custom")
        model = self.cfg.get("model") or self._settings.get("default_model") or None
        client = self._pm.get_client(provider, model)

        limit = None
        try:
            pcfg = (self._settings.get("providers", {}) or {}).get(provider, {}) or {}
            limit = pcfg.get("context_window") or None
        except Exception:  # noqa: BLE001
            pass

        # 兜底客户端：主接口连续异常/噪声时自动切换到「其它已配置的接口」
        # （本项目不内置服务商，用户只配一个接口时此处置空，仍保留单接口自身重试）
        fallback_client = None
        try:
            for cand, ccfg in (self._settings.get("providers", {}) or {}).items():
                if cand == provider or not (ccfg.get("base_url") or "").strip():
                    continue
                fmodel = ccfg.get("default_model") or (ccfg.get("models") or [None])[0]
                if not fmodel:
                    continue
                fallback_client = self._pm.get_client(cand, fmodel)
                break
        except Exception as exc:  # noqa: BLE001
            logger.debug("邮件任务兜底客户端构建失败：%s", exc)

        agent = Agent(
            client,
            self._system_prompt,
            context_limit=limit,
            settings=self._settings,
            fallback_client=fallback_client,
            # 全工具 + 主动记忆（与主聊天一致）
            eager_memory=True,
            auto_approve=bool(self.cfg.get("auto_approve", True)),
            # ★ 修复（2026-08-06）：thinking 从 False 改为 None——
            #   与前端 /api/chat 一致，保留接口默认思考行为（推理链可见、处理质量相同）。
            #   历史原因（思考链吃光 max_tokens 返回空串）由 agent_loop 的
            #   空输出/噪声防护兜底，不再在网关层禁用思考。
            thinking=None,
        )

        buf: list[str] = []
        sid = f"email:{mid}"

        async def emit(ev: dict) -> None:
            t = ev.get("type")
            if t in ("content", "content_delta"):
                buf.append(str(ev.get("text", "")))
            # 全事件透传前端（content/reasoning/tool_call/tool_result/usage/message_end/error/approval…）
            # 前端据此在「📧 邮件会话」里像普通聊天一样流式渲染。
            await self._emit({
                "type": "email_agent_event",
                "message_id": mid,
                "session_id": sid,
                "event": ev,
            })

        await agent.run([], prompt, emit, ctx={"kind": "chat", "session_id": sid}, session_id=sid)
        text = "".join(buf).strip()
        return text or "（助手这次没有产出文本回复）"

    # ---------------- 事件广播 ----------------
    async def _emit(self, ev: dict) -> None:
        if not self._broadcast:
            return
        try:
            r = self._broadcast(ev)
            if asyncio.iscoroutine(r):
                await r
        except Exception as exc:  # noqa: BLE001
            logger.debug("邮件事件广播失败：%s", exc)

    # ---------------- 状态 ----------------
    def status(self) -> dict[str, Any]:
        st = _load_state()
        running = bool(self._task and not self._task.done())
        return {
            "enabled": self.enabled,
            "running": running,
            "cli_available": bool(resolve_cli()),
            "provider": self.cfg.get("provider") or self._settings.get("default_provider", "custom"),
            "model": self.cfg.get("model") or self._settings.get("default_model") or "",
            "poll_interval": self.interval,
            "auto_reply": bool(self.cfg.get("auto_reply", True)),
            "auto_approve": bool(self.cfg.get("auto_approve", True)),
            "last_poll": st.get("last_poll", 0),
            "seen_count": len(st.get("seen", [])),
            "last_error": self._last_error,
            "recent": list(st.get("log", []))[-10:],
            "messages": list(st.get("messages", []))[-200:],
            "me": self._me,
            # —— 实时反馈 ——
            "polling": self._polling,
            "poll_count": self._poll_seq,
            "next_poll": int(self._next_poll_at) if running else 0,
            "last_result": self._last_result,
            "polls": list(self._polls)[-20:],
            "backoff_until": int(self._backoff_until) if self._backoff_until else 0,
            "server_time": int(time.time()),
        }
