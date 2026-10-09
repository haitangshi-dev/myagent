import { useEffect, useRef, useState } from 'react'
import {
  debugLog,
  getLogBuffer,
  clearLog,
  subscribeLog,
  levelColor,
  type LogEntry,
} from '../lib/debug'
import { useDebugStore } from '../store/useDebugStore'

// 最多渲染末尾多少条，避免日志面板 DOM 无限增长导致卡顿
const MAX_RENDER = 200

function fmt(t: number): string {
  return new Date(t).toISOString().slice(11, 23)
}

function renderData(d: unknown): string {
  if (d === undefined) return ''
  try {
    const s = typeof d === 'string' ? d : JSON.stringify(d)
    return s.length > 240 ? s.slice(0, 240) + '…' : s
  } catch {
    return String(d)
  }
}

export function DebugOverlay() {
  const visible = useDebugStore((s) => s.visible)
  const [entries, setEntries] = useState<LogEntry[]>(getLogBuffer())
  const [autoscroll, setAutoscroll] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)

  // ★ 仅在面板可见时才订阅日志 + 触发重渲染；隐藏时零开销（此前无条件订阅，
  //    每次日志都 setEntries 全量重渲染，是流式卡死的根因之一）
  useEffect(() => {
    if (!visible) {
      setEntries([])
      return
    }
    setEntries(getLogBuffer())
    const unsub = subscribeLog(() => setEntries(getLogBuffer()))
    return unsub
  }, [visible])

  useEffect(() => {
    if (autoscroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [entries, autoscroll, visible])

  if (!visible) return null

  return (
    <div
      style={{
        position: 'fixed',
        right: 12,
        bottom: 12,
        width: 460,
        maxHeight: '46vh',
        // 调试面板是角落工具层，必须低于阻塞式 modal（z-60/70），否则会盖住 modal 右下角按钮
        zIndex: 40,
        background: 'rgba(8,11,15,0.94)',
        border: '1px solid #1b2733',
        borderRadius: 10,
        display: 'flex',
        flexDirection: 'column',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: 11,
        boxShadow: '0 8px 30px rgba(0,0,0,0.5)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 10px',
          borderBottom: '1px solid #1b2733',
          color: '#9aa4b2',
        }}
      >
        <span style={{ color: '#3ddc97', fontWeight: 700 }}>● DEBUG</span>
        <span>共 {entries.length} 条</span>
        <label style={{ display: 'flex', alignItems: 'center', gap: 3, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={autoscroll}
            onChange={(e) => setAutoscroll(e.target.checked)}
          />
          自动滚
        </label>
        <button
          onClick={() => {
            clearLog()
            setEntries([])
          }}
          style={btn}
        >
          清空
        </button>
        <button onClick={() => useDebugStore.getState().set(false)} style={btn}>
          关闭
        </button>
      </div>
      <div
        ref={scrollRef}
        style={{ overflowY: 'auto', padding: '6px 10px', flex: 1, lineHeight: 1.5 }}
      >
        {entries.length === 0 && (
          <div style={{ color: '#5a6675' }}>暂无日志。点击界面任意处 / 发消息试试。</div>
        )}
        {entries.slice(-MAX_RENDER).map((e, i) => (
          <div key={i} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            <span style={{ color: '#5a6675' }}>{fmt(e.t)}</span>{' '}
            <span style={{ color: levelColor(e.level) }}>[{e.cat}]</span>{' '}
            <span style={{ color: '#d7dce3' }}>{e.msg}</span>
            {e.data !== undefined && (
              <div style={{ color: '#7d8794', paddingLeft: 8 }}>{renderData(e.data)}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

const btn: React.CSSProperties = {
  marginLeft: 'auto',
  background: '#15202b',
  color: '#9aa4b2',
  border: '1px solid #1b2733',
  borderRadius: 6,
  padding: '2px 8px',
  fontSize: 11,
  cursor: 'pointer',
}

// 全局：Ctrl+Shift+D 切换；并在首次导入时记录一次
if (!(window as any).__debugWired) {
  ;(window as any).__debugWired = true
  debugLog('debug', '调试系统已加载。Ctrl+Shift+D 开关面板')
}
