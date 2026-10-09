# MY_AGENT

一个**自带工具链的桌面 AI Agent**：Electron 桌面壳 + FastAPI/SSE 后端 + React 前端。
能真正读写文件、执行命令、联网检索、跑 Python、控制桌面，并把思考链、工具调用、审批过程实时呈现出来。

> **本项目不提供任何服务商，也不提供推理后端。**
> 它只做「接口层」：填入任意 **OpenAI 兼容**接口（地址 / 密钥 / 模型名）即可使用。
> 不绑定任何厂商，不带任何密钥，不下载任何模型。

---

## 特性

| 能力 | 说明 |
|---|---|
| 工具调用 | 文件读写 / 精确替换 / 列目录 / 代码检索 / Shell / Python / 下载 / HTTP / 联网搜索 |
| 桌面控制 | 截图（OCR 或视觉理解）/ 鼠标 / 键盘 / 窗口操作，用于操作没有命令行的图形程序 |
| 任务看板 | 复杂需求自动拆成子任务清单，紧贴输入框实时显示进度 |
| 定时任务 | 「每天 9 点帮我…」这类需求由调度器在后台自动执行 |
| 内置知识库 | 随程序自带的**空**知识库，首次运行自动创建；可检索、可沉淀、可长期复用 |
| 记忆系统 | 跨会话长期记忆，自动抽取关键事实，支持主动召回 |
| 子智能体 | 把子任务派给独立角色的子智能体并行处理 |
| 两种模式 | **完整模式**（全部工具 + 完整提示词）/ **极简模式**（精简工具 + 短提示词，省 token） |
| 权限 3 档 | 只读 / 常规 / 接管电脑，敏感操作（删除等）强制前端确认 |
| 电子桌面壳 | 双击即跑，自动拉起后端、托盘常驻、资源管理器右键唤起 |

---

## 快速开始

### 方式一：安装包（推荐）

1. 运行 `MY_AGENT-Setup-x.y.z.exe` 完成安装；
2. 双击 `MY_AGENT.exe`，桌面窗口自动打开（后端由壳程序自动拉起）；
3. 点右上角 **🔑 钥匙按钮** → 填入你的接口：

   | 字段 | 示例 |
   |---|---|
   | 接口地址 | `https://api.openai.com/v1` |
   | 密钥 | `sk-...` |
   | 模型名 | `gpt-4o-mini` |

4. 保存后即可对话。

> 任何 OpenAI 兼容接口都可以：官方 API、国内各家大模型、自建 vLLM / Ollama / LM Studio /
> one-api 网关等。**地址以 `/v1` 结尾**通常即为正确写法。

### 方式二：源码运行

```bash
pip install -r requirements.txt
python main.py --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

或一条命令拉起后端并自动开浏览器：

```bash
python start.py
```

---

## 打包（Windows 安装包）

```bash
cd web
npm install
npm run dist:win        # = npm run build && electron-builder --win --x64
```

产物：`web/release/MY_AGENT-Setup-<version>.exe`

打包要点（`web/package.json` 的 `build` 段）：

- `extraResources` 把后端整棵代码（`main.py` / `config` / `agents` / `providers` / `server` /
  `scripts` / `skills`）拷进 `resources/backend/`，前端构建产物拷进 `resources/backend/web/dist`；
- `appId: com.myagent.desktop`，`productName: MY_AGENT`，NSIS 非一键安装、可选安装目录；
- `.npmrc` 已配国内镜像（electron / electron-builder 二进制不走 GitHub，避免限速）；
- **打包必须前台执行**：Electron 的 NSIS 打包在无窗口/后台环境下会卡住。

---

## 数据与隐私

所有用户数据都落在**用户数据目录**，不进安装包、卸载不丢：

| 内容 | 位置（Windows） |
|---|---|
| 接口配置（地址/密钥/模型名） | `%APPDATA%\MY_AGENT\settings.override.yaml` |
| 内置知识库（初始为空） | `%APPDATA%\MY_AGENT\knowledge` |
| 任务看板 / 定时任务 | `%APPDATA%\MY_AGENT\data` |
| 跨会话记忆 | `%APPDATA%\MY_AGENT\memory` |
| 已安装技能 | `%APPDATA%\MY_AGENT\skills` |

- **本仓库不含任何密钥、不含任何用户数据、不含模型权重**；
- 后端默认只绑 `127.0.0.1`，仅本机可达，不暴露到局域网/公网；
- 密钥只存在本机，前端回显时自动掩码。

---

## 项目结构

```
.
├── main.py                  # 后端入口（FastAPI + uvicorn）
├── start.py                 # 一体化启动器（后端 + 浏览器）
├── config/
│   ├── __init__.py          # 配置加载（含用户 override 深度合并）
│   └── settings.yaml        # 默认配置（不含任何密钥）
├── agents/
│   ├── agent_loop.py        # Agent 主循环（工具调用 / 审批 / 取消 / 兜底）
│   ├── tools.py             # 全部工具实现与注册表
│   ├── knowledge_base.py    # 内置知识库（首次运行自动创建空库）
│   ├── global_memory.py     # 跨会话长期记忆
│   ├── scheduler.py         # 定时任务调度
│   ├── email_gateway.py     # 邮件唤醒网关（可选）
│   └── ...
├── providers/
│   ├── manager.py           # 接口管理器
│   └── openai_compat.py     # 统一 OpenAI 兼容流式客户端
├── server/api.py            # HTTP/SSE 接口 + 前端静态托管
└── web/                     # React + Vite + Tailwind 前端 & Electron 壳
    ├── src/                 # 前端源码
    ├── electron/            # Electron 主进程 / 预加载
    └── package.json         # 含打包配置
```

---

## 接口契约（简要）

| 端点 | 说明 |
|---|---|
| `GET /api/health` | 存活探测 |
| `GET /api/providers` | 可用接口列表（密钥掩码） |
| `GET /api/mode` / `POST /api/mode` | 读取 / 切换模式（`full` / `minimal`） |
| `POST /api/chat` | 流式对话（SSE） |
| `POST /api/approve` | 审批敏感操作 |
| `POST /api/cancel` | 中断当前对话 |
| `POST /api/settings/override` | 保存接口配置到用户数据目录 |
| `GET /api/tools` | 工具清单 |

SSE 事件：`message_start / content / reasoning / tool_call / tool_result / usage / approval_required / error / message_end`

---

## 说明

### 关于语音识别资源

离线语音识别（sherpa-ncnn wasm）需要约 135MB 的模型文件，超出代码托管平台的单文件限制，
因此**未随仓库提供**。需要该功能时执行：

```bash
cd web
npm run fetch:sherpa
```

未下载时语音识别会自动回退到浏览器/系统的在线能力，不影响其他功能。

### 其他

本项目为个人作品，功能可用但仍在演进中。欢迎提 Issue 反馈问题。

---

## ☕ 支持作者

这个项目是**我完全用 AI 搭建起来的** —— 代码、界面、文档，绝大部分都是 AI 写的，
我只负责提需求、试用、和不停地「继续」。

如果你觉得它有点用，可以请我喝瓶水 😄

<div align="center">
  <img src="docs/donate-qrcode.png" width="280" alt="微信收款码">
  <br>
  <sub>微信扫一扫 · 感谢你的支持</sub>
</div>

> 本项目系个人弃用项目，现仅作开源分享，不再更新维护。
> 愿意的话点个 Star，就是对我最大的鼓励 ⭐
