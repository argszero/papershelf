"""「修复插图」= 把磁盘上缺掉的图片文件补回来，**一个块都不改**（存量出口，零 token）。

病灶（cycle 22 实测，`#7` `84e94bc` 之后仍在）：

- `#7` 让「命中解析缓存时把资产搬进本篇目录」成立，但那段搬运**只在整条转换里跑**
  （`converter._run`，全仓库唯一调用点）⇒ 已经 `done` 的篇目**永远不会再进它**；
- 于是 `p<id>/assets/` 里少一张图就是**永久 404**，而现存三条出口没有一条能把文件放回去：
  `/convert` 整篇重跑（百万级 token，且不补图）、`/reextract` 会
  `DELETE FROM notes/highlights`、`/rebuild-refs` 只动文末文献段。

可发生面不是"新用户"：`#7` 之前转换的存量篇目、`p<id>/assets/` 被磁盘清理或恢复丢失、
以及**同源那篇被删文献**（整目录被收走，`doc_cache` 行却还在）。

本文件钉住四条不变量（顺序就是风险从大到小）：

1. **批注一条不动、一个字节不变** —— 这条出口的全部价值就在这里；它是**结构性**成立的：
   本操作从不写 `docs` / `blocks` / `notes` / `highlights`，只写文件；
2. **零 token** —— 插图是磁盘文件，这条路径一次模型调用都不发；
3. **幂等** —— 插图齐全时再点一次是 no-op（不复制、不重新解析）；
4. **不猜** —— 来源目录没了就必须先证明"重新解析出来的是同一篇"（块 id + 原文逐字相同），
   证明不了就放弃并点名「重新提取」，绝不把别人的同名图安到这篇上（`#8` 同源的失败模式）。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 一张最小 PNG（内容不重要：这里量的是"文件在不在、端点回不回 200"）
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ── 夹具 ────────────────────────────────────────────────────────────────
def _done_paper(client, make_user, *, conv_state: str = "done") -> int:
    """登录 + 建计划 + 上传一份假 PDF（不转换），返回 paper_id。"""
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect, tx

    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    up = client.post(f"/api/plans/{plan_id}/papers/upload",
                     files={"files": ("目标文献.pdf", io.BytesIO(b"%PDF-1.4\n%fake"),
                                      "application/pdf")},
                     data={"background_convert": "false"})
    assert up.status_code == 201, up.text
    pid = int(up.json()[0]["id"])

    conn = connect(get_settings())
    try:
        with tx(conn):
            conn.execute("UPDATE papers SET conv_state=? WHERE id=?", (conv_state, pid))
    finally:
        conn.close()
    return pid


def _store_doc(pid: int, *, meta: dict, figures: list[str], extra_text: str = ""):
    """把一份产物直接写进库：两块正文 + 每张图一个 `figure` 块。返回 Doc。

    直接落库（而不是跑真解析）是刻意的：本文件量的是"缺文件能不能补回来"，
    与解析器无关；跑真解析只会让夹具变慢、还要额外桩掉 ①c/翻译。
    """
    from papershelf.pipeline.model import Block, Doc
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    blocks = [
        Block(id="b-0001", type="p", en="Opening paragraph of the paper.",
              zh="论文的开篇段落。", zh_source="mt", section="1"),
        Block(id="b-0002", type="p", en="Second paragraph." + extra_text,
              zh="第二段。", zh_source="mt", section="1"),
    ]
    for i, name in enumerate(figures, start=3):
        blocks.append(Block(id=f"b-{i:04d}", type="figure", en="Fig. 1. A caption.",
                            zh="图 1. 一条图注。", zh_source="mt", section="1",
                            payload={"src": f"assets/{name}", "page": 1}))
    doc = Doc(meta=dict(meta), assets=[], blocks=blocks)
    conn = connect(get_settings())
    try:
        save_doc(conn, pid, doc)
    finally:
        conn.close()
    return doc


def _put_source_asset(settings, rel_dir: str, name: str) -> Path:
    """造出"来源目录里的那张图"（模拟同源那一篇的目录还在）。"""
    src = settings.papers_dir / rel_dir
    src.mkdir(parents=True, exist_ok=True)
    path = src / name
    path.write_bytes(_PNG)
    return path


def _annotate(pid: int) -> None:
    """一条笔记 + 一道划痕（用户真正在乎的那两样）。"""
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect, tx

    conn = connect(get_settings())
    try:
        with tx(conn):
            conn.execute("INSERT INTO notes (paper_id, block_id, content, created_at) "
                         "VALUES (?,?,?,datetime('now'))", (pid, "b-0001", "我的笔记"))
            conn.execute("INSERT INTO highlights "
                         "(paper_id, block_id, lang, start, end, color, created_at) "
                         "VALUES (?,?,?,?,?,?,datetime('now'))",
                         (pid, "b-0001", "zh", 0, 6, "amber"))
    finally:
        conn.close()


def _annotations(pid: int) -> dict[str, list[dict]]:
    """批注的**全字段**快照（逐字段比对，不只看行数）。"""
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect

    conn = connect(get_settings())
    try:
        return {t: [dict(r) for r in conn.execute(
            f"SELECT * FROM {t} WHERE paper_id=? ORDER BY id", (pid,)).fetchall()]
            for t in ("notes", "highlights")}
    finally:
        conn.close()


def _state(pid: int) -> dict:
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect

    conn = connect(get_settings())
    try:
        return dict(conn.execute(
            "SELECT conv_state, conv_error, tokens_used, pending_job FROM papers WHERE id=?",
            (pid,)).fetchone())
    finally:
        conn.close()


def _set_pending(pid: int, job: str | None) -> None:
    """模拟"上一轮操作留下的残留 job"（`pending_job` 是跨请求存活的）。"""
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect, tx

    conn = connect(get_settings())
    try:
        with tx(conn):
            conn.execute("UPDATE papers SET pending_job=? WHERE id=?", (job, pid))
    finally:
        conn.close()


def _spy_parse(monkeypatch) -> list:
    """记录 `assetsfix.parse_pdf` 被调用了几次 —— 钉住"齐全时不该重新解析"。"""
    from papershelf.server import assetsfix as af

    calls: list = []

    def _spy(*a, **k):
        calls.append((a, k))
        raise AssertionError("插图齐全 / 或本用例不允许重新解析，却调用了 parse_pdf")

    monkeypatch.setattr(af, "parse_pdf", _spy)
    return calls


def _asset_url(pid: int, name: str) -> str:
    """资产路由挂在 `app` 上，不在 `/api` 前缀下。"""
    return f"/papers/{pid}/assets/{name}"


# ── ① 主路径：来源目录还在，把图补回来 ──────────────────────────────────
def test_rebuild_assets_restores_a_missing_figure_and_keeps_annotations(
        client, settings, make_user):
    """⭐ 缺一张图 → 端点 → 文件回来、端点 200、批注逐字段不变、零 token。"""
    pid = _done_paper(client, make_user)
    sibling = 4242                              # 「同源那一篇」：只借它的目录
    _put_source_asset(settings, f"p{sibling}/assets", "p1_img1.png")
    _store_doc(pid, meta={"assets_dir": f"p{sibling}/assets"}, figures=["p1_img1.png"])

    url = _asset_url(pid, "p1_img1.png")
    assert client.get(url).status_code == 404, "前提不成立：图还在，就没有这个病灶"

    _annotate(pid)
    before = _annotations(pid)
    tokens_before = _state(pid)["tokens_used"]

    r = client.post(f"/api/papers/{pid}/rebuild-assets")
    assert r.status_code == 202, r.text
    assert r.json()["job"] == "assets", "排队时没有声明要跑的是「修复插图」"

    assert (settings.papers_dir / f"p{pid}" / "assets" / "p1_img1.png").is_file(), \
        "文件没有落进这一篇自己的目录（供图端点只认 p<pid>/assets）"
    assert client.get(url).status_code == 200, "修完之后用户看到的还是一张裂图"
    assert _annotations(pid) == before, (
        "笔记/划痕变了 —— 这条出口的全部价值就是「批注一条不动」"
        "（本操作从不写 docs/blocks/notes/highlights，只写文件）"
    )
    st = _state(pid)
    assert st["conv_state"] == "done"
    assert st["tokens_used"] == tokens_before, \
        "修复插图花了 token —— 插图是磁盘文件，这条路径不该发任何模型调用"


# ── ② 幂等：齐全时什么都不做，更不重新解析 ──────────────────────────────
def test_rebuild_assets_is_idempotent_and_never_reparses(client, settings, make_user,
                                                         monkeypatch):
    """再点一次是 no-op：不复制、不重新解析（真的重解析会烧掉几秒 CPU 并可能动库）。"""
    pid = _done_paper(client, make_user)
    _put_source_asset(settings, "p4242/assets", "p1_img1.png")
    _store_doc(pid, meta={"assets_dir": "p4242/assets"}, figures=["p1_img1.png"])
    assert client.post(f"/api/papers/{pid}/rebuild-assets").status_code == 202

    after_first = (settings.papers_dir / f"p{pid}" / "assets" / "p1_img1.png").read_bytes()
    calls = _spy_parse(monkeypatch)             # 任何一次解析都会 AssertionError

    assert client.post(f"/api/papers/{pid}/rebuild-assets").status_code == 202
    assert not calls, "插图已经齐全，却又重新解析了一遍"
    assert (settings.papers_dir / f"p{pid}" / "assets" / "p1_img1.png").read_bytes() \
        == after_first, "第二次调用改动了已有的文件（应当只补缺的、不覆盖）"
    assert _state(pid)["conv_state"] == "done"


# ── ③ 来源目录没了：必须先证明是同一篇 ─────────────────────────────────
def _fresh_doc_like(stored, *, extra_block: bool = False, changed_text: bool = False):
    """照库里那份产物造一份"重新解析的结果"（块 id 与原文一致）。"""
    from papershelf.pipeline.model import Block, Doc

    blocks = [
        Block(id=b.id, type=b.type,
              en=(b.en + " (reflowed)" if changed_text and b.type == "p" else b.en),
              payload=dict(b.payload or {}))
        for b in stored.blocks
    ]
    if extra_block:
        blocks.append(Block(id="b-0099", type="p", en="A paragraph the old parse never had."))
    return Doc(meta=dict(stored.meta), assets=[], blocks=blocks)


def test_rebuild_assets_reparses_when_the_source_is_gone_and_identity_holds(
        client, settings, make_user, monkeypatch):
    """同源那篇被删（来源目录整个没了）→ 就地重新解析，块 id 与原文一致才敢搬图。"""
    from papershelf.server import assetsfix as af

    pid = _done_paper(client, make_user)
    stored = _store_doc(pid, meta={"assets_dir": "p4242/assets"}, figures=["p1_img1.png"])
    assert not (settings.papers_dir / "p4242").exists(), "来源目录不该存在（这就是病灶）"

    def fake_parse(pdf_path, assets_dir=None, **kw):
        out = Path(assets_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "p1_img1.png").write_bytes(_PNG)
        return _fresh_doc_like(stored)

    monkeypatch.setattr(af, "parse_pdf", fake_parse)

    _annotate(pid)
    before = _annotations(pid)
    assert client.post(f"/api/papers/{pid}/rebuild-assets").status_code == 202

    assert client.get(_asset_url(pid, "p1_img1.png")).status_code == 200
    assert _annotations(pid) == before
    assert _state(pid)["conv_state"] == "done"
    assert not (settings.papers_dir / f"p{pid}" / "reparse").exists(), \
        "重新解析用的临时目录没有收拾掉（会一直留在 p<id>/ 里）"


@pytest.mark.parametrize("kwargs, why", [
    ({"changed_text": True}, "块原文变了"),
    ({"extra_block": True}, "块序列变了"),
])
def test_rebuild_assets_declines_when_the_reparse_does_not_match(
        client, settings, make_user, monkeypatch, kwargs, why):
    """证明不了是同一篇就**放弃**：绝不把同名文件当成"这张图"安上去。"""
    from papershelf.server import assetsfix as af

    pid = _done_paper(client, make_user)
    stored = _store_doc(pid, meta={"assets_dir": "p4242/assets"}, figures=["p1_img1.png"])

    def fake_parse(pdf_path, assets_dir=None, **kw):
        out = Path(assets_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "p1_img1.png").write_bytes(_PNG)
        return _fresh_doc_like(stored, **kwargs)

    monkeypatch.setattr(af, "parse_pdf", fake_parse)

    _annotate(pid)
    before = _annotations(pid)
    assert client.post(f"/api/papers/{pid}/rebuild-assets").status_code == 202

    st = _state(pid)
    assert st["conv_state"] == "failed", f"{why}却仍然报了成功（用户会以为已经修过了）"
    assert "重新提取" in (st["conv_error"] or ""), \
        f"没有点名下一步该点哪个按钮（{why}）：{st['conv_error']}"
    assert not (settings.papers_dir / f"p{pid}" / "assets" / "p1_img1.png").exists(), \
        f"{why}时仍然把文件搬过去了 —— 那可能是别人的同名图"
    assert client.get(_asset_url(pid, "p1_img1.png")).status_code == 404
    assert _annotations(pid) == before, "放弃修复时不该动批注"


def test_rebuild_assets_declines_without_a_source_or_a_pdf(client, settings, make_user):
    """来源目录没了、PDF 也没了 ⇒ 明确放弃（不解析、不复制），并说清下一步。"""
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect, tx

    pid = _done_paper(client, make_user)
    _store_doc(pid, meta={"assets_dir": "p4242/assets"}, figures=["p1_img1.png"])

    conn = connect(get_settings())
    try:
        with tx(conn):
            conn.execute("UPDATE papers SET pdf_path=NULL WHERE id=?", (pid,))
    finally:
        conn.close()

    assert client.post(f"/api/papers/{pid}/rebuild-assets").status_code == 202
    st = _state(pid)
    assert st["conv_state"] == "failed"
    assert "重新提取" in (st["conv_error"] or ""), st["conv_error"]
    assert not (settings.papers_dir / f"p{pid}" / "assets" / "p1_img1.png").exists()


# ── ④ 状态与权限：没有产物就先 409，别排一趟空队 ────────────────────────
def test_rebuild_assets_409_before_there_is_a_product(client, settings, make_user):
    """还没转换完就别排队（重活留给队列，能立刻判定的错当场说清）。"""
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect, tx

    pid = _done_paper(client, make_user, conv_state="none")
    r = client.post(f"/api/papers/{pid}/rebuild-assets")
    assert r.status_code == 409, r.text
    assert "没有可修复的产物" in r.json()["detail"]

    conn = connect(get_settings())
    try:
        with tx(conn):
            conn.execute("UPDATE papers SET conv_state='doing' WHERE id=?", (pid,))
    finally:
        conn.close()
    r = client.post(f"/api/papers/{pid}/rebuild-assets")
    assert r.status_code == 409
    assert "正在转换" in r.json()["detail"]


def test_rebuild_assets_missing_or_foreign_paper_is_404(client, make_user):
    """别人的文献 / 不存在的 id —— 一律 404（不泄露"这个 id 存在"）。"""
    pid = _done_paper(client, make_user)
    _, pw2 = make_user("other@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": "other@tsinghua.edu.cn", "password": pw2})

    assert client.post(f"/api/papers/{pid}/rebuild-assets").status_code == 404
    assert client.post("/api/papers/999999/rebuild-assets").status_code == 404


# ── ⑤ `pending_job` 是跨请求存活的：残留值不许劫持下一次操作 ─────────────
def test_a_stale_refs_job_does_not_hijack_the_assets_repair(client, settings, make_user):
    """上一轮「重建参考文献」留下的 `job='refs'` 不能把这次「修复插图」变成修文献。

    这是 ㊹修订二 记下的那条硬约束（"所有排队入口必须显式声明 job"）在本入口上的验证：
    若漏声明，用户点的是"修复插图"，跑的是别的事 —— 静默且难查。
    """
    pid = _done_paper(client, make_user)
    _put_source_asset(settings, "p4242/assets", "p1_img1.png")
    _store_doc(pid, meta={"assets_dir": "p4242/assets"}, figures=["p1_img1.png"])
    _set_pending(pid, "refs")                   # ← 残留

    assert client.post(f"/api/papers/{pid}/rebuild-assets").status_code == 202
    assert (settings.papers_dir / f"p{pid}" / "assets" / "p1_img1.png").is_file(), \
        "残留的 job='refs' 把这次操作劫持成了修文献（图没补回来）"
    assert client.get(_asset_url(pid, "p1_img1.png")).status_code == 200
    assert _state(pid)["pending_job"] is None, "终态没有清掉 pending_job"


def test_a_stale_assets_job_does_not_hijack_a_full_conversion(client, make_user):
    """反过来也要成立：残留 `job='assets'` 不能让「重新转换」变成"只补图"。"""
    pid = _done_paper(client, make_user, conv_state="none")
    _set_pending(pid, "assets")                 # ← 残留

    assert client.post(f"/api/papers/{pid}/convert").status_code == 202
    err = _state(pid)["conv_error"] or ""
    assert "LLM" in err, (
        "「重新转换」没有真的去跑转换（被残留的 assets 任务劫持了）："
        f"conv_error={err!r}"
    )


# ── ⑥ 前端必须接线（本仓库已出现过两次"后端早有、前端从没接"）─────────────
def test_frontend_actually_wires_the_button():
    """api 里有封装、Literature 里真的调用它、按钮带自己的标记类。

    ⑲（元数据）与 ㉙（删除）都是同一个病灶：后端能改、前端无路可走 ⇒ 功能等于不存在。
    """
    api_ts = (ROOT / "web" / "src" / "api.ts").read_text(encoding="utf-8")
    lib_tsx = (ROOT / "web" / "src" / "pages" / "Library.tsx").read_text(encoding="utf-8")

    assert "rebuildAssets" in api_ts, "api.ts 没有 rebuildAssets 封装"
    assert "/rebuild-assets" in api_ts, "api.ts 里的路径不是 /rebuild-assets"
    assert "api.rebuildAssets(" in lib_tsx, "文献库从没调用 rebuildAssets（死入口）"
    assert "t-ast" in lib_tsx, "修复插图按钮缺少标记类 t-ast"
    assert "修复插图" in lib_tsx, "按钮文案没了（用户看不懂 t-ast 是什么）"
