import { motion } from 'framer-motion'
import { Sparkles, FileSearch, TerminalSquare, ShieldCheck } from 'lucide-react'

const SUGGESTIONS: { icon: typeof Sparkles; label: string; prompt: string }[] = [
  {
    icon: FileSearch,
    label: '解析与总结',
    prompt: '帮我总结下面这段代码的职责与潜在问题：',
  },
  {
    icon: TerminalSquare,
    label: '写脚本',
    prompt: '写一个 Python 脚本：递归扫描目录，按扩展名统计文件大小。',
  },
  {
    icon: ShieldCheck,
    label: '触发审批',
    prompt: '删除 /tmp 下的所有 .tmp 临时文件（需要我确认）。',
  },
  {
    icon: Sparkles,
    label: '概念讲解',
    prompt: '用通俗语言解释 OAuth 2.0 的授权码流程。',
  },
]

export function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col items-center justify-center px-6 text-center">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
      >
        <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl border border-line bg-accent/10">
          <Sparkles size={24} className="text-accent-strong" />
        </div>
        <h1 className="text-balance text-2xl font-semibold tracking-tight text-ink">
          与 MY_AGENT 对话
        </h1>
        <p className="mx-auto mt-2 max-w-md text-balance text-sm leading-relaxed text-dim">
          一个自带工具链的桌面 Agent。思考链、工具调用与审批闸门会实时呈现——
          原始参数永不进入前端。
        </p>
      </motion.div>

      <div className="mt-8 grid w-full grid-cols-1 gap-2.5 sm:grid-cols-2">
        {SUGGESTIONS.map((s, i) => {
          const Icon = s.icon
          return (
            <motion.button
              key={s.label}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5, delay: 0.1 + i * 0.06, ease: [0.16, 1, 0.3, 1] }}
              onClick={() => onPick(s.prompt)}
              className="glass group flex items-start gap-3 rounded-xl2 p-3.5 text-left transition-colors duration-200 ease-expo hover:bg-surface-strong"
            >
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent/10 text-accent-strong">
                <Icon size={16} />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-ink">{s.label}</span>
                <span className="mt-0.5 block truncate text-xs text-faint">
                  {s.prompt}
                </span>
              </span>
            </motion.button>
          )
        })}
      </div>
    </div>
  )
}
