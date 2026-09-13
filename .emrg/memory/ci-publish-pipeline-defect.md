---
id: ci-publish-pipeline-defect
event_at: 2026-09-11T17:15:00
created_at: 2026-09-11T17:15:00
updated_at: 2026-09-11T17:15:00
type: decision
scope: project
status: active
---

# 发布流水线禁止「按版本删包」—— CI 全绿却静默删掉了刚发布的镜像

**事故日期 2026-09-11（提交 `828114e` 那次 CI）· 修复提交 `c0a7996`（workflow）+ `f5f8ecf`（文档）**

## 现象

宿主反映「线上还是旧页面」。排查发现**不是"镜像太旧"，是镜像根本不存在**：
GHCR 上 `latest` / `sha-828114e` / manifest digest **全部 404**，包页面显示
「Recent tagged image versions → **No tagged versions found**」，本机与部署机
`docker pull ghcr.io/argszero/papershelf:latest` → **not found**。
而那次 CI 是**全绿**的（`test/build×2/merge/smoke/cleanup` 全 success）。

## 时序证据（同一次 run `34578853109`，全部来自日志）

| 时刻 | 事件 |
|---|---|
| 08:24:29 | `merge` 推上 `latest` + `sha-828114e`，并 `imagetools inspect latest` **成功** |
| ~08:25 | `smoke` 拉 `latest` 起容器、断言 `/api/health` 与前端产物 —— **成功（此时镜像还在）** |
| 08:25:05 | `cleanup` 打印 **`Total versions deleted till now: 8`**（另有一条 `delete version API failed`） |
| 之后 | 所有 tag/manifest 404 |

→ **凶手唯一且确定，就是那个 cleanup job。**

## 根因：参数组合被写反

```yaml
uses: actions/delete-package-versions@v5
with:
  delete-only-untagged-versions: false      # ← 连「有 tag 的版本」也删
  ignore-versions: '^(latest|v?[0-9].*)$'   # ← 保护名单不含 sha-<commit> / sha256-<hex>
  min-versions-to-keep: 0                   # ← 一个都不留
```

`build` job 用 `push-by-digest=true` 推送，除了 `merge` 打的 `latest`/`sha-<短哈希>` 之外，
还有 buildx 自己产的 `sha256-<64hex>` 中间版本。本意是只删最后这类，但保护名单写成了
"只有 latest 和数字版本受保护"，而 `delete-only-untagged-versions: false` 又授权删有 tag 的版本。

## 修法（已落地）

1. **删掉该 cleanup job**，换成只读的 `verify` job（跑在流水线最后）：
   `imagetools inspect` 断言 `latest` 与 `sha-<短哈希>` 在**远端**仍可解析，并把 manifest 写进 step summary。
2. **加 `tests/test_workflow_guard.py`**（5 条离线回归）：发布流水线里不得出现删包类 action、
   `latest` 必须在默认分支推、`packages:write` 只给真正推镜像的 job、`smoke` 必须验 `latest`。

## 贯穿性教训

- **破坏性清理动作的失败模式是"静默删掉交付物"，CI 看不出来** —— 所以对这类步骤的正确反应
  不是"调参数"，而是**直接禁止**，并用断言钉住（清理 packages 页面只是个观感收益，不值得任何风险）。
- **断言要看"远端"而不是"本地刚推成功"**：`merge` 里那句 `imagetools inspect latest` 是在删之前跑的，
  所以它成功了也说明不了发布物还在。校验必须放在**所有会改远端状态的步骤之后**。
- 这次事故能被发现，是因为宿主去看了一眼线上页面；**如果没人看，它会一直静默存在**。

## 部署机拉取（顺带查清）

部署机 `/root/.docker/config.json` 只有阿里云凭据、**没有 GHCR 登录**，但这个包是**公开**的
（匿名 `ghcr.io/token` 会给 token；私有会返回 `DENIED`），所以 `docker compose pull` 匿名即可成功，
不需要 `docker login`。
