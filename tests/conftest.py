"""pytest 夹具：每个测试一个**隔离的临时数据目录**，绝不碰真实数据。

⚠️ 根因提醒（踩过）：papershelf 各模块用 `from .config import get_settings` 直接引用函数，
若测试想去 patch，那是**两条完全不同的路径**（模块导入时已绑定）。所以这里不 patch 任何东西，
只在 import 应用代码**之前**设置环境变量，让 `Settings()` 天然读到测试值
（`get_settings(refresh=True)` 每次重建实例 → 必然生效）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _fresh_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> str:
    tmp = tempfile.mkdtemp(prefix="papershelf-test-")
    env = {
        "PAPERSHELF_DATA_DIR": tmp,
        "PAPERSHELF_SECRET": "test-secret",
        "PAPERSHELF_ADMIN_EMAIL": "",
        "PAPERSHELF_ADMIN_PASSWORD": "",
        "PAPERSHELF_SMTP_HOST": "",
        "PAPERSHELF_SMTP_FROM": "",
        "PAPERSHELF_LLM_BASE_URL": "",
        "PAPERSHELF_LLM_API_KEY": "",
        # ⚠️ 常驻转换队列默认**关**：它会在后台扫 `queued` 并真的跑 `convert_paper`
        #    （测试里没配 LLM → 会把用例刚塞的行翻成 failed），且线程时序不确定。
        #    `tests/test_queue.py` 自己显式打开并注入替身 runner 来测它。
        "PAPERSHELF_QUEUE": "false",
    }
    env.update(extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return tmp


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """**每个测试一个**临时数据目录，且 `settings` 与 `client` 共享同一个。

    ⚠️ 不能各自建目录：那样 fixture 里的库和 app 读的库是两份，
    写进去的文献在接口里查不到（踩过）。
    """
    tmp = _fresh_env(monkeypatch)
    from papershelf.server.config import get_settings

    get_settings(refresh=True).ensure_dirs()
    return tmp


@pytest.fixture()
def settings(_isolated_env):
    """干净的 Settings（已建目录、已建表）。"""
    from papershelf.server.config import get_settings
    from papershelf.server.db import init_db

    s = get_settings()
    init_db(s)
    return s


@pytest.fixture()
def client(_isolated_env):
    """TestClient + 真实 SQLite（与 settings 同一个数据目录）。"""
    from fastapi.testclient import TestClient

    from papershelf.server.app import create_app
    from papershelf.server.config import get_settings

    get_settings(refresh=True)
    app = create_app()
    with TestClient(app) as c:
        c.app_ref = app  # type: ignore[attr-defined]
        yield c


@pytest.fixture()
def spa_shell():
    """给「SPA 兜底」类测试临时放一份 index.html。

    ⚠️ 前端构建产物**不入库**（.gitignore）—— 全新克隆 / CI 上
    `src/papershelf/static/index.html` 并不存在，于是后端返回的是「前端尚未构建」
    的 JSON 降级页，断言 text/html 就会失败。**本地永远测不出来**，因为本机恰好 build 过。

    这里只在缺失时补一份最小 shell，测试后删除；已有真产物则原样保留（不碰）。
    """
    from papershelf.server.app import STATIC_DIR

    index = STATIC_DIR / "index.html"
    created = not index.exists()
    if created:
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        index.write_text(
            '<!doctype html><html lang="zh-CN"><body><div id="root"></div></body></html>',
            encoding="utf-8",
        )
    try:
        yield index
    finally:
        if created:
            index.unlink(missing_ok=True)


@pytest.fixture()
def no_spa_shell():
    """把构建产物**挪开**，模拟「没跑过 npm run build」。

    ⚠️ 不能用 monkeypatch 改 `app.STATIC_DIR`：路由里的 index 路径是 `create_app()`
    时闭包捕获的，patch 模块属性对已建好的 app 毫无作用（实测：测试会"通过"，
    但通过的其实是另一条分支）。挪文件才是真生效 —— `index.exists()` 是每次请求现算的。
    """
    from papershelf.server.app import STATIC_DIR

    index = STATIC_DIR / "index.html"
    hidden = index.with_suffix(".html.hidden-by-test")
    moved = index.exists()
    if moved:
        index.rename(hidden)
    try:
        yield
    finally:
        if moved:
            hidden.rename(index)


@pytest.fixture()
def make_user(settings):
    """直接在库里建一个可用账号，返回 (email, password)。"""
    from papershelf.server.db import connect
    from papershelf.server.security import create_user

    def _make(email: str = "a@tsinghua.edu.cn", password: str = "password123",
              is_admin: bool = False):
        conn = connect(settings)
        try:
            create_user(conn, email=email, password=password, status_="active",
                        is_admin=is_admin)
        finally:
            conn.close()
        return email, password

    return _make
