---
id: "c2f5a8d3"
event_at: "2026-09-10T17:54:00+08:00"
created_at: "2026-09-10T17:54:00+08:00"
updated_at: "2026-09-10T17:54:00+08:00"
type: "task"
scope: "session"
status: "superseded"
---

# 问题⑫ v1 是否做「术语表对齐 / 图表中文说明」—— ✅ 已结（B），本文件已失效

**提问时间**：2026-09-10（⑪ 归档后立即提问）。

> ⚠️ **已 superseded（2026-09-10）**：宿主已答 **B（只做术语表对齐，图表说明缓到 v2）**，结论已落档为项目记忆 `design-decisions.md` 决策⑫。本文件仅留提问时的推理备查，不再更新。

## 背景：原型四个转换设置，两个已永久置为开（不再是选项）
| 设置项 | 状态 | 落点 |
|---|---|---|
| 保留公式为 LaTeX | ✅ 总是开 | 决策③ `decision-engine-proven-pipeline.md` |
| 段落级中英对照输出 | ✅ 总是开 | 决策④⑤ `decision-pairing-marker-passthrough.md` / `decision-compare-side-by-side.md` |
| **术语表对齐（自定义名词译法）** | ❓ 本问题 | — |
| **为图表生成中文说明** | ❓ 本问题 | — |

## 两个待定项的性质对比（助手给出的判断）
- **术语表对齐**：用户维护译名表（如 `control barrier function → 控制障函数`），翻译时注入 prompt。
  **成本≈0（prompt 多一段）**，但直接决定精读一致性（CBF/DIW/闭环控制等领域术语几十篇读下来不各译各的）。
- **图表中文说明**：VLM 看图出中文图注/解读。对"看不懂图"价值高，但**要引入第二个（多模态）模型**、每图一次视觉调用、**幻觉风险高于文本翻译**（可能编造图中不存在的数字/趋势）。

## 选项（已给宿主）
- **A. 两个都做** —— 管线最完整
- **B. 只做术语表对齐**（**助手推荐**）—— 近零成本、直接服务精读一致性；图表说明留 v2，避免第二个模型与幻觉面
- **C. 只做图表说明** —— 术语一致性靠 agent 自觉
- **D. 都不做** —— v1 纯翻译，先跑通主干

## 后续依赖（若答 A 或 C）
需追问**图表说明的模型来源**（这是下一个问题，不与本题混问）。

## 相关
- 决策③：`decision-engine-proven-pipeline.md`｜决策④：`decision-pairing-marker-passthrough.md`｜决策⑤：`decision-compare-side-by-side.md`
- 进度单一事实来源：`.emrg/memory/design-qa-progress.md`
