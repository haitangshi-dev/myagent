import { useChatStore } from '../store/useChatStore'
import { useUIStore } from '../store/useUIStore'

function fmtTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`
  return `${n}`
}

// TopBar 的上下文占用可视化：显示当前对话已用 token / 模型上下文窗口。
// 数据来自后端最近一次 usage 事件（prompt_tokens = 本轮流式请求实际消耗的输入上下文）。
// 阈值配色：<70% 翡翠绿（正常）、70%~90% 琥珀（偏高）、≥90% 红（逼近上限）。
export function ContextMeter() {
  const usage = useChatStore((s) => s.contextUsage)
  const provider = useUIStore((s) => s.provider)
  const providers = useUIStore((s) => s.providers)

  const limit = providers.find((p) => p.id === provider)?.context_window ?? null
  // 未知上限（如 custom provider 未配 context_window）→ 不显示
  if (!limit || limit <= 0) return null

  const used = usage?.prompt_tokens ?? 0
  const pct = Math.min(100, (used / limit) * 100)
  const color = pct >= 90 ? '#f87171' : pct >= 70 ? '#fbbf24' : '#34d399'

  return (
    <div
      className="hidden items-center gap-2 md:flex"
      title={
        `上下文占用：${used.toLocaleString()} / ${limit.toLocaleString()} tokens（${pct.toFixed(1)}%）` +
        (usage ? `　·　本次输出 ${usage.completion_tokens.toLocaleString()} tokens` : '')
      }
    >
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-white/10">
        <div
          className="h-full rounded-full transition-[width] duration-500 ease-out"
          style={{ width: `${pct}%`, backgroundColor: color }}
        />
      </div>
      <span className="font-mono text-[11px] tabular-nums text-dim">
        {fmtTokens(used)}/{fmtTokens(limit)}
      </span>
    </div>
  )
}
