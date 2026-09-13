/** 整站路由表 —— **登录态与只读分享共用这一张**（决策⑩）。
 *
 * 为什么单独一个模块：分享外壳（`shell/ShareShell`）与登录外壳（`App.PrivateShell`）
 * 都要把这张表挂进自己的 `.main` 里。若写在 `App.tsx` 里，`shell` 与 `App` 就会
 * 互相 import（循环）。表本身与"谁来守卫"无关，抽出来两边都能用。
 *
 * 分享态下 `:paperId` / `:planId` 照常匹配 —— 它们只是路径参数，取数走
 * `/api/shares/{token}*` 白名单，越权的那篇由服务端 404（⑪ 的硬约束在后端）。
 */

import { Route, Routes } from 'react-router-dom'

import { NotFound } from './components'
import { AdminPage } from './pages/Admin'
import { BoardPage } from './pages/Board'
import { LibraryPage } from './pages/Library'
import { OverviewPage } from './pages/Overview'
import { PlansPage } from './pages/Plans'
import { ReaderPage } from './pages/Reader'
import { SharesPage } from './pages/Shares'

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<OverviewPage />} />
      <Route path="/library" element={<LibraryPage />} />
      <Route path="/board" element={<BoardPage />} />
      <Route path="/plans" element={<PlansPage />} />
      <Route path="/plans/:planId" element={<PlansPage />} />
      <Route path="/shares" element={<SharesPage />} />
      <Route path="/reader" element={<ReaderPage />} />
      <Route path="/reader/:paperId" element={<ReaderPage />} />
      <Route path="/papers/:paperId" element={<ReaderPage />} />
      <Route path="/admin" element={<AdminPage />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  )
}
