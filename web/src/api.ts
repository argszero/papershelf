/**
 * 极简 API 客户端 —— 只做四件事：拼 URL、带 cookie、解析错误、类型标注。
 *
 * 刻意不引入 axios / react-query：请求形态很少（十来个端点），
 * 加一层库只会让"错误怎么呈现"这件事被藏起来（决策⑨：保持单体简单）。
 */

import type {
  AdminUser, AuthConfig, Block, CodeSent, CurrentUser, GlossaryEntry, HlColor, Lang, Mark, Note,
  Paper, PaperDoc, Plan, Share, ShareMeta,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: init.body && !(init.body instanceof FormData)
      ? { 'Content-Type': 'application/json' }
      : undefined,
    ...init,
  })
  if (res.status === 204) return undefined as T
  const text = await res.text()
  let data: unknown = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) {
    const detail = (data as { detail?: string } | null)?.detail
    throw new ApiError(res.status, detail || `请求失败（${res.status}）`)
  }
  return data as T
}

const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) })
const patch = (body: unknown): RequestInit => ({ method: 'PATCH', body: JSON.stringify(body) })

export const api = {
  // ── 认证 ──
  authConfig: () => request<AuthConfig>('/api/auth/config'),
  me: () => request<CurrentUser>('/api/auth/me'),
  login: (email: string, password: string) =>
    request<CurrentUser>('/api/auth/login', json({ email, password })),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  // ⑭ 邮箱验证码（对齐原型）：注册与忘记密码各一套发码/提交
  registerCode: (email: string) =>
    request<CodeSent>('/api/auth/register/code', json({ email })),
  register: (email: string, code: string, password: string, display_name = '', agree = true) =>
    request<CurrentUser>('/api/auth/register',
      json({ email, code, password, display_name, agree })),
  resetCode: (email: string) =>
    request<CodeSent>('/api/auth/reset/code', json({ email })),
  resetPassword: (email: string, code: string, password: string) =>
    request<{ ok: boolean }>('/api/auth/reset', json({ email, code, password })),

  // ── 计划 ──
  plans: () => request<Plan[]>('/api/plans'),
  plan: (id: number) => request<Plan>(`/api/plans/${id}`),
  createPlan: (name: string, description = '', goal: number | null = null) =>
    request<Plan>('/api/plans', json({ name, description, goal })),
  updatePlan: (id: number, body: Partial<Pick<Plan, 'name' | 'description' | 'goal'>>) =>
    request<Plan>(`/api/plans/${id}`, patch(body)),
  deletePlan: (id: number) => request<void>(`/api/plans/${id}`, { method: 'DELETE' }),
  glossary: (planId: number) => request<{ glossary: GlossaryEntry[] }>(`/api/plans/${planId}/settings`),
  setGlossary: (planId: number, glossary: GlossaryEntry[]) =>
    request<{ glossary: GlossaryEntry[] }>(`/api/plans/${planId}/settings`, patch({ glossary })),

  // ── 文献 ──
  papers: (planId: number) => request<Paper[]>(`/api/plans/${planId}/papers`),
  paper: async (id: number) => {
    // 后端没有单篇 GET，用 doc 端点带出的 meta；列表页的数据从 plans 列表里取
    const doc = await request<PaperDoc>(`/api/papers/${id}/doc`)
    return doc
  },
  updatePaper: (id: number, body: Partial<Paper> & { status_?: string }) =>
    request<Paper>(`/api/papers/${id}`, patch(body)),
  deletePaper: (id: number) => request<void>(`/api/papers/${id}`, { method: 'DELETE' }),
  uploadPapers: (planId: number, files: File[], tags = '') => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    fd.append('tags', tags)
    fd.append('background_convert', 'true')
    return request<Paper[]>(`/api/plans/${planId}/papers/upload`, { method: 'POST', body: fd })
  },
  importArxiv: (planId: number, ref: string) =>
    request<Paper>(`/api/plans/${planId}/papers/arxiv`, json({ ref })),
  retryConvert: (id: number) =>
    request<{ ok: boolean; conv_state: string }>(`/api/papers/${id}/convert`, { method: 'POST' }),
  // 重新提取：清解析缓存 + 作废笔记/划痕，从头再跑一遍管线（有 token 代价）
  reextractPaper: (id: number) =>
    request<{ ok: boolean; conv_state: string; cache_cleared: boolean }>(
      `/api/papers/${id}/reextract`, { method: 'POST' }),
  // 重建参考文献（㊹ 修订的存量出口，宿主 2026-09-19 点单）：只重跑**文末文献段**的
  // 条目合并 —— 正文块 id 不变 ⇒ **笔记与划痕一条不动**，代价只有新增条目的翻译。
  // 异步（几百条要跑几分钟），完成后 `conv_state` 回到 `done`（与重新提取同一套观感）。
  rebuildRefs: (id: number) =>
    request<{ ok: boolean; conv_state: string; job: string }>(
      `/api/papers/${id}/rebuild-refs`, { method: 'POST' }),

  // ── 阅读（块级 JSON 是唯一事实来源，决策⑳）──
  doc: (paperId: number) => request<PaperDoc>(`/api/papers/${paperId}/doc`),
  /** 单块重取（㉛）：划一道之后要让屏幕出现 `<mark>`，而 `<mark>` 是**服务端**渲染的。
   *  重拉整篇太大（900 块的文档几百 KB），所以只重拉这一块。 */
  block: (paperId: number, blockId: string) =>
    request<Block>(`/api/docs/${paperId}/blocks/${blockId}`),
  // 两个端点都回**完整块**（含服务端重渲染的 en_html/zh_html）→ 前端整块替换
  editBlock: (paperId: number, blockId: string, zh: string, reconciled = true) =>
    request<Block>(`/api/docs/${paperId}/blocks/${blockId}`, patch({ zh, reconciled })),
  retranslateBlock: (paperId: number, blockId: string) =>
    request<Block>(`/api/docs/${paperId}/blocks/${blockId}/retranslate`, { method: 'POST' }),

  // ── 笔记（⑯ 块锚点 + ㉛ 区间锚点）──
  notes: (paperId: number) => request<Note[]>(`/api/papers/${paperId}/notes`),
  /** `anchor` 有则锚到一段选区，没有则锚到整块/整篇（三档锚点，见 `routers/notes.py`）。 */
  /** 写笔记。给**选区**写笔记时 `color` 是"顺手补一道划痕"用的那支笔
   *  （服务端只有在该区间还没有划痕、且没带 `hl_id` 时才用得上，见 ㉛）。 */
  addNote: (paperId: number, content: string, anchor: {
    block_id?: string | null; lang?: Lang; start?: number; end?: number
    quote?: string | null; hl_id?: number | null; color?: HlColor
  } = {}) => request<Note>(`/api/papers/${paperId}/notes`, json({ content, ...anchor })),
  editNote: (noteId: number, content: string) =>
    request<Note>(`/api/notes/${noteId}`, patch({ content })),
  deleteNote: (noteId: number) => request<void>(`/api/notes/${noteId}`, { method: 'DELETE' }),

  // ── 划痕（㉛）：任意字符区间 + 一支颜色笔 ──
  marks: (paperId: number) => request<Mark[]>(`/api/papers/${paperId}/highlights`),
  addMark: (paperId: number, body: {
    block_id: string; lang: Lang; start: number; end: number; color?: HlColor
  }) => request<Mark>(`/api/papers/${paperId}/highlights`, json(body)),
  /** 换一支笔：只改颜色，端点不动（所以这一道上的笔记锚点不受影响）。 */
  setMarkColor: (hlId: number, color: HlColor) =>
    request<Mark>(`/api/highlights/${hlId}`, patch({ color })),
  /** 擦掉一道划痕。**幂等**（服务端对不存在的 id 也回 200）。 */
  removeMark: (hlId: number) =>
    request<{ id: number; deleted: boolean }>(`/api/highlights/${hlId}`, { method: 'DELETE' }),

  // ── 分享（⑩⑪ + 2026-09-12 分享管理）──
  // 有效期单位为**小时**，服务端硬上限 24（2026-09-11）。
  shares: (planId: number) => request<Share[]>(`/api/plans/${planId}/shares`),
  /** 跨计划的全部分享（分享管理页）—— 带 `plan_name`，不必逐计划取数再拼 */
  allShares: () => request<Share[]>('/api/shares'),
  createShare: (planId: number, hours = 0, label = '') =>
    request<Share>(`/api/plans/${planId}/shares`, json({ hours, label })),
  /** 重置有效期：从**现在**起算（服务端保证，不是从原到期时间顺延） */
  renewShare: (token: string, hours: number, label?: string) =>
    request<Share>(`/api/shares/${token}/renew`,
      json(label === undefined ? { hours } : { hours, label })),
  revokeShare: (token: string) =>
    request<void>(`/api/shares/${token}`, { method: 'DELETE' }),

  // ── 只读分享视图（匿名可访问）──
  // ⚠️ 这三个端点的返回形状**必须与登录态一一对应**（`plans/papers/doc/notes`）：
  //    分享页复用整站页面（⑩「和分享者看到的一模一样」），那些页面读的是完整
  //    `Plan` / `Paper`。这里若退化成精简子集，页面会渲染出 `undefined` 而 TS 不报错。
  // `share` 里带失效时间：分享页 banner 的倒计时读它（匿名访客没有管理端可查）
  sharedPlan: (token: string) =>
    request<{ plan: Plan; papers: Paper[]; readonly: boolean; share: ShareMeta }>(
      `/api/shares/${token}`),
  sharedPlans: (token: string) => request<Plan[]>(`/api/shares/${token}/plans`),
  sharedDoc: (token: string, paperId: number) =>
    request<PaperDoc>(`/api/shares/${token}/papers/${paperId}`),
  sharedNotes: (token: string, paperId: number) =>
    request<Note[]>(`/api/shares/${token}/papers/${paperId}/notes`),
  sharedHighlights: (token: string, paperId: number) =>
    request<Mark[]>(`/api/shares/${token}/papers/${paperId}/highlights`),
  sharedExportUrl: (token: string, paperId: number, lang: string) =>
    `/api/shares/${token}/papers/${paperId}/export?lang=${lang}`,

  exportUrl: (paperId: number, lang: string, download = false) =>
    `/api/papers/${paperId}/export?lang=${lang}${download ? '' : '&view=1'}`,

  // ── 管理（㉑）──
  adminUsers: () => request<AdminUser[]>('/api/admin/users'),
  adminCreateUser: (email: string, password: string, display_name = '', is_admin = false) =>
    request<AdminUser>('/api/admin/users', json({ email, password, display_name, is_admin })),
  adminPatchUser: (id: number, body: { status_?: string; password?: string; is_admin?: boolean }) =>
    request<AdminUser>(`/api/admin/users/${id}`, patch(body)),
}
