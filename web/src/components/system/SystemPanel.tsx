import { useEffect, useRef, useState } from 'react'
import { Monitor, Clipboard, Eye, Trash2, Plus, Copy, Pause, Play, FolderOpen, Sparkles, MessageSquarePlus, CalendarDays, RefreshCw, ShieldAlert, FolderPlus, X } from 'lucide-react'
import { fetchAmbientState, fetchProactiveSuggest, fetchDailySummary, fetchDeleteConfirmDirs, saveDeleteConfirmDirs, type AmbientState, type DeleteConfirmDirsResponse } from '../../lib/api'
import { useUIStore } from '../../store/useUIStore'
import { useChatStore } from '../../store/useChatStore'
import { debugLog } from '../../lib/debug'

interface ClipEntry {
  text: string
  ts: number
}

function relTime(ts: number): string {
  const d = Date.now() - ts
  if (d < 60_000) return '刚刚'
  if (d < 3_600_000) return `${Math.floor(d / 60_000)} 分钟前`
  if (d < 86_400_000) return `${Math.floor(d / 3_600_000)} 小时前`
  return new Date(ts).toLocaleDateString()
}

function AmbientCard() {
  const [state, setState] = useState<AmbientState | null>(null)
  const [err, setErr] = useState(false)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const s = await fetchAmbientState()
        if (alive) {
          setState(s)
          setErr(false)
        }
      } catch {
        if (alive) setErr(true)
      }
    }
    void tick()
    const t = setInterval(tick, 3000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  const aw = state?.active_window
  return (
    <div className="rounded-xl border border-line bg-surface/50 p-3">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-ink">
        <Eye size={14} className="text-accent-strong" /> 环境感知
      </div>
      {err ? (
        <p className="text-[12px] text-faint">后端未连接</p>
      ) : aw ? (
        <div className="text-[12.5px] leading-relaxed text-dim">
          <span className="chip !px-1.5 !py-0.5 !text-[10px]">
            {aw.app || '未知应用'}
          </span>
          <p className="mt-1.5 break-words">{aw.title}</p>
        </div>
      ) : (
        <p className="text-[12px] text-faint">
          无法读取前台窗口（依赖缺失或未授权）
        </p>
      )}
      <p className="mt-2 text-[11px] leading-relaxed text-faint">
        这是「主动插话」的信号源：知道你正在用什么，才能在合适时轻声提醒。
      </p>
    </div>
  )
}

function ClipboardCard() {
  const api = (window as any).electronAPI
  const setPendingComposer = useUIStore((s) => s.setPendingComposer)
  const [items, setItems] = useState<ClipEntry[]>([])
  const [watching, setWatching] = useState(false)
  const [available] = useState<boolean>(!!api?.getClipboardHistory)

  const reload = async () => {
    if (!api?.getClipboardHistory) return
    try {
      const list = (await api.getClipboardHistory()) as ClipEntry[]
      setItems(list || [])
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  const toggleWatch = async () => {
    if (!api?.setClipboardWatch) return
    try {
      const on = !watching
      await api.setClipboardWatch(on)
      setWatching(on)
      if (on) setTimeout(reload, 900)
      debugLog('clipboard', '监听切换', { on })
    } catch {
      /* ignore */
    }
  }

  const clearAll = async () => {
    if (!api?.clearClipboardHistory) return
    try {
      await api.clearClipboardHistory()
      setItems([])
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="rounded-xl border border-line bg-surface/50 p-3">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-ink">
        <Clipboard size={14} className="text-accent-strong" /> 剪贴板历史
        <span className="ml-auto flex items-center gap-1">
          {available && (
            <button
              onClick={toggleWatch}
              className="btn-ghost flex items-center gap-1 rounded-lg px-2 py-1 text-[11px]"
              title={watching ? '暂停监听' : '开启监听'}
            >
              {watching ? <Pause size={11} /> : <Play size={11} />}
              {watching ? '监听中' : '开启'}
            </button>
          )}
          {items.length > 0 && (
            <button
              onClick={clearAll}
              className="btn-ghost flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] text-rose-300"
              title="清空全部"
            >
              <Trash2 size={11} /> 清空
            </button>
          )}
        </span>
      </div>

      {!available ? (
        <p className="text-[12px] text-faint">仅桌面端（Electron）可用</p>
      ) : items.length === 0 ? (
        <p className="text-[12px] text-faint">
          {watching ? '复制点东西试试…' : '点「开启」开始记录复制内容'}
        </p>
      ) : (
        <div className="space-y-1.5">
          {items.slice(0, 30).map((it, i) => (
            <div
              key={i}
              className="group rounded-lg border border-line/70 bg-bg/40 p-2"
            >
              <p className="line-clamp-2 break-words text-[12px] leading-snug text-dim">
                {it.text}
              </p>
              <div className="mt-1 flex items-center gap-2 text-[10.5px] text-faint">
                <span>{relTime(it.ts)}</span>
                <span className="ml-auto flex gap-2 opacity-0 transition-opacity group-hover:opacity-100">
                  <button
                    onClick={() => setPendingComposer(it.text)}
                    className="flex items-center gap-0.5 hover:text-accent-strong"
                    title="填入输入框"
                  >
                    <Plus size={11} /> 填入
                  </button>
                  <button
                    onClick={() => navigator.clipboard?.writeText(it.text)}
                    className="flex items-center gap-0.5 hover:text-accent-strong"
                    title="再次复制"
                  >
                    <Copy size={11} /> 复制
                  </button>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function DownloadTidyCard() {
  const sendMessage = useChatStore((s) => s.sendMessage)
  const [busy, setBusy] = useState(false)

  const run = async () => {
    if (busy) return
    setBusy(true)
    try {
      await sendMessage('请先用 organize_downloads(mode="preview") 给我看看下载文件夹的整理方案，确认无误后我再让你 apply。')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-xl border border-line bg-surface/50 p-3">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-ink">
        <FolderOpen size={14} className="text-accent-strong" /> 下载整理
      </div>
      <p className="mb-2 text-[12px] leading-relaxed text-dim">
        把散落的下载文件按类型（图片/视频/文档/压缩包/代码/安装包）归类到子目录。
        先预览方案，确认后再移动。
      </p>
      <button
        onClick={run}
        disabled={busy}
        className="btn-primary w-full rounded-lg px-3 py-1.5 text-[12px] disabled:opacity-50"
      >
        {busy ? '整理中…' : '预览下载文件夹整理方案'}
      </button>
    </div>
  )
}

function ProactiveCard() {
  const sendMessage = useChatStore((s) => s.sendMessage)
  const [suggestion, setSuggestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [auto, setAuto] = useState(false)
  const lastWindowRef = useRef('')
  const lastTextRef = useRef('')

  const suggest = async () => {
    if (busy) return
    setBusy(true)
    try {
      let ctx: AmbientState | null = null
      try {
        ctx = await fetchAmbientState()
      } catch {
        /* 环境读取失败也能搭话 */
      }
      const res = await fetchProactiveSuggest(ctx)
      const text = res.suggestion?.trim()
      if (text && text !== lastTextRef.current) {
        lastTextRef.current = text
        setSuggestion(text)
      }
    } catch {
      /* 失败静默：保留上一条 */
    } finally {
      setBusy(false)
    }
  }

  // 自动模式：每 120s 根据当前前台窗口变化搭一次话（同一窗口不重复刷）
  useEffect(() => {
    if (!auto) return
    let alive = true
    const tick = async () => {
      try {
        const ctx = await fetchAmbientState()
        const key = ctx?.active_window?.title || ''
        if (key && key !== lastWindowRef.current) {
          lastWindowRef.current = key
          if (alive) await suggest()
        }
      } catch {
        /* ignore */
      }
    }
    void tick()
    const t = setInterval(tick, 120_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [auto])

  return (
    <div className="rounded-xl border border-line bg-surface/50 p-3">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-ink">
        <Sparkles size={14} className="text-accent-strong" /> 主动搭话
        <span className="ml-auto flex items-center gap-1">
          <button
            onClick={() => setAuto((v) => !v)}
            className="btn-ghost flex items-center gap-1 rounded-lg px-2 py-1 text-[11px]"
            title={auto ? '关闭自动搭话' : '开启自动搭话'}
          >
            {auto ? <Pause size={11} /> : <Play size={11} />}
            {auto ? '自动中' : '自动'}
          </button>
        </span>
      </div>

      {suggestion ? (
        <div className="rounded-lg border border-line/70 bg-bg/40 p-2.5">
          <p className="text-[12.5px] leading-relaxed text-dim">{suggestion}</p>
          <button
            onClick={() => sendMessage(suggestion)}
            className="mt-2 flex items-center gap-1 text-[11px] text-accent-strong hover:underline"
            title="把这句话丢给助手继续聊"
          >
            <MessageSquarePlus size={11} /> 说给助手
          </button>
        </div>
      ) : (
        <p className="text-[12px] text-faint">
          {busy ? '助手正在想…' : '点下面的按钮，让助手根据你正在用的程序主动说点什么。'}
        </p>
      )}

      <button
        onClick={suggest}
        disabled={busy}
        className="btn-primary mt-2 w-full rounded-lg px-3 py-1.5 text-[12px] disabled:opacity-50"
      >
        {busy ? '思考中…' : '搭个话'}
      </button>
      <p className="mt-2 text-[11px] leading-relaxed text-faint">
        开启「自动」后，助手每隔一段时间看一眼你在用什么，换程序才轻轻搭一句，不打扰。
      </p>
    </div>
  )
}

function DailySummaryCard() {
  const sendMessage = useChatStore((s) => s.sendMessage)
  const [summary, setSummary] = useState('')
  const [date, setDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [auto, setAuto] = useState(false)

  const generate = async () => {
    if (busy) return
    setBusy(true)
    try {
      const res = await fetchDailySummary()
      const text = res.summary?.trim()
      if (text) {
        setSummary(text)
        setDate(res.date || '')
      }
    } catch {
      /* 失败静默：保留上一条 */
    } finally {
      setBusy(false)
    }
  }

  // 打开面板先生成一次今日摘要
  useEffect(() => {
    void generate()
    // 仅挂载时执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 自动模式：每 45 分钟刷新一次今日摘要（面板常开时保持「自治感知」）
  useEffect(() => {
    if (!auto) return
    const t = setInterval(() => void generate(), 45 * 60_000)
    return () => clearInterval(t)
  }, [auto])

  return (
    <div className="rounded-xl border border-line bg-surface/50 p-3">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-ink">
        <CalendarDays size={14} className="text-accent-strong" /> 每日摘要
        <span className="ml-auto flex items-center gap-1">
          <button
            onClick={() => setAuto((v) => !v)}
            className="btn-ghost flex items-center gap-1 rounded-lg px-2 py-1 text-[11px]"
            title={auto ? '关闭自动刷新' : '开启自动刷新'}
          >
            {auto ? <Pause size={11} /> : <Play size={11} />}
            {auto ? '自动中' : '自动'}
          </button>
        </span>
      </div>

      {date && <p className="mb-1.5 text-[11px] text-faint">{date}</p>}

      {summary ? (
        <div className="rounded-lg border border-line/70 bg-bg/40 p-2.5">
          <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-dim">
            {summary}
          </p>
          <button
            onClick={() => sendMessage(summary)}
            className="mt-2 flex items-center gap-1 text-[11px] text-accent-strong hover:underline"
            title="把今日摘要丢给助手继续聊"
          >
            <MessageSquarePlus size={11} /> 说给助手
          </button>
        </div>
      ) : (
        <p className="text-[12px] text-faint">
          {busy ? '助手正在总结今天…' : '正在生成今日摘要…'}
        </p>
      )}

      <button
        onClick={generate}
        disabled={busy}
        className="btn-primary mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] disabled:opacity-50"
      >
        <RefreshCw size={12} className={busy ? 'animate-spin' : ''} />
        {busy ? '生成中…' : '重新生成今日摘要'}
      </button>
    </div>
  )
}

function DeleteConfirmCard() {
  const api = (window as any).electronAPI
  const [data, setData] = useState<DeleteConfirmDirsResponse | null>(null)
  const [saving, setSaving] = useState(false)

  const reload = async () => {
    try {
      setData(await fetchDeleteConfirmDirs())
    } catch {
      /* 后端未就绪 */
    }
  }
  useEffect(() => {
    void reload()
  }, [])

  const addDirs = async () => {
    if (!api?.selectFolders) return
    try {
      const picked: string[] = (await api.selectFolders()) || []
      if (!picked.length) return
      const cur = data?.dirs ?? []
      // 去重合并（后端还会过滤系统保护目录）
      const merged = Array.from(new Set([...cur, ...picked]))
      setSaving(true)
      try {
        const r = await saveDeleteConfirmDirs(merged)
        setData((d) => (d ? { ...d, dirs: r.dirs } : d))
        debugLog('security', '删除确认目录已保存', { dirs: r.dirs })
      } finally {
        setSaving(false)
      }
    } catch {
      /* ignore */
    }
  }

  const removeDir = async (p: string) => {
    const cur = (data?.dirs ?? []).filter((x) => x !== p)
    setSaving(true)
    try {
      const r = await saveDeleteConfirmDirs(cur)
      setData((d) => (d ? { ...d, dirs: r.dirs } : d))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="rounded-xl border border-line bg-surface/50 p-3">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-ink">
        <ShieldAlert size={14} className="text-danger" /> 删除确认目录
        <span className="ml-auto">
          <button
            onClick={addDirs}
            disabled={saving}
            className="btn-ghost flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] disabled:opacity-50"
            title="通过资源管理器多选目录（Ctrl/Shift 可多选）"
          >
            <FolderPlus size={11} /> {saving ? '保存中…' : '添加目录'}
          </button>
        </span>
      </div>

      <p className="mb-2 text-[12px] leading-relaxed text-dim">
        开最高权限后，对这些目录内的删除仍会先问你。其余位置 high 权限可直接删。
      </p>

      {!data || data.dirs.length === 0 ? (
        <p className="text-[12px] text-faint">未配置（默认所有删除都需确认）</p>
      ) : (
        <div className="space-y-1.5">
          {data.dirs.map((p) => (
            <div
              key={p}
              className="group flex items-center gap-2 rounded-lg border border-line/70 bg-bg/40 px-2 py-1.5"
            >
              <FolderOpen size={13} className="shrink-0 text-dim" />
              <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-dim" title={p}>
                {p}
              </span>
              <button
                onClick={() => removeDir(p)}
                className="shrink-0 text-faint hover:text-danger"
                title="移出确认列表"
              >
                <X size={13} />
              </button>
            </div>
          ))}
        </div>
      )}

      {data && data.protected.length > 0 && (
        <div className="mt-2 border-t border-line/60 pt-2">
          <p className="mb-1 text-[11px] font-medium text-faint">
            系统保护（永远禁止删除，不可配置解除）
          </p>
          <div className="space-y-0.5">
            {data.protected.map((p) => (
              <p key={p} className="truncate font-mono text-[11px] text-faint/80" title={p}>
                {p}
              </p>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export function SystemPanel() {
  return (
    <div className="space-y-3 p-3">
      <div className="flex items-center gap-2 text-[12px] text-faint">
        <Monitor size={13} /> 系统嵌入 · 感知与上下文
      </div>
      <AmbientCard />
      <ClipboardCard />
      <DownloadTidyCard />
      <ProactiveCard />
      <DailySummaryCard />
      <DeleteConfirmCard />
    </div>
  )
}
