/** 分享有效期倒计时 —— **全站唯一**的时间格式与递减逻辑。
 *
 * 为什么单独一个模块而不是各页面各写一个 `useEffect`：
 * 分享后有三处同时显示倒计时（只读页 banner、分享管理表格、新建/续期后的结果框），
 * 三处若各自维护一份剩余秒数，就会出现"同一时刻两处差一秒"的观感缺陷
 * （更糟的是其中一处忘了清理定时器，页面切走后还在跑）。
 *
 * 这里统一：**服务端给剩余秒数**（`Share.seconds_left` / `ShareMeta.seconds_left`），
 * 前端只做每秒递减 —— 客户端时钟与服务端有偏差时以服务端为准，本地只负责走过去。
 */

import { useEffect, useState } from 'react'

/** 秒 → `HH:MM:SS`（原型 `fmtCd` 同款；超过 24 小时也照常显示 `24:00:00`）。 */
export function fmtCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`
}

/** 把**起始剩余秒数**变成每秒递减的实时值。
 *
 * ⚠️ `from` 变化时重新起算（续期后同一组件会拿到新的秒数）；
 * 页面切走由 `useEffect` 的清理负责 —— 否则每个视图都留一个 1s 心跳。
 */
export function useCountdown(from: number, active = true): number {
  const [left, setLeft] = useState(from)
  useEffect(() => { setLeft(from) }, [from])
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => setLeft((v) => Math.max(0, v - 1)), 1000)
    return () => clearInterval(t)
  }, [active, from])
  return left
}

/** 剩余不足 1 小时 = 即将到期（与**服务端** `state === 'expiring'` 同一阈值）。 */
export function isExpiring(secondsLeft: number): boolean {
  return secondsLeft > 0 && secondsLeft < 3600
}
