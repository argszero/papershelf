/** 按 PDF 页码把块分组 —— **纯函数，无 JSX**（单独放一个模块是为了让
 *  `PagesBody.tsx` 只导出组件，React Fast Refresh 才不会被非组件导出打断）。
 *
 * 页码来自解析阶段（`pipeline/parse.py` 的 `_paged_adder`，写入 `payload.page`）。
 * 降级：老文档（解析版本 2 之前落的库）块里没有 `page` → 归为第 1 组、
 * 页脚不显示页码，**绝不因为缺页码而少渲染内容**。
 */

import type { Block } from '../types'

export interface PageGroup {
  /** PDF 页码；`0` 表示"没有页码信息"（老文档降级） */
  no: number
  blocks: Block[]
}

/** 按 `payload.page` 把块切成页，**保持原顺序**。 */
export function groupByPage(blocks: Block[]): PageGroup[] {
  const pages: PageGroup[] = []
  for (const b of blocks) {
    const raw = (b.payload as Record<string, unknown> | undefined)?.page
    const no = typeof raw === 'number' && raw > 0 ? raw : 0
    const last = pages[pages.length - 1]
    if (last && last.no === no) last.blocks.push(b)
    else pages.push({ no, blocks: [b] })
  }
  return pages
}
