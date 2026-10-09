'use strict';

/**
 * MY_AGENT — 全局划词即问（高风险模块，隔离封装）。
 *
 * 设计铁律（来自用户"遇麻烦砍功能 + 不引脆弱依赖"的偏好）：
 *   1. 本模块绝不在启动时自动加载；仅在用户于设置里显式开启「全局划词」后，
 *      由 main.cjs 调用 enableTextCapture() 才 require('uiohook-napi')。
 *   2. require / 运行任何一步失败 → 立即降级（disableTextCapture 返回 false），
 *      绝不抛出、绝不影响主窗口/托盘/后端。
 *   3. 监听器回调仅把选中文本通过 IPC 推给前端，不直接操作 DOM。
 *   4. 仅在 Windows/macOS 这类需要 hook 的平台生效；Linux 可利用 PRIMARY 选区。
 *
 * 关于 Windows 的现实约束：
 *   系统没有"鼠标选中文本"的系统缓冲区，必须 hook 鼠标/键盘或读剪贴板。
 *   因此本模块默认策略是"监听 Ctrl+C 复制动作 + 鼠标弹起时读剪贴板"，
 *   命中则推送最近复制/选区的纯文本给前端弹小窗。
 *   如果 uiohook-napi 装不上或被杀软拦截，调用方应改用 preload 暴露的
 *   setClipboardWatch(true)（轮询剪贴板）作为零 hook 的等价降级。
 */

let uiohook = null;
let started = false;
let onText = null;

function safeStart(getText) {
  try {
    uiohook = require('uiohook-napi');
  } catch (e) {
    console.log('[textcapture] uiohook-napi 不可用（跳过真·划词）:', e.message);
    return false;
  }
  try {
    onText = getText;
    uiohook.on('keydown', (e) => {
      // Ctrl+C / Cmd+C：复制后把剪贴板内容推送（去抖由调用方处理）
      if ((e.ctrlKey || e.metaKey) && (e.keycode === 67 || e.keycode === 99)) {
        setTimeout(() => pushCaptured(), 60);
      }
    });
    uiohook.on('mouseup', () => {
      // 鼠标松开：尝试读剪贴板（非破坏性，仅读）
      pushCaptured();
    });
    uiohook.start();
    started = true;
    return true;
  } catch (e) {
    console.log('[textcapture] 启动失败，降级:', e.message);
    try { uiohook?.stop(); } catch (_) {}
    uiohook = null;
    started = false;
    return false;
  }
}

function pushCaptured() {
  if (!started || !onText) return;
  try {
    const { clipboard } = require('electron');
    const t = clipboard.readText();
    if (t && t.trim()) onText(t.trim());
  } catch (_) {}
}

function enableTextCapture(getText) {
  if (started) return true;
  return safeStart(getText);
}

function disableTextCapture() {
  if (!started) return true;
  try {
    uiohook?.stop();
  } catch (_) {}
  uiohook = null;
  started = false;
  return true;
}

module.exports = { enableTextCapture, disableTextCapture };
