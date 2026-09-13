/**
 * 认证上下文 —— 一个用户对象 + 几个动作，够用即可。
 *
 * `me()` 是**唯一**判断登录态的途径：cookie 是 HttpOnly 的（安全），
 * 前端拿不到也不该拿，所以刷新页面后必须重新问服务端（决策⑨ 的会话机制落地）。
 */

import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'

import { ApiError, api } from './api'
import type { CurrentUser } from './types'

interface AuthValue {
  user: CurrentUser | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      setUser(await api.me())
    } catch (e) {
      // 401 = 未登录（正常状态）；其它错误也不要卡住界面
      if (!(e instanceof ApiError) || e.status !== 401) console.warn(e)
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const login = useCallback(async (email: string, password: string) => {
    setUser(await api.login(email, password))
  }, [])

  const logout = useCallback(async () => {
    await api.logout()
    setUser(null)
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthValue {
  const v = useContext(AuthContext)
  if (!v) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return v
}
