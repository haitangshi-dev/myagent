# MY_AGENT — Web 前端

React + Vite + TypeScript 的桌面 Agent 前端，对接 `server/api.py` 的 SSE 后端。
设计语言：石墨墨黑底 + 克制翡翠绿 + 液态玻璃面板（Geist 字体，无 emoji）。

## 技术栈
- Vite 5 / React 18 / TypeScript
- Tailwind CSS 3（透过 CSS 变量定义设计 token）
- Zustand 4（会话 + UI 状态，localStorage 持久化）
- Framer Motion 11（ease-out-expo 过渡）
- react-markdown + remark-gfm（助手正文）
- lucide-react（图标）/ @fontsource/geist（字体）

## 目录结构
- `src/lib`：`types`（后端契约）/ `sse`（fetch + ReadableStream 解析器）/ `api`（providers/health/tools/approve）/ `format`
- `src/store`：`useChatStore`（会话/消息/流式/SSE 归约/审批）、`useUIStore`（provider/健康/面板/命令面板）
- `src/components/layout`：`TopBar` / `Sidebar` / `RightPanel` / `StatusBar` / `CommandPalette`
- `src/components/chat`：`ChatView` / `MessageBubble` / `ReasoningBlock` / `ToolCard` / `ApprovalCard` / `Composer` / `EmptyState`

## 开发
```bash
cd web
npm install
npm run dev        # http://localhost:5173 ，/api 代理到 :8000
```

## 生产构建 + 运行
```bash
cd web
npm run build      # 产物输出到 web/dist
cd ..
python start.py    # 拉起后端并用浏览器打开 Web（默认 gui 模式）
# 或仅后端： python start.py --mode backend ，自行打开 http://localhost:8000
```
> 后端 `server/api.py` 将 `web/dist` 挂载在 `/`，构建后即被托管，无需额外静态服务器。

## 桌面端（Electron 壳，不依赖浏览器）
把现有 React 前端包进 Electron 主进程，产出一个双击即跑的 `.exe` 桌面程序（与 ClawX 同技术栈：Electron + React + Vite + Tailwind + Zustand）。**前端代码零改动**，Electron 只是「浏览器窗口」的替代品，并负责自动拉起本地 Python 后端。

主进程：`electron/main.cjs`（`main` 字段指向它）。行为：
1. 启动本地后端 `python main.py --host 127.0.0.1 --port 8000`（`resolveBackendRoot()` 在开发态取 `web/` 上一级项目根，打包态取 `process.resourcesPath/backend`）。
2. 每 500ms 轮询 `/api/health`，最多 30s；后端已手动起（端口占用）则跳过启动、直接复用。
3. 就绪后 `BrowserWindow` 加载 `http://127.0.0.1:8000`（即 React 前端，`dist/` 由后端托管）。
4. 应用退出（单实例锁 + `before-quit`/`quit`）用 `taskkill /F /T` 清掉整棵后端进程树。

### 开发态
```bash
cd web
npm install
npm run dev          # 1) 先起 Vite dev server :5173（/api 代理到 :8000）
npm run electron     # 2) Electron 加载打包态地址 :8000（需后端已起）
# 或热更： npm run electron:hot  → ELECTRON_DEV=1，Electron 直接加载 :5173
```
> 开发态 Electron 不内置后端，需另开一个终端 `python main.py --port 8000` 或用 `start.py`。

### 打包（Windows NSIS 安装包）
```bash
cd web
npm run dist:win     # = npm run build && electron-builder --win --x64
```
- 产物：`web/release/MY_AGENT-Setup-${version}.exe`（含 `win-unpacked/resources/backend/` 内嵌后端：main.py / config / agents / providers / server / scripts）。
- `appId: com.myagent.desktop`，`productName: MY_AGENT`，NSIS 非一键安装、可选安装目录。
- 镜像：`.npmrc` 已配 npmmirror（electron / electron-builder-binaries 二进制从国内镜像拉，避免 GitHub 限速）。
- 用户双击 `MY_AGENT-Setup-0.1.0.exe` 安装后运行 `MY_AGENT.exe` 即自动拉起后端、打开桌面窗口，全程无需手动起服务或浏览器。

> 沙箱无显示器，窗口实际渲染需用户本机双击安装包验证；后端启动/健康检查/进程清理逻辑可用纯 Python 冒烟测试（`scripts/smoke_electron_backend.py`）等价验证通过。

## 与后端的契约
- `POST /api/chat`：body `{message, provider, model, history, session_id, kind, ...}`；
  SSE 事件 `message_start / content / reasoning / tool_call / tool_result / usage / approval_required / error / message_end`
- `POST /api/approve`：`{approval_id, decision:"approve"|"deny"}`
- `POST /api/cancel`：`{session_id}` — 中断该会话的当前对话（点「停止」时前端自动调用）
- `GET /api/providers`、`/api/health`、`/api/tools`

## 已知限制
- 审批闸门依赖后端 `wait_for_approval` 阻塞；前端确认/拒绝后流自动续传（仍成立）。
- 「停止」现已**真正中断后端**：点停止 → 前端 `POST /api/cancel` + 中断 SSE；后端 `agent_loop` 在流式/工具循环检测取消标志提前退出（已生成内容保留）。
