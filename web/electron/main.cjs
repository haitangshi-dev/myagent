'use strict';

/**
 * MY_AGENT — Electron 主进程（壳，v3：自诊断 + 可见状态）。
 *
 * 相比上一版的关键改进：
 *   1. 启动即创建一个「可见的加载窗口」（show:true），带状态文字，避免静默黑屏。
 *   2. 任何失败都通过 dialog.showErrorBox 弹窗 + 写 backend.log，不再静默。
 *   3. 兜底：即便 ready-to-show 不触发，也会在超时后强制显示窗口。
 *   4. 单实例锁被占用时明确提示「已有实例运行」，而不是静默退出。
 */

const { app, BrowserWindow, Tray, Menu, globalShortcut, Notification, shell, dialog, ipcMain, clipboard, screen, nativeImage, nativeTheme } = require('electron');
const { spawn, spawnSync } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

// ---------------------------------------------------------------------------
// 剪贴板文件解析辅助（Ctrl+V 粘贴上传）：解析 Windows CF_HDROP 格式
// ---------------------------------------------------------------------------
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif', 'tiff', 'svg']);
const MIME_BY_EXT = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  bmp: 'image/bmp',
  ico: 'image/x-icon',
  avif: 'image/avif',
  tiff: 'image/tiff',
  svg: 'image/svg+xml',
};

// 解析 clipboard.read('CF_HDROP') 返回的 DROPFILES 二进制，提取文件路径列表
function parseDropFiles(buf) {
  try {
    if (!buf || buf.length < 20) return [];
    // DROPFILES 头：pFiles(DWORD) @0, pt(POINT) @4, fNC(BOOL) @12, fWide(BOOL) @16
    const pFiles = buf.readUInt32LE(0);
    const fWide = buf.readUInt32LE(16) !== 0;
    const base = pFiles >= 0 && pFiles < buf.length ? pFiles : 20;
    const slice = buf.slice(base);
    const paths = [];
    if (fWide) {
      // UTF-16LE，以双 NUL（\0\0）结尾的字符串数组
      let i = 0;
      while (i + 1 < slice.length) {
        if (slice[i] === 0 && slice[i + 1] === 0) break;
        let j = i;
        while (j + 1 < slice.length && !(slice[j] === 0 && slice[j + 1] === 0)) j += 2;
        const s = slice.slice(i, j).toString('utf16le');
        if (s) paths.push(s);
        i = j + 2;
      }
    } else {
      // ANSI（很少见），以双 NUL 结尾
      const str = slice.toString('latin1');
      const parts = str.split('\0\0')[0].split('\0');
      for (const p of parts) if (p) paths.push(p);
    }
    return paths;
  } catch (_) {
    return [];
  }
}

const HOST = '127.0.0.1'; // PC-only：后端绑 127.0.0.1 回环，前后端地址一致，规避 Windows 上 :: 的 IPv6-only 陷阱

// 托盘/窗口图标：优先 electron/icon.png（确保随 app 打包进 resources）；回退到 public/icon.png
const TRAY_ICON = (() => {
  const cand = [
    path.join(__dirname, 'icon.png'),
    path.join(__dirname, '..', 'public', 'icon.png'),
  ];
  for (const c of cand) if (fs.existsSync(c)) return c;
  return cand[0];
})();

const HIDDEN_LAUNCH = process.argv.includes('--hidden') || process.env.MY_AGENT_HIDDEN === '1';
let BACKEND_PORT = parseInt(process.env.MY_AGENT_PORT || '8000', 10);
let BASE_URL = `http://${HOST}:${BACKEND_PORT}`;

const DEV = !!process.env.ELECTRON_DEV; // 指向 Vite dev server :5173
const HEALTH_TIMEOUT_MS = 30000;

let backendProc = null;
let _managedKill = false;  // Electron 主动杀后端（重启/退出）时标记，exit 事件中不触发自愈重启
let pollTimer = null;
let backendLogPath = null;
let mainWindow = null;
let rendererMounted = false; // 前端 React 是否已挂载（app:renderer-ready 置位）
let appQuitting = false; // 应用正常退出时不触发后端自愈重启

// 系统通知（托盘气泡）；Electron 自带 Notification，无需额外依赖
function notify(title, body) {
  try {
    if (typeof Notification !== 'undefined') {
      new Notification({ title: title, body: body });
    }
  } catch (_) {
    /* 通知失败不阻断主流程 */
  }
}

function setPort(p) {
  BACKEND_PORT = p;
  BASE_URL = `http://${HOST}:${p}`;
}

// ---------------------------------------------------------------------------
// 日志
// ---------------------------------------------------------------------------
function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}`;
  console.log(line);
  if (backendLogPath) {
    try {
      fs.appendFileSync(backendLogPath, line + '\n');
    } catch (_) {
      /* 忽略日志写入失败 */
    }
  }
}

// 顶层异常一律弹窗 + 写日志，避免静默崩溃
process.on('uncaughtException', (e) => {
  log('uncaughtException: ' + (e && e.stack ? e.stack : e));
  try {
    dialog.showErrorBox('MY_AGENT 崩溃', String(e && e.stack ? e.stack : e));
  } catch (_) {}
});
process.on('unhandledRejection', (e) => {
  log('unhandledRejection: ' + e);
  try {
    dialog.showErrorBox('MY_AGENT 未处理异常', String(e));
  } catch (_) {}
});

// ---------------------------------------------------------------------------
// 路径解析
// ---------------------------------------------------------------------------
function resolveBackendRoot() {
  if (process.env.ELECTRON_PYTHON_ROOT) return process.env.ELECTRON_PYTHON_ROOT;
  if (app.isPackaged) return path.join(process.resourcesPath, 'backend');
  // 开发态：web/ 的上一级 = 项目根 C:/my agent
  return path.resolve(__dirname, '..', '..');
}

function findPython() {
  const candidates = ['python', 'python3', 'py'];
  for (const c of candidates) {
    try {
      const r = spawnSync(c, ['--version'], { stdio: 'pipe', timeout: 5000 });
      if (r.status === 0) return c;
    } catch (_) {
      /* 找不到就试下一个 */
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// 后端生命周期
// ---------------------------------------------------------------------------
function startBackend(root) {
  const py = findPython();
  if (!py) {
    const msg = '未找到 Python。请安装 Python 3 并加入 PATH，或设置 ELECTRON_PYTHON_ROOT。';
    log('[electron] ' + msg);
    dialog.showErrorBox('MY_AGENT 启动失败', msg);
    return false;
  }
  const entry = path.join(root, 'main.py');
  if (!fs.existsSync(entry)) {
    const msg = '找不到后端入口: ' + entry;
    log('[electron] ' + msg);
    dialog.showErrorBox('MY_AGENT 启动失败', msg);
    return false;
  }

  // 知识库目录：独立目录，不进安装包，初始为空库。
  // 打包态：%APPDATA%/MY_AGENT/knowledge（卸载不丢）；开发态由后端默认解析。
  const kbDir = path.join(app.getPath('userData'), 'knowledge');

  // 首次启动自动创建知识库目录（避免后端首次检索时目录不存在）
  try {
    if (!fs.existsSync(kbDir)) {
      fs.mkdirSync(kbDir, { recursive: true });
      log(`[electron] 已创建知识库目录: ${kbDir}`);
    }
  } catch (e) {
    log(`[electron] 创建知识库目录失败: ${e}`);
  }

  const args = ['main.py', '--host', HOST, '--port', String(BACKEND_PORT)];
  log(`[electron] 启动后端: ${py} ${args.join(' ')}  (cwd=${root})`);

  // spawn 的 stdio 不接受 WriteStream 对象，只能用 'pipe' + 手动转发到日志文件
  backendProc = spawn(py, args, {
    cwd: root,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env,
      // 技能安装目录放到用户数据目录（更新 exe 不丢，且不污染安装目录）
      MY_AGENT_SKILLS_DIR: path.join(app.getPath('userData'), 'skills'),
      // 全局记忆放到用户数据目录（更新 / 重装 exe 不丢，不污染安装目录）
      MY_AGENT_MEMORY_DIR: path.join(app.getPath('userData'), 'memory'),
      // 业务数据（任务看板 boards / 定时任务 schedules.json）放到用户数据目录
      // （更新 / 重装 exe 不丢，不污染安装目录）
      MY_AGENT_DATA_DIR: path.join(app.getPath('userData'), 'data'),
      // 知识库目录放到用户数据目录（更新 / 重装 exe 不丢，初始为空库）
      MY_AGENT_KB_DIR: kbDir,
    },
  });

  // stdout/stderr 同时输出到控制台（开发态可见）和 backend.log（用户排查用）
  const tee = (stream, label) => {
    if (!stream) return;
    stream.on('data', (d) => {
      const line = `[${label}] ${d}`;
      process.stdout.write(line);
      if (backendLogPath) {
        try { fs.appendFileSync(backendLogPath, line); } catch (_) {}
      }
    });
  };
  tee(backendProc.stdout, 'backend');
  tee(backendProc.stderr, 'backend');
  backendProc.on('exit', (code, signal) => {
    backendProc = null;
    if (appQuitting) return; // 应用正常退出（关闭/托盘退出）不重启
    if (_managedKill) {      // Electron 主动重启/替换后端（菜单「重启后端」等），已自行拉起，不重复
      _managedKill = false;
      return;
    }
    // 其余（崩溃 code!=0 或被外部强杀 code=null/signal 触发）一律自愈重启
    log(`[electron] 后端进程退出（code=${code}, signal=${signal}），准备自愈重启`);
    notify('MY_AGENT', '后端意外退出，正在自动恢复…');
    scheduleBackendRestart();
  });
  return true;
}

// 自愈：后端崩溃后指数退避重启（2→4→8→…封顶 30s），避免崩溃循环刷屏/烧 CPU
let backendRestartAttempts = 0;
let backendRestartTimer = null;
function scheduleBackendRestart() {
  if (appQuitting || backendRestartTimer) return;
  const backoff = Math.min(2 ** (backendRestartAttempts + 1), 30);
  backendRestartAttempts += 1;
  log(`[electron] ${backoff}s 后重启后端（第 ${backendRestartAttempts} 次）`);
  backendRestartTimer = setTimeout(() => {
    backendRestartTimer = null;
    if (appQuitting) return;
    // 端口可能仍被占用，先探活；活着就跳过
    checkOnce().then((alive) => {
      if (alive) return;
      const root = app.getAppPath ? app.getAppPath().replace(/[\\/]app\.asar$/, '') : process.cwd();
      const ok = startBackend(root);
      if (ok) {
        waitForBackend(
          () => {
            backendRestartAttempts = 0;
            notify('MY_AGENT', '后端已恢复');
            setStatus && setStatus('READY');
          },
          () => scheduleBackendRestart(),
        );
      } else {
        scheduleBackendRestart();
      }
    });
  }, backoff * 1000);
}

function killBackend() {
  if (!backendProc) return;
  _managedKill = true;  // 标记：本次退出是 Electron 主动发起，exit 时勿自愈重启
  const pid = backendProc.pid;
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/F', '/T', '/PID', String(pid)], {
        stdio: 'ignore',
        windowsHide: true,
      });
    } else {
      backendProc.kill('SIGTERM');
    }
  } catch (_) {
    /* 忽略清理异常 */
  }
  backendProc = null;
}

// ---------------------------------------------------------------------------
// 健康检查 / 前端探测 / 空闲端口
// ---------------------------------------------------------------------------
function checkOnce(port) {
  const url = `http://${HOST}:${port || BACKEND_PORT}/api/health`;
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.setTimeout(1200, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function servesFrontend() {
  return new Promise((resolve) => {
    const req = http.get(`${BASE_URL}/`, (res) => {
      const ct = res.headers['content-type'] || '';
      res.resume();
      resolve(res.statusCode === 200 && ct.includes('text/html'));
    });
    req.on('error', () => resolve(false));
    req.setTimeout(1200, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function findFreePort(start) {
  for (let p = start; p < start + 50; p++) {
    if (!(await checkOnce(p))) return p;
  }
  return start;
}

function waitForBackend(onReady, onTimeout) {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  const tick = async () => {
    if (await checkOnce()) {
      clearInterval(pollTimer);
      pollTimer = null;
      onReady();
      return;
    }
    if (Date.now() > deadline) {
      clearInterval(pollTimer);
      pollTimer = null;
      onTimeout();
    }
  };
  pollTimer = setInterval(tick, 500);
  tick();
}

// ---------------------------------------------------------------------------
// 加载窗口（可见 + 状态文字）
// ---------------------------------------------------------------------------
function splashHtml() {
  const html = `<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;height:100vh;background:#0d0a07;color:#b8996a;
font-family:'Press Start 2P',monospace;display:flex;
flex-direction:column;align-items:center;justify-content:center;gap:14px">
<div style="font-size:18px;color:#FB923C;letter-spacing:2px">MY_AGENT</div>
<div id="s" style="font-size:10px;opacity:.8">INITIALIZING...</div>
<div style="width:160px;height:4px;background:#231a0e;border:2px solid #3d2e18;overflow:hidden">
<div id="bar" style="width:30%;height:100%;background:#F97316"></div></div>
</body></html>`;
  return 'data:text/html,' + encodeURIComponent(html);
}

function setStatus(t) {
  if (!mainWindow) return;
  try {
    mainWindow.webContents.executeJavaScript(
      `var s=document.getElementById('s');if(s)s.textContent=${JSON.stringify(t)};`
    );
  } catch (_) {
    /* 页面还没加载好，忽略 */
  }
}

// 原生 Windows 材质（Win11 mica/acrylic）；失败静默降级，不影响功能
function applyWindowMaterial(win, material) {
  if (process.platform !== 'win32' || !win) return;
  try {
    if (typeof win.setBackgroundMaterial === 'function') {
      win.setBackgroundMaterial(material);
    }
  } catch (_) {
    /* 老系统/不支持则忽略，退回普通窗口 */
  }
}

// 系统主题跟随：Windows 下跟随系统浅/深色，并实时推送前端
function initThemeSync() {
  if (process.platform !== 'win32') return;
  try {
    nativeTheme.themeSource = 'system'; // 跟随系统
  } catch (_) {}
  try {
    nativeTheme.on('updated', () => {
      const dark = nativeTheme.shouldUseDarkColors;
      if (mainWindow && mainWindow.webContents && !mainWindow.webContents.isDestroyed()) {
        mainWindow.webContents.send('theme:change', dark);
      }
        if (edgeWindow && edgeWindow.webContents && !edgeWindow.webContents.isDestroyed()) {
        edgeWindow.webContents.send('theme:change', dark);
        try { edgeWindow.setBackgroundColor(dark ? '#1a1208' : '#f3e9dc'); } catch (_) {}
      }
      if (mascotWindow && mascotWindow.webContents && !mascotWindow.webContents.isDestroyed()) {
        mascotWindow.webContents.send('theme:change', dark);
      }
    });
  } catch (_) {}
}

// 前端加载后兜底校验：若 8s 内 React 仍未挂载（#root 无子节点），
// 说明前端白屏（通常是资源 404 / 脚本运行时报错），主动弹窗 + 写日志，而不是静默白屏。
function verifyFrontendMounted() {
  if (!mainWindow) return;
  setTimeout(async () => {
    try {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      const ok = await mainWindow.webContents.executeJavaScript(
        "document.getElementById('root') && document.getElementById('root').childElementCount > 0"
      );
      if (!ok && !rendererMounted) {
        const msg =
          '前端已加载但未渲染出内容（白屏）。常见原因：\n' +
          '• web/dist 资源未正确打包（index.html / assets 缺失或 404）\n' +
          '• 前端脚本运行时报错（详见 backend.log 中的 [console.error] / page-error）\n' +
          `日志见：${backendLogPath}`;
        log('[electron] 前端白屏校验未通过：#root 无子节点。');
        dialog.showErrorBox('MY_AGENT 前端白屏', msg);
      }
    } catch (_) {
      /* 页面已跳转/销毁，忽略 */
    }
  }, 8000);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#1a1208',
    title: 'MY_AGENT',
    icon: TRAY_ICON,
    show: !HIDDEN_LAUNCH, // 纯后台守护模式：--hidden 启动不弹主窗，只留托盘+通知
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, 'preload.cjs'),
    },
  });

  // 加载窗口先显示状态页
  mainWindow.loadURL(splashHtml());

  // 兜底：6s 内没 ready-to-show 也强制显示，避免窗口永远隐藏
  const forceShow = setTimeout(() => {
    if (mainWindow && !mainWindow.isVisible()) mainWindow.show();
  }, 6000);
  mainWindow.once('ready-to-show', () => {
    clearTimeout(forceShow);
    if (!HIDDEN_LAUNCH) mainWindow.show();
  });

  // 关窗 → 隐藏而不是退出（托盘常驻：关窗口不退出，托盘图标右键可退出）
  mainWindow.on('close', (e) => {
    if (!app.isQuiting) {
      e.preventDefault();
      mainWindow.hide();
      return;
    }
  });

  // 外部链接（如文档、OAuth）用系统浏览器打开
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // 若真实前端加载失败，在窗口里显示错误，而不是白屏
  mainWindow.webContents.on('did-fail-load', (_e, code, desc, url) => {
    if (DEV) return; // 开发态失败是常态，忽略
    log(`[electron] 前端加载失败: ${desc} (${url})`);
    setStatus('前端加载失败：' + desc + ' —— 请查看 backend.log');
  });

  // 渲染进程运行时错误 / 控制台错误：全部记录，避免「白屏但无日志」无从排查
  mainWindow.webContents.on('page-error', (err) => {
    log('[electron] 渲染进程异常(page-error): ' + (err && err.stack ? err.stack : err));
  });
  mainWindow.webContents.on('console', (_e, level, msg) => {
    if (level === 'error' || level === 'warning') {
      log(`[electron][console.${level}] ${msg}`);
    }
  });

  setThumbarButtons(mainWindow);

  // 主窗启用 Windows 11 mica 原生材质（系统主题跟随由 initThemeSync 处理）
  applyWindowMaterial(mainWindow, 'mica');

  return mainWindow;
}

// ---------------------------------------------------------------------------
// 托盘常驻 + 全局快捷键 + 开机自启 + 协议唤起
// ---------------------------------------------------------------------------
let tray = null;

function showMainWindow() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  if (tray) return;
  try {
    tray = new Tray(TRAY_ICON);
  } catch (e) {
    log('[electron] 创建托盘失败: ' + e);
    return;
  }
  tray.setToolTip('MY_AGENT — 电脑管家助手');
  const menu = Menu.buildFromTemplate([
    { label: '打开 MY_AGENT', click: showMainWindow },
    { label: '快速提问（边缘条）', click: () => createEdgeWindow() },
    { label: '吉祥物（开关）', click: () => createMascotWindow() },
    {
      label: '重启后端',
      click: () => {
        killBackend();
        startBackend(resolveBackendRoot());
        showMainWindow();
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        app.isQuiting = true;
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);
  // 双击托盘图标 = 打开主窗
  tray.on('double-click', showMainWindow);
}

function registerGlobalShortcut() {
  // Ctrl+Space：弹出/聚焦主对话窗（frameless 悬浮窗留作后续，先用主窗）
  const ok = globalShortcut.register('CommandOrControl+Space', () => {
    if (!mainWindow) return;
    if (mainWindow.isVisible()) {
      mainWindow.close(); // 再次按下则收起到托盘
    } else {
      showMainWindow();
    }
  });
  if (!ok) log('[electron] 全局快捷键 Ctrl+Space 注册失败（可能被其他程序占用）');

  // Ctrl+Shift+Space：唤起 / 收起常驻边缘条（迷你对话）
  const okEdge = globalShortcut.register('CommandOrControl+Shift+Space', () => {
    createEdgeWindow();
  });
  if (!okEdge) log('[electron] 全局快捷键 Ctrl+Shift+Space 注册失败（可能被占用）');

  // Ctrl+Shift+M：唤起 / 收起吉祥物浮窗
  const okMascot = globalShortcut.register('CommandOrControl+Shift+M', () => {
    createMascotWindow();
  });
  if (!okMascot) log('[electron] 全局快捷键 Ctrl+Shift+M 注册失败（可能被占用）');
}

function enableAutoLaunch() {
  // 原生开机自启，不碰注册表、不引第三方
  try {
    app.setLoginItemSettings({
      openAtLogin: true,
      args: ['--hidden'],
    });
  } catch (e) {
    log('[electron] 设置开机自启失败: ' + e);
  }
}

function registerProtocol() {
  // myagent://chat?q=... 从外部唤起并传参
  try {
    app.setAsDefaultProtocolClient('myagent');
  } catch (e) {
    log('[electron] 注册 myagent:// 协议失败: ' + e);
  }
}

// ---------------------------------------------------------------------------
// 剪贴板历史（持久化到 userData；默认不记录，前端显式开启 watch 才积累）
// ---------------------------------------------------------------------------
const CLIP_HISTORY_MAX = 50;
let clipboardHistory = [];

function clipHistoryPath() {
  return path.join(app.getPath('userData'), 'clipboard-history.json');
}

function loadClipboardHistory() {
  try {
    const p = clipHistoryPath();
    if (fs.existsSync(p)) {
      const arr = JSON.parse(fs.readFileSync(p, 'utf-8'));
      if (Array.isArray(arr)) return arr;
    }
  } catch (_) {
    /* 损坏则忽略，从头开始 */
  }
  return [];
}

function saveClipboardHistory() {
  try {
    fs.writeFileSync(clipHistoryPath(), JSON.stringify(clipboardHistory.slice(0, CLIP_HISTORY_MAX)));
  } catch (_) {
    /* 忽略写入失败 */
  }
}

function pushClipboardHistory(text) {
  text = (text || '').trim();
  if (!text) return;
  if (clipboardHistory[0] && clipboardHistory[0].text === text) return; // 与最新相同则跳过
  clipboardHistory.unshift({ text: text, ts: Date.now() });
  if (clipboardHistory.length > CLIP_HISTORY_MAX) clipboardHistory.length = CLIP_HISTORY_MAX;
  saveClipboardHistory();
}

// ---------------------------------------------------------------------------
// 常驻边缘条（空间嵌入：永远在手的迷你对话窗，不抢焦点、Esc/失焦收起）
// ---------------------------------------------------------------------------
let edgeWindow = null;

// 吉祥物浮窗（独立透明窗：30 帧循环动画 + 状态气泡 + 模型输出气泡）
let mascotWindow = null;

function createEdgeWindow() {
  if (edgeWindow) {
    if (edgeWindow.isVisible()) edgeWindow.hide();
    else {
      edgeWindow.show();
      edgeWindow.focus();
    }
    return;
  }
  edgeWindow = new BrowserWindow({
    width: 560,
    height: 72,
    minWidth: 360,
    frame: false,
    transparent: true,
    backgroundColor: '#1a1208', // acrylic 需要底色托底
    alwaysOnTop: true,
    resizable: false,
    minimizable: false,
    maximizable: false,
    skipTaskbar: true,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, 'preload.cjs'),
    },
  });
  // 边缘条用 acrylic 磨砂（Win11）；透明窗 + 底色，磨砂可见
  applyWindowMaterial(edgeWindow, 'acrylic');
  // 初始底色跟随系统主题（浅色模式不再透出暗底）
  try { edgeWindow.setBackgroundColor(nativeTheme.shouldUseDarkColors ? '#1a1208' : '#f3e9dc'); } catch (_) {}
  edgeWindow.loadFile(path.join(__dirname, 'edge.html'), { query: { base: BASE_URL } });
  edgeWindow.once('ready-to-show', () => {
    const { width } = edgeWindow.getBounds();
    const { width: sw } = screen.getPrimaryDisplay().workAreaSize;
    edgeWindow.setPosition(Math.round((sw - width) / 2), 8);
    edgeWindow.show();
  });
  edgeWindow.on('closed', () => {
    edgeWindow = null;
  });
}

// ---------------------------------------------------------------------------
// 吉祥物浮窗（身份嵌入：独立的透明小窗，常驻桌面一角，显示状态+模型输出）
// ---------------------------------------------------------------------------
function createMascotWindow() {
  if (mascotWindow) {
    if (mascotWindow.isVisible()) mascotWindow.hide();
    else {
      mascotWindow.show();
      mascotWindow.focus();
    }
    return;
  }
  mascotWindow = new BrowserWindow({
    width: 280,
    height: 380,
    minWidth: 220,
    minHeight: 300,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    alwaysOnTop: true,
    resizable: true,
    minimizable: false,
    maximizable: false,
    skipTaskbar: true,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, 'preload.cjs'),
    },
  });
  // 纯透明窗：不加 mica/acrylic（会铺底），保持镂空悬浮
  mascotWindow.loadFile(path.join(__dirname, 'mascot.html'), { query: { base: BASE_URL } });
  mascotWindow.once('ready-to-show', () => {
    const { width, height } = mascotWindow.getBounds();
    const { width: sw, height: sh } = screen.getPrimaryDisplay().workAreaSize;
    // 默认右下角
    mascotWindow.setPosition(Math.round(sw - width - 24), Math.round(sh - height - 24));
    mascotWindow.show();
  });
  mascotWindow.on('closed', () => {
    mascotWindow = null;
  });
}

// ---------------------------------------------------------------------------
// 任务栏缩略图工具条（身份嵌入：悬停任务栏图标即出快捷操作）
// ---------------------------------------------------------------------------
function setThumbarButtons(win) {
  if (!win) return;
  try {
    const icon = nativeImage.createFromPath(TRAY_ICON);
    win.setThumbarButtons([
      { tooltip: '快速提问（边缘条）', icon: icon, click: () => createEdgeWindow() },
      { tooltip: '打开主窗口', icon: icon, click: () => showMainWindow() },
      {
        tooltip: '重启后端',
        icon: icon,
        click: () => {
          killBackend();
          startBackend(resolveBackendRoot());
          showMainWindow();
        },
      },
    ]);
  } catch (e) {
    log('[electron] 设置任务栏缩略图工具条失败: ' + e);
  }
}

// ---------------------------------------------------------------------------
// 外部事件排队：前端渲染进程挂载并注册监听前，事件先入队，待 renderer-ready 补发
// （避免「首实例启动即带 --ask-path」之类早于前端监听的丢失）
// ---------------------------------------------------------------------------
let pendingExternalEvents = [];
let rendererReady = false;
function sendOrQueue(channel, payload) {
  pendingExternalEvents.push({ channel, payload });
  // 渲染进程就绪后才真正下发；否则排队等 ackReady 补发（避免监听未挂载就清空）
  if (rendererReady) flushPendingExternal();
}
function flushPendingExternal() {
  if (!mainWindow || !mainWindow.webContents) return;
  if (pendingExternalEvents.length === 0) {
    rendererReady = true; // 无积压也标记就绪，后续事件可立即下发
    return;
  }
  const list = pendingExternalEvents;
  pendingExternalEvents = [];
  rendererReady = true;
  for (const e of list) {
    try {
      mainWindow.webContents.send(e.channel, e.payload);
    } catch (_) {
      /* 发送失败忽略 */
    }
  }
}

// 外部通过 myagent:// 协议唤起时，把参数投到前端
function handleProtocolArg(arg) {
  if (!arg || !arg.startsWith('myagent://')) return;
  log('[electron] 收到协议唤起: ' + arg);
  // 也支持 myagent://open?path=... （与 --ask-path 等价，仅作兜底）
  try {
    const u = new URL(arg);
    if (u.host === 'open' && u.searchParams.has('path')) {
      dispatchOpenPath(decodeURIComponent(u.searchParams.get('path')));
      return;
    }
  } catch (_) {}
  sendOrQueue('myagent:protocol', arg);
}

// 解析命令行中 --ask-path <path>（资源管理器右键「问助手」走这条路，避免 URL 编码歧义）
function extractAskPath(argv) {
  const i = argv.indexOf('--ask-path');
  if (i >= 0 && argv[i + 1]) return argv[i + 1];
  return null;
}

// 把「外部传入的文件/文件夹路径」整理后发给前端（stat 判定是否目录）
function dispatchOpenPath(p) {
  if (!p) return;
  let isDir = false;
  try {
    isDir = fs.statSync(p).isDirectory();
  } catch (_) {
    /* 路径不存在也不阻塞，照常带入 */
  }
  log('[electron] 外部带入路径: ' + p + (isDir ? ' (目录)' : ' (文件)'));
  sendOrQueue('myagent:open-path', { path: p, isDir });
}

// ---------------------------------------------------------------------------
// 资源管理器右键「问助手」（数据嵌入：对任意文件/文件夹右键直接发上下文）
// 写 HKCU\Software\Classes（HKEY_CLASSES_ROOT 的每用户覆盖层，免管理员）。
// ---------------------------------------------------------------------------
function registerExplorerContextMenu() {
  const exe = process.execPath; // 当前 exe 全路径（安装后指向安装目录）
  const cmd = `"${exe}" --ask-path "%1"`;
  const keys = [
    ['HKCU\\Software\\Classes\\*\\shell\\MyAgent', null, '问助手'],
    ['HKCU\\Software\\Classes\\*\\shell\\MyAgent', 'Icon', exe],
    ['HKCU\\Software\\Classes\\*\\shell\\MyAgent\\command', null, cmd],
    ['HKCU\\Software\\Classes\\Directory\\shell\\MyAgent', null, '问助手'],
    ['HKCU\\Software\\Classes\\Directory\\shell\\MyAgent', 'Icon', exe],
    ['HKCU\\Software\\Classes\\Directory\\shell\\MyAgent\\command', null, cmd],
    ['HKCU\\Software\\Classes\\Directory\\Background\\shell\\MyAgent', null, '问助手'],
    ['HKCU\\Software\\Classes\\Directory\\Background\\shell\\MyAgent', 'Icon', exe],
    ['HKCU\\Software\\Classes\\Directory\\Background\\shell\\MyAgent\\command', null, cmd],
  ];
  for (const [key, valName, val] of keys) {
    const args = ['add', key];
    if (valName == null) args.push('/ve');
    else args.push('/v', valName);
    args.push('/d', val, '/f');
    try {
      spawnSync('reg', args, { stdio: 'ignore', windowsHide: true });
    } catch (_) {
      /* reg 不可用则跳过（非 Windows / 权限异常） */
    }
  }
  log('[electron] 已注册资源管理器右键「问助手」（HKCU，免管理员）');
}

// 卸载/退出时清除右键项（HKCU，仅本用户），保持注册表干净
function unregisterExplorerContextMenu() {
  const keys = [
    'HKCU\\Software\\Classes\\*\\shell\\MyAgent',
    'HKCU\\Software\\Classes\\Directory\\shell\\MyAgent',
    'HKCU\\Software\\Classes\\Directory\\Background\\shell\\MyAgent',
  ];
  for (const k of keys) {
    try {
      spawnSync('reg', ['delete', k, '/f'], { stdio: 'ignore', windowsHide: true });
    } catch (_) {
      /* 忽略 */
    }
  }
}
// 单实例锁：防止多开
const gotLock = app.requestSingleInstanceLock();

if (!gotLock) {
  // 已有实例在运行：明确告知，而不是静默退出
  app.whenReady().then(() => {
    dialog.showErrorBox(
      'MY_AGENT 已在运行',
      '检测到另一个 MY_AGENT 实例。请先关闭它再启动（任务管理器结束 MY_AGENT.exe / python 进程）。'
    );
    app.quit();
  });
} else {
  app.on('second-instance', (e, argv) => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
    // 第二实例的命令行里可能带 myagent:// 协议参数或 --ask-path 路径
    for (const a of argv) {
      if (a.startsWith('myagent://')) handleProtocolArg(a);
    }
    const ap = extractAskPath(argv);
    if (ap) dispatchOpenPath(ap);
  });

  // macOS 协议唤起走 open-url
  app.on('open-url', (e, url) => {
    e.preventDefault();
    handleProtocolArg(url);
  });

  app.whenReady().then(async () => {
    backendLogPath = path.join(app.getPath('userData'), 'backend.log');
    clipboardHistory = loadClipboardHistory();
    const root = resolveBackendRoot();
    log(`[electron] 启动。backendRoot=${root}  packaged=${app.isPackaged}`);

    // 工作区会话：调用系统资源管理器选择工作目录
    ipcMain.handle('dialog:selectFolder', async () => {
      if (!mainWindow) return null;
      try {
        const result = await dialog.showOpenDialog(mainWindow, {
          title: '选择工作区目录',
          properties: ['openDirectory', 'createDirectory'],
        });
        if (result.canceled || !result.filePaths || !result.filePaths.length) {
          return null;
        }
        return result.filePaths[0];
      } catch (e) {
        log('[electron] dialog:selectFolder 失败: ' + e);
        return null;
      }
    });

    // 删除确认目录（2026-08-08）：多选目录（可按住 Ctrl/Shift 连续多选）
    ipcMain.handle('dialog:selectFolders', async () => {
      if (!mainWindow) return [];
      try {
        const result = await dialog.showOpenDialog(mainWindow, {
          title: '选择需要删除确认的目录（可多选）',
          properties: ['openDirectory', 'multiSelections'],
        });
        if (result.canceled || !result.filePaths || !result.filePaths.length) {
          return [];
        }
        return result.filePaths;
      } catch (e) {
        log('[electron] dialog:selectFolders 失败: ' + e);
        return [];
      }
    });

    // 系统通知：主动推送
    const { Notification: ElNotification } = require('electron');
    ipcMain.handle('app:notify', async (_e, { title, body }) => {
      try {
        if (!ElNotification.isSupported()) {
          log('[electron] 系统不支持通知');
          return false;
        }
        const n = new ElNotification({ title: title || 'MY_AGENT', body: body || '', icon: TRAY_ICON });
        n.show();
        return true;
      } catch (e) {
        log('[electron] 通知失败: ' + e);
        return false;
      }
    });

    // 剪贴板读取（前端主动取一次）
    ipcMain.handle('clipboard:read', () => {
      try {
        return clipboard.readText();
      } catch (_) {
        return '';
      }
    });

    // 剪贴板文件读取（Ctrl+V 粘贴上传）：解析 CF_HDROP 文件列表；图片转 data URL，非图片返回路径
    ipcMain.handle('clipboard:readFiles', () => {
      try {
        const result = [];
        // 1) 优先解析资源管理器复制的文件/图片（带真实路径）
        const drop = clipboard.read('CF_HDROP');
        if (drop && drop.length) {
          for (const p of parseDropFiles(drop)) {
            try {
              const stat = fs.statSync(p);
              const ext = (path.extname(p) || '').toLowerCase().replace(/^\./, '');
              const isImage = IMAGE_EXT.has(ext);
              const item = {
                name: path.basename(p),
                ext,
                isImage,
                size: stat.size,
                path: p,
              };
              if (isImage) {
                const mime = MIME_BY_EXT[ext] || 'application/octet-stream';
                const data = fs.readFileSync(p);
                item.dataUrl = 'data:' + mime + ';base64,' + data.toString('base64');
              }
              result.push(item);
            } catch (e) {
              log('[electron] clipboard:readFiles 解析单文件失败: ' + e);
            }
          }
          if (result.length) return result;
        }
        // 2) 兜底：剪贴板中的纯图片（截图 / 浏览器复制图片），无文件路径
        const img = clipboard.readImage();
        if (img && !img.isEmpty()) {
          const png = img.toPNG();
          if (png && png.length) {
            result.push({
              name: 'clipboard-image.png',
              ext: 'png',
              isImage: true,
              size: png.length,
              path: '',
              dataUrl: 'data:image/png;base64,' + png.toString('base64'),
            });
          }
        }
        return result;
      } catch (e) {
        log('[electron] clipboard:readFiles 失败: ' + e);
        return [];
      }
    });

    // 剪贴板历史（需先 setClipboardWatch(true) 才会积累；持久化到 userData）
    ipcMain.handle('clipboard:history', () => clipboardHistory.slice());
    ipcMain.handle('clipboard:clear', () => {
      clipboardHistory = [];
      try { fs.unlinkSync(clipHistoryPath()); } catch (_) {}
      return true;
    });

    // 前端请求显示/聚焦主窗
    ipcMain.handle('window:show', () => {
      showMainWindow();
      return true;
    });

    // 渲染进程就绪：把排队中的外部事件（协议/路径）补发给前端
    ipcMain.handle('app:renderer-ready', () => {
      rendererMounted = true;
      flushPendingExternal();
      // 同时把当前系统主题推给前端（前端此时已注册 onThemeChange 监听）
      if (process.platform === 'win32') {
        try {
          if (mainWindow && mainWindow.webContents && !mainWindow.webContents.isDestroyed()) {
            mainWindow.webContents.send('theme:change', nativeTheme.shouldUseDarkColors);
          }
        } catch (_) {}
      }
      return true;
    });

    // 前端主动问当前系统主题（兜底同步）
    ipcMain.handle('app:get-theme-dark', () => {
      if (process.platform !== 'win32') return true; // 非 Windows 默认深色
      try {
        return nativeTheme.shouldUseDarkColors;
      } catch (_) {
        return true;
      }
    });

    // 吉祥物浮窗：前端把派生的状态/输出推给主进程，再由主进程转发到吉祥物窗
    ipcMain.handle('mascot:push', (_e, payload) => {
      if (mascotWindow && !mascotWindow.isDestroyed() && mascotWindow.webContents) {
        try {
          mascotWindow.webContents.send('mascot:update', payload);
        } catch (_) {
          /* 窗口还没 ready，忽略 */
        }
      }
      return true;
    });
    ipcMain.handle('mascot:hide', () => {
      if (mascotWindow && mascotWindow.isVisible()) {
        try { mascotWindow.hide(); } catch (_) {}
      }
      return true;
    });

    // 读取本地文件（拖拽文件进窗口时用）
    const { readFile: fsReadFile } = require('fs/promises');
    ipcMain.handle('app:readFile', async (_e, filePath) => {
      try {
        if (!filePath || typeof filePath !== 'string') return { error: 'invalid path' };
        // 基本安全：仅读文件（不列目录），且大小上限 5MB
        const stat = fs.statSync(filePath);
        if (stat.isDirectory()) return { error: 'is directory' };
        if (stat.size > 5 * 1024 * 1024) return { error: 'too large', size: stat.size };
        const buf = await fsReadFile(filePath);
        return { name: path.basename(filePath), size: stat.size, text: buf.toString('utf-8') };
      } catch (e) {
        return { error: String(e) };
      }
    });

    // 剪贴板监听（去抖 + 默认关闭，隐私第一；前端显式开启才跑）
    let clipboardTimer = null;
    let lastClip = '';
    ipcMain.handle('clipboard:watch', (_e, on) => {
      if (on && !clipboardTimer) {
        lastClip = clipboard.readText();
        clipboardTimer = setInterval(() => {
          const cur = clipboard.readText();
          if (cur && cur !== lastClip) {
            lastClip = cur;
            pushClipboardHistory(cur);
            if (mainWindow && mainWindow.webContents) {
              mainWindow.webContents.send('clipboard:changed', cur);
            }
          }
        }, 800);
        return true;
      }
      if (!on && clipboardTimer) {
        clearInterval(clipboardTimer);
        clipboardTimer = null;
      }
      return true;
    });

    // 全局划词即问（Phase3，默认关闭；仅用户显式开启才加载真·Hook）
    let textCaptureOn = false;
    ipcMain.handle('textcapture:set', (_e, on) => {
      if (on === textCaptureOn) return true;
      try {
        const tc = require('./textcapture');
        if (on) {
          const ok = tc.enableTextCapture((text) => {
            if (mainWindow && mainWindow.webContents) {
              // 推给前端：弹小窗展示选中文本（前端决定如何呈现/发送）
              mainWindow.webContents.send('textcapture:text', text);
              showMainWindow();
            }
          });
          textCaptureOn = ok;
          log('[electron] 划词Hook开启=' + ok + (ok ? '' : '（已降级到剪贴板监听，可在设置开启）'));
          // 降级兜底：Hook 没装上则改用剪贴板轮询监听
          if (!ok) {
            ipcMain.handle('clipboard:watch', (_e, w) => {
              if (w && !clipboardTimer) {
                lastClip = clipboard.readText();
                clipboardTimer = setInterval(() => {
                  const cur = clipboard.readText();
                  if (cur && cur !== lastClip) {
                    lastClip = cur;
                    mainWindow?.webContents.send('textcapture:text', cur);
                  }
                }, 800);
              } else if (!w && clipboardTimer) {
                clearInterval(clipboardTimer);
                clipboardTimer = null;
              }
              return true;
            });
          }
          return ok;
        } else {
          tc.disableTextCapture();
          textCaptureOn = false;
          return true;
        }
      } catch (e) {
        log('[electron] 划词模块异常，已安全降级: ' + e);
        return false;
      }
    });

    createWindow();
    initThemeSync(); // 系统主题跟随（需在窗创建后，监听 nativeTheme）
    createTray();
    registerGlobalShortcut();
    enableAutoLaunch();
    registerProtocol();
    registerExplorerContextMenu(); // 资源管理器右键「问助手」（HKCU，免管理员）
    // Windows：首实例启动时的协议参数在 process.argv
    for (const a of process.argv) {
      if (a.startsWith('myagent://')) handleProtocolArg(a);
    }
    // 首实例也可能被资源管理器右键唤起（带 --ask-path）
    const ap0 = extractAskPath(process.argv);
    if (ap0) dispatchOpenPath(ap0);
    setStatus('正在检测后端…');

    const healthUp = await checkOnce();
    const frontendOk = healthUp ? await servesFrontend() : false;

    if (healthUp && frontendOk) {
      log('[electron] 检测到可用后端（含前端），复用现有实例。');
      setStatus('复用已有后端，加载前端…');
    } else {
      if (healthUp && !frontendOk) {
        const free = await findFreePort(BACKEND_PORT + 1);
        setPort(free);
        log(`[electron] 端口 ${BACKEND_PORT} 被旧后端占用且无前端，改用 ${free}`);
        setStatus(`端口冲突，改用 ${free} 启动后端…`);
      }
      setStatus('正在启动后端（首次约 3-8 秒）…');
      startBackend(root);
    }

    waitForBackend(
      async () => {
        log('[electron] 后端就绪，加载前端。');
        setStatus('后端就绪，加载前端…');
        const target = DEV ? 'http://localhost:5173' : BASE_URL;
        log(`[electron] 加载前端: ${target}`);

        // 安装包升级后，Electron 的 HTTP 缓存可能还残留旧 index.html，
        // 导致它引用已不存在的旧 assets → 404 → 白屏。每次加载前端前先清缓存。
        if (mainWindow) {
          try {
            await mainWindow.webContents.session.clearCache();
            log('[electron] 已清除 renderer HTTP 缓存（避免升级后命中旧 index.html）');
          } catch (e) {
            log('[electron] 清除缓存失败（非致命）: ' + e.message);
          }
          mainWindow.loadURL(target, { extraHeaders: 'pragma: no-cache\n' });
        }
        verifyFrontendMounted();
        createMascotWindow(); // 后端就绪后拉起吉祥物浮窗（默认右下角常驻）
      },
      () => {
        const msg =
          '后端在 30 秒内未就绪。常见原因：\n' +
          '• 系统 Python 未安装 fastapi/uvicorn 等依赖\n' +
          '• 端口被占用且无法启动新后端\n' +
          `日志见：${backendLogPath}`;
        log('[electron] 后端超时。' + msg);
        setStatus('后端启动失败（详见弹窗与 backend.log）');
        dialog.showErrorBox('MY_AGENT 后端启动失败', msg);
        killBackend();
        app.quit();
      }
    );
  });

  app.on('window-all-closed', () => {
    // 托盘常驻：关所有窗口不退出；仅托盘右键"退出"或 before-quit 才真正退出
  });

  app.on('before-quit', () => {
    appQuitting = true; // 抑制退出过程中的后端自愈重启
    app.isQuiting = true;
    unregisterExplorerContextMenu(); // 清除右键项，保持注册表干净
    killBackend();
    try { globalShortcut.unregisterAll(); } catch (_) {}
    if (tray) { try { tray.destroy(); } catch (_) {} tray = null; }
  });
  app.on('quit', () => killBackend());
}
