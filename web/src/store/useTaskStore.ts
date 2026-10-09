import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { TaskItem } from '../lib/types'
import { fetchTasks } from '../lib/api'
import { debugLog } from '../lib/debug'

interface TaskState {
  // 按 session_id 维度存放任务清单（前端本地缓存，便于刷新后快速恢复）
  boards: Record<string, TaskItem[]>
  setTasks: (sid: string, tasks: TaskItem[]) => void
  clear: (sid: string) => void
  // 从后端拉取某会话的任务清单（切换会话时调用，覆盖本地缓存）
  hydrate: (sid: string) => Promise<void>
}

export const useTaskStore = create<TaskState>()(
  persist(
    (set) => ({
      boards: {},
      setTasks: (sid, tasks) =>
        set((st) => ({ boards: { ...st.boards, [sid]: tasks } })),
      clear: (sid) =>
        set((st) => {
          const boards = { ...st.boards }
          delete boards[sid]
          return { boards }
        }),
      hydrate: async (sid) => {
        if (!sid) return
        try {
          const tasks = await fetchTasks(sid)
          set((st) => ({ boards: { ...st.boards, [sid]: tasks } }))
          debugLog('task', 'hydrate 完成', { sid, count: tasks.length })
        } catch {
          /* 后端未连 / 无任务：保留本地缓存即可 */
        }
      },
    }),
    { name: 'my-agent-tasks' },
  ),
)
