/** 总览 —— 原型的 `renderOverview`。
 *
 * 六个板块，全部只呈现**当前计划**的文献：
 *   ① 四张指标卡      ② 精读完成度（圆环 + 图例 + 堆叠条）+ 转换流水线
 *   ③ 阅读节奏（日/周柱状图 + 四项统计）+ 最近阅读
 *   ④ 需要关注（失败/久未开始/进度落后）+ 进度规划
 *
 * ⚠️ 阅读节奏的数据来自 `papers.status_at`（⑰：每次改状态都会盖时间戳）。
 * 这是本页**唯一**的推算成分，也是唯一一处必须诚实的地方：
 * 它统计的是"**被标记为已读/已整理**的时间"，不是真实的阅读行为。
 * 所以文案写「完成」而非「阅读」，并且旁边直接标出最大偏差来源。
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { IconAlert, IconClock, IconFile, IconLang } from '../icons'
import { usePlan } from '../planContext'
import { useLink, useReadonly } from '../shareContext'
import { NoPlan, TopBar } from '../shell'
import { Legend, Ring, StatusBar, Tile } from '../components'
import {
  CONV, CONV_ORDER, dayKey, daysSince, fmtDate, isDone, relTime, startOfWeek,
} from '../vocab'
import type { Paper } from '../types'

type Range = 'd7' | 'd30' | 'w12'

const RANGES: Array<{ k: Range; label: string }> = [
  { k: 'd7', label: '近 7 天' },
  { k: 'd30', label: '近 30 天' },
  { k: 'w12', label: '近 12 周' },
]

function countBy<T extends string>(papers: Paper[], get: (p: Paper) => T): Record<string, number> {
  const out: Record<string, number> = {}
  for (const p of papers) { const v = get(p); out[v] = (out[v] || 0) + 1 }
  return out
}

export function OverviewPage() {
  const { plan, papers, loading } = usePlan()
  const link = useLink()
  const readonly = useReadonly()
  const [range, setRange] = useState<Range>('d7')

  const byStatus = countBy(papers, (p) => p.status)
  const byConv = countBy(papers, (p) => p.conv_state)
  const done = papers.filter((p) => isDone(p.status)).length
  const n = papers.length
  const pct = n ? Math.round((done / n) * 100) : 0

  if (!plan) return <><TopBar title="总览" sub="还没有阅读计划" /><NoPlan /></>

  return <OverviewBody {...{ plan, papers, link, readonly, loading, range, setRange, byStatus, byConv, done, n, pct }} />
}

/** 分出来纯粹是因为上面那个早退分支必须**在 hooks 之前**（React 的规则）。 */
function OverviewBody({ plan, papers, link, readonly, loading, range, setRange, byStatus, byConv, done, n, pct }: {
  link: (p: string) => string
  readonly: boolean
  plan: NonNullable<ReturnType<typeof usePlan>['plan']>
  papers: Paper[]
  loading: boolean
  range: Range
  setRange: (r: Range) => void
  byStatus: Record<string, number>
  byConv: Record<string, number>
  done: number
  n: number
  pct: number
}) {
  // 「已完成的时刻」= 状态时间戳；未读的没有时间戳
  const stamped = papers.filter((p) => isDone(p.status) && p.status_at)

  const pace = useMemo(() => buildPace(stamped, range), [stamped, range])

  const recent = useMemo(
    () => [...papers].sort((a, b) => (b.status_at || b.created_at).localeCompare(a.status_at || a.created_at)).slice(0, 5),
    [papers],
  )

  /** 需要关注：三类真实可判定的事件（不是原型的演示数据）。 */
  const attention = useMemo(() => buildAttention(papers), [papers])

  if (loading && n === 0) {
    return <><TopBar title="总览" /><div className="view"><p className="muted">加载中…</p></div></>
  }

  return (
    <>
      <TopBar title="总览" sub={`${plan.name} · 计划 ${plan.goal ?? '—'} 篇`} />
      <div className="view-scroll">
        <div className="view">
          <section className="grid g-4" style={{ marginBottom: 16 }}>
            <Tile k="阅读目标" v={plan.goal ?? '—'} unit={plan.goal ? '篇' : ''}
                  d={plan.goal ? `已入库 ${n} 篇 · 完成 ${n ? Math.round((n / plan.goal) * 100) : 0}%`
                    : '可在「阅读计划」里设置'} />
            <Tile k="已精读" v={done} unit="篇"
                  d={n ? `占已入库 ${pct}%` : '还没有文献'} />
            <Tile k="中文版已生成" v={byConv.done || 0} unit="篇"
                  d={`转换中 ${(byConv.doing || 0) + (byConv.queued || 0)} · 待转换 ${byConv.none || 0}${byConv.failed ? ` · 失败 ${byConv.failed}` : ''}`} />
            <Tile k={range === 'w12' ? '近 12 周完成' : range === 'd30' ? '近 30 天完成' : '近 7 天完成'}
                  v={pace.total} unit="篇" d={n ? `占已入库 ${Math.round((pace.total / n) * 100)}%` : '—'} />
          </section>

          <section className="grid g-2" style={{ gridTemplateColumns: '1.35fr 1fr', marginBottom: 16 }}>
            <div className="card">
              <div className="card-h">
                <div>
                  <span className="eyebrow">阅读进度</span>
                  <h3 className="h3" style={{ marginTop: 5 }}>库内精读完成度</h3>
                </div>
                <span className="meta">目标 {plan.goal ?? '—'} 篇</span>
              </div>
              <div className="ring-wrap">
                <Ring pct={pct} label="已精读" />
                <Legend counts={byStatus} />
              </div>
              <div style={{ marginTop: 20 }}><StatusBar counts={byStatus} total={n} /></div>
              <p className="meta" style={{ marginTop: 12 }}>
                「已精读」= 已读 + 已整理，与进度看板的第四列口径一致。
              </p>
            </div>

            <div className="card">
              <div className="card-h">
                <div>
                  <span className="eyebrow">转换流水线</span>
                  <h3 className="h3" style={{ marginTop: 5 }}>中文版生成</h3>
                </div>
                <Link className="btn btn-ghost btn-sm" to={link('/library')}>查看文献库</Link>
              </div>
              {CONV_ORDER.map((k) => (
                <div className="pipe-row" key={k}>
                  <span className="pr-dot" style={{ background: CONV[k].color }} />
                  <span className="pr-name">{CONV[k].label}</span>
                  <span className="pr-n">{byConv[k] || 0}</span>
                </div>
              ))}
              <p className="meta" style={{ marginTop: 14 }}>
                已生成中文版占已入库的 {n ? Math.round(((byConv.done || 0) / n) * 100) : 0}%。
                未转换的文献无法进入中文精读。
              </p>
            </div>
          </section>

          <section className="grid g-2" style={{ gridTemplateColumns: '1.35fr 1fr', marginBottom: 16 }}>
            <div className="card">
              <div className="card-h wrap">
                <div>
                  <span className="eyebrow">阅读节奏</span>
                  <h3 className="h3" style={{ marginTop: 5 }}>精读产出趋势</h3>
                </div>
                <div className="seg" role="group" aria-label="选择时间范围">
                  {RANGES.map((r) => (
                    <button key={r.k} className={range === r.k ? 'is-on' : ''}
                            aria-pressed={range === r.k} onClick={() => setRange(r.k)}>{r.label}</button>
                  ))}
                </div>
              </div>
              <PaceFig pace={pace} />
            </div>

            <div className="card">
              <div className="card-h">
                <div>
                  <span className="eyebrow">最近阅读</span>
                  <h3 className="h3" style={{ marginTop: 5 }}>继续上次的进度</h3>
                </div>
                <span className="meta">{recent.length} 篇</span>
              </div>
              {recent.length === 0 && <p className="muted">还没有文献。</p>}
              {recent.map((p) => (
                <Link className="recent-row" key={p.id} to={link(`/reader/${p.id}`)}>
                  <div style={{ minWidth: 0 }}>
                    <div className="rr-t">{p.title || `文献 #${p.id}`}</div>
                    <div className="rr-s">
                      {[p.authors, p.venue, p.year].filter(Boolean).join(' · ') || '未提取到元数据'}
                    </div>
                  </div>
                  <div className="rr-p">
                    <span className="meta">{p.progress}%</span>
                    <div className="mini accent" style={{ marginTop: 5, width: 60 }}>
                      <i style={{ width: `${p.progress}%` }} />
                    </div>
                  </div>
                </Link>
              ))}
            </div>
          </section>

          <section className="grid g-2">
            <div className="card">
              <div className="card-h"><span className="eyebrow">需要关注</span></div>
              {attention.length === 0 && (
                <p className="muted">没有需要处理的事项 —— 没有转换失败，也没有长期停摆的文献。</p>
              )}
              {attention.map((a, i) => (
                <div className={`attn-row${a.tone ? ` is-${a.tone}` : ''}`} key={i}>
                  <span className="ar-ic">{a.Icon ? <a.Icon /> : <IconLang />}</span>
                  <div style={{ minWidth: 0 }}>
                    <div className="ar-t">{a.title}</div>
                    <div className="ar-s">{a.sub}</div>
                  </div>
                  {a.to && <Link className="btn btn-ghost btn-sm" to={link(a.to)} style={{ marginLeft: 'auto' }}>前往</Link>}
                </div>
              ))}
            </div>

            <div className="card">
              <div className="card-h"><span className="eyebrow">计划进度</span></div>
              {plan.goal ? (
                <>
                  <p className="h3" style={{ fontSize: 15, marginBottom: 6 }}>
                    还差 {Math.max(0, plan.goal - n)} 篇达成目标
                  </p>
                  <p className="meta" style={{ lineHeight: 1.7 }}>
                    已入库 {n} 篇，目标 {plan.goal} 篇。<br />
                    {n >= plan.goal
                      ? '篇数已达标；继续把「待读」推进到「已整理」。'
                      : `若按 24 周推进，每周需新增入库约 ${Math.max(1, Math.ceil((plan.goal - n) / 24))} 篇。`}
                  </p>
                  <div className="mini accent" style={{ width: '100%', height: 7, margin: '16px 0 8px' }}>
                    <i style={{ width: `${Math.min(100, Math.round((n / plan.goal) * 100))}%` }} />
                  </div>
                  <div className="row">
                    <span className="meta">入库 {n}</span>
                    <div className="spacer" />
                    <span className="meta">目标 {plan.goal}</span>
                  </div>
                </>
              ) : (
                <>
                  <p className="h3" style={{ fontSize: 15, marginBottom: 6 }}>还没有设定阅读目标</p>
                  <p className="meta" style={{ lineHeight: 1.7 }}>
                    当前已入库 {n} 篇。设定目标后，这里会显示达成进度与每周建议新增量。
                  </p>
                  {/* 只读分享：目标是分享者的事，匿名访客无处置 —— 不给这个入口 */}
                  {!readonly && (
                    <Link className="btn btn-secondary btn-sm" to={link('/plans')} style={{ marginTop: 12 }}>去设置</Link>
                  )}
                </>
              )}
            </div>
          </section>
        </div>
      </div>
    </>
  )
}

/* ── 阅读节奏（按 status_at 分桶）──────────────────────────────────── */

interface Bucket { l: string; v: number; full: string }
interface Pace {
  kind: 'day' | 'week'
  dense: boolean
  bars: Bucket[]
  total: number
  prev: number
  label: string
  unitName: string
  coverDays: number
}

function buildPace(stamped: Paper[], range: Range): Pace {
  const buckets: Array<{ key: string; l: string; v: number; full: string }> = []
  const byKey = new Map<string, number>()

  const nearest = (iso: string) => {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return null
    return d
  }
  for (const p of stamped) {
    const d = nearest(p.status_at as string)
    if (!d) continue
    const key = range === 'w12' ? dayKey(startOfWeek(d)) : dayKey(d)
    byKey.set(key, (byKey.get(key) || 0) + 1)
  }

  const today = new Date()
  const WEEK = ['一', '二', '三', '四', '五', '六', '日']
  if (range === 'w12') {
    for (let i = 11; i >= 0; i--) {
      const d = startOfWeek(today)
      d.setDate(d.getDate() - i * 7)
      const key = dayKey(d)
      buckets.push({
        key,
        l: i === 0 ? '本周' : `${d.getMonth() + 1}/${d.getDate()}`,
        v: byKey.get(key) || 0,
        full: `${fmtDate(d.toISOString())} 起的一周`,
      })
    }
  } else {
    const days = range === 'd7' ? 7 : 30
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today.getFullYear(), today.getMonth(), today.getDate())
      d.setDate(d.getDate() - i)
      const key = dayKey(d)
      buckets.push({
        key,
        l: range === 'd7' ? `周${WEEK[(d.getDay() + 6) % 7]}` : (i === 0 ? '今天' : `${d.getMonth() + 1}/${d.getDate()}`),
        v: byKey.get(key) || 0,
        full: fmtDate(d.toISOString()),
      })
    }
  }

  // 环比：紧邻的上一个**等长**窗口（不是"自然上周"，否则月初永远在跟上一整月比）
  const coverDays = range === 'd7' ? 7 : range === 'd30' ? 30 : 84
  const now = Date.now()
  const span = coverDays * 86_400_000
  const prev = stamped.filter((p) => {
    const t = new Date(p.status_at as string).getTime()
    return !Number.isNaN(t) && t >= now - 2 * span && t < now - span
  }).length

  const total = buckets.reduce((a, b) => a + b.v, 0)

  return {
    kind: range === 'w12' ? 'week' : 'day',
    dense: range === 'd30',
    bars: buckets.map(({ l, v, full }) => ({ l, v, full })),
    total,
    prev,
    label: range === 'd7' ? '近 7 天' : range === 'd30' ? '近 30 天' : '近 12 周',
    unitName: range === 'w12' ? '周' : '天',
    coverDays,
  }
}

function PaceFig({ pace }: { pace: Pace }) {
  const vals = pace.bars.map((b) => b.v)
  const maxV = Math.max(...vals, 1)
  const peakIdx = vals.indexOf(maxV)
  const avg = pace.bars.length ? pace.total / pace.bars.length : 0
  const delta = pace.prev ? Math.round(((pace.total - pace.prev) / pace.prev) * 100) : 0
  const peakLbl = pace.kind === 'week'
    ? `第 ${peakIdx + 1} 周`
    : pace.dense ? `第 ${peakIdx + 1} 天` : `周${pace.bars[peakIdx]?.l ?? '—'}`

  const stat = (k: string, v: string, d: string, cls = '') => (
    <div className="pace-stat">
      <div className="ps-k">{k}</div>
      <div className={`ps-v${cls}`}>{v}</div>
      <div className="ps-d">{d}</div>
    </div>
  )

  return (
    <>
      <div className={`pace-fig${pace.dense ? ' is-dense' : ''}`}>
        {pace.bars.map((b, i) => (
          <div className={`bar${b.v === maxV && maxV > 0 ? ' is-peak' : ''}`} key={i}
               title={`${b.full}：${b.v} 篇`}>
            {!pace.dense && <span className="num" style={{ fontSize: 11, color: 'var(--muted)' }}>{b.v || ''}</span>}
            {/* 0 的柱子给 3px 基线而不是百分比：用百分比时"没有数据"会画成一根粗短棍，
                看着像数据（原型就是这个观感）。 */}
            <div className="fill" style={{ height: b.v === 0 ? 3 : `${Math.max(6, (b.v / maxV) * 100)}%` }} />
            <span className={`bl${b.l ? '' : ' is-off'}`}>{b.l || '·'}</span>
          </div>
        ))}
      </div>
      <div className="pace-stats">
        {stat('合计', `${pace.total} 篇`, `${pace.label}共完成`)}
        {stat(pace.kind === 'week' ? '周均' : '日均', avg.toFixed(1), `${pace.label}合计 ${pace.total} 篇`)}
        {stat('环比', `${delta > 0 ? '+' : ''}${delta}%`, `上一周期 ${pace.prev} 篇`, delta > 0 ? ' is-up' : delta < 0 ? ' is-down' : '')}
        {stat('峰值', `${maxV} 篇`, peakLbl)}
      </div>
      <p className="meta" style={{ marginTop: 10 }}>
        按「被标记为已读/已整理」的时间统计（当前计划内 {pace.total} 次）。
        在阅读器里滚动只累计进度，不改状态 —— 所以这里的低值不代表没在读。
      </p>
    </>
  )
}

/* ── 需要关注 ──────────────────────────────────────────────────────── */

function buildAttention(papers: Paper[]) {
  const out: Array<{ Icon?: (p: { className?: string }) => React.ReactElement; tone: '' | 'warn' | 'danger'; title: string; sub: string; to?: string }> = []

  const failed = papers.filter((p) => p.conv_state === 'failed')
  if (failed.length) {
    out.push({
      Icon: IconAlert, tone: 'danger',
      title: `转换失败 ${failed.length} 篇：${failed[0].title || `文献 #${failed[0].id}`}`,
      sub: (failed[0].conv_error || '未记录失败原因').slice(0, 120),
      to: '/library',
    })
  }

  const stalled = papers.filter((p) => {
    if (p.conv_state !== 'done' || p.status !== 'unread') return false
    const d = p.status_at ? daysSince(p.status_at) : daysSince(p.created_at)
    return d !== null && d >= 14
  })
  if (stalled.length) {
    out.push({
      Icon: IconClock, tone: 'warn',
      title: `${stalled.length} 篇已生成中文版但 14 天未开始阅读`,
      sub: stalled.slice(0, 3).map((p) => p.title || `#${p.id}`).join('、'),
      to: '/library',
    })
  }

  const stuck = papers.filter((p) => p.status === 'reading' && p.progress > 0 && p.progress < 100
    && (daysSince(p.status_at || p.created_at) ?? 0) >= 21)
  if (stuck.length) {
    out.push({
      Icon: IconFile, tone: 'warn',
      title: `${stuck.length} 篇「在读」已停摆 21 天以上`,
      sub: `最近更新：${relTime(stuck[0].status_at || stuck[0].created_at)} · ${stuck[0].title || `文献 #${stuck[0].id}`}`,
      to: '/board',
    })
  }

  return out
}
