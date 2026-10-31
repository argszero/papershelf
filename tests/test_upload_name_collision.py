"""落盘文件名撞车（2026-10-31，上传路径的并发/重复行为 Recon）。

原先 `papers_dir` 里的目标是 `f"{utcnow().replace(':', '')}_{name}"`，而 `utcnow()` 是
`isoformat(timespec="seconds")` —— **只到秒**。于是「同一 UTC 秒 + 同一个 basename」的两篇
拿到**同一条路径**，后一份先截断再写自己：实测同一秒内 6 次**串行**上传 `same.pdf`
→ **6 行 papers / 1 个文件**，文件里只剩最后一份的字节，前 5 行的 `pdf_path` 全指向它，
而 6 个请求**全部 201**、日志全绿。转换 / 导出 / 「重新提取」读的都是 `pdf_path`
⇒ 那几篇会**静默变成另一篇的副本**。

⚠️ 这**不是竞态**，所以本文件一个线程都不开、一次也不碰真实时钟：

1. 同一个请求里两个同名文件（`<input type="file" multiple>` 就能选出来）是**确定性**撞，
   连「同一秒」都不需要；
2. 两次请求撞在同一秒的情形，用 `monkeypatch` 把 `utcnow` 钉死来**构造**，而不是去跑
   真实时钟碰运气 —— 靠 `:xx` 边界才红的测试等于没有测试。

`utcnow` 要打在 `papershelf.server.routers.papers` 上：那个模块 `from ..db import utcnow`
是**导入时绑定**的，去 patch `db.utcnow` 完全打不到它（`tests/conftest.py` 顶部有同源提醒）。

判据全落在**可观测结果**上：返回的 `pdf_path`、盘上的文件、各自的字节、papers 行数。
"""

from __future__ import annotations

from pathlib import Path

# ── 小工具（与 tests/test_memory_bounds.py 同一形状：这里只读库，不写库） ──────

def _login_and_plan(client, make_user) -> int:
    email, pw = make_user("owner@tsinghua.edu.cn")
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return client.post("/api/plans", json={"name": "精读"}).json()["id"]


def _rows(settings, plan_id: int) -> list[dict]:
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, title, pdf_path FROM papers WHERE plan_id=? ORDER BY id",
            (plan_id,)).fetchall()]
    finally:
        conn.close()


def _files(settings) -> list[Path]:
    return sorted(settings.papers_dir.iterdir())


def _freeze_utcnow(monkeypatch, value: str = "2026-10-31T00:00:00+00:00") -> str:
    """把端点里的秒级时间戳钉死 —— 让「同一秒」成为**构造出来的**事实。"""
    monkeypatch.setattr("papershelf.server.routers.papers.utcnow", lambda: value)
    return value


def _set_upload_cap(monkeypatch, mb: int):
    monkeypatch.setenv("PAPERSHELF_MAX_UPLOAD_MB", str(mb))
    from papershelf.server.config import get_settings
    return get_settings(refresh=True)


# ── 1. 同一个请求里两个同名文件（确定性，不需要并发也不需要同一秒） ──────────

def test_two_same_named_files_in_one_request_keep_their_own_bytes(
        client, settings, make_user):
    """`multiple` 选两个同名 PDF：两条路径、两个文件、字节各归各的。

    这条是本次缺陷**最便宜的**触发路径：`web/src/pages/Plans.tsx` 的上传框就是
    `<input type="file" multiple>`，两个 `paper.pdf` 走一次请求，修复前第二个文件
    会覆盖第一个（201、两行、同一路径）。
    """
    plan_id = _login_and_plan(client, make_user)
    first = b"%PDF-1.4\n" + b"A" * 20000
    second = b"%PDF-1.4\n" + b"B" * 30000

    r = client.post(
        f"/api/plans/{plan_id}/papers/upload",
        files=[("files", ("paper.pdf", first, "application/pdf")),
               ("files", ("paper.pdf", second, "application/pdf"))],
        data={"background_convert": "false"},
    )

    assert r.status_code == 201, r.text
    rows = _rows(settings, plan_id)
    assert len(rows) == 2, rows
    paths = [Path(row["pdf_path"]) for row in rows]
    assert len(set(paths)) == 2, f"两篇文献指向同一个文件：{paths}"
    assert all(p.exists() for p in paths), [str(p) for p in paths]
    assert paths[0].read_bytes() == first, "第一个文件的字节被第二个覆盖了"
    assert paths[1].read_bytes() == second
    assert len(_files(settings)) == 2, [p.name for p in _files(settings)]


# ── 2. 两次请求撞在同一秒（用钉死的时间戳构造，不跑真实时钟） ────────────────

def test_two_uploads_of_the_same_name_in_the_same_second_do_not_share_a_file(
        monkeypatch, client, settings, make_user):
    """同一秒 + 同 basename 的**两次请求**：两条路径、两个文件，先上传的字节还在。

    修复前：两次都 201，`pdf_path` 完全相同，盘上只剩后一份 —— 第一行从此指向别人的 PDF。
    """
    plan_id = _login_and_plan(client, make_user)
    frozen = _freeze_utcnow(monkeypatch)
    first = b"%PDF-1.4\n" + b"first" * 4000
    second = b"%PDF-1.4\n" + b"second" * 5000

    for payload in (first, second):
        r = client.post(f"/api/plans/{plan_id}/papers/upload",
                        files={"files": ("same.pdf", payload, "application/pdf")},
                        data={"background_convert": "false"})
        assert r.status_code == 201, r.text

    rows = _rows(settings, plan_id)
    assert len(rows) == 2, rows
    paths = [Path(row["pdf_path"]) for row in rows]
    assert len(set(paths)) == 2, f"同一秒的两篇共用一条路径：{paths}"

    # 未撞名的那一份仍沿用原来的命名（<秒级时间戳>_<basename>），运维习惯不受影响
    stem = frozen.replace(":", "")
    assert paths[0].name == f"{stem}_same.pdf", paths[0].name
    assert paths[0].read_bytes() == first, "先上传那份的字节被后一份覆盖了"
    assert paths[1].read_bytes() == second
    assert len(_files(settings)) == 2, [p.name for p in _files(settings)]


# ── 3. 同秒的 413 不许删掉别人（修复的第二个出口） ──────────────────────────

def test_a_rejected_upload_does_not_delete_an_earlier_file_of_the_same_name(
        monkeypatch, client, settings, make_user):
    """同秒的第二个请求超限 413 ⇒ 只删它自己写的那半截，前一份完好无损。

    ⚠️ 这条是同一个根因的另一个出口：`except BaseException` 里的 `unlink` 打在
    「秒级时间戳 + basename」这条**共享**路径上，于是后一个请求 413 时会把前一个
    已经写好的文件一起删掉 —— 库里留着那一行，`pdf_path` 指向一个不存在的文件。
    """
    plan_id = _login_and_plan(client, make_user)
    _freeze_utcnow(monkeypatch)
    _set_upload_cap(monkeypatch, 1)
    good = b"%PDF-1.4\n" + b"good" * 2000

    r = client.post(f"/api/plans/{plan_id}/papers/upload",
                    files={"files": ("same.pdf", good, "application/pdf")},
                    data={"background_convert": "false"})
    assert r.status_code == 201, r.text
    kept = Path(_rows(settings, plan_id)[0]["pdf_path"])
    assert kept.exists() and kept.read_bytes() == good

    too_big = b"%PDF-1.4\n" + b"x" * (1024 * 1024)
    r = client.post(f"/api/plans/{plan_id}/papers/upload",
                    files={"files": ("same.pdf", too_big, "application/pdf")},
                    data={"background_convert": "false"})
    assert r.status_code == 413, r.text

    assert kept.exists(), "同秒另一个请求 413，把前一份已经写好的文件删掉了"
    assert kept.read_bytes() == good
    assert _files(settings) == [kept], [p.name for p in _files(settings)]
    assert len(_rows(settings, plan_id)) == 1
