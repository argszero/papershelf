"""FastAPI 应用装配 —— 决策⑨（Python 单体）与 §4 契约的落地。

两个与安全直接相关的装配点：
1. **只读分享的写保护**（⑪）：带 share token 的请求，非 GET 一律 403 —— 必须由**后端**拒绝。
2. **静态前端**：SPA 的构建产物从 `static/` 提供；没有构建产物时给一个可用的降级页。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import connect, init_db
from .routers import admin, auth, blocks, highlights, notes, papers, plans, shares
from .routers.shares import resolve_share
from .security import SESSION_COOKIE, session_user

log = logging.getLogger("papershelf.app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 图片文件名白名单：p12_img3.png 这类，杜绝 ../ 穿越
_SAFE_ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]+\.(png|jpe?g|gif|webp|svg)$")

# 导出里会被内联的资产引用：登录内联 `/papers/<id>/assets` 与分享内联
# `/share/<token>/assets` 两种前缀，指向同一个 `p<id>/assets` 目录。
_ASSET_SRC = re.compile(
    r'src="(?:/papers/\d+/assets/|/share/[A-Za-z0-9_-]+/assets/)([^"]+)"')

# 一次从磁盘读多少字节去 base64。**必须是 3 的整数倍**：base64 按 3 字节一组编码，
# 块边界对不齐会在流中间塞进 `=` 填充，拼出来的 data URI 直接坏掉（有测试钉住）。
_INLINE_CHUNK = 768 * 1024


def _inline_targets(html_doc: str, paper_id: int, settings) -> list:
    """HTML 里的资产引用 → `[(match, 文件, mime)]`。**只 stat，不读内容。**

    为什么不在内联时直接读（2026-10-26 的内存教训）：原先这里是
    `re.sub(..., b64encode(path.read_bytes()))` —— 一篇图多的论文，单个请求同时持有
    原始的 `bytes`、base64 的 `str`、`re.sub` 新建的整篇文档，以及最终的响应体，
    实测峰值 **37.81 MB = 全篇资产字节的 4 倍**，且**没有任何上限**：上传有
    `PAPERSHELF_MAX_UPLOAD_MB`，导出这边当时什么都没有。而分享导出的那个端点
    **不需要会话**（⑪ 的设计），有链接就能反复触发。
    """
    import mimetypes

    out: list = []
    for m in _ASSET_SRC.finditer(html_doc):
        name = m.group(1)
        if not _SAFE_ASSET_NAME.match(name):
            continue                        # 不合白名单：原样留在文档里
        path = settings.papers_dir / f"p{paper_id}" / "assets" / name
        if not path.is_file():
            continue                        # 文件不在：同样原样留着
        out.append((m, path, mimetypes.guess_type(name)[0] or "image/png"))
    return out


def inline_asset_bytes(html_doc: str, paper_id: int, settings) -> int:
    """即将被内联的资产**总字节数**（stat，不读内容）—— 上限判据用它。"""
    return sum(p.stat().st_size for _, p, _ in _inline_targets(html_doc, paper_id, settings))


def iter_inlined_html(html_doc: str, paper_id: int, settings):
    """流式吐出内联后的 HTML：文档本身是碎的，每张资产读完一块就丢一块。

    峰值因此只与 `_INLINE_CHUNK` 有关，与**论文有多少张图、图多大**无关。
    """
    import base64

    pos = 0
    for m, path, mime in _inline_targets(html_doc, paper_id, settings):
        yield html_doc[pos:m.start()]
        yield f'src="data:{mime};base64,'
        with path.open("rb") as fh:
            while True:
                block = fh.read(_INLINE_CHUNK)
                if not block:
                    break
                yield base64.b64encode(block).decode("ascii")
        yield '"'
        pos = m.end()
    yield html_doc[pos:]


def stream_inlined_export(html_doc: str, paper_id: int, settings):
    """**上限与流式必须一起做**，所以只留这一个入口。

    拆成两个函数、让路由自己"先查上限、再要生成器"，就会出现只做了流式、
    没查上限的端点 —— 分享导出正好是这种最容易漏的地方（它连 `inline` 参数都没有，
    无条件内联）。这里返回的是迭代器，**检查在返回之前就已经跑完**，路由拿到的东西
    一定过过闸。

    超限用 413：HTTP 里没有"响应太大"的状态码，而本项目已经有
    `PAPERSHELF_MAX_UPLOAD_MB` 用 413 表示体积拒绝，沿用同一个口径。
    """
    cap = max(1, int(settings.max_export_mb)) * 1024 * 1024
    total = inline_asset_bytes(html_doc, paper_id, settings)
    if total > cap:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"导出内联体积 {total / 1048576:.1f} MB 超过上限 {settings.max_export_mb} MB"
            f"（配置项 PAPERSHELF_MAX_EXPORT_MB）；改用 inline=0 可只导出图片引用路径")
    return iter_inlined_html(html_doc, paper_id, settings)


def _lifespan(app: FastAPI):
    """常驻转换队列的起停（`server/queue.py`）。

    ⚠️ 用 `lifespan` 而不是 `@app.on_event`：后者在 FastAPI 0.141 已弃用，
    且**只有 lifespan 能在 `TestClient(app)` 的上下文管理器里可靠触发**（`with` 进出）。

    队列必须在**进程级**存活：`BackgroundTasks` 只在单次请求期间存在，
    请求一结束就没有任何东西再看着 `queued` 了（见模块 docstring 的事故复盘）。
    """
    from contextlib import asynccontextmanager

    from .queue import ConversionQueue

    @asynccontextmanager
    async def _cm(_app: FastAPI):
        settings_ = get_settings()
        q: ConversionQueue | None = None
        if settings_.queue_enabled:
            q = ConversionQueue(settings_, interval=settings_.queue_interval)
            q.start()
        else:
            log.warning("转换队列已关闭（PAPERSHELF_QUEUE=false）——"
                        "重启会丢掉排队中的文献，仅测试/排障时使用")
        try:
            yield
        finally:
            if q is not None:
                q.stop()

    return _cm(app)


def create_app() -> FastAPI:
    settings = get_settings()
    # ⚠️ 日志必须在**任何业务动作之前**配好：`init_db`/`ensure_admin` 都会打日志，
    # 晚一步配就等于把启动阶段（最容易出错的一段）的日志丢掉。
    # `serve` 里也调了一次（同一入口，幂等）；这里再调是为了覆盖
    # `uvicorn papershelf.server.app:app`（不经 CLI）与测试直接建 app 的用法。
    from .logging_setup import setup_logging

    setup_logging(settings.log_level, access_log=settings.log_access)
    init_db(settings)
    # 启动摘要：排障时先看这几行就够判断"实例是以什么配置跑起来的"。
    # ⚠️ **绝不打印密钥**（`llm_api_key` / `secret` / `smtp_pass` 一律只报"有无"）。
    log.info("启动配置：data=%s base_url=%s queue=%s 并发=%s max_attempts=%s "
             "llm=%s model=%s smtp=%s 自助注册=%s 日志级别=%s（每篇日志=%s）",
             settings.data_dir, settings.base_url,
             settings.queue_enabled, settings.max_concurrency, settings.max_conv_attempts,
             (settings.llm_base_url or "未配置"), settings.llm_model,
             ("已配置" if settings.smtp_configured else "未配置"), settings.open_registration,
             settings.log_level, settings.log_per_paper)
    if settings.admin_email:
        conn = connect(settings)
        try:
            from .security import ensure_admin

            try:
                ensure_admin(conn, settings.admin_email, settings.admin_password)
                log.info("已确保管理员账号：%s", settings.admin_email)
            except ValueError as exc:
                # 不静默降级：引导失败必须响亮地说出来（否则实例形同无法登录）
                log.error("管理员引导失败：%s", exc)
        finally:
            conn.close()
    if settings.secret.startswith("dev-insecure"):
        log.warning("PAPERSHELF_SECRET 未设置（正在用开发默认值）—— 生产部署必须显式配置")

    app = FastAPI(title="papershelf", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=_lifespan)

    # 每请求一个 SQLite 连接（WAL 下并发读不互斥；写极少且都很短）
    def conn_factory():
        return connect(settings)

    app.state.conn_factory = conn_factory
    app.state.settings = settings
    app.state.stream_inlined_export = (
        lambda html_doc, pid: stream_inlined_export(html_doc, pid, settings))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.base_url.startswith("http://localhost") else [settings.base_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── ⑪ 只读分享的写保护：后端拒绝，不依赖前端 ──
    @app.middleware("http")
    async def readonly_share_guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/share/") or "/shares/" in path:
            method = request.method.upper()
            if method not in ("GET", "HEAD", "OPTIONS"):
                # 允许会话作者管理自己的分享（撤销走 /api/shares/{token} DELETE，已登录才可）
                if not request.cookies.get("papershelf_session"):
                    return JSONResponse(
                        {"detail": "只读分享链接不允许写操作"}, status_code=status.HTTP_403_FORBIDDEN
                    )
        return await call_next(request)

    # ── 图片资产：块 payload 里的 `assets/xxx.png` 必须能被读到，否则图全是 404 ──
    # 路径经白名单解析（防目录穿越），且**不做目录列举**（不挂 StaticFiles）；
    # 归属校验沿用决策①（登录用户只能读自己的文献；分享则凭 token）。
    def _serve_asset(pid: int, name: str):
        # ⚠️ 白名单必须作用在**原始路径**上：`x.png/../../papershelf.db` 这类
        #    穿越串在 URL 解码后仍会被 `Path.resolve()` 折叠掉，只查"解析后是否在目录内"
        #    是**不够**的（踩过：该串能一路解析到 papershelf.db 并 200 返回）。
        if not _SAFE_ASSET_NAME.match(name) or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "非法文件名")
        base = (settings.papers_dir / f"p{pid}" / "assets").resolve()
        target = (base / name).resolve()
        if not str(target).startswith(str(base) + os.sep) or not target.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "图片不存在")
        return FileResponse(target)

    def _may_read_paper(paper_id: int, request: Request) -> bool:
        """会话所有者 或 有效分享 token（`?share=<token>`）才可读。"""
        conn = conn_factory()
        try:
            user = session_user(conn, request.cookies.get(SESSION_COOKIE))
            if user is not None:
                row = conn.execute(
                    """SELECT p.id FROM papers p JOIN plans pl ON pl.id = p.plan_id
                       WHERE p.id=? AND pl.user_id=?""",
                    (paper_id, user["id"])).fetchone()
                if row is not None:
                    return True
            token = request.query_params.get("share")
            if token:
                share = resolve_share(conn, token)          # 无效/撤销/过期 → 抛 HTTP 异常
                row = conn.execute("SELECT id FROM papers WHERE id=? AND plan_id=?",
                                   (paper_id, share["plan_id"])).fetchone()
                return row is not None
            return False
        finally:
            conn.close()

    @app.get("/papers/{paper_id}/assets/{name:path}", include_in_schema=False)
    def paper_asset(paper_id: int, name: str, request: Request):
        if not _may_read_paper(paper_id, request):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "图片不存在")
        return _serve_asset(paper_id, name)

    @app.get("/share/{token}/assets/{name:path}", include_in_schema=False)
    def share_asset(token: str, name: str):
        """匿名只读分享里的图片（⑪ 持链接即可看整篇，图不能缺）。"""
        if not _SAFE_ASSET_NAME.match(name) or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "非法文件名")
        conn = conn_factory()
        try:
            share = resolve_share(conn, token)
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM papers WHERE plan_id=?", (share["plan_id"],)).fetchall()]
        finally:
            conn.close()
        for cand in ids:
            try:
                return _serve_asset(cand, name)
            except HTTPException:
                continue
        raise HTTPException(status.HTTP_404_NOT_FOUND, "图片不存在")

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Papershelf", "0.1.0")
        return response

    for r in (auth.router, plans.router, papers.router, blocks.router,
              notes.router, highlights.router, shares.router, admin.router):
        app.include_router(r)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "open_registration": settings.open_registration,
                "llm_configured": bool(settings.llm_base_url and settings.llm_api_key)}

    # ── 前端 ──
    assets = STATIC_DIR / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = STATIC_DIR / "index.html"

    # 顶层静态小文件（favicon 等）。**必须白名单**：若用通配路由，任意 URL 都能
    # 读到 static/ 下的文件（含将来可能放进来的敏感产物），且 `..` 能穿越出去。
    _STATIC_FILES = {"favicon.svg", "icons.svg", "robots.txt"}

    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/icons.svg", include_in_schema=False)
    @app.get("/robots.txt", include_in_schema=False)
    async def static_file(request: Request):
        name = request.url.path.lstrip("/")
        if name not in _STATIC_FILES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
        target = STATIC_DIR / name
        if not target.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
        return FileResponse(target)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # SPA 只吃「页面路由」。带扩展名的静态资源（旧 hash 的 js/css、别人扒的 .map）
        # 落到这里要老实 404，否则浏览器拿到一份 HTML 当 JS 解析，报一堆莫名其妙的
        # 语法错误，掩盖真正的 404。
        if "." in full_path.rsplit("/", 1)[-1]:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
        if index.exists():
            return FileResponse(index)
        return JSONResponse(
            {"detail": "前端尚未构建。开发态请访问 /api/docs 查看接口，或用 vite dev server（见 web/）。",
             "api_docs": "/api/docs"},
            status_code=status.HTTP_200_OK,
        )

    return app


# 供 uvicorn 以字符串导入（`papershelf.server.app:app`）。
# ⚠️ 上面 create_app() 已把连接交给闭包，这里没有泄漏的连接；也不要在此加顶层副作用。
app = create_app()
