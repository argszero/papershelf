"""内存上限（2026-10-26 生产事故，rant 2026-10-26T09:30:12）。

一台 1888 MB / 2 核 / 零 swap 的机器连续上传几篇 PDF：容器 RSS 从 447 MB 单调涨到
518 MB、峰值 670 MB，同机 11 个容器连同 SSH 一起失去响应。`smaps_rollup` 显示匿名内存
479 MB 而文件映射仅 17 MB（不是 PDF 被 mmap、也不是 SQLite 页缓存），
`VmData 563 MB > VmRSS 492 MB`（已 free 的内存仍攥在 glibc arena 里）。

三处**单篇内部**的无上限行为叠在一起 —— 转换队列本身是对的，本次一个字都不动它：

1. `proofread.ProofreadTools._cache`：页图 base64 无淘汰、无上限；
2. `POST /papers/upload`：整份 PDF `read()` 进内存再落盘，且没有任何体积上限；
3. 每篇转换收尾只 `conn.close()`，从不把已 free 的内存还给操作系统。

本文件钉住 1 与 2 的行为边界、3 的**接线**（`release_memory` 真的被调、且在没有 glibc
的平台上不抛），以及 compose 文件里那条硬上限确实写了。真实 RSS 回落与
`HostConfig.Memory` 是**部署后的测量项**，不在离线回归里假装测过。
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from papershelf.pipeline.model import Block, Doc
from papershelf.pipeline.proofread import ProofreadTools
from papershelf.pipeline.translator import LLMConfig

ROOT = Path(__file__).resolve().parents[1]
CFG = LLMConfig(base_url="http://x", api_key="k", model="m")


def _doc(*texts: str, page: int = 1) -> Doc:
    doc = Doc()
    for i, t in enumerate(texts, start=1):
        doc.blocks.append(Block(id=f"b-{i:04d}", type="p", en=t,
                                payload={"page": page, "bbox": [0, 0, 1, 1]}))
    return doc


def _tiny_pdf(path, pages=2):
    fitz = pytest.importorskip("fitz")
    src = fitz.open()
    for i in range(pages):
        pg = src.new_page()
        pg.insert_text((72, 72), f"page {i + 1} " + "lorem ipsum dolor sit amet " * 3)
    src.save(str(path))
    src.close()


# ── 1. 页图缓存必须有上限 ────────────────────────────────────────────────

def test_page_image_cache_never_exceeds_its_cap(tmp_path):
    """验收 2：跑完一篇 6 页的稿子，缓存条数不超过上限（留下的正是最近几页）。"""
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=6)
    tools = ProofreadTools(_doc("text"), pdf, cache_size=3)
    try:
        for page in range(1, 7):
            assert tools._render(page), f"第 {page} 页渲染失败"
            assert len(tools._cache) <= 3, (
                f"渲染到第 {page} 页时缓存已有 {len(tools._cache)} 条 —— 上限 3 没生效"
            )
        # LRU：留下的是最近渲染的三页，最早那页已被淘汰
        assert [key[0] for key in tools._cache] == [4, 5, 6]
    finally:
        tools.close()


def test_page_image_cache_counts_a_cache_hit_as_recent(tmp_path):
    """复查（命中）过的页不该被当成"最久没用过"而淘汰 —— 那正是它会再看的那一页。"""
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=4)
    tools = ProofreadTools(_doc("text"), pdf, cache_size=3)
    try:
        for page in (1, 2, 3):
            tools._render(page)
        tools._render(1)                      # 复查第 1 页
        tools._render(4)                      # 该走的是第 2 页，不是第 1 页
        assert [key[0] for key in tools._cache] == [3, 1, 4]
    finally:
        tools.close()


def test_evicting_a_page_only_costs_a_local_re_render(tmp_path):
    """被淘汰 ≠ 丢信息：再渲染一次得到**逐字节相同**的图，且不花任何 token。"""
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=2)
    tools = ProofreadTools(_doc("text"), pdf, cache_size=1)
    try:
        first = tools._render(1)
        tools._render(2)                      # 第 1 页被挤出缓存
        assert tools._render(1) == first
    finally:
        tools.close()


def test_cache_size_is_clamped_to_at_least_one(tmp_path):
    """配置成 0 / 负数不能变成"每次都渲染、且每次都不存"的死循环式浪费。"""
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=1)
    tools = ProofreadTools(_doc("text"), pdf, cache_size=0)
    try:
        assert tools.cache_size == 1
        tools._render(1)
        tools._render(1)
        assert len(tools._cache) == 1
    finally:
        tools.close()


def test_proofreader_hands_the_configured_cache_size_to_the_tools(monkeypatch, tmp_path):
    """配置项要真的走到工具层（`Settings` → `Proofreader` → `ProofreadTools`）。

    ⚠️ 只钉"字段存在"是不够的 —— 本仓库踩过"有配置项、没有消费端"（`test_config_guard.py`）。
    """
    import papershelf.pipeline.proofread as pfmod

    seen: dict = {}
    real = pfmod.ProofreadTools

    class Spy(real):                          # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pfmod, "ProofreadTools", Spy)
    monkeypatch.setattr(pfmod.Proofreader, "_run", lambda self, messages, impl: None)

    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=1)
    pf = pfmod.Proofreader(CFG, image_cache=5)
    pf.proofread_doc(_doc("Some text to review."), pdf)

    assert pf.image_cache == 5
    assert seen.get("cache_size") == 5, f"cache_size 没传到工具层：{seen}"


# ── 2. 上传：流式落盘 + 体积上限 ──────────────────────────────────────────

def _login_and_plan(client, make_user) -> int:
    email, pw = make_user("owner@tsinghua.edu.cn")
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return client.post("/api/plans", json={"name": "精读"}).json()["id"]


def _set_upload_cap(monkeypatch, mb: int):
    monkeypatch.setenv("PAPERSHELF_MAX_UPLOAD_MB", str(mb))
    from papershelf.server.config import get_settings
    return get_settings(refresh=True)


def _paper_rows(settings, plan_id: int) -> int:
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        return conn.execute("SELECT COUNT(*) FROM papers WHERE plan_id=?",
                            (plan_id,)).fetchone()[0]
    finally:
        conn.close()


def test_upload_over_the_configured_cap_returns_413(monkeypatch, client, settings, make_user):
    """验收 3 的第一半：超出上限直接 413，且**不留下半个文件、不建空文献行**。"""
    plan_id = _login_and_plan(client, make_user)
    _set_upload_cap(monkeypatch, 1)
    before = set(settings.papers_dir.iterdir())

    payload = b"%PDF-1.4\n" + b"x" * (1024 * 1024)          # 略多于 1 MB
    r = client.post(f"/api/plans/{plan_id}/papers/upload",
                    files={"files": ("big.pdf", payload, "application/pdf")},
                    data={"background_convert": "false"})

    assert r.status_code == 413, r.text
    assert "1 MB" in r.json()["detail"]
    assert set(settings.papers_dir.iterdir()) == before, "413 之后 papers_dir 里留下了残file"
    assert _paper_rows(settings, plan_id) == 0, "413 之后仍然建了文献行"


def test_upload_right_at_the_cap_is_accepted(monkeypatch, client, settings, make_user):
    """边界是 `>` 不是 `>=`：正好等于上限必须收下（否则用户算不准该压到多少）。"""
    plan_id = _login_and_plan(client, make_user)
    _set_upload_cap(monkeypatch, 1)

    payload = b"%PDF-1.4\n" + b"x" * (1024 * 1024 - 9)
    assert len(payload) == 1024 * 1024
    r = client.post(f"/api/plans/{plan_id}/papers/upload",
                    files={"files": ("exact.pdf", payload, "application/pdf")},
                    data={"background_convert": "false"})

    assert r.status_code == 201, r.text
    written = [p for p in settings.papers_dir.iterdir() if p.name.endswith("exact.pdf")]
    assert len(written) == 1
    assert written[0].stat().st_size == len(payload), "落盘字节数与上传字节数不一致"


def test_upload_writes_in_chunks_instead_of_reading_the_whole_file(
        monkeypatch, client, settings, make_user):
    """验收 3 的第二半：**不许**再出现"整份读进内存"。

    判据落在真调用上：记录每次 `UploadFile.read` 收到的参数 —— 无参 `read()`（或
    `size=-1`）就是整份取，那正是 200 MB 上传变成 200 MB 常驻内存的那一行。
    """
    from papershelf.server.routers import papers as papers_router
    from starlette.datastructures import UploadFile as StarletteUploadFile

    plan_id = _login_and_plan(client, make_user)
    _set_upload_cap(monkeypatch, 8)

    sizes: list[int] = []

    # ⚠️ 要**同时**钉两处：`fastapi.UploadFile` 是 `starlette.datastructures.UploadFile`
    # 的子类且自己定义了一份 `read`，而表单解析器交给端点的是父类的实例 ——
    # 只 patch 子类时间谍一次都不会被调用（第一版就这么写错了，断言在"判据失效"上转红）。
    for cls in (papers_router.UploadFile, StarletteUploadFile):
        original = cls.read

        async def spy(self, size=-1, _original=original):
            sizes.append(size)
            return await _original(self, size)

        monkeypatch.setattr(cls, "read", spy)

    payload = b"%PDF-1.4\n" + b"y" * (3 * 1024 * 1024)      # 3 MB
    r = client.post(f"/api/plans/{plan_id}/papers/upload",
                    files={"files": ("three.pdf", payload, "application/pdf")},
                    data={"background_convert": "false"})

    assert r.status_code == 201, r.text
    assert sizes, "上传端点一次 read 都没调 —— 这条判据本身失效了"
    assert all(s == 1 << 20 for s in sizes), f"有人用无界 read 整份取文件：{sizes}"
    assert len(sizes) >= 4, f"3 MB 的文件应该分多块搬运，实测调用序列 {sizes}"


def test_rejected_upload_leaves_no_file_behind(client, settings, make_user):
    """非 PDF：照旧 400，并且**同样**不留下写了一半的文件。"""
    plan_id = _login_and_plan(client, make_user)
    before = set(settings.papers_dir.iterdir())

    r = client.post(f"/api/plans/{plan_id}/papers/upload",
                    files={"files": ("notes.txt", b"not a pdf at all", "text/plain")},
                    data={"background_convert": "false"})

    assert r.status_code == 400, r.text
    assert set(settings.papers_dir.iterdir()) == before


# ── 3. 每篇转换收尾把内存还给操作系统 ──────────────────────────────────

def test_conversion_releases_memory_when_it_ends(monkeypatch, settings):
    """接线：`convert_paper` 的 `finally` 必须调到 `release_memory`（三条路径都走这里）。"""
    from papershelf.server import converter

    calls: list[int] = []
    monkeypatch.setattr(converter, "release_memory",
                        lambda: calls.append(1) or True)

    converter.convert_paper(999_999)          # 不存在的文献：try 里直接 return，finally 照样跑

    assert calls == [1], "转换收尾没有调 release_memory"


def test_release_memory_calls_malloc_trim_with_zero(monkeypatch):
    """glibc 上必须调 `malloc_trim(0)`：参数 0 = "能还多少还多少"。"""
    from papershelf.server import converter

    calls: list[int] = []

    class FakeLib:
        def malloc_trim(self, n):             # noqa: N802 — 名字来自 libc
            calls.append(n)
            return 1

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: FakeLib())

    assert converter.release_memory() is True
    assert calls == [0]


def test_release_memory_is_a_no_op_when_the_symbol_is_missing(monkeypatch):
    """macOS / musl 上没有 `malloc_trim`：静默跳过，**不抛**。"""
    from papershelf.server import converter

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: object())
    assert converter.release_memory() is False


def test_release_memory_is_a_no_op_when_libc_cannot_be_opened(monkeypatch):
    """连 libc 都打不开（异常平台）：返回 False，不许把异常抛进 `finally`。"""
    from papershelf.server import converter

    def boom(*a, **kw):
        raise OSError("no such library")

    monkeypatch.setattr(ctypes, "CDLL", boom)
    assert converter.release_memory() is False


def test_release_memory_never_raises_when_trim_fails(monkeypatch):
    """`malloc_trim` 自己失败也不许炸 —— 它挂在 `finally` 里，抛出去会盖掉真正的转换错误。"""
    from papershelf.server import converter

    class FakeLib:
        def malloc_trim(self, n):             # noqa: N802
            raise RuntimeError("boom")

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: FakeLib())
    assert converter.release_memory() is False


# ── 4. 容器必须有硬上限 ─────────────────────────────────────────────────

def test_compose_gives_the_container_a_hard_memory_limit():
    """验收 4：`HostConfig.Memory` 不为 0 —— 离线只能钉住"文件里真的写了"。

    真实读数（`docker inspect … --format '{{.HostConfig.Memory}}'`）是部署动作，
    见 PR 说明里的验收分工。
    """
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "mem_limit:" in text, "compose 里没有容器内存硬上限（越界会拖死整台宿主机）"
    assert "memswap_limit:" in text, "memswap_limit 没设：内核会以为还有 swap 可换"
    assert text.count("PAPERSHELF_MEM_LIMIT:-900m") == 2, \
        "mem_limit 与 memswap_limit 必须**同值**，且可由环境变量覆盖"
    # 旧注释与现在的行为相反（"不设 hard limit，避免……OOM"）—— 留着会让人以为这是有意的
    assert "不设 hard limit" not in text
