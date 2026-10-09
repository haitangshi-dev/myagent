/**
 * 吉祥物状态桥接：把前端会话流（useChatStore）派生为 5 态 + 模型输出文本，
 * 经 electronAPI.pushMascot 推给主进程，再转发到吉祥物透明浮窗。
 *
 * 5 态状态机（无需改后端，纯前端依据 active session 最后一条 assistant 消息派生）：
 *   idle     待机      —— 非流式、无进行中任务
 *   thinking 思考中    —— 流式且有 reasoning（尚未产出 content）
 *   tooling  调用工具  —— 流式且有 running 状态的 tool_call
 *   replying 回复中    —— 流式且有 content 正在累积
 *   error    出错了    —— 最后一条 assistant 消息 status === 'error'
 */

import { useChatStore } from '../store/useChatStore'

type MascotState = 'idle' | 'thinking' | 'tooling' | 'replying' | 'error'

let started = false

export function initMascotBridge() {
  if (started) return
  started = true
  const api = (window as any).electronAPI
  if (!api || typeof api.pushMascot !== 'function') return // 浏览器直接打开则跳过

  let lastState: MascotState | null = null
  let lastText = ''
  let lastPush = 0
  let pending: ReturnType<typeof setTimeout> | null = null
  const THROTTLE_MS = 120

  const compute = (): { state: MascotState; text: string } => {
    const st = useChatStore.getState()
    const sess = st.activeSession()
    if (!sess) return { state: 'idle', text: '' }
    const msgs = sess.messages
    const last = msgs[msgs.length - 1] as any
    if (!last) return { state: 'idle', text: '' }

    if (last.role === 'assistant') {
      if (last.status === 'error') {
        return { state: 'error', text: last.content || last.error || '' }
      }
      const runningTool = Array.isArray(last.toolCalls)
        ? last.toolCalls.some((t: any) => t.status === 'running')
        : false
      if (st.streaming) {
        if (runningTool) return { state: 'tooling', text: last.content || '' }
        if (last.content && last.content.length) return { state: 'replying', text: last.content }
        if (last.reasoning && last.reasoning.length) {
          return { state: 'thinking', text: last.content || '' }
        }
        return { state: 'thinking', text: last.content || '' }
      }
      // 非流式：保持上一条回复可见，吉祥物回归待机
      return { state: 'idle', text: last.content || '' }
    }
    // 最后一条是用户消息：无进行中任务即待机
    return { state: 'idle', text: '' }
  }

  const flush = (state: MascotState, text: string) => {
    if (pending) {
      clearTimeout(pending)
      pending = null
    }
    lastState = state
    lastText = text
    lastPush = Date.now()
    try {
      ;(window as any).electronAPI.pushMascot(state, text)
    } catch (_) {
      /* 推送失败忽略 */
    }
  }

  const push = () => {
    const { state, text } = compute()
    const stateChanged = state !== lastState
    const textChanged = text !== lastText
    if (!stateChanged && !textChanged) return

    const now = Date.now()
    // 状态切换立即推送；纯文本增量按节流合并，避免高频 IPC
    if (stateChanged || now - lastPush >= THROTTLE_MS) {
      flush(state, text)
    } else {
      if (pending) clearTimeout(pending)
      pending = setTimeout(() => {
        const c = compute()
        flush(c.state, c.text)
      }, THROTTLE_MS - (now - lastPush))
    }
  }

  // 订阅 store：流式缓冲已 raf 合并，这里再按状态/节流去抖
  useChatStore.subscribe(push)
  // 首帧：推送当前状态（多为 idle）
  push()
}
