import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  ChevronDown,
  Clock,
  Command,
  Gauge,
  KeyRound,
  Layers,
  PanelLeft,
  PanelRight,
  Settings,
  Sparkles,
} from 'lucide-react'
import { useChatStore } from '../../store/useChatStore'
import { useUIStore } from '../../store/useUIStore'
import { ContextMeter } from '../ContextMeter'

function ModelPicker() {
  const providers = useUIStore((s) => s.providers)
  const provider = useUIStore((s) => s.provider)
  const model = useUIStore((s) => s.model)
  const setProvider = useUIStore((s) => s.setProvider)
  const setModel = useUIStore((s) => s.setModel)
  const setProviderSettingsOpen = useUIStore((s) => s.setProviderSettingsOpen)
  const [open, setOpen] = useState(false)

  const cur = providers.find((p) => p.id === provider)
  const label = cur ? cur.display_name : provider || '未配置接口'
  const configured = !!(cur?.base_url ?? '').trim()
  const modelLabel = model ?? cur?.default_model ?? ''

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="btn-ghost rounded-xl border border-line px-3 py-1.5 text-[13px]"
        title={configured ? '点击切换接口 / 模型' : '尚未配置接口，点击设置'}
      >
        <span className="text-dim">{label}</span>
        <span className="max-w-[200px] truncate font-mono text-ink">
          {modelLabel || (configured ? '—' : '未配置')}
        </span>
        <ChevronDown size={14} className="text-faint" />
      </button>

      <AnimatePresence>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div
              initial={{ opacity: 0, y: -6, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -6, scale: 0.98 }}
              transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
              className="glass-strong absolute right-0 z-50 mt-2 max-h-[60vh] w-80 overflow-y-auto rounded-xl2 p-2"
            >
              {!configured && (
                <div className="mb-2 rounded-lg border border-accent/30 bg-accent/10 px-2.5 py-2 text-[12.5px] text-accent-strong">
                  尚未配置接口。本程序不提供服务商与推理后端，
                  <button
                    className="ml-1 underline"
                    onClick={() => {
                      setOpen(false)
                      setProviderSettingsOpen(true)
                    }}
                  >
                    点此填入接口地址 / 密钥 / 模型名
                  </button>
                </div>
              )}
              {providers.map((p) => (
                <div key={p.id} className="mb-1.5 last:mb-0">
                  <div className="flex items-center justify-between px-2 py-1">
                    <span className="text-[11px] uppercase tracking-wide text-faint">
                      {p.display_name}
                    </span>
                    {!(p.base_url ?? '').trim() && (
                      <span className="chip !px-1.5 !py-0.5 !text-[10px]">未配置</span>
                    )}
                  </div>
                  {(p.models ?? []).length === 0 ? (
                    <div className="px-2.5 py-1.5 text-[12.5px] text-faint">
                      未设置模型名
                    </div>
                  ) : (
                    (p.models ?? []).map((m) => {
                      const selected = p.id === provider && m === model
                      return (
                        <button
                          key={m}
                          onClick={() => {
                            setProvider(p.id)
                            setModel(m)
                            setOpen(false)
                          }}
                          className={`flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left font-mono text-[12.5px] transition-colors ${
                            selected
                              ? 'bg-accent/15 text-accent-strong'
                              : 'text-dim hover:bg-white/5'
                          }`}
                        >
                          <span className="truncate">{m}</span>
                          {p.supports_vision && (
                            <span className="chip shrink-0 !px-1.5 !py-0.5 !text-[10px]">
                              视觉
                            </span>
                          )}
                        </button>
                      )
                    })
                  )}
                </div>
              ))}
              <div className="mt-1 border-t border-line/60 px-2.5 pt-1.5">
                <button
                  className="text-[12px] text-dim hover:text-ink"
                  onClick={() => {
                    setOpen(false)
                    setProviderSettingsOpen(true)
                  }}
                >
                  接口设置…
                </button>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}

function ModeToggle() {
  const mode = useUIStore((s) => s.mode)
  const setMode = useUIStore((s) => s.setMode)

  const toggle = async () => {
    const next = mode === 'full' ? 'minimal' : 'full'
    await setMode(next)
  }

  const isMinimal = mode === 'minimal'

  return (
    <button
      onClick={toggle}
      title={
        isMinimal
          ? '极简模式：精简工具集 + 短提示词（省 token）'
          : '完整模式：全部工具 + 完整提示词'
      }
      className={`btn-ghost flex items-center gap-1.5 rounded-xl border border-line px-2.5 py-1.5 text-[12px] transition-colors ${
        isMinimal
          ? 'border-accent/30 bg-accent/10 text-accent-strong'
          : 'text-dim hover:text-ink'
      }`}
    >
      {isMinimal ? <Gauge size={14} /> : <Layers size={14} />}
      <span className="hidden sm:inline">{isMinimal ? '极简' : '完整'}</span>
    </button>
  )
}

export function TopBar() {
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  const toggleRight = useUIStore((s) => s.toggleRight)
  const setCommandOpen = useUIStore((s) => s.setCommandOpen)
  const setSettingsOpen = useUIStore((s) => s.setSettingsOpen)
  const setProviderSettingsOpen = useUIStore((s) => s.setProviderSettingsOpen)

  const activeId = useChatStore((s) => s.activeId)
  const sessions = useChatStore((s) => s.sessions)
  const session = activeId ? sessions[activeId] : null

  return (
    <header className="flex h-[52px] shrink-0 items-center gap-3 border-b border-line bg-raise/60 px-3">
      <button
        onClick={toggleSidebar}
        title="切换侧栏"
        className="btn-ghost h-9 w-9 rounded-xl p-0"
      >
        <PanelLeft size={18} />
      </button>

      <div className="flex items-center gap-2">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent/15">
          <Sparkles size={15} className="text-accent-strong" />
        </span>
        <span className="text-[15px] font-semibold tracking-tight text-ink">
          MY_AGENT
        </span>
      </div>

      <div className="mx-2 hidden min-w-0 flex-1 items-center sm:flex">
        {session && (
          <span className="truncate text-[13px] text-dim">{session.name}</span>
        )}
      </div>

      <div className="ml-auto flex items-center gap-2">
        <button
          onClick={() => toggleRight('market')}
          title="技能市场"
          className="btn-ghost h-9 rounded-xl px-2.5 text-[13px]"
        >
          <Sparkles size={16} className="text-skill" />
          <span className="hidden md:inline">技能市场</span>
        </button>
        <button
          onClick={() => toggleRight('schedule')}
          title="定时任务"
          className="btn-ghost h-9 rounded-xl px-2.5 text-[13px]"
        >
          <Clock size={16} className="text-accent-strong" />
          <span className="hidden md:inline">定时任务</span>
        </button>
        <ModelPicker />
        <ModeToggle />
        <ContextMeter />
        <button
          onClick={() => setSettingsOpen(true)}
          title="语音设置"
          className="btn-ghost h-9 w-9 rounded-xl p-0"
        >
          <Settings size={17} />
        </button>
        <button
          onClick={() => setProviderSettingsOpen(true)}
          title="模型密钥 / Provider 设置"
          className="btn-ghost h-9 w-9 rounded-xl p-0"
        >
          <KeyRound size={17} />
        </button>
        <button
          onClick={() => setCommandOpen(true)}
          title="命令面板 (⌘K)"
          className="btn-ghost h-9 w-9 rounded-xl p-0"
        >
          <Command size={17} />
        </button>
        <button
          onClick={() => toggleRight()}
          title="上下文面板"
          className="btn-ghost h-9 w-9 rounded-xl p-0"
        >
          <PanelRight size={17} />
        </button>
      </div>

    </header>
  )
}
