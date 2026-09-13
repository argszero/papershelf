/**
 * 划痕的坐标换算（决策㉛）—— 前端**唯一**需要懂"裸文本字符偏移"的地方。
 *
 * ## 为什么需要这个文件
 *
 * 划痕的坐标是块**裸文本**（`blocks.en` / `blocks.zh`）的字符偏移，
 * 而 DOM 里的文本与裸文本**不是一一对应**的：公式在服务端被渲染成了 MathML
 * （`\(x^2\)` 三个字符变成 `<math>` 里的一堆节点）。所以用户在屏幕上划的选区，
 * 要变成可持久化的 `(start, end)`，必须有一把尺子。
 *
 * 尺子就是渲染器自己吐出的零宽锚点 `<span class="o" data-o="N">`（见
 * `pipeline/markup.prose_html`）：按文档序走一遍文本节点、累加字符数、
 * 遇到锚点就把计数拨到 N、遇到 `<math>` 整棵跳过。于是：
 *
 * - 文本节点的每个字符都有确定的裸文本偏移；
 * - 公式是**原子**：光标落在它内部时，吸附到它的两端（整公式高亮，
 *   绝不出现"半个公式"）。前端吸附 + 服务端原子渲染，两条规则互相兜底。
 */

import type { HlColor, Lang, Mark } from './types'

/** 四支笔。顺序 = 色板里的显示顺序（**不带含义**，只是好看）。 */
export const HL_COLORS: HlColor[] = ['amber', 'green', 'blue', 'pink']

/** 笔色中文名（无障碍标签用；界面上不显示文字，避免诱导"选颜色要想一想"）。 */
export const HL_LABEL: Record<HlColor, string> = {
  amber: '琥珀黄', green: '青绿', blue: '靛蓝', pink: '玫红',
}

export type Seg = {
  blockId: string
  lang: Lang
  start: number
  end: number
  quote: string
}

type Run = { node: Text; start: number; end: number }

const isMath = (n: Node) => n.nodeType === 1 && (n as Element).tagName.toLowerCase() === 'math'

/** 划痕容器（一块的一种语言）：`<div class="b-inline" data-lang="en">`。 */
export function containerOf(node: Node | null | undefined): HTMLElement | null {
  if (!node) return null
  const el = node.nodeType === 1 ? (node as HTMLElement) : node.parentElement
  return el?.closest<HTMLElement>('.b-inline[data-lang]') ?? null
}

/** 容器里的文本节点 → 裸文本偏移。顺序与 `prose_html` 生成锚点的顺序一致。 */
function runs(container: HTMLElement): Run[] {
  const out: Run[] = []
  let pos = 0
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (n: Node) => (isMath(n) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  })
  let n = walker.nextNode()
  while (n) {
    if (n.nodeType === 1) {
      const v = (n as HTMLElement).dataset?.o
      // 锚点把计数**拨**到它的值（不是加）：这就是尺子的刻度
      if (v !== undefined) pos = Number(v)
    } else {
      const t = n as Text
      out.push({ node: t, start: pos, end: pos + t.data.length })
      pos += t.data.length
    }
    n = walker.nextNode()
  }
  return out
}

/** 公式单元的边界 = 它两侧的锚点（渲染器保证公式前后各有一个）。 */
function mathBounds(container: HTMLElement, math: Element): [number, number] | null {
  let start: number | null = null
  let end: number | null = null
  for (const a of container.querySelectorAll<HTMLElement>('.o')) {
    const rel = a.compareDocumentPosition(math)
    if (rel & Node.DOCUMENT_POSITION_FOLLOWING) start = Number(a.dataset.o)
    else if ((rel & Node.DOCUMENT_POSITION_PRECEDING) && end === null) end = Number(a.dataset.o)
  }
  if (start === null || end === null) return null
  return [start, end]
}

/**
 * 把一个 DOM 光标位（`node` + `offset`，即 `Range` 的边界点）换算成裸文本偏移。
 *
 * `bias` 只在两种**没有唯一答案**的情况下起作用：
 * 1. 光标落在公式内部 —— `start` 取公式头、`end` 取公式尾（公式是原子）；
 * 2. 光标落在元素边界（拖选时常出现在 `<mark>` / 段落两端）—— 取相邻文本的起点或终点。
 */
export function caretOffset(container: HTMLElement, node: Node, offset: number,
                            bias: 'start' | 'end'): number | null {
  const rs = runs(container)
  if (node.nodeType === 3) {
    const run = rs.find((r) => r.node === node)
    if (run) return run.start + offset
    // 公式内部（MathML 自己的文本节点）→ 吸附到整公式
    const math = (node as Text).parentElement?.closest('math')
    if (math) {
      const b = mathBounds(container, math)
      return b ? b[bias === 'start' ? 0 : 1] : null
    }
    return null
  }
  const caret = document.createRange()
  try { caret.setStart(node, offset) } catch { return null }
  caret.collapse(true)
  const before = (r: Run) => {
    const rr = document.createRange()
    rr.setStart(r.node, 0)
    rr.collapse(true)
    return caret.compareBoundaryPoints(Range.START_TO_START, rr) > 0
  }
  const prev = rs.filter(before).pop()
  const next = rs.find((r) => !before(r))
  return bias === 'start' ? (next?.start ?? prev?.end ?? null) : (prev?.end ?? next?.start ?? null)
}

/**
 * 当前选区 → 每段一道划痕（跨段的选区**内部拆开**）。
 *
 * 为什么拆开而不是"一道跨段划痕"：坐标是**每块每语言一份裸文本**的偏移，
 * 块与块之间没有共同的坐标系 —— 跨段划痕在数据模型里本来就是好几道。
 * 界面上看不出区别（同色连成一片），但"删除"只删你点中的那一段，这是可接受的。
 */
export function selectionSegments(stage: HTMLElement): Seg[] {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return []
  const r = sel.getRangeAt(0)
  if (!stage.contains(r.commonAncestorContainer)) return []

  const startEl = containerOf(r.startContainer)
  const endEl = containerOf(r.endContainer)
  const targets = startEl && startEl === endEl
    ? [startEl]                                        // 常见路径：一段之内
    : Array.from(stage.querySelectorAll<HTMLElement>('.b-inline[data-lang]'))
        .filter((el) => r.intersectsNode(el))          // 跨段：只挑真正相交的那些

  const out: Seg[] = []
  for (const el of targets) {
    const blockId = el.closest<HTMLElement>('[data-b]')?.dataset.b || ''
    if (!blockId) continue
    const sub = document.createRange()
    if (el.contains(r.startContainer)) sub.setStart(r.startContainer, r.startOffset)
    else sub.setStart(el, 0)
    if (el.contains(r.endContainer)) sub.setEnd(r.endContainer, r.endOffset)
    else sub.setEnd(el, el.childNodes.length)
    if (sub.collapsed) continue
    const start = caretOffset(el, sub.startContainer, sub.startOffset, 'start')
    const end = caretOffset(el, sub.endContainer, sub.endOffset, 'end')
    if (start === null || end === null || end <= start) continue
    out.push({
      blockId,
      lang: el.dataset.lang === 'zh' ? 'zh' : 'en',
      start,
      end,
      quote: sub.toString().replace(/\s+/g, ' ').trim().slice(0, 60),
    })
  }
  return out
}

/** 划痕元素的定位选择器（跳转/闪烁都靠它）。 */
export const markSelector = (id: number) => `[data-h="${id}"]`

/** 已有划痕里**正好覆盖**这一段的那一道（用来判断"这一段已经有笔了"）。 */
export function sameRange(marks: Mark[], seg: Seg): Mark | undefined {
  return marks.find((m) => m.block_id === seg.blockId && m.lang === seg.lang
    && m.start === seg.start && m.end === seg.end)
}
