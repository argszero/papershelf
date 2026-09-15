---
id: localdev-node-asdf
event_at: 2026-09-15T10:05:00
created_at: 2026-09-15T10:05:00
updated_at: 2026-09-15T10:05:00
type: reference
scope: project
status: active
---

# 本机开发环境的两处坑（2026-09-15 实测）

## node 在 asdf 下，**不在 PATH**

```
~/.asdf/installs/nodejs/26.5.0/bin/{node,npm,npx}
```

`PATH=/Users/argszero/.emrg/install/bin:/usr/bin:/bin:...` 里**没有 node**，
`/opt/homebrew/Cellar` 里也没有 node（brew 装过、现在没有了）。
症状：`cd web && ./node_modules/.bin/tsc -b` → `env: node: No such file or directory`；
`python -m tsc` → `No module named tsc`（那是**假线索**，别顺着它排查）。

正确姿势：

```sh
cd web && export PATH="$HOME/.asdf/installs/nodejs/26.5.0/bin:$PATH" && npx tsc -b && npx vite build
```

构建产物落 `src/papershelf/static/`（`vite.config.ts` 的 `outDir: '../src/papershelf/static'`，
**直接进 Python 包**，由 FastAPI 同进程托管）。⚠️ `emptyOutDir: true` → 旧 bundle 会被删，
**浏览器缓存着的旧 `index.html` 会指向已 404 的旧 JS**（表现为「改完刷新还是老界面」）。
必须 `cdp("Page.reload", ignoreCache=True)` 硬刷，再另起一次调用导航（导航后返回串会丢）。

## macOS 上没有 `setsid`，也没有 `timeout`

本地起服务的标准姿势（browser-harness 的 30s 上限会掐掉前台启动）：

```sh
cd /Users/argszero/scm/github.com/argszero/papershelf
(nohup sh scripts/dev.sh > /tmp/pslocal/dev.out 2>&1 &)   # 8012，数据 /tmp/pslocal/data
curl -s -o /dev/null -w "%{http_code}\n" localhost:8012/health
```

改**后端**代码要重启（`ps aux | grep "papershelf.cli serve"` → `kill <pid>`）；
只改**前端**则只需 rebuild + 硬刷（静态文件每请求读盘）。

⚠️ 重启前先确认 PID 是 `papershelf.cli serve --port 8012` —— **别误杀 emrg server**。
