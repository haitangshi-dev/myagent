import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Settings, Volume2, Mic, X, RotateCcw } from 'lucide-react'
import {
  useVoiceSettings,
  CLOUD_VOICES,
  type TtsMode,
  type SttMode,
  type VoiceDevice,
} from '../../store/useVoiceSettings'
import { useUIStore } from '../../store/useUIStore'

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <div className="mb-1.5 flex items-baseline justify-between">
        <span className="text-[13px] font-medium text-ink">{label}</span>
        {hint && <span className="text-[11px] text-faint">{hint}</span>}
      </div>
      {children}
    </div>
  )
}

function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
}) {
  return (
    <div className="flex gap-1 rounded-xl border border-line bg-bg/60 p-1">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={`flex-1 rounded-lg px-2 py-1.5 text-[12px] transition-colors ${
            value === o.value
              ? 'bg-accent/20 text-accent-strong'
              : 'text-dim hover:bg-white/5'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

function Slider({
  value,
  min,
  max,
  step,
  onChange,
  fmt,
}: {
  value: number
  min: number
  max: number
  step: number
  onChange: (v: number) => void
  fmt: (v: number) => string
}) {
  return (
    <div className="flex items-center gap-3">
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="flex-1 accent-[var(--accent)]"
      />
      <span className="w-12 shrink-0 text-right font-mono text-[12px] text-dim">{fmt(value)}</span>
    </div>
  )
}

export function VoiceSettingsModal() {
  const open = useUIStore((s) => s.settingsOpen)
  const setOpen = useUIStore((s) => s.setSettingsOpen)
  const s = useVoiceSettings()
  const [localVoices, setLocalVoices] = useState<SpeechSynthesisVoice[]>([])

  useEffect(() => {
    if (!open) return
    const load = () => setLocalVoices(window.speechSynthesis.getVoices())
    load()
    window.speechSynthesis.onvoiceschanged = load
    return () => {
      window.speechSynthesis.onvoiceschanged = null
    }
  }, [open])

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
            className="glass-strong mt-[10vh] max-h-[80vh] w-full max-w-lg overflow-y-auto rounded-xl3"
          >
            <div className="sticky top-0 flex items-center gap-2 border-b border-line bg-raise/80 px-4 py-3 backdrop-blur">
              <Settings size={16} className="text-accent-strong" />
              <span className="flex-1 text-[14px] font-semibold text-ink">语音设置</span>
              <button
                onClick={() => s.reset()}
                title="恢复默认"
                className="btn-ghost flex h-8 w-8 items-center justify-center rounded-lg p-0"
              >
                <RotateCcw size={15} />
              </button>
              <button
                onClick={() => setOpen(false)}
                className="btn-ghost flex h-8 w-8 items-center justify-center rounded-lg p-0"
              >
                <X size={16} />
              </button>
            </div>

            <div className="p-4">
              {/* ===== 语音输出 ===== */}
              <div className="mb-2 flex items-center gap-2 text-[12px] font-semibold uppercase tracking-wide text-faint">
                <Volume2 size={13} /> 语音输出（朗读）
              </div>

              <Row label="输出引擎" hint="auto=Edge TTS 优先，失败回退本地">
                <Segmented<TtsMode>
                  value={s.ttsMode}
                  onChange={(v) => s.set({ ttsMode: v })}
                  options={[
                    { value: 'auto', label: '自动' },
                    { value: 'cloud', label: '仅 Edge TTS' },
                    { value: 'local', label: '仅本地' },
                  ]}
                />
              </Row>

              <Row label="神经语音音色（Edge TTS / 微软在线）">
                <select
                  value={s.cloudVoice}
                  onChange={(e) => s.set({ cloudVoice: e.target.value })}
                  className="w-full rounded-lg border border-line bg-bg/60 px-3 py-2 text-[13px] text-ink outline-none focus:border-accent"
                >
                  {CLOUD_VOICES.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.label}
                    </option>
                  ))}
                </select>
              </Row>

              <Row label="本地音色" hint="本地无此音色时自动选最佳女声">
                <select
                  value={s.localVoice ?? ''}
                  onChange={(e) => s.set({ localVoice: e.target.value || null })}
                  className="w-full rounded-lg border border-line bg-bg/60 px-3 py-2 text-[13px] text-ink outline-none focus:border-accent"
                >
                  <option value="">自动（最佳中文女声）</option>
                  {localVoices
                    .filter((v) => v.lang.toLowerCase().startsWith('zh') || v.lang.toLowerCase().includes('cn'))
                    .map((v) => (
                      <option key={v.voiceURI} value={v.name}>
                        {v.name}
                      </option>
                    ))}
                </select>
                {localVoices.length === 0 && (
                  <div className="mt-1 text-[11px] text-faint">（浏览器尚未枚举到本地语音，稍候或首次朗读后可见）</div>
                )}
              </Row>

              <Row label="语速" hint="1.0 = 正常">
                <Slider value={s.rate} min={0.5} max={3} step={0.05} onChange={(v) => s.set({ rate: v })} fmt={(v) => `${v.toFixed(2)}x`} />
              </Row>

              <Row label="音调" hint="1.0 = 正常">
                <Slider value={s.pitch} min={0.5} max={2} step={0.05} onChange={(v) => s.set({ pitch: v })} fmt={(v) => `${v.toFixed(2)}x`} />
              </Row>

              <Row label="音量">
                <Slider value={s.volume} min={0} max={1} step={0.05} onChange={(v) => s.set({ volume: v })} fmt={(v) => `${Math.round(v * 100)}%`} />
              </Row>

              <Row label="输出设备" hint="应用内切换，不改系统默认">
                <Segmented<VoiceDevice>
                  value={s.device}
                  onChange={(v) => s.set({ device: v })}
                  options={[
                    { value: 'ag', label: 'AG Audio 优先' },
                    { value: 'system', label: '系统默认' },
                  ]}
                />
              </Row>

              {/* ===== 语音输入 ===== */}
              <div className="mb-2 mt-6 flex items-center gap-2 border-t border-line pt-4 text-[12px] font-semibold uppercase tracking-wide text-faint">
                <Mic size={13} /> 语音输入（你说的话）
              </div>

              <Row label="识别引擎" hint="sherpa=本地离线，不上云">
                <Segmented<SttMode>
                  value={s.sttMode}
                  onChange={(v) => s.set({ sttMode: v })}
                  options={[
                    { value: 'auto', label: '自动' },
                    { value: 'sherpa', label: '仅本地离线' },
                    { value: 'webspeech', label: '仅浏览器' },
                  ]}
                />
              </Row>

              <div className="mt-4 rounded-lg border border-line/60 bg-bg/40 px-3 py-2 text-[11.5px] leading-relaxed text-faint">
                默认读取使用 <b>Edge TTS</b>（微软在线神经语音，<b>免费免密钥</b>，最佳中文女声
                <code> XiaoxiaoNeural</code>）。需后端已 <code>pip install edge-tts</code> 且能联网。
                若已配置 Azure 密钥（<code>config/settings.yaml</code> 的 <code>azure_speech</code>），Edge 失败时会回退 Azure；
                两者都不可用则回退本地语音。
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
