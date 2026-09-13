---
id: "b7e2a1c4"
event_at: "2026-08-31T21:40:00+08:00"
created_at: "2026-09-10T16:52:00+08:00"
updated_at: "2026-09-10T16:52:00+08:00"
type: reference
scope: project
status: active
---

# 宿主机上已跑通的 PDF → 中文 HTML 管线（papershelf 设计基线）

来源：`pku-paper-3d` 项目记忆 `html-chinese-version.md`（project scope）+ 会话 `s_260803_0946_bf50`。
宿主 2026-09-10 明确指向该会话，说明这条管线是 papershelf 转换模块的**现实起点**（而非从零设计）。

## 现有流程（人工/agent 驱动，一篇一确认）
1. `fitz`(PyMuPDF，见 `conda dl` 环境，含 PIL) 提取文本 → `原文.md`；`get_text("dict")` 取 image blocks bbox 提取图片
2. agent 读原文 + 精读笔记 → 写中文 Markdown（判断性内容用「（注：…）」标注）
3. 图片↔图号映射由**坐标交叉验证**确认（左栏 x≈54 / 右栏 x≈309，按 (y,x) 排序对齐图注）
4. 分块 `cat >>` 追加生成**单文件仿期刊 HTML**（MathJax CDN + `figures/` 相对路径图 + 锚点目录）
5. **宿主逐篇 `open` 浏览器人工验收 → 才 commit → 再做下一篇**

## 关键教训（都是踩过的坑，papershelf 必须内建这些校验）
- **图片完整性**：代理会静默截断；须比对 content-length + PIL 检测底部 25% 纯黑占比 >50% 判截断；补全用 `curl -sL -C -` 多轮续传
- **arXiv 优先官方 HTML 版**（`arxiv.org/html/<ID>v<N>`）：取原图质量高于 PDF 渲染，`<math alttext>` 含未损坏 LaTeX，参考文献从 `<li id="bib.bibNN">` 提取（需清理 `Cited by: §II` 残渣）
- **表格必须坐标级重建**：`get_text()` 会打乱行内单元格顺序，须用 line 级 `x0` 判定列归属
- **双栏图注**：`get_image_rects()` 的 bbox 不可靠（全从 y=58 起），须用 image blocks + 图注 block 的 (y,x) 联合排序
- 大 HTML 分块写，避免长输出被截断

## 现状与 papershelf 目标的差距（设计要点）
| 维度 | 现有流程 | 原型（文献台）暗示 |
|---|---|---|
| 自动化 | 一篇一个人工验收 | 应用内「转换队列」，批量、无人值守 |
| 输出形态 | 单栏仿期刊 HTML | **双栏保版式**（`span:full` 跨栏块、块保留原始顺序与位置） |
| 图/表 | 人工坐标核对 | 应由管线自动保证正确 |
| 校验 | 人眼 | 需内建（VLM 自检 / 待校对队列） |

→ papershelf 的本质是**把这条已验证的手工管线产品化**，而非另起炉灶。
