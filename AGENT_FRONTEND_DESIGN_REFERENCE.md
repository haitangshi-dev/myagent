# Agent 前端设计参考（Codex / ClawX / Hermes / Claude Code / 2026 Agentic UX）

> 调研目的：为 **MY_AGENT React 前端** 提供一份可参考的设计规范。
> 来源：Codex CLI 源码解析 + App Server 协议、ClawX（OpenClaw 桌面端）、Hermes Desktop、
> Claude Code（React+Ink）、2026 年 Agentic UX 设计范式（AG-UI / agentsurface.dev）。
> 调研时间：2026-07-25。
> 现有资产：后端 FastAPI + SSE，Hermes 显示协议（工具原始 JSON 永不下发，只发 `label/preview/status`），
> 事件齐全（`message_start/content/reasoning/tool_call/tool_result/usage/approval_required/error/message_end`）。
>
> 👉 结论先行：**后端协议已经站在 2026 主流范式上**，前端要做的是把「事件」翻译成「对的 UI」。

---

## 一、一句话结论

主流 agent 前端（Codex / ClawX / Hermes / Claude Code）在架构上高度一致：

> **前端是「表层（Surface）」，核心是「事件协议」。前端只消费结构化事件流，绝不直接碰模型、绝不渲染工具原始 JSON。**

这正好和你已有的 Hermes 协议 + SSE 完全对齐。你不用改后端，只差一个「懂范式」的 React 渲染层。

---

## 二、三个反复出现的架构原则（必抄）

### 2.1 前端只是表层，核心是事件协议
- **Codex 四层栈**：Surface（TUI / App Server / Web / IDE）→ Session（线程生命周期/配置/认证）→ Core（agent loop）→ Execution（沙箱/工具）。Core 不知道自己跑在哪个 Surface 上，通过 `async channel + 事件协议` 对外通信。
- **Codex App Server 双通道**：`Op`（client→session，如 `Op::UserTurn`/`Interrupt`/`Shutdown`）+ `EventMsg`（session→client，如 `AgentMessageDelta`/`ExecCommandBegin/End`/`PatchApplied`/`TokensUsed`）。**天然异步**：用户可在 agent 执行时继续打字/取消。
- **Codex Web 部署**：浏览器 tab 经 **HTTP + SSE** 与后端对话，后端把 worker 产生的任务事件流式推给前端；**状态/进度留在服务端**，关 tab 任务不丢，重连可续。→ 这直接验证了你「SSE 流式 + 服务端持有会话」的路线。

### 2.2 会话原语：Item → Turn → Thread（直接可抄的建模）
Codex App Server 把对话抽象成三级原语，是 React 状态层的最佳骨架：
- **Item（项）**：输入/输出的原子单位，有明确生命周期 `started → delta(可选) → completed`。可以是用户消息、agent 消息、工具执行、审批请求、diff。
- **Turn（轮）**：一次 agent 工作单元产生的 Item 序列，由用户输入启动。
- **Thread（线程）**：持久会话容器，支持 create / resume / fork / archive，保留事件历史供重连渲染。

👉 **映射到你的前端状态**：
```
Thread(会话) → Turn(一轮对话) → Item(消息/工具卡/审批卡/推理块)
```
每个 Item 带 `id + status(started|streaming|completed|failed)` + `role` + `kind`，React 只做 `status` 驱动的渲染，天然支持流式追加、工具卡转圈→打勾、失败标红。

### 2.3 工具即「UI 事件」，不是 JSON（行业共识）
- **Claude Code**：每个 Tool 是携带 UI 方法的运行时对象（`renderToolUseMessage` / `renderToolResultMessage`），工具自己决定怎么显示。
- **Hermes / 你的后端**：工具原始参数**永不下发**，只发 `{name, label, preview, status}`（已脱敏）。
- **AG-UI / agentsurface.dev**：`tool.started / tool.progress / tool.completed / tool.failed` 是结构化事件，携带 `runId + timestamp + source + display-safe summary`，**敏感输入不进前端**。

👉 你**已经满足**。前端铁律（见 `FRONTEND_FEATURE_PLAN.md` §八）：只渲染 `label/preview/status`，绝不再拼回 JSON。

---

## 三、布局范式（ClawX / Hermes Desktop 最成熟）

成熟 agent 工作台不是「聊天框」，而是一个三栏工作台：

| 区域 | 内容 | 来源 |
|------|------|------|
| **左栏** | 会话列表（新建/搜索/归档/多 profile 切换）+ 导航（Skills / Cron / Settings） | Hermes Desktop、ClawX `pages/` |
| **中栏（主区）** | 聊天流：消息气泡 + 工具卡 + 推理折叠块；底部 Composer（支持 ↑↓ 召回历史、拖拽文件） | 三者一致 |
| **右栏（预览轨）** | 边聊边渲染：文件/网页/工具输出/代码 diff，side-by-side（对标 Claude Artifacts） | Hermes Desktop、ClawX |
| **底部状态栏** | 行内模型选择器 + 每会话 YOLO/审批开关 + Token 用量 + 延迟 | Hermes Desktop |
| **命令面板** | `Cmd/Ctrl+K` 跳转动作与导航（发消息/切模型/搜会话/清屏） | Hermes、ClawX |

> 你已有 `FRONTEND_FEATURE_PLAN.md`，其中 Chat(5.x) / Skills(5.12) / Cron(5.14) / Profile(5.13) / 记忆(6.5) 正好对应左栏导航项。布局直接套这一套即可。

---

## 四、流式与工具可视化（2026 Agentic UX 范式）

### 4.1 三个可见状态，cancel 语义各不同
不要只放一个 spinner（那是「我也不知道在干嘛」的坦白）。生产级前端至少区分：

| 状态 | 显示 | Cancel 语义 |
|------|------|-------------|
| **Planning** | 「思考中…」+ 可取消，默认**不**把原始 CoT 推给用户 | 取消本轮规划 |
| **Acting** | 工具卡运行中（转圈/进度） | 取消**这一个工具**（不是整个对话） |
| **Responding** | token 流式输出 + 显眼 **Stop** 按钮 | 结束生成，只计已产生 token |

- **推理默认折叠**：提供可展开的思考面板（off by default），运营商看 trace、普通用户看干净状态切换。→ 你的 `reasoning` 事件已支持，前端做折叠即可。
- **Stop 按钮即发送按钮位**：流式一开始，发送键变成 Stop，同位置、不隐藏、不进菜单、`Esc` 快捷键、首次按下免确认。

### 4.2 工具卡三阶段：Announce → Stream → Result
每个工具调用在 UI 上走三步（metacto 范式）：
1. **Announce（宣告）**：一张卡，写明「工具名 + 入参（脱敏）+ 意图」，例如「即将读取 `/tmp/a.log`」。差异就在这一张卡——agent 和魔术的区别。
2. **Stream（执行中）**：快工具（<200ms）合并成单状态；慢工具（查库/调 API/多步）像 token 一样流式显示状态与进度。
3. **Result（结果）**：行内渲染结果预览，用户成为审计链一环。

👉 对应你的 `tool_call(status=running) → tool_result(status=done/failed, preview)`。把 `label/preview` 直接喂进这张卡即可。

### 4.3 结构化输出做成卡片，不是段落
列表/对比/实体用可视化卡片（对标 Perplexity/Linear AI）。你的 `content` 已是 Markdown，前端用 `react-markdown` + 代码块「复制」按钮即可达到这一档。

---

## 五、审批闸门（Hermes `acp_adapter` / Cline 范式）

危险动作（删文件/覆盖/跑命令/发消息/扣费）前必须人审。共识模型：

- **三级分级**：`safe`（静默执行）/ `risky`（本次询问）/ `destructive`（强制审批）。
- **EditProposal 统一结构**（Hermes，直接抄）：`{tool_name, path, old_text, new_text, arguments}`。后端落盘前先发前端，前端展示 diff + 回 `allow_once / allow_session / allow_always / deny / deny_always`。
- **敏感文件自动否决**：`.env / id_rsa / id_ed25519` 等强制走审批（补你现有 `C:\` 校验盲区）。
- **提示必须精确**：显示「将做什么 + 文件路径 + 收件人 + 金额」，不写「agent 想做点什么，允许？」。
- **每次审批留痕**：记录 allow/deny 供后续审计。

👉 你的后端已有 `approval_required` 事件 + `POST /api/approve`（见 `MEMORY.md`）。前端只需把这张「审批卡」做成和工具卡同族 UI，按钮文案直接用 Hermes 那套。

---

## 六、技术栈对照（ClawX 栈 = 你的 frontend-dev 推荐栈）

ClawX（OpenClaw 桌面端）是目前最公开的「React + Agent」参考实现，技术栈：

| 层 | ClawX 选型 | 你的 React 前端 |
|----|-----------|----------------|
| UI 框架 | React 19 + TypeScript | ✅ 同 |
| 样式 | Tailwind CSS + shadcn/ui | ✅ frontend-dev 推荐 |
| 状态 | **Zustand**（轻量 store：chat/gateway/settings） | ✅ 直接采用 |
| 构建 | Vite | ✅ 同 |
| 动画 | **Framer Motion**（ease-out-expo） | ✅ frontend-dev/impeccable 推荐 |
| 图标 | **Lucide React**（线性、无 emoji） | ✅ 同 |
| Markdown | 富 Markdown 渲染 | `react-markdown` + `remark-gfm` + 代码高亮 |
| 传输 | Electron Main 持有 WS/HTTP，含重连/超时/退避 | 纯 web：抽一个 **SSE Client 单例**承担「传输所有权」 |

**ClawX 的双进程思想，纯 web 版怎么落地**：
- Electron Main 干的活（WS/HTTP 所有权、重连/`timeout`/`backoff`、CORS 代理、keychain）→ 在 web 版里集中到一个 **`lib/transport.ts` 单例**：建立 SSE、自动重连、指数退避、断线保留草稿。
- 「单入口调用」原则 → 所有后端请求走 `lib/api.ts`，屏蔽协议细节。
- 安全存储 → web 端用 `localStorage`/`IndexedDB` 存会话与设置（密钥仍在后端，前端只存非敏感配置）。

---

## 七、映射到 MY_AGENT 的具体落地清单

### 7.1 后端协议 → 前端事件（直接对齐，零后端改动）
| 后端 SSE 事件 | 前端渲染 | 对应范式 |
|---------------|---------|---------|
| `message_start` | 建消息占位（去重用 `message_id`） | message.created |
| `content` | Markdown 流式追加 + 代码块复制 | Responding 态 |
| `reasoning` | 折叠「思考中…」可展开 | agent.thinking（默认折叠） |
| `tool_call` | 工具卡 `Announce`（label+preview+转圈） | tool.started |
| `tool_result` | 工具卡打勾/标红（preview） | tool.completed/failed |
| `usage` | 底部状态栏累加 Token/估算 ¥ | 零成本拿下 |
| `approval_required` | 审批卡（diff + allow/deny 按钮） | approval.requested |
| `error` | 只显人类可读 `message`，不显 traceback | tool.failed |
| `message_end` | 收尾，恢复发送按钮 | message.completed |

### 7.2 第一迭代（零后端，体感最直接）
按 `FRONTEND_FEATURE_PLAN.md` 的优先级，先做：
1. **三栏工作台布局**（左会话 / 中聊天 / 右预览轨 + 底部状态栏 + Ctrl+K）。
2. **Markdown 渲染 + 代码块复制**（5.1）。
3. **会话持久化**（5.2，SSE 无状态 → 前端序列化 `history` 到 localStorage/IndexedDB）。
4. **Token/成本显示**（5.3，吃 `usage` 事件）。
5. **工具卡三态 + 推理折叠 + Stop 按钮**（本参考 §四）。
6. **Slash 命令 `/`**（5.11）、**技能片段库**（5.12）。

### 7.3 第二迭代（需后端小改，功能质变）
- **变更文件追踪 + Diff**（6.1，后端 `tool_call` 加 `affected_path`）。
- **工具结果完整查看**（6.2，后端 `tool_result` 加 `full_result`）。
- **Plan 模式**（6.3，`/api/chat` 加 `plan_mode`）。
- **审批闸门 UI**（6.4，直接用 Hermes `EditProposal` 模型）。
- **记忆面板**（6.5，`GET/POST /api/memory`）。

---

## 八、关键不要踩的坑（行业血泪）
1. **不要把原始工具 JSON 推给前端**（你已规避）：既不安全也丑。
2. **不要默认把思维链（CoT）推给用户**：放大感知延迟、泄露内部上下文；做成可展开。
3. **Stop 不要做成「确认弹窗」**：首次按下免确认，否则是安慰剂。
4. **不要「聊天框唯一控制面」**：能做成按钮/表单/diff 的，别让用户打字纠正。
5. **审批不要「每次都问」也别「从不问」**：三级分级 + 「本次/永远允许」。
6. **断线要保留草稿 + 自动重连退避**（ClawX 的 Graceful Recovery）：否则长任务用户一刷新就丢审批。

---

## 九、参考资料（可深入）
- Codex 内部实现解析（四层栈 / 双通道）：https://grapeot.me/share/codex-cli-internals-survey-20260314.html
- Codex App Server 官方协议（Item/Turn/Thread + 审批请求）：https://openai.com/index/unlocking-the-codex-harness/
- Codex App Server 远程/WebSocket/鉴权：https://codex.danielvaughan.com/2026/03/31/codex-cli-app-server-remote-websocket
- ClawX 架构（Electron + React 19 + Zustand + Tailwind + Framer Motion）：https://techloghub.com/open-source/clawx-openclaw-ai-desktop-interface
- Hermes Desktop 官方文档（三栏工作台范式）：https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/desktop
- Claude Code 架构（React+Ink，Tool 自带 UI 方法）：https://claude-wiki.com/architecture.html
- Agentic UX 前端范式（AG-UI / 事件模型 / 审批 / 撤销）：https://agentsurface.dev/docs/agentic-ui
- 2026 AI Agent UX 设计范式（工具透明 / 审批闸门 / 行内 diff）：https://www.aydesign.ai/blog/ai-agent-ux-design-patterns-2026
- 生产级 AI Chat UX 模式（三态 / 中断 / 推理默认折叠）：https://www.metacto.com/blogs/ai-chat-ux-patterns-production
