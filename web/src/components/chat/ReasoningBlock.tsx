import { useState, useEffect } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronRight, Lightbulb } from 'lucide-react'

export function ReasoningBlock({
  text,
  streaming,
}: {
  text: string
  streaming: boolean
}) {
  // ★ 流式期间自动展开，实时呈现思考过程。glm-4.7-flash 等深度思考模型会输出
  //   很长推理链；若默认折叠，用户会长时间只看到「推理中…」而无任何内容反馈。
  //   流式结束保持展开（完整推理可读），用户可手动折叠；流式期间用户手动折叠也生效。
  const [open, setOpen] = useState(streaming)
  useEffect(() => {
    if (streaming) setOpen(true)
  }, [streaming])
  if (!text) return null
  const active = streaming && !open

  return (
    <div className="mb-2 rounded-xl border border-line bg-surface/40">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <Lightbulb size={14} className={active ? 'text-accent animate-pulse-soft' : 'text-accent-strong'} />
        <span className="text-[13px] font-medium text-dim">
          {active ? '推理中…' : '推理过程'}
        </span>
        {active && (
          <span className="ml-1 h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
        )}
        <ChevronRight
          size={15}
          className={`ml-auto text-faint transition-transform duration-200 ${open ? 'rotate-90' : ''}`}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            // ★ 性能：流式期间（streaming=true）禁用 height 动画——每帧文本增长时
            //   height:'auto' 动画会持续触发布局计算，长推理链下是卡顿源之一；
            //   流式结束恢复动画让折叠/展开顺滑。
            initial={streaming ? false : { height: 0, opacity: 0 }}
            animate={streaming ? { opacity: 1 } : { height: 'auto', opacity: 1 }}
            exit={streaming ? undefined : { height: 0, opacity: 0 }}
            transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <pre className="whitespace-pre-wrap break-words border-t border-line/70 px-3 py-2.5 font-mono text-[12.5px] leading-relaxed text-dim">
              {text}
              {streaming && <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse-soft bg-accent align-middle" />}
            </pre>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
