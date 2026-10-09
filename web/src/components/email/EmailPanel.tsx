import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Mail,
  RefreshCw,
  Power,
  PowerOff,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Radio,
  Timer,
  XCircle,
} from 'lucide-react'
import {
  fetchEmailStatus,
  checkEmailNow,
  updateEmailConfig,
  type EmailStatus,
  type EmailPollItem,
} from '../../lib/api'
import { subscribeEmailEvents } from '../../lib/emailBridge'
import { relativeTime } from '../../lib/format'
import { debugLog } from '../../lib/debug'

/** 把一轮轮询的结果翻译成人话。 */
function describePoll(p: EmailPollItem): string {
  if (p.rate_limited) return `触发限频，已自动退避：${p.error}`
  if (!p.ok) return p.error || '检查失败'
  if (p.note) return p.note
  if (p.processed > 0) return `处理 ${p.processed} 封新邮件`
  if (p.fresh > 0) return `发现 ${p.fresh} 封新邮件（本轮排队）`
  return `无新邮件（收件箱共 ${p.total} 封）`
}

function clockText(ts: number): string {
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 邮件网关面板：邮箱给助手发信 → 系统检测 → 唤醒模型执行 → 自动回信。 */
export function EmailPanel() {
  const [st, setSt] = useState<EmailStatus | null>(null)
  const [polls, setPolls] = useState<EmailPollItem[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [tip, setTip] = useState<string>('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [, forceTick] = useState(0)
  const timer = useRef<number | null>(null)
  // 本地时钟与服务端时钟的偏差（秒），用于倒计时不跑偏
  const skew = useRef(0)

  const reload = useCallback(async () => {
    try {
      const s = await fetchEmailStatus()
      if (s.server_time) skew.current = Date.now() / 1000 - s.server_time
      setSt(s)
      setPolls(s.polls ?? [])
    } catch (e: any) {
      debugLog('email', 'fetchEmailStatus 失败', { error: e?.message }, 'warn')
    } finally {
      setLoading(false)
    }
  }, [])

  // 兜底轮询状态（SSE 为主，这里只防漏）
  useEffect(() => {
    void reload()
    timer.current = window.setInterval(() => void reload(), 20000)
    return () => {
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [reload])

  // 每秒 tick，驱动"下次检查"倒计时
  useEffect(() => {
    const id = window.setInterval(() => forceTick((n) => n + 1), 1000)
    return () => window.clearInterval(id)
  }, [])

  // 实时事件流：每一轮轮询的开始/结束都会推过来（全局桥，面板开关不影响连接）
  useEffect(() => {
    return subscribeEmailEvents((ev) => {
      switch (ev?.type) {
        case 'email_poll_start':
          setSt((p) => (p ? { ...p, polling: true } : p))
          setTip(ev.trigger === 'manual' ? '手动检查中…' : '正在检查新邮件…')
          break
        case 'email_poll_done': {
          const item: EmailPollItem = {
            seq: ev.seq,
            trigger: ev.trigger,
            ts: ev.ts,
            elapsed_ms: ev.elapsed_ms,
            ok: !!ev.ok,
            total: ev.total ?? 0,
            fresh: ev.fresh ?? 0,
            processed: ev.processed ?? 0,
            error: ev.error ?? '',
            note: ev.note ?? '',
            rate_limited: !!ev.rate_limited,
          }
          setPolls((prev) => [...prev.filter((x) => x.seq !== item.seq), item].slice(-20))
          setSt((p) =>
            p
              ? {
                  ...p,
                  polling: false,
                  poll_count: item.seq,
                  last_poll: item.ts,
                  next_poll: ev.next_poll || p.next_poll,
                  last_result: item,
                  last_error: item.ok ? '' : item.error,
                }
              : p,
          )
          setTip(describePoll(item))
          if (item.processed > 0) void reload()
          break
        }
        case 'email_received':
          setTip(`收到新邮件：${ev.subject || ''}`)
          break
        case 'email_agent_start':
          setTip('助手正在执行邮件任务…')
          void reload()
          break
        case 'email_agent_end':
          setTip(ev.replied ? '任务完成，已自动回信 ✓' : '任务完成（未回信）')
          void reload()
          break
        case 'email_replied':
          setTip('已自动回信 ✓')
          void reload()
          break
        case 'email_reply_failed':
          setTip(`回信失败：${ev.error || ''}`)
          break
        default:
          break
      }
    })
  }, [reload])

  async function onToggle() {
    if (!st) return
    setBusy('toggle')
    try {
      const r = await updateEmailConfig({ enabled: !st.enabled })
      setSt(r.status)
      setPolls(r.status.polls ?? [])
      setTip(!st.enabled ? `邮件网关已开启，每 ${r.status.poll_interval}s 检查一次` : '邮件网关已关闭')
    } catch (e: any) {
      setTip(`操作失败：${e?.message ?? e}`)
    } finally {
      setBusy(null)
    }
  }

  async function onCheck() {
    setBusy('check')
    setTip('正在检查新邮件…')
    try {
      const r = await checkEmailNow()
      if (r.status) {
        setSt(r.status)
        setPolls(r.status.polls ?? [])
      }
      if (r.busy) setTip('上一轮检查还没结束，稍等')
      else if (!r.ok) setTip(r.error || '检查失败')
      await reload()
    } catch (e: any) {
      setTip(`检查失败：${e?.message ?? e}`)
    } finally {
      setBusy(null)
    }
  }

  if (loading) {
    return <div className="px-4 py-8 text-center text-sm text-faint">加载中…</div>
  }
  if (!st) {
    return (
      <div className="px-4 py-8 text-center text-sm text-faint">
        无法获取邮件网关状态（后端未连接？）
      </div>
    )
  }

  const nowSec = Date.now() / 1000 - skew.current
  const countdown =
    st.running && st.next_poll ? Math.max(0, Math.round(st.next_poll - nowSec)) : null
  const backoff =
    st.backoff_until && st.backoff_until > nowSec
      ? Math.round(st.backoff_until - nowSec)
      : 0

  return (
    <div className="space-y-3 p-3">
      {/* 状态卡 */}
      <div className="rounded-xl border border-line bg-surface/50 p-3">
        <div className="flex items-center gap-2">
          <Mail size={15} className="text-accent-strong" />
          <span className="flex-1 text-[13.5px] font-medium text-ink">邮件唤醒</span>
          <span
            className={`chip !px-2 !py-0.5 !text-[11px] ${
              st.running ? '!text-emerald-400' : '!text-faint'
            }`}
          >
            {st.running ? '运行中' : st.enabled ? '已启用·未运行' : '已关闭'}
          </span>
        </div>
        <p className="mt-1.5 text-[12px] leading-relaxed text-dim">
          用邮箱给助手发信，系统检测到新邮件就唤醒模型执行并自动回信。
        </p>

        {/* 实时脉冲条：正在检查 / 下次检查倒计时 */}
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-line bg-base/50 px-2.5 py-1.5">
          {st.polling ? (
            <>
              <Radio size={13} className="animate-pulse text-emerald-400" />
              <span className="flex-1 text-[11.5px] text-ink">正在检查收件箱…</span>
            </>
          ) : backoff > 0 ? (
            <>
              <AlertTriangle size={13} className="text-amber-400" />
              <span className="flex-1 text-[11.5px] text-amber-300">
                触发限频，{backoff}s 后重试
              </span>
            </>
          ) : st.running ? (
            <>
              <Timer size={13} className="text-faint" />
              <span className="flex-1 text-[11.5px] text-dim">
                下次检查 <span className="font-mono text-ink">{countdown ?? '—'}s</span> 后
              </span>
            </>
          ) : (
            <>
              <Timer size={13} className="text-faint" />
              <span className="flex-1 text-[11.5px] text-faint">未运行，不会自动检查</span>
            </>
          )}
          <span className="font-mono text-[11px] text-faint">#{st.poll_count}</span>
        </div>

        <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[11.5px] text-dim">
          <div>
            模型：<span className="font-mono text-ink">{st.model}</span>
          </div>
          <div>
            轮询：<span className="text-ink">{st.poll_interval}s</span>
          </div>
          <div>
            自动回信：<span className="text-ink">{st.auto_reply ? '开' : '关'}</span>
          </div>
          <div>
            自动批准：<span className="text-ink">{st.auto_approve ? '开' : '关'}</span>
          </div>
          <div>
            已处理：<span className="text-ink">{st.seen_count}</span>
          </div>
          <div>
            上次检查：
            <span className="text-ink">
              {st.last_poll ? relativeTime(st.last_poll * 1000) : '—'}
            </span>
          </div>
        </div>

        <div className="mt-2.5 flex gap-1.5">
          <button
            onClick={onToggle}
            disabled={busy !== null}
            className={`btn flex-1 py-1.5 text-[12.5px] ${
              st.enabled ? 'btn-ghost' : 'btn-accent'
            }`}
          >
            {st.enabled ? <PowerOff size={13} /> : <Power size={13} />}
            {st.enabled ? '关闭' : '开启'}
          </button>
          <button
            onClick={onCheck}
            disabled={busy !== null || st.polling}
            className="btn btn-ghost flex-1 py-1.5 text-[12.5px]"
          >
            <RefreshCw
              size={13}
              className={busy === 'check' || st.polling ? 'animate-spin' : ''}
            />
            立即检查
          </button>
        </div>
      </div>

      {/* agently-cli 状态 */}
      <div className="rounded-xl border border-line bg-surface/40 p-3">
        <div className="flex items-center gap-2">
          {st.cli_available ? (
            <CheckCircle2 size={14} className="text-emerald-400" />
          ) : (
            <AlertTriangle size={14} className="text-amber-400" />
          )}
          <span className="flex-1 text-[12.5px] text-ink">
            {st.cli_available ? 'agently-cli 已就绪，邮件网关可运行' : '未找到 agently-cli'}
          </span>
        </div>
        {!st.cli_available && (
          <p className="mt-1.5 text-[11.5px] leading-relaxed text-faint">
            请在本机安装并授权官方客户端：<br />
            <code className="text-ink">npm install -g @tencent-qqmail/agently-cli</code>
            <br />
            然后运行 <code className="text-ink">agently-cli auth login</code> 完成 device-code 授权。
          </p>
        )}
      </div>

      {(tip || st.last_error) && (
        <div className="rounded-xl border border-line bg-surface/40 px-3 py-2 text-[11.5px] leading-relaxed text-dim">
          {tip || st.last_error}
        </div>
      )}

      {/* 轮询实时记录 */}
      <div>
        <div className="flex items-center gap-1.5 px-1 pb-1.5">
          <span className="text-[11.5px] font-medium text-faint">检查记录</span>
          <span className="text-[11px] text-faint">（最近 {polls.length} 轮）</span>
        </div>
        {polls.length === 0 ? (
          <div className="rounded-xl border border-line bg-surface/30 px-3 py-5 text-center text-[12px] text-faint">
            {st.running ? '等待第一轮检查…' : '开启网关后这里会显示每一轮检查结果'}
          </div>
        ) : (
          <div className="max-h-64 space-y-1 overflow-y-auto rounded-xl border border-line bg-surface/30 p-1.5">
            {[...polls].reverse().map((p) => (
              <div
                key={p.seq}
                className="flex items-start gap-1.5 rounded-lg px-2 py-1.5 hover:bg-base/40"
              >
                {p.ok ? (
                  p.processed > 0 ? (
                    <Mail size={12} className="mt-0.5 shrink-0 text-accent-strong" />
                  ) : (
                    <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-emerald-500/70" />
                  )
                ) : (
                  <XCircle size={12} className="mt-0.5 shrink-0 text-rose-400" />
                )}
                <div className="min-w-0 flex-1">
                  <div
                    className={`truncate text-[11.5px] ${
                      p.ok ? 'text-dim' : 'text-rose-300'
                    }`}
                  >
                    {describePoll(p)}
                  </div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[10.5px] text-faint">
                    <span className="font-mono">#{p.seq}</span>
                    <span className="font-mono">{clockText(p.ts)}</span>
                    <span>{p.elapsed_ms}ms</span>
                    {p.trigger === 'manual' && (
                      <span className="rounded bg-base/60 px-1 text-[10px]">手动</span>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 邮箱消息列表（完整邮件归档：正文 + 处理状态 + 自动回复） */}
      <div>
        <div className="flex items-center gap-1.5 px-1 pb-1.5">
          <span className="text-[11.5px] font-medium text-faint">邮箱消息列表</span>
          <span className="text-[11px] text-faint">（{st.messages?.length ?? 0} 封）</span>
        </div>
        {(st.messages ?? []).length === 0 ? (
          <div className="rounded-xl border border-line bg-surface/30 px-3 py-6 text-center text-[12px] text-faint">
            还没有收到过邮件
          </div>
        ) : (
          <div className="max-h-96 space-y-1 overflow-y-auto rounded-xl border border-line bg-surface/30 p-1.5">
            {[...(st.messages ?? [])].reverse().map((it) => {
              const open = expanded === it.id
              return (
                <div key={it.id} className="rounded-lg border border-line/60 bg-surface/40 p-2.5">
                  <button
                    onClick={() => setExpanded(open ? null : it.id)}
                    className="flex w-full items-start gap-1.5 text-left"
                  >
                    {open ? (
                      <ChevronDown size={13} className="mt-0.5 shrink-0 text-faint" />
                    ) : (
                      <ChevronRight size={13} className="mt-0.5 shrink-0 text-faint" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate text-[12.5px] text-ink">
                          {it.subject || '(无主题)'}
                        </span>
                        {it.status === 'replied' ? (
                          <span className="chip !px-1.5 !py-0 !text-[10px] !text-emerald-400">
                            已回信
                          </span>
                        ) : it.status === 'failed' ? (
                          <span className="chip !px-1.5 !py-0 !text-[10px] !text-rose-400">
                            回信失败
                          </span>
                        ) : (
                          <span className="chip !px-1.5 !py-0 !text-[10px] !text-skill">
                            已处理
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 truncate text-[11px] text-faint">
                        {it.from} · {relativeTime(it.ts * 1000)}
                      </div>
                      {!open && (
                        <div className="mt-1 truncate text-[11px] text-dim">
                          {it.body || '(空)'}
                        </div>
                      )}
                    </div>
                  </button>
                  {open && (
                    <div className="mt-2 space-y-2">
                      <div>
                        <div className="mb-1 text-[10.5px] font-medium text-faint">邮件内容</div>
                        <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-base/60 p-2 text-[11.5px] leading-relaxed text-dim">
                          {it.body || '(空)'}
                        </pre>
                      </div>
                      <div>
                        <div className="mb-1 flex items-center gap-1.5 text-[10.5px] font-medium text-faint">
                          助手的回复
                          {it.status === 'failed' && it.reply_error && (
                            <span className="text-rose-400">（失败：{it.reply_error}）</span>
                          )}
                        </div>
                        <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-base/60 p-2 text-[11.5px] leading-relaxed text-dim">
                          {it.reply || '(无回复内容)'}
                        </pre>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
