/** 用户管理（决策㉑ 最小管理页）。
 *
 * 刻意**不做**的事：编辑 SMTP / LLM Key / 白名单 —— 那些走环境变量（㉑ 的边界）。
 * 这里只有四件事：列表、开号、禁用、重置密码。
 * ⑮ 未配 SMTP 时，这里是**唯一**的开号入口。
 *
 * 观感对齐原型 `renderAdmin`：指标卡 + 筛选 chips + `.ptable` 用户表。
 * 与原型的三处**刻意差异**（后端数据形态决定，不是偷懒）：
 *   ① 没有「最近登录 / 登录次数」——v1 不记登录行为（㉑ 最小管理页）；
 *   ② 没有删除用户——v1 的处置是**禁用**（删号会连带删掉他的文献与笔记）；
 *   ③ 开通账号做成表格上方的行内表单，而不是弹窗 —— 服务端开号有 3 个字段，弹窗不值当。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { ApiError, api } from '../api'
import { TopBar } from '../shell'
import { Tile } from '../components'
import { IconPlus } from '../icons'
import type { AdminUser } from '../types'

const USTATUS: Record<AdminUser['status'], { label: string; cls: string }> = {
  active: { label: '正常', cls: 'st-read' },
  pending: { label: '待激活', cls: 'st-converting' },
  disabled: { label: '已停用', cls: 'st-failed' },
}

export function AdminPage() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [q, setQ] = useState('')
  const [filter, setFilter] = useState<'all' | AdminUser['status']>('all')
  const [formOpen, setFormOpen] = useState(false)
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)

  const load = useCallback(async () => {
    try { setUsers(await api.adminUsers()) }
    catch (e) { setError(e instanceof ApiError ? e.message : '加载失败') }
  }, [])

  useEffect(() => { void load() }, [load])

  const counts = useMemo(() => ({
    all: users.length,
    active: users.filter((u) => u.status === 'active').length,
    pending: users.filter((u) => u.status === 'pending').length,
    disabled: users.filter((u) => u.status === 'disabled').length,
  }), [users])

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return users.filter((u) => {
      if (filter !== 'all' && u.status !== filter) return false
      if (!needle) return true
      return `${u.email} ${u.display_name || ''}`.toLowerCase().includes(needle)
    })
  }, [users, q, filter])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setError(''); setMsg('')
    try {
      await api.adminCreateUser(email, password, name, isAdmin)
      setMsg(`已开通 ${email}（可直接登录，无需邮件激活）`)
      setEmail(''); setName(''); setPassword(''); setIsAdmin(false); setFormOpen(false)
      await load()
    } catch (err) { setError(err instanceof Error ? err.message : '开号失败') }
  }

  async function resetPassword(u: AdminUser) {
    const pwd = prompt(`为 ${u.email} 设置新密码（至少 8 位）：`)
    if (!pwd) return
    if (pwd.length < 8) { setError('密码至少 8 位'); return }
    try { await api.adminPatchUser(u.id, { password: pwd }); setMsg(`已重置 ${u.email} 的密码`) }
    catch (e) { setError(e instanceof Error ? e.message : '重置失败') }
  }

  return (
    <>
      <TopBar title="用户管理" sub={`${counts.all} 个账号 · 开放注册限白名单域名，白名单走环境变量`} />
      <div className="view-scroll">
        <div className="view">
          <section className="grid g-4" style={{ marginBottom: 16 }}>
            <Tile k="账号总数" v={counts.all} unit="个" d="SMTP 未配时的唯一开号入口" />
            <Tile k="正常" v={counts.active} unit="个" d="可直接登录" />
            <Tile k="待激活" v={counts.pending} unit="个" d="注册后未点激活链接" />
            <Tile k="已停用" v={counts.disabled} unit="个" d="被管理员停用的账号" />
          </section>

          {error && <div className="banner error">{error}</div>}
          {msg && <div className="banner ok">{msg}</div>}

          <div className="lib-toolbar">
            <button className="btn btn-primary btn-sm" onClick={() => setFormOpen((v) => !v)}>
              <IconPlus />{formOpen ? '收起' : '开通账号'}
            </button>
            <span className="spacer" />
            {([['all', '全部'], ['active', '正常'], ['pending', '待激活'], ['disabled', '已停用']] as const)
              .map(([k, label]) => (
                <button key={k} className={`chip${filter === k ? ' is-on' : ''}`}
                        onClick={() => setFilter(k)}>
                  {label} <span className="c">{counts[k as keyof typeof counts]}</span>
                </button>
              ))}
            <label className="search" style={{ minWidth: 200, maxWidth: 320 }}>
              <input type="search" value={q} placeholder="搜索邮箱或姓名…" aria-label="搜索用户"
                     onChange={(e) => setQ(e.target.value)} />
            </label>
          </div>

          {formOpen && (
            <form className="card" onSubmit={create} style={{ marginBottom: 16 }}>
              <div className="card-h">
                <div>
                  <span className="eyebrow">开通账号</span>
                  <h3 className="h3" style={{ marginTop: 5 }}>直接创建一个可用账号</h3>
                </div>
                <span className="meta">无需邮件激活</span>
              </div>
              <div className="row wrap" style={{ alignItems: 'flex-end', gap: 12 }}>
                <div className="field" style={{ flex: '1 1 220px' }}>
                  <label>邮箱</label>
                  <input className="input" value={email} type="email" required
                         onChange={(e) => setEmail(e.target.value)} />
                </div>
                <div className="field" style={{ flex: '1 1 140px' }}>
                  <label>姓名（可选）</label>
                  <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
                </div>
                <div className="field" style={{ flex: '1 1 160px' }}>
                  <label>初始密码（≥8 位）</label>
                  <input className="input" value={password} type="password" minLength={8} required
                         onChange={(e) => setPassword(e.target.value)} />
                </div>
                <label className="row" style={{ gap: 6, marginBottom: 8 }}>
                  <input type="checkbox" checked={isAdmin} style={{ width: 'auto' }}
                         onChange={(e) => setIsAdmin(e.target.checked)} />
                  <span>设为管理员</span>
                </label>
                <button className="btn btn-primary" style={{ marginBottom: 8 }}>开通</button>
              </div>
            </form>
          )}

          <div className="card" style={{ padding: '6px 8px 2px' }}>
            <div className="table-scroll">
              <table className="ptable">
                <thead>
                  <tr>
                    <th style={{ width: '34%' }}>用户</th>
                    <th>角色</th><th>状态</th>
                    <th className="col-hide">开通时间</th>
                    <th className="act">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((u) => (
                    <tr key={u.id} style={{ cursor: 'default' }}>
                      <td>
                        <div className="u-who">
                          <span className="avatar">
                            {(u.display_name || u.email || '用').slice(0, 1).toUpperCase()}
                          </span>
                          <span style={{ minWidth: 0 }}>
                            <span className="u-name">
                              {u.display_name || u.email.split('@')[0]}
                              {u.is_admin && <span className="pill st-reading" style={{ marginLeft: 8 }}>管理员</span>}
                            </span>
                            <span className="u-mail">{u.email}</span>
                          </span>
                        </div>
                      </td>
                      <td>{u.is_admin ? '管理员' : '普通用户'}</td>
                      <td><span className={`pill ${USTATUS[u.status].cls}`}>{USTATUS[u.status].label}</span></td>
                      <td className="col-hide">
                        <span className="t-year">{(u.activated_at || u.created_at || '').slice(0, 10)}</span>
                      </td>
                      <td className="act">
                        <div className="row-acts">
                          <button className="btn btn-ghost btn-sm"
                                  onClick={() => void api.adminPatchUser(u.id, { is_admin: !u.is_admin })
                                    .then(load).catch((e) => setError(e.message))}>
                            {u.is_admin ? '取消管理员' : '设为管理员'}
                          </button>
                          <button className="btn btn-ghost btn-sm"
                                  onClick={() => void api.adminPatchUser(u.id, {
                                    status_: u.status === 'disabled' ? 'active' : 'disabled',
                                  }).then(load).catch((e) => setError(e.message))}>
                            {u.status === 'disabled' ? '启用' : '停用'}
                          </button>
                          <button className="btn btn-ghost btn-sm"
                                  onClick={() => void resetPassword(u)}>重置密码</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {shown.length === 0 && (
                    <tr style={{ cursor: 'default' }}>
                      <td colSpan={5}>
                        <div className="empty">
                          <p style={{ margin: '0 0 6px' }}>没有匹配的用户</p>
                          <span className="meta">尝试更换关键词或筛选条件</span>
                        </div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="table-foot">
              <span className="meta">显示 {shown.length} / {counts.all} 个账号</span>
              <span className="meta">SMTP、LLM Key、注册白名单走环境变量（决策㉑）</span>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}
