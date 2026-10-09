import { useEffect, useState } from 'react'
import { FolderOpen, RefreshCw } from 'lucide-react'
import { useChatStore } from '../../store/useChatStore'
import { selectFolder, fetchWorkspaceMemory } from '../../lib/api'
import type { SandboxPolicy } from '../../lib/types'

const DEFAULT_SANDBOX: SandboxPolicy = {
  allow_file_write: true,
  allow_file_delete: false,
  allow_shell: true,
}

function Toggle({
  label,
  hint,
  on,
  onChange,
}: {
  label: string
  hint: string
  on: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <button
      onClick={() => onChange(!on)}
      className={`flex flex-col items-start gap-0.5 rounded-lg border px-2.5 py-1.5 text-left transition-colors ${
        on
          ? 'border-accent/40 bg-accent/10'
          : 'border-line bg-surface hover:bg-white/5'
      }`}
    >
      <span className="flex items-center gap-1.5 text-[12px] font-medium text-ink">
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            on ? 'bg-accent' : 'bg-faint'
          }`}
        />
        {label}
      </span>
      <span className="text-[10.5px] text-faint">{hint}</span>
    </button>
  )
}

export function WorkspaceBar() {
  const activeId = useChatStore((s) => s.activeId)
  const sessions = useChatStore((s) => s.sessions)
  const setWorkspaceDir = useChatStore((s) => s.setWorkspaceDir)
  const setSandbox = useChatStore((s) => s.setSandbox)

  const session = activeId ? sessions[activeId] : null
  const isWs = session?.kind === 'workspace'
  const dir = session?.workspaceDir ?? null
  const sandbox = session?.sandbox ?? (isWs ? DEFAULT_SANDBOX : null)

  const [memCount, setMemCount] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)

  // 拉取工作区记忆条数
  useEffect(() => {
    if (!isWs || !dir) {
      setMemCount(null)
      return
    }
    let alive = true
    fetchWorkspaceMemory(dir, '', 100)
      .then((r) => {
        if (alive) setMemCount(Array.isArray(r.memory) ? r.memory.length : 0)
      })
      .catch(() => alive && setMemCount(null))
    return () => {
      alive = false
    }
  }, [isWs, dir])

  if (!isWs) return null

  async function onChangeDir() {
    setBusy(true)
    try {
      const picked = await selectFolder()
      if (picked && activeId) {
        await setWorkspaceDir(activeId, picked)
      }
    } finally {
      setBusy(false)
    }
  }

  function patchSandbox(key: keyof SandboxPolicy, v: boolean) {
    if (!sandbox) return
    void setSandbox(session!.id, { ...sandbox, [key]: v })
  }

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-line bg-raise/40 px-4 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <span className="chip shrink-0 !bg-skill/15 !text-skill">工作区</span>
        <span className="max-w-[280px] truncate font-mono text-[12px] text-dim" title={dir ?? ''}>
          {dir ?? '未选择工作目录'}
        </span>
        <button
          onClick={onChangeDir}
          disabled={busy}
          className="btn-ghost shrink-0 rounded-lg px-2 py-1 text-[12px]"
          title="更换工作目录"
        >
          {busy ? <RefreshCw size={13} className="animate-spin" /> : <FolderOpen size={13} />}
          更改
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Toggle
          label="写文件"
          hint={sandbox?.allow_file_write ? '允许' : '禁止'}
          on={!!sandbox?.allow_file_write}
          onChange={(v) => patchSandbox('allow_file_write', v)}
        />
        <Toggle
          label="删文件"
          hint={sandbox?.allow_file_delete ? '允许（危险）' : '禁止'}
          on={!!sandbox?.allow_file_delete}
          onChange={(v) => patchSandbox('allow_file_delete', v)}
        />
        <Toggle
          label="Shell"
          hint={sandbox?.allow_shell ? '允许' : '禁止'}
          on={!!sandbox?.allow_shell}
          onChange={(v) => patchSandbox('allow_shell', v)}
        />
      </div>

      <div className="ml-auto text-[11.5px] text-faint">
        记忆：{memCount === null ? '—' : `${memCount} 条`}
      </div>
    </div>
  )
}
