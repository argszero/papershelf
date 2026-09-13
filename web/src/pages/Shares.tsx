/** 分享管理（2026-09-12 宿主：「需要有分享管理，可以创建多个分享…可以手动取消分享
 *  或者重置现有分享的有效期」）—— 原型 `renderShares` 的移植。
 *
 * 与原型的一处**本质差异**：原型没有服务端，`shareReg` 只是一份 localStorage 记录，
 * 所以它的"取消/续期只在创建者本机生效"是设计缺陷而非特性。这里是真服务端：
 * 取消 = `DELETE /api/shares/{token}`，续期 = `POST .../renew`，**对访客立即生效**。
 *
 * 倒计时的剩余秒数由**服务端**给（`Share.seconds_left`），本页只做每秒递减 ——
 * 三处显示（本表格 / 新建结果框 / 访客页 banner）共用同一个起始值。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api'
import { usePlan } from '../planContext'
import { useReadonly } from '../shareContext'
import { NoPlan, TopBar } from '../shell'
import { Tile } from '../components'
import { IconPlus } from '../icons'
import { fmtDate } from '../vocab'
import { fmtCountdown, isExpiring, useCountdown } from '../shareCountdown'
import { CreateShareModal } from './ShareModal'
import { STATE_LABEL, STATE_PILL, isLive } from './shareState'
import type { Share } from '../types'

type Filter = 'all' | 'active' | 'expired' | 'revoked'

export function SharesPage() {
  const { plan } = usePlan()
  const readonly = useReadonly()
  const [shares, setShares] = useState<Share[]>([])
  const [filter, setFilter] = useState<Filter>('all')
  const [creating, setCreating] = useState(false)
  const [err, setErr] = useState('')
  const [toast, setToast] = useState('')

  const load = useCallback(async () => {
    try { setShares(await api.allShares()); setErr('') }
    catch (e) { setErr(e instanceof Error ? e.message : '加载失败') }
  }, [])
  useEffect(() => { void load() }, [load])

  const counts = useMemo(() => {
    const c = { active: 0, expiring: 0, expired: 0, revoked: 0 }
    for (const s of shares) c[s.state] += 1
    return c
  }, [shares])

  const list = useMemo(() => shares.filter((s) => {
    if (filter === 'all') return true
    // 「有效」把两态算一起（`expiring` 仍然可访问，只是快到期了）—— 与原型同款
    if (filter === 'active') return s.state === 'active' || s.state === 'expiring'
    return s.state === filter
  }), [shares, filter])

  // 有链接在倒计时 → 每 30 秒对一次服务端的剩余秒数。
  // 为什么还要重取：倒计时走完那一刻状态要翻成「已过期」，只靠本地递减
  // 会让表格永远停在 00:00:00 而状态还写着「有效」。
  const hasLive = shares.some((s) => isLive(s.state))
  useEffect(() => {
    if (!hasLive) return
    const t = setInterval(() => { void load() }, 30_000)
    return () => clearInterval(t)
  }, [hasLive, load])

  if (readonly) return null
  if (!plan && shares.length === 0 && !err) {
    return <><TopBar title="分享管理" sub="只读分享" /><NoPlan /></>
  }

  const activeN = counts.active + counts.expiring
  const chips: Array<[Filter, string, number]> = [
    ['all', '全部', shares.length],
    ['active', '有效', activeN],
    ['expired', '已过期', counts.expired],
    ['revoked', '已取消', counts.revoked],
  ]

  async function revoke(s: Share) {
    if (!window.confirm('取消这条分享？通过该链接的只读访问将立即失效。')) return
    try {
      await api.revokeShare(s.token)
      await load()
      setToast('分享已取消')
    } catch (e) { setErr(e instanceof Error ? e.message : '取消失败') }
  }

  return (
    <>
      <TopBar title="分享管理" sub={`共 ${shares.length} 条分享链接 · 最长有效期 24 小时`} />
      <div className="view-scroll">
        <div className="view">
          {err && <div className="banner error">{err}</div>}

          <section className="grid g-4" style={{ marginBottom: 16 }}>
            <Tile k="有效分享" v={activeN} unit="条" d="当前可访问的只读链接" />
            <Tile k="即将到期" v={counts.expiring} unit="条" d="剩余不足 1 小时" />
            <Tile k="已过期" v={counts.expired} unit="条" d="超过最长 24 小时有效期" />
            <Tile k="已取消" v={counts.revoked} unit="条" d="手动取消的分享" />
          </section>

          <div className="sec-head">
            <div>
              <span className="eyebrow">只读分享</span>
              <h2 className="h2" style={{ marginTop: 6, fontSize: 22 }}>分享链接管理</h2>
            </div>
            <button className="btn btn-primary" onClick={() => setCreating(true)}>
              <IconPlus />新建分享
            </button>
          </div>

          <div className="lib-toolbar">
            {chips.map(([k, label, n]) => (
              <button key={k} className={`chip${filter === k ? ' is-on' : ''}`}
                      onClick={() => setFilter(k)}>
                {label} <span className="c">{n}</span>
              </button>
            ))}
            <span className="spacer" />
            <span className="meta">链接有效期最长 24 小时，到期后自动失效</span>
          </div>

          <div className="card" style={{ padding: '6px 8px 2px' }}>
            <div className="utable-wrap">
              <table className="ptable">
                <thead>
                  <tr>
                    <th style={{ width: '30%' }}>分享</th>
                    <th>有效期</th>
                    <th>状态</th>
                    <th className="col-hide">创建</th>
                    <th className="act">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {list.length === 0 ? (
                    <tr><td colSpan={5}>
                      <div className="empty">
                        <p style={{ margin: '0 0 6px' }}>
                          {shares.length ? '没有匹配的分享' : '还没有分享链接'}
                        </p>
                        <span className="meta">
                          {shares.length ? '尝试更换筛选条件'
                            : '把某个阅读计划以只读链接分享给同学或导师'}
                        </span>
                      </div>
                    </td></tr>
                  ) : list.map((s) => (
                    <ShareRow key={s.token} share={s}
                              onRevoke={() => void revoke(s)}
                              onRenewed={load} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="table-foot">
              <span className="meta">显示 {list.length} / {shares.length} 条分享</span>
              <span className="meta">取消与续期对访客立即生效</span>
            </div>
          </div>
        </div>
      </div>

      {creating && (
        <CreateShareModal planId={plan?.id} onClose={() => setCreating(false)}
                          onCreated={() => { void load(); setToast('分享链接已创建') }} />
      )}
      {toast && <div className="toast" onAnimationEnd={() => setToast('')}>{toast}</div>}
    </>
  )
}

/** 一行分享。倒计时按秒走，`expiring`（剩余 < 1 小时）整行转告警色。 */
function ShareRow({ share, onRevoke, onRenewed }: {
  share: Share
  onRevoke: () => void
  onRenewed: () => Promise<void> | void
}) {
  const live = isLive(share.state)
  const left = useCountdown(share.seconds_left, live)
  const [renewing, setRenewing] = useState(false)
  const [copied, setCopied] = useState(false)
  const warn = share.state === 'expiring' || (live && isExpiring(left))

  async function copy() {
    try {
      await navigator.clipboard.writeText(share.url)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch { /* 浏览器不允许剪贴板时，链接就摆在下面那行，手动复制 */ }
  }

  return (
    <tr>
      <td>
        <div className="u-who">
          <span className="avatar">
            {(share.plan_name || '计').slice(0, 1).toUpperCase()}
          </span>
          <span style={{ minWidth: 0 }}>
            <span className="u-name">{share.plan_name || `计划 #${share.plan_id}`}</span>
            <span className="u-mail">
              {share.label || `分享 · ${share.token.slice(-6)}`}
            </span>
          </span>
        </div>
      </td>
      <td>
        <div className="sh-cd-cell">
          {live ? (
            <>
              <b className={`cd${warn ? ' is-warn' : ''}`}>{fmtCountdown(left)}</b>
              <span className="meta">
                {share.hours ? `${share.hours} 小时有效` : '有效期内'}
              </span>
            </>
          ) : (
            <span className="meta">
              {share.state === 'revoked' ? '已手动取消' : '有效期已结束'}
            </span>
          )}
        </div>
      </td>
      <td><span className={`pill ${STATE_PILL[share.state]}`}>{STATE_LABEL[share.state]}</span></td>
      <td className="col-hide"><span className="t-year">{fmtDate(share.created_at)}</span></td>
      <td className="act">
        <div className="sh-actions">
          <button className="btn btn-ghost" onClick={() => void copy()}>
            {copied ? '已复制' : '复制'}
          </button>
          <Link className="btn btn-ghost" to={`/share/${share.token}`} target="_blank"
                rel="noreferrer">预览</Link>
          <button className="btn btn-ghost" disabled={renewing}
                  onClick={() => setRenewing(true)}>重置有效期</button>
          {share.state !== 'revoked' && (
            <button className="btn btn-ghost" style={{ color: 'var(--danger)' }}
                    onClick={onRevoke}>取消分享</button>
          )}
        </div>
      </td>
      {renewing && (
        <RenewDialog share={share}
                     onClose={() => setRenewing(false)}
                     onDone={async () => { setRenewing(false); await onRenewed() }} />
      )}
    </tr>
  )
}

/** 重置有效期的小对话框（原型复用新建模态的「重置」模式；这里独立成一个小弹窗，
 *  因为它的字段只有"时长"，塞进新建模态反而要处理两套分支）。 */
function RenewDialog({ share, onClose, onDone }: {
  share: Share
  onClose: () => void
  onDone: () => Promise<void> | void
}) {
  const [hours, setHours] = useState(String(share.hours || 24))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  async function submit() {
    setBusy(true); setErr('')
    try {
      await api.renewShare(share.token, Number(hours) || 24)
      await onDone()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '重置失败')
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer" style={{ width: 'min(460px, 94vw)' }}>
        <header>
          <strong style={{ flex: 1 }}>重置分享有效期</strong>
          <button className="ghost" onClick={onClose}>关闭</button>
        </header>
        <div className="body stack">
          <p className="meta" style={{ lineHeight: 1.65, margin: 0 }}>
            有效期从<b>现在</b>重新起算（不是从原到期时间顺延），最长 24 小时。
            {share.state !== 'active' && share.state !== 'expiring'
              ? '该链接已失效，重置后将立即恢复可访问。' : ''}
          </p>
          <div className="field" style={{ maxWidth: '100%' }}>
            <label>有效期 <span className="meta">（最长 24 小时）</span></label>
            <div className="seg" role="group" aria-label="选择分享有效期">
              {['1', '6', '12', '24'].map((h) => (
                <button key={h} type="button" className={hours === h ? 'is-on' : ''}
                        aria-pressed={hours === h} onClick={() => setHours(h)}>
                  {h} 小时
                </button>
              ))}
            </div>
          </div>
          {err && <div className="banner error">{err}</div>}
          <div className="row">
            <button className="btn btn-primary" disabled={busy} onClick={() => void submit()}>
              {busy ? '处理中…' : '重置有效期'}
            </button>
          </div>
        </div>
      </aside>
    </>
  )
}
