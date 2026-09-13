/** 分享状态的**唯一**文案与配色来源。
 *
 * 为什么独立一个模块：同一条链接会在多处出现（分享管理表格 / 新建分享抽屉 /
 * 分享页 banner），几处若各写一份 `state === 'active' ? ... : ...`，
 * 迟早有一处漏掉 `expiring` 这一态 —— 它就会显示成「已撤销」或空白。
 * （这正是一次真实缺陷的成因：已退役的 `SharePanel` 只认识三态。）
 *
 * 状态由**服务端**判定（`routers/shares.py::_share_row`），前端只负责翻译成中文。
 */

import type { ShareState } from '../types'

export const STATE_LABEL: Record<ShareState, string> = {
  active: '有效',
  expiring: '即将到期',
  expired: '已过期',
  revoked: '已取消',
}

/** 药丸配色复用全站 `st-*` 变量（与决策⑰ 的四态同一套色，不新造颜色）。 */
export const STATE_PILL: Record<ShareState, string> = {
  active: 'st-read',
  expiring: 'st-converting',
  expired: 'st-unread',
  revoked: 'st-unread',
}

/** 该状态是否仍可访问（`expiring` 只是快到期，链接还能用）。 */
export function isLive(s: ShareState): boolean {
  return s === 'active' || s === 'expiring'
}
