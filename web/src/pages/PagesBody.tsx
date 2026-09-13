/** PDF 分页容器 —— 把连续的块按 `payload.page` 切回原型那样的「页」。
 *
 * 原型（`literature-workbench.html` L1813）把正文渲染成一串
 * `<section class="pdf-page">` 加底部「第 N 页 / 共 M 页」页脚；我们的块是**流式**的，
 * 页码来自解析阶段（`payload.page`，见 `pipeline/parse.py` 的 `_paged_adder`）。
 * 这里只负责**按页码分组 + 套上同一套 `.pdf-page` / `.page-foot` 版式**，
 * 块的内部渲染仍由调用方决定（阅读器要挂块工具栏、分享页不要）。
 *
 * 分组逻辑在 `pageGroups.ts`（纯函数，便于单测与 Fast Refresh）。
 */

import type { ReactNode } from 'react'

/** 页容器。`total` 是 PDF 总页数（`doc.meta.pages`），未知则只显当前页。 */
export function Page({ no, total, children }: { no: number; total: number; children: ReactNode }) {
  const has = no > 0
  return (
    <section className="pdf-page" data-page={has ? no : undefined}>
      <div className="page-body">{children}</div>
      {has && (
        <div className="page-foot">
          <span className="pf-rule" />
          <span className="pf-no">
            第 {no} 页{total > 0 ? ` / 共 ${total} 页` : ''}
          </span>
        </div>
      )}
    </section>
  )
}
