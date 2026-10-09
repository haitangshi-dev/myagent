/**
 * sherpaNcnn.ts
 * ---------------------------------------------------------------------------
 * 浏览器/Electron 内本地离线语音识别（sherpa-ncnn wasm + 内嵌 int8 模型中英双语）。
 *
 * 用法：
 *   const rec = await initSherpaNcnn('/sherpa')
 *   const stream = rec.createStream()
 *   stream.acceptWaveform(16000, float32Samples)  // 16k 单声道，[-1,1]
 *   while (rec.isReady(stream)) rec.decode(stream)
 *   const text = rec.getResult(stream)
 *
 * 模型：v2.1.7 的 `sherpa-ncnn-wasm-main.data`（~141MB）已内嵌
 *   sherpa-ncnn-streaming-zipformer-bilingual-zh-en-2023-02-13 的 int8 模型，
 *   开箱即用，无需任何云端识别、也无需额外下载模型文件。
 *   createRecognizer() 走包内胶水默认 config（./encoder... 等相对路径 → 由
 *   内嵌 data 提供），本文件不传 config（包内胶水忽略 config 参数）。
 *
 * 失败回退：wasm 缺失/加载失败 → initSherpaNcnn reject → 上层回退 Web Speech。
 * ---------------------------------------------------------------------------
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
// 让 TS 不报 wasm 全局对象
declare global {
  interface Window {
    Module?: any;
  }
}

export interface SherpaStream {
  acceptWaveform(sampleRate: number, samples: Float32Array): void;
  free(): void;
}

export interface SherpaRecognizer {
  createStream(): SherpaStream;
  isReady(s: SherpaStream): boolean;
  isEndpoint(s: SherpaStream): boolean;
  decode(s: SherpaStream): void;
  reset(s: SherpaStream): void;
  getResult(s: SherpaStream): string;
}

let initPromise: Promise<SherpaRecognizer> | null = null;

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`加载脚本失败: ${src}`));
    document.body.appendChild(s);
  });
}

export function initSherpaNcnn(modelPath = '/sherpa'): Promise<SherpaRecognizer> {
  if (initPromise) return initPromise;

  initPromise = new Promise<SherpaRecognizer>((resolve, reject) => {
    try {
      // 先配置全局 Module（emscripten 运行时会沿用），再注入 wasm 运行时脚本。
      // locateFile 统一把 .wasm / .data 指向 /sherpa/ 下对应文件名。
      (window as any).Module = {
        locateFile: (path: string) => `${modelPath}/${path}`,
        onRuntimeInitialized: () => {
          try {
            // 包内胶水 createRecognizer() 无参，使用全局 Module + 内嵌模型
            const recognizer: SherpaRecognizer = (window as any).createRecognizer();
            resolve(recognizer);
          } catch (e: any) {
            reject(new Error('sherpa 初始化失败: ' + (e?.message || e)));
          }
        },
      };

      // sherpa-ncnn.js（胶水，定义全局 createRecognizer / Recognizer）
      // sherpa-ncnn-wasm-main.js（emscripten 运行时，最后注入并触发 onRuntimeInitialized）
      loadScript(`${modelPath}/sherpa-ncnn.js`)
        .then(() => loadScript(`${modelPath}/sherpa-ncnn-wasm-main.js`))
        .catch((e) => reject(e));
    } catch (e: any) {
      reject(new Error('sherpa 加载异常: ' + (e?.message || e)));
    }
  });

  return initPromise;
}

// 探测 wasm 运行时入口是否可达（用于决定走 sherpa 还是回退 Web Speech）
export async function sherpaAvailable(modelPath = '/sherpa'): Promise<boolean> {
  try {
    const res = await fetch(`${modelPath}/sherpa-ncnn-wasm-main.js`, { method: 'HEAD' });
    return res.ok;
  } catch {
    return false;
  }
}
