import { ShieldAlert } from 'lucide-react'
import type { ApprovalInfo } from '../../lib/types'
import { useChatStore } from '../../store/useChatStore'

export function ApprovalCard({
  approval,
  sessionId,
  messageId,
}: {
  approval: ApprovalInfo
  sessionId: string
  messageId: string
}) {
  const approve = useChatStore((s) => s.approve)
  const status = approval.status ?? 'pending'
  const pending = status === 'pending'

  return (
    <div className="rounded-xl border border-warn/40 bg-warn/5">
      <div className="flex items-start gap-3 px-3.5 py-3">
        <ShieldAlert size={18} className="mt-0.5 shrink-0 text-warn" />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-ink">
            需要确认：{approval.tool}
          </div>
          {approval.path && (
            <div className="mt-1 truncate font-mono text-[12px] text-dim">
              {String(approval.path)}
            </div>
          )}
          {approval.description && (
            <div className="mt-1 text-[12.5px] text-dim">{String(approval.description)}</div>
          )}
          <div className="mt-1 text-[11px] text-faint">
            该操作已暂停，等待你的决定（5 分钟未处理将自动拒绝）。
          </div>
        </div>
      </div>
      <div className="flex gap-2 border-t border-warn/20 px-3.5 py-2.5">
        {pending ? (
          <>
            <button
              onClick={() => approve(sessionId, messageId, approval.approval_id, 'approve')}
              className="btn-accent flex-1 py-1.5"
            >
              批准执行
            </button>
            <button
              onClick={() => approve(sessionId, messageId, approval.approval_id, 'deny')}
              className="btn-danger flex-1 py-1.5"
            >
              拒绝
            </button>
          </>
        ) : (
          <div
            className={`w-full rounded-lg py-1.5 text-center text-[13px] font-medium ${
              status === 'approved'
                ? 'bg-accent/10 text-accent-strong'
                : 'bg-danger/10 text-danger'
            }`}
          >
            {status === 'approved' ? '已批准，继续执行' : '已拒绝'}
          </div>
        )}
      </div>
    </div>
  )
}
