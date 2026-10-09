import { motion } from 'framer-motion'
import { Boxes, Cpu, Eye, Sparkles, Clock, Brain, X, Monitor, Mail } from 'lucide-react'
import { useUIStore } from '../../store/useUIStore'
import { SkillMarket } from '../skill/SkillMarket'
import { ScheduledTasks } from '../schedule/ScheduledTasks'
import { MemoryPanel } from '../memory/MemoryPanel'
import { SystemPanel } from '../system/SystemPanel'
import { EmailPanel } from '../email/EmailPanel'

function ToolsTab() {
  const tools = useUIStore((s) => s.tools)
  if (tools.length === 0) {
    return (
      <div className="px-4 py-8 text-center text-sm text-faint">
        暂无工具信息（后端未连接？）
      </div>
    )
  }
  return (
    <div className="space-y-2 p-3">
      {tools.map((t) => (
        <div key={t.name} className="rounded-xl border border-line bg-surface/50 p-3">
          <div className="flex items-center gap-2">
            <Boxes size={14} className="text-accent-strong" />
            <span className="font-mono text-[13px] text-ink">{t.name}</span>
          </div>
          {t.description && (
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-dim">
              {t.description}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}

function ProvidersTab() {
  const providers = useUIStore((s) => s.providers)
  const setProvider = useUIStore((s) => s.setProvider)
  const provider = useUIStore((s) => s.provider)
  if (providers.length === 0) {
    return (
      <div className="px-4 py-8 text-center text-sm text-faint">
        没有可用的 Provider
      </div>
    )
  }
  return (
    <div className="space-y-3 p-3">
      {providers.map((p) => {
        const active = p.id === provider
        return (
          <div
            key={p.id}
            className={`rounded-xl border p-3 transition-colors ${
              active ? 'border-accent/40 bg-accent/10' : 'border-line bg-surface/40'
            }`}
          >
            <button
              onClick={() => setProvider(p.id)}
              className="flex w-full items-center gap-2 text-left"
            >
              <Cpu size={14} className="text-accent-strong" />
              <span className="flex-1 text-[13.5px] font-medium text-ink">
                {p.display_name}
              </span>
              {p.supports_vision && (
                <span className="chip !px-1.5 !py-0.5 !text-[10px]">
                  <Eye size={11} /> 视觉
                </span>
              )}
            </button>
            {p.description && (
              <p className="mt-1.5 text-[12px] leading-relaxed text-dim">
                {p.description}
              </p>
            )}
            <div className="mt-2 flex flex-wrap gap-1.5">
              {p.models.map((m) => (
                <span key={m} className="chip !px-2 !py-0.5 !text-[11px] font-mono">
                  {m}
                </span>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

export function RightPanel() {
  const open = useUIStore((s) => s.rightOpen)
  const tab = useUIStore((s) => s.rightTab)
  const setTab = useUIStore((s) => s.setRightTab)

  if (!open) return null

  return (
    <motion.aside
      initial={{ x: 16, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: 16, opacity: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      className="flex w-[280px] shrink-0 flex-col border-l border-line bg-raise/60"
    >
      <div className="flex items-center gap-1 border-b border-line px-2 py-2">
        <button
          onClick={() => setTab('tools')}
          className={`btn flex-1 py-1.5 text-[13px] ${
            tab === 'tools' ? 'btn-accent' : 'btn-ghost'
          }`}
        >
          <Boxes size={14} /> 工具
        </button>
        <button
          onClick={() => setTab('providers')}
          className={`btn flex-1 py-1.5 text-[13px] ${
            tab === 'providers' ? 'btn-accent' : 'btn-ghost'
          }`}
        >
          <Cpu size={14} /> Provider
        </button>
        <button
          onClick={() => setTab('market')}
          className={`btn flex-1 py-1.5 text-[13px] ${
            tab === 'market' ? 'btn-accent' : 'btn-ghost'
          }`}
        >
          <Sparkles size={14} /> 技能市场
        </button>
        <button
          onClick={() => setTab('schedule')}
          className={`btn flex-1 py-1.5 text-[13px] ${
            tab === 'schedule' ? 'btn-accent' : 'btn-ghost'
          }`}
        >
          <Clock size={14} /> 定时任务
        </button>
        <button
          onClick={() => setTab('memory')}
          className={`btn flex-1 py-1.5 text-[13px] ${
            tab === 'memory' ? 'btn-accent' : 'btn-ghost'
          }`}
        >
          <Brain size={14} /> 记忆
        </button>
        <button
          onClick={() => setTab('system')}
          className={`btn flex-1 py-1.5 text-[13px] ${
            tab === 'system' ? 'btn-accent' : 'btn-ghost'
          }`}
        >
          <Monitor size={14} /> 系统
        </button>
        <button
          onClick={() => setTab('email')}
          className={`btn flex-1 py-1.5 text-[13px] ${
            tab === 'email' ? 'btn-accent' : 'btn-ghost'
          }`}
        >
          <Mail size={14} /> 邮件
        </button>
        <button
          onClick={() => useUIStore.getState().toggleRight()}
          title="关闭"
          className="btn-ghost h-8 w-8 rounded-lg p-0"
        >
          <X size={16} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'tools' ? (
          <ToolsTab />
        ) : tab === 'providers' ? (
          <ProvidersTab />
        ) : tab === 'market' ? (
          <SkillMarket />
        ) : tab === 'memory' ? (
          <MemoryPanel />
        ) : tab === 'system' ? (
          <SystemPanel />
        ) : tab === 'email' ? (
          <EmailPanel />
        ) : (
          <ScheduledTasks />
        )}
      </div>
    </motion.aside>
  )
}
