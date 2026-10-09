/**
 * sherpaStt.ts
 * ---------------------------------------------------------------------------
 * 基于 sherpa-ncnn wasm 的本地离线语音识别流水线：
 *   麦克风 → AudioContext → ScriptProcessor(降采样到 16k 单声道) → recognizer
 *
 * 不依赖任何云端识别；麦克风硬件指定由 deviceId 控制（AG Audio 优先，仅本 app）。
 * AudioContext 输出接 0 增益节点到 destination，仅为驱动音频图、不产生回放。
 * ---------------------------------------------------------------------------
 */
import { initSherpaNcnn, type SherpaRecognizer, type SherpaStream } from './sherpaNcnn';

export interface SherpaSttCallbacks {
  onPartial?: (text: string) => void;
}

export class SherpaStt {
  private recognizer: SherpaRecognizer;
  private stream: SherpaStream | null = null;
  private audioCtx: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private gain: GainNode | null = null;
  private mediaStream: MediaStream | null = null;
  private deviceId: string | null;
  private lastPartial = '';

  static async create(deviceId: string | null = null): Promise<SherpaStt> {
    const recognizer = await initSherpaNcnn('/sherpa');
    return new SherpaStt(recognizer, deviceId);
  }

  private constructor(recognizer: SherpaRecognizer, deviceId: string | null) {
    this.recognizer = recognizer;
    this.deviceId = deviceId;
  }

  async start(cb: SherpaSttCallbacks): Promise<void> {
    // 优先用指定设备（AG Audio），失败回退默认麦克风
    let mediaStream: MediaStream;
    const constraints: MediaTrackConstraints = this.deviceId
      ? { deviceId: { exact: this.deviceId } }
      : {};
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
    } catch {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    }
    this.mediaStream = mediaStream;

    const audioCtx = new AudioContext();
    if (audioCtx.state === 'suspended') await audioCtx.resume();
    this.audioCtx = audioCtx;

    const source = audioCtx.createMediaStreamSource(mediaStream);
    this.source = source;

    const bufferSize = 4096;
    const processor = audioCtx.createScriptProcessor(bufferSize, 1, 1);
    this.processor = processor;

    // 0 增益：驱动音频图但不回放麦克风，避免啸叫
    const gain = audioCtx.createGain();
    gain.gain.value = 0;
    this.gain = gain;

    this.stream = this.recognizer.createStream();
    const recordRate = audioCtx.sampleRate;

    processor.onaudioprocess = (e: AudioProcessingEvent) => {
      if (!this.stream) return;
      const input = e.inputBuffer.getChannelData(0);
      const samples = downsampleBuffer(input, recordRate, 16000);
      this.stream.acceptWaveform(16000, samples);
      while (this.recognizer.isReady(this.stream)) {
        this.recognizer.decode(this.stream);
      }
      const text = this.recognizer.getResult(this.stream);
      if (text) {
        this.lastPartial = text;
        cb.onPartial?.(text);
      }
    };

    source.connect(processor);
    processor.connect(gain);
    gain.connect(audioCtx.destination);
  }

  // 停止并返回最终识别文本
  stop(): string {
    let text = '';
    if (this.stream) {
      text = this.recognizer.getResult(this.stream);
      if (!text && this.lastPartial) text = this.lastPartial;
      try {
        this.recognizer.reset(this.stream);
        this.stream.free();
      } catch {
        /* ignore */
      }
      this.stream = null;
    }
    this.cleanup();
    return text.trim();
  }

  private cleanup(): void {
    try {
      this.processor?.disconnect();
    } catch {
      /* ignore */
    }
    try {
      this.gain?.disconnect();
    } catch {
      /* ignore */
    }
    try {
      this.source?.disconnect();
    } catch {
      /* ignore */
    }
    this.mediaStream?.getTracks().forEach((t) => t.stop());
    try {
      this.audioCtx?.close();
    } catch {
      /* ignore */
    }
    this.processor = null;
    this.gain = null;
    this.source = null;
    this.audioCtx = null;
    this.mediaStream = null;
  }
}

// 把任意采样率降采样到目标采样率（平均法，与官方 demo 一致）
function downsampleBuffer(buffer: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate) return buffer;
  const ratio = fromRate / toRate;
  const newLength = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLength);
  let offsetResult = 0;
  let offsetBuffer = 0;
  while (offsetResult < newLength) {
    const nextOffsetBuffer = Math.round((offsetResult + 1) * ratio);
    let accum = 0;
    let count = 0;
    for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i++) {
      accum += buffer[i];
      count++;
    }
    result[offsetResult] = count > 0 ? accum / count : 0;
    offsetResult++;
    offsetBuffer = nextOffsetBuffer;
  }
  return result;
}
