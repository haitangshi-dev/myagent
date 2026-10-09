import { useState } from 'react'
import { motion } from 'framer-motion'
import {
  CheckCircle2,
  ChevronDown,
  Loader2,
  Sparkles,
  Terminal,
  XCircle,
} from 'lucide-react'
import type { ToolCall } from '../../lib/types'
import { clamp } from '../../lib/format'

export function ToolCard({ tool }: { tool: ToolCall }) {
  const [open, setOpen] = useState(false)
  // 后端 build_tool_preview 可能返回 None → SSE 透传为 null；此处必须兜底，
  // 否则 tool.preview.length 抛 TypeError 会整页白屏（推理时触发工具调用即炸）。
  const preview = tool.preview ?? ''
  const resultPreview = tool.resultPreview ?? ''
  const command = tool.command ?? ''
  const running = tool.status === 'running'
  const collapsed = !open && preview.length > 90
  const previewText = collapsed ? clamp(preview, 140) : preview
  const isSkill = tool.kind === 'skill'
  // running 阶段优先折叠展示「模型输入命令」；partial 时下一道 pre 还没定型
  const showCommand = command.length > 0

  const statusColor = running
    ? 'text-accent'
    : tool.status === 'success'
      ? 'text-accent-strong'
      : 'text-danger'

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      className={`rounded-xl border bg-surface/60 ${
        isSkill ? 'tool-skill' : 'border-line'
      }`}
    >
      <div className="flex items-center gap-2.5 px-3 py-2">
        <span className={isSkill ? 'text-skill' : statusColor}>
          {running ? (
            <Loader2 size={15} className="animate-spin" />
          ) : tool.status === 'success' ? (
            <CheckCircle2 size={15} />
          ) : (
            <XCircle size={15} />
          )}
        </span>
        <span
          className={`flex h-6 w-6 items-center justify-center rounded-md ${
            isSkill ? 'bg-skill text-skill' : 'bg-white/5 text-faint'
          }`}
        >
          {isSkill ? <Sparkles size={13} /> : <Terminal size={13} />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-medium text-ink">
              {tool.label || tool.name}
            </span>
            {isSkill && (
              <span className="badge-skill">
                <Sparkles size={10} /> 技能
              </span>
            )}
          </div>
        </div>
        {(preview || showCommand) && (
          <button
            onClick={() => setOpen((v) => !v)}
            className="flex items-center gap-0.5 rounded-md px-1.5 py-0.5 text-[11px] text-faint transition-colors hover:text-dim"
          >
            {open ? '收起' : '详情'}
            <ChevronDown
              size={13}
              className={`transition-transform duration-200 ${open ? 'rotate-180' : ''}`}
            />
          </button>
        )}
      </div>

      {/* ★ running 阶段即时展示「模型输入的命令」——无需等 skill 返回 */}
      {showCommand && (
        <div className="border-t border-line/70 px-3 py-1.5">
          <div className="mb-0.5 flex items-center gap-1.5 text-[10.5px] uppercase tracking-wide text-faint">
            <Terminal size={11} />
            模型输入
            {running && (
              <span className="inline-flex items-center gap-1 text-accent">
                <Loader2 size={10} className="animate-spin" /> 执行中
              </span>
            )}
          </div>
          <pre className="overflow-auto whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-dim">
            {command}
          </pre>
        </div>
      )}

      {preview && (
        <div
          className={`overflow-hidden border-t border-line/70 transition-all duration-300 ease-expo ${
            open || !collapsed ? 'max-h-80' : 'max-h-[3.4rem]'
          }`}
        >
          <pre className="overflow-auto whitespace-pre-wrap break-words px-3 py-2 font-mono text-[12px] leading-relaxed text-dim">
            {previewText}
            {collapsed && !open && (
              <span className="text-faint"> …</span>
            )}
          </pre>
        </div>
      )}

      {resultPreview && tool.status !== 'running' && (
        <div className="border-t border-line/70 px-3 py-2">
          <div className="mb-1 text-[11px] uppercase tracking-wide text-faint">
            {tool.status === 'success' ? '结果' : '错误'}
          </div>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-dim">
            {clamp(resultPreview, 400)}
          </pre>
        </div>
      )}
    </motion.div>
  )
}
