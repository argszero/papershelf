---
id: "9d5c1e73"
event_at: "2026-09-10T17:55:00+08:00"
created_at: "2026-09-10T17:35:00+08:00"
updated_at: "2026-09-10T17:55:00+08:00"
type: "task"
scope: "project"
status: "active"
---

# 设计问答进度表（单一事实来源）

papershelf 设计阶段采用**一次一问**（见 `collab-one-question-at-a-time.md`）。
本文件是问答进度的**唯一索引**，各决策细节见对应 memory；不要再把进度表散写进各决策文件。

## 已定（①–⑪）

| # | 问题 | 结果 | 落点 |
|---|---|---|---|
| ① | 部署形态 | 自托管多用户、用户完全隔离、无团队 | `deploy-selfhosted-multiuser.md` |
| ② | 自动化程度 | 全自动无人值守，正确性须内建 | `decision-auto-pipeline.md` |
| ③ | 转换引擎形态 | 沿用 s_260803_0946_bf50 已验证管线（fitz + LLM 整篇精译 + 仿期刊单栏 HTML） | `decision-engine-proven-pipeline.md` |
| ④ | 双语配对方式 | **标记穿透翻译**（英文 HTML 带 `data-b` → 整篇翻译保留标记 → zip 配对） | `decision-pairing-marker-passthrough.md` |
| ⑤ | 双向对比呈现 | **左右并排双栏**（滚动联动 + 按标记对齐），不做上下堆叠 | `decision-compare-side-by-side.md` |
| ⑥ | 「保版式」的确切含义 | **内容保真单栏流式**，不复刻 PDF 双栏；渲染=左英右中各一份单栏 HTML | `decision-content-fidelity-single-column.md` |
| ⑦ | 文献归属 | **计划私有**（导入入口在计划内，`pl.papers.push` 副本；跨计划重复翻译） | `decision-paper-ownership-plan-private.md` |
| ⑧ | LLM Key 归属 | **服务端统一 Key**（OpenAI 兼容，管理员配置；须内建成本护栏） | `decision-llm-server-side-key.md` |
| ⑨ | 技术栈 | **Python 单体（FastAPI+SQLite）+ 前端 SPA**；fitz 直达；前端框架待定 | `decision-tech-stack.md` |
| ⑩ | 分享形态 | **实时只读视图**（非快照；原型 URL 内嵌快照弃用）；场景=同学互督/导师汇报 | `decision-share-live-readonly.md` |
| ⑪ | 分享访问控制 | **持链接匿名只读** + 可撤销/可设有效期，**不做密码** | `decision-share-access-control.md` |

## ⏳ 当前进行中：⑫ v1 功能范围（原型导入抽屉的两个增强开关）

**已永久置为开、不再是选项**的两个转换设置：
- 保留公式为 LaTeX（决策③）
- 段落级中英对照输出（决策④⑤）

**仍在问的两个**：
- **术语表对齐（自定义名词译法）** —— 译名表注入 prompt；成本≈0，直接决定精读术语一致性
- **为图表生成中文说明** —— VLM 出图注；代价=引入第二个（多模态）模型 + 每图一次视觉调用 + 幻觉风险高于文本翻译

选项：**A** 都做 / **B** 只做术语表对齐（**助手推荐**）/ **C** 只做图表说明 / **D** 都不做（v1 纯翻译）。
→ 若答 A 或 C，需追问**图表说明的模型来源**（下一个问题）。

## 仍未定（跨问题遗留）
- **产物存储格式**：结构化 Doc JSON vs 单文件 HTML（段落选中 / 笔记锚点 / 三态切换都依赖它）
- **产物是两份 HTML（en/zh）还是后台合成一份双语 HTML**（取决于阅读器形态）
- **校验机制**：既然无人验收，如何保证图序/表序/公式/引用正确（决策④ 已内建标记与漏译校验）
- **转换失败/质量差的兜底**：能否导入外部已生成的中文版
- **前端框架选型**（决策⑨ 遗留）
- **版权风险闭环**：公开分享链接无鉴权分发论文中文全文 → README/文档须明确警示部署者（决策⑩⑪ 遗留）
- **成本护栏的具体形态**（决策⑧ 遗留）

## 相关
- 协作方式：`collab-one-question-at-a-time.md`
- 基线管线与踩坑：`existing-pdf-to-zh-pipeline.md`
- 原型拆解：`proto-literature-workbench.md`
