import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { HealthInfo, ProviderInfo, ToolSchema } from '../lib/types'
import type { InstalledSkill } from '../lib/api'
import {
  fetchHealth,
  fetchProviders,
  fetchTools,
  measureLatency,
  getInstalledSkills,
  fetchMode,
  setMode as apiSetMode,
} from '../lib/api'
import { debugLog } from '../lib/debug'

export type RightTab =
  | 'tools'
  | 'providers'
  | 'market'
  | 'schedule'
  | 'memory'
  | 'system'
  | 'email'

/** 运行时模式：full = 完整模式（全部工具 + 完整提示词）；minimal = 极简模式（精简工具 + 短提示词）。 */
export type RuntimeMode = 'full' | 'minimal'

interface UIState {
  providers: ProviderInfo[]
  tools: ToolSchema[]
  health: (HealthInfo & { latency: number }) | null

  provider: string
  model: string | null
  // 运行时模式：full（完整）/ minimal（极简）。与推理来源无关。
  mode: RuntimeMode
  // ★ 权限档位（2026-08-07）：low=只读 / medium=常规（默认）/ high=接管电脑
  //   前端滑动条调节，随请求传给后端过滤工具集
  permissionLevel: 'low' | 'medium' | 'high'

  sidebarOpen: boolean
  rightOpen: boolean
  rightTab: RightTab
  commandOpen: boolean
  // 语音设置弹窗
  settingsOpen: boolean
  // 接口设置弹窗（地址 / 密钥 / 模型名）
  providerSettingsOpen: boolean
  // 后端断连横幅：streaming 中若后端掉线，显示非阻塞重连提示
  reconnecting: boolean

  // 技能市场：已安装清单（slug -> meta）
  skillInstalled: Record<string, InstalledSkill>

  // 外部带入的待发送内容（资源管理器右键唤起的预填文本）；前端消费后清空
  pendingComposerText: string | null
  setPendingComposer: (text: string | null) => void

  // actions
  loadProviders: () => Promise<void>
  loadHealth: () => Promise<void>
  loadTools: () => Promise<void>
  loadInstalledSkills: () => Promise<void>
  setProvider: (id: string) => void
  setModel: (m: string | null) => void
  setMode: (m: RuntimeMode) => Promise<void>
  loadMode: () => Promise<void>
  toggleSidebar: () => void
  toggleRight: (tab?: RightTab) => void
  setRightTab: (t: RightTab) => void
  setCommandOpen: (v: boolean) => void
  setSettingsOpen: (v: boolean) => void
  setProviderSettingsOpen: (v: boolean) => void
  setReconnecting: (v: boolean) => void
  // 权限档位
  setPermissionLevel: (lvl: 'low' | 'medium' | 'high') => void
}

export const useUIStore = create<UIState>()(
  persist(
    (set, get) => ({
      providers: [],
      tools: [],
      health: null,
      provider: '',
      model: null,
      mode: 'full',
      permissionLevel: 'medium',
      sidebarOpen: true,
      rightOpen: false,
      rightTab: 'tools',
      commandOpen: false,
      settingsOpen: false,
      providerSettingsOpen: false,
      reconnecting: false,
      skillInstalled: {},
      pendingComposerText: null,

      async loadProviders() {
        try {
          const list = await fetchProviders()
          set({ providers: list })
          debugLog('ui', 'loadProviders 完成', {
            count: list.length,
            ids: list.map((p) => p.id),
          })
          const cur = get().provider
          if (!cur || !list.some((p) => p.id === cur)) {
            const first = list[0]
            if (first) {
              set({ provider: first.id })
              if (get().model == null) set({ model: first.default_model })
              debugLog('ui', '自动选定接口', {
                provider: first.id,
                model: first.default_model,
              })
            } else {
              debugLog('ui', '接口列表为空！', undefined, 'warn')
            }
          } else {
            debugLog('ui', '接口已存在，保留', { provider: cur })
          }
          // 同时加载运行时模式
          try {
            const m = await fetchMode()
            if (m === 'full' || m === 'minimal') {
              set({ mode: m })
              debugLog('ui', 'loadMode (from loadProviders)', { mode: m })
            }
          } catch {
            /* 后端未就绪时忽略 */
          }
        } catch (e: any) {
          debugLog('ui', 'loadProviders 失败（后端可能未就绪）', { error: e?.message }, 'warn')
        }
      },

      async loadHealth() {
        const latency = await measureLatency()
        if (latency < 0) {
          set({ health: { status: 'offline', providers: [], latency } })
          return
        }
        try {
          const h = await fetchHealth()
          set({ health: { ...h, latency } })
        } catch {
          set({ health: { status: 'offline', providers: [], latency } })
        }
      },

      async loadTools() {
        try {
          set({ tools: await fetchTools() })
        } catch {
          /* ignore */
        }
      },

      async loadInstalledSkills() {
        try {
          set({ skillInstalled: await getInstalledSkills() })
        } catch {
          /* ignore */
        }
      },

      setProvider(id) {
        set({ provider: id })
        const p = get().providers.find((x) => x.id === id)
        if (p?.default_model) set({ model: p.default_model })
        debugLog('ui', 'setProvider', { provider: id, model: p?.default_model })
      },
      setModel(m) {
        set({ model: m })
        debugLog('ui', 'setModel', { model: m })
      },
      async setMode(m) {
        try {
          await apiSetMode(m)
        } catch (e: any) {
          debugLog('ui', 'setMode API 失败', { error: e?.message }, 'warn')
        }
        set({ mode: m })
        debugLog('ui', 'setMode', { mode: m })
      },
      async loadMode() {
        try {
          const m = await fetchMode()
          if (m === 'full' || m === 'minimal') {
            set({ mode: m })
            debugLog('ui', 'loadMode', { mode: m })
          }
        } catch (e: any) {
          debugLog('ui', 'loadMode 失败', { error: e?.message }, 'warn')
        }
      },

      toggleSidebar() {
        set({ sidebarOpen: !get().sidebarOpen })
      },
      toggleRight(tab) {
        const open = get().rightOpen
        if (tab) {
          set({ rightOpen: true, rightTab: tab })
        } else {
          set({ rightOpen: !open })
        }
      },
      setRightTab(t) {
        set({ rightTab: t, rightOpen: true })
      },
      setCommandOpen(v) {
        set({ commandOpen: v })
      },
      setSettingsOpen(v) {
        set({ settingsOpen: v })
      },
      setProviderSettingsOpen(v) {
        set({ providerSettingsOpen: v })
      },
      setReconnecting(v) {
        set({ reconnecting: v })
      },
      setPermissionLevel(lvl) {
        set({ permissionLevel: lvl })
        // ★ 同步到后端（2026-08-08）：权限只降不升，后端记录已确认档位，
        // 聊天请求体不能顺带提权。滑动条是唯一提权入口。
        try {
          fetch('/api/permission', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ level: lvl }),
          }).catch(() => {})
        } catch {
          /* 后端未就绪时忽略 */
        }
      },
      setPendingComposer(text) {
        set({ pendingComposerText: text })
      },
    }),
    {
      name: 'my-agent-ui',
      partialize: (s) => ({
        provider: s.provider,
        model: s.model,
        mode: s.mode,
        sidebarOpen: s.sidebarOpen,
        rightOpen: s.rightOpen,
        rightTab: s.rightTab,
        permissionLevel: s.permissionLevel,
      }),
    },
  ),
)
