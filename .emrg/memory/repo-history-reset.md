---
id: a7f3c21b
event_at: 2026-09-13T19:50:00
created_at: 2026-09-13T19:52:00
updated_at: 2026-09-13T19:52:00
type: decision
scope: project
status: active
---

# 仓库历史重置：删库重建 + 单次初始化提交（2026-09-13）

**宿主指示**：「把 github repository 删除后，重新提交代码。作为初始化提交，不需要之前的提交历史。」
（问过删除路径，宿主选 **A：助手代删** → 加了 `delete_repo` 权限后由我执行。）

## 做了什么

1. **删仓库前盘点**：`argszero/papershelf` 公有、49 提交、6.7 MB；
   **没有** release / secret / variable / environment / webhook / deploy key / issue / PR
   → 删除只丢「提交历史 + Actions 运行记录」。旧历史先备份成本地 bundle
   （`tmp/papershelf-pre-wipe.bundle`，`git bundle create --all`，5.6 MB）。
2. `gh repo delete argszero/papershelf --yes` → `gh repo create argszero/papershelf --public`
   （同名同可见性，描述沿用一句项目简介）。
3. 本地 `rm -rf .git && git init -b main` → `git add -A` → **一次提交**
   （155 个文件，与重置前的工作树完全一致，含 `.emrg/` 项目记忆与会话历史）
   → `git push -u origin main`。
   > 本文**不钉这个提交的 SHA**：`.emrg/` 记忆本身就在仓库里，写记忆 → 就得再提交 →
   > SHA 立刻作废（本次已因此 amend 过一次）。要确认真身就用 commit message
   > 「初始化：papershelf —— 自托管文献精读工作台」。
4. 镜像包与生产：见下。

**结果**：远端只剩一个提交（`git log` 的第一条，即上面那个初始化提交）；本地同样只有它。
⚠️ 记忆与文档里引用的旧 SHA（`6f487c4` / `350f33f` / `52b142e` …）**远端已不存在**，
只留在那个 bundle 备份里。

## 关键发现 ①：删仓库**不删** GHCR 包，但孤儿包**推不进去**

- 包 `ghcr.io/argszero/papershelf` 是**账号级**的（不是仓库的附属物）：仓库删掉后，
  匿名 token 拉 `manifests/latest` 仍 **200**，生产容器照常跑（healthy）——
  所以"删仓库会不会把生产镜像搞没"这个担心不成立。
- **但下一次 CI 直接红**：`test` 绿，两个 `build` 都挂
  ```
  ERROR: failed to push ghcr.io/argszero/papershelf: denied: permission_denied: write_package
  ```
  原因：包的 **linked repository 指向已被删除的仓库**，新仓库的 `GITHUB_TOKEN` 没有写权限
  （用户级包 → 仓库的写权限靠"包与仓库的链接"来授予，链接断了就拒绝）。
- **修法（已验证）**：把**整个包删掉**再重跑流水线 —— `DELETE /user/packages/container/papershelf`
  （需 `delete:packages` 权限，30 个版本一起没）→ CI 重跑 → 同名包**自动重建并链接到新仓库**
  （实测 API 返回 `repository: argszero/papershelf`）。
  窗口期：删包到重推成功之间，生产 `docker pull` 会失败（**已在跑的容器不受影响**）。
- 删包是**不可逆**的（版本无法找回）；本次所有版本都是可从代码重建的镜像，故可接受。

## 关键发现 ②：`gh auth refresh` 的轮询窗口太短，别用它等宿主

- 宿主完成网页授权后，`gh auth refresh` 仍打印 `context deadline exceeded`
  （device flow 的 token 交换走 `github.com`，它内部轮询有短 deadline）→
  **验证码其实还有效，但已经白等两次**。
- 改用**自己跑 device flow**（`tmp/gh_device_flow.py`，用后即删）：curl 取 `device_code`
  → urllib 轮询 `oauth/access_token` 直到 `expires_in`（~15 分钟）用满；
  token 落临时文件供 curl/API 用，**用完立刻删**。
- 网络实况：本机 **`github.com` 直连被墙**（`login/device/code` / `oauth/access_token` 都 timeout），
  **`api.github.com` 直连可用**；本机 git 全局代理是 `http://172.16.0.40:6501`
  → `gh` / `curl` 走 GitHub 网页端点时必须显式 `HTTPS_PROXY=...`。
- ⚠️ 副产品风险：为完成本次操作，gh 的 token 现在多了 `delete_repo` + `delete:packages` 权限，
  要不要收回由宿主决定（收回同样需要走一次 device flow）。

## 验收（全部实测）

| 项 | 结果 |
| --- | --- |
| CI | 六 job 全绿（`test` / `build`×2 / `merge` / `smoke` / `verify`） |
| 远端标签 | `latest` **200**、`sha-<该提交短哈希>` **200**（匿名 token 拉 manifest） |
| 包归属 | `visibility: public`、`repository: argszero/papershelf` |
| 生产 | `docker compose pull && up -d` → `docker inspect` 的 `revision` = **该提交的完整 SHA**、容器 **healthy**、`/api/health` **200**、bundle 指纹仍是 `index-5320WwVc.js`（内容未变） |

## 可复用的操作顺序（下次照做）

1. 盘点附属物（release/secret/权限/issue）→ 本地存一份 bundle 备份；
2. 删仓库（缺 `delete_repo` 就先补，见发现②）→ 重建同名同性；
3. **先删 GHCR 包**（否则第 4 步必 `write_package` 拒绝）；
4. 本地 `rm -rf .git && git init && add -A && commit` → push；
5. 盯 CI 六 job → 验远端 `latest` + `sha-<新 commit>` 可解析 → 包 `repository` 指回新仓库；
6. 生产 `pull` + `up -d` → 验 `revision` 标签、healthy、bundle 指纹、health 200。
