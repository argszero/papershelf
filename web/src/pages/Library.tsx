/** 文献库 —— 原型的 `renderLibrary`：筛选 chips（带计数）+ 排序 + 文献表。
 *
 * 只呈现**当前计划**的文献（原型 `papers()` 的语义）。
 * 顶栏搜索把关键词写进 `?q=`，本页从 URL 读 —— 这样"搜到一半刷新"不会丢条件。
 *
 * 表格列刻意与原型一致：标题/作者 · 发表 · 标签 · 中文版 · 状态 · 进度。
 * 「中文版」列是可点的**动作**（待转换 → 生成；失败 → 重试），不是只读徽标：
 * 转换本来就是这台工作台最容易卡住的一环，让它一步可达。
 */

import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '../api'
import { CONV, clip, fmtDate, isDone, relTime, STATUS, STATUS_ORDER } from '../vocab'
import { Pill } from '../components'
import { PaperMetaDrawer } from './PaperMetaDrawer'
import { useLink, useReadonly } from '../shareContext'
import { NoPlan, TopBar } from '../shell'
import { usePlan } from '../planContext'
import type { Paper, PaperStatus } from '../types'

/** 只到「月-日」：文献库单元格很窄，年份在这个场景没有信息量（都是今年入库的）。 */
const shortDay = (iso: string | null | undefined): string => {
  const d = fmtDate(iso)
  return d === '—' ? '—' : d.slice(5)
}

type Sort = 'recent' | 'year' | 'progress' | 'title'

const SORTS: Array<{ k: Sort; label: string }> = [
  { k: 'recent', label: '最近更新' },
  { k: 'year', label: '发表年份' },
  { k: 'progress', label: '阅读进度' },
  { k: 'title', label: '标题' },
]

/** 确认框里指代这篇文献的方式。`original` 是上传时的文件名占位（⑲），念出来毫无信息。 */
function paperLabel(p: Paper): string {
  return p.title && p.title !== 'original' ? `《${p.title}》` : `文献 #${p.id}`
}

export function LibraryPage() {
  const { plan, papers, loading, reload } = usePlan()
  const [params, setParams] = useSearchParams()
  const nav = useNavigate()
  const link = useLink()
  const readonly = useReadonly()

  const [sort, setSort] = useState<Sort>('recent')
  const [filter, setFilter] = useState<'all' | PaperStatus>('all')
  const [busy, setBusy] = useState<number | null>(null)
  const [editing, setEditing] = useState<Paper | null>(null)

  const q = params.get('q') || ''

  const byStatus = useMemo(() => {
    const out: Record<string, number> = { all: papers.length }
    STATUS_ORDER.forEach((k) => { out[k] = papers.filter((p) => p.status === k).length })
    return out
  }, [papers])

  const shown = useMemo(() => {
    let list = [...papers]
    const needle = q.trim().toLowerCase()
    if (needle) {
      list = list.filter((p) =>
        [p.title, p.authors || '', p.venue || '', ...(p.tags || [])]
          .join(' ').toLowerCase().includes(needle))
    }
    if (filter !== 'all') list = list.filter((p) => p.status === filter)
    const cmp: Record<Sort, (a: Paper, b: Paper) => number> = {
      recent: (a, b) => b.created_at.localeCompare(a.created_at),
      year: (a, b) => (b.year || 0) - (a.year || 0),
      progress: (a, b) => b.progress - a.progress,
      title: (a, b) => (a.title || '').localeCompare(b.title || ''),
    }
    return list.sort(cmp[sort])
  }, [papers, q, filter, sort])

  const convert = useCallback(async (p: Paper) => {
    setBusy(p.id)
    try {
      await api.retryConvert(p.id)
      await reload()
    } catch { /* 失败原因会落在 conv_error 里，下次刷新可见 */ }
    finally { setBusy(null) }
  }, [reload])

  // 删除文献：与「删除计划」同一套约定（`window.confirm` 说清不可撤销）。
  // 文案必须点名**连带消失的东西** —— 译文、笔记、分享里看到的内容都靠这篇，
  // 只说"删除文献？"会让人以为只是从列表里去掉。
  const remove = useCallback(async (p: Paper) => {
    const name = paperLabel(p)
    if (!window.confirm(`删除${name}？其译文、笔记与该文献的图片都会一并删除，此操作不可撤销。`)) return
    setBusy(p.id)
    try {
      await api.deletePaper(p.id)
      await reload()
    } catch (e) {
      window.alert(e instanceof Error ? e.message : '删除失败')
    } finally { setBusy(null) }
  }, [reload])

  // 重新提取（宿主 2026-09-15）：清掉这篇的解析缓存，从头再跑一遍管线。
  // ⚠️ 与「重新转换」不是一回事：后者会命中解析缓存（指纹 = PDF hash + 解析版本号），
  //    等于什么都没重来。这里连笔记与划痕一起作废 —— 新解析的块 id 与文本都会变，
  //    旧批注留着只会挂到别的句子上。确认框必须点名**代价**（token + 不可撤销）。
  const reextract = useCallback(async (p: Paper) => {
    const name = paperLabel(p)
    if (!window.confirm(
      `重新提取${name}？\n\n` +
      '将清空该篇的解析缓存，从 PDF 重新解析并重跑整篇原文校对（有 token 成本）；\n' +
      '该篇的译文、笔记与划痕都会作废。此操作不可撤销。')) return
    setBusy(p.id)
    try {
      await api.reextractPaper(p.id)
      await reload()
    } catch (e) {
      window.alert(e instanceof Error ? e.message : '重新提取失败')
    } finally { setBusy(null) }
  }, [reload])

  if (!plan) return <><TopBar title="文献库" sub="还没有阅读计划" /><NoPlan /></>

  const chips: Array<{ k: 'all' | PaperStatus; label: string }> = [
    { k: 'all', label: '全部' },
    ...STATUS_ORDER.map((k) => ({ k, label: STATUS[k].label })),
  ]

  return (
    <>
      <TopBar title="文献库" sub={`共 ${papers.length} 篇 · ${plan.name}`} />
      <div className="view-scroll">
        <div className="view">
          <div className="lib-toolbar">
            {chips.map((c) => (
              <button key={c.k} className={`chip${filter === c.k ? ' is-on' : ''}`}
                      onClick={() => setFilter(c.k)}>
                {c.label} <span className="c">{byStatus[c.k] || 0}</span>
              </button>
            ))}
            {q && (
              <button className="chip is-on" onClick={() => setParams({})} title="清除搜索">
                搜索：{q} <span className="c">✕</span>
              </button>
            )}
            <span className="spacer" />
            <select className="input" value={sort} aria-label="排序方式"
                    onChange={(e) => setSort(e.target.value as Sort)}>
              {SORTS.map((s) => <option key={s.k} value={s.k}>{s.label}</option>)}
            </select>
          </div>

          {loading && papers.length === 0 && <p className="muted">加载中…</p>}

          <div className="card" style={{ padding: '6px 8px 2px' }}>
            <div className="table-scroll">
              <table className="ptable">
                <thead>
                  <tr>
                    <th style={{ width: '42%' }}>标题 / 作者</th>
                    <th className="col-hide">发表</th>
                    <th className="col-hide">标签</th>
                    <th>中文版</th>
                    <th>状态</th>
                    <th className="num-col">进度</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((p) => (
                    <tr key={p.id} onClick={() => nav(link(`/reader/${p.id}`))} tabIndex={0}
                        onKeyDown={(e) => { if (e.key === 'Enter') nav(link(`/reader/${p.id}`)) }}>
                      <td>
                        <div className="t-title">
                          {p.title || `文献 #${p.id}`}
                          {/* ⑲「可手动改」的入口。原先后端能改、前端无路可走（死列）。
                              只读分享态不显示（写权限在服务端，前端别给假入口）。 */}
                          {!readonly && (
                            <button className="t-edit" title="编辑文献信息"
                                    aria-label={`编辑《${p.title || p.id}》的信息`}
                                    onClick={(e) => { e.stopPropagation(); setEditing(p) }}>编辑</button>
                          )}
                          {/* 删除同样只给写权限态。确认框在 handler 里，
                              不在这里（`stopPropagation` 是防止点删除时顺带进阅读器）。 */}
                          {!readonly && (
                            <>
                              {/* 重新提取：解析缓存**没有失效机制**（指纹 = PDF hash + 解析版本号），
                                  单篇想"重来一遍"只有这条路。转换中禁用（后端也 409 兜底）。 */}
                              <button className="t-edit t-re" title="清空解析缓存，从 PDF 重新解析（译文、笔记与划痕会作废）"
                                      aria-label={`重新提取《${p.title || p.id}》`}
                                      disabled={busy === p.id || p.conv_state === 'doing' || p.conv_state === 'queued'}
                                      onClick={(e) => { e.stopPropagation(); void reextract(p) }}>重新提取</button>
                              <button className="t-edit t-del" title="删除这篇文献"
                                      aria-label={`删除《${p.title || p.id}》`}
                                      disabled={busy === p.id}
                                      onClick={(e) => { e.stopPropagation(); void remove(p) }}>删除</button>
                            </>
                          )}
                        </div>
                        <div className="t-sub">{p.authors || '未提取到作者'}</div>
                      </td>
                      <td className="col-hide">
                        <div className="t-year">{p.year || '—'}</div>
                        <div className="t-venue">{p.venue || '—'}</div>
                      </td>
                      <td className="col-hide">
                        <div className="t-tags">
                          {(p.tags || []).slice(0, 3).map((t) => <span className="tag" key={t}>{t}</span>)}
                        </div>
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {/* ⑩ 只读：转换入口整块隐藏（原型 `body.readonly [data-conv]`）。
                            已生成的文献照常显示状态 —— 那只是"读"。 */}
                        {readonly ? (
                          <span className={`pill ${CONV[p.conv_state].cls}`}>
                            {CONV[p.conv_state].label}
                          </span>
                        ) : p.conv_state === 'done' ? (
                          /* 「已生成」与原型一致（原型这格只有这两个字；`· N 天前` 是复刻时
                             自己加的，2026-09-15 按宿主意见去掉 —— 那读的是**阅读状态**时间戳，
                             拿去显示"中文版生成时间"是**语义错位**：用户读一次它就变成"今天"）。
                             宿主：「实际上创建时间和最近阅读时间才是需要的」——
                             一行放不下，走本表既有的一行两段式（标题/作者、年份/期刊同款）。 */
                          <div>
                            <div className="t-cv">已生成</div>
                            <div className="t-sub">
                              创建 {shortDay(p.created_at)} · 最近阅读 {p.last_read_at ? relTime(p.last_read_at) : '未读'}
                            </div>
                          </div>
                        ) : p.conv_state === 'failed' ? (
                          <button className="chip danger" disabled={busy === p.id}
                                  onClick={() => void convert(p)}>
                            {busy === p.id ? '重试中…' : '重新转换'}
                          </button>
                        ) : p.conv_state === 'none' ? (
                          <button className="chip" disabled={busy === p.id}
                                  onClick={() => void convert(p)}>
                            {busy === p.id ? '提交中…' : '生成中文版'}
                          </button>
                        ) : (
                          <span className={`pill ${CONV[p.conv_state].cls}`}>{CONV[p.conv_state].label}</span>
                        )}
                      </td>
                      <td><Pill status={p.status} /></td>
                      <td className="num-col">
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, justifyContent: 'flex-end' }}>
                          <span className="meta">{p.progress}%</span>
                          <span className={`mini${isDone(p.status) ? '' : ' accent'}`}>
                            <i style={{ width: `${p.progress}%` }} />
                          </span>
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {shown.length > 0 ? (
              <div className="table-foot">
                <span className="meta">显示 {shown.length} / {papers.length} 篇</span>
                <span className="meta">单击行进入阅读器 · {clip(plan.name, 20)}</span>
              </div>
            ) : papers.length === 0 ? (
              <div className="empty">
                <p style={{ margin: '0 0 6px' }}>该计划还没有文献</p>
                <span className="meta">导入 PDF 或 arXiv 链接，开始建立这个计划的文献库</span>
                {!readonly && (
                  <div className="row" style={{ justifyContent: 'center', marginTop: 16 }}>
                    <Link className="btn btn-primary btn-sm" to="/plans?import=1">导入文献</Link>
                  </div>
                )}
              </div>
            ) : (
              <div className="empty">
                <p style={{ margin: '0 0 6px' }}>没有匹配的文献</p>
                <span className="meta">尝试更换关键词或筛选条件</span>
              </div>
            )}
          </div>

          <p className="meta" style={{ marginTop: 12 }}>
            进度条在阅读器里滚动时自动累计（只增不减），并会自动把「待读」推进到「在读」；
            手动标记「已读」会锁到 100%。
          </p>
        </div>
      </div>

      {editing && (
        <PaperMetaDrawer paper={editing} onClose={() => setEditing(null)}
                         onSaved={() => void reload()} />
      )}
    </>
  )
}
