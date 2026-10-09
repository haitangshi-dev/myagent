import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type {
  ChatMessage,
  Session,
  SessionKind,
  SandboxPolicy,
  TaskItem,
  ToolCall,
  ToolKind,
  Attachment,
} from '../lib/types'
import { streamChat } from '../lib/sse'
import {
  postApprove,
  postCancel,
  putWorkspaceSandbox,
  fetchWorkspaceInfo,
} from '../lib/api'
import { uid } from '../lib/format'
import { useUIStore } from './useUIStore'
import { useTaskStore } from './useTaskStore'
import { debugLog } from '../lib/debug'

// 工作区默认沙盒：允许写文件、禁止删文件、允许 shell
const DEFAULT_SANDBOX: SandboxPolicy = {
  allow_file_write: true,
  allow_file_delete: false,
  allow_shell: true,
}

// AbortController 不进 store（不可序列化），用模块级变量持有
let controller: AbortController | null = null
// 当前正在流式输出的会话 id，供 stop() 精准取消（不依赖 activeId）
let streamSessionId: string | null = null

// —— 持久化防抖 ——
// persist 在**每次 set** 时都会把整个 sessions 全量 JSON.stringify 写 localStorage。
// 流式期间 flushStreamBuf 每帧 set（约 60 次/秒），长会话下字符串巨大 → 每帧写盘
// 阻塞主线程，是「长文本对话卡顿」的核心瓶颈之一。这里对 setItem 做 800ms 防抖，
// 只有空闲后才真正落盘；flushChatPersistence() 供对话收尾/停止时立即落盘，防丢最后一段。
const CHAT_PERSIST_DEBOUNCE = 800
let _chatPersistTimer: ReturnType<typeof setTimeout> | null = null
let _chatPersistPending: { name: string; value: string } | null = null

const debouncedStorage = {
  getItem: (name: string): string | null => {
    try {
      return localStorage.getItem(name)
    } catch {
      return null
    }
  },
  setItem: (name: string, value: string) => {
    _chatPersistPending = { name, value }
    if (_chatPersistTimer) clearTimeout(_chatPersistTimer)
    _chatPersistTimer = setTimeout(() => {
      try {
        localStorage.setItem(_chatPersistPending!.name, _chatPersistPending!.value)
      } catch {
        /* 配额超限等忽略 */
      }
      _chatPersistPending = null
      _chatPersistTimer = null
    }, CHAT_PERSIST_DEBOUNCE)
  },
  removeItem: (name: string) => {
    try {
      localStorage.removeItem(name)
    } catch {
      /* ignore */
    }
  },
}

export function flushChatPersistence() {
  if (_chatPersistTimer) {
    clearTimeout(_chatPersistTimer)
    _chatPersistTimer = null
  }
  if (_chatPersistPending) {
    try {
      localStorage.setItem(_chatPersistPending.name, _chatPersistPending.value)
    } catch {
      /* ignore */
    }
    _chatPersistPending = null
  }
}

interface ChatState {
  sessions: Record<string, Session>
  order: string[]
  activeId: string | null
  streaming: boolean
  // 模型连接重试（被限流/网络抖动）瞬时状态：驱动状态吉祥物与气泡显示「连接模型中…」
  // 明确区分「真在思考（流式推理链）」与「在重试 / 重连模型」。
  modelRetrying: { attempt: number; reason: string; isRateLimit: boolean } | null
  // 全局上下文用量（来自最近一次 usage 事件），驱动 TopBar 占用可视化
  contextUsage: { prompt_tokens: number; completion_tokens: number } | null

  initSession: () => void
  newSession: (
    kind?: SessionKind,
    workspaceDir?: string | null,
    sandbox?: SandboxPolicy | null,
  ) => void
  selectSession: (id: string) => void
  renameSession: (id: string, name: string) => void
  deleteSession: (id: string) => void
  clearSession: (id: string) => void
  activeSession: () => Session | null

  // 工作区会话：设置工作目录（并从后端拉取已部署沙盒/记忆数）
  setWorkspaceDir: (id: string, dir: string) => Promise<void>
  // 工作区会话：更新沙盒策略（乐观）+ 持久化到目录
  setSandbox: (id: string, sandbox: SandboxPolicy) => Promise<void>

  sendMessage: (text: string, images?: Attachment[]) => Promise<void>
  stop: () => void
  approve: (
    sessionId: string,
    messageId: string,
    approvalId: string,
    decision: 'approve' | 'deny',
  ) => Promise<void>

  // —— 邮件唤醒会话（邮件网关触发，模拟用户在 chat 界面输入需求） ——
  injectEmailSession: (meta: {
    message_id: string
    session_id: string
    from: string
    subject: string
    body: string
  }) => string | null
  appendEmailAgentEvent: (payload: {
    session_id: string
    event: Record<string, unknown>
  }) => void
  finalizeEmailSession: (payload: {
    session_id: string
    reply: string
    replied: boolean
    reply_error: string
  }) => void
}

function newSessionObj(
  kind: SessionKind = 'chat',
  workspaceDir: string | null = null,
  sandbox: SandboxPolicy | null = null,
): Session {
  const now = Date.now()
  return {
    id: uid(),
    name: kind === 'workspace' ? '工作区会话' : '新会话',
    kind,
    workspaceDir,
    sandbox,
    messages: [],
    createdAt: now,
    updatedAt: now,
  }
}

// —— 流式增量批处理：reasoning/content 高频分片合并，按 ~20fps 节流刷新一次，
//    避免每片都触发全量状态重建 + 全部 bubble 重渲染。节流后最后一帧在流结束时强制 flush。 ——
const FLUSH_MS = 50 // ≈20fps：足够流畅，且把 set 次数从 60/s 降到 ~20/s
let _flushTimer: ReturnType<typeof setTimeout> | null = null
let _lastFlush = 0
const _streamBuf = new Map<string, { sid: string; aid: string; content: string; reasoning: string }>()

function flushStreamBuf(set: (fn: (s: any) => any) => void, _force = false) {
  if (_flushTimer) {
    clearTimeout(_flushTimer)
    _flushTimer = null
  }
  _lastFlush = Date.now()
  if (_streamBuf.size === 0) return
  const entries = Array.from(_streamBuf.values())
  _streamBuf.clear()
  set((state: any) => {
    const sessions = { ...state.sessions }
    for (const e of entries) {
      const sess = sessions[e.sid]
      if (!sess) continue
      sessions[e.sid] = {
        ...sess,
        messages: sess.messages.map((m: any) =>
          m.id === e.aid
            ? {
                ...m,
                content: e.content ? m.content + e.content : m.content,
                reasoning: e.reasoning ? m.reasoning + e.reasoning : m.reasoning,
              }
            : m,
        ),
        updatedAt: Date.now(),
      }
    }
    return { sessions }
  })
}

function bufferStreamDelta(
  set: (fn: (s: any) => any) => void,
  sid: string,
  aid: string,
  kind: 'content' | 'reasoning',
  text: string,
) {
  const key = `${sid}:${aid}`
  const cur = _streamBuf.get(key) || { sid, aid, content: '', reasoning: '' }
  if (kind === 'content') cur.content += text
  else cur.reasoning += text
  _streamBuf.set(key, cur)
  if (_flushTimer) return // 已有节流计时器在跑，增量已入缓冲，等待下次 flush
  const elapsed = Date.now() - _lastFlush
  const delay = Math.max(0, FLUSH_MS - elapsed)
  _flushTimer = setTimeout(() => {
    _flushTimer = null
    flushStreamBuf(set)
  }, delay)
}

// 持久化的部分状态（partialize 输出）
interface ChatPersistedState {
  sessions: Record<string, Session>
  order: string[]
  activeId: string | null
}

export const useChatStore = create<ChatState>()(
  persist<ChatState, [], [], ChatPersistedState>(
    (set, get) => {
      // —— 内部工具：不可变地更新某条消息 ——
      const patchMessage = (
        sessionId: string,
        msgId: string,
        updater: (m: ChatMessage) => ChatMessage,
      ) => {
        set((state) => {
          const sess = state.sessions[sessionId]
          if (!sess) return state
          const messages = sess.messages.map((m) =>
            m.id === msgId ? updater(m) : m,
          )
          const updated: Session = { ...sess, messages, updatedAt: Date.now() }
          return {
            sessions: { ...state.sessions, [sessionId]: updated },
            order:
              state.order[0] === sessionId
                ? state.order
                : [sessionId, ...state.order.filter((x) => x !== sessionId)],
          }
        })
      }

      const ensureSession = (): Session | null => {
        let s = get().activeSession()
        if (!s) {
          const obj = newSessionObj()
          set((st) => ({
            sessions: { ...st.sessions, [obj.id]: obj },
            order: [obj.id, ...st.order],
            activeId: obj.id,
          }))
          s = obj
        }
        return s
      }

      return {
        sessions: {},
        order: [],
        activeId: null,
        streaming: false,
        modelRetrying: null,
        contextUsage: null,

        initSession() {
          if (get().order.length === 0) {
            const obj = newSessionObj('chat')
            set({
              sessions: { [obj.id]: obj },
              order: [obj.id],
              activeId: obj.id,
            })
          } else if (!get().activeId) {
            set({ activeId: get().order[0] })
          }
        },

        newSession(kind = 'chat', workspaceDir = null, sandbox = null) {
          const obj = newSessionObj(
            kind,
            workspaceDir,
            // 工作区会话无显式沙盒时给默认策略
            kind === 'workspace' && !sandbox
              ? { ...DEFAULT_SANDBOX }
              : sandbox,
          )
          set((st) => ({
            sessions: { ...st.sessions, [obj.id]: obj },
            order: [obj.id, ...st.order],
            activeId: obj.id,
          }))
        },

        async setWorkspaceDir(id, dir) {
          let info = { sandbox: { ...DEFAULT_SANDBOX }, memory_count: 0 }
          try {
            info = await fetchWorkspaceInfo(dir)
          } catch {
            /* 目录可能尚不存在，使用默认沙盒即可 */
          }
          set((st) => {
            const s = st.sessions[id]
            if (!s) return st
            return {
              sessions: {
                ...st.sessions,
                [id]: {
                  ...s,
                  workspaceDir: dir,
                  sandbox: info.sandbox,
                },
              },
            }
          })
        },

        async setSandbox(id, sandbox) {
          // 乐观更新本地
          set((st) => {
            const s = st.sessions[id]
            if (!s) return st
            return {
              sessions: {
                ...st.sessions,
                [id]: { ...s, sandbox: { ...sandbox } },
              },
            }
          })
          // 持久化到工作目录（写入 .myagent_sandbox.json）
          const s = get().sessions[id]
          if (s?.workspaceDir) {
            try {
              await putWorkspaceSandbox(s.workspaceDir, sandbox)
            } catch {
              /* 写文件失败不影响本地 UI，下次再试 */
            }
          }
        },

        selectSession(id) {
          set((st) => ({
            activeId: id,
            order: [id, ...st.order.filter((x) => x !== id)],
          }))
          // 切换会话时从后端拉取该会话的任务看板（覆盖本地缓存）
          void useTaskStore.getState().hydrate(id)
        },

        renameSession(id, name) {
          set((st) => {
            const s = st.sessions[id]
            if (!s) return st
            return {
              sessions: {
                ...st.sessions,
                [id]: { ...s, name: name.trim() || '未命名会话' },
              },
            }
          })
        },

        deleteSession(id) {
          set((st) => {
            const sessions = { ...st.sessions }
            delete sessions[id]
            const order = st.order.filter((x) => x !== id)
            const activeId =
              st.activeId === id ? order[0] ?? null : st.activeId
            return { sessions, order, activeId }
          })
        },

        clearSession(id) {
          set((st) => {
            const s = st.sessions[id]
            if (!s) return st
            return {
              sessions: {
                ...st.sessions,
                [id]: { ...s, messages: [], name: '新会话' },
              },
            }
          })
        },

        activeSession() {
          const st = get()
          return st.activeId ? st.sessions[st.activeId] ?? null : null
        },

        async sendMessage(text: string, images?: Attachment[]) {
          const trimmed = text.trim()
          const attachments = images && images.length ? images : undefined
          // 图片走多模态 data URL；非图片文件仅附路径说明，模型用 read_file 读取
          const imgDataUrls = attachments
            ? attachments.filter((a) => a.isImage && a.dataUrl).map((a) => a.dataUrl as string)
            : []
          const fileNotes = attachments
            ? attachments
                .filter((a) => !a.isImage)
                .map((a) => `- ${a.path} (${(a.size / 1024).toFixed(1)} KB)`)
            : []
          const hasContent = trimmed || imgDataUrls.length > 0
          if (!hasContent || get().streaming) {
            debugLog('chat', 'sendMessage 提前返回', {
              empty: !hasContent,
              streaming: get().streaming,
            }, 'warn')
            return
          }
          // 非图片附件路径说明附加到正文，便于模型用工具读取
          const mergedText =
            trimmed + (fileNotes.length ? `\n\n[已附文件，可用 read_file 读取]\n${fileNotes.join('\n')}` : '')
          const sendImages = imgDataUrls // 仅图片进入多模态 images 字段
          const sess = ensureSession()
          if (!sess) {
            debugLog('chat', 'ensureSession 返回 null', undefined, 'error')
            return
          }
          const ui = useUIStore.getState()
          const isWs = sess.kind === 'workspace'
          debugLog('chat', 'sendMessage 入口', {
            sessionId: sess.id,
            kind: sess.kind,
            workspaceDir: sess.workspaceDir,
            textLen: trimmed.length,
            provider: ui.provider,
            model: ui.model,
            historyLen: sess.messages.length,
          })

          // 历史：仅取既有消息的 role/content（不含即将追加的两条）
          const history = sess.messages.map((m) => ({
            role: m.role,
            content: m.content,
          }))

          const userMsg: ChatMessage = {
            id: uid(),
            role: 'user',
            createdAt: Date.now(),
            content: mergedText,
            reasoning: '',
            toolCalls: [],
            approvals: [],
            status: 'done',
            images: attachments,
          }
          const assistantMsg: ChatMessage = {
            id: uid(),
            role: 'assistant',
            createdAt: Date.now(),
            content: '',
            reasoning: '',
            toolCalls: [],
            approvals: [],
            status: 'streaming',
          }

          set((st) => {
            const s = st.sessions[sess.id]
            const updated: Session = {
              ...s,
              name:
                s.messages.length === 0 && s.name === '新会话'
                  ? trimmed.slice(0, 24)
                  : s.name,
              messages: [...s.messages, userMsg, assistantMsg],
              updatedAt: Date.now(),
            }
            return {
              sessions: { ...st.sessions, [sess.id]: updated },
              streaming: true,
            }
          })

          controller = new AbortController()
          const aId = assistantMsg.id
          const sId = sess.id
          streamSessionId = sId

          const applyError = (msg: string) =>
            patchMessage(sId, aId, (m) => ({
              ...m,
              status: 'error',
              error: msg,
            }))

          try {
            for await (const ev of streamChat(
              {
                message: mergedText,
                provider: ui.provider,
                model: ui.model,
                history,
                images: sendImages,
                session_id: sess.id,
                session_name: sess.name,
                kind: sess.kind,
                workspace_dir: isWs ? sess.workspaceDir : null,
                sandbox: isWs ? sess.sandbox : null,
                mode: ui.mode,
                permission_level: ui.permissionLevel,
              },
              controller.signal,
            )) {
              switch (ev.type) {
                case 'message_start':
                  patchMessage(sId, aId, (m) => ({
                    ...m,
                    messageId: (ev as any).message_id,
                  }))
                  // 新一轮对话开始：清除可能存在的「连接模型中」瞬时态
                  set({ modelRetrying: null })
                  break
                case 'content':
                  // 模型已开始产出正文 → 不再处于「连接/重试」状态
                  set({ modelRetrying: null })
                  bufferStreamDelta(set, sId, aId, 'content', (ev as any).text || '')
                  break
                case 'reasoning':
                  // 模型已开始产出推理链 → 不再处于「连接/重试」状态
                  set({ modelRetrying: null })
                  bufferStreamDelta(set, sId, aId, 'reasoning', (ev as any).text || '')
                  break
                case 'tool_call': {
                  // 模型已决定调用工具 → 不再处于「连接/重试」状态
                  set({ modelRetrying: null })
                  patchMessage(sId, aId, (m) => {
                    const e = ev as any
                    const kind: ToolKind = e.kind === 'skill' ? 'skill' : 'tool'
                    const exists = m.toolCalls.some((t) => t.id === e.id)
                    const base: ToolCall = {
                      id: e.id,
                      name: e.name,
                      label: e.label,
                      preview: e.preview ?? '',
                      status: 'running' as const,
                      kind,
                      command: e.command ?? '',
                      partial: !!e.partial,
                    }
                    return {
                      ...m,
                      toolCalls: exists
                        ? m.toolCalls.map((t) =>
                            t.id === e.id
                              ? { ...t, ...base }
                              : t,
                          )
                        : [...m.toolCalls, base],
                    }
                  })
                  const e2 = ev as any
                  debugLog('chat', e2.partial ? 'tool_call partial' : 'tool_call full', {
                    id: e2.id,
                    name: e2.name,
                  })
                  break
                }
                case 'tool_result':
                  patchMessage(sId, aId, (m) => {
                    const e = ev as any
                    return {
                      ...m,
                      toolCalls: m.toolCalls.map((t) =>
                        t.id === e.id
                          ? {
                              ...t,
                              status: e.status,
                              resultPreview: e.preview ?? '',
                            }
                          : t,
                      ),
                    }
                  })
                  break
                case 'usage':
                  patchMessage(sId, aId, (m) => ({
                    ...m,
                    usage: {
                      prompt_tokens: (ev as any).prompt_tokens,
                      completion_tokens: (ev as any).completion_tokens,
                    },
                  }))
                  // 同步全局上下文用量，驱动 TopBar 占用可视化
                  set({
                    contextUsage: {
                      prompt_tokens: (ev as any).prompt_tokens ?? 0,
                      completion_tokens: (ev as any).completion_tokens ?? 0,
                    },
                  })
                  break
                case 'approval_required': {
                  const e = ev as any
                  patchMessage(sId, aId, (m) => ({
                    ...m,
                    approvals: [
                      ...m.approvals,
                      {
                        approval_id: e.approval_id,
                        tool: e.tool,
                        path: e.path,
                        description: e.description,
                        status: 'pending',
                      },
                    ],
                  }))
                  break
                }
                case 'error':
                  applyError((ev as any).message)
                  // 致命错误 → 不再处于「连接/重试」状态
                  set({ modelRetrying: null })
                  break
                case 'retry': {
                  const e = ev as any
                  // 先把缓冲里可能残留的末帧增量落盘，再重置，避免旧内容覆盖「连接中」提示
                  flushStreamBuf(set)
                  // 连接重试：重置上一轮（可能已部分输出）的内容，并追加一条提示；
                  // 同时置位瞬时「连接模型中」态，驱动吉祥物/气泡显示。
                  set({
                    modelRetrying: {
                      attempt: e.attempt,
                      reason: e.reason || '',
                      isRateLimit: !!e.is_rate_limit,
                    },
                  })
                  patchMessage(sId, aId, (m) => ({
                    ...m,
                    content: '',
                    reasoning: '',
                    toolCalls: [],
                    notices: [
                      ...(m.notices || []),
                      {
                        type: 'retry' as const,
                        attempt: e.attempt,
                        max: e.max,
                        delay: e.delay,
                        message:
                          e.max && e.max > 0
                            ? `模型连接重试（第 ${e.attempt}/${e.max} 次，${e.delay}s 后）…`
                            : `模型连接重试（第 ${e.attempt} 次，${e.delay}s 后）…`,
                      },
                    ],
                  }))
                  debugLog('chat', 'retry 事件', { attempt: e.attempt, max: e.max, delay: e.delay, isRateLimit: e.is_rate_limit })
                  break
                }
                case 'cancelled':
                  patchMessage(sId, aId, (m) => ({ ...m, status: 'stopped' }))
                  set({ modelRetrying: null })
                  break
                case 'message_end': {
                  const e = ev as any
                  const fr = e.finish_reason
                  // 先强制把缓冲里的最后一帧增量落盘，再标完成（节流下末尾可能未 flush）
                  flushStreamBuf(set, true)
                  // finish_reason=length → 模型被 max_tokens 截断；content_filter → 被安全过滤。
                  // 这两种都属「异常终止」，状态标 truncated 并提示，而非假装正常 done。
                  const truncated = fr === 'length' || fr === 'content_filter'
                  patchMessage(sId, aId, (m) =>
                    m.status === 'error' || m.status === 'stopped'
                      ? m
                      : {
                          ...m,
                          status: truncated ? 'truncated' : 'done',
                          finishReason: fr ?? undefined,
                        },
                  )
                  // 本轮结束 → 清除「连接模型中」瞬时态
                  set({ modelRetrying: null })
                  debugLog('chat', 'message_end', {
                    sessionId: sId,
                    finish_reason: fr,
                    truncated,
                  })
                  break
                }
                case 'task_update': {
                  const e = ev as any
                  // 任务看板只关心当前正在对话的会话（定时任务触发的任务属 scheduled:* 会话）
                  if (e.session_id === sId) {
                    useTaskStore
                      .getState()
                      .setTasks(sId, (e.tasks as TaskItem[]) || [])
                    debugLog('chat', 'task_update 接收', {
                      sid: sId,
                      count: (e.tasks || []).length,
                    })
                  }
                  break
                }
                case 'scheduled_run': {
                  // 定时/后台任务完成：弹系统通知（Electron 环境），并写入调试日志
                  const e = ev as any
                  const title = e.title || '定时任务已完成'
                  const detail = e.detail || ''
                  debugLog('chat', 'scheduled_run 接收', { title, detail })
                  try {
                    if (typeof window !== 'undefined' && (window as any).electronAPI?.notify) {
                      ;(window as any).electronAPI.notify(title, detail)
                    } else if ('Notification' in window) {
                      new Notification(title, { body: detail })
                    }
                  } catch {
                    /* 通知失败不阻断 */
                  }
                  break
                }
              }
            }
          } catch (err: any) {
            if (err?.name !== 'AbortError') {
              // 友好化网络断连提示：区分「后端被杀/网络中断」与真正错误
              const msg = err?.message || ''
              const isNetwork = /network|failed to fetch|load failed|TypeError/i.test(msg)
              const friendly = isNetwork ? '连接已断开（后端进程已结束或网络中断）' : msg
              applyError(friendly || '连接中断')
              debugLog('chat', 'sendMessage 异常', { error: err?.message, name: err?.name, friendly }, 'error')
              // 断连横幅：后端掉线时显示非阻塞重连提示，恢复后自动消失
              if (isNetwork) {
                useUIStore.getState().setReconnecting(true)
                const poll = setInterval(async () => {
                  try {
                    await useUIStore.getState().loadHealth()
                    const ok = useUIStore.getState().health?.status === 'ok'
                    if (ok) {
                      clearInterval(poll)
                      useUIStore.getState().setReconnecting(false)
                    }
                  } catch {
                    /* 继续轮询 */
                  }
                }, 2000)
              }
            } else {
              debugLog('chat', 'sendMessage 被 AbortError 中断', undefined, 'warn')
            }
    } finally {
      controller = null
      streamSessionId = null
      // 强制把缓冲里的最后一帧增量落盘（abort/异常时末尾可能未 flush）
      flushStreamBuf(set, true)
      set({ streaming: false })
      // 立即落盘，避免依赖防抖延迟（防丢最后一段流式内容）
      flushChatPersistence()
            // 若因 abort 中断，标记 stopped
            const st = get()
            const s = st.sessions[sId]
            const m = s?.messages.find((x) => x.id === aId)
            debugLog('chat', 'sendMessage 收尾', {
              finalStatus: m?.status,
              contentLen: m?.content.length,
              toolCalls: m?.toolCalls.length,
            })
            if (m && m.status === 'streaming') {
              patchMessage(sId, aId, (mm) => ({ ...mm, status: 'stopped' }))
            }
          }
        },

        stop() {
          const sid = streamSessionId
          if (sid) void postCancel(sid)
          controller?.abort()
          controller = null
          set({ streaming: false, modelRetrying: null })
          // 立即停止后落盘，保存已生成的内容
          flushChatPersistence()
        },

        async approve(sessionId, messageId, approvalId, decision) {
          patchMessage(sessionId, messageId, (m) => ({
            ...m,
            approvals: m.approvals.map((a) =>
              a.approval_id === approvalId
                ? { ...a, status: decision === 'approve' ? 'approved' : 'denied' }
                : a,
            ),
          }))
          try {
            await postApprove(approvalId, decision)
          } catch {
            /* 失败则保持原状态，用户可重试 */
          }
        },

        // ---------- 邮件唤醒会话（模拟用户在 chat 界面输入需求） ----------

        injectEmailSession(meta) {
          const sid = meta.session_id || `email:${meta.message_id}`
          const userMsgId = `email-msg:${meta.message_id}`
          const st = get()
          const exist = st.sessions[sid]
          if (exist) {
            // 幂等：同一封邮件重复触发时复用现有会话，不重复追加 user 消息
            if (exist.messages.some((m) => m.id === userMsgId)) return sid
          }
          const now = Date.now()
          const subject = (meta.subject || '新邮件').slice(0, 24)
          const userMsg: ChatMessage = {
            id: userMsgId,
            role: 'user',
            createdAt: now,
            content: `【📧 邮件唤醒】发件人：${meta.from || '(未知)'}\n主题：${meta.subject || '(无主题)'}\n\n${meta.body || '(空)'}`.trim(),
            reasoning: '',
            toolCalls: [],
            approvals: [],
            status: 'done',
          }
          const assistantMsg: ChatMessage = {
            id: `email-msg:${meta.message_id}:a`,
            role: 'assistant',
            createdAt: now,
            content: '',
            reasoning: '',
            toolCalls: [],
            approvals: [],
            status: 'streaming',
          }
          set((state) => {
            // 自动激活邮件会话（用户醒来直接看到执行过程，如同自己发起）；
            // 若当前会话正在流式输出则保留现场，避免打断。
            const anyStreaming = Object.values(state.sessions).some((s) =>
              s.messages.some((m) => m.status === 'streaming'),
            )
            return {
              sessions: {
                ...state.sessions,
                [sid]: {
                  id: sid,
                  name: `📧 ${subject}`,
                  kind: 'chat',
                  workspaceDir: null,
                  sandbox: null,
                  messages: [userMsg, assistantMsg],
                  createdAt: now,
                  updatedAt: now,
                },
              },
              order: [sid, ...state.order.filter((x) => x !== sid)],
              activeId: anyStreaming ? state.activeId : sid,
            }
          })
          debugLog('chat', 'injectEmailSession 创建', { sid, subject })
          return sid
        },

        appendEmailAgentEvent(payload) {
          const sid = payload.session_id
          const ev = (payload.event || {}) as any
          const st = get()
          const sess = st.sessions[sid]
          if (!sess) return
          // 定位本会话最后一条 assistant 消息（邮件会话结构固定：user + assistant）
          const aMsg = [...sess.messages].reverse().find((m) => m.role === 'assistant')
          if (!aMsg) return
          const aId = aMsg.id
          const t = ev?.type
          switch (t) {
            case 'message_start':
              patchMessage(sid, aId, (m) => ({ ...m, messageId: ev.message_id }))
              break
            case 'content':
              bufferStreamDelta(set, sid, aId, 'content', ev.text || '')
              break
            case 'reasoning':
              bufferStreamDelta(set, sid, aId, 'reasoning', ev.text || '')
              break
            case 'tool_call': {
              const kind: ToolKind = ev.kind === 'skill' ? 'skill' : 'tool'
              const exists = aMsg.toolCalls.some((tc) => tc.id === ev.id)
              const base: ToolCall = {
                id: ev.id,
                name: ev.name,
                label: ev.label,
                preview: ev.preview ?? '',
                status: 'running' as const,
                kind,
                command: ev.command ?? '',
                partial: !!ev.partial,
              }
              patchMessage(sid, aId, (m) => ({
                ...m,
                toolCalls: exists
                  ? m.toolCalls.map((tc) => (tc.id === ev.id ? { ...tc, ...base } : tc))
                  : [...m.toolCalls, base],
              }))
              break
            }
            case 'tool_result':
              patchMessage(sid, aId, (m) => ({
                ...m,
                toolCalls: m.toolCalls.map((tc) =>
                  tc.id === ev.id
                    ? { ...tc, status: ev.status, resultPreview: ev.preview ?? '' }
                    : tc,
                ),
              }))
              break
            case 'usage':
              patchMessage(sid, aId, (m) => ({
                ...m,
                usage: {
                  prompt_tokens: ev.prompt_tokens,
                  completion_tokens: ev.completion_tokens,
                },
              }))
              break
            case 'approval_required':
              patchMessage(sid, aId, (m) => ({
                ...m,
                approvals: [
                  ...m.approvals,
                  {
                    approval_id: ev.approval_id,
                    tool: ev.tool,
                    path: ev.path,
                    description: ev.description,
                    status: 'pending' as const,
                  },
                ],
              }))
              break
            case 'retry':
              // 邮件任务的重试仅追加提示，不清空已生成内容（与主聊天保持一致观感）
              patchMessage(sid, aId, (m) => ({
                ...m,
                notices: [
                  ...(m.notices || []),
                  {
                    type: 'info' as const,
                    message: `模型连接重试（第 ${ev.attempt} 次）…`,
                  },
                ],
              }))
              break
            case 'cancelled':
              flushStreamBuf(set, true)
              patchMessage(sid, aId, (m) => ({ ...m, status: 'stopped' }))
              break
            case 'error':
              flushStreamBuf(set, true)
              patchMessage(sid, aId, (m) => ({ ...m, status: 'error', error: ev.message }))
              break
            case 'message_end': {
              flushStreamBuf(set, true)
              const fr = ev.finish_reason
              const truncated = fr === 'length' || fr === 'content_filter'
              patchMessage(sid, aId, (m) =>
                m.status === 'error' || m.status === 'stopped'
                  ? m
                  : {
                      ...m,
                      status: truncated ? 'truncated' : 'done',
                      finishReason: fr ?? undefined,
                    },
              )
              flushChatPersistence()
              break
            }
            case 'task_update': {
              // 邮件任务里若模型用了 task_board 看板 → 同步到前端 TaskList（与主聊天一致）
              const e = ev as any
              if (e.session_id === sid) {
                useTaskStore.getState().setTasks(sid, (e.tasks as TaskItem[]) || [])
              }
              break
            }
            case 'scheduled_run': {
              // 与主聊天一致：定时/后台任务完成弹系统通知
              const e = ev as any
              const title = e.title || '定时任务已完成'
              const detail = e.detail || ''
              try {
                if (typeof window !== 'undefined' && (window as any).electronAPI?.notify) {
                  ;(window as any).electronAPI.notify(title, detail)
                } else if ('Notification' in window) {
                  new Notification(title, { body: detail })
                }
              } catch {
                /* 通知失败不阻断 */
              }
              break
            }
          }
        },

        finalizeEmailSession(payload) {
          const sid = payload.session_id
          const st = get()
          const sess = st.sessions[sid]
          if (!sess) return
          const aMsg = [...sess.messages].reverse().find((m) => m.role === 'assistant')
          if (!aMsg) return
          flushStreamBuf(set, true)
          const statusNote =
            payload.replied
              ? '✅ 已自动回信'
              : payload.reply_error
                ? `⚠️ 回信失败：${payload.reply_error}`
                : ''
          patchMessage(sid, aMsg.id, (m) => ({
            ...m,
            // 模型若全程走工具调用没产出正文，用最终 reply 兜底填充
            content: m.content.trim() ? m.content : (payload.reply || m.content),
            status: m.status === 'streaming' ? 'done' : m.status,
            notices: statusNote
              ? [...(m.notices || []), { type: 'info' as const, message: statusNote }]
              : m.notices,
          }))
          flushChatPersistence()
          debugLog('chat', 'finalizeEmailSession', {
            sid,
            replied: payload.replied,
            contentLen: get().sessions[sid]?.messages?.find((m) => m.role === 'assistant')?.content?.length,
          })
        },
      }
    },
    {
      name: 'my-agent-chat',
      // ★ 持久化防抖：避免流式期间每帧全量写 localStorage（见 debouncedStorage）
      storage: createJSONStorage(() => debouncedStorage),
      partialize: (s) => ({
        sessions: s.sessions,
        order: s.order,
        activeId: s.activeId,
      }),
      onRehydrateStorage: () => (state: any) => {
        // 刷新后，将任何残留的 streaming 消息标记为 done，避免卡死
        if (!state) return
        for (const s of Object.values(state.sessions) as any[]) {
          // 兼容旧版本：补齐工作区字段
          if (s.kind === undefined) s.kind = 'chat'
          if (s.workspaceDir === undefined) s.workspaceDir = null
          if (s.sandbox === undefined) s.sandbox = null
          for (const m of s.messages) {
            if (m.status === 'streaming') m.status = 'done'
            if (m.notices === undefined) m.notices = []
          }
        }
      },
    },
  ),
)
