---
id: "85b5851a"
event_at: "2026-09-10T08:48:05Z"
created_at: "2026-09-10T08:48:05Z"
updated_at: "2026-09-10T08:48:05Z"
type: "project"
scope: "project"
status: "active"
---

# papershelf 原型：文献台 PaperDesk（literature-workbench.html）

`papershelf` 是开源项目，UI/功能原型为单文件 HTML：`/Users/argszero/Downloads/t1/literature-workbench.html`（约 2489 行，纯前端 + localStorage，无后端）。

## 核心闭环
PDF 导入 → AI 转成**保版式中文 HTML** → 双语精读 → 笔记 → 进度追踪。

## 模块
| 模块 | 内容 |
|---|---|
| 认证 | 登录/注册（分步表单、密码强度）；localStorage 存账号，session/localStorage 存会话（含“记住我”） |
| 总览 | 指标卡、状态甜甜圈、阅读节奏柱状图（7天/30天/12周）、待办提醒、最近文献 |
| 文献库 | 表格：标题/作者/会议/年份/标签/阅读态/转换态/进度，筛选 chips + 排序 |
| 阅读器 | 保版式双栏 `doc-stage`，三态切换（中英对照/仅中文/仅原文），右栏笔记+大纲，字号调节，段落可选可挂笔记 |
| 进度看板 | 四列拖拽：待读 → 在读 → 已读 → 已整理 |
| 阅读计划 | 一等公民：每个计划自有文献集、目标篇数、进度、笔记、转换队列（`queue`）；支持只读快照分享链接 |

## 数据模型（原型实现）
- `Plan{ id, name, goal, desc, papers[], status{}, progress{}, notes{}, pace, activeId, queue }`
- `Paper{ id, title, authors, venue, year, tags[], st(4态), cv(4态: none/待转换/转换中/已生成/失败), pr(0-100), upd }`
- `Doc{ pageList[{ no, blocks[{ k:h|p, lvl, en, zh, sec }] }] }` ← 保版式的灵魂：块保留 PDF 原始顺序与位置，`span:full` 表示跨栏图表
- 持久化：单 localStorage key（view / readerMode / fs / rail / activePlanId / plans）；分享态 `SHARE.active` 时禁止写入
- 分享：URL 内嵌快照（只读）

## 关键实现细节（对后续设计有约束）
- **文献归属**：往计划里加文献时是**复制**一份 `Paper` 进 `pl.papers`（见约 L2150 `pl.papers.push({...})`），不是引用，因此原型中同一篇文献在不同计划里是独立副本。
- 计划级队列 `queue` 是“转换队列”，与阅读进度 `pace` 并列，属于计划而非全局。

## 待定/风险点（设计阶段需澄清）
① PDF→保版式中文 HTML 的管线；② 计划的资源归属（文献是否跨计划共享）；③ 匿名分享快照的存储。
