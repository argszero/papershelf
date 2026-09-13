/** 可复用小组件：计划卡片、状态药丸、分布条、完成度圆环。
 *
 * 抽出来的理由：这些形状在**多个视图**里重复出现（计划卡在计划页与侧栏切换器；
 * 状态药丸在文献库/看板/总览；圆环与堆叠条在总览与计划卡），
 * 原型里是靠字符串拼 HTML 复制的 —— 组件化后只有一份。
 */

import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { isDone, STATUS, STATUS_ORDER } from './vocab'
import type { Paper, PaperStatus, Plan } from './types'

export function Pill({ status }: { status: PaperStatus }) {
  const s = STATUS[status]
  return <span className={`pill ${s.cls}`}>{s.label}</span>
}

/** 四态分布条：颜色即数据，宽度按占比。 */
export function StatusBar({ counts, total }: { counts: Record<string, number>; total: number }) {
  if (!total) return <div className="stackbar" />
  return (
    <div className="stackbar" role="img" aria-label="阅读状态分布">
      {STATUS_ORDER.map((k) => {
        const n = counts[k] || 0
        if (!n) return null
        return <i key={k} style={{ width: `${(n / total) * 100}%`, background: STATUS[k].color }} />
      })}
    </div>
  )
}

/** 完成度圆环（原型 138px 甜甜圈）。 */
export function Ring({ pct, label, center }: { pct: number; label: string; center?: ReactNode }) {
  const r = 60
  const c = 2 * Math.PI * r
  const off = c * (1 - Math.max(0, Math.min(100, pct)) / 100)
  return (
    <div className="ring-center">
      <svg className="ring" viewBox="0 0 138 138" role="img" aria-label={`${label} ${pct}%`}>
        <circle cx="69" cy="69" r={r} fill="none" strokeWidth="9" style={{ stroke: 'var(--fg-soft)' }} />
        <circle cx="69" cy="69" r={r} fill="none" strokeWidth="9" strokeLinecap="round"
                style={{ stroke: 'var(--accent)' }} strokeDasharray={c.toFixed(1)}
                strokeDashoffset={off.toFixed(1)} transform="rotate(-90 69 69)" />
      </svg>
      <span className="rc-val"><b>{pct}%</b><span>{center ?? label}</span></span>
    </div>
  )
}

export function Legend({ counts }: { counts: Record<string, number> }) {
  return (
    <div className="legend">
      {STATUS_ORDER.map((k) => (
        <div className="legend-row" key={k}>
          <span className="sw" style={{ background: STATUS[k].color }} />
          <span className="lt">{STATUS[k].label}</span>
          <span className="lv">{counts[k] || 0}</span>
        </div>
      ))}
    </div>
  )
}

export function Tile({ k, v, unit, d }: { k: string; v: ReactNode; unit?: string; d?: string }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className="v">{v}{unit && <span className="u">{unit}</span>}</div>
      {d && <div className="d">{d}</div>}
    </div>
  )
}

/** 计划卡（原型 `.plan-card`）—— 计划页与"切换计划"共用。 */
export function PlanCard({ plan, active, papers, onOpen, onEdit, onDelete, onShare, onGlossary, onImport, readonly }: {
  plan: Plan
  active: boolean
  /** 当前计划的文献（只有当前计划才有；其它计划用后端给的计数） */
  papers?: Paper[]
  onOpen?: () => void
  onEdit?: () => void
  onDelete?: () => void
  onShare?: () => void
  onGlossary?: () => void
  onImport?: () => void
  readonly?: boolean
}) {
  const total = papers ? papers.length : plan.paper_count
  const done = papers ? papers.filter((p) => isDone(p.status)).length : plan.done_count
  const pct = total ? Math.round((done / total) * 100) : 0

  return (
    <article className={`plan-card${active ? ' is-active' : ''}`}>
      <div className="pc-head">
        <span className="pc-mark">{(plan.name || '计').slice(0, 1)}</span>
        {active
          ? <span className="pill st-reading">当前计划</span>
          : <span className="meta">{total} 篇</span>}
      </div>
      <h3 className="pc-t">{plan.name}</h3>
      <p className="pc-d">{plan.description || '尚未填写计划说明'}</p>
      <div className="mini accent" style={{ width: '100%', height: 6, margin: '14px 0 8px' }}>
        <i style={{ width: `${pct}%` }} />
      </div>
      <div className="pc-stats">
        <span className="meta">已精读 {done}</span>
        <span className="meta">目标 {plan.goal ? `${plan.goal} 篇` : '未设'}</span>
        <span className="meta">完成 {pct}%</span>
      </div>
      <div className="pc-foot">
        {/* ⑩ 只读分享：整条动作栏换成"打开"（原型 `body.readonly [data-share-card],
            [data-rename], [data-del-plan] { display: none }`，只留 `data-open-plan`）。 */}
        {readonly
          ? <button className="btn btn-secondary btn-sm" onClick={onOpen}>打开</button>
          : (
            <>
              {active && papers
                ? <Link className="btn btn-primary btn-sm" to="/">查看总览</Link>
                : <button className="btn btn-primary btn-sm" onClick={onOpen}>切换到此计划</button>}
              {onImport && <button className="btn btn-ghost btn-sm" onClick={onImport}>导入</button>}
              {onShare && <button className="btn btn-ghost btn-sm" onClick={onShare}>分享</button>}
              {onGlossary && <button className="btn btn-ghost btn-sm" onClick={onGlossary}>术语表</button>}
              {onEdit && <button className="btn btn-ghost btn-sm" onClick={onEdit}>重命名</button>}
              {onDelete && (
                <button className="btn btn-ghost btn-sm" style={{ color: 'var(--danger)', marginLeft: 'auto' }}
                        onClick={onDelete}>删除</button>
              )}
            </>
          )}
      </div>
    </article>
  )
}

/** 路由兜底页（未匹配的路径 & 失效的分享链接）。
 *
 *  原来叫 `ShareNotFound`，但它现在也兜 `/anything` 这类打错的地址，
 *  所以搬到 components 里按用途命名 —— 分享页只管分享。 */
export function NotFound({ title = '页面不存在', hint }: { title?: string; hint?: string }) {
  return (
    <div className="auth-wrap">
      <div className="card" style={{ maxWidth: 440, width: '100%' }}>
        <h1 className="h2" style={{ fontSize: 20 }}>{title}</h1>
        <p className="meta" style={{ marginTop: 8, lineHeight: 1.7 }}>
          {hint || '地址可能已失效，检查一下链接。'}
        </p>
        <Link className="btn btn-secondary" style={{ marginTop: 14 }} to="/login">前往登录</Link>
      </div>
    </div>
  )
}
