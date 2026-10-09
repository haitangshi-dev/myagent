// 访问令牌：手机/远程客户端首次打开时输入一次，存 localStorage，之后所有 /api/* 请求自动带。
// 后端 server/api.py 的 auth_middleware 校验 Bearer token；未带或错误返回 401。

const KEY = 'myagent_token'

export function getToken(): string | null {
  try {
    return localStorage.getItem(KEY)
  } catch {
    return null
  }
}

export function setToken(t: string): void {
  try {
    localStorage.setItem(KEY, t.trim())
  } catch {
    /* ignore */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* ignore */
  }
}

/** 拦截 window.fetch：给同源 /api/* 请求自动附加 Authorization: Bearer <token>。 */
export function installAuthInterceptor(): void {
  const w = window as unknown as { __authInstalled?: boolean }
  if (w.__authInstalled) return
  w.__authInstalled = true

  const orig = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const token = getToken()
    if (token) {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.href
            : (input as Request).url
      // 只给同源 /api 请求加头，避免把令牌泄露给外域
      if (typeof url === 'string' && url.startsWith('/api')) {
        const headers = new Headers(init?.headers)
        if (!headers.has('Authorization')) {
          headers.set('Authorization', `Bearer ${token}`)
        }
        init = { ...init, headers }
      }
    }
    return orig(input, init)
  }
}
