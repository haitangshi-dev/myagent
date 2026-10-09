/**
 * fetch-sherpa-assets.mjs
 * ---------------------------------------------------------------------------
 * 拉取 sherpa-ncnn 的 WebAssembly 构建（v2.1.7，已内嵌中英双语 int8 模型），
 * 放到 web/public/sherpa/ 下，供前端在浏览器/Electron 内本地离线做语音识别
 *（不依赖任何云端识别）。
 *
 * 运行（在能正常访问 GitHub 的机器上，比如你的编译 PC；沙箱网络可能拉不到）：
 *   node scripts/fetch-sherpa-assets.mjs
 * 或在 web/ 目录：  npm run fetch:sherpa
 *
 * 说明：
 *  - wasm 包（sherpa-ncnn-wasm-simd-v2.1.7.tar.bz2）是【必需】的，解压后提供 4 个产物：
 *      sherpa-ncnn.js            （胶水，定义全局 createRecognizer / Recognizer）
 *      sherpa-ncnn-wasm-main.js  （emscripten 运行时）
 *      sherpa-ncnn-wasm-main.wasm（wasm 字节码）
 *      sherpa-ncnn-wasm-main.data（~141MB，内嵌 bilingual zh+en int8 模型）
 *  - 模型已内嵌在 .data 里（sherpa-ncnn-streaming-zipformer-bilingual-zh-en-2023-02-13），
 *    无需再单独下载模型文件。包内胶水 createRecognizer() 用默认 config 直接读取内嵌模型。
 *  - 若 GitHub 直连慢/被墙，可设环境变量用代理：
 *      GH_PROXY=https://ghproxy.net  node scripts/fetch-sherpa-assets.mjs
 *    （脚本会把下载 URL 的 https://github.com 替换为 ${GH_PROXY}/https://github.com）
 * ---------------------------------------------------------------------------
 */
import { execSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, statSync, writeFileSync, copyFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..');
const OUT_DIR = join(REPO_ROOT, 'web', 'public', 'sherpa');

const WASM_VERSION = 'v2.1.7';
const WASM_URL = `https://github.com/k2-fsa/sherpa-ncnn/releases/download/${WASM_VERSION}/sherpa-ncnn-wasm-simd-${WASM_VERSION}.tar.bz2`;
const WASM_ARTIFACTS = [
  'sherpa-ncnn.js',
  'sherpa-ncnn-wasm-main.js',
  'sherpa-ncnn-wasm-main.wasm',
  'sherpa-ncnn-wasm-main.data',
];

mkdirSync(OUT_DIR, { recursive: true });

function log(msg) {
  console.log(`[fetch-sherpa] ${msg}`);
}

// 允许通过 GH_PROXY 走代理（值形如 https://ghproxy.net）
function resolveUrl(url) {
  const proxy = process.env.GH_PROXY?.replace(/\/$/, '');
  if (proxy) return url.replace('https://github.com', `${proxy}/https://github.com`);
  return url;
}

// ---------- 下载并解压 wasm 包 ----------
async function fetchWasm() {
  const tmpTar = join(OUT_DIR, 'sherpa_wasm.tar.bz2');
  const tmpExtract = join(OUT_DIR, '_wasm_extract');
  log(`下载 wasm 构建: ${resolveUrl(WASM_URL)}`);
  const res = await fetch(resolveUrl(WASM_URL));
  if (!res.ok) throw new Error(`下载 wasm 失败: HTTP ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  writeFileSync(tmpTar, buf);
  log(`已下载 ${(buf.length / 1024 / 1024).toFixed(1)} MB，解压中…`);

  rmSync(tmpExtract, { recursive: true, force: true });
  mkdirSync(tmpExtract, { recursive: true });
  try {
    execSync(`tar -xjf "${tmpTar}" -C "${tmpExtract}"`, { stdio: 'inherit' });
  } catch (e) {
    throw new Error(`解压失败（需要系统 tar 支持 .tar.bz2；Windows 10+ 自带 tar.exe）: ${e.message}`);
  }

  // 在解压目录里递归找 4 个产物
  const found = {};
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (WASM_ARTIFACTS.includes(name)) found[name] = p;
    }
  };
  walk(tmpExtract);

  for (const art of WASM_ARTIFACTS) {
    if (!found[art]) throw new Error(`解压结果缺少 ${art}`);
    copyFileSync(found[art], join(OUT_DIR, art));
    log(`  ✓ ${art} (${(statSync(found[art]).size / 1024 / 1024).toFixed(1)} MB)`);
  }

  rmSync(tmpTar, { force: true });
  rmSync(tmpExtract, { recursive: true, force: true });
}

// ---------- main ----------
try {
  await fetchWasm();
  log('完成。wasm + 内嵌模型均在 web/public/sherpa/。');
  log('之后运行 npm run build / npm run dist:win 即可打包进桌面端。');
} catch (e) {
  console.error('[fetch-sherpa] 失败:', e.message);
  process.exit(1);
}
