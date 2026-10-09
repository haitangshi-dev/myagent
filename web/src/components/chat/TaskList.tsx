import { useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  Circle,
  Loader2,
  CheckCircle2,
  XCircle,
  Ban,
  MinusCircle,
  ListChecks,
} from 'lucide-react'
import { useChatStore } from '../../store/useChatStore'
import { useTaskStore } from '../../store/useTaskStore'
import type { TaskItem, TaskStatus } from '../../lib/types'

function StatusIcon({ status }: { status: TaskStatus | string }) {
  switch (status) {
    case 'doing':
    case 'running':
      return <Loader2 size={14} className="animate-spin text-accent-strong shrink-0" />
    case 'done':
      return <CheckCircle2 size={14} className="text-accent-strong shrink-0" />
    case 'failed':
      return <XCircle size={14} className="text-[rgb(var(--danger))] shrink-0" />
    case 'blocked':
      return <Ban size={14} className="text-[rgb(var(--warn))] shrink-0" />
    case 'skipped':
      return <MinusCircle size={14} className="text-faint shrink-0" />
    default:
      return <Circle size={14} className="text-faint shrink-0" />
  }
}

function statusLabel(s: TaskStatus | string): string {
  switch (s) {
    case 'doing':
    case 'running':
      return '进行中'
    case 'done':
      return '已完成'
    case 'failed':
      return '失败'
    case 'blocked':
      return '受阻'
    case 'skipped':
      return '已跳过'
    default:
      return '待办'
  }
}

export function TaskList() {
  const activeId = useChatStore((s) => s.activeId)
  const tasks = useTaskStore((s) => (activeId ? s.boards[activeId] : undefined))
  const [open, setOpen] = useState(true)

  if (!activeId || !tasks || tasks.length === 0) return null

  const done = tasks.filter((t) => t.status === 'done').length
  const running = tasks.filter((t) => t.status === 'running' || t.status === 'doing').length
  const pct = Math.round((done / tasks.length) * 100)

  const list: TaskItem[] = tasks

  return (
    <div className="mx-4 mb-2 rounded-xl border border-line bg-raise/70 px-3 py-2.5">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
      >
        {open ? (
          <ChevronDown size={15} className="text-dim shrink-0" />
        ) : (
          <ChevronRight size={15} className="text-dim shrink-0" />
        )}
        <ListChecks size={15} className="text-accent-strong shrink-0" />
        <span className="text-[13px] font-medium text-ink">任务清单</span>
        <span className="chip !px-2 !py-0.5 !text-[10px]">
          {done}/{tasks.length}
        </span>
        {running > 0 && (
          <span className="chip !px-2 !py-0.5 !text-[10px] !text-accent-strong">
            <Loader2 size={11} className="animate-spin" /> {running} 进行中
          </span>
        )}
        {/* 进度条 */}
        <div className="ml-auto h-1.5 w-20 overflow-hidden rounded-full bg-surface-strong">
          <div
            className="h-full rounded-full bg-accent transition-all duration-300"
            style={{ width: `${pct}%` }}
          />
        </div>
      </button>

      {open && (
        <ul className="mt-2 space-y-1 border-t border-line pt-2">
          {list.map((t) => (
            <li
              key={t.id}
              className={`flex items-start gap-2 rounded-lg px-1.5 py-1 text-[12.5px] ${
                t.status === 'done' ? 'text-dim' : 'text-ink'
              }`}
            >
              <span className="mt-0.5">
                <StatusIcon status={t.status} />
              </span>
              <div className="min-w-0 flex-1">
                <div
                  className={
                    t.status === 'done' ? 'line-through opacity-70' : ''
                  }
                >
                  {t.title}
                </div>
                {t.note && (
                  <div className="mt-0.5 text-[11.5px] leading-relaxed text-faint">
                    {t.note}
                  </div>
                )}
              </div>
              <span className="shrink-0 text-[10.5px] text-faint">
                {statusLabel(t.status)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
