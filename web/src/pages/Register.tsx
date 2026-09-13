/** 注册页 —— 对齐原型（`data-auth="register"`）：**单表单 + 获取验证码**。
 *
 *  ⑭ 邮箱验证码 / ⑮ 无 SMTP 时服务端会 403（此时整页给出"联系管理员开号"）。
 *  原型细节都保留了：6 位数字码（等宽字体、字距拉大）、60 秒倒计时按钮、
 *  密码强度条、确认密码、同意条款。
 */

import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ApiError, api } from '../api'
import { useAuth } from '../auth'
import { AuthAside } from './Login'
import type { AuthConfig } from '../types'

/** 原型 `pwOk`：至少 8 位且含字母与数字。 */
const pwOk = (pw: string) => pw.length >= 8 && /[A-Za-z]/.test(pw) && /\d/.test(pw)

/** 原型 `pwScore`：0 空 / 1 弱 / 2 中 / 3 强。 */
function pwScore(pw: string) {
  if (!pw) return 0
  let s = 0
  if (pw.length >= 8) s++
  if (/[A-Za-z]/.test(pw) && /\d/.test(pw)) s++
  if (pw.length >= 12 && /[^A-Za-z0-9]/.test(pw)) s++
  return Math.min(3, s) || 1
}
const PW_LABEL = ['', '弱', '中', '强']

export function RegisterPage() {
  const nav = useNavigate()
  const { refresh } = useAuth()
  const [cfg, setCfg] = useState<AuthConfig | null>(null)
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [agree, setAgree] = useState(false)
  const [showPw, setShowPw] = useState(false)
  const [error, setError] = useState('')
  const [hint, setHint] = useState('')
  const [left, setLeft] = useState(0)
  const [busy, setBusy] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(() => { api.authConfig().then(setCfg).catch(() => setCfg(null)) }, [])
  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current) }, [])

  function startCountdown(seconds: number) {
    setLeft(seconds)
    if (timer.current) window.clearInterval(timer.current)
    timer.current = window.setInterval(() => {
      setLeft((n) => {
        if (n <= 1) {
          if (timer.current) window.clearInterval(timer.current)
          return 0
        }
        return n - 1
      })
    }, 1000)
  }

  async function sendCode() {
    setError('')
    if (!/^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$/.test(email)) { setError('请输入有效的邮箱地址'); return }
    try {
      const r = await api.registerCode(email)
      setHint(`验证码已发送至 ${email}`)
      startCountdown(r.cooldown || 60)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '验证码发送失败')
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    if (!code.trim()) { setError('请填写邮箱验证码'); return }
    if (!pwOk(password)) { setError('密码至少 8 位，且包含字母与数字'); return }
    if (password2 !== password) { setError('两次输入的密码不一致'); return }
    if (!agree) { setError('请先阅读并同意用户协议'); return }
    setBusy(true)
    try {
      // agree 必须传给服务端：前端这个勾选框只是交互，**服务端也会拒**不同意的人。
      await api.register(email, code.trim(), password, '', true)
      await refresh()                       // 服务端注册即登录；刷新一下拿到用户态
      nav('/', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '注册失败')
    } finally { setBusy(false) }
  }

  if (cfg && !cfg.open_registration) {
    return (
      <div className="auth">
        <AuthAside />
        <main className="auth-main">
          <div className="auth-card">
            <h1 className="auth-title">注册已关闭</h1>
            <p className="auth-sub">本站未配置邮件服务，无法自助注册。请联系管理员开通账号。</p>
            <p className="auth-switch"><Link className="link" to="/login">返回登录</Link></p>
          </div>
        </main>
      </div>
    )
  }

  const score = pwScore(password)

  return (
    <div className="auth">
      <AuthAside />
      <main className="auth-main">
        <div className="auth-card">
          <div className="auth-logo"><span className="brand-mark">文</span></div>
          <h1 className="auth-title">创建账号</h1>
          <p className="auth-sub">验证邮箱后即可开始建立你的文献库</p>

          {error && <div className="banner error" style={{ marginTop: 18 }}>{error}</div>}

          <form className="auth-view" onSubmit={submit}>
            <div className="field">
              <label htmlFor="rgEmail">邮箱</label>
              <input id="rgEmail" className="input" type="email" autoComplete="email"
                     placeholder="you@university.edu" value={email}
                     onChange={(e) => setEmail(e.target.value)} required />
              {cfg && cfg.allowed_domains.length > 0 && (
                <p className="hint" style={{ marginLeft: 0 }}>
                  仅接受以下域名的邮箱：{cfg.allowed_domains.join('、')}
                </p>
              )}
            </div>

            <div className="field">
              <label htmlFor="rgCode">邮箱验证码</label>
              <div className="code-row">
                <input id="rgCode" className="input code" inputMode="numeric" maxLength={6}
                       autoComplete="one-time-code" placeholder="6 位数字" value={code}
                       onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} />
                <button className="btn btn-secondary" type="button" id="rgSend"
                        disabled={left > 0} onClick={() => void sendCode()}>
                  {left > 0 ? `${left} 秒后重发` : '获取验证码'}
                </button>
              </div>
              {hint && <p className="hint">{hint}</p>}
            </div>

            <div className="field">
              <label htmlFor="rgPass">设置密码</label>
              <div className="input-wrap">
                <input id="rgPass" className="input" type={showPw ? 'text' : 'password'}
                       autoComplete="new-password" placeholder="至少 8 位，含字母与数字"
                       value={password} onChange={(e) => setPassword(e.target.value)} />
                <button className="reveal" type="button" aria-label="显示密码"
                        onClick={() => setShowPw((v) => !v)}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
                       strokeLinecap="round" strokeLinejoin="round">
                    <path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z" />
                    <circle cx="12" cy="12" r="2.9" />
                  </svg>
                </button>
              </div>
              {password && (
                <div className={`pw s${score}`}>
                  <span className="pw-bar"><i /><i /><i /></span>
                  <span className="pw-txt">{PW_LABEL[score]}</span>
                </div>
              )}
            </div>

            <div className="field">
              <label htmlFor="rgPass2">确认密码</label>
              <input id="rgPass2" className="input" type={showPw ? 'text' : 'password'}
                     autoComplete="new-password" placeholder="再次输入密码"
                     value={password2} onChange={(e) => setPassword2(e.target.value)} />
            </div>

            {/*
              用户协议（2026-09-11）：条文由服务端 `GET /api/auth/config` 下发，
              前端只负责渲染 —— 文案的唯一事实来源是 `server/terms.py`，改一处两边同步。
              用 `<details>` 而不是弹窗：条款要能**在提交前读完**，弹窗会诱导直接点"同意"。
            */}
            <details className="terms">
              <summary>用户协议（必读）</summary>
              <ol className="terms-list">
                {(cfg?.terms_body ?? []).map((s) => (
                  <li key={s.title}>
                    <b>{s.title}</b>
                    <span>{s.text}</span>
                  </li>
                ))}
              </ol>
              <p className="terms-ver">版本 {cfg?.terms_version ?? '—'}</p>
            </details>

            <label className="check block">
              <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
              <span>{cfg?.terms_summary ?? '我已阅读并同意用户协议，并对上传与分享行为承担全部责任'}</span>
            </label>

            <button className="btn btn-primary btn-block btn-lg" style={{ marginTop: 16 }} disabled={busy}>
              {busy ? '创建中…' : '创建账号'}
            </button>
          </form>

          <p className="auth-switch">已有账号？<Link className="link" to="/login">直接登录</Link></p>
        </div>
      </main>
    </div>
  )
}
