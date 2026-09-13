/** 新建分享（原型 `shareModal` 的移植）—— 用**抽屉**而不是模态。
 *
 * 为什么不用原型的模态：本仓库至今所有"填几个字段然后提交"的交互都走 `.drawer`
 * （阅读计划的编辑、导入、术语表…），modal 一套样式并不存在。为这一处新造一套
 * 模态样式，会立刻出现两种并存的浮层形态。原型是单文件、只有一个浮层，所以它
 * 用它自己的模态；这里跟随本仓库既有形态。
 *
 * 2026-09-12 起，**顶栏「分享计划」与计划卡「分享」都开这个抽屉**（对齐原型
 * `data-share-card` → `openShareModal(planId)`）；计划可预选、有备注、建完显示
 * 倒计时与「再创建一个」。旧 `SharePanel`（只列链接、只认三态）已退役删除。
 */

import { useEffect, useMemo, useState } from 'react'

import { api } from '../api'
import { usePlan } from '../planContext'
import { fmtCountdown, isExpiring, useCountdown } from '../shareCountdown'
import type { Share } from '../types'

const HOURS = ['1', '6', '12', '24']

export function CreateShareModal({ planId, onClose, onCreated }: {
  /** 预选计划；分享管理页进来时通常是"当前计划" */
  planId?: number
  onClose: () => void
  onCreated: () => void
}) {
  const { plans } = usePlan()
  const [pid, setPid] = useState<string>(String(planId ?? plans[0]?.id ?? ''))
  const [hours, setHours] = useState('24')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [done, setDone] = useState<Share | null>(null)

  const plan = useMemo(() => plans.find((p) => p.id === Number(pid)) ?? null, [plans, pid])

  // 建完之后抽屉停在"结果"态：显示链接与倒计时。原型也是这个流程
  // （`showShareResult`），而不是建完就关 —— 链接要当场复制走。
  const left = useCountdown(done?.seconds_left ?? 0, Boolean(done))

  useEffect(() => {
    if (done) onCreated()
    // 建完即刷新列表；`done` 只在这里变化一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [done])

  async function submit() {
    if (!plan) { setErr('请选择要分享的计划'); return }
    setBusy(true); setErr('')
    try {
      setDone(await api.createShare(plan.id, Number(hours) || 24, label.trim()))
    } catch (e) {
      setErr(e instanceof Error ? e.message : '创建失败')
    } finally { setBusy(false) }
  }

  async function copy() {
    if (!done) return
    try { await navigator.clipboard.writeText(done.url) } catch { /* 下面那行可手动复制 */ }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer" style={{ width: 'min(480px, 94vw)' }}>
        <header>
          <strong style={{ flex: 1 }}>{done ? '分享链接已创建' : '新建分享链接'}</strong>
          <button className="ghost" onClick={onClose}>关闭</button>
        </header>
        <div className="body stack">
          {err && <div className="banner error">{err}</div>}

          {!done && (
            <>
              <p className="meta" style={{ lineHeight: 1.65, margin: 0 }}>
                任何拿到链接的人都能以只读方式查看该计划的全部文献、进度与笔记，
                <b style={{ color: 'var(--fg)' }}>无需登录</b>，也无法修改内容。
              </p>

              <div className="share-plan">
                <span className="pc-mark">{(plan?.name || '计').slice(0, 1)}</span>
                <div style={{ minWidth: 0 }}>
                  <div className="sp-n">{plan?.name || '未选择计划'}</div>
                  <div className="meta">
                    {plan ? `${plan.paper_count ?? 0} 篇 · 已精读 ${plan.done_count ?? 0} · 目标 ${plan.goal ?? '—'} 篇` : ''}
                  </div>
                </div>
              </div>

              <div className="field">
                <label>分享哪个计划</label>
                <select className="input" value={pid} onChange={(e) => setPid(e.target.value)}>
                  {plans.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>

              <div className="field">
                <label>有效期 <span className="meta">（最长 24 小时）</span></label>
                <div className="seg" role="group" aria-label="选择分享有效期">
                  {HOURS.map((h) => (
                    <button key={h} type="button" className={hours === h ? 'is-on' : ''}
                            aria-pressed={hours === h} onClick={() => setHours(h)}>
                      {h} 小时
                    </button>
                  ))}
                </div>
              </div>

              <div className="field">
                <label>备注 <span className="meta">（可选）</span></label>
                <input className="input" value={label} maxLength={30} autoFocus
                       placeholder="例如：给导师看 / 组会前"
                       onChange={(e) => setLabel(e.target.value)} />
              </div>

              <div className="row">
                <button className="btn btn-primary" disabled={busy || !plan}
                        onClick={() => void submit()}>
                  {busy ? '创建中…' : '创建分享链接'}
                </button>
              </div>
            </>
          )}

          {done && (
            <>
              <div className="field" style={{ maxWidth: '100%' }}>
                <label className="flabel">分享链接</label>
                <div className="share-link-row">
                  <input className="input" readOnly value={done.url}
                         onFocus={(e) => e.currentTarget.select()} />
                  <button className="btn btn-primary" onClick={() => void copy()}>复制链接</button>
                </div>
              </div>
              <div className={`share-cd${isExpiring(left) ? ' is-warn' : ''}`}>
                <span className="meta">有效期剩余</span>
                <b className={`cd${isExpiring(left) ? ' is-warn' : ''}`}>{fmtCountdown(left)}</b>
              </div>
              <p className="meta" style={{ lineHeight: 1.6, margin: 0 }}>
                链接只包含对该计划的只读访问权，不含邮箱、密码等账号信息。
                可在「分享管理」里随时取消或重置有效期，对访客立即生效。
              </p>
              <div className="row wrap" style={{ gap: 8 }}>
                <a className="btn btn-secondary" href={done.url} target="_blank" rel="noreferrer">
                  在新标签页预览
                </a>
                <button className="btn btn-ghost"
                        onClick={() => { setDone(null); setLabel('') }}>再创建一个</button>
              </div>
            </>
          )}
        </div>
      </aside>
    </>
  )
}
