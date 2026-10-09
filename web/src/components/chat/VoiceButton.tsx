/**
 * VoiceButton — 语音助手按钮（Composer 内嵌）。
 *
 * 语音【输入】（你说→文字）：
 *   优先使用 sherpa-ncnn wasm 本地离线识别（int8 模型，纯前端、不上云）；
 *   若 wasm 未就位或加载失败，自动回退到浏览器 Web Speech API（SpeechRecognition）。
 *
 * 语音【输出】（回复→朗读）：优先 Azure 云端神经语音（/api/tts，最佳中文女声 XiaoxiaoNeural），失败回退本地 SpeechSynthesis。
 *
 * 设备策略：
 *   - 麦克风强制使用 ERAZER EP03 Hands-Free AG Audio（不改系统默认），找不到降级默认。
 *   - 应用内切换 AG Audio，绝不改系统级默认设备/注册表。
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Mic, MicOff, Volume2, VolumeX } from 'lucide-react'
import { useChatStore } from '../../store/useChatStore'
import { useVoiceSettings } from '../../store/useVoiceSettings'
import { debugLog } from '../../lib/debug'
import { SherpaStt } from '../../lib/sherpaStt'
import { sherpaAvailable } from '../../lib/sherpaNcnn'

interface AudioDeviceInfo {
  deviceId: string
  kind: MediaDeviceKind
  label: string
}

interface VoiceState {
  agMicId: string | null
  agSpeakerId: string | null
  micReady: boolean
  listening: boolean
  transcript: string
  finalTranscript: string
  speaking: boolean
}

const AG_AUDIO_KEYWORDS = ['AG Audio', 'ag audio', 'Hands-Free']
const CHINESE_LANG = 'zh-CN'

export function useVoice() {
  const [state, setState] = useState<VoiceState>({
    agMicId: null,
    agSpeakerId: null,
    micReady: false,
    listening: false,
    transcript: '',
    finalTranscript: '',
    speaking: false,
  })

  const recognitionRef = useRef<any>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const mountedRef = useRef(true)

  // sherpa 本地识别相关
  const sttRef = useRef<SherpaStt | null>(null)
  const engineRef = useRef<'sherpa' | 'webspeech' | null>(null)

  // ---- 1. 枚举设备：找 AG Audio ----
  const refreshDevices = useCallback(async () => {
    try {
      const temp = await navigator.mediaDevices.getUserMedia({ audio: true })
      temp.getTracks().forEach((t) => t.stop())
      const devices = await navigator.mediaDevices.enumerateDevices()
      const audios: AudioDeviceInfo[] = devices
        .filter((d) => d.kind === 'audioinput' || d.kind === 'audiooutput')
        .map((d) => ({ deviceId: d.deviceId, kind: d.kind, label: d.label }))
      const findAg = (kind: MediaDeviceKind) =>
        audios.find((d) => d.kind === kind && AG_AUDIO_KEYWORDS.some((kw) => d.label.toLowerCase().includes(kw.toLowerCase())))
      const mic = findAg('audioinput')
      const speaker = findAg('audiooutput')
      setState((prev) => ({
        ...prev,
        agMicId: mic?.deviceId || null,
        agSpeakerId: speaker?.deviceId || null,
        micReady: true,
      }))
    } catch (err: any) {
      debugLog('voice', '枚举设备失败（可能被拒绝权限）', { error: err?.message }, 'warn')
      setState((prev) => ({ ...prev, micReady: false }))
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refreshDevices()
    const onChange = () => {
      if (mountedRef.current) void refreshDevices()
    }
    navigator.mediaDevices.addEventListener('devicechange', onChange)
    return () => {
      mountedRef.current = false
      navigator.mediaDevices.removeEventListener('devicechange', onChange)
      stopListening()
      stopSpeaking()
      streamRef.current?.getTracks().forEach((t) => t.stop())
      audioCtxRef.current?.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshDevices])

  // ---- 确保 sherpa 本地识别可用（缓存一次）----
  const ensureSherpa = useCallback(async (): Promise<SherpaStt | null> => {
    if (sttRef.current) return sttRef.current
    if (!(await sherpaAvailable())) return null
    try {
      const stt = await SherpaStt.create(state.agMicId)
      sttRef.current = stt
      engineRef.current = 'sherpa'
      return stt
    } catch (e: any) {
      debugLog('voice', 'sherpa 初始化失败，回退 Web Speech', { error: e?.message }, 'warn')
      return null
    }
  }, [state.agMicId])

  // ---- 2. 开始录音（按设置 sttMode 选择引擎）----
  const startListening = useCallback(async (): Promise<boolean> => {
    const vs = useVoiceSettings.getState()
    // 非「仅浏览器」模式才尝试本地 sherpa 离线识别
    if (vs.sttMode !== 'webspeech') {
      const stt = await ensureSherpa()
      if (stt) {
        try {
          await stt.start({
            onPartial: (t) => setState((prev) => ({ ...prev, transcript: t, finalTranscript: prev.finalTranscript })),
          })
          setState((prev) => ({ ...prev, listening: true, transcript: '', finalTranscript: '' }))
          debugLog('voice', 'sherpa 本地识别已启动')
          return true
        } catch (e: any) {
          debugLog('voice', 'sherpa 启动失败，回退 Web Speech', { error: e?.message }, 'warn')
        }
      }
      // 系统语音不可用：回退浏览器并提示
      if (vs.sttMode === 'sherpa') {
        debugLog('voice', '设置为仅本地离线，但 sherpa 不可用，回退浏览器识别', undefined, 'warn')
      }
    }
    // 回退 / 仅浏览器：Web Speech API
    engineRef.current = 'webspeech'
    return startWebSpeech()
  }, [ensureSherpa])

  // Web Speech 实现（原逻辑，作为回退）
  const startWebSpeech = useCallback((): boolean => {
    if (!('webkitSpeechRecognition' in window || 'SpeechRecognition' in window)) {
      debugLog('voice', '浏览器不支持 SpeechRecognition', undefined, 'error')
      return false
    }
    const SpeechRecognitionCtor = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SpeechRecognitionCtor) return false
    const rec: any = new SpeechRecognitionCtor()
    rec.lang = CHINESE_LANG
    rec.continuous = true
    rec.interimResults = true
    rec.maxAlternatives = 1

    try {
      const constraints: MediaTrackConstraints = {}
      if (state.agMicId) constraints.deviceId = { exact: state.agMicId }
      // getUserMedia 仅用于锁定 AG Audio 设备（Web Speech 本身走系统识别）
      navigator.mediaDevices
        .getUserMedia({ audio: constraints })
        .then((s) => {
          streamRef.current = s
        })
        .catch(() => {
          try {
            navigator.mediaDevices.getUserMedia({ audio: true }).then((s) => (streamRef.current = s))
          } catch {
            /* ignore */
          }
        })
    } catch {
      /* ignore */
    }

    rec.onresult = (event: any) => {
      let interimTranscript = ''
      let finalTranscript = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i]
        if (result.isFinal) finalTranscript += result[0].transcript
        else interimTranscript += result[0].transcript
      }
      setState((prev) => ({
        ...prev,
        transcript: (prev.finalTranscript + ' ' + interimTranscript).trim(),
        finalTranscript: prev.finalTranscript + (finalTranscript ? ' ' + finalTranscript : ''),
      }))
    }
    rec.onerror = (event: any) => {
      if ((event as any).error !== 'no-speech' && (event as any).error !== 'aborted') stopListening()
    }
    rec.onend = () => {
      if (mountedRef.current && state.listening) {
        try {
          rec.start()
        } catch {
          /* ignore */
        }
      }
    }
    recognitionRef.current = rec
    try {
      rec.start()
    } catch (e: any) {
      debugLog('voice', 'rec.start() 失败', { error: e?.message }, 'error')
      return false
    }
    setState((prev) => ({ ...prev, listening: true, transcript: '', finalTranscript: '' }))
    return true
  }, [state.agMicId, state.listening])

  // ---- 3. 停止录音（按引擎路由，返回最终文本）----
  const stopListening = useCallback((): string => {
    let finalFromStop = ''
    if (engineRef.current === 'sherpa' && sttRef.current) {
      finalFromStop = sttRef.current.stop()
    } else {
      const rec = recognitionRef.current
      if (rec) {
        try {
          rec.stop()
        } catch {
          /* ignore */
        }
        recognitionRef.current = null
      }
      streamRef.current?.getTracks().forEach((t) => t.stop())
      streamRef.current = null
    }
    setState((prev) => ({ ...prev, listening: false }))
    return finalFromStop
  }, [])

  // ---- 4. 提交语音文字到对话 ----
  const submitTranscript = useCallback(() => {
    const finalFromStop = stopListening()
    const text = (finalFromStop || state.finalTranscript || state.transcript || '').trim()
    if (!text) return
    debugLog('voice', '提交语音文字', { text, len: text.length, engine: engineRef.current })
    void useChatStore.getState().sendMessage(text)
    setState((prev) => ({ ...prev, transcript: '', finalTranscript: '' }))
  }, [stopListening, state.finalTranscript, state.transcript])

  // ---- 5. TTS：朗读文字（按设置 ttsMode / 音色 / 语速 / 音调 / 设备）----
  const speak = useCallback(
    async (text: string) => {
      const t = (text || '').trim()
      if (!t) return
      const vs = useVoiceSettings.getState()
      const wantCloud = vs.ttsMode === 'cloud' || vs.ttsMode === 'auto'

      // 输出设备路由：仅当设置 device=ag 且检测到 AG Audio 才 setSinkId，绝不改系统默认
      const routeDevice = async (el: { setSinkId?: (id: string) => Promise<void> }) => {
        if (vs.device === 'ag' && state.agSpeakerId && (el as any).setSinkId) {
          try {
            await (el as any).setSinkId(state.agSpeakerId)
          } catch {
            /* ignore */
          }
        }
      }

      // 优先：云端 Azure 神经语音（密钥在服务端 /api/tts）
      if (wantCloud) {
        try {
          const resp = await fetch('/api/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              text: t,
              voice: vs.cloudVoice,
              rate: vs.rate,
              pitch: vs.pitch,
            }),
          })
          if (resp.ok) {
            const blob = await resp.blob()
            const url = URL.createObjectURL(blob)
            const audio = new Audio(url)
            await routeDevice(audio)
            setState((prev) => ({ ...prev, speaking: true }))
            const done = () => {
              setState((prev) => ({ ...prev, speaking: false }))
              try {
                URL.revokeObjectURL(url)
              } catch {
                /* ignore */
              }
            }
            audio.onended = done
            audio.onerror = done
            await audio.play()
            return
          }
        } catch {
          /* 云端不可用 → 回退本地 */
        }
      }

      // 回退 / 仅本地：本地 SpeechSynthesis
      if (!('speechSynthesis' in window)) return
      window.speechSynthesis.cancel()
      const utter = new SpeechSynthesisUtterance(t)
      utter.lang = CHINESE_LANG
      utter.rate = vs.rate
      utter.pitch = vs.pitch
      utter.volume = vs.volume
      const voices = window.speechSynthesis.getVoices()
      const pick = (vs2: SpeechSynthesisVoice[]) => {
        if (vs.localVoice) {
          const exact = vs2.find((v) => v.name === vs.localVoice)
          if (exact) return exact
        }
        return (
          vs2.find((v) => /XiaoxiaoNeural/i.test(v.name)) ||
          vs2.find((v) => /Xiaoxiao/i.test(v.name)) ||
          vs2.find((v) => v.lang.startsWith('zh') && /female|女/i.test(v.name)) ||
          vs2.find((v) => v.lang.startsWith('zh') && /Chinese|Microsoft/i.test(v.name)) ||
          vs2.find((v) => v.lang.startsWith('zh')) ||
          vs2.find((v) => v.lang.includes('CN')) ||
          null
        )
      }
      const zhVoice = pick(voices)
      if (zhVoice) utter.voice = zhVoice
      if (vs.device === 'ag' && state.agSpeakerId && audioCtxRef.current) {
        try {
          await (audioCtxRef.current as any).setSinkId(state.agSpeakerId)
        } catch {
          /* ignore */
        }
      }
      utter.onstart = () => setState((prev) => ({ ...prev, speaking: true }))
      utter.onend = () => setState((prev) => ({ ...prev, speaking: false }))
      utter.onerror = () => setState((prev) => ({ ...prev, speaking: false }))
      window.speechSynthesis.speak(utter)
    },
    [state.agSpeakerId],
  )

  const stopSpeaking = useCallback(() => {
    try {
      window.speechSynthesis.cancel()
    } catch {
      /* ignore */
    }
    setState((prev) => ({ ...prev, speaking: false }))
  }, [])

  useEffect(() => {
    const loadVoices = () => {
      const voices = window.speechSynthesis.getVoices()
      if (voices.length > 0) {
        debugLog('voice', 'TTS 语音加载完成', { count: voices.length, zh: voices.filter((v) => v.lang.startsWith('zh')).length })
      }
    }
    loadVoices()
    window.speechSynthesis.onvoiceschanged = loadVoices
    return () => {
      window.speechSynthesis.onvoiceschanged = null
    }
  }, [])

  useEffect(() => {
    if (state.agSpeakerId && !audioCtxRef.current) {
      try {
        audioCtxRef.current = new AudioContext()
      } catch {
        /* ignore */
      }
    }
  }, [state.agSpeakerId])

  return {
    ...state,
    engine: engineRef.current,
    startListening,
    stopListening,
    submitTranscript,
    speak,
    stopSpeaking,
    refreshDevices,
  }
}

// ---------- UI 组件 ----------

export function VoiceButton() {
  const voice = useVoice()
  const [held, setHeld] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const handlePointerDown = (e: React.PointerEvent) => {
    e.preventDefault()
    setHeld(true)
    void voice.startListening()
  }
  const handlePointerUp = () => {
    setHeld(false)
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (voice.listening || voice.transcript) {
      voice.submitTranscript()
    }
  }
  const handleClick = (e: React.MouseEvent) => {
    e.preventDefault()
    if (voice.listening) {
      voice.submitTranscript()
    } else {
      void voice.startListening()
    }
  }

  useEffect(() => {
    if (held && voice.listening) {
      timerRef.current = setTimeout(() => {
        voice.submitTranscript()
        setHeld(false)
      }, 60_000)
    }
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
  }, [held, voice.listening, voice.submitTranscript])

  const isActive = voice.listening || held
  const hasText = (voice.transcript || voice.finalTranscript).trim().length > 0

  const engineLabel = voice.engine === 'sherpa' ? '本地离线识别' : voice.engine === 'webspeech' ? '浏览器识别' : '语音输入'

  return (
    <div className="flex items-center gap-1">
      <button
        onClick={handleClick}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerUp}
        onPointerLeave={() => {
          if (held) handlePointerUp()
        }}
        title={
          voice.agMicId
            ? isActive
              ? '松开发送（或点此发送）'
              : `按住录音（AG Audio 麦克风 · ${engineLabel}）`
            : `语音输入（未检测到 AG Audio，使用默认麦克风 · ${engineLabel}）`
        }
        className={`mb-0.5 flex h-9 w-9 items-center justify-center rounded-xl p-0 transition-all ${
          isActive ? 'bg-red-500/20 text-red-400 animate-pulse' : 'hover:bg-surface text-faint hover:text-ink'
        }`}
      >
        {isActive ? <MicOff size={15} /> : <Mic size={15} />}
      </button>

      {isActive && (
        <div className="max-w-[180px] truncate rounded-lg border border-line bg-surface/80 px-2 py-1 text-[11px] text-dim">
          {hasText ? (
            <span className="text-accent-strong">{voice.transcript || voice.finalTranscript}</span>
          ) : (
            <span className="animate-pulse text-faint">{voice.engine === 'sherpa' ? '本地识别中…' : '正在听…'}</span>
          )}
        </div>
      )}

      {voice.speaking ? (
        <button
          onClick={voice.stopSpeaking}
          title="停止朗读"
          className="mb-0.5 flex h-9 w-9 items-center justify-center rounded-xl bg-accent-strong/20 p-0 text-accent-strong"
        >
          <VolumeX size={14} />
        </button>
      ) : (
        <button
          onClick={() => {
            const { sessions, activeId } = useChatStore.getState()
            const sess = sessions[activeId || '']
            if (!sess) return
            const msgs = sess.messages.filter((m) => m.role === 'assistant')
            const last = msgs[msgs.length - 1]
            if (last?.content) void voice.speak(last.content)
          }}
          title="朗读最新回复（AG Audio 扬声器）"
          className="mb-0.5 flex h-9 w-9 items-center justify-center rounded-xl p-0 text-faint hover:bg-surface hover:text-ink"
        >
          <Volume2 size={14} />
        </button>
      )}
    </div>
  )
}
