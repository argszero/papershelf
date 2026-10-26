"""解析缓存是**全局**的，它引用的图片却是**按篇**落盘的 —— 同一份 PDF 的第二篇会一张图都没有。

病灶（2026-10-26 离线实测，解析 v17）：

- `doc_cache` 的键 = `sha256("parse-v{N}" + PDF 字节)`（`routers/papers.py::_pdf_fingerprint`），
  **不含 paper / plan / user**，全库一个命名空间（待定项 7 的设计目的：同一份 PDF 只解析一次）；
- 解析时图片写进 `papers_dir/p<id>/assets`（`converter.py` 的 `parse_pdf(assets_dir=…)`），
  而块里只记相对名 `payload["src"] = "assets/<name>"`；
- 供图端点 `_serve_asset` 严格在 `p<pid>/assets` 内解析，越界或不存在一律 404。

⇒ 同一份 PDF 的**第二篇**命中缓存（`Doc.from_json`，一个文件都不写），它引用的图
只在**第一篇**的目录里 ⇒ 该篇全部插图 404，且**永不自愈**（同一个 fingerprint 永久命中）。

不是"多用户才有的边角"：同一个人把同一份 PDF 导进两个计划就会撞上，
而缓存键不含 user 正是它存在的理由。

本文件钉住四件事：
1. 命中缓存时，图片必须**真的落在本篇目录里**，字节与第一篇一致；
2. 供图端点对第二篇返回 200 —— 判据落在用户真正走的那一层；
3. **缓存复用没有被修掉**：资产齐全时第二篇不再解析（否则等于用"每次都重解析"换掉了这个设计）；
4. 源目录已消失（第一篇被删文献）时**就地为这一篇重新解析**，并且把来源记进 meta。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _png(w: int = 8, h: int = 8, rgb: tuple[int, int, int] = (200, 60, 60)) -> bytes:
    """手工拼一张最小 PNG —— 不依赖 Pillow/Pixmap 的构造签名。"""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _make_pdf(path: Path) -> None:
    """一页、一张图、一行字 —— 让解析真的产出一个引用图片的块。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(72, 72, 172, 172), stream=_png())
    page.insert_text((72, 220), "The figure above is the whole point of this page.")
    doc.save(str(path))
    doc.close()


def _upload(client, plan_id: int, name: str, payload: bytes) -> tuple[int, str]:
    import io

    up = client.post(f"/api/plans/{plan_id}/papers/upload",
                     files={"files": (name, io.BytesIO(payload), "application/pdf")},
                     data={"background_convert": "false"})
    assert up.status_code == 201, up.text
    pid = int(up.json()[0]["id"])
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect

    conn = connect(get_settings())
    try:
        pdf_path = conn.execute("SELECT pdf_path FROM papers WHERE id=?", (pid,)).fetchone()[0]
    finally:
        conn.close()
    return pid, pdf_path


def _stub_llm(monkeypatch, cv) -> None:
    """把 ①c/②/②b/③/④ 全换成替身：本文件只关心解析与资产，不烧 token、不联网。"""

    class _FakeLz:
        tokens_used = 0

        def latexize(self, blocks, **kw):
            return {"blocks": 0, "chunks": 0, "retried": 0, "failed": 0, "done": 0}

    class _FakeMx:
        tokens_used = 0

        def extract(self, blocks, **kw):
            return {"title_en": "Fake Title"}

    class _FakeTr:
        tokens_used = 0
        needs_review: list[str] = []

        def __init__(self, cfg, glossary):
            pass

        @staticmethod
        def ordered(blocks):
            return blocks

        def translate_blocks(self, blocks, **kw):
            return [b.zh or b.en for b in blocks]

    from papershelf.pipeline.validate import Report

    monkeypatch.setattr(cv, "MetadataExtractor", lambda cfg: _FakeMx())
    monkeypatch.setattr(cv, "Translator", _FakeTr)
    monkeypatch.setattr(cv, "Latexizer", lambda cfg: _FakeLz())
    monkeypatch.setattr(cv, "validate", lambda *a, **k: Report())


def _spy_parse(monkeypatch, cv) -> list:
    """记录 `parse_pdf` 被调用了几次 —— 钉住"缓存复用没有被这次修复废掉"。"""
    from papershelf.pipeline import parse_pdf as real_parse

    calls: list = []

    def _spy(*a, **k):
        calls.append((a, k))
        return real_parse(*a, **k)

    monkeypatch.setattr(cv, "parse_pdf", _spy)
    return calls


def _run_paper(settings, pid: int, fingerprint: str | None):
    """按产品路径跑一次 `_run`（正式转换的外壳），返回它的 doc。"""
    from papershelf.server.db import connect
    from papershelf.server import converter as cv

    conn = connect(settings)
    try:
        row = dict(conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone())
        doc, _tokens = cv._run(conn, row, settings, fingerprint)
    finally:
        conn.close()
    return doc


def _fp(pdf_path: str) -> str:
    from papershelf.server.routers.papers import _pdf_fingerprint

    return _pdf_fingerprint(Path(pdf_path))


def _referenced_names(doc) -> set[str]:
    names: set[str] = set()
    for b in doc.blocks:
        src = str((b.payload or {}).get("src") or "")
        if src.startswith("assets/"):
            names.add(src.split("/", 1)[1])
    return names


def _seed(client, settings, make_user, monkeypatch, cv, *, first: str, second: str):
    """登录 + 建计划 + 传**字节完全相同**的两份 PDF；返回 (pid_a, pid_b, pdf_a, pdf_b)。"""
    _stub_llm(monkeypatch, cv)
    settings.llm_base_url = "https://example.invalid/v1"   # 只为过前置检查，不联网
    settings.llm_api_key = "test-key"
    settings.proofread = False

    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]

    src = settings.data_dir / "seed.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    _make_pdf(src)
    payload = src.read_bytes()

    pid_a, pdf_a = _upload(client, plan_id, first, payload)
    pid_b, pdf_b = _upload(client, plan_id, second, payload)
    assert pdf_a != pdf_b, "两篇必须各自落一个 PDF 文件（上传路径带时间戳）"
    assert _fp(pdf_b) == _fp(pdf_a), "两份字节相同就必须共用同一个缓存键"
    return pid_a, pid_b, pdf_a, pdf_b


def test_cache_hit_materialises_the_assets_into_the_second_papers_directory(
        client, settings, make_user, monkeypatch):
    """⭐ 同一份 PDF 的第二篇：块里引用的每张图都必须真的在**它自己**的目录里。"""
    from papershelf.server import converter as cv

    pid_a, pid_b, pdf_a, pdf_b = _seed(
        client, settings, make_user, monkeypatch, cv, first="第一篇.pdf", second="第二篇.pdf")
    calls = _spy_parse(monkeypatch, cv)

    # 第一篇：真解析（把图片写进 p<A>/assets）并落下缓存行 —— 与产品路径一致
    _run_paper(settings, pid_a, _fp(pdf_a))
    assert len(calls) == 1, "第一篇没有真正解析过 —— 夹具没跑通"
    calls.clear()

    assets_a = settings.papers_dir / f"p{pid_a}" / "assets"
    first_names = {p.name for p in assets_a.iterdir()} if assets_a.is_dir() else set()
    assert first_names, "第一篇的资产目录是空的 —— 夹具没跑通"

    # 第二篇：必须命中缓存（不再解析），且图片必须落到它自己的目录里
    doc_b = _run_paper(settings, pid_b, _fp(pdf_b))
    assert not calls, (
        f"第二篇重新解析了 {len(calls)} 次 —— 资产的修复不该把缓存复用废掉"
        "（待定项 7：同一份 PDF 只解析一次）"
    )

    names = _referenced_names(doc_b)
    assert names, "第二篇的文档没有引用任何图片 —— 夹具没跑通"
    assets_b = settings.papers_dir / f"p{pid_b}" / "assets"
    missing = [n for n in sorted(names) if not (assets_b / n).is_file()]
    assert not missing, (
        "解析缓存命中了，但图片没有搬进**第二篇自己**的目录："
        f"缺 {missing}（第一篇的目录里有：{sorted(first_names)}）"
        "—— 供图端点只认 p<pid>/assets，这一篇的插图会全部 404"
    )
    for n in names:
        assert (assets_b / n).read_bytes() == (assets_a / n).read_bytes(), \
            f"{n} 搬过去了但字节不一致"

    r = client.get(f"/papers/{pid_b}/assets/{sorted(names)[0]}")
    assert r.status_code == 200, (
        f"供图端点对第二篇返回 {r.status_code} —— 用户看到的就是一张裂图"
    )


def test_cache_hit_falls_back_to_reparsing_when_the_source_assets_are_gone(
        client, settings, make_user, monkeypatch):
    """第一篇被删（`rmtree p<id>/`）之后，第二篇**必须自己重新解析**，而不是留一堆裂图。"""
    import shutil

    from papershelf.server import converter as cv

    pid_a, pid_b, pdf_a, pdf_b = _seed(
        client, settings, make_user, monkeypatch, cv, first="甲.pdf", second="乙.pdf")
    calls = _spy_parse(monkeypatch, cv)

    _run_paper(settings, pid_a, _fp(pdf_a))
    calls.clear()

    # 第一篇被删文献：整个目录被收走，缓存行却还在（删文献并不会让 doc_cache 失效）
    shutil.rmtree(settings.papers_dir / f"p{pid_a}", ignore_errors=True)
    assert not (settings.papers_dir / f"p{pid_a}").exists()

    doc_b = _run_paper(settings, pid_b, _fp(pdf_b))
    assert len(calls) == 1, (
        f"源目录已经不在了，却没有为这一篇重新解析（parse_pdf 调用 {len(calls)} 次）"
        "—— 这一篇的插图会全部 404 且永不自愈"
    )

    names = _referenced_names(doc_b)
    assert names, "重新解析出来的文档没有引用任何图片 —— 夹具没跑通"
    assets_b = settings.papers_dir / f"p{pid_b}" / "assets"
    missing = [n for n in sorted(names) if not (assets_b / n).is_file()]
    assert not missing, f"重解析之后图片仍然不在本篇目录里：缺 {missing}"
    assert doc_b.meta.get("assets_dir") == f"p{pid_b}/assets", (
        "重解析后没把资产的来源目录记进 meta —— 下一次别人命中这份缓存时又搬不了"
    )
