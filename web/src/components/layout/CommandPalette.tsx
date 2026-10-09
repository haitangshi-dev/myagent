import { useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  CornerDownLeft,
  MessageSquarePlus,
  PanelLeft,
  Search,
  Sparkles,
  Clock,
  Trash2,
  Wrench,
  Settings,
} from 'lucide-react'
import { useChatStore } from '../../store/useChatStore'
import { useUIStore } from '../../store/useUIStore'

interface Cmd {
  id: string
  title: string
  group: string
  icon: typeof Search
  run: () => void
}

export function CommandPalette() {
  const open = useUIStore((s) => s.commandOpen)
  const setOpen = useUIStore((s) => s.setCommandOpen)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  const toggleRight = useUIStore((s) => s.toggleRight)
  const setProvider = useUIStore((s) => s.setProvider)
  const setSettingsOpen = useUIStore((s) => s.setSettingsOpen)
  const providers = useUIStore((s) => s.providers)

  const newSession = useChatStore((s) => s.newSession)
  const clearSession = useChatStore((s) => s.clearSession)
  const selectSession = useChatStore((s) => s.selectSession)
  const order = useChatStore((s) => s.order)
  const sessions = useChatStore((s) => s.sessions)
  const activeId = useChatStore((s) => s.activeId)

  const [query, setQuery] = useState('')
  const [sel, setSel] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const commands = useMemo<Cmd[]>(() => {
    const list: Cmd[] = [
      {
        id: 'new',
        title: '新会话',
        group: '会话',
        icon: MessageSquarePlus,
        run: () => newSession(),
      },
      {
        id: 'clear',
        title: '清空当前会话',
        group: '会话',
        icon: Trash2,
        run: () => activeId && clearSession(activeId),
      },
      {
        id: 'sidebar',
        title: '切换左侧栏',
        group: '视图',
        icon: PanelLeft,
        run: () => toggleSidebar(),
      },
      {
        id: 'tools',
        title: '打开工具面板',
        group: '视图',
        icon: Wrench,
        run: () => toggleRight('tools'),
      },
      {
        id: 'market',
        title: '打开技能市场',
        group: '视图',
        icon: Sparkles,
        run: () => toggleRight('market'),
      },
      {
        id: 'schedule',
        title: '打开定时任务',
        group: '视图',
        icon: Clock,
        run: () => toggleRight('schedule'),
      },
      {
        id: 'voice-settings',
        title: '打开语音设置',
        group: '视图',
        icon: Settings,
        run: () => setSettingsOpen(true),
      },
    ]
    for (const p of providers) {
      list.push({
        id: `prov-${p.id}`,
        title: `切换 Provider：${p.display_name}`,
        group: 'Provider',
        icon: Wrench,
        run: () => setProvider(p.id),
      })
    }
    for (const id of order) {
      const s = sessions[id]
      if (!s) continue
      list.push({
        id: `sess-${id}`,
        title: `打开：${s.name}`,
        group: '会话',
        icon: MessageSquarePlus,
        run: () => selectSession(id),
      })
    }
    return list
  }, [
    providers,
    order,
    sessions,
    activeId,
    newSession,
    clearSession,
    toggleSidebar,
    toggleRight,
    setProvider,
    setSettingsOpen,
    selectSession,
  ])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return commands
    return commands.filter((c) => c.title.toLowerCase().includes(q))
  }, [commands, query])

  useEffect(() => {
    if (open) {
      setQuery('')
      setSel(0)
      setTimeout(() => inputRef.current?.focus(), 30)
    }
  }, [open])

  useEffect(() => {
    setSel(0)
  }, [query])

  if (!open) return null

  function runCmd(c: Cmd | undefined) {
    if (!c) return
    c.run()
    setOpen(false)
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSel((i) => Math.min(i + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSel((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      runCmd(filtered[sel])
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
    }
  }

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-[60] flex items-start justify-center bg-black/50 backdrop-blur-sm"
        onClick={() => setOpen(false)}
      >
        <motion.div
          initial={{ opacity: 0, y: -14, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -14, scale: 0.98 }}
          transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          onClick={(e) => e.stopPropagation()}
          className="glass-strong mt-[12vh] w-full max-w-lg overflow-hidden rounded-xl3"
        >
          <div className="flex items-center gap-2 border-b border-line px-4">
            <Search size={16} className="text-faint" />
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onKey}
              placeholder="输入命令或搜索…"
              className="flex-1 bg-transparent py-3.5 text-sm text-ink outline-none placeholder:text-faint"
            />
            <span className="chip !px-1.5 !py-0.5 !text-[10px]">ESC</span>
          </div>
          <div className="max-h-[50vh] overflow-y-auto p-2">
            {filtered.length === 0 && (
              <div className="px-3 py-6 text-center text-sm text-faint">
                没有匹配的命令
              </div>
            )}
            {filtered.map((c, i) => {
              const Icon = c.icon
              return (
                <button
                  key={c.id}
                  onMouseEnter={() => setSel(i)}
                  onClick={() => runCmd(c)}
                  className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors ${
                    i === sel ? 'bg-accent/15' : 'hover:bg-white/5'
                  }`}
                >
                  <Icon size={15} className={i === sel ? 'text-accent-strong' : 'text-faint'} />
                  <span className="flex-1 truncate text-sm text-ink">{c.title}</span>
                  <span className="text-[10.5px] uppercase tracking-wide text-faint">
                    {c.group}
                  </span>
                  {i === sel && (
                    <CornerDownLeft size={13} className="text-faint" />
                  )}
                </button>
              )
            })}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
