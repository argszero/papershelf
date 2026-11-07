"""删除文献（2026-09-12 宿主：「导入的文献，应该支持删除」）。

`DELETE /api/papers/{id}` 早就有了（决策里从没提过，前端也从没接过线），
所以这一轮补的是**前端入口** + 两处后端行为：

1. **磁盘产物必须一起收**：DB 侧有外键级联，但 PDF 与 `papers_dir/p<id>/assets/`
   是裸文件，不收就留下孤儿；更糟的是**重新导入同一篇时解析缓存会命中旧产物**，
   表现为"删了但没删干净"。
2. **删除后不得留下"能点进去的入口"**：阅读器/分享里已删文献必须 404，而不是 500。
"""

from __future__ import annotations

import io
import logging


def _seed(client, settings, make_user, *, title="要删的文献"):
    """登录 + 建计划 + 上传一份假 PDF，返回 (paper_id, plan_id, pdf_path)。"""
    from papershelf.server.db import connect

    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    up = client.post(f"/api/plans/{plan_id}/papers/upload",
                     files={"files": (f"{title}.pdf", io.BytesIO(b"%PDF-1.4\n%fake"), "application/pdf")},
                     data={"background_convert": "false"})
    assert up.status_code == 201, up.text
    pid = up.json()[0]["id"]
    conn = connect(settings)
    try:
        pdf_path = conn.execute("SELECT pdf_path FROM papers WHERE id=?", (pid,)).fetchone()[0]
    finally:
        conn.close()
    return pid, plan_id, pdf_path


def test_delete_removes_paper_and_cascades(client, settings, make_user):
    """行没了、列表里也没了（`docs`/`blocks`/`notes` 靠外键级联）。"""
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc
    from papershelf.pipeline import Doc

    pid, plan_id, _ = _seed(client, settings, make_user)
    conn = connect(settings)
    try:
        save_doc(conn, pid, Doc(meta={}, blocks=[]))          # 造一份译文
        conn.execute("INSERT INTO notes (paper_id,content,created_at) VALUES (?,?,datetime('now'))",
                     (pid, "笔记"))
        conn.commit()
    finally:
        conn.close()

    assert client.delete(f"/api/papers/{pid}").status_code == 204
    conn = connect(settings)
    try:
        for table in ("papers", "docs", "blocks", "notes"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {'id' if table == 'papers' else 'paper_id'}=?",
                             (pid,)).fetchone()[0]
            assert n == 0, f"{table} 未清理"
    finally:
        conn.close()
    assert all(p["id"] != pid for p in client.get(f"/api/plans/{plan_id}/papers").json())


def test_delete_removes_files_on_disk(client, settings, make_user):
    """**磁盘产物必须一起收** —— 否则重新导入同一篇会命中旧的解析缓存。"""
    from pathlib import Path

    pid, _, pdf_path = _seed(client, settings, make_user)
    pdf = Path(pdf_path)
    assert pdf.exists()
    assets = settings.papers_dir / f"p{pid}" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "fig1.png").write_bytes(b"fake")
    # v12：整页旋转的**转正副本**也落在 `p<id>/` 下（`normalized.pdf`）——
    # 它正是为此放在这儿：整目录 `rmtree` 一次收干净，不会留孤儿。
    (settings.papers_dir / f"p{pid}" / "normalized.pdf").write_bytes(b"fake")

    assert client.delete(f"/api/papers/{pid}").status_code == 204
    assert not pdf.exists(), "落盘 PDF 未删除"
    assert not (settings.papers_dir / f"p{pid}").exists(), "图片资产目录（含转正副本）未删除"


def test_delete_missing_or_foreign_paper_is_404(client, settings, make_user):
    """别人的文献 / 不存在的 id —— 都必须是 404（不能泄露"这个 id 存在"）。"""
    pid, _, _ = _seed(client, settings, make_user)
    _, pw2 = make_user("other@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": "other@tsinghua.edu.cn", "password": pw2})

    assert client.delete(f"/api/papers/{pid}").status_code == 404
    assert client.delete("/api/papers/999999").status_code == 404


def test_deleted_paper_is_not_readable(client, settings, make_user):
    """删完之后阅读器取数必须 404（不能 500，也不能给出半份数据）。"""
    pid, _, _ = _seed(client, settings, make_user)
    assert client.get(f"/api/papers/{pid}/doc").status_code in (200, 409)
    assert client.delete(f"/api/papers/{pid}").status_code == 204
    assert client.get(f"/api/papers/{pid}/doc").status_code == 404


def test_delete_while_queued_does_not_resurrect(client, settings, make_user):
    """**排队中删除**：队列不得把已删的篇目再写回来（`convert_paper` 查不到行就返回）。

    队列线程是常驻的，删除与"被捡起"是并发事件 —— 若 `convert_paper` 不复查行是否存在，
    会往已删的 paper_id 上写 blocks（外键立刻炸），或更糟：把行"复活"成 doing。
    """
    from papershelf.server.db import connect
    from papershelf.server.queue import pending
    from papershelf.server.converter import convert_paper

    pid, _, _ = _seed(client, settings, make_user)          # 上传即 queued
    assert client.delete(f"/api/papers/{pid}").status_code == 204

    conn = connect(settings)
    try:
        assert all(p["id"] != pid for p in pending(conn)), "已删文献仍在待认领队列里"
    finally:
        conn.close()
    convert_paper(pid)                                     # 直接触发，必须静默返回
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM papers WHERE id=?", (pid,)).fetchone()[0] == 0
    finally:
        conn.close()


# ── 每篇日志 `logs/p<id>.log` 也必须跟着文献一起被删（2026-11-06 实测）─────────
#
# 病灶两条（`tmp/recon47_area2.py` / `tmp/recon47_ids.py`，两支脚本都未改一行）：
# ①`_remove_paper_files` 只收 PDF 与 `papers/p<id>/`，日志原样留在盘上（实测 4,626 B）；
# ②`papers.id` 是 `INTEGER PRIMARY KEY`（无 `AUTOINCREMENT`）⇒ 删掉最大 id 后下一篇
#   复用同一个号，而日志是**追加**打开 ⇒ 新篇的时间线接在死篇后面。
# 修法 = 让日志跟着它描述的那一行一起消失（不动表、不加 `AUTOINCREMENT`）。

def _log_of(settings, pid):
    return settings.logs_dir / f"p{pid}.log"


def _write_log(settings, pid):
    """按产品路径写一次每篇日志 —— `paper_log` 就是生产上挂这个文件的那一处。"""
    from papershelf.server.logging_setup import paper_log

    with paper_log(pid, settings.logs_dir):
        logging.getLogger("papershelf.converter").info("paper=%s 开始转换（第 1 次尝试）", pid)
    return _log_of(settings, pid)


def _fail_one_round(settings, pid, *, pdf_name="gone.pdf"):
    """真跑一轮**必然失败**的转换（零 token、不联网），返回日志文件路径。

    走的是产品路径（认领 → `paper_log` 挂载 → `_run` 抛 → 落 `failed`）；失败点在
    `_run` 开头那条检查上，所以只花日志、不花钱、不碰解析与模型。
    """
    from papershelf.server.converter import convert_paper
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE papers SET pdf_path=?, conv_state='queued', conv_attempts=0 WHERE id=?",
            (str(settings.papers_dir / pdf_name), pid))
        conn.commit()
    finally:
        conn.close()
    convert_paper(pid)
    return _log_of(settings, pid)


def test_delete_removes_the_per_paper_log(client, settings, make_user):
    """**删了就得没有**：`logs/p<id>.log` 也是这篇文献名下的文件。

    原先它不在 `_remove_paper_files` 的清单里 —— 删一篇文献后日志原样在盘上，
    而它还会被复用同一个 id 的新篇继续追加（见下一条用例）。
    """
    from pathlib import Path

    pid, _, pdf_path = _seed(client, settings, make_user)
    assets = settings.papers_dir / f"p{pid}" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "fig1.png").write_bytes(b"fake")
    log_file = _write_log(settings, pid)
    assert log_file.is_file() and log_file.stat().st_size > 0

    assert client.delete(f"/api/papers/{pid}").status_code == 204
    assert not log_file.exists(), "每篇日志没有跟着文献一起被收掉 —— 它会越过删除活在盘上"
    assert not (settings.papers_dir / f"p{pid}").exists()
    assert not Path(pdf_path).exists()


def test_a_deleted_papers_log_is_not_inherited_by_a_reused_id(client, settings, make_user):
    """删掉一篇之后，**复用同一个 id 的新篇必须从空日志开始**。

    实测（脚本 `tmp/recon47_ids.py`）：一篇失败 3 轮 → 875 B；删掉 → 下一篇又拿到
    同一个 id → 它第一轮就让日志变成 1,752 B、**两条**「开始转换」，并带着已删那篇的
    PDF 名。判据落在**行数**与**旧篇的指纹**上：不是"日志该不该更长"，而是"这个文件
    还在讲别人的事"。
    """
    from papershelf.server.db import connect

    # 死篇那轮的日志里必须留一个**属于它的**指纹：`convert_paper` 的开始行会印
    # `PDF=<pdf_path 的文件名>`，所以这一轮指向一个自有名字的路径。
    old_marker = "old-paper.pdf"
    pid, plan_id, _ = _seed(client, settings, make_user, title="上一篇")
    log_file = _fail_one_round(settings, pid, pdf_name=old_marker)
    text = log_file.read_text(encoding="utf-8")
    assert text.count("开始转换") == 1, "夹具没跑通：一次失败转换应当只有一条「开始转换」"
    assert old_marker in text, "夹具没跑通：日志里应当有这一篇的 PDF 名"

    assert client.delete(f"/api/papers/{pid}").status_code == 204
    assert not log_file.exists()

    # 复用同一个 id —— 「删掉的正是最大 id」时 SQLite 就是这么分配下一个号的，这里写明它。
    conn = connect(settings)
    try:
        had = conn.execute("SELECT COUNT(*) FROM papers WHERE id=?", (pid,)).fetchone()[0]
        assert had == 0
        conn.execute(
            """INSERT INTO papers (id, plan_id, title, source, status, conv_state, pdf_path)
               VALUES (?,?,?,?,?,?,?)""",
            (pid, plan_id, "下一篇", "upload", "unread", "queued",
             str(settings.papers_dir / "next.pdf")))
        conn.commit()
    finally:
        conn.close()

    fresh = _fail_one_round(settings, pid, pdf_name="next.pdf")
    text = fresh.read_text(encoding="utf-8")
    assert text.count("开始转换") == 1, (
        f"新篇的日志里有 {text.count('开始转换')} 条「开始转换」—— 它在往死篇的日志里追加")
    assert old_marker not in text, "新篇的日志里带着已删那篇的 PDF 名（两篇共用了一个文件）"


def test_deleting_a_plan_reclaims_every_papers_files(client, settings, make_user):
    """删计划 = 删它名下的每篇文献，**磁盘产物一篇都不能留**。

    原先这条路径只 `DELETE FROM plans`（级联到 papers/docs/blocks/notes/shares），
    **一个文件都不收**：库里干净、盘上 PDF / 资产目录 / 每篇日志原样留着。
    判据是"零文件"—— 那个计划名下不再有任何一篇的产物。
    """
    from pathlib import Path

    from papershelf.server.db import connect

    email, pw = make_user("plan-owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "整计划删"}).json()["id"]
    pids = []
    for name in ("甲.pdf", "乙.pdf"):
        up = client.post(f"/api/plans/{plan_id}/papers/upload",
                         files={"files": (name, io.BytesIO(b"%PDF-1.4\n%fake"), "application/pdf")},
                         data={"background_convert": "false"})
        assert up.status_code == 201, up.text
        pids.append(int(up.json()[0]["id"]))
    conn = connect(settings)
    try:
        pdfs = [conn.execute("SELECT pdf_path FROM papers WHERE id=?", (p,)).fetchone()[0]
                for p in pids]
    finally:
        conn.close()
    for pid in pids:
        assets = settings.papers_dir / f"p{pid}" / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "fig1.png").write_bytes(b"fake")
        _write_log(settings, pid)
        assert _log_of(settings, pid).is_file()

    assert client.delete(f"/api/plans/{plan_id}").status_code == 204
    for pid in pids:
        assert not _log_of(settings, pid).exists(), f"p{pid} 的日志没被收掉"
        assert not (settings.papers_dir / f"p{pid}").exists(), f"p{pid} 的资产目录没被收掉"
    for p in pdfs:
        assert not Path(p).exists(), "落盘 PDF 没被收掉"
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM papers WHERE plan_id=?",
                            (plan_id,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_delete_leaves_the_shared_parse_cache_row_alone(client, settings, make_user):
    """`doc_cache` **刻意不跟着删** —— 它是"按 PDF 字节"的内容缓存，**不按篇**。

    这一格不是漏掉的：缓存键 = `sha256("parse-v{N}" + PDF 字节)`，全库一个命名空间
    （待定项 7：同一份 PDF 只解析一次）⇒ 两篇共用一行是常态，删 A 的行会连带打掉 B 的
    命中；而"删了但没删干净"那条老担心已由 `_materialise_cached_assets` 处理
    （源目录不在就为这一篇就地重解析，见 `tests/test_parse_cache_assets.py`），
    显式失效的出口是「重新提取」（`DELETE FROM doc_cache`）。

    把"故意不删"写成可执行的记录：将来谁要改这个口径，会先在这里看到理由。
    """
    from pathlib import Path

    from papershelf.server.db import connect
    from papershelf.server.routers.papers import _pdf_fingerprint

    pid, _, pdf_path = _seed(client, settings, make_user)
    fp = _pdf_fingerprint(Path(pdf_path))
    assert fp, "夹具没跑通：落盘 PDF 应当能算出指纹"
    conn = connect(settings)
    try:
        conn.execute("INSERT OR REPLACE INTO doc_cache (fingerprint, doc_json, created_at) "
                     "VALUES (?,?,datetime('now'))", (fp, "{}"))
        conn.commit()
    finally:
        conn.close()

    assert client.delete(f"/api/papers/{pid}").status_code == 204
    conn = connect(settings)
    try:
        left = conn.execute("SELECT COUNT(*) FROM doc_cache WHERE fingerprint=?",
                            (fp,)).fetchone()[0]
    finally:
        conn.close()
    assert left == 1, "doc_cache 行被跟着删了 —— 那会打掉共用同一份 PDF 的另一篇的缓存命中"
