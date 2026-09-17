/**
 * 块级数据模型 —— 与服务端 `repo.doc_public` 的形状一一对应（决策⑳）。
 *
 * ⚠️ 这些类型必须与后端保持同步：后端改字段名而这里没改，
 * TypeScript 不会报错，只会在运行时静默变成 undefined。
 * 对应的后端源码：`src/papershelf/server/repo.py`。
 */

export type BlockType =
  | 'meta' | 'abstract' | 'h1' | 'h2' | 'h3' | 'h4'
  | 'p' | 'figure' | 'table' | 'eq'
  /** 参考文献区里**尚未切出条目**的碎片块：整块保持原文（服务端 `validate.NO_ZH_TYPES`），
   *  中文栏回落英文 —— 前端据此「单栏横跨」。 */
  | 'refs'
  /** **一条完整的文献条目**（解析 v13，`parse.merge_ref_entries`）：**只译标题**，
   *  作者/期刊/卷期页/DOI 保留原文，所以中文栏 = "原条目 + 中文标题"（两栏只差标题）。
   *  ⚠️ 它**不是** `no_zh`：译出来了就有中文栏，没译出来（`zh` 为空）才单栏横跨。 */
  | 'ref'
  /** 页边装饰图（v11）：出版社/期刊的**矢量标识**栅格化成的透明 PNG
   *  （`parse._margin_graphics`）。没有文字、免中文、不参与校验。 */
  | 'deco'

export type ZhSource = 'none' | 'mt' | 'human'

export interface Block {
  id: string
  type: BlockType
  level: number
  section: string
  en: string
  zh: string
  zh_source: ZhSource
  payload: Record<string, unknown>
  /** 校验/重试后仍存疑 → 显示「待校对」徽标（决策⑯ 的兜底入口） */
  needs_review: boolean
  /** 免中文块（`refs` 碎片 / 纯公式 / 装饰图）→ 单栏横跨，不参与左右配对。
   *  ⚠️ `ref`（完整文献条目）**不算**免中文块 —— 它只译标题，没译出来时
   *  前端按 `zh` 为空自行走单栏横跨（不是靠这个字段）。 */
  no_zh: boolean
  /** **只有表格块**有：中文网格是否真的可用（形状一致 + 不是逐格照抄英文）。
   *  对照模式据此决定渲不渲右边那张中文表 —— 判据在服务端（`model.table_zh_usable`），
   *  前端不再自己判一遍（否则是第二份判据，会与渲染漂开）。 */
  table_zh?: boolean
  /** 服务端渲染好的块 HTML（公式已是 MathML、图片 src 已改写）。
   *  只有 `?html=1`（默认）的响应才有这两个字段——前端不自己排版公式。 */
  en_html?: string
  zh_html?: string
}

export interface PaperDoc {
  paper_id: number
  meta: Record<string, string>
  /** 资产表。`ratio` = 宽高比，供**图片加载前**占位（否则 lazy 图加载时
   *  整篇高度突变，大纲跳转落点会偏掉）。 */
  assets: Array<{ name: string; width?: number; height?: number; ratio?: number; page?: number }>
  blocks: Block[]
}

export interface Plan {
  id: number
  name: string
  goal: number | null
  description: string
  created_at: string
  updated_at: string
  paper_count: number
  done_count: number
}

export type PaperStatus = 'unread' | 'reading' | 'read' | 'reviewed'
export type ConvState = 'none' | 'queued' | 'doing' | 'done' | 'failed'

export interface Paper {
  id: number
  plan_id: number
  title: string
  authors: string | null
  venue: string | null
  year: number | null
  tags: string[]
  source: 'upload' | 'arxiv'
  source_ref: string | null
  status: PaperStatus
  status_at: string | null
  progress: number
  progress_mode: 'auto' | 'manual'
  /** 「最近阅读」（⑰ 补充）：只有阅读器滚动上报写它；从没读过为 null。 */
  last_read_at: string | null
  conv_state: ConvState
  conv_error: string | null
  created_at: string
}

export interface Note {
  id: number
  paper_id: number
  block_id: string | null
  content: string
  created_at: string
  /** 选区锚点（决策㉛）：`lang` + 裸文本字符偏移 `[start, end)`；文献级/块级笔记为 null。 */
  lang?: Lang | null
  start?: number | null
  end?: number | null
  /** 写笔记时选中的那段原文摘录，列表里以「…」回显。 */
  quote?: string | null
  /** 这次笔记是写在哪道划痕上的（划痕被擦掉后置 null，笔记本身留着）。 */
  hl_id?: number | null
}

/** 划痕的语言侧：原文 / 译文。中英各划各的（两侧没有字级对应，不该连着亮）。 */
export type Lang = 'en' | 'zh'

/** 笔色（决策㉛）。**不带含义** —— 宿主原话「好看的几种颜色、没有含义」，
 *  所以没有"黄=重点"这类语义，也就没有图例；四支笔完全等价。 */
export type HlColor = 'amber' | 'green' | 'blue' | 'pink'

/** 一道划痕：`(block_id, lang, start, end)` 是坐标，`color` 只是外观。
 *  坐标是块**裸文本**（`blocks.en` / `blocks.zh`）的字符偏移 —— 与服务端渲染
 *  `<mark>` 时用的是同一套（`pipeline/markup.prose_html`）。 */
export interface Mark {
  id: number
  paper_id: number
  block_id: string
  lang: Lang
  start: number
  end: number
  color: HlColor
  created_at: string
}

export interface GlossaryEntry {
  en: string
  zh: string
  note?: string
}

/** 分享链接状态（服务端算好的 `state`，见 `routers/shares.py::_share_row`）。
 *
 * `expiring` 是**服务端**判定的"剩余不足 1 小时"—— 三处显示（分享页 banner /
 * 管理页表格 / 新建结果）共用同一个阈值，不各自比一遍。 */
export type ShareState = 'active' | 'expiring' | 'expired' | 'revoked'

export interface Share {
  token: string
  plan_id: number
  /** 跨计划列表（`GET /api/shares`）才带；计划内列表没有 */
  plan_name?: string
  /** 备注（"给导师看 / 组会前"），可空 */
  label: string | null
  /** 本次续期用了多少小时；存量老链接为 null（不编数字） */
  hours: number | null
  created_at: string
  expires_at: string | null
  revoked_at: string | null
  url: string
  state: ShareState
  /** 剩余秒数（服务端算好，前端只负责每秒递减） */
  seconds_left: number
}

/** 分享页载荷里随链接一起下发的有效期信息（匿名访客没有管理端可查）。 */
export interface ShareMeta {
  token: string
  label: string
  expires_at: string | null
  seconds_left: number
}

export interface CurrentUser {
  id: number
  email: string
  display_name: string | null
  is_admin: boolean
  status: string
}

export interface TermsSection {
  title: string
  text: string
}

export interface AuthConfig {
  open_registration: boolean
  smtp_configured: boolean
  allowed_domains: string[]
  // 用户协议（2026-09-11）：正文由服务端提供 —— 前端不复制一份，避免两处漂移。
  terms_version: string
  terms_summary: string
  terms_body: TermsSection[]
}

/** 发验证码的响应。`cooldown` 是前端倒计时的秒数（服务端说了算，前端不自己编）。 */
export interface CodeSent {
  sent: boolean
  cooldown: number
}

export interface AdminUser {
  id: number
  email: string
  display_name: string | null
  status: 'pending' | 'active' | 'disabled'
  is_admin: boolean
  created_at: string
  activated_at: string | null
}
