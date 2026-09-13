---
id: prod-config-state
event_at: 2026-09-11T18:30:00
created_at: 2026-09-11T17:25:00
updated_at: 2026-09-11T18:30:00
type: task
scope: session
status: active
---

# 生产 papershelf 配置现状（2026-09-11 18:30，已跑上验证码版）

**`papershelf.args.fun`（39.105.53.16）· 容器 `papershelf` · `/root/app/papershelf`（无 git checkout）**

```bash
ssh argszero        # 目录只有 .env / docker-compose.yml / data/ + .env.bak.*
```

## `.env` 最终值（全部宿主给值或已加固）

| 键 | 值 |
|---|---|
| `PAPERSHELF_SECRET` | 32 字节随机（原占位 `change-me-…`） |
| `PAPERSHELF_BASE_URL` | `https://papershelf.args.fun`（原 `http://localhost:8000` → 会破坏分享链接 + 放宽 CORS） |
| `PAPERSHELF_ADMIN_EMAIL` | `argszero.reg@gmail.com`（唯一管理员；旧占位账号已删） |
| `PAPERSHELF_ADMIN_PASSWORD` | 宿主给（`ensure_admin` 每次启动重置该邮箱口令 → 必须写进 `.env`） |
| `PAPERSHELF_LLM_BASE_URL` | `https://aitokenpool.args.fun/v1` |
| `PAPERSHELF_LLM_API_KEY` | `atk_live_…445` |
| `PAPERSHELF_LLM_MODEL` | `deepseek-v4-flash-vision-exp` |
| `PAPERSHELF_SMTP_HOST` | `smtp.gmail.com` |
| `PAPERSHELF_SMTP_PORT` | `465`（走 `SMTP_SSL` 分支） |
| `PAPERSHELF_SMTP_USER` / `_PASS` / `_FROM` | `4tempuse@gmail.com` / Gmail app password / 同邮箱 |
| `PAPERSHELF_SMTP_TLS` | `true` |
| `PAPERSHELF_EMAIL_DOMAIN_ALLOWLIST` | `edu.cn`（未改） |

**镜像**：`ghcr.io/argszero/papershelf:latest` = `sha-1de2277`，含 **M5 + 分页 + 验证码认证 + 发信重试**
（前端 bundle `index-U1cJuCKf.js` / CSS `index-BSRYqHhl.css`）。

## 生产库现状（验收后已清理干净）

| id | email | 角色 |
|---|---|---|
| 2 | `argszero.reg@gmail.com` | 管理员 |
| 3 | `aqshao25@stu.pku.edu.cn` | 普通用户 |

`plans: 0 / papers: 0 / verification_codes: 0`。
验收期间建的 `probe-verify@pku.edu.cn`、`probe-register@pku.edu.cn` **已连同其 sessions 删除**。

## 实测（每项都验过）

- `/api/health` → `{"ok":true,"open_registration":true,"llm_configured":true}`
- `/api/auth/config` → `{"open_registration":true,"smtp_configured":true,"allowed_domains":["edu.cn"]}`
- 非 `edu.cn` 邮箱实测 `400 仅接受以下域名的邮箱：edu.cn`（注册口通、白名单仍在拦）
- **验证码全流程（生产实测）**：
  - 找回：旧密码 200 → 错码 400 → **正确码重置 200** → **码重用 400（一次性）** → 新密码 200 → 旧密码 401
  - 注册：非 edu.cn 400 → 发码 200 → **同邮箱 429 冷却** → 错码 400 → 正确码注册 200 **并直接带 cookie（注册即登录）** → 重复注册 409
  - 防探测：未注册邮箱走 `/reset/code` 也返回 `{sent:true}`（**不泄露邮箱是否存在**）
- **发信可靠性**：连发 12 封 **12/12 成功、最慢 9.1s**（修重试前实测 37.5% 失败，见 `mailer-gmail-ip-rotation`）
- 部署机**无需登录 GHCR**（包公开，匿名 token 可取）

## ⚠️ 邮件只能「发」不能「收」

`4tempuse@gmail.com` 是**发件人**，收件人是注册者自己的 edu.cn 邮箱。
所以验证码邮件**不会回到这个 Gmail**（除非收件地址正好是它，但白名单只放 edu.cn）。
验收时用 `4tempuse+psfix@gmail.com` 想当收件人是**错的**，`probe-verify@pku.edu.cn`
这种不存在的校区邮箱在 Gmail 里既收不到信也**没有退信（没有 backscatter）**。

**后果**：无法端到端"从收件箱取码"验证注册。已做的替代验证是
① 验收生产站点上的 UI 与接口语义；② 用 app 自己的 `issue_code()` 取一个**真实签发**的码走完 HTTP 流程。
若要真·端到端，需要宿主的 edu.cn 邮箱（可用 `+` 别名：`realaddr+ps1@pku.edu.cn`）。

## 宿主给的字段里被忽略的（papershelf 无对应项）

`smtp_host/port/user/password/from` 已映射。`from_name` / `verify_subject` 宿主 2026-09-11 指示
**从给的信息里只取需要的，其余忽略**（不必再提）。注：改成验证码后 `from_name` 其实已**支持**可配
（`PAPERSHELF_SMTP_FROM_NAME`），但线上为空、用默认品牌名。

## 待办（可选，宿主未指示）

1. `mailer.send_share_notice` 是**死代码**（全仓库无调用方）→ 分享只能手动复制链接。
   （465 `SMTP_SSL` 分支**已在 mailer 重写中补上**，不再是隐患。）
