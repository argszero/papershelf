/** 图标 —— 内联 SVG，**不引第三方图标库**（决策：不依赖任何 CDN，产物要能离线自托管）。
 *
 * 造型直接取自原型「文献台 PaperDesk」：1.6~1.9 描边、圆角端点、24×24 viewBox。
 * 用 `currentColor` 取色，所以图标永远跟它所在元素的文字颜色一致
 * （状态色药丸、幽灵按钮都不需要额外传色）。
 */

type P = { className?: string }

const box = '0 0 24 24'

export function IconGrid({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.6" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.6" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.6" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.6" />
    </svg>
  )
}

export function IconLibrary({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 19V6a2 2 0 0 1 2-2h8.5L18 7.5V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z" />
      <path d="M8 9h5M8 13h6M8 17h4" />
    </svg>
  )
}

export function IconBoard({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <rect x="3.5" y="4" width="5" height="16" rx="1.5" /><rect x="9.5" y="4" width="5" height="10" rx="1.5" />
      <rect x="15.5" y="4" width="5" height="13" rx="1.5" />
    </svg>
  )
}

export function IconLayers({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <path d="m12 3 9 5-9 5-9-5 9-5z" /><path d="m3.5 12.5 8.5 4.7 8.5-4.7" />
    </svg>
  )
}

export function IconUsers({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="9" cy="8" r="3.2" /><path d="M3.5 19c0-3 2.5-5 5.5-5s5.5 2 5.5 5" />
      <path d="M16 5.2a3.2 3.2 0 0 1 0 5.9M18 13.6c1.6.7 2.5 2.1 2.5 4.4" />
    </svg>
  )
}

export function IconSearch({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
      <circle cx="11" cy="11" r="7" /><path d="m20 20-3.4-3.4" />
    </svg>
  )
}

export function IconChevron({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="m6 9 6 6 6-6" />
    </svg>
  )
}

export function IconBack({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M19 12H5M11 18l-6-6 6-6" />
    </svg>
  )
}

export function IconPlus({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

export function IconTick({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m5 12.5 4.5 4.5L19 7" />
    </svg>
  )
}

export function IconAlert({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 4 2.6 20h18.8z" /><path d="M12 10v4M12 17.3v.4" />
    </svg>
  )
}

export function IconClock({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="8.2" /><path d="M12 7.8V12l2.8 1.8" />
    </svg>
  )
}

export function IconLang({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.5 6h9M8 6V4M5.2 6c.5 3 2.4 5.6 5.3 7.4M10.8 6c-.5 3-2.4 5.6-5.3 7.4" />
      <path d="m13 20 3.6-9 3.6 9M14.4 17h4.4" />
    </svg>
  )
}

export function IconFile({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <path d="M7 3h7l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" /><path d="M14 3v4h4" />
    </svg>
  )
}

export function IconImage({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round">
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" /><circle cx="9" cy="9.6" r="1.6" />
      <path d="m5.4 17 4.4-4.4 3.4 3.4 2.4-2.4L20 16.6" />
    </svg>
  )
}

export function IconShare({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="12" r="2.4" /><circle cx="17.5" cy="6" r="2.4" /><circle cx="17.5" cy="18" r="2.4" />
      <path d="m8.2 10.9 7.1-3.6M8.2 13.1l7.1 3.6" />
    </svg>
  )
}

export function IconLogout({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M15 4h3.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H15" />
      <path d="M10 8.5 13.5 12 10 15.5M13.5 12H4" />
    </svg>
  )
}

export function IconNote({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z" /><path d="M14 4v5h5M8 13h7M8 17h4" />
    </svg>
  )
}

export function IconOutline({ className }: P) {
  return (
    <svg className={className} viewBox={box} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
      <path d="M4 6h16M4 12h11M4 18h7" />
    </svg>
  )
}
