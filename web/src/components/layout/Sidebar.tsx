import { useState } from 'react'
import { motion } from 'framer-motion'
import { Check, FolderPlus, MessageSquarePlus, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { useChatStore } from '../../store/useChatStore'
import { useUIStore } from '../../store/useUIStore'
import { selectFolder } from '../../lib/api'
import { dateLabel, relativeTime } from '../../lib/format'

// 折叠态：常驻 48px 细条（rail），提供展开入口，避免折叠后无处恢复
function SidebarRail() {
  const order = useChatStore((s) => s.order)
  const newSession = useChatStore((s) => s.newSession)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  return (
    <motion.aside
      initial={{ x: -16, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: -16, opacity: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      className="flex w-12 shrink-0 flex-col items-center gap-2 border-r border-line bg-raise/60 py-3"
    >
      <button
        onClick={toggleSidebar}
        title="展开侧栏"
        className="btn-ghost h-9 w-9 rounded-xl p-0"
      >
        <MessageSquarePlus size={18} />
      </button>
      <div className="my-1 h-px w-6 bg-line" />
      <button
        onClick={() => newSession()}
        title="新聊天会话"
        className="btn-ghost h-9 w-9 rounded-xl p-0"
      >
        <Plus size={18} />
      </button>
      <button
        onClick={toggleSidebar}
        title="展开侧栏"
        className="btn-ghost mt-auto h-9 w-9 rounded-xl p-0 text-faint hover:text-dim"
      >
        <FolderPlus size={18} />
      </button>
      <span className="mt-1 rounded-md bg-surface/60 px-1.5 py-0.5 text-[10px] text-faint">
        {order.length}
      </span>
    </motion.aside>
  )
}

export function Sidebar() {
  const sidebarOpen = useUIStore((s) => s.sidebarOpen)
  const order = useChatStore((s) => s.order)
  const sessions = useChatStore((s) => s.sessions)
  const activeId = useChatStore((s) => s.activeId)
  const newSession = useChatStore((s) => s.newSession)
  const setWorkspaceDir = useChatStore((s) => s.setWorkspaceDir)
  const selectSession = useChatStore((s) => s.selectSession)
  const renameSession = useChatStore((s) => s.renameSession)
  const deleteSession = useChatStore((s) => s.deleteSession)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [wsBusy, setWsBusy] = useState(false)

  // 折叠态 → 渲染常驻 rail
  if (!sidebarOpen) return <SidebarRail />

  function startRename(id: string, name: string) {
    setEditingId(id)
    setDraft(name)
  }
  function commitRename() {
    if (editingId) renameSession(editingId, draft)
    setEditingId(null)
  }

  async function createWorkspace() {
    if (wsBusy) return
    setWsBusy(true)
    try {
      const dir = await selectFolder()
      if (!dir) return
      // 创建工作区会话（绑定目录 + 默认沙盒），随后从后端拉取/部署沙盒
      newSession('workspace', dir)
      const id = useChatStore.getState().activeId
      if (id) await setWorkspaceDir(id, dir)
    } finally {
      setWsBusy(false)
    }
  }

  return (
    <motion.aside
      initial={{ x: -16, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: -16, opacity: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      className="flex w-[240px] shrink-0 flex-col border-r border-line bg-raise/60"
    >
      <div className="flex items-center justify-between px-4 py-3.5">
        <span className="text-[13px] font-semibold uppercase tracking-wide text-faint">
          会话
        </span>
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => newSession()}
            title="新聊天会话"
            className="btn-ghost h-8 w-8 rounded-lg p-0"
          >
            <Plus size={16} />
          </button>
          <button
            onClick={createWorkspace}
            disabled={wsBusy}
            title="新工作区会话（选择工作目录）"
            className="btn-ghost h-8 w-8 rounded-lg p-0 text-skill"
          >
            {wsBusy ? <RefreshCw size={16} className="animate-spin" /> : <FolderPlus size={16} />}
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2.5 pb-3">
        {order.length === 0 && (
          <div className="px-3 py-6 text-center text-xs text-faint">
            还没有会话，点右上角 + 新建。
          </div>
        )}
        {order.map((id) => {
          const s = sessions[id]
          if (!s) return null
          const active = id === activeId
          const last = s.messages[s.messages.length - 1]
          const snippet = last
            ? (last.role === 'user' ? '你：' : '') +
              (last.content || (last.toolCalls[0]?.label ?? '…')).slice(0, 40)
            : '空会话'
          const editing = editingId === id

          return (
            <div
              key={id}
              onClick={() => !editing && selectSession(id)}
              className={`group relative cursor-pointer rounded-xl border px-3 py-2.5 transition-colors duration-200 ease-expo ${
                active
                  ? 'border-accent/30 bg-accent/10'
                  : 'border-transparent hover:bg-surface'
              }`}
            >
              {editing ? (
                <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
                  <input
                    autoFocus
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitRename()
                      if (e.key === 'Escape') setEditingId(null)
                    }}
                    className="field flex-1 py-1 text-[13px]"
                  />
                  <button onClick={commitRename} className="btn-accent h-7 w-7 rounded-lg p-0">
                    <Check size={14} />
                  </button>
                  <button
                    onClick={() => setEditingId(null)}
                    className="btn-ghost h-7 w-7 rounded-lg p-0"
                  >
                    <X size={14} />
                  </button>
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-1.5">
                      {s.kind === 'workspace' && (
                        <span className="chip shrink-0 !bg-skill/15 !px-1.5 !py-0 !text-[9.5px] !text-skill">
                          区
                        </span>
                      )}
                      <span
                        className={`truncate text-[13.5px] font-medium ${
                          active ? 'text-ink' : 'text-dim'
                        }`}
                      >
                        {s.name}
                      </span>
                    </span>
                    <span className="shrink-0 text-[10.5px] text-faint">
                      {dateLabel(s.updatedAt)}
                    </span>
                  </div>
                  <div className="mt-0.5 flex items-center justify-between gap-2">
                    <span className="truncate text-[11.5px] text-faint">
                      {s.kind === 'workspace' && s.workspaceDir
                        ? s.workspaceDir.split(/[\\/]/).slice(-1)[0]
                        : snippet}
                    </span>
                  </div>

                  <div className="absolute right-2 top-2 flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                    <button
                      title="重命名"
                      onClick={(e) => {
                        e.stopPropagation()
                        startRename(id, s.name)
                      }}
                      className="rounded-md p-1 text-faint hover:bg-white/5 hover:text-dim"
                    >
                      <Pencil size={12} />
                    </button>
                    <button
                      title="删除"
                      onClick={(e) => {
                        e.stopPropagation()
                        deleteSession(id)
                      }}
                      className="rounded-md p-1 text-faint hover:bg-danger/10 hover:text-danger"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </>
              )}
            </div>
          )
        })}
      </div>

      <div className="border-t border-line px-4 py-3 text-[10.5px] text-faint">
        会话自动保存于本机
      </div>
    </motion.aside>
  )
}
