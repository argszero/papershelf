/** 只读分享上下文 —— 决策⑩⑪ 的**视图层**落点。
 *
 * 宿主 2026-09-11 的原话：「分享后的页面应该和分享者看到的一模一样，只是只读。」
 * 原型给出的答案也正是如此：进分享模式时**不是另做一个页面**，而是
 *   ① 整站外壳照旧（侧栏 + 顶栏 + 五个一级视图）；
 *   ② 挂一条 `.share-banner`，顶栏多一枚「只读分享」pill；
 *   ③ 所有写入口隐藏（原型 `body.readonly` 那一串选择器）；
 *   ④ 所有需要登录的取数换成 `/api/shares/{token}*` 白名单。
 *
 * 所以本文件只回答一个问题：**哪些行为要换、哪些要藏**。
 * 页面组件拿 `useShare()` 判空即可，不需要各自发明一套"分享态"。
 *
 * ⚠️ 为什么路由要在分享作用域下重挂一遍（`/share/:token/library`）：
 * 原型的分享是**同一份 `location`**（`#share=` 只是 hash，视图切换照旧），
 * 所以侧栏导航、行点击、卡片打开全部沿用组内链接。SPA 里等价的做法是把 token
 * 放进路径前缀，用 `path()` 统一改写 —— 否则匿名访客一点侧栏就被弹回登录页。
 */

import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useParams } from 'react-router-dom'

import { api } from './api'
import type { Mark, Note, PaperDoc, ShareMeta } from './types'

export interface ShareValue {
  /** 分享令牌（取数白名单的钥匙） */
  token: string
  /** 把站内路径映射到分享作用域：`/library` → `/share/<token>/library` */
  path: (p: string) => string
  loadDoc: (paperId: number) => Promise<PaperDoc>
  loadNotes: (paperId: number) => Promise<Note[]>
  /** 分享者的划痕（㉛）：只读页**看得见划痕**，只是不能改 */
  loadMarks: (paperId: number) => Promise<Mark[]>
  exportUrl: (paperId: number, lang: string) => string
  /** 这条链接自己的有效期信息（由 `PlanProvider` 取数时回填，banner 的倒计时读它） */
  meta: ShareMeta | null
  setMeta: (m: ShareMeta | null) => void
}

const Ctx = createContext<ShareValue | null>(null)

export function ShareProvider({ children }: { children: ReactNode }) {
  const { token = '' } = useParams()
  // `meta` 存放分享链接的到期时间：它随 `PlanProvider` 的取数一起回来，
  // 而 `PlanProvider` 在本 Provider **内层** —— 所以状态放在这里由它回填。
  // `setMeta` 直接透传（React 保证 setState 引用稳定）：`PlanProvider` 要靠它
  // 稳定才能在依赖数组里只用引用而非整个 `share` 对象（否则死循环）。
  const [meta, setMeta] = useState<ShareMeta | null>(null)

  const value = useMemo<ShareValue>(() => ({
    token,
    path: (p: string) => `/share/${token}${p.startsWith('/') ? p : `/${p}`}`,
    loadDoc: (paperId: number) => api.sharedDoc(token, paperId),
    loadNotes: (paperId: number) => api.sharedNotes(token, paperId),
    loadMarks: (paperId: number) => api.sharedHighlights(token, paperId),
    exportUrl: (paperId: number, lang: string) => api.sharedExportUrl(token, paperId, lang),
    meta,
    setMeta,
  }), [token, meta])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

/** 分享上下文；**登录态下是 `null`** —— 页面用 `share ? ... : ...` 分流。 */
export function useShare(): ShareValue | null {
  return useContext(Ctx)
}

/** 是否处于只读分享（登录态一律 false）。 */
export function useReadonly(): boolean {
  return useContext(Ctx) !== null
}

/** 站内链接的"作用域化"改写。登录态原样返回，分享态加 token 前缀。
 *
 * 页面里每次写 `to="/library"` 都改成 `to={link('/library')}` —— 这是分享页能
 * "像原站一样点"的唯一一处代价，集中在这里而不是散落成 `share ? ... : ...`。
 */
export function useLink(): (p: string) => string {
  const share = useContext(Ctx)
  return useCallback((p: string) => (share ? share.path(p) : p), [share])
}
