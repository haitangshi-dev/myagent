import { useEffect } from 'react'
import { AnimatePresence } from 'framer-motion'
import { useChatStore } from './store/useChatStore'
import { useUIStore } from './store/useUIStore'
import { TopBar } from './components/layout/TopBar'
import { Sidebar } from './components/layout/Sidebar'
import { RightPanel } from './components/layout/RightPanel'
import { StatusBar } from './components/layout/StatusBar'
import { CommandPalette } from './components/layout/CommandPalette'
import { VoiceSettingsModal } from './components/settings/VoiceSettingsModal'
import { ProviderSettingsModal } from './components/settings/ProviderSettingsModal'
import { ChatView } from './components/chat/ChatView'
import { WorkspaceBar } from './components/chat/WorkspaceBar'
import { DebugOverlay } from './components/DebugOverlay'
import { debugLog } from './lib/debug'
import { useDebugStore } from './store/useDebugStore'
import { installAuthInterceptor } from './lib/auth'
import { TokenGate } from './components/TokenGate'
import { ReconnectBanner } from './components/layout/ReconnectBanner'
import { subscribeEmailEvents } from './lib/emailBridge'

// 安装 fetch 拦截器：远程/手机客户端自动给 /api/* 带访问令牌
installAuthInterceptor()

export default function App() {
  const initSession = useChatStore((s) => s.initSession)
  const rightOpen = useUIStore((s) => s.rightOpen)
  const setCommandOpen = useUIStore((s) => s.setCommandOpen)

  // 启动引导
  useEffect(() => {
    initSession()
    const ui = useUIStore.getState()
    void ui.loadProviders()
    void ui.loadHealth()
    void ui.loadTools()
    const t = setInterval(() => void useUIStore.getState().loadHealth(), 15000)
    return () => clearInterval(t)
  }, [initSession])

  // 邮件网关 → 主 chat 渲染桥：邮件唤醒的 Agent 会话像用户自己输入一样
  // 出现在 chat 界面（邮件正文=user 消息，模型输出=assistant 消息流式渲染）。
  // 全局常驻订阅，不依赖邮件面板是否打开。
  useEffect(() => {
    const off = subscribeEmailEvents((ev) => {
      const chat = useChatStore.getState()
      const e = ev as any
      switch (e?.type) {
        case 'email_agent_start':
          chat.injectEmailSession(e)
          break
        case 'email_agent_event':
          chat.appendEmailAgentEvent(e)
          break
        case 'email_agent_end':
          chat.finalizeEmailSession(e)
          break
        default:
          break
      }
    })
    return off
  }, [])

  // ⌘K / Ctrl+K 打开命令面板
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setCommandOpen(!useUIStore.getState().commandOpen)
      }
      // Ctrl+Shift+D 切换调试面板
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'd') {
        e.preventDefault()
        useDebugStore.getState().toggle()
        debugLog('debug', '调试面板切换')
      }
      // Ctrl/Cmd+\ 切换侧栏折叠
      if ((e.ctrlKey || e.metaKey) && e.key === '\\') {
        e.preventDefault()
        useUIStore.getState().toggleSidebar()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setCommandOpen])

  // 全局点击捕获：细到记录每一次点击的元素
  useEffect(() => {
    function onGlobalClick(e: MouseEvent) {
      const el = e.target as HTMLElement | null
      if (!el) return
      const tag = el.tagName.toLowerCase()
      const cls = (el.className?.toString() || '').slice(0, 70)
      const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40)
      debugLog(
        'click',
        `${tag}${cls ? '.' + cls : ''}${txt ? ` "${txt}"` : ''}`,
        { x: e.clientX, y: e.clientY },
      )
    }
    // capture 阶段，确保任何元素被点都能记录
    document.addEventListener('click', onGlobalClick, true)
    return () => document.removeEventListener('click', onGlobalClick, true)
  }, [])

  // 深度嵌入桥接：Electron 壳才有的能力（浏览器直接打开时 window.electronAPI 不存在）
  useEffect(() => {
    const api = (window as any).electronAPI
    if (!api) return
    let offProto: (() => void) | undefined
    let offClip: (() => void) | undefined
    let offOpenPath: (() => void) | undefined
    let offTheme: (() => void) | undefined

    // 系统主题跟随：切换 <html class="light">，index.css 据此覆盖浅色 token
    const applyTheme = (dark: boolean) => {
      const root = document.documentElement
      if (dark) root.classList.remove('light')
      else root.classList.add('light')
    }
    if (api.onThemeChange) {
      offTheme = api.onThemeChange((dark: boolean) => applyTheme(dark))
    }
    // 兜底：主动问一次当前主题（避免 initial 事件早于监听丢失）
    if (api.getThemeDark) {
      api.getThemeDark().then((dark: boolean) => applyTheme(dark))
    }

    // myagent://chat?q=... 外部唤起 → 直接发消息
    if (api.onProtocol) {
      offProto = api.onProtocol((arg: string) => {
        try {
          const u = new URL(arg)
          const q = u.searchParams.get('q') || u.searchParams.get('text') || ''
          if (q) {
            api.showMainWindow?.()
            void useChatStore.getState().sendMessage(q)
          }
        } catch (_) {}
      })
    }

    // 资源管理器右键「问助手」→ 预填输入框（先看再发，安全）
    if (api.onOpenPath) {
      offOpenPath = api.onOpenPath((payload: { path: string; isDir: boolean }) => {
        try {
          const p = payload?.path
          if (!p) return
          api.showMainWindow?.()
          const prompt = payload.isDir
            ? `帮我看看这个文件夹：${p}\n（请告诉我你想对它做什么）`
            : `帮我看看这个文件：${p}\n（请告诉我你想对它做什么）`
          useUIStore.getState().setPendingComposer(prompt)
        } catch (_) {}
      })
    }

    // 剪贴板变化 → 轻提示（不自动发，隐私优先；用户可点击处理）
    if (api.onClipboardChanged) {
      offClip = api.onClipboardChanged((text: string) => {
        debugLog('clipboard', '剪贴板变化', { len: text?.length })
        // 暂不自动处理，仅记录；后续可在 UI 显示"已捕获剪贴板内容"气泡
      })
    }

    // 通知主进程：前端已就绪并注册完监听，可补发排队中的外部事件
    api.ackReady?.()

    return () => {
      offProto?.()
      offClip?.()
      offOpenPath?.()
      offTheme?.()
    }
  }, [])

  // 文件拖拽进窗口 → 读内容作为附件发给对话
  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    const api = (window as any).electronAPI
    if (!api?.readFile) return
    const files = Array.from(e.dataTransfer.files || [])
    const paths = files.map((f: any) => f.path).filter(Boolean)
    if (!paths.length) return
    let combined = '【拖入文件】\n'
    for (const p of paths) {
      const r = await api.readFile(p)
      if (r?.error) combined += `- ${p}: 读取失败(${r.error})\n`
      else combined += `- ${r.name} (${r.size}B):\n${r.text.slice(0, 4000)}\n\n`
    }
    void useChatStore.getState().sendMessage(combined.trim())
  }
  const onDragOver = (e: React.DragEvent) => {
    if (e.dataTransfer.types.includes('Files')) e.preventDefault()
  }

  return (
    <TokenGate>
      <div
        className="flex h-full flex-col bg-bg text-ink"
        onDrop={onDrop}
        onDragOver={onDragOver}
      >
        <TopBar />
        <ReconnectBanner />
        <WorkspaceBar />
        <div className="flex min-h-0 flex-1">
          <Sidebar />
          <main className="flex min-w-0 flex-1 flex-col">
            <ChatView />
          </main>
          <AnimatePresence>{rightOpen && <RightPanel />}</AnimatePresence>
        </div>
      <StatusBar />
      <CommandPalette />
      <VoiceSettingsModal />
      <ProviderSettingsModal />
      <DebugOverlay />
      {/* 调试面板悬浮开关 */}
      <button
        onClick={() => {
          useDebugStore.getState().toggle()
          debugLog('debug', '点击悬浮按钮切换调试面板')
        }}
        title="调试日志 (Ctrl+Shift+D)"
        style={{
          position: 'fixed',
          right: 12,
          bottom: 12,
          // 调试开关同样是角落工具层，低于阻塞式 modal（z-60/70），避免压住 modal 角落按钮
          zIndex: 40,
          width: 34,
          height: 34,
          borderRadius: '50%',
          border: '1px solid #1b2733',
          background: '#0f1620',
          color: '#3ddc97',
          fontSize: 16,
          cursor: 'pointer',
          display: useDebugStore((s) => s.visible) ? 'none' : 'block',
        }}
      >
        🐞
      </button>
      </div>
    </TokenGate>
  )
}
