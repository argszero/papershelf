/** 阅读计划 —— 原型的 `renderPlans`：计划卡片网格 + 新建/重命名 + 分享 + 删除。
 *
 * 页面同时也承接三个来自外壳的意图（顶栏的「导入文献 / 分享」、切换器的「新建」）：
 *   `/plans?import=<planId>` · `/plans?share=<planId>` · `/plans?new=1`
 * 用 URL 而不是全局状态，是为了让"点了导入结果没打开"这类问题可复现、可回退。
 */

import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '../api'
import { PlanCard } from '../components'
import { usePlan } from '../planContext'
import { useLink, useReadonly } from '../shareContext'
import { NoPlan, TopBar } from '../shell'
import { IconPlus } from '../icons'
import { CreateShareModal } from './ShareModal'
import type { GlossaryEntry, Plan } from '../types'

export function PlansPage() {
  const { plan, plans, papers, reload, switchPlan } = usePlan()
  const [params, setParams] = useSearchParams()
  const nav = useNavigate()
  const link = useLink()
  const readonly = useReadonly()
  const [editing, setEditing] = useState<Plan | null>(null)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')

  const drawer = params.get('import') ? 'import'
    : params.get('share') ? 'share'
      : params.get('glossary') ? 'glossary' : ''

  // 切换器/顶栏点「新建」→ 直接开弹窗。`?new=1` 就是弹窗的开关：直接由 URL 推导，
  // 不用 effect 再抄一份 state（那样会有"URL 已变、弹窗还没开"的一帧）。
  const modalOpen = creating || Boolean(params.get('new'))
  const closeModal = useCallback(() => {
    setCreating(false)
    if (params.get('new')) {
      const next = new URLSearchParams(params)
      next.delete('new')
      setParams(next, { replace: true })
    }
  }, [params, setParams])

  const close = useCallback(() => {
    const next = new URLSearchParams(params)
    next.delete('import'); next.delete('share'); next.delete('new'); next.delete('glossary')
    setParams(next, { replace: true })
  }, [params, setParams])

  async function remove(p: Plan) {
    if (readonly) return
    if (!confirm(`删除「${p.name}」及其文献、译文、笔记与分享链接？此操作不可撤销。`)) return
    try {
      await api.deletePlan(p.id)
      await reload()
    } catch (e) { setError(e instanceof Error ? e.message : '删除失败') }
  }

  // ⑩ 只读分享：计划页退化为"这一个计划长什么样"，没有任何操作
  if (readonly) {
    return (
      <>
        <TopBar title="阅读计划" sub={`共 ${plans.length} 个计划 · 各自独立记录文献与进度`} />
        <div className="view-scroll">
          <div className="view">
            <div className="sec-head">
              <div>
                <span className="eyebrow">阅读计划 · 只读分享</span>
                <h2 className="h2" style={{ marginTop: 6, fontSize: 22 }}>这个计划由分享链接提供</h2>
              </div>
              <span className="pill st-reading">只读</span>
            </div>
            <div className="plan-grid">
              {plans.map((p) => (
                <PlanCard key={p.id} plan={p} active readonly
                          papers={p.id === plan?.id ? papers : undefined}
                          onOpen={() => nav(link('/'))} />
              ))}
            </div>
          </div>
        </div>
      </>
    )
  }

  if (plans.length === 0) {
    return (
      <>
        <TopBar title="阅读计划" sub="还没有计划" />
        <NoPlan />
        {modalOpen && (
          <PlanModal onClose={closeModal}
                     onSaved={() => { void reload(); closeModal() }} />
        )}
        <button className="btn btn-primary" style={{ display: 'none' }}
                onClick={() => setCreating(true)} />
      </>
    )
  }

  return (
    <>
      <TopBar title="阅读计划"
              sub={`共 ${plans.length} 个计划 · 各自独立记录文献与进度`} />
      <div className="view-scroll">
        <div className="view">
          <div className="sec-head">
            <div>
              <span className="eyebrow">阅读计划</span>
              <h2 className="h2" style={{ marginTop: 6, fontSize: 22 }}>一个计划，一套文献与进度</h2>
            </div>
            <button className="btn btn-primary" onClick={() => setCreating(true)}>
              <IconPlus /> 新建计划
            </button>
          </div>

          {error && <div className="banner error">{error}</div>}

          <div className="plan-grid">
            {plans.map((p) => (
              <PlanCard key={p.id} plan={p} active={p.id === plan?.id}
                        papers={p.id === plan?.id ? papers : undefined}
                        onOpen={() => { switchPlan(p.id); nav(link('/')) }}
                        onShare={() => setParams({ share: String(p.id) })}
                        onGlossary={() => setParams({ glossary: String(p.id) })}
                        onImport={() => setParams({ import: String(p.id) })}
                        onEdit={() => setEditing(p)}
                        onDelete={() => void remove(p)} />
            ))}
            <button className="plan-new" onClick={() => setCreating(true)}>
              <IconPlus />
              <span>新建阅读计划</span>
              <span className="meta">为新的研究主题建立独立文献库</span>
            </button>
          </div>
        </div>
      </div>

      {(modalOpen || editing) && (
        <PlanModal plan={editing ?? undefined}
                   onClose={() => { setEditing(null); closeModal() }}
                   onSaved={() => { void reload(); setEditing(null); closeModal() }} />
      )}
      {drawer === 'share' && (
        <CreateShareModal planId={Number(params.get('share')) || plan?.id}
                          onClose={close}
                          onCreated={() => { /* 链接当场复制；列表在分享管理页 */ }} />
      )}
      {drawer === 'import' && (
        <ImportDrawer planId={Number(params.get('import')) || plan?.id || 0}
                      onClose={close} onDone={() => { void reload(); close() }} />
      )}
      {drawer === 'glossary' && (
        <GlossaryDrawer planId={Number(params.get('glossary')) || plan?.id || 0}
                        onClose={close} />
      )}
    </>
  )
}

/* ── 新建 / 编辑计划 ──────────────────────────────────────────────── */
function PlanModal({ plan, onClose, onSaved }: {
  plan?: Plan
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(plan?.name ?? '')
  const [goal, setGoal] = useState(plan?.goal ? String(plan.goal) : '')
  const [desc, setDesc] = useState(plan?.description ?? '')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim()) { setErr('请填写计划名称'); return }
    setBusy(true); setErr('')
    try {
      const body = { name: name.trim(), description: desc.trim(), goal: goal ? Number(goal) : null }
      if (plan) await api.updatePlan(plan.id, body)
      else await api.createPlan(body.name, body.description, body.goal)
      onSaved()
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : '保存失败')
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer" style={{ width: 'min(460px, 94vw)' }}>
        <header>
          <strong style={{ flex: 1 }}>{plan ? '编辑阅读计划' : '新建阅读计划'}</strong>
          <button className="ghost" onClick={onClose}>关闭</button>
        </header>
        <form className="body stack" onSubmit={submit}>
          {err && <div className="banner error">{err}</div>}
          <div className="field" style={{ maxWidth: '100%' }}>
            <label>计划名称</label>
            <input className="input" value={name} autoFocus
                   onChange={(e) => setName(e.target.value)} placeholder="如：安全控制精读" />
          </div>
          <div className="field" style={{ maxWidth: '100%' }}>
            <label>目标篇数（可选）</label>
            <input className="input" type="number" min={1} value={goal}
                   onChange={(e) => setGoal(e.target.value)} placeholder="如：120" />
          </div>
          <div className="field" style={{ maxWidth: '100%' }}>
            <label>计划说明（可选）</label>
            <textarea className="textarea" value={desc} rows={3}
                      onChange={(e) => setDesc(e.target.value)}
                      placeholder="这个方向要解决什么问题、为什么收这些文献" />
          </div>
          <button className="btn btn-primary" disabled={busy}>
            {busy ? '保存中…' : (plan ? '保存' : '创建')}
          </button>
        </form>
      </aside>
    </>
  )
}

/* ── 导入文献抽屉（⑱：PDF 上传 + arXiv 链接）─────────────────────── */
function ImportDrawer({ planId, onClose, onDone }: {
  planId: number; onClose: () => void; onDone: () => void
}) {
  const [tab, setTab] = useState<'pdf' | 'arxiv'>('pdf')
  const [files, setFiles] = useState<File[]>([])
  const [tags, setTags] = useState('')
  const [ref, setRef] = useState('')
  const [over, setOver] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function upload() {
    if (files.length === 0) return
    setBusy(true); setErr(''); setMsg('')
    try {
      const created = await api.uploadPapers(planId, files, tags)
      setMsg(`已加入 ${created.length} 篇，正在后台自动转换（可关闭本窗口）。`)
      setFiles([])
      window.setTimeout(onDone, 900)
    } catch (e) { setErr(e instanceof Error ? e.message : '上传失败') }
    finally { setBusy(false) }
  }

  async function arxiv() {
    if (!ref.trim()) return
    setBusy(true); setErr(''); setMsg('')
    try {
      await api.importArxiv(planId, ref.trim())
      setMsg('已加入，正在后台自动转换（优先抓 arXiv 官方 HTML）。')
      setRef('')
      window.setTimeout(onDone, 900)
    } catch (e) { setErr(e instanceof Error ? e.message : '导入失败') }
    finally { setBusy(false) }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer">
        <header>
          <strong style={{ flex: 1 }}>导入文献</strong>
          <button className="ghost" onClick={onClose}>关闭</button>
        </header>
        <div className="body">
          <div className="tabs">
            <button className={tab === 'pdf' ? 'active' : ''} onClick={() => setTab('pdf')}>上传 PDF</button>
            <button className={tab === 'arxiv' ? 'active' : ''} onClick={() => setTab('arxiv')}>arXiv 链接</button>
          </div>
          {err && <div className="banner error">{err}</div>}
          {msg && <div className="banner ok">{msg}</div>}

          {tab === 'pdf' ? (
            <div className="stack">
              <div className={`drop${over ? ' over' : ''}`}
                   onDragOver={(e) => { e.preventDefault(); setOver(true) }}
                   onDragLeave={() => setOver(false)}
                   onDrop={(e) => {
                     e.preventDefault(); setOver(false)
                     setFiles(Array.from(e.dataTransfer.files)
                       .filter((f) => f.name.toLowerCase().endsWith('.pdf')))
                   }}>
                <div>把 PDF 拖到这里，或</div>
                <label className="btn btn-secondary" style={{ marginTop: 8, display: 'inline-flex' }}>
                  选择文件
                  <input type="file" accept="application/pdf" multiple hidden
                         onChange={(e) => setFiles(Array.from(e.target.files || []))} />
                </label>
              </div>
              {files.length > 0 && (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {files.map((f) => (
                    <li key={f.name}>{f.name}
                      <span className="meta">（{Math.round(f.size / 1024)} KB）</span></li>
                  ))}
                </ul>
              )}
              <div className="field" style={{ maxWidth: '100%' }}>
                <label>标签（逗号分隔，可选）</label>
                <input className="input" value={tags} onChange={(e) => setTags(e.target.value)}
                       placeholder="CBF, 综述" />
              </div>
              <button className="btn btn-primary" disabled={busy || files.length === 0}
                      onClick={() => void upload()}>
                {busy ? '上传中…' : `上传并转换${files.length ? `（${files.length}）` : ''}`}
              </button>
              <p className="meta">
                转换在服务端后台进行：先解析 PDF，再把公式 LaTeX 化，然后逐块翻译。
                同一份 PDF 的解析结果会被缓存复用（重复上传不重复烧钱）。
              </p>
            </div>
          ) : (
            <div className="stack">
              <div className="field" style={{ maxWidth: '100%' }}>
                <label>arXiv 编号或链接</label>
                <input className="input" value={ref} onChange={(e) => setRef(e.target.value)}
                       placeholder="1706.03762 或 https://arxiv.org/abs/2405.04434v2" />
              </div>
              <button className="btn btn-primary" disabled={busy || !ref.trim()}
                      onClick={() => void arxiv()}>
                {busy ? '导入中…' : '导入并转换'}
              </button>
              <p className="meta">
                优先抓 arXiv 官方 HTML：公式来自页面自带的原生 LaTeX（无需模型转换，省钱且更保真）；
                没有 HTML 版时自动回落下载 PDF 走同一条管线。
              </p>
            </div>
          )}
        </div>
      </aside>
    </>
  )
}

/* ── 术语表抽屉（⑫ v1 只做术语表，计划级）────────────────────────── */
function GlossaryDrawer({ planId, onClose }: { planId: number; onClose: () => void }) {
  const [rows, setRows] = useState<GlossaryEntry[]>([])
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  useEffect(() => {
    if (!planId) return
    api.glossary(planId).then((r) => setRows(r.glossary)).catch(() => {})
  }, [planId])

  async function save() {
    setErr(''); setMsg('')
    try {
      const clean = rows.filter((r) => r.en.trim() && r.zh.trim())
      await api.setGlossary(planId, clean)
      setRows(clean)
      setMsg('已保存。下次转换（或对某块「重译」）时生效。')
    } catch (e) { setErr(e instanceof Error ? e.message : '保存失败') }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer">
        <header>
          <strong style={{ flex: 1 }}>术语表（本计划内生效）</strong>
          <button className="ghost" onClick={onClose}>关闭</button>
        </header>
        <div className="body stack">
          {err && <div className="banner error">{err}</div>}
          {msg && <div className="banner ok">{msg}</div>}
          <p className="meta" style={{ lineHeight: 1.7 }}>
            术语表在本计划内生效，翻译时注入提示词。同一概念在不同计划里需要各配一次 ——
            这是 v1 的边界（术语表归属推到 v2）。
          </p>
          {rows.map((r, i) => (
            <div className="row" key={i} style={{ gap: 6 }}>
              <input className="input" value={r.en} placeholder="英文"
                     onChange={(e) => setRows((rs) => rs.map((x, j) => (j === i ? { ...x, en: e.target.value } : x)))} />
              <input className="input" value={r.zh} placeholder="中文"
                     onChange={(e) => setRows((rs) => rs.map((x, j) => (j === i ? { ...x, zh: e.target.value } : x)))} />
              <button className="btn btn-ghost btn-sm"
                      onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}>删</button>
            </div>
          ))}
          <div className="row">
            <button className="btn btn-secondary btn-sm"
                    onClick={() => setRows((rs) => [...rs, { en: '', zh: '' }])}>添加一行</button>
            <div className="spacer" />
            <button className="btn btn-primary btn-sm" onClick={() => void save()}>保存</button>
          </div>
        </div>
      </aside>
    </>
  )
}

/* ── 分享（⑩⑪ + 2026-09-12 分享管理）─────────────────────────────────
 * 计划卡上的「分享」与顶栏的「分享」都开**新建分享**抽屉（原型 `data-share-card`
 * → `openShareModal(planId)` 同款）。**已建链接的列表与续期/取消**统一在侧栏
 * 「分享管理」页 —— 一处管"要分享"，一处管"分享过什么"，不重复两份列表。
 */
