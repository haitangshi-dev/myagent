// 后端契约类型（与 server/api.py + agents/agent_loop.py 对齐）

export type Role = 'user' | 'assistant'

export type ToolStatus = 'running' | 'success' | 'error'

// 工具类型：'tool' 普通工具 | 'skill' 技能市场/技能型工具
export type ToolKind = 'tool' | 'skill'

// 任务看板（task_board 工具 + SSE task_update 事件）
// 与后端 agents/tools.py 的 task_board 实际产出对齐：pending / doing / done
export type TaskStatus =
  | 'pending'
  | 'doing'
  | 'done'
export interface TaskItem {
  id: string
  title: string
  status: TaskStatus | string
  note?: string
  created_at?: string
  updated_at?: string
}

// 工作区沙盒策略（写入工作目录下的 .myagent_sandbox.json）
export interface SandboxPolicy {
  allow_file_write: boolean
  allow_file_delete: boolean
  allow_shell: boolean
}

// 会话类型：'chat' 普通聊天 | 'workspace' 工作区（受沙盒约束、文件落在工作目录）
export type SessionKind = 'chat' | 'workspace'

// 气泡内提示（重试 / 系统通知等），渲染为消息下的小灰条
export interface Notice {
  type: 'retry' | 'info'
  attempt?: number
  max?: number
  delay?: number
  message: string
}

export interface ToolCall {
  id: string
  name: string
  label: string
  preview: string
  status: ToolStatus
  kind?: ToolKind
  resultPreview?: string
  // 模型原始输入命令（脱敏），running 阶段即展示，使「调了什么」即时可见
  command?: string
  // 是否为 partial（流式过程中 arguments 未攒齐时的即时卡片）
  partial?: boolean
}

export type MessageStatus = 'streaming' | 'done' | 'error' | 'stopped' | 'truncated'

export interface ApprovalInfo {
  approval_id: string
  tool: string
  path?: string
  description?: string
  status?: 'pending' | 'approved' | 'denied'
  [key: string]: unknown
}

// 粘贴 / 拖拽的附件（图片或非图片文件）
export interface Attachment {
  name: string
  ext: string
  isImage: boolean
  size: number
  path: string          // 本地路径（非图片文件用，模型以 read_file 读取）
  dataUrl?: string      // 图片 base64（isImage=true 时必有），直接发给多模态模型
}

export interface ChatMessage {
  id: string
  role: Role
  createdAt: number
  content: string
  reasoning: string
  toolCalls: ToolCall[]
  approvals: ApprovalInfo[]
  usage?: { prompt_tokens: number; completion_tokens: number }
  status: MessageStatus
  error?: string
  messageId?: string
  finishReason?: string
  // 附件（图片 / 文件）：随消息一并展示与发送
  images?: Attachment[]
  // 重试 / 系统通知等提示
  notices?: Notice[]
}

export interface Session {
  id: string
  name: string
  kind: SessionKind
  // 工作区会话绑定的工作目录（绝对路径）；聊天会话为 null
  workspaceDir: string | null
  // 工作区沙盒策略；聊天会话为 null
  sandbox: SandboxPolicy | null
  messages: ChatMessage[]
  createdAt: number
  updatedAt: number
}

export interface ProviderInfo {
  id: string
  display_name: string
  description: string
  models: string[]
  default_model: string | null
  supports_vision: boolean
  /** 接口地址（空 = 尚未配置；本项目不内置服务商，由使用者自行填写） */
  base_url?: string
  /** 上下文窗口（token 数）；null 表示不限制（前端不显示占用条） */
  context_window?: number | null
}

export interface HealthInfo {
  status: string
  providers: string[]
}

// 工作区信息（后端 /api/workspace/info 返回）
export interface WorkspaceInfo {
  exists: boolean
  dir: string
  sandbox: SandboxPolicy
  memory_count: number
}

export interface ToolSchema {
  name: string
  description?: string
  parameters?: unknown
}

// SSE 事件联合（type 判别）
export type SSEEvent =
  | { type: 'message_start'; message_id: string }
  | { type: 'content'; text: string }
  | { type: 'reasoning'; text: string }
  | { type: 'tool_call'; id: string; name: string; label: string; preview: string; status: 'running'; kind?: ToolKind; command?: string; partial?: boolean }
  | { type: 'tool_result'; id: string; status: 'success' | 'error'; preview: string }
  | { type: 'usage'; prompt_tokens: number; completion_tokens: number }
  | { type: 'approval_required'; approval_id: string; tool: string; [k: string]: unknown }
  | { type: 'error'; message: string }
  | { type: 'cancelled' }
  | { type: 'retry'; attempt: number; max: number; delay: number; reason: string; is_rate_limit?: boolean; reset?: boolean }
  | { type: 'task_update'; session_id: string; tasks: TaskItem[] }
  | { type: 'scheduled_run'; title?: string; detail?: string; [k: string]: unknown }
  | { type: 'message_end'; finish_reason?: string | null }

export interface ChatRequestBody {
  message: string
  provider?: string
  model?: string | null
  history: { role: string; content: string }[]
  // 多模态：内联图片 data URL 列表（发给支持视觉的模型）
  images?: string[]
  session_id?: string | null
  session_name?: string | null
  kind?: string
  workspace_dir?: string | null
  sandbox?: {
    allow_file_write?: boolean
    allow_file_delete?: boolean
    allow_shell?: boolean
  } | null
}
