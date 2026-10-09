import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type TtsMode = 'auto' | 'cloud' | 'local'
export type SttMode = 'auto' | 'sherpa' | 'webspeech'
export type VoiceDevice = 'ag' | 'system'

export interface VoiceSettings {
  // 语音输出（TTS）
  ttsMode: TtsMode // auto=云端优先回退本地 / cloud=仅云端 / local=仅本地
  cloudVoice: string // 云端音色（Azure voice name，如 zh-CN-XiaoxiaoNeural）
  localVoice: string | null // 本地音色（SpeechSynthesis voice name）；null=自动选最佳中文女声
  rate: number // 语速倍率 0.5~3.0（默认 1.05）
  pitch: number // 音调倍率 0.5~2.0（默认 1.0）
  volume: number // 音量 0~1（默认 1.0）
  device: VoiceDevice // ag=AG Audio 优先 / system=系统默认

  // 语音输入（STT）
  sttMode: SttMode // auto=sherpa 本地优先 / sherpa=仅本地离线 / webspeech=仅浏览器

  set: (patch: Partial<VoiceSettings>) => void
  reset: () => void
}

export const DEFAULT_VOICE_SETTINGS: Omit<VoiceSettings, 'set' | 'reset'> = {
  ttsMode: 'auto',
  cloudVoice: 'zh-CN-XiaoxiaoNeural',
  localVoice: null,
  rate: 1.05,
  pitch: 1.0,
  volume: 1.0,
  device: 'ag',
  sttMode: 'auto',
}

export const useVoiceSettings = create<VoiceSettings>()(
  persist(
    (set) => ({
      ...DEFAULT_VOICE_SETTINGS,
      set: (patch) => set(patch),
      reset: () => set({ ...DEFAULT_VOICE_SETTINGS }),
    }),
    {
      name: 'my-agent-voice',
      partialize: (s) => ({
        ttsMode: s.ttsMode,
        cloudVoice: s.cloudVoice,
        localVoice: s.localVoice,
        rate: s.rate,
        pitch: s.pitch,
        volume: s.volume,
        device: s.device,
        sttMode: s.sttMode,
      }),
    },
  ),
)

// 已知 Azure 中文神经语音（name -> 中文描述）
export const CLOUD_VOICES: { id: string; label: string }[] = [
  { id: 'zh-CN-XiaoxiaoNeural', label: '云珍 · 女声·温柔（默认推荐）' },
  { id: 'zh-CN-XiaoyiNeural', label: '晓伊 · 女声·活泼' },
  { id: 'zh-CN-XiaohanNeural', label: '晓涵 · 女声·成熟' },
  { id: 'zh-CN-XiaomoNeural', label: '晓墨 · 女声·知性' },
  { id: 'zh-CN-XiaoxuanNeural', label: '晓萱 · 女声·甜美' },
  { id: 'zh-CN-XiaoyouNeural', label: '晓悠 · 童声·可爱' },
  { id: 'zh-CN-YunyangNeural', label: '云扬 · 男声·新闻播报' },
  { id: 'zh-CN-YunxiNeural', label: '云希 · 男声·阳光' },
  { id: 'zh-CN-YunjianNeural', label: '云健 · 男声·浑厚' },
]
