import { Activity, Square } from 'lucide-react'
import { useChatStore } from '../../store/useChatStore'
import { useUIStore } from '../../store/useUIStore'
import { formatLatency, formatTokens } from '../../lib/format'
import { StatusMascot } from '../StatusMascot'

export function StatusBar() {
  const activeId = useChatStore((s) => s.activeId)
  const sessions = useChatStore((s) => s.sessions)
  const streaming = useChatStore((s) => s.streaming)
  const stop = useChatStore((s) => s.stop)

  const provider = useUIStore((s) => s.provider)
  const model = useUIStore((s) => s.model)
  const health = useUIStore((s) => s.health)
  const providers = useUIStore((s) => s.providers)

  const providerName =
    providers.find((p) => p.id === provider)?.display_name ?? provider

  const session = activeId ? sessions[activeId] : null
  const lastAssistant = [...(session?.messages ?? [])]
    .reverse()
    .find((m) => m.role === 'assistant' && m.usage)
  const usage = lastAssistant?.usage

  const online = health?.status === 'ok'

  return (
    <div className="flex h-7 shrink-0 items-center gap-4 border-t border-line bg-raise/70 px-4 text-[11px] text-faint">
      <span className="flex items-center gap-1.5">
        <span
          className={`h-1.5 w-1.5 rounded-full ${
            online ? 'bg-accent animate-pulse-soft' : 'bg-danger'
          }`}
        />
        {online ? '在线' : '离线'}
        <StatusMascot />
        {health && health.latency >= 0 && (
          <span className="font-mono text-faint/80">
            {formatLatency(health.latency)}
          </span>
        )}
      </span>

      <span className="h-3 w-px bg-line" />

      <span className="truncate">
        {providerName}
        {model && <span className="text-faint/70"> · {model}</span>}
      </span>

      {usage && (
        <>
          <span className="h-3 w-px bg-line" />
          <span className="font-mono">
            {formatTokens(usage.prompt_tokens)}↑ ·{' '}
            {formatTokens(usage.completion_tokens)}↓
          </span>
        </>
      )}

      <div className="ml-auto flex items-center gap-3">
        {streaming ? (
          <button
            onClick={stop}
            className="flex items-center gap-1 rounded-md px-2 py-0.5 text-danger transition-colors hover:bg-danger/10"
          >
            <Square size={11} className="fill-current" />
            停止
          </button>
        ) : (
          <span className="flex items-center gap-1">
            <Activity size={11} />
            就绪
          </span>
        )}
      </div>
    </div>
  )
}
