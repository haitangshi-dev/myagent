import { useUIStore } from '../../store/useUIStore'

// ★ 权限档位滑动条（2026-08-07）
// 三档：低=只读（搜索/读文件） / 中=常规（写文件+命令，默认） / 高=接管电脑（桌面自动化/卸载软件）
// 档位随请求传给后端 → 过滤模型可见/可用的工具集；模型可经 request_permission 申请临时提权。
const LEVELS = [
  { key: 'low', label: '低', desc: '只读', tip: '仅搜索/读文件/看网页，不可写不可执行' },
  { key: 'medium', label: '中', desc: '常规', tip: '写文件+执行命令（默认），危险操作需确认' },
  { key: 'high', label: '高', desc: '接管电脑', tip: '桌面自动化/卸载软件/系统级操作，需谨慎' },
] as const

export function PermissionSlider() {
  const level = useUIStore((s) => s.permissionLevel)
  const setLevel = useUIStore((s) => s.setPermissionLevel)

  const idx = LEVELS.findIndex((l) => l.key === level)
  const cur = LEVELS[Math.max(0, idx)]

  return (
    <div className="flex items-center gap-2 px-1 pb-0.5" title={cur.tip}>
      <span className="shrink-0 text-[10.5px] font-medium text-faint">权限</span>
      <div className="flex flex-1 items-center gap-1.5">
        {LEVELS.map((l, i) => (
          <div
            key={l.key}
            className="flex flex-1 items-center gap-1"
            title={l.tip}
          >
            <button
              onClick={() => setLevel(l.key)}
              className={`flex-1 rounded-lg border px-1 py-0.5 text-center text-[11px] transition-colors ${
                level === l.key
                  ? l.key === 'high'
                    ? 'border-danger/50 bg-danger/15 text-danger'
                    : l.key === 'low'
                      ? 'border-line-strong bg-surface-strong text-faint'
                      : 'border-accent/50 bg-accent/15 text-accent-strong'
                  : 'border-transparent text-faint/60 hover:bg-surface'
              }`}
            >
              {l.label}·{l.desc}
            </button>
            {i < LEVELS.length - 1 && (
              <span className={`h-px flex-1 ${level === l.key || idx > i ? 'bg-line-strong' : 'bg-line'}`} />
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
