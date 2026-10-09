// 极细调试日志系统：console + 内存环形缓冲（供屏幕内 DebugOverlay 渲染）。
// 设计目标：打包后的 exe 也能直接看到日志，无需 DevTools。
// 细到：每次点击、每个 SSE 事件、每次 store 状态变更都记录。

export type LogLevel = 'info' | 'warn' | 'error' | 'sse' | 'click'

export interface LogEntry {
  t: number
  level: LogLevel
  cat: string
  msg: string
  data?: unknown
}

const MAX = 800
const buffer: LogEntry[] = []
const listeners = new Set<() => void>()

const CAT_COLORS: Record<LogLevel, string> = {
  info: '#9aa4b2',
  warn: '#e6b450',
  error: '#ff6b6b',
  sse: '#3ddc97',
  click: '#6ea8fe',
}

export function debugLog(
  cat: string,
  msg: string,
  data?: unknown,
  level: LogLevel = 'info',
) {
  const entry: LogEntry = { t: Date.now(), level, cat, msg, data }
  buffer.push(entry)
  if (buffer.length > MAX) buffer.shift()

  const ts = new Date(entry.t).toISOString().slice(11, 23)
  const prefix = `[${ts}][${cat}]`
  const payload = data !== undefined ? data : ''
  if (level === 'error') console.error(prefix, msg, payload)
  else if (level === 'warn') console.warn(prefix, msg, payload)
  else console.log(prefix, msg, payload)

  listeners.forEach((l) => l())
}

export function getLogBuffer(): LogEntry[] {
  return buffer.slice()
}

export function clearLog() {
  buffer.length = 0
  listeners.forEach((l) => l())
}

export function subscribeLog(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function levelColor(l: LogLevel): string {
  return CAT_COLORS[l] ?? '#9aa4b2'
}

// 在 window 上挂一个快捷函数，方便控制台直接调：__dbg('cat','msg',data)
;(window as any).__dbg = debugLog
