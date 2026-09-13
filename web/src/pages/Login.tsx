/** 登录页 —— ⑭⑮ 的服务端策略直接体现在这里：
 *  未配 SMTP → 不显示注册入口，改为提示「联系管理员开号」；
 *  配了 SMTP → 显示注册表单，并明示只接受白名单域名。
 *
 * 版式对齐原型：左侧深色品牌区 + 右侧表单（窄屏只剩表单）。
 */

import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '../api'
import { useAuth } from '../auth'
import type { AuthConfig } from '../types'

export function AuthAside() {
  return (
    <aside className="auth-aside">
      <div className="auth-brand">
        <span className="brand-mark">文</span>
        <span>
          <span className="brand-name">文献台</span>
          <span className="brand-sub">PaperDesk</span>
        </span>
      </div>
      <div className="auth-body">
        <div className="auth-pitch">
          <h2>把论文读成自己的<br />中文精读稿</h2>
          <p>PDF 与 arXiv 进库后自动转换：保版式排版、左右对照、公式原位渲染。</p>
        </div>
        <ol className="auth-steps">
          <li><span className="as-n">01</span><span><b>导入</b>上传 PDF 或贴 arXiv 编号</span></li>
          <li><span className="as-n">02</span><span><b>转换</b>解析 → 公式 LaTeX 化 → 逐块翻译</span></li>
          <li><span className="as-n">03</span><span><b>精读</b>左右对照、块级修订、笔记与进度</span></li>
        </ol>
        <p className="auth-foot">自托管 · 数据留存在你自己的机器上</p>
      </div>
    </aside>
  )
}

export function LoginPage() {
  const { login, user } = useAuth()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [cfg, setCfg] = useState<AuthConfig | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.authConfig().then(setCfg).catch(() => setCfg(null)) }, [])
  useEffect(() => { if (user) nav(params.get('next') || '/', { replace: true }) }, [user, nav, params])

  const reset = params.get('reset')
  const registered = params.get('registered')

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      await login(email, password)
      nav(params.get('next') || '/', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally { setBusy(false) }
  }

  return (
    <div className="auth">
      <AuthAside />
      <main className="auth-main">
        <div className="auth-card">
          <div className="auth-logo"><span className="brand-mark">文</span></div>
          <h1 className="auth-title">登录文献台</h1>
          <p className="auth-sub">使用邮箱继续管理你的文献阅读进度</p>

          {registered && <div className="banner ok" style={{ marginTop: 18 }}>注册成功，请登录。</div>}
          {reset && <div className="banner ok" style={{ marginTop: 18 }}>密码已重置，请使用新密码登录。</div>}
          {error && <div className="banner error" style={{ marginTop: 18 }}>{error}</div>}

          <form className="auth-view" onSubmit={submit}>
            <div className="field">
              <label htmlFor="liEmail">邮箱</label>
              <input id="liEmail" className="input" type="email" autoComplete="username"
                     placeholder="you@university.edu" value={email}
                     onChange={(e) => setEmail(e.target.value)} required />
            </div>
            <div className="field">
              <label htmlFor="liPass">密码</label>
              <input id="liPass" className="input" type="password" autoComplete="current-password"
                     placeholder="输入密码" value={password}
                     onChange={(e) => setPassword(e.target.value)} required />
            </div>
            <div className="auth-row">
              <span />
              <Link className="link" to="/forgot">忘记密码？</Link>
            </div>
            <button className="btn btn-primary btn-block btn-lg" disabled={busy}>
              {busy ? '登录中…' : '登录'}
            </button>
          </form>

          {cfg && !cfg.open_registration && (
            <p className="auth-note">
              本站未配置邮件服务，自助注册已关闭 —— 请联系管理员开通账号。
            </p>
          )}
          {cfg?.open_registration && (
            <p className="auth-switch">
              没有账号？<Link className="link" to="/register">注册</Link>
              {cfg.allowed_domains.length > 0 && `（仅限 ${cfg.allowed_domains.join('、')} 邮箱）`}
            </p>
          )}
          <p className="auth-legal">papershelf · PDF / arXiv → 保版式中文精读</p>
        </div>
      </main>
    </div>
  )
}
