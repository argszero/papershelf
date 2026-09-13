# papershelf —— 单镜像自托管（决策⑨「Python 单体」的容器形态）
#
# **镜像由 GitHub Actions 构建并推送到 GHCR**（.github/workflows/docker.yml）：
#   docker pull ghcr.io/argszero/papershelf:latest
# 本地构建仅在调试 Dockerfile 时用：
#   docker build -t papershelf .
#
# 刻意不写 `# syntax=docker/dockerfile:1`：本文件只用经典指令，加上那行会让每次构建
# 都先去 docker.io 拉一次 frontend 镜像（网络受限时直接构建失败，实测踩到）。
#
# 两个阶段：
#   1. web   —— Node 构建 SPA。**必须在镜像里构建**：前端产物是 vite 输出、不入库
#               （见 .gitignore），而 hatchling 打包 Python 包时又不会带上被 gitignore 的文件
#               （已实测：wheel 里只有 favicon/icons，没有 index.html）——
#               所以"先在宿主机 build 好再打镜像"这条路是不通的，除非额外 COPY 一份。
#   2. runtime —— Python 单体：API + 转换 worker + 静态前端同进程、同端口。
#
# 刻意不做的事：
#   · 不引 nginx —— 静态产物由 FastAPI 同进程托管（app.py 的 static 路由），
#     这是"屏幕上看到的与导出 HTML 是同一套渲染"的实现前提（§6 约束 1）。
#   · 不起多 worker —— 转换队列是进程内的，SQLite 也是单文件；多进程会各跑各的队列。
#     要横向扩，先换掉待定项 3，而不是加 -w。
#
# 基础镜像可覆盖（CI 用默认值；docker.io 不通的机器可换镜像源）：
#   docker build --build-arg NODE_BASE=docker.m.daocloud.io/library/node:22-alpine \
#                --build-arg PY_BASE=docker.m.daocloud.io/library/python:3.13-slim .
ARG NODE_BASE=node:22-alpine
ARG PY_BASE=python:3.13-slim


# ═══════════════════════════════════════════════════════════════════════
# 阶段 1｜构建前端 SPA
# ═══════════════════════════════════════════════════════════════════════
FROM ${NODE_BASE} AS web

WORKDIR /build/web

# 先只拷清单，让 npm ci 这一层能被缓存（改前端源码不必重装依赖）
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/ ./

# vite 的 outDir 指向 ../src/papershelf/static（web/vite.config.ts），
# `npm run build` 里含 `tsc -b`：类型错误会在这里让构建失败（前端 CI 的实质）。
RUN npm run build


# ═══════════════════════════════════════════════════════════════════════
# 阶段 2｜Python 运行时
# ═══════════════════════════════════════════════════════════════════════
FROM ${PY_BASE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PAPERSHELF_DATA_DIR=/data

WORKDIR /app

# 包本体 + 依赖。可编辑安装让源码留在 /app/src —— 前端产物随后覆盖进同一个目录，
# 避免"装进 site-packages 的那份 / 被拷贝的那份"两处分叉。
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install -e .

# 前端产物（来自阶段 1）覆盖进包内 static/
COPY --from=web /build/src/papershelf/static ./src/papershelf/static

COPY docker/entrypoint.sh docker/healthcheck.py /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.py

# 非 root 运行；数据卷的属主也一并给到（命名卷首次创建时会继承镜像里的属主）
RUN useradd --create-home --uid 10001 papershelf \
    && mkdir -p /data/papers \
    && chown -R papershelf:papershelf /data
USER papershelf

# SQLite + 原始 PDF + 抽出的图片都落在这里（PAPERSHELF_DATA_DIR）
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["healthcheck.py"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["papershelf", "serve", "--host", "0.0.0.0", "--port", "8000"]
