/** 应用外壳：左侧边栏 + 顶栏 + 主区（原型 `.app` / `.sidebar` / `.topbar` / `.main`）。
 *
 * 侧栏的五项导航与原型一一对应，且**只呈现当前计划**（原型的 `papers()` 语义）：
 * 总览 / 文献库 / 阅读器 / 进度看板 / 阅读计划（+ 管理员才可见的用户管理）。
 *
 * 为什么计划切换器放在侧栏而不是独立页面：原型就是这么用的 —— 计划是"工作台的门"，
 * 切换要一步可达，且切换后整站（含侧栏进度卡与计数）同步刷新。
 *
 * ── 只读分享（⑩）复用同一套外壳 ──
 * 原型进分享模式只做三件事：给外壳挂 `readonly`、顶栏多一枚「只读分享」pill、
 * 下方多一条 `.share-banner`。侧栏的每一项都照旧（切换器不可展开、用户区隐藏、
 * 导航照常可点）。这里同样只做这三件事，**不另写一套外壳**。
 */

import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { api } from '../api'
import { useAuth } from '../auth'
import {
  IconBoard, IconChevron, IconClock, IconGrid, IconLayers, IconLibrary, IconLogout, IconPlus,
  IconSearch, IconShare, IconUsers,
} from '../icons'
import { usePlan } from '../planContext'
import { useLink, useShare } from '../shareContext'
import { fmtCountdown, isExpiring, useCountdown } from '../shareCountdown'
import { isDone } from '../vocab'
import { NotFound } from '../components'
import { AppRoutes } from '../routes'
import { CreateShareModal } from '../pages/ShareModal'

/** 侧栏「分享管理」上的计数 = **当前有效**（含即将到期）的链接条数。
 *
 * 为什么要单独取一次而不复用管理页那份：侧栏在每个视图都渲染，管理页只是其中一个；
 * 让侧栏依赖管理页的数据，就得把分享列表提到全局 Provider，而只有侧栏这一处需要它。
 * 取数失败一律回 `null`（不显示计数），**绝不阻塞或报错** —— 侧栏计数挂掉不该影响主流程。
 *
 * `enabled` 为 null 表示"当前是只读分享态"：匿名访客不查、也不显示。
 * 60 秒重取一次：链接过期后那个数字要自己减下去。
 */
function useLiveShareCount(enabled: unknown): number | null {
  const [n, setN] = useState<number | null>(null)
  useEffect(() => {
    if (!enabled) { setN(null); return }
    let alive = true
    const tick = async () => {
      try {
        const rows = await api.allShares()
        if (alive) setN(rows.filter((s) => s.state === 'active' || s.state === 'expiring').length)
      } catch { if (alive) setN(null) }
    }
    void tick()
    const t = setInterval(() => void tick(), 60_000)
    return () => { alive = false; clearInterval(t) }
  }, [enabled])
  return n
}

const NAV: Array<{
  to: string
  key: string
  label: string
  Icon: (p: { className?: string }) => React.ReactElement
  count?: 'papers' | 'plans' | 'shares'
}> = [
  { to: '/', key: 'overview', label: '总览', Icon: IconGrid },
  { to: '/library', key: 'library', label: '文献库', Icon: IconLibrary, count: 'papers' },
  { to: '/board', key: 'board', label: '进度看板', Icon: IconBoard },
  { to: '/plans', key: 'plans', label: '阅读计划', Icon: IconLayers, count: 'plans' },
  { to: '/shares', key: 'shares', label: '分享管理', Icon: IconShare, count: 'shares' },
]

function PlanSwitcher() {
  const { plans, plan, papers, switchPlan } = usePlan()
  const share = useShare()
  const nav = useNavigate()
  const link = useLink()
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const mark = (plan?.name || '计').slice(0, 1)

  return (
    <div className="plan-wrap" ref={wrap}>
      <button className="plan-switch" aria-haspopup="menu" aria-expanded={open}
              aria-label="切换阅读计划" onClick={() => { if (share) return; setOpen((v) => !v) }}>
        <span className="ps-mark">{mark}</span>
        <span className="ps-txt">
          <span className="ps-name">{plan?.name || '还没有计划'}</span>
          {/* 原型的 `ps-sub` 是手写常量「当前阅读计划」；这里让它带上有用的信息，
              分享态下额外点明「来自分享链接」（⑩ 单计划视图）。 */}
          <span className="ps-sub">{share ? '只读分享' : plan ? `${papers.length} 篇文献` : '当前阅读计划'}</span>
        </span>
        {!share && <IconChevron className="ps-chev" />}
      </button>

      {open && !share && (
        <div className="plan-pop is-open" role="menu" aria-label="阅读计划列表">
          {plans.map((p) => {
            const on = p.id === plan?.id
            return (
              <button key={p.id} className={`pp-item${on ? ' is-on' : ''}`} role="menuitem"
                      onClick={() => { switchPlan(p.id); setOpen(false) }}>
                <span className="pp-mark">{(p.name || '计').slice(0, 1)}</span>
                <span className="pp-txt">
                  <span className="pp-n">{p.name}</span>
                  <span className="pp-s">
                    {p.paper_count} 篇 · {p.paper_count
                      ? Math.round((p.done_count / p.paper_count) * 100) : 0}%
                  </span>
                </span>
              </button>
            )
          })}
          <div className="pp-sep" />
          <button className="pp-item" role="menuitem"
                  onClick={() => { setOpen(false); nav(link('/plans?new=1')) }}>
            <span className="pp-mark"><IconPlus /></span>
            <span className="pp-txt"><span className="pp-n">新建阅读计划</span></span>
          </button>
        </div>
      )}
    </div>
  )
}

export function Sidebar() {
  const { user, logout } = useAuth()
  const { plans, plan, papers } = usePlan()
  const share = useShare()
  const link = useLink()
  const loc = useLocation()

  const done = papers.filter((p) => isDone(p.status)).length
  const total = papers.length
  const pct = total ? Math.round((done / total) * 100) : 0
  // 分享态：token 前缀要从 pathname 里剥掉才能正确高亮导航
  const sub = share ? loc.pathname.replace(`/share/${share.token}`, '') || '/' : loc.pathname
  const activeKey = sub.startsWith('/library') ? 'library'
    : sub.startsWith('/board') ? 'board'
      : sub.startsWith('/plans') ? 'plans'
        : sub.startsWith('/shares') ? 'shares'
        : sub.startsWith('/admin') ? 'admin'
          // 阅读器不再是侧栏的一项（宿主 2026-09-13）：它是**从文献库点进去的**
          // 二级页面，所以在侧栏里高亮的是「文献库」（原型 L1665 同款）。
          : sub.startsWith('/reader') || /^\/papers\/\d+/.test(sub) ? 'library'
            : 'overview'

  // 分享计数：侧栏显示**有效条数**（含即将到期），与原型 `navCountShares` 同口径 ——
  // 显示"历史总条数"会让这个数字只增不减，看不出当前有没有对外开着的链接。
  const liveShares = useLiveShareCount(share ? null : user)

  const countOf = (kind?: string) => kind === 'papers' ? papers.length
    : kind === 'plans' ? plans.length
      : kind === 'shares' && liveShares !== null ? liveShares : null

  return (
    <aside className="sidebar">
      <Link className="brand" to={link('/')} title="papershelf">
        <span className="brand-mark">文</span>
        <span>
          <span className="brand-name">文献台</span>
          <span className="brand-sub">PaperDesk</span>
        </span>
      </Link>

      <PlanSwitcher />

      <nav className="sidenav">
        {NAV.map(({ to, key, label, Icon, count }) => {
          // ⑩ 只读分享态：分享管理是写入口（建/撤/续），匿名访客一律不给入口
          // （原型 `body.readonly #navShares` 同款）
          if (share && key === 'shares') return null
          const n = countOf(count)
          return (
            <Link key={key} to={link(to)}
                  className={`nav-item${activeKey === key ? ' is-active' : ''}`}>
              <Icon />
              {label}
              {n !== null && <span className="nav-count">{n}</span>}
            </Link>
          )
        })}
        {!share && user?.is_admin && (
          <Link to="/admin" className={`nav-item${activeKey === 'admin' ? ' is-active' : ''}`}>
            <IconUsers />
            用户管理
          </Link>
        )}
      </nav>

      <div className="side-sep" />

      <Link className="side-goal" to={link('/plans')} aria-label="查看阅读计划详情">
        <div className="sg-top">
          <span className="eyebrow">当前计划</span>
          <span className="meta">{plan?.goal ? `${plan.goal} 篇` : '未设目标'}</span>
        </div>
        <div className="sg-pct">{pct}%</div>
        <div className="mini accent" style={{ width: '100%', height: 6, margin: '10px 0 8px' }}>
          <i style={{ width: `${pct}%` }} />
        </div>
        <div className="meta">已精读 {done} / 已入库 {total} 篇</div>
      </Link>

      {/* ⑩ 原型：`body.readonly .side-user { display: none }` —— 匿名访客没有账号区 */}
      {!share && (
        <div className="side-user">
          <span className="avatar">
            {((user?.display_name || user?.email || '用').trim()[0] || '用').toUpperCase()}
          </span>
          <span className="su-id">
            <span className="su-name">{user?.display_name || user?.email?.split('@')[0]}</span>
            <span className="su-sub">{user?.email}</span>
          </span>
          <button className="icon-btn su-out" title="退出登录" aria-label="退出登录"
                  onClick={() => void logout()}>
            <IconLogout />
          </button>
        </div>
      )}
    </aside>
  )
}

/** 顶栏：标题 + 副标题 + 全局搜索 + 分享/导入动作。
 *
 * 搜索框把关键词落到 `?q=`（文献库读它）—— 这样"搜索"是一个**可分享、可回退**的状态，
 * 而不是藏在组件里的局部变量；原型也是这个行为（顶栏搜索直接驱动文献库筛选）。
 *
 * ⑩ 只读态：搜索照旧（这是读）、`导入文献` / `分享` 两个写入口整块不渲染
 * （原型 `body.readonly #importBtn, #shareBtn { display: none }`），
 * 换成一枚「只读分享」pill。
 */
export function TopBar({ title, sub, actions }: {
  title: string
  sub?: string
  actions?: React.ReactNode
}) {
  const { plan, papers } = usePlan()
  const share = useShare()
  const link = useLink()
  const nav = useNavigate()
  const loc = useLocation()
  const urlQ = new URLSearchParams(loc.search).get('q') || ''
  // 输入框是本地受控的（否则每敲一个字都会改 URL），但 URL 变了要跟上 ——
  // 用"上一次看到的 URL 关键词"做哨兵，在渲染期重同步，比 effect 少一帧闪烁。
  const [q, setQ] = useState(urlQ)
  const [sharing, setSharing] = useState(false)
  const [syncedFrom, setSyncedFrom] = useState(urlQ)
  if (syncedFrom !== urlQ) {
    setSyncedFrom(urlQ)
    setQ(urlQ)
  }

  function submit(e: React.FormEvent) {
    e.preventDefault()
    const params = new URLSearchParams(q.trim() ? { q: q.trim() } : {})
    nav(`${link('/library')}${params.toString() ? `?${params}` : ''}`)
  }

  return (
    <>
    <header className="topbar">
      <div className="tb-title">
        <h1>{title}</h1>
        {sub && <span className="tb-sub">{sub}</span>}
      </div>
      <div className="tb-actions">
        {share && <span className="pill st-reading">只读分享</span>}
        <form className="search" onSubmit={submit} role="search">
          <IconSearch />
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)}
                 placeholder="搜索标题、作者、标签…" aria-label="搜索文献" />
        </form>
        {actions}
        {!share && (
          <>
            {/* 原型 `#shareBtn` 点开的是**新建分享模态**（不是列表页）。列表在
                侧栏「分享管理」里 —— 顶栏是"我现在要分享"，侧栏是"我分享过什么"。 */}
            <button className="btn btn-secondary" title="分享当前计划"
                    onClick={() => setSharing(true)}>
              <IconShare />分享
            </button>
            <Link className="btn btn-primary" to={`/plans?import=${plan?.id ?? ''}`}
                  title={plan ? `向「${plan.name}」导入文献` : '导入文献'}>
              <IconPlus />导入文献
            </Link>
          </>
        )}
        {papers.length === 0 && plan && (
          <span className="meta" style={{ whiteSpace: 'nowrap' }}>计划为空</span>
        )}
      </div>
    </header>
    {sharing && (
      <CreateShareModal planId={plan?.id} onClose={() => setSharing(false)}
                        onCreated={() => { /* 列表页有自己的刷新；这里只是建完给链接 */ }} />
    )}
    </>
  )
}

/** 只读分享的提示带（原型 `.share-banner`）。
 *
 * 位置在**顶栏之下、视图之上** —— 属于外壳而不是某个页面，所以五个视图都带着它。
 * 尾部的按钮在原型里是「创建我的文献台」（原型没有后端，点了只是清掉 hash）；
 * 这里指向真实的注册页，是这条横幅唯一诚实的去处。
 *
 * 2026-09-12 新增**有效期倒计时**（宿主：「分享后的只读页面可以看到有效期倒计时」）：
 * 剩余秒数由服务端随载荷下发（`ShareMeta.seconds_left`），这里只递减。
 * 走完那一刻不再只是停在 `00:00:00` —— 弹失效浮层（原型 `showShareExpired`）。
 */
export function ShareBanner() {
  const { plan } = usePlan()
  const share = useShare()
  const nav = useNavigate()
  const [dead, setDead] = useState(false)

  const left = useCountdown(share?.meta?.seconds_left ?? 0, Boolean(share?.meta))
  // 倒计时归零：加载后就把到期那一刻当作"已过期"。
  // ⚠️ 必须判 `meta` 是否存在 —— 载荷还没回来时 `seconds_left` 也是 0，
  //    不判就会在正常链接上闪一下失效浮层。
  useEffect(() => {
    if (share?.meta && left <= 0) setDead(true)
  }, [share?.meta, left])

  return (
    <>
      <div className="share-banner">
        <span className="sb-dot" />
        <span>只读分享 · <b>{plan?.name || '阅读计划'}</b></span>
        {share?.meta && left > 0 && (
          <span className="cd-wrap">
            有效期剩余
            <b className={`cd${isExpiring(left) ? ' is-warn' : ''}`}>{fmtCountdown(left)}</b>
          </span>
        )}
        <span className="sb-note">通过分享链接打开，内容不可编辑</span>
        <button className="btn btn-secondary btn-sm" onClick={() => nav('/register')}>
          创建我的文献台
        </button>
      </div>
      {dead && <ShareExpired onExit={() => nav('/register')} />}
    </>
  )
}

/** 链接失效浮层（原型 `.share-dead` / `showShareExpired`）。
 *
 * 倒计时归零时盖住整页：让访客继续读一份"看起来正常、实际已失效"的内容，
 * 比直接告诉他更糟 —— 他可能正把这篇当成最新版本在看。
 */
function ShareExpired({ onExit }: { onExit: () => void }) {
  return (
    <div className="share-dead is-open" role="alertdialog" aria-modal="true">
      <div className="sd-card">
        <div className="sd-icon"><IconClock /></div>
        <h2>分享链接已过期</h2>
        <p>分享有效期最长为 24 小时，此链接已超过有效期。请联系分享者重新生成。</p>
        <button className="btn btn-primary" onClick={onExit}>创建我的文献台</button>
      </div>
    </div>
  )
}

/** 只读分享外壳：**与 PrivateShell 同构**，差别只有 banner 与只读态。
 *
 * 为什么这里也要判一次 `error`：令牌失效（撤销/过期/不存在）时，`PlanProvider`
 * 的取数会静默失败，页面就会渲染成"空计划 + 0 篇"的死页面 —— 匿名访客完全不知道
 * 链接为什么没用。所以守卫在这一层给出口（复用 `NotFound`，它就是为失效链接写的）。
 * 守卫所需的取数由 `PlanProvider` 完成（它已经在拉同一份数据），这里只读 `error`，
 * **不重复发请求**。
 */
export function ShareShell({ children }: { children?: React.ReactNode }) {
  const { plan, error, loading } = usePlan()
  const share = useShare()

  if (!share) return <NotFound title="链接不完整" hint="分享链接缺少令牌，请让分享者重新复制。" />
  if (error) return <div className="main"><NotFound title="链接不可用" hint={error} /></div>
  // 首次加载（还没有任何计划）时先别下结论：可能只是还没回来
  if (!plan && loading) {
    return <div className="main"><div className="auth-wrap"><p className="muted">加载中…</p></div></div>
  }

  return (
    <div className="app readonly">
      <Sidebar />
      <div className="main">
        <ShareBanner />
        {children ?? <AppRoutes />}
      </div>
    </div>
  )
}

/** 占位：尚无计划时整站都用它（而不是让每个页面各写一份空状态）。 */
export function NoPlan() {
  const link = useLink()
  return (
    <div className="view">
      <div className="card">
        <h2 className="h3">还没有阅读计划</h2>
        <p className="muted" style={{ marginTop: 6 }}>
          一个计划 = 一个研究方向或一门课。文献、术语表与进度都挂在计划内。
        </p>
        <div className="row" style={{ marginTop: 16 }}>
          <Link className="btn btn-primary" to={link('/plans?new=1')}><IconPlus />新建阅读计划</Link>
        </div>
      </div>
    </div>
  )
}
