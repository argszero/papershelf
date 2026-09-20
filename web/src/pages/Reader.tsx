/** 精读阅读器 —— 决策⑤⑥⑯⑰⑳㉛ 汇聚的地方（原型 `renderReader` 的落地版）。
 *
 * 核心机制（改动前务必理解）：
 * 1. **配对由块 ID 保证**：中英两列渲染的是同一份块数组，同一行共享 `id`，
 *    所以左右天然对齐，不需要任何"段落对齐"算法（决策④ 的构造性保证）。
 * 2. **免中文块单栏横跨**（`no_zh`）：参考文献、纯公式、尚无译文 → 只渲染英文一次。
 * 3. **公式是服务端渲染的 MathML**（`en_html`/`zh_html` 来自后端，遗留项 8）：
 *    前端零数学运行时。`dangerouslySetInnerHTML` 是**有意为之**——
 *    内容是本服务端自己渲染的块 HTML（不是用户贴的 HTML），且服务端已做转义。
 * 4. **滚动进度上报**（⑰）：节流到 ≥2% 才发一次，且只在**变大**时发。
 *    已手动标记已读时后端会 409 拒绝——前端静默，因为那是预期的锁定行为。
 * 5. **字号三档模式**（对照 / 仅中文 / 仅原文）+ A−/A+ 字号：存在 localStorage，
 *    不进 URL（这是"我的阅读偏好"，不是可分享的状态）。
 * 6. **划痕（㉛）= 任意字符区间 + 一支颜色笔**：选中文字 → 浮条（选笔 / 加笔记）；
 *    点已有划痕 → 就地菜单（换笔 / 擦掉 / 加笔记）。
 *    `<mark>` 由**服务端**渲染，前端只把 DOM 选区换算成裸文本偏移（`marks.ts`），
 *    划完重拉**这一块**（不重拉整篇）让新的 `<mark>` 出现。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { ApiError, api } from '../api'
import { HL_COLORS, HL_LABEL, selectionSegments, sameRange } from '../marks'
import type { Seg } from '../marks'
import { usePlan } from '../planContext'
import { useLink, useReadonly, useShare } from '../shareContext'
import { NoPlan, TopBar } from '../shell'
import { IconBack, IconImage, IconOutline } from '../icons'
import { clip, relTime, STATUS } from '../vocab'
import { Page } from './PagesBody'
import { groupByPage } from './pageGroups'
import type { Block, HlColor, Mark, Note, PaperDoc } from '../types'

export type Mode = 'dual' | 'zh' | 'en'
type Rail = 'notes' | 'outline'

const FS_MIN = 12
const FS_MAX = 21

/** 手工能编辑/改类型的块类型 —— **必须与服务端 `blockops.TEXT_TYPES` 一致**
 *  （前端多给一个按钮 = 点下去收到 400）。其余类型（`figure`/`table`/`deco`/`eq`）的内容在
 *  `payload` 里（图片名 / 网格 / LaTeX），不在 `en`/`zh` 里，凭空造一个只会渲染成空白。
 *  `h1` 不在其中：正文不产生 h1 块（解析时 h1 全被收进论文标题，`parse._finalize`），
 *  标题文字改的是元数据，不是块。 */
const TEXT_TYPES = new Set(['p', 'h2', 'h3', 'h4', 'abstract', 'refs', 'ref'])
/** 编辑抽屉里可选的块类型（比 `TEXT_TYPES` 少 `refs`/`ref` —— 参考文献的样式是**块类型自带**的，
 *  手改类型只会把一段正文变成参考文献的样子；真要改，改的是内容不是类型）。 */
const TYPE_OPTIONS: Array<{ v: string; label: string }> = [
  { v: '', label: '（不改类型）' },
  { v: 'p', label: '正文段落' },
  { v: 'h2', label: '标题 2' },
  { v: 'h3', label: '标题 3' },
  { v: 'h4', label: '标题 4' },
  { v: 'abstract', label: '摘要' },
]

/** `doc.blocks` 缺失时的稳定空数组 —— 字面量 `[]` 每次都换新引用，
 * 会让下面两个 `useMemo` 每帧都重算（oxlint 的 exhaustive-deps 警告即此）。 */
const EMPTY_BLOCKS: Block[] = []
/** 同理：`marks` 的空数组 */
const EMPTY_MARKS: Mark[] = []

function readPref<T extends string | number>(key: string, fallback: T): T {
  const v = localStorage.getItem(`papershelf.reader.${key}`)
  if (v === null) return fallback
  return (typeof fallback === 'number' ? Number(v) : v) as T
}

/** 块渲染：服务端给 HTML 就直接用，没有则退化为纯文本（**绝不丢内容**）。
 *
 * 服务端给的是 `<p …>正文</p>` 这类**已成段落的**片段，所以这里必须把它
 * 放进 `display: contents` 的壳里 —— 否则会出现 `<p><p>…</p></p>`，
 * 浏览器会把内层 `<p>` 甩到外层之前，双栏对齐随之错位。
 *
 * `data-lang` 是**划痕的坐标系标识**（㉛）：同一个块的中英两栏各是一份独立的
 * 裸文本偏移空间，`marks.ts` 靠它区分"划的是哪一侧"。
 */
function BlockBody({ block, lang, as = 'div' }: {
  block: Block; lang: 'en' | 'zh'; as?: 'div' | 'span'
}) {
  const html = lang === 'en' ? block.en_html : block.zh_html
  const text = lang === 'en' ? block.en : (block.zh || block.en)
  // 没有服务端 HTML 时退化为纯文本 —— ⚠️ 标题里**绝不能**吐 `<p>`：
  // `<h2><p>…</p></h2>` 是非法的，浏览器会把 `<p>` 甩到 `<h2>` 之前（整行标题跑到上面去）。
  if (!html) return (as === 'span' ? <>{text}</> : <p>{text}</p>)
  if (as === 'span') {
    return <span className="b-inline" data-lang={lang} dangerouslySetInnerHTML={{ __html: html }} />
  }
  return <div className="b-inline" data-lang={lang} dangerouslySetInnerHTML={{ __html: html }} />
}

/** 正文段的块类型 —— 与**服务端可划区域**必须一致（`markup.render_block`：
 * `abstract` 分支 + 兜底 `<p>` 分支）。这里用来数「第 N 段」：
 * 段号是**正文段**的序号，标题/图表/公式/参考文献都不占号（原型 `buildFlat` 同款）。 */
const PROSE_TYPES = new Set(['p', 'abstract', 'meta'])

/** 块 → 文档舞台里的元素。类名与 `styles.css` 的 `.doc-*` / `.t-*` 对应。
 *
 * 分页容器由外层 `PagesBody` 负责（按 `payload.page` 分组）——这里只管**单块**长什么样。
 *
 * `assets` 是整篇的资产表（`{name,width,height}`），**只有 figure 需要它**：
 * 给 `img` 挂上 `width`/`height` 属性，浏览器就能在图片**下载完成前**按宽高比预留位置。
 * 不这么做的话（踩过）：`loading="lazy"` 的图未加载时高度≈0，滚到哪长到哪，
 * 整篇高度事后上浮数千像素 —— 滚动位置随之漂移，**大纲跳转的落点会偏掉 7500px**。
 *
 * 点击**不在这里处理**：整篇有上千个块，逐块挂 onClick 不如在舞台（`.doc-stage`）上
 * 委派一次（见 `ReaderPage.stageClick`）。这里只留键盘可达性。
 */
export function DocBlock({ b, mode, assets, selected, flash, onPick }: {
  b: Block; mode: Mode
  assets?: Map<string, { width?: number; height?: number }>
  selected?: boolean
  flash?: boolean
  onPick?: (blockId: string) => void
}) {
  const wide = b.no_zh || !(b.zh || '').trim()
  /** 是否用**双栏网格**排版（只有对照模式 + 该块确实有译文时才并排）。 */
  const dual = mode === 'dual' && !wide
  /** 是否渲染中文那一栏。
   *
   * ⚠️ **不能只用 `mode === 'dual'`**（2026-09-15 真缺陷）：`styles.css` 在 `.lang-zh`
   * 下把 `.t-en` 藏了（`.lang-zh .t-en { display: none !important }`），所以仅中文模式
   * 若不渲染中文栏，屏幕上就是**一片空白** —— 实测 34 个正文段落的可见文字为 0
   * （`.blk-p` 高度 14px，只剩内边距）。真浏览器逐段核对才发现的，光看"有渲染"看不出来。
   *
   * 仅中文模式**一律渲染**（哪怕这页没有译文）：`markup.render_block` 在没有 `zh` 时
   * 回落英文原文且不渲染空白，所以"没译文的块"会显示一次原文 —— 这比开天窗好，
   * 也与服务端"中文视图绝不空白"的口径一致。
   *
   * 附带好处：`.b-inline[data-lang="zh"]` 是划痕的坐标系（㉛），不渲染它，
   * 仅中文模式下连划重点都用不了（选区根本没有落点）。
   */
  const showZh = mode === 'zh' || dual

  if (b.type.startsWith('h')) {
    const lvl = Math.min(3, Math.max(1, b.level || 2))
    return (
      <h2 className={`doc-h lvl${lvl}`} data-b={b.id}>
        {/* ⚠️ 标题的每一栏也必须包一层 `.b-inline[data-lang]`（2026-09-16 宿主：
            「标题行，选中后没有笔记工具的弹出 mark-bar」）。`marks.ts::selectionSegments`
            是从选区的文本节点往上找 `.b-inline[data-lang]` 拿坐标系与块 id 的 ——
            标题原先直接吐 `<span className="b-en">{b.en}</span>` 纯文本，于是
            `containerOf()` 返回 null → `segs` 为空 → 浮条根本不出现（连"划了但没上色"
            都做不到）。服务端同步改成了 `prose()`，两侧缺一不可：这里给坐标系，
            服务端给尺子（零宽锚点）与 `<mark>`。
            用 `span` 而不是 `div`：`<h2>` 里只允许短语内容，塞 `div` 会被浏览器甩出去。 */}
        {mode !== 'zh' && <span className="b-en"><BlockBody block={b} lang="en" as="span" /></span>}
        {/* 仅中文模式即使标题没译文也要渲染（回落原文）—— 否则整个标题空白。
            对照模式仍然只在有译文时给右栏，免得同一行英文出现两次。 */}
        {mode !== 'en' && (!wide || mode === 'zh') && (
          <span className="b-zh"><BlockBody block={b} lang="zh" as="span" /></span>
        )}
      </h2>
    )
  }
  if (b.type === 'figure') {
    const src = b.payload?.src ? String(b.payload.src) : ''
    const dim = src ? assets?.get(src.split('/').pop() || '') : undefined
    return (
      <figure className="blk fig" data-b={b.id}>
        <div className="fig-frames">
          {src
            ? <img src={src} alt="" loading="lazy"
                   width={dim?.width} height={dim?.height} />
            : <div className="fig-frame"><span className="frame-ic"><IconImage /></span></div>}
        </div>
        <figcaption className={`cap${dual ? ' dual' : ''}`}>
          {mode !== 'zh' && <span className="b-en">{b.en || String(b.payload?.caption || '')}</span>}
          {mode !== 'en' && <span className="b-zh">{b.zh || b.en || String(b.payload?.caption || '')}</span>}
        </figcaption>
      </figure>
    )
  }

  if (b.type === 'deco') {
    /* 页边装饰图（v11）：**出版社/期刊的矢量标识**（Springer 马标、"Check for updates"
       徽标…）。这类东西在 PDF 里是**画出来的矢量填充**，`get_text()` 与图片层都拿不到，
       所以从来没进过产物（宿主 2026-09-17：「Springer 的图还是没有」）。服务端现在把它们
       栅格化成**透明 PNG** 落成 `deco` 块（`parse._margin_graphics` / `_add_graphic`）。

       ⚠️ 必须在这里显式出一支，不能指望"落到正文兜底分支也能显示"：
       兜底分支会把它包进 `.t-en`（`text-align: justify` + 正文行高），且
       `payload.band/align` 的贴边/左右交替整个丢失 —— 标识会跑到正文流的中间。
       `width`/`height` 用服务端算好的 CSS px（PDF pt × 1.45，见 `parse._PT_TO_PX`），
       挂成 img 属性 → 图**下载完成前**就占好位置（不挂会整篇高度上浮、大纲跳转偏位）。 */
    const src = b.payload?.src ? String(b.payload.src) : ''
    if (!src) return null
    const band = String(b.payload?.band || '')
    const align = String(b.payload?.align || '')
    return (
      <div className={`blk deco${band ? ` deco-${band}` : ''}${align ? ` deco-${align}` : ''}`}
           data-b={b.id}>
        <img src={src} alt="" loading="lazy"
             width={Number(b.payload?.w) || undefined}
             height={Number(b.payload?.h) || undefined} />
      </div>
    )
  }

  if (b.type === 'table') {
    /* 表格块（决策㊴ 重建 / ㊵ 并排 + 可划）。
     *
     * ⚠️ 表格的 HTML **一律来自服务端**（`en_html`/`zh_html`），前端不再自己拼 `<table>`。
     * 三条理由，每条都对应一次踩过的坑：
     * 1. **划痕的尺子只有服务端有** —— 选中格子里几个字要变成 `(start,end)` 坐标，
     *    靠的是 `<span class="o" data-o="N">` 零宽锚点（`markup.prose_html`，见 `marks.ts`）。
     *    前端自己排版就没有这把尺子 → **选中后浮条根本不出现**（同 ㊳「标题不可划」）。
     * 2. **`<mark>` 也必须由服务端吐** —— 自己包一层 = 第二份排版实现，早晚与锚点分叉（㉛）。
     * 3. 列对齐/表头双线/等宽数字这套版式，导出与分享页看到的是同一份 HTML。
     *
     * 对照模式（2026-09-16 宿主：「应该是左侧英文表格，右侧中文表格。而不是在一个单元格里，
     * 既有中文，又有英文」）：两张表**并排**，左边英文表、右边中文表，各带自己的表注
     * （表注就在 `<caption>` 里，跟着自己那张表 → 天然与两栏对齐）。
     * 这也正是**导出/分享的双语 HTML 一直是的样子**（`synth._dual_rows`），此处只是让阅读器对齐它。
     *
     * `table_zh` 由服务端给（`model.table_zh_usable`）：中文网格形状不符、或逐格照抄英文时
     * **不配第二张表** —— 否则右边会是一张与左边一模一样的表，比"一格中英两行"更糟。
     */
    const pair = mode === 'dual' && b.table_zh === true
    return (
      <div className={`blk tbl${pair ? ' dual' : ''}${selected ? ' is-sel' : ''}${flash ? ' is-flash' : ''}`}
           data-b={b.id}
           tabIndex={0} role="button" aria-label="选中此块"
           onKeyDown={(e) => {
             if (e.key !== 'Enter' && e.key !== ' ') return
             e.preventDefault()
             onPick?.(b.id)
           }}>
        <div className="tbl-pair">
          {mode !== 'zh' && (
            <div className="tbl-col en"><div className="tbl-wrap"><BlockBody block={b} lang="en" /></div></div>
          )}
          {(mode === 'zh' || pair) && (
            <div className="tbl-col zh"><div className="tbl-wrap"><BlockBody block={b} lang="zh" /></div></div>
          )}
        </div>
      </div>
    )
  }

  // 正文 / 摘要 / 公式 / 参考文献：内容与排版全部由服务端渲染（含 `<mark>`）。
  return (
    <div className={`blk blk-p${dual ? ' dual' : ' wide'}${selected ? ' is-sel' : ''}${flash ? ' is-flash' : ''}`}
         data-b={b.id}
         tabIndex={0} role="button" aria-label="选中此块"
         onKeyDown={(e) => {
           if (e.key !== 'Enter' && e.key !== ' ') return
           e.preventDefault()
           onPick?.(b.id)
         }}>
      <div className="t-en"><BlockBody block={b} lang="en" /></div>
      {showZh && <div className="t-zh"><BlockBody block={b} lang="zh" /></div>}
    </div>
  )
}

/** 段落序号文案（`paraIndex` 的段号是**正文段**计数，不是块序号：块序号里混着
 *  标题、图表、公式，直接用会出现"第 137 段"这种对人不友好的数字）。
 *
 * ㉛ 换掉了句号：锚点不再是句子，所以文案里也没有"第 M 句"了 —— 换成
 * 「第 a–b 字」（1 起、闭区间，对人就是对"划住的那几个字"）。 */
function anchorLabel(blockId: string | null | undefined,
                     lang: string | null | undefined,
                     start: number | null | undefined,
                     end: number | null | undefined,
                     paraIndex: Map<string, number>): string {
  const bid = blockId || ''
  const ord = bid ? paraIndex.get(bid) : undefined
  const base = ord ? `第 ${ord} 段` : (bid ? `块 ${bid}` : '文献级')
  if (!bid) return '文献级笔记（未锚定到具体段落）'
  if (start === null || start === undefined || end === null || end === undefined) {
    return `${base}（整段）`
  }
  return `${base}（${lang === 'zh' ? '中文' : '原文'}）· 第 ${start + 1}–${end} 字`
}

/** 浮条/菜单里要显示的东西：选区（一段或多段）+ 它落在一道已有划痕上时的 id。 */
type Target = { segs: Seg[]; hlId: number | null }

export function ReaderPage() {
  const { paperId = '' } = useParams()
  const pid = Number(paperId)
  const nav = useNavigate()
  const { plan, papers, reload } = usePlan()
  const share = useShare()
  const link = useLink()
  const readonly = useReadonly()

  const [doc, setDoc] = useState<PaperDoc | null>(null)
  const [error, setError] = useState('')
  const [notes, setNotes] = useState<Note[]>([])
  const [marks, setMarks] = useState<Mark[]>(EMPTY_MARKS)
  /** 选中的**块**（块工具/整段笔记的锚点） */
  const [selBlock, setSelBlock] = useState<string>('')
  /** 当前标注目标（选区或已有划痕）：笔记表单的锚点、浮条的落点 */
  const [target, setTarget] = useState<Target | null>(null)
  /** 浮条 / 就地菜单（`null` = 不显示） */
  const [bar, setBar] = useState<(Target & {
    kind: 'sel' | 'mark'; x: number; y: number
  }) | null>(null)
  /** 当前那支笔（默认第一支）—— 先选笔再划、划完再选笔两条路都通 */
  const [pen, setPen] = useState<HlColor>(() => readPref<HlColor>('pen', HL_COLORS[0]))
  /** 点「加笔记」时把焦点送进右栏文本框（自增的"信号"，不是值） */
  const [focusNote, setFocusNote] = useState(0)
  /** 正在闪烁的**块**（点笔记跳转到整块时的落点提示）。划痕那一侧不走状态，见 `gotoNote`。 */
  const [flashBlock, setFlashBlock] = useState<string>('')
  /** 编辑/插入抽屉。`isNew` ⇒ 走插入（`side` 决定落在参照块的上方还是下方）；
   *  `id` 是**参照块**的 id —— 编辑时也是"被编辑的那一块"。 */
  const [editing, setEditing] = useState<{
    id: string; en: string; zh: string; isNew?: boolean; side?: 'before' | 'after'
  } | null>(null)
  /** 编辑/插入时选的块类型（`editType`）。空串 = 不动（编辑时）或跟随参照块（插入时）。 */
  const [editType, setEditType] = useState('')
  const [mode, setMode] = useState<Mode>(() => readPref<Mode>('mode', 'dual'))
  const [rail, setRail] = useState<Rail>(() => readPref<Rail>('rail', 'notes'))
  const [fs, setFs] = useState<number>(() => readPref<number>('fs', 14.5))
  const [showTools, setShowTools] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [progEdit, setProgEdit] = useState<string | null>(null)   // ㊺ 工具栏「进度 N%」就地编辑的草稿
  const progDone = useRef(false)              // ㊺ 这一次输入是否已提交/取消（挡迟到的那次 blur）
  const [toast, setToast] = useState('')

  const scrollRef = useRef<HTMLDivElement>(null)
  const lastPct = useRef(-1)

  const paper = papers.find((p) => p.id === pid) || null

  const load = useCallback(async () => {
    if (!Number.isFinite(pid)) return
    try {
      // ⑩ 分享态走 `/api/shares/{token}*` 白名单（登录态的写端点匿名一律 403）
      const d = share ? await share.loadDoc(pid) : await api.doc(pid)
      setDoc(d)
      setError('')
      lastPct.current = -1
      try {
        setNotes(share ? await share.loadNotes(pid) : await api.notes(pid))
      } catch { /* 笔记失败不该拦住阅读 */ }
      // ㉛ 划痕与笔记同理：取不到只是"没有划痕"，绝不拦住阅读
      try {
        setMarks(share ? await share.loadMarks(pid) : await api.marks(pid))
      } catch { /* 划痕失败不该拦住阅读 */ }
    } catch (e) {
      // ⑪ 匿名访客的文案不能沿用"稍后自动刷新"（那是给登录用户的），
      // 分享端点已经回了一句人话（见 shares.py），这里直接用。
      setError(e instanceof ApiError && e.status === 409
        ? '这篇文献还在转换中，稍后自动刷新…'
        : (e instanceof Error ? e.message : '加载失败'))
    }
  }, [pid, share])

  useEffect(() => { void load() }, [load])
  useEffect(() => { localStorage.setItem('papershelf.reader.mode', mode) }, [mode])
  useEffect(() => { localStorage.setItem('papershelf.reader.rail', rail) }, [rail])
  useEffect(() => { localStorage.setItem('papershelf.reader.fs', String(fs)) }, [fs])
  useEffect(() => { localStorage.setItem('papershelf.reader.pen', pen) }, [pen])

  // 换一篇文献：上一份选区/目标不该跟过来（否则新文档一打开就"选中"了不存在的字）。
  // 用"上一次见到的 pid"当哨兵**在渲染期**重同步 —— 与顶栏搜索框同一个套路，
  // 比 effect 少一帧（那一帧里旧目标会与新文档对不上）。
  const [syncedPid, setSyncedPid] = useState(pid)
  if (syncedPid !== pid) {
    setSyncedPid(pid)
    setSelBlock('')
    setTarget(null)
    setBar(null)
  }

  // 未就绪时轮询（② 全自动无人值守，用户不必手动刷新）
  useEffect(() => {
    if (doc || error === '') return
    const t = window.setInterval(() => { void load() }, 4000)
    return () => window.clearInterval(t)
  }, [doc, error, load])

  // Esc 收掉浮条（不改变任何已存的数据）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setBar(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  /** ⑰ 滚动进度：节流 + 只在变大 ≥2% 时发（后端另有"只增不减"兜底）。 */
  const onScroll = useCallback(() => {
    // ⑩ 只读：不上报进度（原型 `onStageScroll` 里 `if (best >= 0 && !SHARE.active)`）
    if (readonly) return
    const el = scrollRef.current
    if (!el) return
    const max = el.scrollHeight - el.clientHeight
    if (max <= 0) return
    const pct = Math.min(100, Math.round((el.scrollTop / max) * 100))
    if (pct <= lastPct.current) return
    if (pct - lastPct.current < 2 && pct < 100) return
    lastPct.current = pct
    api.updatePaper(pid, { progress: pct } as never).then(() => { void reload() }).catch((e) => {
      // 409 = 已标记已读、进度锁定（预期行为，不打扰用户）
      if (!(e instanceof ApiError) || e.status !== 409) console.warn(e)
    })
  }, [pid, reload, readonly])

  const blocks = doc?.blocks ?? EMPTY_BLOCKS
  const meta = doc?.meta ?? {}

  /** 正文段序号（1 起）：块 ID → 第几段。笔记卡片/表单的"第 N 段"读它。 */
  const paraIndex = useMemo(() => {
    const m = new Map<string, number>()
    let n = 0
    for (const b of blocks) if (PROSE_TYPES.has(b.type)) m.set(b.id, ++n)
    return m
  }, [blocks])

  /** 划痕 id → 划痕（笔记卡片的颜色圆点、菜单里的"当前笔色"都读它）。 */
  const markById = useMemo(() => new Map(marks.map((m) => [m.id, m])), [marks])

  /** PDF 分页容器：按 `payload.page` 切页（原型 `.pdf-page`）。 */
  const pageGroups = useMemo(() => groupByPage(blocks), [blocks])
  const pageTotal = Number(meta.pages) || 0

  /** 资产索引（按文件名）：给 figure 的 `img` 提前定宽高，避免 lazy 加载撑高整篇。 */
  const assetDims = useMemo(() => new Map(
    (doc?.assets ?? []).map((a) => [a.name, { width: a.width, height: a.height }] as const)),
    [doc])

  /** 大纲：从标题块推出来（原型是手写常量，这里必须由数据生成）。
   *
   * 原型 `oi-n` 显示的是**章节号**（`3.1`），不是层级 —— 所以这里把标题开头的
   * 编号剥出来单独放，剩下的才是文字（`I. 引言` → `I.` + `引言`）；没有编号的
   * 标题（摘要、参考文献）退回 `·`，与原型一致。 */
  const outline = useMemo(() => blocks
    .filter((b) => b.type === 'h1' || b.type === 'h2' || b.type === 'h3')
    .map((b) => {
      const text = (b.zh || b.en || '').trim()
      const m = /^([IVXLC]+\.|[A-Z]\.|\d+(?:\.\d+)*\.?)\s+(.+)$/.exec(text)
      return {
        id: b.id,
        level: Math.min(3, Math.max(1, b.level || 2)),
        num: m ? m[1] : '·',
        label: m ? m[2] : text,
      }
    }),
    [blocks])

  const stats = useMemo(() => ({
    total: blocks.length,
    translated: blocks.filter((b) => b.zh_source !== 'none' && (b.zh || '').trim()).length,
    review: blocks.filter((b) => b.needs_review).length,
    human: blocks.filter((b) => b.zh_source === 'human').length,
    figs: blocks.filter((b) => b.type === 'figure').length,
    marks: marks.length,
  }), [blocks, marks])

  /** 打开「编辑此块」（宿主三个原语之二：老 block 的**编辑**）。
   *  两栏都给出来 —— 编辑器里 `en`/`zh` 各按自己的坐标系重锚批注（见 `blockops.edit_block`）。 */
  function openEdit(b: Block) {
    setEditType('')
    setEditing({ id: b.id, en: b.en || '', zh: b.zh || '' })
  }

  /** 打开「插入新块」（三个原语之一）。`side` 决定落点在参照块的**前**还是**后**。
   *  拆分 = 这里插入 + 把要挪走的字从原块剪到新块（两个原语自己组合）；
   *  合并 = 把下一块的字接进上一块 + 删掉它。程序不提供"拆分/合并"两个动作。 */
  function openInsert(b: Block, side: 'before' | 'after') {
    setEditType('')
    setEditing({ id: b.id, en: '', zh: '', isNew: true, side })
  }

  async function saveBlock() {
    if (!editing) return
    const { id, en, zh, isNew, side } = editing
    try {
      if (isNew) {
        const body = { en, zh, type: editType || undefined }
        const fresh = await api.insertBlock(pid, side === 'before' ? { before: id, ...body }
                                                                    : { after: id, ...body })
        // 插入会改后续块的 `ord`（阅读顺序）—— 整篇重拉最稳（rare op），
        // 也顺带把新块带上屏幕。**已有块 id 一个不变** ⇒ 邻居的批注天然不受影响。
        flash(zh.trim() ? `已插入新块 ${fresh.id}` : `已插入新块 ${fresh.id}（还没有中文，可「重译此块」）`)
        setEditing(null)
        setSelBlock(fresh.id)
        await load()
        return
      }
      // ⚠️ **只发真正变了的字段**：`edit_block` 见到 `zh` 就会把 `zh_source` 置 `human`
      //    （⑯ 的正确语义），照原样把两栏都发过去 ⇒ "打开抽屉再点保存"这一下会把整块的
      //    译文永久标成"人工修订"、重跑再也不覆盖它。用户没改的字不该被他的一次误触接管。
      const orig = blocks.find((x) => x.id === id)
      const body: { en?: string; zh?: string; type?: string; reconciled: boolean } = { reconciled: true }
      if (!orig || en !== (orig.en || '')) body.en = en
      if (!orig || zh !== (orig.zh || '')) body.zh = zh
      if (editType) body.type = editType
      if (body.en === undefined && body.zh === undefined && !body.type) {
        setEditing(null)
        flash('没有改动')
        return
      }
      const fresh = await api.editBlock(pid, id, body)
      // 整块替换（不是就地改文本）：后端重渲染了 zh_html（含重切后的划痕），公式/排版才跟着变
      setDoc((d) => d && ({ ...d, blocks: d.blocks.map((b) => (b.id === id ? fresh : b)) }))
      setEditing(null)
      setNotes(share ? await share.loadNotes(pid).catch(() => notes) : await api.notes(pid).catch(() => notes))
      flash(editReport(fresh.anchors_moved, fresh.anchors_dropped, fresh.zh_stale))
    } catch (e) { setError(e instanceof Error ? e.message : '保存失败') }
  }

  /** 保存后如实报出批注的后果 —— **静默丢批注是这里最坏的失败模式**（`blockops` 的三条
   *  不变量就是为它写的）：删掉的划痕/失去落点的笔记必须在界面上当场说出来。
   *
   *  两种"没了"要分开说：划痕（只有坐标）是**真的消失**；笔记（有内容）**只在内容里留着**，
   *  锚点被清空成「文献级笔记」。混成一句"2 处批注随之移除"会让用户以为笔记也没了。 */
  function editReport(moved?: number, dropped?: Array<{ kind?: string }>, zhStale?: boolean): string {
    const gone = dropped ?? []
    const hls = gone.filter((d) => d.kind !== 'note').length
    const ns = gone.length - hls
    const parts = ['已保存（标记为人工修订，重跑不会被覆盖）']
    if (moved) parts.push(`本块 ${moved} 处批注已随文字移动`)
    if (hls) parts.push(`${hls} 处划痕的文字已被删除，随之移除`)
    if (ns) parts.push(`${ns} 条笔记失去落点，已转为「文献级笔记」（内容保留）`)
    if (zhStale) parts.push('译文与新原文已不一致，已标「待校对」')
    return parts.join('·')
  }

  /** 删除本块（宿主 2026-09-20 的第三个原语；合并 = 把下一块接进上一块 + 删掉它）。
   *  该块的**笔记内容不删**（转成「文献级笔记」，锚点清空）—— 所以确认框里必须说清。
   *
   * ⚠️ 确认框里**必须把这一块的原文抄出来**（`clip(b.en, 60)`）：按钮挂在"当前选中的那一块"
   * 上，而长文档里选中项会随着点别处而移动 —— 2026-09-20 真机验收时就因为"以为选中的还是刚
   * 插入的那一块"、对确认框点了确定，误删了一个正文块（原文那一行正是唯一的告警）。
   * `window.confirm` 是**纯文本**：这里不能写 `**加粗**`（会照原样显示星号）。 */
  async function removeBlock(b: Block) {
    const ok = window.confirm(
      `删除这一块？\n\n原文：${clip(b.en || '(空)', 60)}\n\n` +
      '· 正文里这一段会消失（这就是「合并/撤销拆分」的那一半）；\n' +
      '· 这一块上的笔记内容会保留（转成「文献级笔记」，不再指到原文位置）；\n' +
      '· 这一块上的划痕会随之移除。')
    if (!ok) return
    try {
      const res = await api.deleteBlock(pid, b.id)
      setSelBlock('')
      await load()
      const parts = ['已删除此块']
      if (res.notes_unanchored) parts.push(`${res.notes_unanchored} 条笔记已转为「文献级笔记」（内容保留）`)
      if (res.highlights_deleted) parts.push(`${res.highlights_deleted} 道划痕随之移除`)
      flash(parts.join('·'))
    } catch (e) { flash(e instanceof Error ? e.message : '删除失败') }
  }

  async function retranslate(b: Block) {
    try {
      const fresh = await api.retranslateBlock(pid, b.id)
      setDoc((d) => d && ({ ...d, blocks: d.blocks.map((x) => (x.id === b.id ? fresh : x)) }))
      flash('已重译此块')
    } catch (e) { flash(e instanceof Error ? e.message : '重译失败') }
  }

  async function markRead() {
    try {
      await api.updatePaper(pid, { status_: 'reviewed' })
      await reload()
      flash('已标记为「已整理」，进度锁定 100%')
    } catch (e) { flash(e instanceof Error ? e.message : '标记失败') }
  }

  /** ㊺ 手改进度（工具栏就地编辑 / 元数据抽屉，两个入口同一个函数）。
   *
   * `progress_by: 'user'` 是**这个功能的全部**（见 `api.updatePaper` 的注释）：
   * 后端按它把"我说它是多少"与"滚到过哪里"分开 —— 前者可改小、可越过「已读」锁，
   * 且**不写 `last_read_at`、不翻「待读→在读」**（填一个数字不等于此刻在读）。
   *
   * 存完必须 `reload()`：`paper` 是 `reload` 拉回来的，不回拉的话
   * 屏幕上是本地草稿、库里是另一个值（同型坑：改块要回**完整块对象**，否则"保存成功但界面不变"）。
   */
  async function saveProgress(next: number | null) {
    setProgEdit(null)
    if (next === null) return                       // 空草稿 = 没输入东西，不写库
    const cur = paper?.progress ?? 0
    if (next === cur) return
    try {
      await api.updatePaper(pid, { progress: next, progress_by: 'user' })
      await reload()
      flash(`进度已改为 ${next}%`)
    } catch (e) { flash(e instanceof Error ? e.message : '进度保存失败') }
  }

  /** 打开就地编辑器（草稿 = 当前值）。两条路径共用：鼠标 `mousedown`、键盘 `click`。 */
  function openProgEdit() {
    progDone.current = false
    setProgEdit(String(paper?.progress ?? 0))
  }

  /** 草稿 → 0-100 的整数；空串/非数字返回 null（= 什么都不做）。
   *  越界**夹紧**而不是报错：手打 150 的人想要的大概率是 100。 */
  function parseProgress(draft: string): number | null {
    const t = draft.trim()
    if (!t || !/^\d+$/.test(t)) return null
    return Math.max(0, Math.min(100, Number(t)))
  }

  async function refreshDoc() {
    setRefreshing(true)
    try { setDoc(share ? await share.loadDoc(pid) : await api.doc(pid)) } finally { setRefreshing(false) }
  }

  function flash(msg: string) {
    setToast(msg)
    window.setTimeout(() => setToast(''), 2400)
  }

  /** 划痕变了之后：重取划痕列表 + **只重取受影响的那几块**。
   *
   * 为什么必须重取块：`<mark>` 是**服务端**渲染进 `en_html`/`zh_html` 的（㉛ 的
   * 核心不变量：排版/公式/划痕只有一处实现）。前端自己拿 Range 去包 DOM 会立刻
   * 与服务端渲染分叉（公式、转义、跨行都要各写一遍），所以宁可重拉这一块。
   * 重拉整篇也不行：900 块的文档就是几百 KB —— 划一道重下一次，纯浪费。 */
  async function refreshMarks(blockIds: string[]) {
    const ids = [...new Set(blockIds)]
    const [fresh, ...bs] = await Promise.all([
      api.marks(pid),
      ...ids.map((b) => api.block(pid, b)),
    ])
    setMarks(fresh)
    const byId = new Map(bs.map((b) => [b.id, b]))
    setDoc((d) => d && ({ ...d, blocks: d.blocks.map((b) => byId.get(b.id) ?? b) }))
  }

  /** 划一道（或给已有的一道换色）。`color` 同时会成为"当前那支笔"。 */
  async function paint(color: HlColor) {
    if (readonly || !bar) return
    setPen(color)
    try {
      if (bar.kind === 'mark' && bar.hlId) {
        await api.setMarkColor(bar.hlId, color)
        await refreshMarks(bar.segs.map((s) => s.blockId))
        flash('已换色')
        return
      }
      const touched: string[] = []
      let first: Mark | null = null
      for (const seg of bar.segs) {
        touched.push(seg.blockId)
        const dup = sameRange(marks, seg)
        if (dup) {                                  // 同一段同一笔 → 不重复落库
          if (dup.color !== color) await api.setMarkColor(dup.id, color)
          first = first ?? dup
          continue
        }
        const m = await api.addMark(pid, {
          block_id: seg.blockId, lang: seg.lang, start: seg.start, end: seg.end, color,
        })
        first = first ?? m
      }
      await refreshMarks(touched)
      // 划完就把"这道划痕"当成当前目标：接着点「加笔记」时笔记就锚在它上面
      setTarget({ segs: bar.segs, hlId: first?.id ?? null })
      setBar((b) => (b ? { ...b, hlId: first?.id ?? null, kind: 'mark' } : b))
      flash('已标注')
    } catch (e) {
      flash(e instanceof Error ? e.message : '标注失败')
    }
  }

  /** 擦掉当前这一道划痕（笔记**不会**跟着消失，服务端只把它的 `hl_id` 解绑）。 */
  async function erase(id: number, blockIds: string[]) {
    if (readonly) return
    try {
      await api.removeMark(id)
      await refreshMarks(blockIds)
      setBar(null)
      setTarget(null)
      flash('已擦除')
    } catch (e) { flash(e instanceof Error ? e.message : '擦除失败') }
  }

  /** 选中文字（mouseup）→ 浮条。选区塌缩（= 点了空白/点了别处）就把浮条收掉 ——
   *  否则它会一直挂在那儿，让人以为"刚才那一划还算数"。 */
  function onStageMouseUp() {
    if (readonly) return
    const stage = scrollRef.current
    if (!stage) return
    const segs = selectionSegments(stage)
    if (segs.length === 0) { setBar(null); return }
    const sel = window.getSelection()
    const rect = sel && sel.rangeCount > 0 ? sel.getRangeAt(0).getBoundingClientRect() : null
    const hit = sameRange(marks, segs[0])
    setBar({
      kind: 'sel', segs, hlId: hit?.id ?? null,
      x: rect ? rect.left + rect.width / 2 : window.innerWidth / 2,
      y: rect ? rect.top : 120,
    })
    setTarget({ segs, hlId: hit?.id ?? null })
    setSelBlock(segs[0].blockId)
    // 选一段 = 接下来多半要给它写笔记 → 右栏切到「笔记」（原型 `selectSent` 同款）
    setRail('notes')
  }

  /** 舞台上的点击（**委派**，不给上千个块各挂一份）：
   *  点划痕 → 就地菜单；点别处 → 选中该块。
   *
   *  ⚠️ **拖选收尾那一下 click 不算"点了别处"**：浏览器在拖选结束后还会补一个 click
   *  （target = 落点所在块），若照着"点别处 = 收浮条"处理，`onStageMouseUp` 刚点亮的
   *  浮条会被它当场收掉 —— 实测「浮条一闪即没」，划重点这条主路径整个不可用。
   *  原型把"收浮条"挂在 **mousedown**（`stage.addEventListener('mousedown', hideBar)`），
   *  mousedown 在选区形成**之前**，于是天然躲开了这个顺序问题；app 里收浮条挪到了 click，
   *  就必须自己认出"这一下是拖选的尾巴"。判据用**选区还活着**（非塌缩）。 */
  function stageClick(e: React.MouseEvent) {
    const t = e.target as HTMLElement
    const sel = window.getSelection()
    const dragTail = !!sel && !sel.isCollapsed
    const hl = t.closest<HTMLElement>('[data-h]')
    if (hl && !dragTail) {
      const id = Number(hl.dataset.h)
      const m = markById.get(id)
      if (!m || readonly) return
      const rect = hl.getBoundingClientRect()
      const seg: Seg = {
        blockId: m.block_id, lang: m.lang, start: m.start, end: m.end,
        quote: (hl.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60),
      }
      setBar({ kind: 'mark', segs: [seg], hlId: id,
               x: rect.left + rect.width / 2, y: rect.top })
      setTarget({ segs: [seg], hlId: id })
      setSelBlock(m.block_id)
      setRail('notes')
      return
    }
    const blk = t.closest<HTMLElement>('[data-b]')
    if (blk) setSelBlock(blk.dataset.b || '')
    if (dragTail) return              // 拖选的尾巴：浮条与目标都留给 mouseup 定的那一份
    setBar(null)
    setTarget(null)
  }

  /** 从浮条点「加笔记」：保住目标，把焦点送进右栏文本框。 */
  function startNote() {
    if (readonly) return
    setRail('notes')
    setFocusNote((n) => n + 1)
  }

  /** 点笔记 → 滚到对应划痕并**闪一下**（原型 `goToNote`）。
   *
   * 两级落点：先找划痕（`data-h`），划痕没了（或这条笔记本来就没划痕）就退到整块
   * （`data-b`）—— 「仅中文」模式里点一条原文笔记，划痕节点确实存在但不可见，
   * scrollIntoView 会算在 0 处。落点停在视口 32% 处（而不是居中）：
   * 几个字往往只占一行，居中会让上下文全滚出屏幕。 */
  function gotoNote(n: Note) {
    const stage = scrollRef.current
    if (!stage) return
    let target: HTMLElement | null = n.hl_id
      ? stage.querySelector<HTMLElement>(`[data-h="${n.hl_id}"]`)
      : null
    if (target && target.offsetParent === null) target = null
    const bid = n.block_id || ''
    let whole = false
    if (!target && bid) {
      target = stage.querySelector<HTMLElement>(`[data-b="${bid}"]`)
      whole = true
    }
    if (!target) return
    const el = target
    setSelBlock(bid)
    if (!whole && n.lang && n.start !== null && n.start !== undefined
        && n.end !== null && n.end !== undefined) {
      setTarget({ segs: [{ blockId: bid, lang: n.lang, start: n.start, end: n.end,
                           quote: n.quote || '' }], hlId: n.hl_id ?? null })
      setRail('notes')
    }
    stage.scrollTop += el.getBoundingClientRect().top - stage.getBoundingClientRect().top
      - stage.clientHeight * 0.32
    if (whole) {
      // 整块是 **React 管的元素**：类名由 JSX 决定，所以闪烁也交给状态（`flashBlock`）。
      // 直接 `classList.add` 会被紧随其后的那次重渲染（`setSelBlock` 引起的）擦掉 —— 踩过：
      // 划痕级闪烁留得住（`<mark>` 不归 React 管），块级闪烁却静默不出现。
      // 先清空再下一帧挂上：连点同一条笔记时动画才会重放（等价于原型的 `void offsetWidth`）。
      setFlashBlock('')
      window.requestAnimationFrame(() => setFlashBlock(bid))
      window.setTimeout(() => setFlashBlock((cur) => (cur === bid ? '' : cur)), 1400)
    } else {
      el.classList.remove('is-flash')
      void el.offsetWidth          // 强制重排，动画才会在连点时重放
      el.classList.add('is-flash')
      window.setTimeout(() => el.classList.remove('is-flash'), 1400)
    }
  }

  function jump(id: string) {
    setSelBlock(id)
    setBar(null)
    document.querySelector(`[data-b="${id}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }

  /* ── 阅读器空态：没有选文献，但有计划 ────────────────────────────── */
  if (!Number.isFinite(pid)) {
    if (!plan) return <><TopBar title="阅读器" sub="还没有阅读计划" /><NoPlan /></>
    const first = papers.find((p) => p.conv_state === 'done') || papers[0]
    return (
      <>
        <TopBar title="阅读器" sub="单栏阅读 · 中英左右对照 · 图表与原文同位" />
        <div className="view-scroll">
          <div className="view">
            {papers.length === 0 ? (
              <div className="empty">
                <p style={{ margin: '0 0 6px' }}>该计划还没有文献</p>
                <span className="meta">先导入 PDF 或 arXiv 链接，再开始精读。</span>
              </div>
            ) : (
              <>
                <p className="muted">
                  选择一篇开始阅读。已生成中文版的文献才能进入双语精读。
                </p>
                <div className="grid g-2" style={{ marginTop: 16 }}>
                  {papers.map((p) => (
                    <button className="card" key={p.id} style={{ textAlign: 'left' }}
                            disabled={p.conv_state !== 'done'}
                            onClick={() => nav(link(`/reader/${p.id}`))}>
                      <div className="row">
                        <span className="rr-t" style={{ flex: 1 }}>{p.title || `文献 #${p.id}`}</span>
                        <span className={`pill ${STATUS[p.status].cls}`}>{STATUS[p.status].label}</span>
                      </div>
                      <div className="meta" style={{ marginTop: 6 }}>
                        {[p.authors, p.venue, p.year].filter(Boolean).join(' · ')} · 进度 {p.progress}%
                      </div>
                      {p.conv_state !== 'done' && (
                        <div className="meta" style={{ marginTop: 6, color: 'var(--warn)' }}>
                          尚未生成中文版，暂不能精读
                        </div>
                      )}
                    </button>
                  ))}
                </div>
                {first && (
                  <button className="btn btn-primary" style={{ marginTop: 16 }}
                          onClick={() => nav(link(`/reader/${first.id}`))}>继续最近一篇</button>
                )}
              </>
            )}
          </div>
        </div>
      </>
    )
  }

  if (error) {
    return (
      <>
        <TopBar title="阅读器" sub={plan?.name} />
        <div className="view-scroll">
          <div className="view">
            <div className="banner error">{error}</div>
            <button className="btn btn-secondary" onClick={() => void load()}>重试</button>
          </div>
        </div>
      </>
    )
  }
  if (!doc) {
    return (
      <>
        <TopBar title="阅读器" sub={plan?.name} />
        <div className="view-scroll"><div className="view"><p className="muted">加载中…</p></div></div>
      </>
    )
  }

  const title = paper?.title || String(meta.title_zh || meta.title_en || `文献 #${pid}`)

  return (
    <>
      <TopBar title="阅读器" sub={plan?.name} />
      <section className="reader">
        <div className="reader-toolbar">
          <button className="icon-btn" aria-label="返回文献库" title="返回文献库"
                  onClick={() => nav(link('/library'))}><IconBack /></button>
          {/* 工具栏只留标题一行（副标题与四支笔都撤了，保持简洁）：
              论文信息在正文首屏 `doc-head` + 右栏大纲里有，选笔只在浮条里（㉛）。 */}
          <div className="rt-title" style={{ flex: 1 }}>
            <div className="rt-t" title={title}>{clip(title, 44)}</div>
          </div>

          <div className="seg" role="group" aria-label="对照模式">
            {([['dual', '中英对照'], ['zh', '仅中文'], ['en', '仅原文']] as Array<[Mode, string]>).map(([k, l]) => (
              <button key={k} className={mode === k ? 'is-on' : ''} aria-pressed={mode === k}
                      onClick={() => setMode(k)}>{l}</button>
            ))}
          </div>

          <div className="seg" role="group" aria-label="字号">
            <button aria-label="缩小字号" onClick={() => setFs((v) => Math.max(FS_MIN, v - 1))}>A−</button>
            <button aria-label="放大字号" onClick={() => setFs((v) => Math.min(FS_MAX, v + 1))}>A+</button>
          </div>

          {/* ⑩ 只读：块工具（编辑译文/重译）与"标记已读"整块隐藏
              （原型 `body.readonly #markRead` + `.note-form`）。只读**仍可导出**。 */}
          {!readonly && (
            <button className="icon-btn" title={showTools ? '隐藏块工具' : '显示块工具'}
                    aria-label="切换块工具" onClick={() => setShowTools((v) => !v)}><IconOutline /></button>
          )}
          <button className="icon-btn" title="刷新本文档" aria-label="刷新"
                  disabled={refreshing} onClick={() => void refreshDoc()}>⟳</button>
          {/* ㊺ 手改进度（宿主 2026-09-19：「还需要支持手动修改进度」，入口选 **C** = 这里
              与「编辑文献信息」抽屉两处都有）。原型里这行是只读文本 `#progLbl`；
              只读分享态（`readonly`）**保持只读** —— 分享页本来就没有写入权。 */}
          {readonly ? (
            <span className="meta" id="progLbl">进度 {paper?.progress ?? 0}%</span>
          ) : progEdit !== null ? (
            <span className="meta prog-edit" id="progLbl">
              进度
              <input className="input prog-input" value={progEdit} autoFocus inputMode="numeric"
                     aria-label="阅读进度百分比" placeholder="0-100"
                     // 一获得焦点就**全选**：否则"点开 → 直接打字"会变成往旧值里插字
                     // （实测：29 → 打 5 → `529` → 夹紧成 100 写进库，看着像对、其实是错的）。
                     onFocus={(e) => e.currentTarget.select()}
                     onChange={(e) => setProgEdit(e.target.value.replace(/[^0-9]/g, '').slice(0, 3))}
                     onKeyDown={(e) => {
                       if (e.key === 'Enter') { progDone.current = true; void saveProgress(parseProgress(progEdit)) }
                       else if (e.key === 'Escape') { progDone.current = true; setProgEdit(null) }
                     }}
                     // 失焦 = 提交（不是取消）：**取消会静默吞掉刚打的字**，
                     // 那正是本项目反复踩过的"看着改了、其实没改"。Esc 才是取消。
                     // `progDone` 挡住 Enter/Esc 之后那次迟到 blur，免得同一次输入写两遍。
                     onBlur={() => {
                       if (progDone.current) return
                       progDone.current = true
                       void saveProgress(parseProgress(progEdit))
                     }} />
              %
            </span>
          ) : (
            // ⚠️ 开编辑器挂在 **mousedown**（不是 click）：`click` 在 mouseup 之后才派发，
            //    那时输入框已经渲染出来，鼠标松开的那一下会落在这个窄输入框上并**把光标放到最左**
            //    （右对齐 ⇒ 框中心在文字左边）—— 于是"点开就打字"从第 0 位插入（实测 29 → 5 得到 529）。
            //    mousedown 早于焦点/光标逻辑，输入框还没出现，尾巴落不到它身上。
            //    `onClick` 保留给**键盘**（Tab 聚焦后回车）；鼠标路径上按钮已先卸载，不会重复触发。
            <button className="meta prog-edit" id="progLbl" title="点击修改进度"
                    onMouseDown={(e) => { e.preventDefault(); openProgEdit() }}
                    onClick={openProgEdit}>
              进度 {paper?.progress ?? 0}%
            </button>
          )}
          {!readonly && (
            <button className="btn btn-secondary btn-sm"
                    disabled={paper?.status === 'reviewed' || paper?.status === 'read'}
                    onClick={() => void markRead()}>
              {paper && (paper.status === 'read' || paper.status === 'reviewed') ? '已标记完成' : '标记已读'}
            </button>
          )}
          <a className="btn btn-secondary btn-sm"
             href={share ? share.exportUrl(pid, 'dual') : api.exportUrl(pid, 'dual')}
             target="_blank" rel="noreferrer">导出</a>
        </div>

        <div className="reader-body">
          {/* 选区/划痕的鼠标事件都在舞台这一层委派（上千个块逐块挂事件没意义） */}
          <div className={`doc-stage lang-${mode}`} ref={scrollRef} onScroll={onScroll}
               onMouseUp={onStageMouseUp} onClick={stageClick}
               style={{ ['--doc-fs' as string]: `${fs}px` }}>
            {/* PDF 分页容器：按 `payload.page` 切成页（原型同款 `.pdf-page` + 页脚）。
                **标题区跟着第一页走** —— 原型也是 `idx === 0` 时才渲染 `doc-head`。 */}
            {pageGroups.map((pg, pi) => (
              <Page key={pi} no={pg.no} total={pageTotal}>
                {pi === 0 && (
                  <header className="doc-head">
                    <p className="doc-venue">
                      {[meta.journal, paper?.venue, paper?.year].filter(Boolean).join(' · ')
                        || (paper?.source === 'arxiv' ? `arXiv ${paper.source_ref || ''}` : 'PDF')}
                    </p>
                    <h1 className="doc-title">
                      {mode !== 'zh' && <span className="b-en">{String(meta.title_en || title)}</span>}
                      {/* 仅中文模式：没有中文标题就回落英文（否则整行标题空白）。
                          对照模式保持原样（有中文才给右栏），避免同一行出现两次英文。 */}
                      {mode !== 'en' && (meta.title_zh || mode === 'zh') && (
                        <span className="b-zh">{String(meta.title_zh || meta.title_en || title)}</span>
                      )}
                    </h1>
                    {meta.authors && <p className="doc-authors">{String(meta.authors)}</p>}
                    {Array.isArray(meta.affiliations) && (meta.affiliations as string[]).length > 0 && (
                      <p className="doc-affil">{(meta.affiliations as string[]).join(' · ')}</p>
                    )}
                  </header>
                )}

                {pg.blocks.map((b) => (
                  <div key={b.id}>
                    <DocBlock b={b} mode={mode} assets={assetDims}
                              selected={selBlock === b.id} flash={flashBlock === b.id}
                              onPick={setSelBlock} />
                    {!readonly && showTools && selBlock === b.id && (
                      <div className="block-tools" style={{ opacity: 1 }}>
                        {b.zh_source === 'human' && <span className="chip ok">已人工修订</span>}
                        {b.needs_review && <span className="chip warn">待校对</span>}
                        {/* 三个原语（宿主 2026-09-20）：插入 / 编辑 / 删除。
                            「插入」对**任何**块都开放（图、表旁边也常要补一句）；
                            「编辑」只对文字块开放（`payload` 类块的文字不在 en/zh 里）。 */}
                        <button className="bt-ins" title="在这一块前面插入一个新块"
                                onClick={() => openInsert(b, 'before')}>↑ 上方插入</button>
                        <button className="bt-ins" title="在这一块后面插入一个新块（拆分的一半）"
                                onClick={() => openInsert(b, 'after')}>↓ 下方插入</button>
                        {TEXT_TYPES.has(b.type) && (
                          <button title="改这一块的原文/译文/类型（拆分 = 插入 + 把字剪走）"
                                  onClick={() => openEdit(b)}>编辑此块</button>
                        )}
                        {TEXT_TYPES.has(b.type) && (
                          <button onClick={() => void retranslate(b)}>重译此块</button>
                        )}
                        <button onClick={() => { setRail('notes'); setSelBlock(b.id); setTarget(null) }}>加笔记</button>
                        <button className="bt-del" title="删除这一块（合并的一半）"
                                onClick={() => void removeBlock(b)}>删除此块</button>
                      </div>
                    )}
                  </div>
                ))}
              </Page>
            ))}
          </div>

          <aside className="reader-rail">
            <div className="rail-tabs">
              <button className={rail === 'notes' ? 'is-on' : ''}
                      onClick={() => setRail('notes')}>笔记 {notes.length}</button>
              <button className={rail === 'outline' ? 'is-on' : ''}
                      onClick={() => setRail('outline')}>大纲</button>
            </div>

            {rail === 'notes' ? (
              <>
                <div className="rail-body">
                  <NoteList notes={notes} paraIndex={paraIndex} markById={markById}
                            selHl={bar?.hlId ?? null} readonly={readonly}
                            onGoto={gotoNote}
                            onDelete={(id) => void api.deleteNote(id)
                              .then(() => setNotes((ns) => ns.filter((n) => n.id !== id)))} />
                </div>
                {!readonly && (
                  <NoteForm paperId={pid}
                            blockId={target?.segs[0]?.blockId ?? selBlock ?? null}
                            seg={target?.segs[0] ?? null}
                            hlId={target?.hlId ?? null}
                            color={pen}
                            focusKey={focusNote}
                            targetLabel={anchorLabel(
                              target?.segs[0]?.blockId ?? selBlock,
                              target?.segs[0]?.lang, target?.segs[0]?.start,
                              target?.segs[0]?.end, paraIndex)}
                            quote={target?.segs[0]?.quote ?? ''}
                            onAdded={(n) => {
                              // ⚠️ **不能 `[n, ...ns]`**：列表的顺序是「锚点在原文里的位置」
                              // （服务端 `_NOTE_ORDER`），新写的笔记落点可能在文档中间甚至最前。
                              // 位置只有服务端算得对（它才有 `blocks.ord`），所以重拉一次列表，
                              // 前端不自己插 —— 两份排序实现必然分叉。
                              void (share ? share.loadNotes(pid) : api.notes(pid))
                                .then(setNotes)
                                .catch(() => setNotes((ns) => [...ns, n]))  // 拉不到也别把笔记藏起来
                              // 服务端给这段选区**顺手补了一道划痕** → 块得重拉一次才看得见
                              // （`<mark>` 是服务端渲染进 `en_html`/`zh_html` 的，㉛ 的不变量）
                              if (n.hl_id != null && !markById.has(n.hl_id)) {
                                const fresh = n.hl_id
                                setTarget((t) => (t ? { ...t, hlId: fresh } : t))
                                setBar((b) => (b ? { ...b, hlId: fresh, kind: 'mark' } : b))
                                void refreshMarks(n.block_id ? [n.block_id] : [])
                                flash('笔记已保存，并已高亮')
                              } else {
                                setBar(null)
                                flash('笔记已保存')
                              }
                            }} />
                )}
              </>
            ) : (
              <div className="rail-body">
                {outline.length === 0 && <p className="meta">这份文档没有识别到章节标题。</p>}
                {outline.map((o) => (
                  <button key={o.id} className={`outline-item lv${o.level}${selBlock === o.id ? ' is-on' : ''}`}
                          onClick={() => jump(o.id)}>
                    <span className="oi-n">{o.num}</span>
                    <span style={{ minWidth: 0 }}>{clip(o.label, 34)}</span>
                  </button>
                ))}
                <p className="meta" style={{ marginTop: 14, lineHeight: 1.7 }}>
                  章节按原文顺序排列，点击可跳转。图表与公式保留在原文中的位置
                  {stats.figs > 0 ? `（共 ${stats.figs} 张图）` : ''}。
                </p>
                <div className="stack" style={{ marginTop: 16 }}>
                  <div className="meta">共 {stats.total} 块 · 已译 {stats.translated}</div>
                  <div className="meta">标注 {stats.marks} 处 · 笔记 {notes.length} 条</div>
                  {stats.human > 0 && <div className="meta">人工修订 {stats.human} 块</div>}
                  {stats.review > 0 && (
                    <div className="meta" style={{ color: 'var(--warn)' }}>待校对 {stats.review} 块</div>
                  )}
                </div>
              </div>
            )}
          </aside>
        </div>

        {/* 浮条（选中文字）/ 就地菜单（点已有划痕）：同一个组件的两种形态。
            固定定位到视口坐标 —— 文档舞台自己会滚，用绝对定位反而要跟着算偏移。 */}
        {!readonly && bar && (
          <div className="mark-bar" style={{ left: bar.x, top: bar.y - 12 }}>
            {HL_COLORS.map((c) => {
              const active = bar.hlId !== null
                ? markById.get(bar.hlId)?.color === c
                : pen === c
              return (
                <button key={c} className={`pen pen-${c}${active ? ' is-on' : ''}`}
                        aria-label={`用${HL_LABEL[c]}笔`} title={HL_LABEL[c]}
                        onClick={() => void paint(c)} />
              )
            })}
            <span className="mb-sep" />
            <button className="mb-btn" onClick={startNote}>加笔记</button>
            {bar.hlId !== null && (
              <button className="mb-btn mb-del"
                      onClick={() => void erase(bar.hlId as number, bar.segs.map((s) => s.blockId))}>
                擦掉
              </button>
            )}
          </div>
        )}
      </section>

      {editing && (
        <>
          <div className="drawer-mask" onClick={() => setEditing(null)} />
          <aside className="drawer">
            <header>
              <strong style={{ flex: 1 }}>
                {editing.isNew
                  ? `插入新块 · 在 ${editing.id} 的${editing.side === 'before' ? '上方' : '下方'}`
                  : `编辑块 · ${editing.id}`}
              </strong>
              <button className="ghost" onClick={() => setEditing(null)}>取消</button>
            </header>
            <div className="body stack">
              {editing.isNew && (
                <p className="meta" style={{ margin: 0, lineHeight: 1.75 }}>
                  拆分 = 在这里插入一块，再把要挪走的文字从原块<b>剪</b>过来（原块用「编辑此块」改）。
                  合并 = 把下一块的字接到这一块末尾，再用「删除此块」收掉空的那块。
                  两栏各按自己的坐标系重锚批注，<b>前后未动的块一条都不受影响</b>。
                </p>
              )}
              <div>
                <label>原文（英文）{editing.isNew && ' · 留空则是一段待补的空块'}</label>
                <textarea className="textarea" rows={7} value={editing.en}
                          placeholder="粘贴/输入这一段原文…"
                          onChange={(e) => setEditing({ ...editing, en: e.target.value })} />
              </div>
              <div>
                <label>
                  中文译文
                  {!editing.isNew && '（保存后标记为「人工修订」，重跑不会被覆盖）'}
                  {editing.isNew && '（留空会标「待校对」，可随后「重译此块」）'}
                </label>
                <textarea className="textarea" rows={7} value={editing.zh}
                          placeholder="中文…"
                          onChange={(e) => setEditing({ ...editing, zh: e.target.value })} />
              </div>
              <div>
                <label>块类型</label>
                <select className="input" value={editType} aria-label="块类型"
                        onChange={(e) => setEditType(e.target.value)}>
                  {editing.isNew
                    ? [{ v: '', label: '（跟随相邻块的类型）' }, ...TYPE_OPTIONS.slice(1)].map((o) => (
                        <option key={o.v} value={o.v}>{o.label}</option>))
                    : TYPE_OPTIONS.map((o) => (
                        <option key={o.v} value={o.v}>{o.label}</option>))}
                </select>
              </div>
              {!editing.isNew && (
                <p className="meta" style={{ margin: 0, lineHeight: 1.7 }}>
                  改了原文而没改译文 ⇒ 会如实挂「待校对」（老译文与新原文已对不上）。
                  只有**这一块**的批注会被重新锚定：写进被删掉那段文字里的划痕随之移除，
                  那里的笔记转为「文献级笔记」（内容保留）；前后块一个不动。
                </p>
              )}
              <button className="btn btn-primary" disabled={!editing.en.trim() && !editing.zh.trim()}
                      onClick={() => void saveBlock()}>
                {editing.isNew ? '插入' : '保存'}
              </button>
            </div>
          </aside>
        </>
      )}
      {toast && <div className="toast">{toast}</div>}
    </>
  )
}

/** 右栏笔记列表。**整张卡片可点 = 跳到它锚定的那一段划痕**（原型 `bindNoteGoto`），
 * 只有删除键 `stopPropagation`。卡片做成 `role="button"` + Enter/空格可触发 ——
 * 若只挂 onClick，键盘用户点不动它（原型同时绑了两套事件）。 */
function NoteList({ notes, paraIndex, markById, selHl, onGoto, onDelete, readonly }: {
  notes: Note[]
  paraIndex: Map<string, number>
  markById: Map<number, Mark>
  selHl: number | null
  onGoto: (n: Note) => void
  onDelete: (id: number) => void
  readonly?: boolean
}) {
  if (notes.length === 0) {
    return (
      <p className="meta" style={{ textAlign: 'center', padding: '30px 8px', lineHeight: 1.7 }}>
        选中正文任意一段文字即可划重点，<br />或为它写下阅读笔记。
      </p>
    )
  }
  return (
    <>
      {notes.map((n) => {
        const label = anchorLabel(n.block_id, n.lang, n.start, n.end, paraIndex)
        const hl = n.hl_id ? markById.get(n.hl_id) : undefined
        return (
          <div className={`note-card${hl && hl.id === selHl ? ' is-on' : ''}`} key={n.id}
               tabIndex={0} role="button" aria-label={`跳转到 ${label}`}
               onClick={() => onGoto(n)}
               onKeyDown={(e) => {
                 if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onGoto(n) }
               }}>
            <div className="nc-head">
              <span className="nc-q">{label}</span>
              {/* 小圆点 = 这条笔记写在一道划痕上（原型 `.nc-hl`）：颜色就是那支笔 */}
              {hl && <span className={`nc-hl pen-${hl.color}`} title="已标注" />}
              <span className="meta" style={{ marginLeft: 'auto' }}>{relTime(n.created_at)}</span>
              {/* ⑩ 原型 `body.readonly .nc-del { display: none }` —— 只读不能删分享者的笔记 */}
              {!readonly && (
                <button className="nc-del" aria-label="删除笔记"
                        onClick={(e) => { e.stopPropagation(); onDelete(n.id) }}>×</button>
              )}
            </div>
            {n.quote ? <div className="nc-q2">「{n.quote}」</div> : null}
            <div className="nc-t">{n.content}</div>
          </div>
        )
      })}
    </>
  )
}

/** 笔记表单：锚定**当前划住的那一段**（选区 / 已有划痕），没划住就锚到选中的块，
 * 都没有则是文献级 —— 原型没有"是否锚定"的开关，划了什么就是给什么写笔记。
 *
 * 锚在**选区**上、而这一段还没划过时，服务端会**顺手补一道划痕**（宿主 2026-09-13：
 * 「选中添加笔记时，应该同时自动高亮」），颜色取 `color`（工具栏当前那支笔）。 */
function NoteForm({ paperId, blockId, seg, hlId, color, targetLabel, quote, focusKey, onAdded }: {
  paperId: number
  blockId: string | null
  seg: Seg | null
  hlId: number | null
  color: HlColor
  targetLabel: string
  quote: string
  focusKey: number
  onAdded: (n: Note) => void
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const box = useRef<HTMLTextAreaElement>(null)

  // 浮条上点了「加笔记」→ 焦点直接落进文本框（focusKey 自增就是那个信号）
  useEffect(() => {
    if (focusKey > 0) box.current?.focus()
  }, [focusKey])

  async function add() {
    if (!text.trim()) return
    setBusy(true)
    try {
      const n = await api.addNote(paperId, text.trim(), {
        block_id: blockId,
        lang: seg?.lang, start: seg?.start, end: seg?.end,
        quote: quote || null, hl_id: hlId, color,
      })
      onAdded(n)
      setText('')
    } finally { setBusy(false) }
  }

  return (
    <div className="note-form">
      <div className="nf-target">{targetLabel}</div>
      {quote && <blockquote className="nf-quote">「{quote}」</blockquote>}
      <textarea className="textarea" rows={3} value={text} ref={box}
                placeholder="写下你的理解、疑问或可引用的要点…（Ctrl/Cmd + Enter 提交）"
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') void add() }} />
      <button className="btn btn-primary btn-sm" style={{ width: '100%', justifyContent: 'center', marginTop: 9 }}
              disabled={busy || !text.trim()} onClick={() => void add()}>
        保存笔记
      </button>
    </div>
  )
}
