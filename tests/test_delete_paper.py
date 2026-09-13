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

    assert client.delete(f"/api/papers/{pid}").status_code == 204
    assert not pdf.exists(), "落盘 PDF 未删除"
    assert not (settings.papers_dir / f"p{pid}").exists(), "图片资产目录未删除"


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
