/** 计划上下文 —— 侧栏的「当前计划」是**全局状态**，而路由只带 paperId（不带 planId）。
 *
 * 为什么要这一层（而不是每个页面各自取一遍）：
 * 1. 总览 / 文献库 / 看板全部只呈现**当前计划**（原型的 `papers()` 就是这个语义）；
 * 2. 侧栏的进度卡、导航计数、顶栏副标题都依赖同一份 `papers`；
 * 3. 计划切换必须能**整站生效**（切完停在同一条路由上，数据换掉）—— 分享态例外，
 *    ⑩ 是单计划视图，切换器里只有一个计划且不可展开（原型 `SHARE.active` 同款）。
 *
 * **只读分享复用这一整层**（决策⑩「和分享者看到的一模一样」）：只在 `load()`
 * 里把取数口换成 `/api/shares/{token}*` 白名单，页面组件完全不知情。
 *
 * `reload()` 由各页面在写完数据后调用（改状态、导入、重试转换），
 * 走同一个 ref 计数，避免每页自己维护一份 papers 副本而彼此不一致。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'

import { api } from './api'
import { useAuth } from './auth'
import { useShare } from './shareContext'
import type { Paper, Plan } from './types'

const LS_KEY = 'papershelf.activePlanId'

interface PlanValue {
  plans: Plan[]
  /** 当前计划；一个计划都没有时为 null */
  plan: Plan | null
  papers: Paper[]
  loading: boolean
  error: string
  /** 重新拉计划列表 + 当前计划的文献（写完数据后调用） */
  reload: () => Promise<void>
  switchPlan: (id: number) => void
  setPlanLocal: (p: Plan) => void
}

const Ctx = createContext<PlanValue | null>(null)

export function PlanProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const share = useShare()
  // ⚠️ `share` 是 useMemo 出来的对象，`meta` 一变它就换新身份。若把它整个放进
  //    `load` 的依赖，取回 meta → 换新 share → load 换新 → effect 重跑 → 再取一次，
  //    每轮多一次请求。这里只取**稳定**的两样：token 与 setMeta。
  const shareToken = share?.token
  const setShareMeta = share?.setMeta
  const nav = useNavigate()
  const loc = useLocation()
  const route = useParams()

  const [plans, setPlans] = useState<Plan[]>([])
  const [papers, setPapers] = useState<Paper[]>([])
  const [activeId, setActiveId] = useState<number | null>(() => {
    const v = Number(localStorage.getItem(LS_KEY))
    return Number.isFinite(v) && v > 0 ? v : null
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const seq = useRef(0)

  // 路由里若显式带了 planId（/plans/:planId），它优先于本地记忆
  const routePlanId = Number(route.planId)
  const routePlanValid = Number.isFinite(routePlanId) && routePlanId > 0

  const load = useCallback(async (targetId: number | null, quiet = false) => {
    // 分享态：匿名也能取数，走白名单端点；登录态：先看有没有登录
    if (!shareToken && !user) { setPlans([]); setPapers([]); setLoading(false); return }
    const mine = ++seq.current
    if (!quiet) setLoading(true)
    try {
      if (shareToken) {
        const [list, payload] = await Promise.all([
          api.sharedPlans(shareToken), api.sharedPlan(shareToken),
        ])
        if (mine !== seq.current) return
        setPlans(list)
        setActiveId(payload.plan.id)
        setPapers(payload.papers)
        // 分享页 banner 的倒计时读它（匿名访客没有管理端可查）
        setShareMeta?.(payload.share ?? null)
        setError('')
        return
      }
      const list = await api.plans()
      if (mine !== seq.current) return
      setPlans(list)
      const wanted = targetId && list.some((p) => p.id === targetId)
        ? targetId
        : (list[0]?.id ?? null)
      setActiveId(wanted)
      if (wanted) localStorage.setItem(LS_KEY, String(wanted))
      setPapers(wanted ? await api.papers(wanted) : [])
      if (mine !== seq.current) return
      setError('')
    } catch (e) {
      if (mine === seq.current) setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      if (mine === seq.current) setLoading(false)
    }
  }, [user, shareToken, setShareMeta])

  useEffect(() => {
    const want = routePlanValid ? routePlanId : activeId
    void load(want)
    // activeId 不进依赖：它由 load 内部回写，进来会自激
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, routePlanValid, routePlanId])

  const reload = useCallback(async () => { await load(activeId, true) }, [load, activeId])

  /* ── 转换进行中时的轻量轮询（2026-09-12）─────────────────────────────
   * 为什么需要：转换现在由**服务端队列**在后台依次跑（`server/queue.py`），
   * 前端除了"导入完那一刻"再没有别的刷新时机 —— 传 12 篇时界面会永远停在
   * 「转换中 12」，用户只能手动刷新才看到一篇篇变「已生成」。
   *
   * ⚠️ 只在**真有活儿**（有 queued/doing）时才开表：安静时不打服务器。
   * 一旦全部落定就自动停（`papers` 一变即重新判断），不会长期挂着空转。
   * 间隔 4s 是权衡：比服务端 3s 轮询略慢，避免"前端刚刷完队列又没动"的空拍。
   */
  const busyCount = papers.filter(
    (p) => p.conv_state === 'queued' || p.conv_state === 'doing').length
  useEffect(() => {
    if (busyCount === 0) return
    const t = window.setInterval(() => { void load(activeId, true) }, 4000)
    return () => window.clearInterval(t)
    // activeId 不进依赖的理由同上（由 load 内部回写）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busyCount, load])

  const switchPlan = useCallback((id: number) => {
    // ⑩ 分享是单计划视图：原型 `switchPlan` 直接 toast 拒绝（切换器也只有一个计划）
    if (shareToken) return
    setActiveId(id)
    localStorage.setItem(LS_KEY, String(id))
    void load(id)
    // 切计划时停在同一条路由上换数据；若当前正看着某篇文献的阅读器，
    // 那篇多半不属于新计划 → 退回总览（否则会 404 或串计划）
    if (/^\/(reader|library|board)\//.test(loc.pathname)) nav('/')
  }, [load, loc.pathname, nav, shareToken])

  const setPlanLocal = useCallback((p: Plan) => {
    setPlans((ps) => ps.map((x) => (x.id === p.id ? p : x)))
  }, [])

  const plan = useMemo(
    () => plans.find((p) => p.id === activeId) ?? plans[0] ?? null,
    [plans, activeId],
  )

  const value: PlanValue = {
    plans, plan, papers, loading, error, reload, switchPlan, setPlanLocal,
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function usePlan(): PlanValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('usePlan 必须在 PlanProvider 内使用')
  return v
}
