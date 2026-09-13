---
id: metadata-gap-19-unimplemented
event_at: 2026-09-12T14:20:00
created_at: 2026-09-12T14:25:00
updated_at: 2026-09-12T15:05:00
type: reference
scope: project
status: active
---

# 「所有文章都没有名字和作者」—— ⑲ 元数据自动抽取从未实现

**宿主报告 2026-09-12**：生产文献库 12 篇全部显示标题 `original`、副标题「未提取到作者」、
发表/标签/年份全 `—`。

> **✅ 已修（2026-09-12）**：宿主选 C（手动 + 自动都做）→ `pipeline/metadata.py` 抽取器
> + 元数据编辑抽屉 + `papers.title_is_placeholder` 列 + `parse._finalize` 保留候选标题。
> 决策落在 **㉘**、实现说明在 `docs/design.md` §5.6b。**生产 12 篇待回填（只抽元数据，
> ≈1.8 万 tokens；不要重跑转换 —— 那要 185 万）**。
> 本文件保留为**事故记录**：下面五条病因是真事实，"修复中又踩的第二个坑"（§最后）
> 是比原缺陷更值钱的教训。

## 五个真缺陷叠加（都不是"抽取失败"，是从没实现）

### ① 上传时写入的是**文件名**，而且这个值再也改不掉

`routers/papers.py` 上传路由拿 `Path(name).stem` 当标题 —— 宿主把 12 个文件都命名成
`original.pdf`，于是 12 篇全叫 `original`。

更要命的是转换完成后的写回（`converter.py`）：

```sql
title = COALESCE(NULLIF(title, ''), ?)
```

`NULLIF('original','')` = `'original'`（非空）→ `COALESCE` 取**旧值** → 抽到的真标题
**永远写不进去**。这个 COALESCE 的意图是"别覆盖用户手填的标题"，但它无法区分
"用户填的" 和 "上传时的占位符"。

### ② ⑲「LLM 自动抽取元数据」**一行都没实现**

全仓库搜索：写 `authors`/`venue`/`year`/`journal` 的地方**只有 `converter.py` 那一处
读 `meta.get(...)`**，而没有任何地方**往 meta 里写**这些键；`pipeline/` 里所有
`doc.meta[...]` 赋值只有 `title_en` / `dropped_images` / `refs_*`。

`parse.py` 也**从不解析作者**（无任何 authors 相关代码）。
⇒ `meta.get("authors")` 恒为 `None` → 写回空串 → 界面恒「未提取到作者」。

### ③ `title_zh`（中文标题）同样从没人写

决策 ㉓ 定了公式 LaTeX 化、翻译管线译正文，但 `title_zh` **没有任何赋值路径**
（只有读：`markup.py` 页眉、`routers/papers.py` 导出、`synth.py`）。
⇒ 阅读器标题永远走 `paper.title`（= 文件名），中英对照标题不存在。

### ④ `PATCH /papers/{paper_id}` **有后端、无前端入口**

后端 `patch_paper` 支持改 `title`/`authors`/`venue`/`year`/`tags`（⑲ 的「可手动改」），
但前端**没有任何 UI 调用它**（`web/src` 里搜不到对它的调用）。
⇒ 用户既看不到自动结果，也没法手填 —— 表格那几列是**死列**。

## 附带发现：`title_en` 本身也有提取错误

生产 12 篇的 `docs.meta.title_en` 实测：

| paper | title_en | 判定 |
|---|---|---|
| 2 | Process monitoring and machine learning for defect detection in laser-based metal AM | ✅ 真标题 |
| 4/5/9/11/12 | 真标题 | ✅ |
| **1** | Journal Manufacturing Processes | ❌ 期刊名（页眉） |
| **3** | Precision Engineering | ❌ 期刊名（页眉） |
| **6** | （空） | ❌ |
| **7** | npj \| advanced manufacturing Article | ❌ 期刊名 + "Article" |
| 8/10 | arXiv:2603.19455v1 [eess.SY] 19 Mar 2026 | ⚠️ 是 arXiv 号（预印本，可接受但难看） |

⇒ `_finalize` 取"第一个 h1"当标题，在**页眉被识别成 h1** 时就取到期刊名。
`_running_headers` 只处理重复出现的页眉，首现的没兜住。

## 附带发现 2：`relTime` 的"天"是 UTC 不是本地

`web/src/vocab.ts` 的 `DAY = 86400000`，`Math.round((Date.now() - new Date(iso)) / DAY)`。
`created_at` 存的是 UTC（`2026-09-11T14:35Z` = 北京时间 22:35），而 `Date.now()` 是本地时刻；
跨过 UTC 零点后 `d` 就变成 1 → 显示「昨天」，但**对北京时间用户而言其实是今天**。
（当晚一次导入 12 篇，过了 08:00 UTC 就集体显示"昨天"。）

## 附带发现 3：`tags` 同样恒为空

`tags` 只在上传 `Form("")`（前端不传）或 `PATCH /papers/{id}`（无 UI）时写入
⇒ 生产 12 篇全 `[]`。⑲ 的「LLM 自动生成 3–5 个标签」也未实现。

## 与决策的关系（⑲ 已定，是**漏做**不是待定）

⑲ 原文：「元数据（标题/作者/会议/年份）：由 LLM **自动抽取**（解析首块/摘要），导入即填好」
+「标签：LLM 自动生成 3–5 个」+ 标题「…**可手动改**」。
三条**全都没落地**。生产实测：`tags` 12 篇全 `[]`。

## 当前 UI 上全部表现为死列

| 列 | 界面显示 | 原因 |
|---|---|---|
| 标题 | `original`（12 篇全一样） | ① 文件名 + COALESCE 锁死 |
| 作者（副标题） | 未提取到作者 | ② 抽取未实现 |
| 发表 / 标签 | `—` | ②⑲ 未实现；`year` 恒 NULL |
| 中文版标题 | 走 `paper.title` 即文件名 | ③ `title_zh` 从无赋值 |
| 元数据手改入口 | **不存在** | ④ 后端有 PATCH、前端无 UI |

## 教训

**「有写回代码」不等于「有数据来源」**：`converter.py` 那几行 `meta.get(...)` 看起来
像已实现（有 SQL、有 COALESCE、有注释"决策⑲"），但**上游从来没人往 meta 里写**。
排查方法是**从赋值端反向搜**（`grep -rn "meta\[" pipeline/`）而不是看消费端。

## ⚠️ 修复时踩的第二个坑（比原缺陷更值钱）

修 ① 时第一版判据是「`title == Path(pdf_path).stem`」—— 看着特别优雅：
用户一改标题就不相等，**自动获得保护、连迁移都不用写**。写完自测也"通过"了
（单测里我喂的 `pdf_path` 是干净的 `a.pdf`）。

**拿生产真实行一跑，12 篇全部判成"非占位"**：
`pdf_path` 落盘时被加了时间戳前缀（`2026-09-11T143352+0000_original.pdf`），
`stem` = `2026-09-11T143352+0000_original` ≠ `title='original'`。
⇒ "修好"后真标题**仍然写不进去**，只是从"必错"变成"**看起来对**"。

**两条通用教训**：
1. **占位符是一种意图，不是一种值。** 凡是需要"当时那个名字/状态"的信息，
   事后从**被改过的**字段里推不出来 —— 只能落列（`title_is_placeholder`）。
   "不用迁移"这种便利承诺，通常是判据找错了的信号。
2. **单测喂的输入要来自生产真实形状，不能自己编个干净的。** 我这里编的
   `pdf_path='/x/a.pdf'` 恰好绕过了唯一的坑；用 `prod.db` 的真行一跑就现形。
   （附带：`source_ref` 才是**原始文件名** —— 兜底判据先比它，生产 12 篇靠它全认回。）

