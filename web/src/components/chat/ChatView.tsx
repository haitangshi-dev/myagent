import { useLayoutEffect, useRef } from 'react'
import { useChatStore } from '../../store/useChatStore'
import { MessageBubble } from './MessageBubble'
import { EmptyState } from './EmptyState'
import { Composer } from './Composer'
import { TaskList } from './TaskList'
import { PermissionSlider } from './PermissionSlider'

export function ChatView() {
  const activeId = useChatStore((s) => s.activeId)
  const sessions = useChatStore((s) => s.sessions)
  const send = useChatStore((s) => s.sendMessage)
  const session = activeId ? sessions[activeId] : null

  const scrollRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

  // 滚动签名 O(1)：只取尾消息的轻量字段 + 总条数，不遍历全部消息拼接大字符串
  // （旧实现每帧 O(N) 遍历所有消息，长对话流式期间每帧重建 sig 是卡顿源之一）
  const msgs = session?.messages
  const last = msgs && msgs.length > 0 ? msgs[msgs.length - 1] : null
  const sig = `${activeId}|${msgs?.length ?? 0}|${last?.content.length ?? 0}|${last?.reasoning.length ?? 0}|${last?.status ?? ''}|${last?.toolCalls.length ?? 0}|${last?.approvals.length ?? 0}`

  useLayoutEffect(() => {
    if (stick.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [sig])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }

  const empty = !session || session.messages.length === 0

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto"
      >
        {empty ? (
          <EmptyState onPick={(t) => void send(t)} />
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 sm:px-6">
            {session!.messages.map((m) => (
              <MessageBubble key={m.id} message={m} sessionId={session!.id} />
            ))}
          </div>
        )}
      </div>
      <TaskList />
      <PermissionSlider />
      <Composer />
    </div>
  )
}
