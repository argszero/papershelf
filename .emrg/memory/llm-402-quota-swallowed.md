---
id: 6b1e9d47
event_at: 2026-09-11T21:00:00
created_at: 2026-09-11T21:40:00
updated_at: 2026-09-11T21:40:00
type: reference
scope: project
status: active
---

# 转换「一半没译文」真凶：LLM 池子余额为负 → 402，翻译器静默吞掉

宿主 2026-09-11 上传真论文（911 块解析），转换重试后只有 **154 块**有译文、
391 块被标「待校对」。**不是管线 bug，是上游欠费。**

## 证据链

- `docker compose exec app python` 直接调一次 `Translator._chat()` →
  `httpx.HTTPStatusError: 402 Payment Required`
- `curl -H "Authorization: Bearer <key>" https://aitokenpool.args.fun/api/wallet` →
  `{"available": -0.02, "gift_balance": 0.0, "month_use": 1.02}`
- aitokenpool 侧 `src/gateway.rs::forward`：`if balance <= 0.0 { 402 "点数余额不足" }`
  （**预检**，上游调用前就拦）
- 生产 token 库 `transactions`：`topup 5000` 只出现过一次，`consume` 累计 131 笔

## 两个要记住的点

1. **`translator.translate_blocks` 把 `_chat` 的异常整段吞掉**：
   `except Exception as exc: log(f"! 调用失败：{exc}"); continue`
   —— 设计意图是「网络抖动跳过、交后续重译」，但**配额型错误（402/401）不会自愈**，
   于是变成：每个切片都失败 → 全篇无译文 → 出口标满「待校对」→
   用户看到「转换成功但全是待校对」，**完全看不出是欠费**。
   排查时最省事的判据：**`tokens_used` 是否明显低于块数量级**（本次只有 154 块有译文
   却烧了 110k tokens，说明只跑了前 1/4 就断了）。
2. **改 renderer / 改管线时，别把「结构性失败」与「上游欠费」混为一谈** ——
   前者该重试，后者该立刻点名欠费。是否对 402/401 做硬失败可在 README 里写清。

## 环境事实（复用时别再找）

- 生产 LLM：`https://aitokenpool.args.fun/v1`，model `deepseek-v4-flash-vision-exp`
- 查余额（key 自带身份，无需登录）：`GET /api/wallet`
- 生产 token 库在容器 `aitokenpool` 的 `/data/aitokenpool.db`，`docker cp` 出来用宿主
  `sqlite3` 查（容器里**没有** python3 / sqlite3 命令行）
