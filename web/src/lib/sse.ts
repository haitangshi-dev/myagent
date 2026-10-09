// SSE 客户端：用 fetch + ReadableStream 解析 /api/chat 的 text/event-stream。
// 因为需要 POST 带 body，不能用浏览器原生 EventSource（仅支持 GET）。

import { debugLog } from './debug'

export interface RawSSEEvent {
  type: string
  [key: string]: unknown
}

/**
 * 消费 /api/chat 的流式响应，按事件逐个 yield。
 * 解析规则：以空行(\n\n)分隔事件块；块内每行以 "data:" 开头的取其后 JSON。
 */
export async function* streamChat(
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<RawSSEEvent> {
  debugLog('sse', 'fetch /api/chat 开始', {
    message: (body as any)?.message,
    provider: (body as any)?.provider,
    model: (body as any)?.model,
    historyLen: Array.isArray((body as any)?.history) ? (body as any).history.length : 0,
  })

  let res: Response
  try {
    res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    })
  } catch (err: any) {
    debugLog('sse', 'fetch 失败（网络/中断）', { error: err?.message, name: err?.name }, 'error')
    throw err
  }

  debugLog('sse', `响应状态 ${res.status}`, {
    ok: res.ok,
    contentType: res.headers.get('content-type'),
    hasBody: !!res.body,
  })

  if (!res.ok || !res.body) {
    let detail = ''
    try {
      detail = await res.text()
    } catch {
      /* ignore */
    }
    debugLog('sse', `HTTP 错误 ${res.status}`, { detail }, 'error')
    throw new Error(detail || `HTTP ${res.status}`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventCount = 0
  let rawBytes = 0

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      if (value) rawBytes += value.length
      buffer += decoder.decode(value, { stream: true })

      let sep: number
      while ((sep = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const events = parseBlock(block, rawBytes)
        for (const ev of events) {
          eventCount++
          yield ev
        }
      }
    }
    // 收尾：处理残留（无尾随空行）的情况
    if (buffer.trim()) {
      for (const ev of parseBlock(buffer, rawBytes)) {
        eventCount++
        yield ev
      }
    }
    debugLog('sse', '流结束（reader done）', { totalEvents: eventCount, rawBytes })
  } finally {
    reader.releaseLock()
    debugLog('sse', 'reader 释放')
  }
}

function parseBlock(block: string, _rawBytes: number): RawSSEEvent[] {
  const out: RawSSEEvent[] = []
  const lines = block.split('\n')
  for (const raw of lines) {
    const line = raw.trim()
    if (!line.startsWith('data:')) continue
    const data = line.slice(5).trim()
    if (!data || data === '[DONE]') continue
    try {
      const parsed = JSON.parse(data) as RawSSEEvent
      out.push(parsed)
      // 注意：此处【不】逐事件 debugLog——流式期间 content/reasoning 事件每秒可达上百个，
      // 每次都会经 listeners.forEach 触发 DebugOverlay 全量重渲染，是前端卡死的根因之一。
      // 仅保留流结束处的聚合计数（见下方 streamChat 收尾），足够排查问题且零开销。
    } catch {
      debugLog('sse', '无法解析的帧（已跳过）', { raw: data.slice(0, 120) }, 'warn')
      // 跳过无法解析的帧（容错，不中断流）
    }
  }
  return out
}
