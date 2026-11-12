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
5. **目录被收走之后，缓存行不许再把它当来源**：`p<id>` 会被下一篇复用（`INTEGER PRIMARY
   KEY` 无 `AUTOINCREMENT`），资产名又只按页编号，所以"行还指着 p<id>/assets"等于
   "按名字复制别人的图"。删除时摘掉这条指针 ⇒ 命中缓存只能就地为这一篇重新解析。
6. **判据落在字节上**（`meta["asset_digests"]`）：解析时记下每张图的 sha256，命中缓存时
   源文件必须对得上（老行没有这个字段 ⇒ 重解析），并且本篇目录里的同名文件**必须被覆盖**。
   只按目录名（老行的指针）、只按"文件在不在"（`if dst.is_file(): continue`）判断，
   都会把别人的图当成这一篇的：不报错、不缺图、不自愈。
"""

from __future__ import annotations

import hashlib
import json
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


def _make_pdf(path: Path, rgb: tuple[int, int, int] = (200, 60, 60)) -> None:
    """一页、一张图、一行字 —— 让解析真的产出一个引用图片的块。

    `rgb` 让不同 PDF 的插图**字节不同**：本文件要靠"图是谁的"来判断有没有搬错。
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(72, 72, 172, 172), stream=_png(rgb=rgb))
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


def _prepare(client, settings, make_user, monkeypatch, cv) -> int:
    """登录 + 建一个计划，返回 plan_id（LLM 全替身：零 token、零网络）。"""
    _stub_llm(monkeypatch, cv)
    settings.llm_base_url = "https://example.invalid/v1"   # 只为过前置检查，不联网
    settings.llm_api_key = "test-key"
    settings.proofread = False

    email, pw = make_user("owner@tsinghua.edu.cn")
    assert client.post("/api/auth/login",
                       json={"email": email, "password": pw}).status_code == 200
    plan = client.post("/api/plans", json={"name": "精读"})
    assert plan.status_code in (200, 201), plan.text
    return int(plan.json()["id"])


def _seed(client, settings, make_user, monkeypatch, cv, *, first: str, second: str):
    """登录 + 建计划 + 传**字节完全相同**的两份 PDF；返回 (pid_a, pid_b, pdf_a, pdf_b)。"""
    plan_id = _prepare(client, settings, make_user, monkeypatch, cv)

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


def _cache_meta(settings, fingerprint: str) -> dict | None:
    """读缓存行里的 `meta`（行不在则 None）。"""
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        row = conn.execute("SELECT doc_json FROM doc_cache WHERE fingerprint=?",
                           (fingerprint,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return json.loads(row["doc_json"]).get("meta") or {}


def _asset_digests(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.iterdir()) if p.is_file()}


def test_reclaimed_assets_dir_is_never_used_as_another_papers_figure_source(
        client, settings, make_user, monkeypatch):
    """⭐ 删掉一篇之后，缓存行**不许**再把这已收走的目录当来源（`#19`）。

    四段，全走真上传 / 真删除 / 真解析（LLM 全替身）：

    1. 传 F（红图）并转换 ⇒ `p<id>/assets` 里是 F 的图；
    2. `DELETE /api/papers/<id>` ⇒ 目录与 PDF 都收了，**缓存行留下**（它是共享的）——
       但它**不能再指着** `p<id>/assets`（这就是本文件要钉的不变量）；
    3. 传另一份 G（蓝图）⇒ 复用同一个 id，把**自己的**图写进同一个 `p<id>/assets`；
    4. 再传 F 的字节 ⇒ 命中缓存也必须**为这一篇重新解析**，拿到的是 F 自己的图。

    没有第 2 步那条要求时，第 4 步会按名字从 `p<id>/assets` 复制 —— 0 次解析、
    不报错、不缺图，但那一篇显示的是 **G 的插图**（实测 digest 与 G 相同）。
    """
    from papershelf.server import converter as cv

    plan_id = _prepare(client, settings, make_user, monkeypatch, cv)
    calls = _spy_parse(monkeypatch, cv)

    def _bytes_of(rgb: tuple[int, int, int]) -> bytes:
        src = settings.data_dir / f"seed-{rgb[0]}.pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        _make_pdf(src, rgb=rgb)
        return src.read_bytes()

    red, blue = (200, 60, 60), (60, 60, 200)
    f_bytes, g_bytes = _bytes_of(red), _bytes_of(blue)

    # ── 1. F 真解析 ──
    pid_f, pdf_f = _upload(client, plan_id, "F.pdf", f_bytes)
    fp_f = _fp(pdf_f)                     # ⚠️ 必须在删除**之前**算：键取自 PDF 字节
    _run_paper(settings, pid_f, fp_f)
    assert len(calls) == 1, "第一篇没有真正解析过 —— 夹具没跑通"
    calls.clear()
    f_digests = _asset_digests(settings.papers_dir / f"p{pid_f}" / "assets")
    assert f_digests, "第一篇的资产目录是空的 —— 夹具没跑通"
    assert _cache_meta(settings, fp_f).get("assets_dir") == f"p{pid_f}/assets"

    # ── 2. 删掉它：目录收了，缓存行留下，但**指针必须没了** ──
    assert client.delete(f"/api/papers/{pid_f}").status_code == 204
    assert not (settings.papers_dir / f"p{pid_f}").exists(), "目录没被收走 —— 夹具没跑通"
    meta_after = _cache_meta(settings, fp_f)
    assert meta_after is not None, (
        "缓存行被跟着删了 —— 它会打掉共用同一份 PDF 的另一篇的命中（这一格刻意保留行）"
    )
    assert meta_after.get("assets_dir") != f"p{pid_f}/assets", (
        f"目录 p{pid_f}/ 已经不在了，缓存行却还把它记成图片来源 "
        f"（assets_dir={meta_after.get('assets_dir')!r}）—— p<id> 会被下一篇复用，"
        "届时命中缓存的篇目会按名字复制到**别人的**插图"
    )

    # ── 3. 传另一份 PDF：复用同一个 id，往同一个目录写它自己的图 ──
    pid_g, pdf_g = _upload(client, plan_id, "G.pdf", g_bytes)
    assert pid_g == pid_f, (
        f"id 没有被复用（{pid_g} != {pid_f}）—— 本缺陷需要复用才会发生，夹具不成立"
    )
    _run_paper(settings, pid_g, _fp(pdf_g))
    assert len(calls) == 1, "第二份 PDF 应当真解析一次"
    calls.clear()
    g_digests = _asset_digests(settings.papers_dir / f"p{pid_f}" / "assets")
    assert g_digests, "第二篇的资产目录是空的 —— 夹具没跑通"
    shared = set(f_digests) & set(g_digests)
    assert shared, "两份 PDF 的插图不同名 —— 那这个缺陷就撞不上，夹具不成立"
    assert any(f_digests[n] != g_digests[n] for n in shared), \
        "两份 PDF 的插图字节相同 —— 分不出是谁的图，夹具不成立"

    # ── 4. 再传 F 的字节：必须重解析，且图必须是 F 自己的 ──
    pid_d, pdf_d = _upload(client, plan_id, "F-again.pdf", f_bytes)
    assert pid_d != pid_f, "第三篇应当拿到另一个 id"
    _run_paper(settings, pid_d, _fp(pdf_d))
    assert len(calls) == 1, (
        f"命中缓存后没有为这一篇重新解析（parse_pdf 调用 {len(calls)} 次）—— "
        "它按名字从 p<id>/assets 复制，而那个目录现在属于另一篇"
    )
    d_digests = _asset_digests(settings.papers_dir / f"p{pid_d}" / "assets")
    for name, digest in f_digests.items():
        assert d_digests.get(name) == digest, (
            f"{name} 不是 F 自己的图：这篇拿到 {d_digests.get(name)}，"
            f"F 的原图是 {digest}（G 在同名文件里是 {g_digests.get(name)}）—— "
            "用户看到的是一张**属于别的文献**的插图，不报错、不缺图、不自愈"
        )
    for name in shared:
        assert d_digests.get(name) != g_digests[name], (
            f"{name} 是 G 的图被搬过来了 —— 名字一样、字节不同，复制端无法察觉"
        )


def test_a_live_paper_keeps_its_cache_hit_after_the_other_one_is_deleted(
        client, settings, make_user, monkeypatch):
    """⭐ 删掉共用同一份 PDF 的另一篇之后，**活着的那篇不许开始重解析**。

    摘掉 `assets_dir` 指针只有在「来源目录真的不归这一篇了」时才该付代价。
    这里钉住代价没有被误付：A、B 用**同一份字节**（同一个缓存键），B 第一次命中时
    `_materialise_cached_assets` 已经把 `assets_dir` 改写成 `p<B>/assets`，
    所以 A 被删时那条清空条件根本不成立 —— B 的命中照旧，之后同字节的 C 也只是复制。

    四段，全走真上传 / 真删除 / 真解析（LLM 全替身、零 token）：

    1. 删 A（`p<A>/` 整个收走，缓存行留下）；
    2. **紧接着**传第三份同字节的 C ⇒ **0 次解析**（指针此刻指着 `p<B>/assets`，那是活的）；
    3. C 的插图字节 = 这份 PDF **自己的**图；
    4. B 再转换 ⇒ 也 **0 次解析**。

    ⚠️ 第 2 步的**顺序**是这条用例的关键：`_materialise_cached_assets` 对已经在本篇目录
    里的文件直接跳过，所以*B 自己*哪怕丢了指针也不会重解析；真正会变贵的是**新来的**那一篇
    ——它目录是空的，只能按指针去别处复制。把 C 放在 B 重新转换**之前**，
    测的才是「删除之后这条指针还指着活目录」这个不变量；反过来放，
    B 的命中会把指针重新写回去，等于替被测代码把洞补上了（实测：那样变异也不会红）。

    它在「清空条件不再问这个目录还是不是那一篇的」时红：那时 C 找不到来源，
    只能就地重解析 —— 同一份 PDF 每被删掉一个引用者，下一个新篇目就要重解析一次。
    """
    from pathlib import Path as _Path

    from papershelf.server import converter as cv
    from papershelf.server.db import connect

    pid_a, pid_b, pdf_a, pdf_b = _seed(
        client, settings, make_user, monkeypatch, cv, first="甲.pdf", second="乙.pdf")
    calls = _spy_parse(monkeypatch, cv)

    _run_paper(settings, pid_a, _fp(pdf_a))
    assert len(calls) == 1, "第一篇没有真正解析过 —— 夹具没跑通"
    calls.clear()
    a_digests = _asset_digests(settings.papers_dir / f"p{pid_a}" / "assets")
    assert a_digests, "第一篇的资产目录是空的 —— 夹具没跑通"

    _run_paper(settings, pid_b, _fp(pdf_b))
    assert not calls, "第二篇本该命中缓存（同一份字节）—— 夹具没跑通"
    assert _cache_meta(settings, _fp(pdf_b)).get("assets_dir") == f"p{pid_b}/assets", (
        "命中缓存没有把 assets_dir 改写成**这一篇自己**的目录 —— "
        "后面那条清空条件就会把它当成别人的"
    )

    # ── 删除 A：B 还活着，而它是这份缓存当前唯一的引用者 ──
    assert client.delete(f"/api/papers/{pid_a}").status_code == 204
    assert not (settings.papers_dir / f"p{pid_a}").exists(), "目录没被收走 —— 夹具没跑通"
    calls.clear()

    # ── 第三篇：同字节，**在 B 重新转换之前**到来 ──
    conn = connect(settings)
    try:
        plan_id = int(conn.execute("SELECT plan_id FROM papers WHERE id=?",
                                   (pid_b,)).fetchone()[0])
    finally:
        conn.close()
    pid_c, pdf_c = _upload(client, plan_id, "丙.pdf", _Path(pdf_b).read_bytes())
    assert _fp(pdf_c) == _fp(pdf_b), "第三篇与前面不是同一份字节 —— 夹具没跑通"
    _run_paper(settings, pid_c, _fp(pdf_c))
    assert not calls, (
        f"删掉共用同一份 PDF 的另一篇之后，新传的同一份 PDF 重新解析了 {len(calls)} 次 —— "
        "删除时的清空条件打到了还活着的那篇头上：缓存行不再指着任何活目录，"
        "下一个引用同一份字节的篇目只能自己重解析"
    )
    c_digests = _asset_digests(settings.papers_dir / f"p{pid_c}" / "assets")
    for name, digest in a_digests.items():
        assert c_digests.get(name) == digest, (
            f"{name} 不是这份 PDF 自己的插图：这一篇拿到 {c_digests.get(name)}，"
            f"原始字节是 {digest}"
        )

    # ── 活着的那篇自己再转换一次：同样是 0 次解析 ──
    calls.clear()
    _run_paper(settings, pid_b, _fp(pdf_b))
    assert not calls, (
        f"这一篇自己也重新解析了 {len(calls)} 次 —— 它的命中不该被别人的删除影响"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 目录名不够，同名文件也不够 —— 判据必须是**字节**（`meta["asset_digests"]`）
# ══════════════════════════════════════════════════════════════════════════════

def _age_row(settings, fingerprint: str, *, pointer: str | None,
             drop_digests: bool) -> dict:
    """把缓存行改成"老版本写下的样子"（没有 `asset_digests`），或只把指针挪到别处。"""
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        row = conn.execute("SELECT doc_json FROM doc_cache WHERE fingerprint=?",
                           (fingerprint,)).fetchone()
        assert row is not None, "没有可做旧的缓存行 —— 夹具没跑通"
        doc = json.loads(row["doc_json"])
        if pointer is not None:
            doc["meta"]["assets_dir"] = pointer
        if drop_digests:
            doc["meta"].pop("asset_digests", None)
        conn.execute("UPDATE doc_cache SET doc_json=? WHERE fingerprint=?",
                     (json.dumps(doc), fingerprint))
        conn.commit()
    finally:
        conn.close()
    return _cache_meta(settings, fingerprint) or {}


def _two_pdf_bytes(settings) -> tuple[bytes, bytes]:
    """两份 PDF：版面相同（同名插图）、插图**字节不同** —— "图是谁的"就是全部判据。"""
    out = []
    for rgb in ((200, 60, 60), (60, 60, 200)):
        src = settings.data_dir / f"seed-{rgb[0]}.pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        _make_pdf(src, rgb=rgb)
        out.append(src.read_bytes())
    return out[0], out[1]


def test_a_row_without_asset_digests_reparses_instead_of_trusting_a_directory(
        client, settings, make_user, monkeypatch):
    """⭐ 老缓存行（没有 `asset_digests`）无从核对 ⇒ **重解析**，不许按名字复制。

    造法与生产里那类行一致（cycle 56 的读法 A）：F 解析过一次 → 删掉 F（目录被收走，
    缓存行留下，指针被 `#20` 摘掉）→ G 复用同一个 id，往同一个目录写**它自己的**图 →
    把 F 那行的 `assets_dir` 手改回 `p<id>/assets` 并去掉指纹 = 一条没被修过的老行。

    再传 F 的字节：只有重解析能拿到 F 自己的图；按名字复制拿到的是 G 的 ——
    不报错、不缺图、不自愈。
    """
    from papershelf.server import converter as cv

    plan_id = _prepare(client, settings, make_user, monkeypatch, cv)
    f_bytes, g_bytes = _two_pdf_bytes(settings)

    pid_f, pdf_f = _upload(client, plan_id, "F.pdf", f_bytes)
    fp_f = _fp(pdf_f)                     # ⚠️ 必须在删除**之前**算：键取自 PDF 字节
    doc_f = _run_paper(settings, pid_f, fp_f)
    names = sorted(_referenced_names(doc_f))
    assert names, "F 没有引用任何图片 —— 夹具没跑通"
    f_digests = _asset_digests(settings.papers_dir / f"p{pid_f}" / "assets")
    assert f_digests, "F 的资产目录是空的 —— 夹具没跑通"

    assert client.delete(f"/api/papers/{pid_f}").status_code in (200, 204)
    assert not (settings.papers_dir / f"p{pid_f}").exists()

    pid_g, pdf_g = _upload(client, plan_id, "G.pdf", g_bytes)
    assert pid_g == pid_f, (
        f"id 没有被复用（{pid_g} != {pid_f}）—— 本缺陷需要复用才会发生，夹具不成立")
    _run_paper(settings, pid_g, _fp(pdf_g))
    g_digests = _asset_digests(settings.papers_dir / f"p{pid_g}" / "assets")
    assert g_digests and any(f_digests[n] != g_digests[n] for n in names), \
        "两份 PDF 的插图字节相同 —— 分不出是谁的图，夹具不成立"

    meta = _age_row(settings, fp_f, pointer=f"p{pid_g}/assets", drop_digests=True)
    assert not meta.get("asset_digests"), "行没有被做旧 —— 夹具没跑通"
    assert meta.get("assets_dir") == f"p{pid_g}/assets"

    calls = _spy_parse(monkeypatch, cv)
    pid_d, _pdf_d = _upload(client, plan_id, "F-again.pdf", f_bytes)
    _run_paper(settings, pid_d, fp_f)
    d_digests = _asset_digests(settings.papers_dir / f"p{pid_d}" / "assets")

    assert len(calls) == 1, (
        f"一条无从核对的老行被当成了图片来源（parse_pdf 调用 {len(calls)} 次）—— "
        f"它按名字从 p{pid_g}/assets 复制，而那个目录现在装的是 G 的图")
    for name in names:
        assert d_digests.get(name) == f_digests[name], (
            f"{name} 不是 F 自己的图：这篇拿到 {d_digests.get(name)}，"
            f"F 的是 {f_digests[name]}（G 在同名文件里是 {g_digests[name]}）")
    r = client.get(f"/papers/{pid_d}/assets/{names[0]}")
    assert r.status_code == 200, f"供图端点返回 {r.status_code}"
    assert hashlib.sha256(r.content).hexdigest() == f_digests[names[0]], \
        "供图端点给出的不是 F 自己的图 —— 用户看到的是别人的插图"
    assert _cache_meta(settings, fp_f).get("asset_digests"), (
        "这一篇重解析之后，行里仍然没有图片指纹 —— 下一次命中它的人还是无从核对")


def test_a_tampered_source_asset_is_not_copied_into_another_paper(
        client, settings, make_user, monkeypatch):
    """⭐ 指纹要**真的比对**：源目录里的字节被换掉之后，不许再按名字复制。

    与上一个用例分工：那个查"行里有没有指纹"，这个查"指纹有没有被拿来比" ——
    只把指纹写进去、命中时却不比，这条是红的。
    """
    from papershelf.server import converter as cv

    plan_id = _prepare(client, settings, make_user, monkeypatch, cv)
    f_bytes, _g_bytes = _two_pdf_bytes(settings)

    pid_f, pdf_f = _upload(client, plan_id, "F.pdf", f_bytes)
    fp_f = _fp(pdf_f)
    doc_f = _run_paper(settings, pid_f, fp_f)
    names = sorted(_referenced_names(doc_f))
    assert names, "F 没有引用任何图片 —— 夹具没跑通"
    f_digests = _asset_digests(settings.papers_dir / f"p{pid_f}" / "assets")

    # 源目录里的字节被换掉：指针对、名字对、内容不对（目录易主的另一种形状）
    tampered = settings.papers_dir / f"p{pid_f}" / "assets"
    for name in names:
        (tampered / name).write_bytes(b"not the figure this document was parsed with")

    calls = _spy_parse(monkeypatch, cv)
    pid_b, _pdf_b = _upload(client, plan_id, "F-again.pdf", f_bytes)
    _run_paper(settings, pid_b, fp_f)
    b_digests = _asset_digests(settings.papers_dir / f"p{pid_b}" / "assets")

    assert len(calls) == 1, (
        f"源文件的字节已经和指纹不符，却还是按名字复制了（parse_pdf 调用 {len(calls)} 次）")
    for name in names:
        assert b_digests.get(name) == f_digests[name], (
            f"{name} 拿到的是被换掉的那份字节，不是这篇文档解析时的那张图")


def test_a_same_named_leftover_in_the_papers_own_directory_is_overwritten(
        client, settings, make_user, monkeypatch):
    """⭐ 本篇目录里已有的同名文件必须被**覆盖**，不能当成"已经有了"。

    C 形状（cycle 56 的读法 C）：缓存行**干净**、指针**正确**、源是 F 自己的图，但这一篇
    的 `p<id>/assets` 里已经躺着一张同名的 G 的图 —— 上一任 `p<id>` 的残骸
    （删除时 `rmtree` 失败只记日志）。旧实现的 `if dst.is_file(): continue` 直接跳过它：
    0 次解析、不报错、不缺图，页面显示的是 G 的插图。
    """
    from papershelf.server import converter as cv

    plan_id = _prepare(client, settings, make_user, monkeypatch, cv)
    f_bytes, g_bytes = _two_pdf_bytes(settings)

    pid_f, pdf_f = _upload(client, plan_id, "F.pdf", f_bytes)
    fp_f = _fp(pdf_f)
    doc_f = _run_paper(settings, pid_f, fp_f)
    names = sorted(_referenced_names(doc_f))
    assert names, "F 没有引用任何图片 —— 夹具没跑通"
    f_digests = _asset_digests(settings.papers_dir / f"p{pid_f}" / "assets")

    pid_g, pdf_g = _upload(client, plan_id, "G.pdf", g_bytes)
    _run_paper(settings, pid_g, _fp(pdf_g))
    g_digests = _asset_digests(settings.papers_dir / f"p{pid_g}" / "assets")
    assert any(f_digests[n] != g_digests[n] for n in names), \
        "两份 PDF 的插图字节相同 —— 夹具不成立"

    # 这一篇自己的目录里已经有残骸：同名、字节是 G 的
    pid_d, _pdf_d = _upload(client, plan_id, "F-again.pdf", f_bytes)
    leftover = settings.papers_dir / f"p{pid_d}" / "assets"
    leftover.mkdir(parents=True, exist_ok=True)
    for name in names:
        (leftover / name).write_bytes((settings.papers_dir / f"p{pid_g}" / "assets" / name)
                                      .read_bytes())

    calls = _spy_parse(monkeypatch, cv)
    _run_paper(settings, pid_d, fp_f)
    d_digests = _asset_digests(leftover)

    assert not calls, (
        f"缓存行是干净的、字节也对得上，不该重解析（parse_pdf 调用 {len(calls)} 次）")
    for name in names:
        assert d_digests.get(name) == f_digests[name], (
            f"{name} 用的还是残骸：这篇拿到 {d_digests.get(name)}，"
            f"F 的是 {f_digests[name]}（残骸里是 G 的 {g_digests[name]}）—— "
            "名字一样、字节不同，跳过复制就等于把上一任的图当成这一篇的")
    r = client.get(f"/papers/{pid_d}/assets/{names[0]}")
    assert r.status_code == 200
    assert hashlib.sha256(r.content).hexdigest() == f_digests[names[0]], \
        "供图端点把残骸发出去了"


def test_a_verified_cache_hit_still_costs_no_parse(
        client, settings, make_user, monkeypatch):
    """对照组：指纹对得上就**不许**重解析 —— 否则这个修复等于取消缓存复用。"""
    from papershelf.server import converter as cv

    plan_id = _prepare(client, settings, make_user, monkeypatch, cv)
    f_bytes, _g_bytes = _two_pdf_bytes(settings)

    pid_f, pdf_f = _upload(client, plan_id, "F.pdf", f_bytes)
    fp_f = _fp(pdf_f)
    doc_f = _run_paper(settings, pid_f, fp_f)
    names = sorted(_referenced_names(doc_f))
    f_digests = _asset_digests(settings.papers_dir / f"p{pid_f}" / "assets")
    assert names and f_digests, "夹具没跑通"

    calls = _spy_parse(monkeypatch, cv)
    pid_b, _pdf_b = _upload(client, plan_id, "F-again.pdf", f_bytes)
    _run_paper(settings, pid_b, fp_f)
    b_digests = _asset_digests(settings.papers_dir / f"p{pid_b}" / "assets")

    assert not calls, (
        f"指纹对得上却重解析了（parse_pdf 调用 {len(calls)} 次）—— 缓存复用被废掉了")
    for name in names:
        assert b_digests.get(name) == f_digests[name], f"{name} 的字节与第一篇不一致"
    assert (settings.papers_dir / f"p{pid_b}" / "assets" / names[0]).is_file()
