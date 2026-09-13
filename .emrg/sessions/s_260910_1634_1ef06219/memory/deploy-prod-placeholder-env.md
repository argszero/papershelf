---
id: deploy-prod-placeholder-env
event_at: 2026-09-11T17:00:00
created_at: 2026-09-11T16:25:00
updated_at: 2026-09-11T17:25:00
type: task
scope: session
status: merged
---

# 已并入 `prod-config-state.md`（+ 项目记忆 `ci-publish-pipeline-defect.md`）

本碎片记录的是「生产 `.env` 在用示例占位值」这件事的**发现与拆除过程**。
2026-09-11 17:25 全部结清，**现行配置以 [prod-config-state.md](prod-config-state.md) 为准**。

## 拆除过程留档（四处占位值）

| 占位值 | 风险 | 处置 |
|---|---|---|
| `PAPERSHELF_ADMIN_EMAIL=you@your.edu.cn` + `PASSWORD=at-least-8-chars` | **实测可登管理员**（`is_admin:true`）→ 能烧 LLM 额度 | 换成宿主给的邮箱/口令；⚠️ `ensure_admin` 换邮箱是**新建**、不降级旧的 → **必须手动删**旧账号（已删，实测 401） |
| `PAPERSHELF_LLM_BASE_URL=https://api.siliconflow.cn/v1` 等三行 | 看着"没配 LLM"，其实"配了个假的" → `llm_configured:false` | 换成宿主给的 atk 配置 |
| `PAPERSHELF_BASE_URL=http://localhost:8000` | **真破坏功能**：分享按钮把后端返回的绝对 URL 原样复制 → 复制出 `http://localhost:8000/share/<token>`；且让 CORS 放宽成 `allow_origins=["*"]` | 换成公网域名，实测返回公网链接 + CORS 收紧 |
| `PAPERSHELF_SECRET=change-me-…` | 实际影响小：**只用于启动告警判断**，不参与会话签名（会话是随机 token 存库） | 换成 32 字节随机（消除安全观感问题） |

## 一条重要提醒（留档）

`security.ensure_admin` **每次启动都无条件重置** `PAPERSHELF_ADMIN_EMAIL` 那个邮箱的口令哈希
（`UPDATE users SET is_admin=1, status='active', password_hash=? WHERE id=?`）——
**只改 SQL 重设口令、重启就会被打回去**，必须同时改 `.env`。
