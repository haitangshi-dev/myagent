import { useEffect, useState } from 'react'
import {
  Clock,
  Play,
  Trash2,
  Plus,
  Power,
  PowerOff,
  ChevronDown,
  ChevronRight,
  AlertTriangle,
} from 'lucide-react'
import { useUIStore } from '../../store/useUIStore'
import {
  fetchSchedules,
  createSchedule,
  deleteSchedule,
  updateSchedule,
  runScheduleNow,
  type ScheduleJob,
} from '../../lib/api'
import { relativeTime } from '../../lib/format'
import { debugLog } from '../../lib/debug'

export function ScheduledTasks() {
  const [jobs, setJobs] = useState<ScheduleJob[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({
    name: '',
    prompt: '',
    when: '',
    provider: '',
  })
  const [expanded, setExpanded] = useState<string | null>(null)
  const providers = useUIStore((s) => s.providers)

  async function reload() {
    try {
      setJobs(await fetchSchedules())
    } catch (e: any) {
      debugLog('schedule', 'fetchSchedules 失败', { error: e?.message }, 'warn')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function onCreate() {
    if (!form.prompt.trim() || !form.when.trim()) return
    setBusy('__new__')
    try {
      await createSchedule({
        name: form.name.trim() || '定时任务',
        prompt: form.prompt.trim(),
        when: form.when.trim(),
        provider: form.provider || null,
        model: null,
      })
      setForm({ name: '', prompt: '', when: '', provider: '' })
      setShowForm(false)
      await reload()
    } catch (e: any) {
      alert(`创建失败：${e?.message || e}`)
    } finally {
      setBusy(null)
    }
  }

  async function onToggle(j: ScheduleJob) {
    setBusy(j.id)
    try {
      await updateSchedule(j.id, { enabled: !j.enabled })
      await reload()
    } finally {
      setBusy(null)
    }
  }

  async function onRun(j: ScheduleJob) {
    setBusy(j.id)
    try {
      await runScheduleNow(j.id)
      await reload()
    } catch (e: any) {
      alert(`执行失败：${e?.message || e}`)
    } finally {
      setBusy(null)
    }
  }

  async function onDelete(j: ScheduleJob) {
    if (!confirm(`确认删除定时任务「${j.name}」？`)) return
    setBusy(j.id)
    try {
      await deleteSchedule(j.id)
      await reload()
    } finally {
      setBusy(null)
    }
  }

  if (loading) {
    return <div className="px-4 py-8 text-center text-sm text-faint">加载中…</div>
  }

  return (
    <div className="flex flex-col gap-3 p-3">
      <button
        onClick={() => setShowForm((v) => !v)}
        className="btn btn-ghost w-full border border-line"
      >
        <Plus size={14} /> 新建定时任务
      </button>

      {showForm && (
        <div className="space-y-2 rounded-xl border border-line bg-surface/40 p-3">
          <input
            className="field"
            placeholder="名称（可选）"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <textarea
            className="field min-h-[64px] resize-y"
            placeholder="要定时执行的指令 / 需求…"
            value={form.prompt}
            onChange={(e) => setForm({ ...form, prompt: e.target.value })}
          />
          <input
            className="field"
            placeholder="触发规则：in 30m / every 1h / daily 09:00 / weekly mon 09:00 / cron 0 9 * * *"
            value={form.when}
            onChange={(e) => setForm({ ...form, when: e.target.value })}
          />
          <select
            className="field"
            value={form.provider}
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
          >
            <option value="">默认 Provider</option>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name}
              </option>
            ))}
          </select>
          <div className="flex gap-2">
            <button
              onClick={onCreate}
              disabled={busy === '__new__' || !form.prompt.trim() || !form.when.trim()}
              className="btn btn-accent flex-1 disabled:opacity-40"
            >
              {busy === '__new__' ? '创建中…' : '创建'}
            </button>
            <button
              onClick={() => setShowForm(false)}
              className="btn btn-ghost flex-1"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {jobs.length === 0 ? (
        <div className="px-4 py-8 text-center text-sm text-faint">
          还没有定时任务。
          <br />
          可在此新建，或直接让助手「每天 9 点帮我…」。
        </div>
      ) : (
        jobs.map((j) => (
          <div
            key={j.id}
            className="rounded-xl border border-line bg-surface/40 p-3"
          >
            <div className="flex items-start gap-2">
              <button
                onClick={() => setExpanded((v) => (v === j.id ? null : j.id))}
                className="mt-0.5 text-dim"
              >
                {expanded === j.id ? (
                  <ChevronDown size={14} />
                ) : (
                  <ChevronRight size={14} />
                )}
              </button>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[13.5px] font-medium text-ink">
                    {j.name}
                  </span>
                  {j.enabled ? (
                    <span className="chip !px-1.5 !py-0.5 !text-[10px] !text-accent-strong">
                      启用
                    </span>
                  ) : (
                    <span className="chip !px-1.5 !py-0.5 !text-[10px] !text-faint">
                      已停用
                    </span>
                  )}
                </div>
                <p className="mt-0.5 truncate text-[12px] text-dim">{j.prompt}</p>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-faint">
                  <span className="inline-flex items-center gap-1">
                    <Clock size={11} /> {j.when}
                  </span>
                  {j.next_run && (
                    <span>下次：{relativeTime(new Date(j.next_run).getTime())}</span>
                  )}
                  {j.last_status && (
                    <span
                      className={
                        j.last_status === 'success'
                          ? 'text-accent-strong'
                          : 'text-[rgb(var(--danger))]'
                      }
                    >
                      上次：
                      {j.last_status === 'success' ? '成功' : '失败'}
                      {j.last_run ? ` · ${relativeTime(new Date(j.last_run).getTime())}` : ''}
                    </span>
                  )}
                </div>
                {j.parse_error && (
                  <div className="mt-1 inline-flex items-center gap-1 text-[11px] text-[rgb(var(--warn))]">
                    <AlertTriangle size={11} /> 规则无效：{j.parse_error}
                  </div>
                )}
              </div>
            </div>

            <div className="mt-2 flex items-center gap-1.5">
              <button
                onClick={() => onRun(j)}
                disabled={busy === j.id}
                className="btn-ghost inline-flex h-7 items-center gap-1 rounded-lg px-2 text-[12px]"
                title="立即运行一次"
              >
                <Play size={12} /> 运行
              </button>
              <button
                onClick={() => onToggle(j)}
                disabled={busy === j.id}
                className="btn-ghost inline-flex h-7 items-center gap-1 rounded-lg px-2 text-[12px]"
                title={j.enabled ? '停用' : '启用'}
              >
                {j.enabled ? <Power size={12} /> : <PowerOff size={12} />}
                {j.enabled ? '停用' : '启用'}
              </button>
              <button
                onClick={() => onDelete(j)}
                disabled={busy === j.id}
                className="btn-ghost inline-flex h-7 items-center gap-1 rounded-lg px-2 text-[12px] text-[rgb(var(--danger))]"
                title="删除"
              >
                <Trash2 size={12} /> 删除
              </button>
            </div>

            {expanded === j.id && j.history.length > 0 && (
              <div className="mt-2 space-y-1 border-t border-line pt-2">
                {j.history
                  .slice()
                  .reverse()
                  .map((h, i) => (
                    <div key={i} className="text-[11.5px]">
                      <div className="flex items-center gap-2 text-faint">
                        <span
                          className={
                            h.status === 'success'
                              ? 'text-accent-strong'
                              : 'text-[rgb(var(--danger))]'
                          }
                        >
                          {h.status === 'success' ? '成功' : '失败'}
                        </span>
                        <span>{relativeTime(new Date(h.run_at).getTime())}</span>
                      </div>
                      {h.preview && (
                        <p className="mt-0.5 line-clamp-2 text-dim">{h.preview}</p>
                      )}
                    </div>
                  ))}
              </div>
            )}
          </div>
        ))
      )}
    </div>
  )
}
