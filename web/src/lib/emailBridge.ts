// 邮件网关事件桥：全局唯一 /api/email/stream 连接，向所有订阅者分发。
//
// 背景：EmailPanel 原先自己 new EventSource，但组件卸载就断线——邮件到来时若面板
// 未打开，「唤醒 Agent 的完整过程」就无法在 chat 界面渲染。这里改成模块级单例：
// 只要 App 挂载就常驻连接，EmailPanel 与「邮件→chat 渲染桥」都订阅同一份事件流，
// 避免重复连接、也不怕面板开关导致事件丢失。

import { debugLog } from './debug'

export type EmailEvent = Record<string, any>

type Listener = (ev: EmailEvent) => void

let es: EventSource | null = null
const listeners = new Set<Listener>()

function ensureOpen() {
  if (es) return
  try {
    es = new EventSource('/api/email/stream')
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data) as EmailEvent
        for (const cb of listeners) {
          try {
            cb(ev)
          } catch {
            /* 单个订阅者异常不影响分发 */
          }
        }
      } catch {
        /* 忽略无法解析的帧 */
      }
    }
    es.onerror = () => {
      /* 断线由浏览器自动重连（EventSource 内建）；这里不处理 */
    }
    debugLog('email', 'emailBridge 已连接 /api/email/stream')
  } catch {
    /* 环境不支持 EventSource 时静默降级（调用方依赖轮询兜底） */
  }
}

/** 订阅邮件网关事件；返回取消订阅函数。App 挂载时订阅一次即常驻。 */
export function subscribeEmailEvents(cb: Listener): () => void {
  listeners.add(cb)
  ensureOpen()
  return () => {
    listeners.delete(cb)
  }
}

/** 显式断开（测试/热更新用；正常不需要调用）。 */
export function closeEmailEvents() {
  es?.close()
  es = null
  listeners.clear()
}
