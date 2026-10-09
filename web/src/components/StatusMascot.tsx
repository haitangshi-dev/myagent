import { useMemo } from 'react'
import { useChatStore } from '../store/useChatStore'
import { useUIStore } from '../store/useUIStore'

// 状态吉祥物：把当前会话流 + 后端连接状态派生为「助手」化身表情 + 详细动作，
// 让用户在主窗口随时能看出模型在做什么（而非只看到「思考中」占位）。
type MState =
  | 'idle' // 待命
  | 'thinking' // 深度思考中（reasoning 流式）
  | 'replying' // 生成回复中（content 流式）
  | 'tooling' // 调用工具中
  | 'approval' // 等待用户审批
  | 'error' // 出错
  | 'connecting' // 模型连接重试中（被限流 / 网络抖动），区别于「真在思考」
  | 'reconnecting' // 后端掉线重连中

const EMOJI: Record<MState, string> = {
  idle: '🤖',
  thinking: '💡',
  replying: '✍️',
  tooling: '🛠️',
  approval: '⏳',
  error: '⚠️',
  connecting: '📡',
  reconnecting: '🔄',
}

const COLOR: Record<MState, string> = {
  idle: 'text-faint',
  thinking: 'text-accent',
  replying: 'text-accent-strong',
  tooling: 'text-dim',
  approval: 'text-accent',
  error: 'text-danger',
  connecting: 'text-accent',
  reconnecting: 'text-accent',
}

export function StatusMascot() {
  const activeId = useChatStore((s) => s.activeId)
  const sessions = useChatStore((s) => s.sessions)
  const streaming = useChatStore((s) => s.streaming)
  const modelRetrying = useChatStore((s) => s.modelRetrying)
  const reconnecting = useUIStore((s) => s.reconnecting)

  const { state, label, detail } = useMemo(() => {
    // 重连中优先级最高：后端掉线时其它状态都无意义
    if (reconnecting)
      return {
        state: 'reconnecting' as MState,
        label: '重连中…',
        detail: '连接后端失败，正在重试…',
      }

    // 模型连接重试（被限流 / 网络抖动）：明确显示「连接模型中」，
    // 与「真在思考」区分开，让用户一眼看出不是卡死而是正在重连模型。
    if (modelRetrying)
      return {
        state: 'connecting' as MState,
        label: '连接模型中…',
        detail: modelRetrying.isRateLimit
          ? `模型限流，重连中（第 ${modelRetrying.attempt} 次）`
          : `连接模型失败，重连中（第 ${modelRetrying.attempt} 次）`,
      }

    const session = activeId ? sessions[activeId] : null
    const last = [...(session?.messages ?? [])]
      .reverse()
      .find((m) => m.role === 'assistant')
    if (!last) return { state: 'idle' as MState, label: '待命中', detail: '' }
    if (last.status === 'error')
      return { state: 'error' as MState, label: '出错了', detail: last.error || '' }
    if (!streaming) return { state: 'idle' as MState, label: '待命中', detail: '' }

    // 等待审批（如 delete_file 需用户确认）
    const pendingApproval = (last.approvals ?? []).find(
      (a) => a.status === 'pending' || !a.status,
    )
    if (pendingApproval)
      return {
        state: 'approval' as MState,
        label: '等待你确认',
        detail: pendingApproval.tool || '',
      }

    // 正在跑的工具：显示具体工具名 + 命令/预览，让「调了什么」即时可见
    const running = (last.toolCalls ?? []).find((t) => t.status === 'running')
    if (running)
      return {
        state: 'tooling' as MState,
        label: `调用 ${running.label}`,
        detail: running.command || running.preview || '',
      }

    // 深度思考（reasoning 阶段，尚无正文）
    if (last.reasoning && !last.content)
      return {
        state: 'thinking' as MState,
        label: '深度思考中',
        detail: `已推理 ${last.reasoning.length} 字…`,
      }

    // 正文生成中
    if (last.content)
      return {
        state: 'replying' as MState,
        label: '生成回复中',
        detail: `已生成 ${last.content.length} 字…`,
      }

    return { state: 'thinking' as MState, label: '思考中', detail: '' }
  }, [activeId, sessions, streaming, modelRetrying, reconnecting])

  // 思考 / 工具 / 重连 / 连接模型时轻微脉动，强调「进行中」
  const animate =
    state === 'thinking' ||
    state === 'tooling' ||
    state === 'connecting' ||
    state === 'reconnecting'
      ? 'animate-pulse-soft'
      : ''

  return (
    <div
      className="flex max-w-[300px] items-center gap-1.5"
      title={detail ? `${label}：${detail}` : label}
    >
      <span
        className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent/10 ${animate}`}
      >
        <span className="text-[12px] leading-none">{EMOJI[state]}</span>
      </span>
      <span className={`shrink-0 text-[11px] font-medium ${COLOR[state]}`}>
        {label}
      </span>
      {detail && (
        <span className="truncate text-[11px] text-faint/70">{detail}</span>
      )}
    </div>
  )
}
