# 部署 papershelf

一句话：**一个容器 + 一个数据卷 + 一组环境变量**。没有 Redis、没有 Celery、没有独立前端服务 ——
API、转换 worker、SPA 静态产物都在同一个进程里（决策⑨）。

- 镜像由 GitHub Actions 构建推送到 GHCR（`.github/workflows/docker.yml`），**部署机不需要 Node/Python 工具链**
- 数据全在 `/data` 一个卷里：`papershelf.db`（SQLite）+ `papers/p<N>/`（原始 PDF、抽出的图片）
- 配置**全部**来自环境变量（决策㉑：不写进数据库、没有在线配置页）

---

## 1. 最小可用（3 分钟）

```bash
git clone https://github.com/argszero/papershelf.git && cd papershelf
cp .env.example .env

# 必填 ①：会话签名密钥（别用示例里的 change-me）
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# 把它填进 .env 的 PAPERSHELF_SECRET

# 必填 ②：首次引导管理员（邮箱 + 密码必须成对，不允许静默默认密码）
#   PAPERSHELF_ADMIN_EMAIL=you@your.edu.cn
#   PAPERSHELF_ADMIN_PASSWORD=<至少 8 位>

# 强烈建议 ③：LLM（不配也能起服务，但导入的文献无法转换）
#   PAPERSHELF_LLM_BASE_URL=https://api.siliconflow.cn/v1
#   PAPERSHELF_LLM_API_KEY=sk-...
#   PAPERSHELF_LLM_MODEL=deepseek-ai/DeepSeek-V3

docker compose up -d
docker compose logs -f          # 看启动日志
open http://localhost:8000      # 用 ADMIN_EMAIL / ADMIN_PASSWORD 登录
```

用 `docker run` 等价写法：

```bash
docker run -d --name papershelf --restart unless-stopped \
  -p 8000:8000 -v papershelf-data:/data --env-file .env \
  ghcr.io/argszero/papershelf:latest
```

> `docker compose`（v2 插件）与 `docker-compose`（v1 / 独立安装）都能用这份 `docker-compose.yml`。

## 2. 它启动时会拦住你的几件事

入口脚本（`docker/entrypoint.sh`）**故意不静默降级** —— 配置错了就死在启动时，
而不是带着不安全的默认值跑几天：

| 检查 | 行为 |
|---|---|
| `PAPERSHELF_SECRET` 未设 / 太短 | ✖ 退出并给出生成命令（开发默认值意味着任何人都能伪造登录态） |
| 给了 `PAPERSHELF_ADMIN_EMAIL` 却没给 `_PASSWORD` | ✖ 退出（§7：不允许静默默认密码） |
| `/data` 不可写 | ✖ 退出并提示 `chown 10001:10001`（容器以 uid 10001 非 root 运行） |
| 未配 LLM | ⚠ 警告，**照常启动**（仍可登录、建计划、开号、读已有文献，只是不能转换） |

## 3. 环境变量

完整清单与注释见 [`.env.example`](../.env.example)。分组速查：

| 变量 | 作用 | 默认 |
|---|---|---|
| `PAPERSHELF_SECRET` | 会话签名密钥 | 无（**必填**） |
| `PAPERSHELF_DATA_DIR` | 数据目录（容器内固定 `/data`） | `./data` |
| `PAPERSHELF_BASE_URL` | 拼激活/分享绝对链接；反代后填真实域名 | `http://localhost:8000` |
| `PAPERSHELF_ADMIN_EMAIL` / `_PASSWORD` | 首次引导管理员 | 空 |
| `PAPERSHELF_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | LLM（OpenAI 兼容协议） | 空 |
| `PAPERSHELF_MAX_CONCURRENCY` | 同时转换的文献数 | `2` |
| `PAPERSHELF_TOKEN_BUDGET` / `_MAX_ATTEMPTS` | 单篇 token 预算 / 最大尝试次数 | `400000` / `3` |
| `PAPERSHELF_EMAIL_DOMAIN_ALLOWLIST` | 注册白名单（逗号分隔，含子域） | `edu.cn` |
| `PAPERSHELF_SMTP_HOST` + `_FROM`（+`_PORT/_USER/_PASS/_TLS`） | **配全 → 开放自助注册（邮件激活）；未配 → 自动关闭注册，改由管理员开号** | 空 |
| `PAPERSHELF_SHARE_HOURS` | 分享默认有效期（小时，**硬上限 24**，填更大也只按 24 算） | `24` |

### 没有邮件服务怎么办

SMTP 是**可选**的（决策⑮）。不配 SMTP 时自助注册自动关闭，
管理员登录后在 `/admin` 手动开号即可 —— 功能完整，只是少了自助注册。

### 忘了管理员密码

```bash
docker compose exec papershelf papershelf create-admin you@your.edu.cn
```

（交互式输入新密码；这条路径不依赖环境变量，不必重启容器。）

## 4. 反向代理 + HTTPS

分享链接是**免登录**的，公网实例请务必套 HTTPS（否则会话 cookie 与分享链接等于裸奔）。
`PAPERSHELF_BASE_URL` **必须填外部真实地址** —— 它是**服务端**用来拼绝对 URL 的，
页面上的「分享」按钮把后端返回的 `url` **原样复制到剪贴板**，所以填了默认值时
复制出来的是 `http://localhost:8000/share/<token>`（对任何人都打不开）；
激活邮件里的链接同理。
（顺带：它以 `http://localhost` 开头时后端会把 CORS 放宽成 `allow_origins=["*"]`。）

```nginx
server {
    listen 443 ssl http2;
    server_name papershelf.example.com;

    # 上传的是 PDF，别用默认的 1M
    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;   # 转换是后台任务，但导出/分享页可能等一会儿
    }
}
```

Caddy 更省事（自动签发证书）：

```
papershelf.example.com {
    request_body { max_size 100MB }
    reverse_proxy 127.0.0.1:8000
}
```

> **链路上不止一层代理时（很容易踩）**：`client_max_body_size`（Caddy 是
> `request_body`）**每一层都得放开**，只要有一层是默认值 1M 就会 413。
> 典型场景是「自动反代容器（如 nginx-proxy / Traefik）→ 你自己的 nginx → papershelf」，
> 只改了外面那台，请求照样被里面那台拦掉。
>
> **定案方法**：看**内层**那台的错误日志，会直接写
> `client intended to send too large body: 14680272 bytes` —— 文件名和字节数都在，
> 一眼就能确认拦截发生在哪一层（应用自身不做体积限制，放行后返回的是 401/400/201，不是 413）。
> 顺带一提：把配置 bind-mount 进容器（而不是 `docker cp`）才能让改动在容器重建后仍然生效。

## 5. 备份与恢复

备份 = 备份那个卷。**先停容器**再拷，避免拿到写了一半的 SQLite：

```bash
docker compose stop
docker run --rm -v papershelf-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/papershelf-$(date +%F).tar.gz -C /data .
docker compose start
```

恢复：

```bash
docker compose stop
docker run --rm -v papershelf-data:/data -v "$PWD":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/papershelf-2026-09-10.tar.gz -C /data'
docker compose start
```

（更稳的做法是 `sqlite3 /data/papershelf.db ".backup"`，热备不停机；WAL 模式下可行。）

## 6. 升级

```bash
docker compose pull && docker compose up -d      # 数据卷不动，DB 迁移在启动时自动完成
```

## 7. 自己构建镜像

一般不需要 —— CI 已经构建了多架构（amd64/arm64）镜像。真要改源码：

```bash
docker compose build            # 用 docker-compose.yml 里注释掉的那两行
# 或
docker build -t papershelf:dev .
```

⚠️ 前端**必须在镜像内构建**：vite 产物不入库，而 hatchling 打包时也不会带上被 gitignore 的文件
（实测 wheel 里只有 `favicon.svg`/`icons.svg`，没有 `index.html`）。
所以 Dockerfile 里是「Node 阶段构建 SPA → 拷进 Python 阶段的包目录」，
"先在宿主机 build 好再打镜像"是不成立的。

### 基础镜像拉不动（国内网络）

镜像源可在构建时覆盖，不必改文件：

```bash
docker build \
  --build-arg NODE_BASE=docker.m.daocloud.io/library/node:22-alpine \
  --build-arg PY_BASE=docker.m.daocloud.io/library/python:3.13-slim \
  -t papershelf:dev .
```

## 8. 排障

| 症状 | 原因 / 处理 |
|---|---|
| 启动即退出，日志里有 `✖` | 按提示补齐环境变量（`PAPERSHELF_SECRET` / 管理员密码成对） |
| `数据目录不可写` | 挂的是宿主目录，属主不对：`sudo chown -R 10001:10001 <目录>` |
| 容器 `healthy` 但页面是「前端尚未构建」 | 镜像构建时前端阶段失败了 —— 看 CI 里的 `npm run build`（含 `tsc -b` 类型检查） |
| 导入的文献一直 `queued` | `docker compose logs -f` 看是否有 LLM 报错；未配 LLM 时不会转换（入口脚本已警告） |
| 转换 `failed` | 阅读器/文献表里**直接显示失败原因全文**（不是 tooltip），按提示改环境变量后点重试 |
| 健康检查一路 unhappy | `docker compose exec papershelf healthcheck.py` 手工跑一遍看具体报错 |
| 上传 PDF 报 `413 Request Entity Too Large` | 反代 `client_max_body_size` 太小；**链路上每一层都要放开**，看内层日志里的 `client intended to send too large body: <N> bytes` 定位是哪一层（见第 4 节） |

## 9. 安全与合规须知

- **公网部署必做**：HTTPS（反代）+ 强 `PAPERSHELF_SECRET` + 谨慎开启自助注册与分享。
- **分享链接有效期上限 24 小时**（`PAPERSHELF_SHARE_HOURS`，填更大也只按 24 算）：
  免登录只读，任何拿到链接的人都能读，所以设计上不支持永久链接，并在创建时可随时撤销。
- **注册与上传由用户协议约束**：注册时用户须逐条同意「对上传与分享行为承担全部责任」，
  服务端留档版本号与时间戳（`users.consent_version` / `consent_at`）。
- **单实例假设**：转换队列是进程内常驻线程 + SQLite 单文件 —— 别挂多副本（多进程会各跑一份队列，
  同一篇可能被两份认领）。要横向扩，先把队列换成带租约的 DB 轮询 worker。
