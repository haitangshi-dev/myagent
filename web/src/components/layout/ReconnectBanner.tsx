import { AnimatePresence, motion } from 'framer-motion'
import { WifiOff } from 'lucide-react'
import { useUIStore } from '../../store/useUIStore'

// 非阻塞的断线重连横幅：后端掉线时显示在顶部，不影响输入与浏览。
// 由 useChatStore 在检测到网络断连时置 reconnecting=true，
// 并轮询 health 恢复后自动清除（见 useChatStore.sendMessage 的 catch 分支）。
export function ReconnectBanner() {
  const reconnecting = useUIStore((s) => s.reconnecting)
  return (
    <AnimatePresence>
      {reconnecting && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
          className="shrink-0 overflow-hidden border-b border-warning/30 bg-warning/10"
        >
          <div className="flex items-center justify-center gap-2 px-4 py-1.5 text-[12px] text-warning">
            <WifiOff size={13} />
            后端连接已断开，正在自动重连…（请勿关闭窗口）
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
