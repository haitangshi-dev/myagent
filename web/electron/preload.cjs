'use strict';

/**
 * MY_AGENT — Electron 预加载脚本（sandbox 模式）。
 *
 * 沙盒模式下 preload 仅可使用 electron 提供的 contextBridge / ipcRenderer，
 * 不能访问 Node 的 fs 等模块。这里只向渲染进程暴露受控的桥接 API。
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // 是否为 Electron 壳（区分浏览器直接打开）
  isElectron: true,

  // 调用系统资源管理器选择目录，返回路径字符串或 null
  selectFolder: () => ipcRenderer.invoke('dialog:selectFolder'),

  // 多选目录（删除确认目录配置用）：返回路径字符串数组
  selectFolders: () => ipcRenderer.invoke('dialog:selectFolders'),

  // 系统通知：主动推送（定时任务到点 / agent 提醒）
  notify: (title, body) => ipcRenderer.invoke('app:notify', { title, body }),

  // 剪贴板读取（轮询监听在 main 进程做，这里供前端主动取一次）
  readClipboard: () => ipcRenderer.invoke('clipboard:read'),

  // 读取剪贴板中的文件 / 图片（Ctrl+V 粘贴上传）：返回 [{name,ext,isImage,size,path,dataUrl?}]，无则返回 []
  readClipboardFiles: () => ipcRenderer.invoke('clipboard:readFiles'),

  // 读取本地文件（拖拽文件进窗口时用，返回 {name,size,text} 或 {error}）
  readFile: (filePath) => ipcRenderer.invoke('app:readFile', filePath),

  // 监听 myagent:// 协议唤起（外部传参），回调 arg 字符串
  onProtocol: (cb) => {
    const handler = (_e, arg) => cb(arg);
    ipcRenderer.on('myagent:protocol', handler);
    return () => ipcRenderer.removeListener('myagent:protocol', handler);
  },

  // 主进程请求前端聚焦/显示主窗
  showMainWindow: () => ipcRenderer.invoke('window:show'),

  // 资源管理器右键「问助手」/ 其他外部传路径：回调 {path, isDir}
  onOpenPath: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on('myagent:open-path', handler);
    return () => ipcRenderer.removeListener('myagent:open-path', handler);
  },

  // 渲染进程就绪：主进程据此把排队中的外部事件（协议/路径）补发给前端
  ackReady: () => ipcRenderer.invoke('app:renderer-ready'),

  // 监听剪贴板变化（main 进程轮询后推送）
  onClipboardChanged: (cb) => {
    const handler = (_e, text) => cb(text);
    ipcRenderer.on('clipboard:changed', handler);
    return () => ipcRenderer.removeListener('clipboard:changed', handler);
  },

  // 开启/关闭剪贴板监听（默认关，隐私优先）
  setClipboardWatch: (on) => ipcRenderer.invoke('clipboard:watch', on),

  // 剪贴板历史（需先开启 setClipboardWatch 才会积累；持久化到 userData）
  getClipboardHistory: () => ipcRenderer.invoke('clipboard:history'),
  clearClipboardHistory: () => ipcRenderer.invoke('clipboard:clear'),

  // 全局划词即问：开启/关闭（默认关，内部会尝试真·Hook，失败自动降级剪贴板）
  setTextCapture: (on) => ipcRenderer.invoke('textcapture:set', on),

  // 监听划词/选中文本捕获（主进程推送）
  onTextCapture: (cb) => {
    const handler = (_e, text) => cb(text);
    ipcRenderer.on('textcapture:text', handler);
    return () => ipcRenderer.removeListener('textcapture:text', handler);
  },

  // 系统主题跟随：主进程推送 shouldUseDarkColors（true=深色）
  onThemeChange: (cb) => {
    const handler = (_e, dark) => cb(dark);
    ipcRenderer.on('theme:change', handler);
    return () => ipcRenderer.removeListener('theme:change', handler);
  },

  // 主动询问当前主题（兜底同步一次）
  getThemeDark: () => ipcRenderer.invoke('app:get-theme-dark'),

  // 吉祥物浮窗：前端把派生出的状态/输出推给主进程（主进程再转发到吉祥物窗）
  pushMascot: (state, text) => ipcRenderer.invoke('mascot:push', { state, text }),

  // 吉祥物浮窗：监听主进程转发的状态更新 { state, text }
  onMascotUpdate: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on('mascot:update', handler);
    return () => ipcRenderer.removeListener('mascot:update', handler);
  },

  // 吉祥物浮窗：隐藏窗口
  hideMascot: () => ipcRenderer.invoke('mascot:hide'),
});
