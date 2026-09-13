/** 忘记密码页 —— 对齐原型（`data-auth="forgot"`）：**两步向导**。
 *
 *  第 1 步「验证邮箱」：邮箱 + 6 位验证码（带 60 秒倒计时）
 *  第 2 步「重置密码」：新密码 + 确认
 *
 *  服务端在「未注册邮箱」上也返回成功（防探测），所以这一步**不会**提前告诉
 *  用户邮箱是否存在 —— 文案统一写成"如果该邮箱已注册，验证码已发送"。
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../api'
import { AuthAside } from './Login'

const pwOk = (pw: string) => pw.length >= 8 && /[A-Za-z]/.test(pw) && /\d/.test(pw)

export function ForgotPage() {
  const nav = useNavigate()
  const [step, setStep] = useState<1 | 2>(1)
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [error, setError] = useState('')
  const [hint, setHint] = useState('')
  const [left, setLeft] = useState(0)
  const [busy, setBusy] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current) }, [])

  function startCountdown(seconds: number) {
    setLeft(seconds)
    if (timer.current) window.clearInterval(timer.current)
    timer.current = window.setInterval(() => {
      setLeft((n) => {
        if (n <= 1) { if (timer.current) window.clearInterval(timer.current); return 0 }
        return n - 1
      })
    }, 1000)
  }

  async function sendCode() {
    setError('')
    if (!/^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$/.test(email)) { setError('请输入有效的邮箱地址'); return }
    try {
      const r = await api.resetCode(email)
      setHint(`如果 ${email} 已注册，验证码已发送，请查收`)
      startCountdown(r.cooldown || 60)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '验证码发送失败')
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    // 第 1 步：只做本地校验，真正的验证放在第 2 步提交（服务端一次性校验）
    if (step === 1) {
      if (!code.trim()) { setError('请填写邮箱验证码'); return }
      setStep(2)
      return
    }
    if (!pwOk(password)) { setError('密码至少 8 位，且包含字母与数字'); return }
    if (password2 !== password) { setError('两次输入的密码不一致'); return }
    setBusy(true)
    try {
      await api.resetPassword(email, code.trim(), password)
      nav('/login?reset=1', { replace: true })
    } catch (err) {
      // 验证码可能已过期/作废 → 退回第 1 步让用户重取
      setError(err instanceof ApiError ? err.message : '重置失败')
      setStep(1)
    } finally { setBusy(false) }
  }

  return (
    <div className="auth">
      <AuthAside />
      <main className="auth-main">
        <div className="auth-card">
          <div className="auth-logo"><span className="brand-mark">文</span></div>
          <h1 className="auth-title">找回密码</h1>
          <p className="auth-sub">我们会向你的邮箱发送验证码以重置密码</p>

          <button className="link back" type="button" style={{ marginTop: 14 }}
                  onClick={() => (step === 2 ? setStep(1) : nav('/login'))}>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
                 strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="m14 6-6 6 6 6" />
            </svg>
            {step === 2 ? '上一步' : '返回登录'}
          </button>

          <div className="steps">
            <span className={`step ${step === 1 ? 'is-on' : 'is-done'}`}><i>1</i>验证邮箱</span>
            <span className="step-sep" />
            <span className={`step ${step === 2 ? 'is-on' : ''}`}><i>2</i>重置密码</span>
          </div>

          {error && <div className="banner error" style={{ marginTop: 4, marginBottom: 14 }}>{error}</div>}

          <form className="auth-view" style={{ marginTop: 0 }} onSubmit={submit}>
            {step === 1 ? (
              <>
                <div className="field">
                  <label htmlFor="fpEmail">注册邮箱</label>
                  <input id="fpEmail" className="input" type="email" autoComplete="email"
                         placeholder="you@university.edu" value={email}
                         onChange={(e) => setEmail(e.target.value)} required />
                </div>
                <div className="field">
                  <label htmlFor="fpCode">邮箱验证码</label>
                  <div className="code-row">
                    <input id="fpCode" className="input code" inputMode="numeric" maxLength={6}
                           autoComplete="one-time-code" placeholder="6 位数字" value={code}
                           onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} />
                    <button className="btn btn-secondary" type="button" id="fpSend"
                            disabled={left > 0} onClick={() => void sendCode()}>
                      {left > 0 ? `${left} 秒后重发` : '获取验证码'}
                    </button>
                  </div>
                  {hint && <p className="hint">{hint}</p>}
                </div>
                <button className="btn btn-primary btn-block btn-lg">下一步</button>
              </>
            ) : (
              <>
                <div className="field">
                  <label htmlFor="fpPass">新密码</label>
                  <div className="input-wrap">
                    <input id="fpPass" className="input" type={showPw ? 'text' : 'password'}
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
                </div>
                <div className="field">
                  <label htmlFor="fpPass2">确认新密码</label>
                  <input id="fpPass2" className="input" type={showPw ? 'text' : 'password'}
                         autoComplete="new-password" placeholder="再次输入新密码"
                         value={password2} onChange={(e) => setPassword2(e.target.value)} />
                </div>
                <button className="btn btn-primary btn-block btn-lg" disabled={busy}>
                  {busy ? '重置中…' : '重置密码'}
                </button>
              </>
            )}
          </form>
        </div>
      </main>
    </div>
  )
}
