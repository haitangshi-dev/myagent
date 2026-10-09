import { useEffect, useState } from 'react'
import {
  Brain,
  Search,
  Trash2,
  Eraser,
  AlertTriangle,
  Plus,
  Pencil,
  Check,
  X,
} from 'lucide-react'
import {
  fetchMemory,
  deleteMemory,
  clearMemoryAll,
  addMemory,
  updateMemory,
  type MemoryItem,
} from '../../lib/api'

// 全局记忆可视化管理：查看 / 搜索 / 编辑 / 新增 / 删除单条 / 清空全部。
// 主权交还用户：每轮对话自动提炼（source=auto），但用户可随时增删改、看来源与失效状态。
function parseTags(raw: string): string[] {
  return raw
    .split(/[,，\s]+/)
    .map((t) => t.trim())
    .filter(Boolean)
}

export function MemoryPanel() {
  const [items, setItems] = useState<MemoryItem[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [confirmClear, setConfirmClear] = useState(false)

  // 编辑态
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editContent, setEditContent] = useState('')
  const [editTags, setEditTags] = useState('')

  // 新增态
  const [showAdd, setShowAdd] = useState(false)
  const [addContent, setAddContent] = useState('')
  const [addTags, setAddTags] = useState('')

  const load = async (q = query) => {
    setLoading(true)
    setError('')
    try {
      const data = await fetchMemory(q, 300)
      setItems(data)
    } catch (e: any) {
      setError(e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onDelete = async (it: MemoryItem) => {
    const res = await deleteMemory(it.id)
    if (!res.ok) {
      setError(res.reason || '删除失败')
      return
    }
    void load(query)
  }

  const startEdit = (it: MemoryItem) => {
    setEditingId(it.id)
    setEditContent(it.content)
    setEditTags((it.tags || []).join(', '))
  }

  const cancelEdit = () => {
    setEditingId(null)
    setEditContent('')
    setEditTags('')
  }

  const saveEdit = async () => {
    if (!editingId) return
    const c = editContent.trim()
    if (!c) {
      setError('内容不能为空')
      return
    }
    try {
      const res = await updateMemory(editingId, c, parseTags(editTags))
      if (!res.ok) {
        setError(res.reason || '保存失败')
        return
      }
      cancelEdit()
      void load(query)
    } catch (e: any) {
      setError(e?.message || '保存失败')
    }
  }

  const onAdd = async () => {
    const c = addContent.trim()
    if (!c) {
      setError('内容不能为空')
      return
    }
    try {
      const res = await addMemory(c, parseTags(addTags), 'manual')
      if (!res.ok) {
        setError(res.reason || '新增失败')
        return
      }
      setAddContent('')
      setAddTags('')
      setShowAdd(false)
      void load(query)
    } catch (e: any) {
      setError(e?.message || '新增失败')
    }
  }

  const onClearAll = async () => {
    try {
      await clearMemoryAll()
      setItems([])
      setConfirmClear(false)
    } catch (e: any) {
      setError(e?.message || '清空失败')
    }
  }

  const sourceLabel = (s?: string) => {
    if (s === 'auto') return { text: '自动', cls: 'text-skill' }
    if (s === 'import') return { text: '导入', cls: 'text-info' }
    return { text: '手动', cls: 'text-faint' }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2.5">
        <div className="relative flex-1">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && load()}
            placeholder="搜索记忆…"
            className="field w-full !py-1.5 pl-8 pr-2 text-[12.5px]"
          />
        </div>
        <button onClick={() => load()} className="btn btn-ghost h-8 px-2.5 text-[12px]">
          搜索
        </button>
        <button
          onClick={() => {
            setShowAdd((v) => !v)
            setError('')
          }}
          title="新增记忆"
          className="btn btn-ghost h-8 px-2 text-[12px] text-skill"
        >
          <Plus size={14} /> 新增
        </button>
      </div>

      <div className="flex items-center justify-between px-3 py-2 text-[11.5px] text-faint">
        <span>共 {items.length} 条全局记忆</span>
        {items.length > 0 &&
          (confirmClear ? (
            <span className="flex items-center gap-1.5">
              <span className="text-warning">确认清空？</span>
              <button onClick={onClearAll} className="btn btn-danger h-6 px-2 text-[11px]">
                确认
              </button>
              <button
                onClick={() => setConfirmClear(false)}
                className="btn btn-ghost h-6 px-2 text-[11px]"
              >
                取消
              </button>
            </span>
          ) : (
            <button
              onClick={() => setConfirmClear(true)}
              className="flex items-center gap-1 text-faint transition-colors hover:text-danger"
            >
              <Eraser size={12} /> 清空全部
            </button>
          ))}
      </div>

      <div className="flex items-center gap-1.5 px-3 pb-1.5 text-[11px] text-faint">
        <span className="inline-flex h-1.5 w-1.5 rounded-full bg-skill" />
        自动记忆已开启（每轮对话自动提炼关键事实）
      </div>

      {error && (
        <div className="mx-3 mb-2 flex items-center gap-1.5 rounded-lg border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-[12px] text-danger">
          <AlertTriangle size={12} /> {error}
        </div>
      )}

      {showAdd && (
        <div className="mx-3 mb-2 rounded-xl border border-skill/30 bg-skill/5 p-3">
          <textarea
            value={addContent}
            onChange={(e) => setAddContent(e.target.value)}
            placeholder="要记住的内容…"
            rows={3}
            className="field w-full resize-none text-[12.5px]"
          />
          <input
            value={addTags}
            onChange={(e) => setAddTags(e.target.value)}
            placeholder="标签（逗号或空格分隔，可选）"
            className="field mt-2 w-full !py-1.5 text-[12px]"
          />
          <div className="mt-2 flex justify-end gap-1.5">
            <button
              onClick={() => {
                setShowAdd(false)
                setAddContent('')
                setAddTags('')
              }}
              className="btn btn-ghost h-7 px-2.5 text-[11.5px]"
            >
              取消
            </button>
            <button onClick={onAdd} className="btn btn-primary h-7 px-2.5 text-[11.5px]">
              保存
            </button>
          </div>
        </div>
      )}

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 pb-3">
        {loading && <div className="py-8 text-center text-sm text-faint">加载中…</div>}
        {!loading && items.length === 0 && (
          <div className="py-8 text-center text-sm text-faint">
            暂无记忆（模型用 memory_remember 写入、或每轮对话自动提炼后会出现在这里）
          </div>
        )}
        {items.map((it) => {
          const superseded = it.status === 'superseded'
          const src = sourceLabel(it.source)
          const isEditing = editingId === it.id
          return (
            <div
              key={it.id}
              className={
                'group rounded-xl border border-line bg-surface/50 p-3 ' +
                (superseded ? 'opacity-50' : '')
              }
            >
              {isEditing ? (
                <div>
                  <textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    rows={3}
                    className="field w-full resize-none text-[12.5px]"
                  />
                  <input
                    value={editTags}
                    onChange={(e) => setEditTags(e.target.value)}
                    placeholder="标签（逗号或空格分隔）"
                    className="field mt-2 w-full !py-1.5 text-[12px]"
                  />
                  <div className="mt-2 flex justify-end gap-1.5">
                    <button
                      onClick={cancelEdit}
                      className="btn btn-ghost h-7 px-2.5 text-[11.5px]"
                    >
                      取消
                    </button>
                    <button
                      onClick={saveEdit}
                      className="btn btn-primary h-7 px-2.5 text-[11.5px]"
                    >
                      保存
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex items-start gap-2">
                  <Brain size={14} className="mt-0.5 shrink-0 text-skill" />
                  <div className="min-w-0 flex-1">
                    <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-ink">
                      {it.content}
                    </p>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10.5px] text-faint">
                      <span className="font-mono">{it.ts}</span>
                      <span className={'chip !px-1.5 !py-0.5 !text-[10px] ' + src.cls}>
                        {src.text}
                      </span>
                      {superseded && (
                        <span className="chip !px-1.5 !py-0.5 !text-[10px] text-faint">
                          已失效
                        </span>
                      )}
                      {it.tags.map((t) => (
                        <span key={t} className="chip !px-1.5 !py-0.5 !text-[10px]">
                          #{t}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <button
                      onClick={() => startEdit(it)}
                      title="编辑这条记忆"
                      className="btn-ghost h-7 w-7 rounded-lg p-0 text-faint opacity-0 transition-opacity hover:text-info group-hover:opacity-100"
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      onClick={() => onDelete(it)}
                      title="删除这条记忆"
                      className="btn-ghost h-7 w-7 rounded-lg p-0 text-faint opacity-0 transition-opacity hover:text-danger group-hover:opacity-100"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
