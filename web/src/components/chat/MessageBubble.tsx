import { memo } from 'react'
import { motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertTriangle, FileText } from 'lucide-react'
import type { ChatMessage } from '../../lib/types'
import { formatTokens } from '../../lib/format'
import { ToolCard } from './ToolCard'
import { ReasoningBlock } from './ReasoningBlock'
import { ApprovalCard } from './ApprovalCard'
import { useChatStore } from '../../store/useChatStore'

function Footer({ m }: { m: ChatMessage }) {
  const modelRetrying = useChatStore((s) => s.modelRetrying)
  return (
    <div className="mt-2 flex items-center gap-3 text-[11px] text-faint">
      {m.status === 'streaming' &&
        (modelRetrying ? (
          <span className="flex items-center gap-1 text-accent">
            <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
            {modelRetrying.isRateLimit ? '模型限流，连接中…' : '连接模型中…'}
          </span>
        ) : (
          <span className="flex items-center gap-1 text-accent">
            <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
            生成中…
          </span>
        ))}
      {m.status === 'truncated' && (
        <span className="flex items-center gap-1 text-warning">
          <AlertTriangle size={11} />
          {m.finishReason === 'length'
            ? '输出已达上限（截断）'
            : m.finishReason === 'content_filter'
              ? '内容被安全过滤（截断）'
              : '输出异常终止'}
        </span>
      )}
      {m.status === 'stopped' && <span>已停止</span>}
      {m.usage && (
        <span className="font-mono">
          {formatTokens(m.usage.prompt_tokens)}↑ · {formatTokens(m.usage.completion_tokens)}↓
          tok
        </span>
      )}
    </div>
  )
}

export const MessageBubble = memo(function MessageBubble({
  message,
  sessionId,
}: {
  message: ChatMessage
  sessionId: string
}) {
  if (message.role === 'user') {
    return (
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="flex justify-end"
      >
        <div className="max-w-[80%] rounded-xl2 rounded-br-md border border-line-strong bg-surface-strong px-4 py-2.5">
          {message.images && message.images.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {message.images.map((a, i) =>
                a.isImage && a.dataUrl ? (
                  <img
                    key={i}
                    src={a.dataUrl}
                    alt={a.name}
                    className="max-h-40 max-w-[200px] rounded-lg border border-line object-cover"
                  />
                ) : (
                  <div
                    key={i}
                    className="flex items-center gap-1.5 rounded-lg border border-line bg-white/5 px-2 py-1 text-[12px] text-ink/90"
                    title={a.path || a.name}
                  >
                    <FileText size={14} className="shrink-0 text-faint" />
                    <span className="max-w-[160px] truncate">{a.name}</span>
                  </div>
                ),
              )}
            </div>
          )}
          <p className="whitespace-pre-wrap break-words text-[14.5px] leading-relaxed text-ink">
            {message.content}
          </p>
        </div>
      </motion.div>
    )
  }

  const streaming = message.status === 'streaming'

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
      className="flex justify-start"
    >
      <div className="w-full max-w-[88%]">
        {message.reasoning && (
          <ReasoningBlock text={message.reasoning} streaming={streaming} />
        )}

        {message.content && (
          // ★ 性能：流式期间（status==='streaming'）用纯文本渲染，避免 react-markdown
          // 每帧重解析整段累计文本（长文本越写越卡的核心瓶颈）。流式结束（done/truncated/
          // stopped/error）后 message 对象变化，本组件重渲染一次切回 Markdown 格式化。
          streaming ? (
            <div className="whitespace-pre-wrap break-words text-[14.5px] leading-relaxed text-ink">
              {message.content}
            </div>
          ) : (
            <div className="md">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {message.content}
              </ReactMarkdown>
            </div>
          )
        )}

        {message.notices && message.notices.length > 0 && (
          <div className="mt-2 flex flex-col gap-1">
            {message.notices.map((n, i) => (
              <div
                key={i}
                className="flex items-center gap-1.5 rounded-lg border border-line bg-surface/60 px-2.5 py-1 text-[11.5px] text-faint"
              >
                <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-skill" />
                <span>{n.message}</span>
              </div>
            ))}
          </div>
        )}

        {message.toolCalls.length > 0 && (
          <div className="mt-2 space-y-2">
            {message.toolCalls.map((t) => (
              <ToolCard key={t.id} tool={t} />
            ))}
          </div>
        )}

        {message.approvals.length > 0 && (
          <div className="mt-2 space-y-2">
            {message.approvals.map((a) => (
              <ApprovalCard
                key={a.approval_id}
                approval={a}
                sessionId={sessionId}
                messageId={message.id}
              />
            ))}
          </div>
        )}

        {message.status === 'error' && (
          <div className={`mt-2 flex items-center gap-2 rounded-lg border px-3 py-2 text-[13px] ${
            message.content
              ? 'border-warning/30 bg-warning/10 text-warning'
              : 'border-danger/30 bg-danger/10 text-danger'
          }`}>
            <AlertTriangle size={14} />
            {message.error || '出错了'}
          </div>
        )}

        <Footer m={message} />
      </div>
    </motion.div>
  )
})
