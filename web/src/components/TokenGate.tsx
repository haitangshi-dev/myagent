import { useState } from 'react'
import { getToken, setToken, clearToken } from '../lib/auth'

/**
 * 远程/手机客户端首次打开时，若 localStorage 没有访问令牌，弹出输入层。
 * 输入正确令牌后即存盘，后续所有请求自动带 Authorization 头。
 *
 * 桌面端 Electron（localhost / 127.0.0.1 / [::1]）自动放行，无需输入令牌。
 */

/** 判断当前是否为本地桌面客户端。
 *  PC-only：包装后的 Electron 应用用 navigator.userAgent 含 'Electron' 可靠识别，
 *  不依赖 hostname 猜测；本地浏览器经 127.0.0.1 / localhost / [::1] / file:// 打开也放行。 */
function isLocalDesktop(): boolean {
  const h = window.location.hostname
  const ua = typeof navigator !== 'undefined' ? navigator.userAgent || '' : ''
  return (
    ua.includes('Electron') ||
    h === '' ||
    h === 'localhost' ||
    h === '127.0.0.1' ||
    h === '[::1]'
  )
}

export function TokenGate({ children }: { children: React.ReactNode }) {
  const [token, setLocal] = useState<string>(getToken() ?? '')
  // 本地桌面端直接放行（后端 auth_middleware 同样豁免这些来源）
  const [unlocked, setUnlocked] = useState<boolean>(isLocalDesktop() || !!getToken())
  const [err, setErr] = useState<string>('')

  function submit() {
    const t = token.trim()
    if (!t) {
      setErr('请输入访问令牌')
      return
    }
    setToken(t)
    setErr('')
    setUnlocked(true)
  }

  if (unlocked) return <>{children}</>

  return (
    <div className="flex h-full w-full items-center justify-center bg-bg text-ink p-4">
      <div className="w-full max-w-sm rounded-xl border border-white/10 bg-panel/80 p-6 shadow-2xl">
        <h1 className="text-lg font-semibold text-accent-strong">MY_AGENT 远程访问</h1>
        <p className="mt-2 text-sm text-ink/70">
          此客户端通过公网连接你的电脑后端。请输入电脑端设置的访问令牌（Access Token）以继续。
        </p>
        <input
          type="password"
          autoFocus
          value={token}
          onChange={(e) => setLocal(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
          placeholder="访问令牌"
          className="mt-4 w-full rounded-lg border border-white/10 bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
        />
        {err && <p className="mt-2 text-xs text-red-400">{err}</p>}
        <button
          onClick={submit}
          className="mt-4 w-full rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white hover:opacity-90"
        >
          连接
        </button>
        {getToken() && (
          <button
            onClick={() => {
              clearToken()
              setLocal('')
              setUnlocked(false)
            }}
            className="mt-2 w-full text-xs text-ink/50 hover:text-ink"
          >
            清除已保存令牌
          </button>
        )}
      </div>
    </div>
  )
}
