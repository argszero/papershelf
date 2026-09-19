/** 文献元数据编辑抽屉（决策⑲ 的「可手动改」）。
 *
 * 为什么有这个文件（2026-09-12）：`PATCH /api/papers/{id}` 后端一直支持改
 * `title`/`authors`/`venue`/`year`/`tags`，但**前端没有任何入口** —— 于是自动抽取
 * （⑲，同日补做）出问题或抽错时，用户只能看着错的元数据，无处可改。
 * 表格里那几列因此是"死列"。
 *
 * 形态跟随本仓库既有约定：**抽屉**（`.drawer`），不用模态 —— 与导入、术语表、
 * 新建分享一致。原型里根本没有元数据编辑，所以这里没有形态可抄，只能跟随本仓库。
 */

import { useEffect, useState } from 'react'

import { api } from '../api'
import type { Paper } from '../types'

export function PaperMetaDrawer({ paper, onClose, onSaved }: {
  paper: Paper
  onClose: () => void
  onSaved: () => void
}) {
  const [title, setTitle] = useState(paper.title || '')
  const [authors, setAuthors] = useState(paper.authors || '')
  const [venue, setVenue] = useState(paper.venue || '')
  const [year, setYear] = useState(paper.year ? String(paper.year) : '')
  const [tags, setTags] = useState((paper.tags || []).join(', '))
  // ㊺ 手动改进度（宿主 2026-09-19 选 C：工具栏就地编辑 + 这里，两个入口同一接口）
  const [progress, setProgress] = useState(String(paper.progress ?? 0))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  // Esc 关闭 —— 与导入抽屉一致
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const save = async () => {
    setBusy(true)
    setErr('')
    try {
      // ⚠️ `year` 空串必须发 null 而不是 0：后端 `year: int | None`，
      //    传 0 会被当成"公元 0 年"存进去，之后排序与显示全乱。
      const y = year.trim()
      const p = progress.trim()
      // 进度**只在真被改过时才发**：否则"改个标签"会顺带写一次进度，
      // 而进度是非幂等的（`progress_by:"user"` 无条件生效、可改小、可越过「已读」锁）——
      // 白白多写一次同一个值是小事，把它塞进每一次元数据保存是隐患。
      const nextProgress = p === '' ? Number(paper.progress ?? 0) : Math.max(0, Math.min(100, Number(p)))
      await api.updatePaper(paper.id, {
        title: title.trim(),
        authors: authors.trim(),
        venue: venue.trim(),
        year: y ? Number(y) : null,
        tags: tags.split(/[,，、]/).map((t) => t.trim()).filter(Boolean),
        ...(nextProgress !== (paper.progress ?? 0)
          ? { progress: nextProgress, progress_by: 'user' as const } : {}),
      })
      onSaved()
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="编辑文献信息">
        <header>
          <strong style={{ flex: 1 }}>编辑文献信息</strong>
          <button className="ghost" onClick={onClose}>关闭</button>
        </header>
        <form className="body stack" onSubmit={(e) => { e.preventDefault(); void save() }}>
          {err && <div className="banner error">{err}</div>}

          <div className="field" style={{ maxWidth: '100%' }}>
            <label>标题</label>
            <input className="input" value={title} autoFocus
                   onChange={(e) => setTitle(e.target.value)} placeholder="论文标题" />
          </div>

          <div className="field" style={{ maxWidth: '100%' }}>
            <label>作者</label>
            <input className="input" value={authors}
                   onChange={(e) => setAuthors(e.target.value)} placeholder="多位作者用 · 分隔" />
          </div>

          <div className="field" style={{ maxWidth: '100%' }}>
            <label>期刊 / 会议（可选）</label>
            <input className="input" value={venue}
                   onChange={(e) => setVenue(e.target.value)} placeholder="如 Precision Engineering" />
          </div>

          <div className="field" style={{ maxWidth: '100%' }}>
            <label>年份（可选）</label>
            <input className="input" value={year} inputMode="numeric"
                   onChange={(e) => setYear(e.target.value.replace(/[^0-9]/g, '').slice(0, 4))}
                   placeholder="2024" />
          </div>

          <div className="field" style={{ maxWidth: '100%' }}>
            <label>标签（可选，逗号分隔）</label>
            <input className="input" value={tags}
                   onChange={(e) => setTags(e.target.value)}
                   placeholder="如 增材制造, 缺陷检测" />
          </div>

          <div className="field" style={{ maxWidth: '100%' }}>
            <label>阅读进度（%）</label>
            <input className="input" value={progress} inputMode="numeric"
                   onChange={(e) => setProgress(e.target.value.replace(/[^0-9]/g, '').slice(0, 3))}
                   placeholder="0-100" />
          </div>

          <p className="meta">
            进度也可以直接点阅读器工具栏那行「进度 N%」改。改小之后**继续滚动仍会按
            "只增不减"累计**（方案 B）—— 想让它停住，往下读之前先别滚。
          </p>

          <p className="meta">
            这些字段由转换管线自动抽取（⑲）；这里填的值优先级更高 ——
            <strong>重新转换也不会被覆盖</strong>。
          </p>

          <button className="btn btn-primary" disabled={busy}>
            {busy ? '保存中…' : '保存'}
          </button>
        </form>
      </aside>
    </>
  )
}
