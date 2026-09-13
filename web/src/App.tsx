/** 应用外壳与路由表。
 *
 * 结构对齐原型（M5「复刻原型」）：**左侧栏 + 顶栏 + 视图区**，五个一级视图各占一条路由，
 * 刷新任何一个子路径都能落到对应页面（后端把所有未知 GET 交给 SPA）。
 *
 * ── 只读分享（⑩⑪）为什么是**同一套路由再挂一份**，而不是单个 `/share/:token` 页面 ──
 * 宿主 2026-09-11：「分享后的页面应该和分享者看到的一模一样，只是只读。」
 * 原型给出的正是这个结构：进分享模式只是给外壳加 `.readonly` + 挂一条 banner，
 * 侧栏/顶栏/五个视图**一个都没换**，点的还是同一套 `go('library')`。
 * 所以这里把整张 `AppRoutes` 在 `/share/:token/*` 下**原样再挂一遍**，只有两处不同：
 * 守卫从"必须有登录态"改成"必须是有效分享令牌"，外壳是只读版
 * （写入口隐藏、站内链接带 token 前缀）。另写一个精简分享页会立刻偏离 ⑩
 * —— 这正是被推翻的那版实现。
 */

import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { AuthProvider, useAuth } from './auth'
import { ForgotPage } from './pages/Forgot'
import { LoginPage } from './pages/Login'
import { RegisterPage } from './pages/Register'
import { ShareProvider } from './shareContext'
import { PlanProvider } from './planContext'
import { NotFound } from './components'
import { AppRoutes } from './routes'
import { Sidebar, ShareShell } from './shell'

function PrivateShell() {
  const { user, loading } = useAuth()
  const loc = useLocation()
  if (loading) return <div className="auth-wrap"><p className="muted">加载中…</p></div>
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname)}`} replace />
  return (
    <div className="app">
      <Sidebar />
      <div className="main">
        <AppRoutes />
      </div>
    </div>
  )
}

function Shell() {
  const loc = useLocation()
  // 认证页是独立版式（没有侧栏可言）
  if (/^\/(login|register|forgot)/.test(loc.pathname)) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/forgot" element={<ForgotPage />} />
      </Routes>
    )
  }
  // 只读分享：整张路由表挂在 `/share/:token/*` 下，与登录态一一对应
  if (loc.pathname === '/share' || loc.pathname.startsWith('/share/')) {
    return (
      <Routes>
        <Route path="/share/:token/*" element={
          <ShareProvider>
            <PlanProvider>
              <ShareShell />
            </PlanProvider>
          </ShareProvider>
        } />
        {/* `/share` 裸路径（漏了 token）：给一句人话，别落到 SPA 兜底页 */}
        <Route path="*" element={
          <NotFound title="链接不完整" hint="分享链接缺少令牌，请让分享者重新复制。" />
        } />
      </Routes>
    )
  }
  return (
    <PlanProvider>
      <PrivateShell />
    </PlanProvider>
  )
}

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Shell />
      </AuthProvider>
    </BrowserRouter>
  )
}
