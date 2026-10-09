import { useEffect, useRef, useState } from 'react'
import { ArrowUp, Square, X, Paperclip, FileText } from 'lucide-react'
import { useChatStore } from '../../store/useChatStore'
import { useUIStore } from '../../store/useUIStore'
import { VoiceButton } from './VoiceButton'
import { debugLog } from '../../lib/debug'
import type { Attachment } from '../../lib/types'

export function Composer() {
  const [text, setText] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const taRef = useRef<HTMLTextAreaElement>(null)
  const streaming = useChatStore((s) => s.streaming)
  const send = useChatStore((s) => s.sendMessage)
  const stop = useChatStore((s) => s.stop)
  const pendingComposer = useUIStore((s) => s.pendingComposerText)
  const setPendingComposer = useUIStore((s) => s.setPendingComposer)

  // 外部带入内容（资源管理器右键「问助手」等）→ 预填输入框，用户先看再发
  useEffect(() => {
    if (!pendingComposer) return
    setText(pendingComposer)
    setPendingComposer(null)
    requestAnimationFrame(() => {
      taRef.current?.focus()
      taRef.current?.setSelectionRange(pendingComposer.length, pendingComposer.length)
    })
    debugLog('composer', '外部带入内容已预填', { len: pendingComposer.length })
  }, [pendingComposer, setPendingComposer])

  function autoGrow() {
    const el = taRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 220) + 'px'
  }
  useEffect(autoGrow, [text])

  // 在光标处插入文本（Electron 下接管纯文本粘贴时用）
  function insertAtCursor(t: string) {
    const el = taRef.current
    if (!el) {
      setText((v) => v + t)
      return
    }
    const start = el.selectionStart ?? text.length
    const end = el.selectionEnd ?? text.length
    const next = text.slice(0, start) + t + text.slice(end)
    setText(next)
    requestAnimationFrame(() => {
      const pos = start + t.length
      el.focus()
      el.setSelectionRange(pos, pos)
    })
  }

  // 合并附件，去重（同 path+name+size 视为同一）
  function addAttachments(list: Attachment[]) {
    setAttachments((prev) => {
      const out = [...prev]
      for (const a of list) {
        const dup = out.some(
          (x) => x.name === a.name && x.size === a.size && x.path === a.path,
        )
        if (dup) continue
        // 图片体积保护：超过 15MB 的图片转为仅记录（避免 base64 过大）
        if (a.isImage && a.size > 15 * 1024 * 1024) {
          debugLog('composer', '图片过大，仅提示不内联', { name: a.name, size: a.size }, 'warn')
          out.push({ ...a, isImage: false, dataUrl: undefined })
          continue
        }
        out.push(a)
      }
      return out
    })
  }

  function removeAttachment(idx: number) {
    setAttachments((prev) => prev.filter((_, i) => i !== idx))
  }

  // Ctrl+V 粘贴上传：Electron 用 clipboard:readFiles IPC；浏览器用 clipboardData.files
  async function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const api = (window as any).electronAPI
    if (api?.readClipboardFiles) {
      // Electron：统一接管粘贴，阻止默认后自行决定是插入文件还是文本
      e.preventDefault()
      try {
        const files: Attachment[] = (await api.readClipboardFiles()) || []
        if (files.length) {
          addAttachments(files)
          debugLog('composer', '粘贴文件(附件)', { count: files.length })
          return
        }
      } catch (err) {
        debugLog('composer', 'readClipboardFiles 失败', { err }, 'warn')
      }
      // 无文件：按普通文本粘贴处理
      try {
        const txt: string = (await api.readClipboard()) || ''
        if (txt) insertAtCursor(txt)
      } catch (_) {
        /* 忽略 */
      }
      return
    }
    // 浏览器：处理 clipboardData 中的图片（转 data URL 内联）
    const items = e.clipboardData?.files
    if (items && items.length) {
      let hasImage = false
      for (const f of Array.from(items)) {
        if (f.type.startsWith('image/')) {
          hasImage = true
          const reader = new FileReader()
          reader.onload = () => {
            const dataUrl = reader.result as string
            addAttachments([
              {
                name: f.name || 'image.png',
                ext: (f.name.split('.').pop() || 'png').toLowerCase(),
                isImage: true,
                size: f.size,
                path: '',
                dataUrl,
              },
            ])
          }
          reader.readAsDataURL(f)
        }
      }
      if (hasImage) {
        e.preventDefault()
        debugLog('composer', '浏览器粘贴图片(附件)')
      }
    }
  }

  // 拖拽文件进输入框同样作为附件
  function handleDrop(e: React.DragEvent<HTMLTextAreaElement>) {
    e.preventDefault()
    const files = e.dataTransfer?.files
    if (!files || !files.length) return
    const list: Attachment[] = []
    for (const f of Array.from(files)) {
      const ext = (f.name.split('.').pop() || '').toLowerCase()
      const isImage = f.type.startsWith('image/')
      if (isImage) {
        // 图片：读为 data URL 内联（Electron 下 f.path 一并记录，仅作展示）
        const reader = new FileReader()
        reader.onload = () => {
          addAttachments([
            { name: f.name, ext, isImage: true, size: f.size, path: (f as any).path || '', dataUrl: reader.result as string },
          ])
        }
        reader.readAsDataURL(f)
      } else {
        // 非图片：记录路径（Electron 下 f.path 可用，模型用 read_file 读取）
        list.push({ name: f.name, ext, isImage: false, size: f.size, path: (f as any).path || '' })
      }
    }
    if (list.length) addAttachments(list)
  }

  function submit() {
    const t = text.trim()
    debugLog('composer', 'submit()', { textLen: t.length, attachments: attachments.length, streaming })
    if ((!t && attachments.length === 0) || streaming) return
    const toSend = attachments
    setText('')
    setAttachments([])
    void send(t, toSend.length ? toSend : undefined)
  }

  function onKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      debugLog('composer', '回车发送')
      submit()
    } else if (e.key === 'Escape' && streaming) {
      e.preventDefault()
      debugLog('composer', 'Esc 停止')
      stop()
    }
  }

  const canSend = (text.trim().length > 0 || attachments.length > 0) && !streaming

  return (
    <div className="px-4 pb-4 pt-2 sm:px-6">
      <div className="glass-strong mx-auto flex max-w-3xl flex-col gap-2 rounded-xl3 p-2">
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 px-1 pt-1">
            {attachments.map((a, i) => (
              <div
                key={`${a.name}-${a.size}-${i}`}
                className="group relative flex items-center gap-2 rounded-lg border border-white/10 bg-white/5 py-1 pl-2 pr-7"
              >
                {a.isImage && a.dataUrl ? (
                  <img
                    src={a.dataUrl}
                    alt={a.name}
                    className="h-9 w-9 rounded object-cover"
                  />
                ) : a.isImage ? (
                  <Paperclip size={16} className="shrink-0 text-faint" />
                ) : (
                  <FileText size={16} className="shrink-0 text-faint" />
                )}
                <span className="max-w-[140px] truncate text-xs text-ink/90">{a.name}</span>
                <button
                  onClick={() => removeAttachment(i)}
                  title="移除附件"
                  className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-0.5 text-faint hover:bg-white/10 hover:text-ink"
                >
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            onPaste={handlePaste}
            onDrop={handleDrop}
            rows={1}
            placeholder="给 MY_AGENT 发消息…（Enter 发送，Shift+Enter 换行，Esc 停止，Ctrl+V 粘贴图片/文件）"
            className="max-h-[220px] flex-1 resize-none bg-transparent px-3 py-2 text-[14.5px] leading-relaxed text-ink outline-none placeholder:text-faint"
          />
          <VoiceButton />
          {streaming ? (
            <button
              onClick={() => {
                debugLog('composer', '点击停止按钮')
                stop()
              }}
              title="停止 (Esc)"
              className="btn-danger mb-0.5 h-9 w-9 rounded-xl p-0"
            >
              <Square size={15} className="fill-current" />
            </button>
          ) : (
            <button
              onClick={() => {
                debugLog('composer', '点击发送按钮', { canSend, textLen: text.trim().length })
                submit()
              }}
              disabled={!canSend}
              title="发送 (Enter)"
              className="btn-accent mb-0.5 h-9 w-9 rounded-xl p-0 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <ArrowUp size={17} />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
