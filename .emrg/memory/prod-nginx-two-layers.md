---
id: 8f2c4a91
event_at: 2026-09-11T20:00:00
created_at: 2026-09-11T20:10:00
updated_at: 2026-09-11T20:10:00
type: reference
scope: project
status: active
---

# 生产 413 真凶：**两层 nginx**，外层的 `client_max_body_size` 改了也没用

宿主报「上传 14MB PDF → `请求失败 (413)`」（2026-09-11 19:58 +08:00）。

## 拓扑（关键：链路上有两个 nginx）

```
公网 → nginxproxy/nginx-proxy（外层，自动生成 vhost）
        → 172.23.0.3 = "website" 容器（内层 nginx:alpine）   ← 真凶在这
            → host.docker.internal:8083 → papershelf 容器（应用本身无 body 限制）
```

## 排错结论

| 层 | 位置 | 默认限制 | 改这个有用吗 |
| --- | --- | --- | --- |
| 应用 | FastAPI/uvicorn `papershelf` | **无限制**（直连 app 传 2MB → 401 而非 413） | — |
| 外层 | `nginxproxy/nginx-proxy` | 1MB | ❌ **无效**，它放行后内层照样 413 |
| 内层 | `website` 容器 `nginx:alpine` | **1MB** | ✅ **真修复点** |

**判定方法（一条命令定案）**：`docker logs website` 里出现
`client intended to send too large body: 14680272 bytes` → 拦截发生在内层，不是外层。

## 修复（只在生产服务器上，**仓库代码零改动**）

**内层**（真修复）：`/root/app/ali.args.fun/nginx/default.conf` 的 `server_name papershelf.args.fun` 块内加
```nginx
client_max_body_size 256m;
```
该文件 `bind-mount` 进 `website` 容器 → **改动持久，容器重建也不丢**。
备份：同目录 `.bak.20260911-2003`。`nginx -t` OK → `docker exec website nginx -s reload`。

**外层**（防御性，非必需）：`/var/lib/docker/volumes/aliargsfun_vhost/_data/papershelf.args.fun`（server 级被 include）写入 `client_max_body_size 256m;`。注意此目录是 nginx-proxy 卷，容器重建会被重新生成，**不可依赖**。

## 验证口径（时间线是铁证）

`docker logs website`（UTC，+08:00 换算）：
- `11:58:55` → 宿主那次 14MB 上传 **413**（19:58 本地）
- `12:02:24/12:02:37` → 我改前复现 **413**
- `12:03:29` 起 → 同一份 2MB / 15MB 请求变成 **401**（放行）
- `12:08:02` → **真 39MB PDF 上传 201 Created**

⚠️ **宿主截图可能是旧的**：宿主报障时贴的截图，修完再看仍是「请求失败 (413)」——必须用 `docker logs website` 的**时间戳**对齐，而不是以截图为准（截图无法自证时刻）。

## 顺带教训

- **抓 `GET /api/health` 的容器 ≠ 有可用代码的容器**（见 [[mailer-gmail-ip-rotation]] 同类"本地全绿生产现形"）。本次用 `docker compose exec` 直接跑库查询确认到 `blocks: 0`，才断定管线是 `failed` 而不是没跑。
- 数据库 `plans` 表**没有** `paper_count` 列（该字段是 API 动态算的）→ 写 SQL 前先 `pragma table_info`。
- 清库要连**磁盘产物**一起清：`data/papers/<ts>_<name>.pdf` + `data/papers/p<id>/assets/`，否则留孤儿目录。
