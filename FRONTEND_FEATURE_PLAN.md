# 原生前端功能增强路线图（FRONTEND FEATURE ROADMAP）

> 目的：早期原生前端（Phase 1–4）功能偏少。本文先盘点 2026 年主流 IDE Agent
> 产品的标杆功能，再对照**本项目后端已具备的能力**与**当前前端已有功能**，给出一份可落地的
> 新增功能清单。每条都标注「行业来源」与「后端依赖等级」，确保是能在这套后端上真正跑起来的，
> 而不是空头支票。
>
> 调研时间：2026-07-25。行业参照：Cursor 3 / Windsurf 2.0 / Cline / Claude Code / GitHub Copilot CLI（2026）。

---

## 一、行业标杆功能盘点（2026）

| 功能 | 代表产品 | 说明 |
|------|----------|------|
| 多文件 Agentic 编辑 | Cursor Composer、Windsurf Cascade、Copilot Agent Mode、Cline Plan/Act | 模型自主读多文件、改多文件、跑命令、自纠错误，循环到任务完成 |
| **Plan 模式**（先规划后执行） | Copilot `Shift+Tab`、Cline Plan/Act 双模式 | 先产出结构化计划，用户批准后再写代码 |
| **后台/并行代理** | Cursor Background Agents、Claude Code Sub-Agents / Workflows | 派生子代理并行跑长任务，主会话不被打断 |
| 终端深度集成 | 全部产品 | 完整 shell，命令执行/审批 |
| **工具审批闸门** | Cline（每步确认）、Copilot autopilot/preToolUse | 危险操作（删/覆盖/跑命令）前征求同意 |
| **MCP 协议** | Cline（深度）、Cursor（有限）、Copilot 内置 GitHub MCP | 连接外部工具/数据源的标准协议 |
| **记忆（跨会话）** | Windsurf Memories、Claude Code `CLAUDE.md`、Copilot repo memory | 记住代码约定/偏好，后续更聪明 |
| **Skills（技能）** | Claude Code Skills、Copilot Agent Skills | Markdown 描述的专用工作流，按上下文自动触发 |
| **Hooks** | Claude Code preToolUse/postToolUse | 生命周期钩子，执行前校验/执行后处理 |
| 行内自动补全（Tab） | Cursor Tab、Windsurf Tab | 光标处幽灵文本补全 |
| **Diff / 撤销（rewind）** | Copilot `/diff`、Claude Code Esc-Esc 回滚 | 会话内改动语法高亮 diff，可回退快照 |
| **会话中切换模型** | Copilot `/model`、Cline BYOK | 任务中途换模型（快模型规划/慢模型执行） |
| **推理可见性开关** | Copilot `Ctrl+T` | 切换是否展示思考链 |
| **上下文自动压缩** | Copilot auto-compaction、本项目 `auto_compress` | 接近窗口上限时后台压缩历史 |
| 多模态（图/PDF） | Copilot、Claude | 直接读图/文档 |
| 任务→PR 工作流 | Copilot Coding Agent | 指派 Issue，自动建分支、写码、跑测、开 PR |

---

## 二、你的后端已具备的能力（可复用资产）

### 2.1 SSE 事件协议（`/api/chat`，已在 `agents/agent_loop.py` 实现）
前端目前只消费了其中一部分，以下字段**后端已经在发**，前端没用就是浪费：

| 事件 type | 字段 | 当前前端是否用 | 可用来做什么 |
|-----------|------|----------------|--------------|
| `message_start` | `message_id` | 部分 | 消息去重/定位 |
| `content` | `text` | ✅ 已流式显示 | — |
| `reasoning` | `text` | ✅ 已折叠显示 | 推理可见性开关 |
| `tool_call` | `id, name, label, preview, status` | ✅ 工具卡 | `label/preview` 已是人话（Hermes 协议，原始 JSON 永不下发） |
| `tool_result` | `id, status, preview` | ✅ 工具卡打勾 | ⚠️ **只发 600 字符预览**，完整结果未下发 |
| `usage` | `prompt_tokens, completion_tokens` | ❌ **未用** | **Token/成本显示**（零成本拿下） |
| `error` | `message` | ✅ | — |
| `message_end` | — | ✅ | — |

> 关键点：Hermes 协议的**脱敏**（`redact_sensitive_text`）已在后端做，密钥不会进前端。
> 工具原始参数永不下发，所以「工具调试」只能看 `label/preview`，要做「查看完整工具输出」必须动后端。

### 2.2 已注册工具（19 个，`agents/tools.py`）
`think` · `shell` · `read_file` · `write_file` · `edit_file` · `list_files` · `search_content`
· `web_fetch` · `web_search` · `python_exec` · `time_now` · `download` · `http_request`
· `memory_remember` · `memory_recall`

> 这些都是**服务端执行**的，前端看不到文件被改到哪。要做「变更文件追踪 / Diff 审阅」，
> 必须让后端在 `tool_call`/`tool_result` 事件里额外带上 `affected_path`（小改，见 §五 6.1）。

### 2.3 配置与 Provider（`config/settings.yaml`）
- 4 个 provider：**NIM**（DeepSeek-V4-Flash，原生 1M 上下文）、**Ollama Cloud**（minimax-m3，支持 reasoning）、**OpenRouter**、**自定义 OpenAI 兼容**。
- `auto_compress.max_context_tokens: 0`（用模型自身上限，1M）。
- `agent.max_tool_rounds: 12`、`tool_timeout: 120`。
- `/api/chat` 是**无状态**的：`history` 由前端自己维护并回传 → **会话持久化纯前端就能做**。
- 后端**无** MCP、无 Hooks、无多模态（所有 provider `supports_vision: false`）、工具是**自主执行无审批**。

### 2.4 当前前端已有（Phase 1–4，`cpp_client`）
多会话聊天（流式/思考链/工具卡）· 文件管理器 + 多标签编辑器（Ctrl+S）· 设置面板（改 base_url/provider/模型/主题 + 实时延迟）· 本地终端（CreateProcess cmd.exe，UTF-8）· 命令面板（Ctrl+Shift+P）· Toast · 健康探测（15s）· 会话切换器。

---

## 三、差距分析矩阵（行业功能 × 你的现状）

| 行业功能 | 你的现状 | 缺口等级 |
|----------|----------|----------|
| 聊天 Markdown 渲染 / 代码块 | 纯文本 | 🔴 大（体感掉档） |
| 会话持久化（关程序不丢） | 内存，关即丢 | 🔴 大 |
| 聊天内搜索 | 无 | 🟠 中 |
| 消息编辑 / 重发 / 重新生成 | 无 | 🟠 中 |
| Token / 成本显示 | `usage` 已下发但未用 | 🟢 零成本 |
| 变更文件追踪 + Diff 审阅 | 无（后端也不带路径） | 🟠 需后端小改 |
| 工具结果完整查看 | 仅 600 字预览 | 🟠 需后端小改 |
| Plan 模式（先规划） | 无 | 🟠 需后端小改 |
| 工具审批闸门（删/覆盖/命令前确认） | 无（后端自主执行） | 🟠 需后端中改 |
| 记忆面板（memory 工具可视化） | 无（仅能聊天触发） | 🟠 需后端小端点 |
| 附件 / 文件拖拽发给模型 | 无 | 🟢 纯前端 |
| 编辑器语法高亮 | 纯文本框 | 🟠 中（接 ImGuiColorTextEdit） |
| 终端带颜色 / ConPTY | 裸管道无颜色 | 🟠 中 |
| 通知中心（Toast 历史） | 仅瞬时 | 🟢 纯前端 |
| 会话导入 / 导出 | 无 | 🟢 纯前端 |
| 快捷键帮助面板 | 无 | 🟢 纯前端 |
| 命令面板扩命令 | 已有基础 | 🟢 纯前端 |
| 后台 / 并行代理 | 无 | 🔴 需后端大改（未来） |
| MCP 扩展 | 无 | 🔴 需后端架构（未来） |
| 多模态（图/PDF） | 后端不支持 vision | ⚫  blocked（需 vision provider） |
| 行内 Tab 补全 | 无 | 🔴 重（未来） |

---

## 四、Hermes 参考实现功能盘点（真实 UI / 审批模型 / 工具集）

> 来源：`C:\myagent_backup_20260722_1705\reference\hermes-agent-main`（真实 Hermes Agent 仓库，
> 本项目的后端即「高仿」它）。目的：把真实 Hermes 已经做出来、且契合我们架构的能力挑出来对齐到
> 路线图，避免闭门造车。下文所有条目都已映射到 §五 的具体 Phase 行号。

### 4.1 真实 Hermes Web UI 的功能面（`web/src`）
Hermes 自带一套完整 Web 客户端，其页面/组件直接告诉我们「一个成熟的 Hermes 前端长什么样」：

| Hermes 页面 / 组件 | 映射到路线图 | 说明 |
|---|---|---|
| `Markdown.tsx` | 5.1 | 已有成熟 Markdown 渲染组件，我们的实现可对标其解析粒度 |
| `ChatSidebar` / `ChatSessionList` / `SessionsPage` | 5.2 / 已有会话切换 | 会话侧栏 + 独立会话页，比顶部下拉更顺手 |
| `SlashPopover`（聊天内 `/` 命令） | **新增 5.11** | 聊天框输入 `/` 弹出命令菜单，比全局 Ctrl+Shift+P 更贴合对话流 |
| `SkillsPage` / `SkillEditorDialog` / `skills/`（19 类） | **新增 5.12** | 技能库页面 + 技能编辑器；前端可做「提示词/技能片段库」 |
| `ModelsPage` / `ModelPickerDialog` / `ReasoningPicker` | 6.6 升级 | 模型选择 + **推理强度(reasoning effort)** 选择器（不止开关，还能调强度） |
| `ToolsetConfigDrawer` | 补全工具面板 | 工具集开关抽屉，让用户启用/禁用某类工具 |
| `ProfilesPage` / `ProfileBuilderPage` / `ProfileSwitcher` | **新增 5.13** | **多 Agent 人设(Profile)切换**（切换 system prompt 人格） |
| `CronPage` / `ScheduleBuilder` / `AutomationBlueprints` | **新增 5.14** | 定时任务 / 自动化蓝图 |
| `AnalyticsPage` | 5.3 升级 | Token/成本/用量分析页（比单行显示更完整） |
| `McpPage` | Phase 7 MCP | MCP 服务器管理页 |
| `LogsPage` / `HermesConsoleModal` | 新增 | 日志 / 控制台查看 |
| `OAuthLoginModal` / `PlatformsCard` / `ChannelsPage` / `WebhooksPage` | 未来 | 外部平台 / OAuth / 频道 / Webhook 集成 |
| `ConfirmDialog` / `DeleteConfirmDialog` | 6.4 | 确认对话框（审批闸门 UI 基座） |
| `LanguageSwitcher` / `i18n` | — | 国际化（我们中文优先，暂不必） |
| `ThemeSwitcher` / `themes/` | 已有三主题 | 主题切换（可作更多预设） |

### 4.2 Hermes 的审批 / Diff 模型（`acp_adapter`）—— 直接喂给 §五 6.1 / 6.4
`acp_adapter/edit_approval.py` 与 `permissions.py` 给出了可复用的精确模型，不要再自己设计：

- **`EditProposal` 数据类**：`{tool_name, path, old_text, new_text, arguments}` —— 这正是 6.1「变更文件追踪 + Diff」和 6.4「审批闸门」的**统一数据结构**。后端在真正落盘前，先把 `EditProposal` 发给前端，前端展示 diff 并回 `allow_once / allow_session / allow_always / deny / deny_always`。
- **自动审批级别**：`AUTO_APPROVE_ASK`（每次问）/ `AUTO_APPROVE_WORKSPACE`（工作区内自动）/ `AUTO_APPROVE_SESSION`（本次会话自动）—— 对应审批弹窗的「本次允许 / 总是允许」选项。
- **敏感文件自动否决**：`SENSITIVE_AUTO_APPROVE_NAMES = {.env, .env.local, id_rsa, id_ed25519, ...}` —— 对这些文件强制走审批，正好补我们 `C:\` 路径安全校验的盲区。
- **权限选项语义**：`allow_once / allow_session / allow_always / deny / deny_always` —— 直接作为审批弹窗的按钮文案。

> 结论：6.4 直接搬 Hermes 的 `EditProposal` + `permissions` 模型；6.1 的 diff 视图直接复用 `old_text / new_text`。

### 4.3 Hermes 工具集里我们后端可借鉴的能力（`tools/`）
我们目前 19 个工具，Hermes 有 80+。以下与「前端体验」强相关、且后端可低成本补：

| Hermes 工具 | 价值 | 对应路线图 |
|---|---|---|
| `checkpoint_manager.py` | **检查点 / 撤销(rewind)** —— 改文件前打 checkpoint，可回退 | **新增 6.10**（Diff/撤销） |
| `todo_tool.py` | 任务清单（agent 自建 todo 跟踪进度） | **新增 5.15**（任务面板） |
| `session_search_tool.py` | **跨会话搜索** | 5.4 升级（跨会话） |
| `clarify_tool.py` / `clarify_gateway.py` | 执行前先向用户澄清（Plan/Ask 近亲） | 6.3 Plan 模式补充 |
| `path_security.py` | 路径安全（禁写系统目录） | 强化现有 `C:\` 校验 |
| `delegate_tool.py` / `async_delegation.py` | 委派子任务 / 异步委托 | Phase 7 后台代理 |
| `kanban_tools.py` | 看板任务管理 | 未来 |
| `cronjob_tools.py` | 定时任务 | 5.14 |
| `image_generation_tool.py` / `browser_tool.py` / `computer_use_tool.py` | 图像生成 / 浏览器 / 电脑操控 | Phase 7 多模态 |

### 4.4 对齐后新增 / 调整的路线图条目（见 §五）
- **5.11** 聊天内 Slash 命令（`/` 弹出，对标 `SlashPopover`）
- **5.12** 技能 / 提示词片段库页面（对标 `SkillsPage`）
- **5.13** 多 Agent 人设(Profile)切换（对标 `ProfilesPage`）
- **5.14** 定时任务 / 自动化（对标 `CronPage`）
- **5.15** 任务(Todo)面板（对标 `todo_tool`）
- **6.6 升级**：推理**强度**选择器（对标 `ReasoningPicker`）
- **6.10** 检查点 / 撤销（rewind，对标 `checkpoint_manager`）

---

## 五、建议新增功能清单（按优先级分 Phase）

依赖等级图例：**零**=前端独立完成；**小改**=后端加字段/加端点；**中改**=后端改执行流；**大改**=后端架构级。

### 🟢 Phase 5 — 纯前端高收益（零后端依赖，体感立刻提升）

| # | 功能 | 行业参照 | 实现要点 |
|---|------|----------|----------|
| 5.1 | **聊天 Markdown 渲染 + 代码块复制** | 所有产品 | 解析 `content` 渲染标题/列表/粗体/行内代码；``` 代码块加语言标签 + 右上「复制」按钮（ImGui 内做轻量 MD 解析器，或 vendor `imgui_markdown`） |
| 5.2 | **会话持久化** | Copilot 跨会话记忆（部分） | `/api/chat` 无状态 → 前端把 `Session.messages` 序列化到 `sessions/*.json`；启动时加载；可设自动保存 |
| 5.3 | **Token / 成本显示** | Copilot 用量 | 已有 `usage` 事件 → 每轮累加 prompt/completion tokens；按 provider 单价估算 ¥ 成本（单价表可配） |
| 5.4 | **聊天内搜索** | Cursor/Cline | 顶部搜索框，过滤消息正文/工具标签，命中高亮 |
| 5.5 | **消息编辑 / 重发 / 重新生成** | Copilot/Cursor | 用户消息可编辑后重发（带着截断后的 history）；Assistant 消息可「重新生成」 |
| 5.6 | **附件 / 文件拖拽** | 通用 | 拖文件进对话 → 前端读文本/路径，自动构造「请用 read_file 读取 `<path>`」或把内容 inline 进消息 |
| 5.7 | **通知中心（Toast 历史）** | — | 右下 Toast 之外，加一个面板列出历史通知，可点击回溯 |
| 5.8 | **会话导入 / 导出** | — | 导出当前会话为 Markdown/JSON；导入恢复 |
| 5.9 | **快捷键帮助面板** | — | `?` 弹出所有快捷键（Ctrl+Shift+P、Ctrl+S、Esc 等） |
| 5.10 | **命令面板扩命令** | Cursor/Cline 命令面板 | 零 | 增加：搜索会话、清空当前会话、复制最后代码块、切换推理可见性、打开工作目录 |
| 5.11 | **聊天内 Slash 命令（`/`）** | Hermes `SlashPopover` | 零 | 聊天框输入 `/` 弹出命令菜单（发消息 / 切模型 / 新建会话 / 调技能等），比全局命令面板更贴合对话流 |
| 5.12 | **技能 / 提示词片段库** | Hermes `SkillsPage` | 零 | 侧栏/页面列出常用提示词片段与技能，点击插入对话；后端暂无 skills 系统，前端先做本地片段库 |
| 5.13 | **多 Agent 人设(Profile)切换** | Hermes `ProfilesPage` | 零 | 切换不同 system prompt 人格（如「代码助手」「写作」「研究」），存为本地预设 |
| 5.14 | **定时任务 / 自动化** | Hermes `CronPage` | 小改 | 前端建定时/周期性任务，后端加 cron 执行端点（包装现有工具） |
| 5.15 | **任务(Todo)面板** | Hermes `todo_tool` | 零 | 展示 agent 自建的任务清单与进度；前端订阅 `todo_tool` 输出或本地维护 |

### 🟠 Phase 6 — 需后端小/中改（功能质变）

| # | 功能 | 行业参照 | 后端依赖 | 实现要点 |
|---|------|----------|----------|----------|
| 6.1 | **变更文件追踪 + Diff 审阅** | Copilot `/diff`、Cursor 工作集 | 小改 | 后端在 `tool_call`/`tool_result` 对 `read_file/write_file/edit_file` 带 `affected_path`；前端汇总成「本次改动」面板，调 `git diff`/文本 diff 可视化 |
| 6.2 | **工具结果完整查看** | Cline 透明日志 | 小改 | 后端加 `full_result` 按需下发（或提高 `tool_result` cap）；前端工具卡可展开看完整输出 |
| 6.3 | **Plan 模式** | Copilot/Cline Plan | 小改 | `/api/chat` 加 `plan_mode=true`：模型首轮回复后**不执行工具**，仅返回计划；前端标「计划待批准」再发二次请求执行 |
| 6.4 | **工具审批闸门** | Cline 每步确认 | 中改 | `/api/chat` 加 `require_approval`：遇到 `shell`/`write_file`/`edit_file`/`download` 等危险工具时，后端 emit `tool_needs_approval` 并暂停，等前端 `POST /api/approve` 再执行 |
| 6.5 | **记忆面板** | Windsurf Memories、Claude `CLAUDE.md` | 小改 | 后端加 `GET/POST /api/memory`（包装现有 `memory_*` 工具）；前端做「记忆库」面板：浏览/搜索/新增/删除条目 |
| 6.6 | **推理强度选择器** | Copilot `Ctrl+T`、Hermes `ReasoningPicker` | 小改 | 不止开关，还能选推理强度（off/low/medium/high）；`/api/chat` 加 `reasoning_effort`，前端 `ReasoningPicker` 风格下拉 |
| 6.7 | **上下文压缩提示** | Copilot auto-compaction | 零 | 当 `history` token 接近阈值，状态栏提示「即将压缩」，可手动「总结历史」 |
| 6.8 | **编辑器语法高亮 + 查找替换** | Cursor/Windsurf 编辑器 | 零 | vendor `ImGuiColorTextEdit` 替换 `editor.cpp` 的 `InputTextMultiline`；加 Ctrl+F 查找 |
| 6.9 | **终端升级 ConPTY + 颜色** | 全部产品 | 零 | 用 Windows ConPTY 替代裸管道，支持 ANSI 颜色、箭头历史、resize |
| 6.10 | **检查点 / 撤销(rewind)** | Hermes `checkpoint_manager` | 中改 | 后端改文件前打 checkpoint（新增 `checkpoint_manager` 工具）；前端「本次改动」面板可一键回退到改动前快照 |

### 🔴 Phase 7 — 架构级 / 未来（重大后端工作，先记录）

| # | 功能 | 行业参照 | 后端依赖 | 说明 |
|---|------|----------|----------|------|
| 7.1 | **后台 / 并行代理** | Cursor Background Agents、Claude Sub-Agents | 大改 | 后端需支持异步任务队列 + 多会话并发；前端用独立标签页承载 |
| 7.2 | **MCP 扩展** | Cline 深度 MCP | 大改 | 后端开放工具注册协议，前端加「MCP 服务器」管理面板 |
| 7.3 | **Hooks（执行前校验）** | Claude Code Hooks | 中改 | 后端加 preToolUse 拦截（如禁止 `git push --force` 到 main） |
| 7.4 | **多模态输入** | Copilot/Claude 读图 | 大改+依赖 | 需接入 `supports_vision: true` 的 provider；前端加图片粘贴/拖入 |
| 7.5 | **行内 Tab 补全** | Cursor Tab | 大改 | 编辑器级补全模型，独立于对话流 |
| 7.6 | **任务→PR 工作流** | Copilot Coding Agent | 大改 | 需 Git 后端 + 平台集成 |

---

## 六、优先级建议 & 第一步

**立刻能做（零后端、体感最明显）**：5.1 Markdown 渲染 + 5.2 会话持久化 + 5.3 Token 显示。
这三条不动后端一行代码，却直接把「像不像个正经客户端」拉起来。顺手还能把 Hermes 对齐的
**5.11 Slash 命令**、**5.12 技能片段库** 一起做了——它们同样是纯前端、且让「高仿 Hermes」名副其实。

**次优先（需后端小改、功能质变）**：6.1 变更文件追踪 + Diff、6.3 Plan 模式、6.5 记忆面板。
这三条能把你的「通用 Agent」从「聊天框」升级成「带工作集审阅的 Agent 工作台」——
而这正是 Cursor/Windsurf 的核心差异点，且完美契合你后端已有的 `edit_file`/`write_file`/`memory_*`。
其中 **6.1 / 6.4 直接复用 Hermes `acp_adapter` 的 `EditProposal` + 权限模型**（见 §四 4.2），不必另起炉灶。

**不建议现在碰**：Phase 7 全部（后台代理、MCP、多模态）属于后端架构级投入，等前端体验夯实再说。

### 推荐落地顺序
1. **Phase 5.1 → 5.2 → 5.3 + 5.11 + 5.12**（一个迭代搞定，纯前端，顺带 Hermes 对齐）
2. **Phase 6.1 + 6.3 + 6.4**（后端加 `affected_path` / `plan_mode` / `require_approval`，审批与 Diff 直接套 Hermes `EditProposal` 模型）
3. **Phase 6.5 + 6.6**（记忆面板 + 推理强度；后端加 `/api/memory` 与 `reasoning_effort`）
4. 之后按需补 6.8/6.9 编辑器与终端打磨、6.10 检查点撤销、5.13/5.14/5.15 人设/定时/任务面板

---

## 七、附录：后端改动清单（供实现时对照）

| 改动 | 影响端点/文件 | 工作量 |
|------|----------------|--------|
| `tool_call` 事件加 `affected_path` | `agents/agent_loop.py` | 小 |
| `tool_result` 加 `full_result` 或提高 cap | `agents/agent_loop.py` (`_TOOL_PREVIEW_CAP`) | 小 |
| `/api/chat` 加 `plan_mode` 参数 | `server/api.py` | 小 |
| `/api/chat` 加 `require_approval` + `/api/approve`（套 `EditProposal` 模型） | `server/api.py` + `agents/agent_loop.py` | 中 |
| 新增 `GET/POST /api/memory` | `server/api.py`（包装 `memory_*` 工具） | 小 |
| `/api/chat` 加 `reasoning_effort` | `server/api.py` | 小 |
| 新增 `checkpoint_manager` 工具（改前打点） | `agents/tools.py` + `agents/agent_loop.py` | 中 |
| 新增 cron 执行端点（定时任务） | `server/api.py`（包装现有工具） | 小 |

> 说明：以上后端改动都**不破坏现有协议**（向前兼容，旧前端忽略新字段即可），
> 可独立提交、独立回归。Hermes 对齐项（审批/Diff/检查点）优先照搬 `acp_adapter` 与 `tools/checkpoint_manager.py` 的设计，避免重复造轮子。

---

## 八、JSON 前端渲染过滤规范（参考 Hermes / Grok Build / OpenClaw）

> 用户要求：json 前端渲染过滤，参考 `hermes-agent-main`、`grok-build`、`openclaw`。
> 结论先行：**后端（Hermes 协议）已经把「JSON 渲染过滤」做完了**——工具调用的原始
> JSON 参数**永不下发前端**，只下发 `label/preview`；工具结果只下发 600 字已脱敏预览。
> 所以「过滤」在前端侧是**渲染规则**，而不是再去做参数解析。本规范把它写死成前端铁律，
> 并已在新增的 `cli/render.py` 中实现。

### 8.1 三款产品各自给的启示
| 产品 | 与「渲染过滤」相关的设计 | 我们怎么用 |
|------|--------------------------|------------|
| **Hermes**（React 网页） | `Markdown.tsx` 只渲染人话（标题/列表/代码块/行内）；`slashExec.ts` 命令管线不暴露内部 JSON | 前端只渲染 `label/preview`；CLI 做轻量终端 Markdown |
| **Grok Build**（Rust TUI） | 终端 agent：工具调用/结果以可读行呈现，原始参数留在服务端；支持 headless（脚本）不渲染 UI | CLI 的 `--message` 无头模式只输出文本/JSON 事件流 |
| **OpenClaw**（Node CLI） | `tool-execution.ts` 对参数 `sanitizeRenderableText(JSON.stringify(args))` 并**preview/full 折叠**；`--no-color`/`--json` 输出模式 | CLI 对工具结果默认折叠、可 `/展开`；`--json` 直出事件流、不过度渲染 |

### 8.2 前端渲染过滤铁律（已写入 `cli/render.py` 顶部 RULES）
1. `tool_call` 事件：只渲染 `{name, label, preview, status}`。★ **绝不**渲染工具原始 JSON 参数（后端本就不下发，前端也绝不再拼回）。
2. `tool_result` 事件：只渲染服务端已脱敏、已截断(600 字)的 `preview`；★ **绝不**渲染完整结果（长结果默认折叠，可 `/展开` 看更多，但仍限于已下发内容）。
3. `content`（模型正文）：Markdown 渲染；若正文里夹带疑似 JSON，再美化的同时也过一遍 `redact_sensitive_text` 二次脱敏。
4. `reasoning`（思考链）：默认折叠为「💭 思考中…」，可 `/思考` 展开；不计入正文。
5. `usage`（用量）：只渲染 `prompt/completion tokens`；不渲染其他内部字段。
6. `error`（错误）：只渲染人类可读 `message`；★ **绝不**渲染 traceback / 内部栈。
7. 任何文本渲染前，统一过 `redact_sensitive_text` 做**防御性二次脱敏**（密钥/令牌/卡号）。

### 8.3 为何不必改后端
后端 `agents/agent_loop.py` 的 `tool_call` 只 emit `label/preview`、`tool_result` 只 emit 600 字 `preview`（且已过 `redact_sensitive_text`），这与 8.2 的铁律天然一致。若未来要「查看完整工具输出」，属于 §五 6.2 的后端小改（加 `full_result` 按需下发），届时前端再决定要不要渲染——但默认仍折叠。

---

## 九、CLI 设计（纯中文，参考三款产品）

> 用户要求：在原生前端之外**加入 CLI 界面**，CLI 设计参考 Hermes / Grok Build / OpenClaw，
> 且 **CLI 界面纯中文**（菜单、提示、斜杠命令、帮助、报错全中文）。
> 新增模块：`C:\my agent\cli\`（纯 Python 标准库，无需任何第三方依赖，与后端同源好维护）。

### 9.1 三款产品的 CLI 借鉴点
| 借鉴对象 | 采纳的设计 | 在 `cli/` 中的落地 |
|----------|------------|---------------------|
| **Grok Build** | 交互式 REPL + 无头(`--message`) 脚本/CI 模式 | `agent_cli.py`：交互聊天；`--message "..."` 一次性问答（无头） |
| **OpenClaw** | 命令树 + 全局 `--json`/`--no-color`/`--url`/`--session`；lobster `#FF5A2D` 配色；`/status` 式 slash；工具 preview/full 折叠 | 全局标志齐全；`render.py` 用 lobster 橙做 ANSI 主色；中文 slash 命令；工具结果折叠 |
| **Hermes** | 轻量 Markdown 渲染 + 聊天内斜杠命令 | `render.py` 终端版 Markdown（标题/列表/代码块/行内代码/粗体/链接）；`/帮助` 等中文斜杠命令 |

### 9.2 文件结构
```
cli/
├── render.py     # 渲染层：ANSI 配色(lobster) · 脱敏 · JSON 美化 · 工具卡 · 终端 Markdown · 流式辅助
├── sse.py        # 纯标准库 SSE 客户端（读 /api/chat）+ health/providers 探测
├── session.py    # 本地会话持久化（仿 OpenClaw session key，存 ~/.myagent/cli_sessions/<key>.json）
└── agent_cli.py  # 主程序：交互/无头 + 子命令 health/models/sessions + 中文斜杠命令
```

### 9.3 命令与标志（全中文界面，英文别名仅兼容）
```
python cli/agent_cli.py                         # 交互聊天（默认会话 main）
python cli/agent_cli.py --message "帮我写快排"   # 一次性问答（无头/脚本模式）
python cli/agent_cli.py health                  # 检查后端健康
python cli/agent_cli.py models                  # 列出可用 provider
python cli/agent_cli.py sessions                # 列出本地会话
python cli/agent_cli.py --session work --provider ollama --model minimax-m3
python cli/agent_cli.py --message "hi" --json   # JSON 事件流输出（管道/脚本友好）
```
全局标志：`--url`(后端地址) · `--no-color`(关颜色，非 TTY 自动关) · `--json`(事件流) · `--session`(会话 key) · `--provider` · `--model`。

### 9.4 纯中文聊天内斜杠命令
`/帮助` · `/新建会话` · `/切换会话 [key]` · `/清空` · `/模型 [provider[:model]]` ·
`/状态`（后端健康+本会话用量）· `/导出 [路径]`（导出 Markdown）· `/思考`（展开/折叠思考链）·
`/展开`（展开/折叠工具结果）· `/关于` · `/退出`（英文 `/quit` `/exit` 同义）。

### 9.5 渲染样例（关键：工具只显 label/preview，原始 JSON 不出现）
```
◀ 助手：你好！这是流式回答。
🔧 工具 shell  ◌ 运行中
  └ 执行命令：ls -la /tmp
  ✓ 结果 total 12
drwxr-xr-x file
命令已执行完成。
💭 思考中…（输入 /思考 展开）
  ⚙ 用量：输入 50 · 输出 20 · 合计 70 tokens
```

### 9.6 与现有架构的关系
- 复用同一套后端 SSE（`/api/chat`）与 Hermes 协议，**零后端改动**。
- 与 `cpp_client`（原生桌面端）、`web/`（网页端）是**并列的第三种前端**，共享后端。
- 会话持久化纯前端（`session.py`），因 `/api/chat` 无状态、history 由前端回传。
- 已联调：编译通过、后端不可用时优雅报错、SSE 流式端到端（mock 验证 content/tool/reasoning/usage/message_end 全事件）、`--json` 直出、子命令与 `--url` 组合正确。

### 9.7 后续可加（非必须）
- `/切换会话` 带交互式选择、`/模型` 拉真实模型列表（需后端 `/api/providers` 返回 models，当前只返回 id）。
- 交互式用 `prompt_toolkit` 做语法高亮/历史（当前用标准 `input()`，保持零依赖）。
- 多行输入（`"""` 或 `Alt+Enter`）、`/搜索` 跨会话检索（复用 5.4）。
