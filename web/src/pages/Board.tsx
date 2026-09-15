/** 进度看板 —— 原型的 `renderBoard`：四列拖拽，拖到哪列就把状态改成那一列。
 *
 * 数据侧**不需要新端点**：四列就是 ⑰ 的四个状态，拖拽走已有的
 * `PATCH /api/papers/{id}`；后端在该端点里顺手处理了"标已读 → 进度锁 100%"，
 * 所以这里只发状态，不自己算进度（避免两套规则打架）。
 *
 * 拖拽用原生 HTML5 DnD（原型同款）：`draggable` + `dragover/drop`。
 * 不引拖拽库 —— 只有"一整张卡换列"这一种手势，引库的收益为负。
 */

import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../api'
import { usePlan } from '../planContext'
import { useLink, useReadonly } from '../shareContext'
import { NoPlan, TopBar } from '../shell'
import { CONV, STATUS, STATUS_ORDER } from '../vocab'
import type { Paper, PaperStatus } from '../types'

export function BoardPage() {
  const { plan, papers, loading, reload } = usePlan()
  const nav = useNavigate()
  const link = useLink()
  const readonly = useReadonly()
  const [over, setOver] = useState<PaperStatus | null>(null)
  const [dragging, setDragging] = useState<number | null>(null)
  const [toast, setToast] = useState('')

  const columns = useMemo(
    () => STATUS_ORDER.map((k) => ({
      k,
      items: papers
        .filter((p) => p.status === k)
        .sort((a, b) => (b.status_at || b.created_at).localeCompare(a.status_at || a.created_at)),
    })),
    [papers],
  )

  function flash(msg: string) {
    setToast(msg)
    window.setTimeout(() => setToast(''), 2200)
  }

  async function drop(target: PaperStatus) {
    setOver(null)
    const id = dragging
    setDragging(null)
    if (!id) return
    if (readonly) return          // ⑩ 只读：拖拽不落库（原型靠 `body.readonly` 隐藏，这里双保险）
    const paper = papers.find((p) => p.id === id)
    if (!paper || paper.status === target) return
    if (paper.conv_state !== 'done' && (target === 'read' || target === 'reviewed')) {
      flash('这篇还没有中文版，先转换完再标记已读')
      return
    }
    // 乐观更新：拖拽要"手感即时"，失败再拉回真实状态
    try {
      await api.updatePaper(id, { status_: target })
      flash(`已移至「${STATUS[target].label}」`)
    } catch (e) {
      flash(e instanceof Error ? e.message : '更新失败')
    }
    await reload()
  }

  if (!plan) return <><TopBar title="进度看板" sub="还没有阅读计划" /><NoPlan /></>

  return (
    <>
      <TopBar title="进度看板" sub={`拖动卡片推进阅读进度 · ${papers.length} 篇`} />
      <div className="view-scroll">
        <div className="view">
          <div className="sec-head">
            <div>
              <span className="eyebrow">阅读阶段</span>
              <h2 className="h2" style={{ marginTop: 6, fontSize: 22 }}>拖动卡片推进阅读进度</h2>
            </div>
            <span className="meta">共 {papers.length} 篇</span>
          </div>

          {loading && papers.length === 0 ? <p className="muted">加载中…</p> : (
            <div className="board">
              {columns.map((col) => (
                <div key={col.k} className={`col${over === col.k ? ' is-over' : ''}`}
                     onDragOver={(e) => { e.preventDefault(); setOver(col.k) }}
                     onDragLeave={() => setOver((o) => (o === col.k ? null : o))}
                     onDrop={(e) => { e.preventDefault(); void drop(col.k) }}>
                  <div className="col-head">
                    <span className="ch-dot" style={{ background: STATUS[col.k].color }} />
                    <span className="ch-n">{STATUS[col.k].label}</span>
                    <span className="ch-c">{col.items.length}</span>
                  </div>
                  {col.items.map((p) => (
                    <Card key={p.id} paper={p} dragging={dragging === p.id} readonly={readonly}
                          onDragStart={() => setDragging(p.id)}
                          onDragEnd={() => setDragging(null)}
                          onOpen={() => nav(link(`/reader/${p.id}`))} />
                  ))}
                  {col.items.length === 0 && (
                    <p className="meta" style={{ padding: '14px 4px', textAlign: 'center' }}>
                      拖一张卡片到这里
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}

          <p className="meta" style={{ marginTop: 16 }}>
            在阅读器里滚动会<b>自动</b>把「待读」推进到「在读」（「读懂没有」仍然只能手动标）；
            拖到「已读」或「已整理」会把进度锁到 100%（⑰ 手动优先）；拖回「待读」则清零重来。
          </p>
        </div>
      </div>
      {toast && <div className="toast">{toast}</div>}
    </>
  )
}

function Card({ paper, dragging, readonly, onDragStart, onDragEnd, onOpen }: {
  paper: Paper
  dragging: boolean
  readonly: boolean
  onDragStart: () => void
  onDragEnd: () => void
  onOpen: () => void
}) {
  const conv = CONV[paper.conv_state]
  return (
    <div className={`kcard${dragging ? ' is-drag' : ''}`} draggable={!readonly}
         onDragStart={(e) => {
           if (readonly) { e.preventDefault(); return }
           onDragStart(); e.dataTransfer.effectAllowed = 'move'
           e.dataTransfer.setData('text/plain', String(paper.id))
         }}
         onDragEnd={onDragEnd}
         onClick={onOpen} role="button" tabIndex={0}
         onKeyDown={(e) => { if (e.key === 'Enter') onOpen() }}>
      <div className="kc-t">{paper.title || `文献 #${paper.id}`}</div>
      <div className="kc-m">
        {[paper.venue, paper.year].filter(Boolean).join(' ') || '未提取元数据'} · {paper.progress}%
      </div>
      <div className="kc-foot">
        {paper.tags?.[0] && <span className="tag">{paper.tags[0]}</span>}
        <span className={`pill ${conv.cls}`} style={{ marginLeft: 'auto' }}>{conv.label}</span>
      </div>
    </div>
  )
}
