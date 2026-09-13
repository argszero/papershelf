/** 状态 / 转换状态 / 时间格式 —— 全站唯一的文案与配色来源。
 *
 * 颜色一律写成 `var(--st-*)`（不是字面色值）：内联 style 里也能用 CSS 变量，
 * 于是**调色板只存在于 styles.css 一处**，甜甜圈、堆叠条、看板圆点、
 * 状态药丸永远不会互相漂移（原型里同一套色被复制了四遍）。
 */

import type { ConvState, PaperStatus } from './types'

interface Vocab {
  /** 中文标签（界面与看板列头共用） */
  label: string
  /** 药丸 / 圆点的 CSS 类 */
  cls: string
  /** 数据可视化用的颜色（CSS 变量） */
  color: string
}

/** ⑰ 四态阅读阶段 —— 看板四列的顺序就是这个数组的顺序。 */
export const STATUS: Record<PaperStatus, Vocab> = {
  unread:   { label: '待读',   cls: 'st-unread',  color: 'var(--st-unread)' },
  reading:  { label: '在读',   cls: 'st-reading', color: 'var(--st-reading)' },
  read:     { label: '已读',   cls: 'st-read',    color: 'var(--st-read)' },
  reviewed: { label: '已整理', cls: 'st-review',  color: 'var(--st-review)' },
}

export const STATUS_ORDER: PaperStatus[] = ['unread', 'reading', 'read', 'reviewed']

/** 「已精读」的定义：已读 + 已整理（看板与总览共用一个口径）。 */
export function isDone(s: PaperStatus): boolean {
  return s === 'read' || s === 'reviewed'
}

export const CONV: Record<ConvState, { label: string; cls: string; color: string }> = {
  none:   { label: '待转换', cls: 'st-unread',     color: 'var(--st-unread)' },
  queued: { label: '排队中', cls: 'st-converting', color: 'var(--warn)' },
  doing:  { label: '转换中', cls: 'st-converting', color: 'var(--warn)' },
  done:   { label: '已生成', cls: 'st-read',       color: 'var(--st-read)' },
  failed: { label: '转换失败', cls: 'st-failed',   color: 'var(--danger)' },
}

export const CONV_ORDER: ConvState[] = ['done', 'doing', 'queued', 'none', 'failed']

/** 把字数压到 `n` 个字以内，用于卡片标题。 */
export function clip(s: string | null | undefined, n = 18): string {
  const t = (s || '').trim()
  return t.length > n ? `${t.slice(0, n)}…` : t
}

const DAY = 86_400_000

/** 后端时间戳是 ISO（UTC，秒级精度）→ 一律按**本地时间**显示。 */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 相对时间（「今天 / 3 天前 / 2 个月前」）。
 *
 * ⚠️ 必须按**本地日历日**相减，不能按「毫秒差 / 一天」四舍五入（2026-09-12 修）。
 * 后端时间戳是 UTC（`2026-09-11T14:35Z` = 北京 22:35 当天），而 `Date.now()` 是本地时刻：
 * 过了 UTC 零点（北京 08:00）后毫秒差就 >1 天，昨夜导入的文献集体显示成「昨天」，
 * 对北京用户而言那明明是**今天**。按本地零点对齐后，"今天/昨天"与人的认知一致。
 */
export function relTime(iso: string | null | undefined): string {
  const d = _localDayDiff(iso)
  if (d === null) return '—'
  if (d <= 0) return '今天'
  if (d === 1) return '昨天'
  if (d < 30) return `${d} 天前`
  if (d < 365) return `${Math.floor(d / 30)} 个月前`
  return `${Math.floor(d / 365)} 年前`
}

/** 本地日历日之差（今天 = 0，昨天 = 1）。时间戳非法时返回 null。
 *
 * 与 `relTime` 同一套口径 —— 两处各写一遍第一个就会漂（一个按日历日、一个按毫秒，
 * 于是"今天"和"0 天前"能同时出现在同一屏）。 */
function _localDayDiff(iso: string | null | undefined): number | null {
  if (!iso) return null
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return null
  const a = new Date(then.getFullYear(), then.getMonth(), then.getDate()).getTime()
  const now = new Date()
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  return Math.round((b - a) / DAY)
}

/** 已过去的天数（用于「转换后 N 天未开始阅读」这类提醒）。与 `relTime` 同口径。 */
export function daysSince(iso: string | null | undefined): number | null {
  return _localDayDiff(iso)
}

/** 本地日期键 `YYYY-MM-DD`（阅读节奏按**本地日**分桶，与用户感知一致）。 */
export function dayKey(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 周起始（周一）。 */
export function startOfWeek(d: Date): Date {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const dow = (x.getDay() + 6) % 7
  x.setDate(x.getDate() - dow)
  return x
}
