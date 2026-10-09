import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { KeyRound, X, Save } from 'lucide-react'
import { useUIStore } from '../../store/useUIStore'
import { fetchSettings, saveSettingsOverride, type SettingsResponse, type ProviderSetting } from '../../lib/api'

interface ProviderForm {
  api_key: string
  base_url: string
  default_model: string
}

type FormMap = Record<string, ProviderForm>

export function ProviderSettingsModal() {
  const open = useUIStore((s) => s.providerSettingsOpen)
  const setOpen = useUIStore((s) => s.setProviderSettingsOpen)

  const [data, setData] = useState<SettingsResponse | null>(null)
  const [form, setForm] = useState<FormMap>({})
  const [defaultProvider, setDefaultProvider] = useState<string>('')
  const [defaultModel, setDefaultModel] = useState<string>('')
  const [status, setStatus] = useState<'' | 'saving' | 'saved' | 'error'>('')
  const [errMsg, setErrMsg] = useState('')
  const initial = useRef<SettingsResponse | null>(null)

  useEffect(() => {
    if (!open) return
    setStatus('')
    setErrMsg('')
    fetchSettings()
      .then((d) => {
        initial.current = d
        setData(d)
        const f: FormMap = {}
        for (const [name, p] of Object.entries(d.providers)) {
          f[name] = {
            api_key: '', // 留空 = 不改动；输入新值 = 覆盖
            base_url: p.base_url ?? '',
            default_model: p.default_model ?? '',
          }
        }
        setForm(f)
        setDefaultProvider(d.default_provider ?? '')
        setDefaultModel(d.default_model ?? '')
      })
      .catch((e) => {
        setErrMsg('加载设置失败: ' + (e?.message ?? ''))
      })
  }, [open])

  function patchProvider(name: string, patch: Partial<ProviderForm>) {
    setForm((prev) => ({ ...prev, [name]: { ...prev[name], ...patch } }))
  }

  async function onSave() {
    if (!initial.current) return
    setStatus('saving')
    setErrMsg('')
    const override: Record<string, unknown> = {}
    const provOverrides: Record<string, Record<string, string>> = {}
    for (const [name, f] of Object.entries(form)) {
      const orig: ProviderSetting | undefined = initial.current.providers[name]
      const patch: Record<string, string> = {}
      if (f.api_key.trim() !== '') patch.api_key = f.api_key.trim()
      if (orig && f.base_url !== (orig.base_url ?? '')) patch.base_url = f.base_url
      if (orig && f.default_model !== (orig.default_model ?? '')) patch.default_model = f.default_model
      if (Object.keys(patch).length) provOverrides[name] = patch
    }
    if (Object.keys(provOverrides).length) override.providers = provOverrides
    if (defaultProvider && defaultProvider !== (initial.current.default_provider ?? ''))
      override.default_provider = defaultProvider
    if (defaultModel && defaultModel !== (initial.current.default_model ?? ''))
      override.default_model = defaultModel

    if (Object.keys(override).length === 0) {
      setStatus('saved')
      setTimeout(() => setOpen(false), 600)
      return
    }
    try {
      const res = await saveSettingsOverride(override)
      setData((prev) => (prev ? { ...prev, providers: res.settings } : prev))
      // 重写后清空已保存的 key 输入，避免明文残留
      setForm((prev) => {
        const f: FormMap = {}
        for (const [name, p] of Object.entries(res.settings)) {
          f[name] = {
            api_key: '',
            base_url: p.base_url ?? '',
            default_model: p.default_model ?? '',
          }
        }
        return f
      })
      setStatus('saved')
      setTimeout(() => setOpen(false), 700)
    } catch (e: any) {
      setStatus('error')
      setErrMsg('保存失败: ' + (e?.message ?? ''))
    }
  }

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="fixed inset-0 z-[70] flex items-start justify-center bg-black/50 backdrop-blur-sm"
          onClick={() => setOpen(false)}
        >
          <motion.div
            initial={{ opacity: 0, y: -14, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -14, scale: 0.98 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            onClick={(e) => e.stopPropagation()}
            className="glass-strong mt-[8vh] max-h-[84vh] w-full max-w-xl overflow-y-auto rounded-xl3"
          >
            <div className="sticky top-0 flex items-center gap-2 border-b border-line bg-raise/80 px-4 py-3 backdrop-blur">
              <KeyRound size={16} className="text-accent-strong" />
              <span className="flex-1 text-[14px] font-semibold text-ink">模型密钥 / Provider 设置</span>
              <button
                onClick={() => setOpen(false)}
                className="btn-ghost flex h-8 w-8 items-center justify-center rounded-lg p-0"
              >
                <X size={16} />
              </button>
            </div>

            <div className="p-4">
              {errMsg && !status.startsWith('saving') && (
                <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-300">
                  {errMsg}
                </div>
              )}

              <div className="mb-4 rounded-lg border border-line/60 bg-bg/40 px-3 py-2 text-[11.5px] leading-relaxed text-faint">
                密钥保存在<b>用户数据目录</b>（<code>%APPDATA%/MY_AGENT/settings.override.yaml</code>），
                <b>重装 / 更新 exe 不丢失</b>。仅填写需要修改的字段，留空表示沿用现有值。
              </div>

              {!data ? (
                <div className="py-8 text-center text-[13px] text-faint">加载中…</div>
              ) : (
                <>
                  <div className="mb-4 grid grid-cols-2 gap-3">
                    <div>
                      <div className="mb-1 text-[12px] font-medium text-ink">默认 Provider</div>
                      <select
                        value={defaultProvider}
                        onChange={(e) => setDefaultProvider(e.target.value)}
                        className="w-full rounded-lg border border-line bg-bg/60 px-3 py-2 text-[13px] text-ink outline-none focus:border-accent"
                      >
                        {(data?.providers ? Object.keys(data.providers) : []).map((n) => (
                          <option key={n} value={n}>
                            {data.providers[n].display_name ?? n}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <div className="mb-1 text-[12px] font-medium text-ink">默认模型</div>
                      <input
                        value={defaultModel}
                        onChange={(e) => setDefaultModel(e.target.value)}
                        placeholder="默认模型名"
                        className="w-full rounded-lg border border-line bg-bg/60 px-3 py-2 text-[13px] text-ink outline-none focus:border-accent"
                      />
                    </div>
                  </div>

                  <div className="space-y-4">
                    {Object.entries(data.providers).map(([name, p]) => (
                      <div key={name} className="rounded-xl border border-line bg-bg/30 p-3">
                        <div className="mb-2 flex items-center justify-between">
                          <span className="text-[13px] font-semibold text-ink">
                            {p.display_name ?? name}
                          </span>
                          <span className="text-[11px] text-faint">{name}</span>
                        </div>

                        <div className="mb-2">
                          <div className="mb-1 text-[11px] text-dim">API Key</div>
                          <input
                            type="password"
                            value={form[name]?.api_key ?? ''}
                            onChange={(e) => patchProvider(name, { api_key: e.target.value })}
                            placeholder={
                              p.has_api_key ? `已设置（${p.api_key ?? '****'}），留空不变` : '未设置，输入新密钥'
                            }
                            className="w-full rounded-lg border border-line bg-bg/60 px-3 py-2 font-mono text-[12px] text-ink outline-none focus:border-accent"
                          />
                        </div>

                        <div className="mb-2">
                          <div className="mb-1 text-[11px] text-dim">Base URL</div>
                          <input
                            value={form[name]?.base_url ?? ''}
                            onChange={(e) => patchProvider(name, { base_url: e.target.value })}
                            className="w-full rounded-lg border border-line bg-bg/60 px-3 py-2 font-mono text-[12px] text-ink outline-none focus:border-accent"
                          />
                        </div>

                        <div>
                          <div className="mb-1 text-[11px] text-dim">默认模型</div>
                          {p.models && p.models.length > 0 ? (
                            <select
                              value={form[name]?.default_model ?? ''}
                              onChange={(e) => patchProvider(name, { default_model: e.target.value })}
                              className="w-full rounded-lg border border-line bg-bg/60 px-3 py-2 text-[12px] text-ink outline-none focus:border-accent"
                            >
                              {p.models.map((m) => (
                                <option key={m} value={m}>
                                  {m}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <input
                              value={form[name]?.default_model ?? ''}
                              onChange={(e) => patchProvider(name, { default_model: e.target.value })}
                              className="w-full rounded-lg border border-line bg-bg/60 px-3 py-2 text-[12px] text-ink outline-none focus:border-accent"
                            />
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}

              <div className="mt-4 flex items-center justify-end gap-2">
                {status === 'saved' && <span className="text-[12px] text-green-400">已保存</span>}
                <button
                  onClick={onSave}
                  disabled={status === 'saving'}
                  className="btn-primary flex items-center gap-1.5 rounded-lg px-4 py-2 text-[13px] font-medium"
                >
                  <Save size={14} />
                  {status === 'saving' ? '保存中…' : '保存'}
                </button>
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
