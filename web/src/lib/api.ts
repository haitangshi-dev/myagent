import type {
  HealthInfo,
  ProviderInfo,
  ToolSchema,
  SandboxPolicy,
  WorkspaceInfo,
  TaskItem,
} from './types'

// 技能市场（SkillHub）相关类型
export interface SkillMarketItem {
  name?: string
  slug?: string
  description?: string
  description_zh?: string
  category?: string
  source?: string
  installs?: number
  homepage?: string
  stars?: number
  version?: string
  iconUrl?: string
  [k: string]: unknown
}

export interface SkillCategory {
  id: string
  label: string
}

export interface InstalledSkill {
  slug: string
  name: string
  description: string
  category: string
  source: string
  homepage: string
  version: string
  icon_url: string
  installed_at: string
  /** 是否装到了真实技能包（含 SKILL.md 正文）；false 表示仅元数据存根 */
  real?: boolean
}

export interface SkillMarketSearchResult {
  skills: SkillMarketItem[]
  total: number
  page: number
  pageSize: number
}

// 定时任务（scheduler.py Job.to_dict 对齐）
export interface ScheduleJob {
  id: string
  name: string
  prompt: string
  when: string
  provider: string | null
  model: string | null
  enabled: boolean
  created_at: string
  last_run: string | null
  last_status: string | null
  next_run: string | null
  history: { run_at: string; status: string; preview: string }[]
  parse_error: string | null
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return (await res.json()) as T
}

async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return (await res.json()) as T
}

async function putJSON<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return (await res.json()) as T
}

export async function fetchProviders(): Promise<ProviderInfo[]> {
  const j = await getJSON<{ providers: ProviderInfo[] }>('/api/providers')
  return j.providers
}

export async function fetchHealth(): Promise<HealthInfo> {
  return getJSON<HealthInfo>('/api/health')
}

// 设置面板（provider / apikey）
export interface ProviderSetting {
  display_name?: string
  description?: string
  api_mode?: string
  base_url?: string
  /** 已掩码的 api_key，形如 ****xxxx；仅在用户未输入新密钥时展示 */
  api_key?: string
  has_api_key?: boolean
  default_model?: string
  models?: string[]
  [k: string]: unknown
}

export interface SettingsResponse {
  default_provider: string | null
  default_model: string | null
  providers: Record<string, ProviderSetting>
  /** override 文件实际路径（用户数据目录，重装不丢） */
  override_path: string
}

export async function fetchSettings(): Promise<SettingsResponse> {
  return getJSON<SettingsResponse>('/api/settings')
}

export async function saveSettingsOverride(
  override: Record<string, unknown>,
): Promise<{ ok: boolean; settings: Record<string, ProviderSetting> }> {
  return postJSON('/api/settings/override', { override })
}

// 后端按 OpenAI 工具 schema 返回：{ type:"function", function:{name,description,parameters} }
interface RawTool {
  function: { name: string; description?: string; parameters?: unknown }
}
export async function fetchTools(): Promise<ToolSchema[]> {
  const j = await getJSON<{ tools: RawTool[] }>('/api/tools')
  return j.tools.map((t) => ({
    name: t.function.name,
    description: t.function.description,
    parameters: t.function.parameters,
  }))
}

export async function postApprove(
  approval_id: string,
  decision: 'approve' | 'deny',
): Promise<boolean> {
  const res = await fetch('/api/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approval_id, decision }),
  })
  if (!res.ok) return false
  const j = (await res.json()) as { ok: boolean }
  return j.ok
}

/** 通知后端中断某会话的当前对话（前端点“停止”时调用）。 */
export async function postCancel(sessionId: string): Promise<void> {
  await fetch('/api/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  })
}

/** 测量一次对后端的往返延迟（ms），用于状态栏实时指示。 */
export async function measureLatency(): Promise<number> {
  const t0 = performance.now()
  try {
    await fetch('/api/health', { cache: 'no-store' })
  } catch {
    return -1
  }
  return Math.round(performance.now() - t0)
}

// =========================================================================
// 技能市场（SkillHub）
// =========================================================================
export async function getSkillCategories(): Promise<SkillCategory[]> {
  const j = await getJSON<{ categories: SkillCategory[] }>(
    '/api/skill-market/categories',
  )
  return j.categories
}

export async function searchSkillMarket(
  keyword = '',
  page = 1,
  pageSize = 12,
): Promise<SkillMarketSearchResult> {
  const params = new URLSearchParams({
    keyword,
    page: String(page),
    pageSize: String(pageSize),
  })
  return getJSON<SkillMarketSearchResult>(`/api/skill-market/search?${params}`)
}

export async function getInstalledSkills(): Promise<Record<string, InstalledSkill>> {
  const j = await getJSON<{ installed: Record<string, InstalledSkill> }>(
    '/api/skill-market/installed',
  )
  return j.installed || {}
}

export async function installSkill(skill: {
  slug: string
  name?: string
  description?: string
  description_zh?: string
  category?: string
  source?: string
  homepage?: string
  version?: string
  icon_url?: string
}): Promise<{ ok: boolean; slug: string; path: string; installed: InstalledSkill }> {
  const res = await fetch('/api/skill-market/install', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(skill),
  })
  if (!res.ok) {
    const msg = await res.text().catch(() => '安装失败')
    throw new Error(msg || `HTTP ${res.status}`)
  }
  return (await res.json()) as {
    ok: boolean
    slug: string
    path: string
    installed: InstalledSkill
  }
}

// =========================================================================
// 工作区会话：选择目录 + 沙盒配置 + 记忆
// =========================================================================

/**
 * 调起系统资源管理器选择工作目录。
 * - Electron 壳：走预加载桥接 ipcRenderer.invoke('dialog:selectFolder')，返回绝对路径。
 * - 纯浏览器：webkitdirectory 受安全限制无法取得绝对路径，故回退为 null（需在桌面端使用）。
 */
export async function selectFolder(): Promise<string | null> {
  const api = (window as unknown as { electronAPI?: { selectFolder?: () => Promise<string | null> } }).electronAPI
  if (api && typeof api.selectFolder === 'function') {
    try {
      return await api.selectFolder()
    } catch {
      return null
    }
  }
  // 浏览器回退：无法获得绝对目录路径
  if (typeof document !== 'undefined') {
    return new Promise<string | null>((resolve) => {
      const input = document.createElement('input')
      input.type = 'file'
      input.setAttribute('webkitdirectory', '')
      input.style.display = 'none'
      input.onchange = () => {
        input.remove()
        resolve(null)
      }
      document.body.appendChild(input)
      input.click()
    })
  }
  return null
}

export async function fetchWorkspaceInfo(dir: string): Promise<WorkspaceInfo> {
  const params = new URLSearchParams({ dir })
  return getJSON<WorkspaceInfo>(`/api/workspace/info?${params}`)
}

export async function putWorkspaceSandbox(
  dir: string,
  sandbox: SandboxPolicy,
): Promise<WorkspaceInfo> {
  const res = await fetch('/api/workspace/sandbox', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      dir,
      allow_file_write: sandbox.allow_file_write,
      allow_file_delete: sandbox.allow_file_delete,
      allow_shell: sandbox.allow_shell,
    }),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const j = (await res.json()) as { ok: boolean; sandbox: SandboxPolicy }
  return { exists: true, dir, sandbox: j.sandbox, memory_count: 0 }
}

export async function fetchWorkspaceMemory(
  dir: string,
  query = '',
  limit = 20,
): Promise<{ memory: unknown[] }> {
  const params = new URLSearchParams({ dir, query, limit: String(limit) })
  return getJSON<{ memory: unknown[] }>(`/api/workspace/memory?${params}`)
}

export async function clearWorkspaceMemory(dir: string): Promise<{ ok: boolean }> {
  const params = new URLSearchParams({ dir })
  const res = await fetch(`/api/workspace/memory?${params}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return (await res.json()) as { ok: boolean }
}

// =========================================================================
// 全局记忆（agent_memory.jsonl）可视化：list / add / update / delete / clear
// 条目以 uuid id 为唯一标识（source: auto|manual|import；status: active|superseded）
// =========================================================================
export interface MemoryItem {
  id: string
  ts: string
  content: string
  tags: string[]
  source?: string
  status?: string
  score?: number
}

export async function fetchMemory(query = '', limit = 300): Promise<MemoryItem[]> {
  const params = new URLSearchParams({ query, limit: String(limit) })
  const j = await getJSON<{ items: MemoryItem[] }>(`/api/memory?${params}`)
  return j.items || []
}

/** 按 id 删除一条记忆。 */
export async function deleteMemory(id: string): Promise<{ ok: boolean; reason?: string }> {
  const res = await fetch(`/api/memory/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!res.ok) {
    const t = await res.text().catch(() => '')
    return { ok: false, reason: t || `HTTP ${res.status}` }
  }
  return (await res.json()) as { ok: boolean; reason?: string }
}

/** 新增一条记忆（前端「+ 新增记忆」用）。 */
export async function addMemory(
  content: string,
  tags: string[] = [],
  source = 'manual',
): Promise<{ ok: boolean; entry?: MemoryItem; reason?: string }> {
  const res = await fetch(`/api/memory`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, tags, source }),
  })
  if (!res.ok) {
    const t = await res.text().catch(() => '')
    return { ok: false, reason: t || `HTTP ${res.status}` }
  }
  return (await res.json()) as { ok: boolean; entry?: MemoryItem }
}

/** 按 id 更新一条记忆（前端内联编辑用）。 */
export async function updateMemory(
  id: string,
  content: string,
  tags: string[],
): Promise<{ ok: boolean; entry?: MemoryItem; reason?: string }> {
  const res = await fetch(`/api/memory/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, tags }),
  })
  if (!res.ok) {
    const t = await res.text().catch(() => '')
    return { ok: false, reason: t || `HTTP ${res.status}` }
  }
  return (await res.json()) as { ok: boolean; entry?: MemoryItem }
}

export async function clearMemoryAll(): Promise<{ ok: boolean }> {
  const res = await fetch(`/api/memory/clear`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return (await res.json()) as { ok: boolean }
}

// =========================================================================
// 任务看板（task_board）+ 定时任务（scheduler）
// =========================================================================

/** 拉取某会话当前的任务清单（切换会话时调用，用于刷新看板）。 */
export async function fetchTasks(sessionId: string): Promise<TaskItem[]> {
  const params = new URLSearchParams({ session_id: sessionId })
  const j = await getJSON<{ session_id: string; tasks: TaskItem[] }>(
    `/api/tasks?${params}`,
  )
  return j.tasks || []
}

// ---------------------------------------------------------------------------
// 邮件网关（QQ 邮箱来信唤醒助手）
// ---------------------------------------------------------------------------
export interface EmailLogItem {
  ts: number
  id: string
  from: string
  subject: string
  reply: string
}

/** 一封完整的邮件消息（邮件网关归档，供「邮箱消息列表」展示）。 */
export interface EmailMessage {
  ts: number
  id: string
  from: string
  subject: string
  body: string
  /** replied=已自动回信 / failed=回信失败 / processed=已处理未回信 */
  status: 'replied' | 'failed' | 'processed' | string
  reply: string
  reply_error: string
}

/** 单轮轮询的真实结果（后端每轮都会产出，并经 SSE 实时推送）。 */
export interface EmailPollItem {
  seq: number
  trigger: 'auto' | 'manual' | string
  ts: number
  elapsed_ms: number
  ok: boolean
  total: number
  fresh: number
  processed: number
  error: string
  note: string
  rate_limited: boolean
}

export interface EmailStatus {
  enabled: boolean
  running: boolean
  cli_available: boolean
  provider: string
  model: string
  poll_interval: number
  auto_reply: boolean
  auto_approve: boolean
  last_poll: number
  seen_count: number
  last_error: string
  recent: EmailLogItem[]
  /** 完整邮件消息归档（最近 200 封，按时间倒序展示） */
  messages: EmailMessage[]
  me: Record<string, unknown>
  // 实时反馈
  polling: boolean
  poll_count: number
  next_poll: number
  last_result: EmailPollItem | Record<string, never>
  polls: EmailPollItem[]
  backoff_until: number
  server_time: number
}

/** 邮件网关状态（是否已连凭据、最近处理的邮件等）。 */
export async function fetchEmailStatus(): Promise<EmailStatus> {
  return getJSON<EmailStatus>('/api/email/status')
}

/** 立即手动拉一次新邮件并处理。 */
export async function checkEmailNow(): Promise<{
  ok: boolean
  processed?: number
  total?: number
  fresh?: number
  elapsed_ms?: number
  seq?: number
  note?: string
  error?: string
  busy?: boolean
  status?: EmailStatus
}> {
  return postJSON('/api/email/check', {})
}

/** 手动写入 QQ 邮箱 MCP token（自动扫描找不到凭据时的兜底）。 */
export async function setEmailToken(token: string): Promise<{ ok: boolean }> {
  return postJSON('/api/email/set-token', { token })
}

/** 运行时改邮件网关配置（enabled 变化会立即起停轮询）。 */
export async function updateEmailConfig(
  patch: Record<string, unknown>,
): Promise<{ ok: boolean; status: EmailStatus }> {
  return postJSON('/api/email/config', patch)
}

/** 列出所有定时任务。 */
export async function fetchSchedules(): Promise<ScheduleJob[]> {
  const j = await getJSON<{ jobs: ScheduleJob[] }>('/api/schedules')
  return j.jobs || []
}

/** 创建定时任务。when 支持 in/every/daily/weekly/cron 语法。 */
export async function createSchedule(input: {
  name?: string
  prompt: string
  when: string
  provider?: string | null
  model?: string | null
}): Promise<ScheduleJob> {
  const res = await fetch('/api/schedules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) {
    const msg = await res.text().catch(() => '创建失败')
    throw new Error(msg || `HTTP ${res.status}`)
  }
  const j = (await res.json()) as { ok: boolean; job: ScheduleJob }
  return j.job
}

/** 更新定时任务（任意字段可缺省）。 */
export async function updateSchedule(
  jobId: string,
  fields: {
    name?: string
    prompt?: string
    when?: string
    provider?: string | null
    model?: string | null
    enabled?: boolean
  },
): Promise<ScheduleJob> {
  const res = await fetch(`/api/schedules/${jobId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  })
  if (!res.ok) {
    const msg = await res.text().catch(() => '更新失败')
    throw new Error(msg || `HTTP ${res.status}`)
  }
  const j = (await res.json()) as { ok: boolean; job: ScheduleJob }
  return j.job
}

/** 删除定时任务。 */
export async function deleteSchedule(jobId: string): Promise<void> {
  const res = await fetch(`/api/schedules/${jobId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
}

/** 立即手动触发一次定时任务。 */
export async function runScheduleNow(jobId: string): Promise<ScheduleJob> {
  const res = await fetch(`/api/schedules/${jobId}/run`, { method: 'POST' })
  if (!res.ok) {
    const msg = await res.text().catch(() => '执行失败')
    throw new Error(msg || `HTTP ${res.status}`)
  }
  const j = (await res.json()) as { ok: boolean; job: ScheduleJob }
  return j.job
}

// =========================================================================
// 环境感知（ambient）：前台窗口标题，主动插话/上下文感知的信号源
// =========================================================================
export interface AmbientState {
  active_window: { title: string; app: string } | null
  timestamp: string
}

export async function fetchAmbientState(): Promise<AmbientState> {
  return getJSON<AmbientState>('/api/ambient/state')
}

// 主动搭话（proactive）：根据当前环境生成一句贴心中文搭话
export interface ProactiveSuggestion {
  suggestion: string
  context: { title?: string; app?: string } | null
}

export async function fetchProactiveSuggest(
  context: { active_window: { title: string; app: string } | null } | null,
): Promise<ProactiveSuggestion> {
  return postJSON<ProactiveSuggestion>('/api/proactive/suggest', { context })
}

// 每日摘要（daily）：汇总当日日期、前台窗口、任务看板进度，生成今日摘要
export interface DailySummaryResponse {
  summary: string
  date: string
  generated_at: string
}

export async function fetchDailySummary(
  sessionId?: string | null,
): Promise<DailySummaryResponse> {
  return postJSON<DailySummaryResponse>('/api/daily/summary', {
    session_id: sessionId ?? null,
  })
}

// =========================================================================
// 运行时模式：full（完整模式）/ minimal（极简模式）
// ★ 与「本地 / 云端」无关：本项目不提供推理后端，只区分助手的行为复杂度。
// =========================================================================
export async function fetchMode(): Promise<string> {
  const j = await getJSON<{ mode: string }>('/api/mode')
  return j.mode
}

export async function setMode(mode: 'full' | 'minimal'): Promise<string> {
  const j = await postJSON<{ mode: string }>('/api/mode', { mode })
  return j.mode
}

// =========================================================================
// 删除确认目录（2026-08-08）：high 权限下仍强制确认的目录（前端多选配置）
// =========================================================================
export interface DeleteConfirmDirsResponse {
  /** 用户配置的「删除需确认」目录 */
  dirs: string[]
  /** 系统保护目录（永远硬拦截，只读展示，不可移除） */
  protected: string[]
}

/** 拉取当前「删除需确认」目录 + 系统保护目录 */
export async function fetchDeleteConfirmDirs(): Promise<DeleteConfirmDirsResponse> {
  return getJSON<DeleteConfirmDirsResponse>('/api/security/delete_confirm_dirs')
}

/** 保存「删除需确认」目录列表（系统保护目录会被后端自动过滤） */
export async function saveDeleteConfirmDirs(
  dirs: string[],
): Promise<{ ok: boolean; dirs: string[] }> {
  return postJSON('/api/security/delete_confirm_dirs', { dirs })
}
