"""定时任务调度器 — 后台 asyncio 循环，按 when 触发 Agent 自动执行任务。

when 语法（大小写/中英文不敏感）：
  - "in 30m" / "in 1h" / "in 2h30m"        相对：从现在起 N 分钟/小时（一次性）
  - "every 30m" / "every 1h" / "every 2h"  周期：每隔 N 分钟/小时（到点重排下次）
  - "daily 09:00" / "daily 21:30"          每天固定时刻
  - "weekly mon 09:00" / "weekly 周一 09:00" 每周固定星期+时刻
  - "cron 0 9 * * *"                       标准 5 段 cron（分 时 日 月 周）

结果（状态 + 输出预览）保存在 data/schedules.json 的 history 里，
前端「定时任务」面板读取并展示；后台完成也会 broadcast scheduled_run 事件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agents import data_dir, events

logger = logging.getLogger(__name__)

# 定时任务落盘位置：必须放在安装目录之外，否则重装 / 卸载会清空。
# 优先级见 data_dir._resolve_data_base（env MY_AGENT_DATA_DIR → 用户数据目录 → 项目根兜底）。
_DATA_FILE = data_dir.resolve_data_path("schedules.json")


def _merge_schedules(new: dict, old: dict) -> dict:
    """合并两个定时任务对象：以 job id 去重，旧数据补全新位置缺失的任务。"""
    new_jobs = {j.get("id"): j for j in (new.get("jobs") or []) if j.get("id")}
    for j in old.get("jobs") or []:
        jid = j.get("id")
        if jid and jid not in new_jobs:
            new_jobs[jid] = j
    return {"jobs": list(new_jobs.values())}


def _migrate_legacy_schedules() -> None:
    """首次导入时把旧安装目录内的 data/schedules.json 并入用户数据目录（幂等 + 异常安全）。"""
    old_file = data_dir._legacy_data_root() / "schedules.json"
    data_dir._maybe_migrate_file(old_file, _DATA_FILE, _merge_schedules)


_migrate_legacy_schedules()

_WEEKDAYS = {
    "mon": 0, "monday": 0, "周一": 0, "星期一": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "周二": 1, "星期二": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "周三": 2, "星期三": 2,
    "thu": 3, "thurs": 3, "thursday": 3, "周四": 3, "星期四": 3,
    "fri": 4, "friday": 4, "周五": 4, "星期五": 4,
    "sat": 5, "saturday": 5, "周六": 5, "星期六": 5,
    "sun": 6, "sunday": 6, "周日": 6, "星期日": 6,
}


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


# =========================================================================
# when 解析
# =========================================================================
def parse_when(when: str) -> dict:
    """把 when 字符串解析成结构化元数据。解析失败抛 ValueError。"""
    s = (when or "").strip().lower()

    # 相对 / 周期：in 30m / every 1h
    m = re.match(r"^(in|every)\s+(\d+h)?\s*(\d+m)?\s*(\d+s)?$", s)
    if m:
        kind = m.group(1)
        h = int(m.group(2)[:-1]) if m.group(2) else 0
        mi = int(m.group(3)[:-1]) if m.group(3) else 0
        sec = int(m.group(4)[:-1]) if m.group(4) else 0
        delta = timedelta(hours=h, minutes=mi, seconds=sec)
        if delta.total_seconds() <= 0:
            raise ValueError("相对/周期时间必须为正")
        # compute_next / reschedule 用 relative|interval 判别，这里归一化
        return {"kind": "relative" if kind == "in" else "interval", "delta": delta}

    # daily 09:00
    m = re.match(r"^daily\s+(\d{1,2}):(\d{2})$", s)
    if m:
        return {"kind": "daily", "h": int(m.group(1)), "m": int(m.group(2))}

    # weekly mon 09:00
    m = re.match(r"^weekly\s+([a-z\u4e00-\u9fa5]+)\s+(\d{1,2}):(\d{2})$", s)
    if m:
        wd = _WEEKDAYS.get(m.group(1))
        if wd is None:
            raise ValueError(f"未知星期：{m.group(1)}")
        return {"kind": "weekly", "wd": wd, "h": int(m.group(2)), "m": int(m.group(3))}

    # cron 0 9 * * *
    m = re.match(r"^cron\s+(.+)$", s)
    if m:
        cron = _parse_cron(m.group(1).strip())
        return {"kind": "cron", "cron": cron}

    raise ValueError(f"无法解析触发规则：{when}")


def _parse_cron(expr: str) -> dict:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron 需要 5 段：分 时 日 月 周")
    minute = _cron_field(parts[0], 0, 59)
    hour = _cron_field(parts[1], 0, 23)
    dom = _cron_field(parts[2], 1, 31)
    mon = _cron_field(parts[3], 1, 12)
    dow_raw = _cron_field(parts[4], 0, 7)
    dow = {0 if v == 7 else v for v in dow_raw}  # 7 视为周日
    dom_star = parts[2].strip() == "*"
    dow_star = parts[4].strip() == "*"
    return {
        "minute": minute, "hour": hour, "dom": dom, "mon": mon, "dow": dow,
        "dom_star": dom_star, "dow_star": dow_star,
    }


def _cron_field(field: str, lo: int, hi: int) -> set[int]:
    result: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            rng, step_s = part.split("/", 1)
            step = int(step_s)
            part = rng if rng != "*" else f"{lo}-{hi}"
        if part == "*":
            part = f"{lo}-{hi}"
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
        else:
            a = b = int(part)
        for v in range(a, b + 1, step):
            if lo <= v <= hi:
                result.add(v)
    return result


def _next_daily(base: datetime, h: int, m: int) -> datetime:
    cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= base:
        cand += timedelta(days=1)
    return cand


def _next_weekly(base: datetime, wd: int, h: int, m: int) -> datetime:
    cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
    days_ahead = (wd - cand.weekday()) % 7
    if days_ahead == 0 and cand <= base:
        days_ahead = 7
    return cand + timedelta(days=days_ahead)


def _next_cron(base: datetime, cron: dict) -> datetime | None:
    cand = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    minute, hour, dom, mon, dow = (
        cron["minute"], cron["hour"], cron["dom"], cron["mon"], cron["dow"],
    )
    dom_star, dow_star = cron["dom_star"], cron["dow_star"]
    for _ in range(2_000_000):
        if (
            cand.minute in minute
            and cand.hour in hour
            and cand.month in mon
            # ★ 修复（2026-08-07，事故 R-01）：标准 cron 语义——
            #   dom 与 dow 同时受限时「任一命中即可」（或）；受限项必须命中。
            #   先求 day_ok（dom/dow 或逻辑），再并入整体条件。
        ):
            if not dom_star and not dow_star:
                # dom 与 dow 都受限：任一命中即可（标准 cron「或」）
                day_ok = (cand.day in dom) or (cand.weekday() in dow)
            elif not dom_star:
                # 仅 dom 受限
                day_ok = cand.day in dom
            elif not dow_star:
                # 仅 dow 受限
                day_ok = cand.weekday() in dow
            else:
                day_ok = True
            if day_ok:
                return cand
        cand += timedelta(minutes=1)
    return None


def compute_next(meta: dict, base: datetime) -> datetime | None:
    """根据已解析的 when 元数据，算出 base 之后的下一次执行时间。"""
    kind = meta["kind"]
    if kind == "relative":
        return base + meta["delta"]
    if kind == "interval":
        return base + meta["delta"]
    if kind == "daily":
        return _next_daily(base, meta["h"], meta["m"])
    if kind == "weekly":
        return _next_weekly(base, meta["wd"], meta["h"], meta["m"])
    if kind == "cron":
        return _next_cron(base, meta["cron"])
    return None


# =========================================================================
# Job 模型
# =========================================================================
class Job:
    def __init__(self, data: dict[str, Any]):
        self.id = data.get("id") or _uid()
        self.name = data.get("name") or "未命名任务"
        self.prompt = data.get("prompt", "")
        self.when = data.get("when", "")
        self.provider = data.get("provider")
        self.model = data.get("model")
        self.enabled = bool(data.get("enabled", True))
        self.created_at = data.get("created_at", _now())
        self.last_run = data.get("last_run")
        self.last_status = data.get("last_status")
        self.next_run = data.get("next_run")
        self.history: list[dict[str, Any]] = data.get("history", []) or []
        # 解析 when（失败则标记为无效、禁用）
        try:
            self.meta = parse_when(self.when)
        except ValueError as exc:
            self.meta = {"kind": "invalid"}
            self._parse_error = str(exc)
            self.enabled = False
            logger.warning("定时任务 %s 规则无效：%s", self.id, exc)
        self._recompute_next_if_needed()

    def _recompute_next_if_needed(self) -> None:
        if self.next_run:
            return
        if self.meta.get("kind") in (None, "invalid"):
            return
        nr = compute_next(self.meta, datetime.now())
        self.next_run = _iso(nr) if nr else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "when": self.when,
            "provider": self.provider,
            "model": self.model,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "next_run": self.next_run,
            "history": self.history,
            "parse_error": getattr(self, "_parse_error", None),
        }

    def reschedule(self, base: datetime) -> None:
        """触发完成后重排下一次；一次性（relative）任务则禁用。"""
        kind = self.meta.get("kind")
        if kind == "relative":
            self.enabled = False
            self.next_run = None
        else:
            nr = compute_next(self.meta, base)
            self.next_run = _iso(nr) if nr else None


# =========================================================================
# 调度器（模块级单例）
# =========================================================================
class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[Job] = []
        self._task: asyncio.Task | None = None
        self._pm = None
        self._settings: dict[str, Any] | None = None
        self._system_prompt = "你是 AI Agent。"
        self._running = False
        self._lock = asyncio.Lock()

    # ---- 初始化（在 FastAPI startup 时调用）----
    def init(self) -> None:
        from config import load_settings
        from providers.manager import ProviderManager

        self._settings = load_settings()
        self._pm = ProviderManager(self._settings)
        self._system_prompt = self._settings.get("agent", {}).get(
            "system_prompt", "你是 AI Agent。"
        )
        self._load()

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("定时任务调度器已启动（%d 个任务）", len(self._jobs))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("scheduler tick 异常: %s", exc)
            await asyncio.sleep(15)

    async def _tick(self) -> None:
        now = datetime.now()
        for job in list(self._jobs):
            if not job.enabled or not job.next_run:
                continue
            nr = _parse_iso(job.next_run)
            if not nr or nr > now:
                continue
            # ★ R-03 错过检测：任务本应执行但已错过（阻塞/宕机导致）→ 记录「未执行」
            #   并跳过本次补跑（避免积压任务瞬间全跑），回执会写明错过。
            grace = timedelta(minutes=10)
            if now - nr > grace:
                logger.warning("定时任务 %s（%s）错过触发：计划 %s，当前 %s", job.id, job.name, nr, now)
                job.last_status = "missed"
                job.history.append(
                    {
                        "run_at": _iso(nr),
                        "status": "missed",
                        "preview": f"错过触发（计划 {_iso(nr)}，当前 {_iso(now)}），未执行",
                    }
                )
                job.history = job.history[-20:]
                job.reschedule(now)
                self._save()
                try:
                    await self._send_receipt(job, "（未执行）错过触发，已跳过本次")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("错过回执发送失败: %s", exc)
                continue
            # ★ R-02 隔离：不再整体持锁串行——每个任务独立启动，死循环/超时任务
            #   只影响自身，不堵后续任务。asyncio.create_task 并发执行。
            try:
                asyncio.create_task(self._run_job_guarded(job, nr))
            except Exception as exc:  # noqa: BLE001
                logger.exception("任务 %s 启动失败: %s", job.id, exc)

    async def _run_job_guarded(self, job: Job, when: datetime) -> None:
        """单任务守卫执行：超时终止 + 状态落盘 + 回执（R-02 超时/隔离/告警核心）。"""
        timeout_s = int(self._get_task_timeout())
        try:
            await asyncio.wait_for(self._fire(job, when), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.error("定时任务 %s（%s）执行超时（>%ss），已强制终止", job.id, job.name, timeout_s)
            job.last_status = "timeout"
            job.history.append(
                {
                    "run_at": _iso(when),
                    "status": "timeout",
                    "preview": f"执行超时（>{timeout_s}s）已强制终止",
                }
            )
            job.history = job.history[-20:]
            self._save()
            try:
                await self._send_receipt(job, f"（超时）执行超过 {timeout_s} 秒被强制终止")
            except Exception as exc:  # noqa: BLE001
                logger.warning("超时回执发送失败: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务 %s 守卫异常: %s", job.id, exc)

    def _get_task_timeout(self) -> int:
        """任务超时秒数：scheduler.task_timeout > email.timeout > 默认 600。"""
        try:
            sched_cfg = (self._settings or {}).get("scheduler", {}) or {}
            v = int(sched_cfg.get("task_timeout", 0) or 0)
            if v > 0:
                return v
            email_cfg = (self._settings or {}).get("email", {}) or {}
            v = int(email_cfg.get("timeout", 0) or 0)
            if v > 0:
                return v * 10  # 邮件超时（单次 CLI 调用）*10 作为任务整体超时
        except Exception:  # noqa: BLE001
            pass
        return 600

    # ---- 触发执行 ----
    async def _fire(self, job: Job, now: datetime) -> None:
        job.last_run = _now()
        preview = ""
        try:
            preview = await self._run_agent(job)
            job.last_status = "success"
        except Exception as exc:  # noqa: BLE001
            logger.exception("定时任务 %s 执行失败", job.id)
            job.last_status = "error"
            preview = f"执行出错：{exc}"[:600]
        job.history.append(
            {"run_at": job.last_run, "status": job.last_status, "preview": preview[:600]}
        )
        job.history = job.history[-20:]  # 仅保留最近 20 次
        job.reschedule(now)
        self._save()
        await events.broadcast(
            {
                "type": "scheduled_run",
                "job_id": job.id,
                "status": job.last_status,
                "preview": preview[:600],
            }
        )
        # ★ 定时任务回执闭环（P2-8）：执行后自动写记忆 + 发邮件通知，不靠模型记性
        try:
            await self._send_receipt(job, preview)
        except Exception as exc:  # noqa: BLE001
            logger.warning("定时任务回执失败（%s）: %s", job.id, exc)

    async def _send_receipt(self, job: Job, preview: str) -> None:
        """定时任务执行回执：写一条全局记忆 + 向 report_to 发一封回执邮件。

        两者都是 best-effort：失败仅记日志，不影响任务主流程。
        """
        settings = self._settings or {}
        email_cfg = settings.get("email", {}) or {}
        if not email_cfg.get("report_on_schedule", True):
            return
        status_cn = "成功" if job.last_status == "success" else "失败"
        title = f"定时任务「{job.name}」执行{status_cn}"
        # 1) 写记忆（全局记忆，带标签便于检索）
        try:
            from agents import global_memory as _gmem

            _gmem.add(
                f"{title}（{job.last_run}）。任务：{job.prompt[:200]}。结果摘要：{preview[:300]}",
                tags=["scheduled", job.last_status or "done"],
                source="auto",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("回执写记忆失败: %s", exc)
        # 2) 发邮件回执（走 email_send 同款一步发送：正文文件+两阶段自动确认）
        report_to = str(email_cfg.get("report_to") or "").strip()
        if not report_to:
            return
        try:
            from agents.email_gateway import AgentlyCLI, resolve_cli

            resolved = resolve_cli()
            if not resolved:
                logger.debug("回执邮件跳过：未找到 agently-cli")
                return
            node, entry = resolved
            cli = AgentlyCLI(node, entry)
            body = (
                f"定时任务执行回执\n\n"
                f"任务：{job.name}\n"
                f"状态：{status_cn}\n"
                f"执行时间：{job.last_run}\n\n"
                f"结果摘要：\n{preview[:1000] or '(无摘要)'}\n\n"
                f"（本邮件由 MY_AGENT 定时任务自动发送）"
            )
            ok, err = await cli.send(to=[report_to], subject=title, body=body)
            if ok:
                logger.info("定时任务回执邮件已发送 → %s", report_to)
            else:
                logger.warning("定时任务回执邮件发送失败: %s", err)
        except Exception as exc:  # noqa: BLE001
            logger.warning("定时任务回执邮件异常: %s", exc)

    async def _run_agent(self, job: Job) -> str:
        from agents.agent_loop import Agent

        provider = job.provider or self._settings.get("default_provider", "custom")
        try:
            client = self._pm.get_client(provider, job.model)
        except (KeyError, ValueError):
            client = self._pm.get_client(
                self._settings.get("default_provider", "custom"), None
            )
        # 上下文压缩上限（与 server/api.py 同策略：auto_compress > provider.context_window > 客户端默认）
        limit = None
        ac_val = int((self._settings.get("auto_compress", {}) or {}).get("max_context_tokens", 0) or 0)
        if ac_val > 0:
            limit = ac_val
        else:
            cfg = (self._settings.get("providers", {}) or {}).get(provider) or {}
            cw = cfg.get("context_window")
            if cw:
                limit = int(cw)
            else:
                limit = getattr(client, "context_limit", None)
        agent = Agent(
            client,
            self._system_prompt,
            context_limit=limit,
            settings=self._settings,
        )
        buf: list[str] = []

        async def emit(ev: dict[str, Any]) -> None:
            if ev.get("type") == "content":
                buf.append(ev.get("text", ""))

        await agent.run(
            [],
            job.prompt,
            emit,
            ctx={"kind": "chat", "session_id": f"scheduled:{job.id}"},
            session_id=f"scheduled:{job.id}",
        )
        return "".join(buf).strip()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not _DATA_FILE.exists():
            self._jobs = []
            return
        try:
            data = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
            self._jobs = [Job(d) for d in (data.get("jobs", []) or [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取定时任务失败：%s", exc)
            self._jobs = []

    def _save(self) -> None:
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DATA_FILE.write_text(
            json.dumps(
                {"jobs": [j.to_dict() for j in self._jobs]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # ---- CRUD ----
    def add_job(self, data: dict[str, Any]) -> Job:
        if not data.get("prompt"):
            raise ValueError("缺少 prompt")
        if not data.get("when"):
            raise ValueError("缺少 when")
        job = Job(
            {
                "id": _uid(),
                "name": data.get("name") or "定时任务",
                "prompt": data["prompt"],
                "when": data["when"],
                "provider": data.get("provider"),
                "model": data.get("model"),
                "enabled": True,
                "created_at": _now(),
                "history": [],
            }
        )
        if job.meta.get("kind") == "invalid":
            raise ValueError(job._parse_error or "触发规则无效")
        # 计算首次 next_run
        nr = compute_next(job.meta, datetime.now())
        job.next_run = _iso(nr) if nr else None
        self._jobs.append(job)
        self._save()
        return job

    def list_jobs(self) -> list[Job]:
        return self._jobs

    def get_job(self, job_id: str) -> Job | None:
        return next((j for j in self._jobs if j.id == job_id), None)

    def update_job(self, job_id: str, fields: dict[str, Any]) -> Job | None:
        job = self.get_job(job_id)
        if not job:
            return None
        if "name" in fields:
            job.name = fields["name"]
        if "prompt" in fields:
            job.prompt = fields["prompt"]
        if "when" in fields and fields["when"] != job.when:
            job.when = fields["when"]
            try:
                job.meta = parse_when(job.when)
                job._parse_error = None  # type: ignore[attr-defined]
                job._recompute_next_if_needed()
            except ValueError as exc:
                job.meta = {"kind": "invalid"}
                job._parse_error = str(exc)  # type: ignore[attr-defined]
                job.enabled = False
                job.next_run = None
        if "provider" in fields:
            job.provider = fields["provider"]
        if "model" in fields:
            job.model = fields["model"]
        if "enabled" in fields:
            job.enabled = bool(fields["enabled"])
            if job.enabled and not job.next_run and job.meta.get("kind") != "invalid":
                nr = compute_next(job.meta, datetime.now())
                job.next_run = _iso(nr) if nr else None
        self._save()
        return job

    def delete_job(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j.id != job_id]
        changed = len(self._jobs) != before
        if changed:
            self._save()
        return changed

    async def run_now(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            return {"ok": False, "error": "任务不存在"}
        now = datetime.now()
        await self._fire(job, now)
        return {"ok": True, "job": job.to_dict()}


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
