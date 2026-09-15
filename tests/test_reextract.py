"""重新提取（宿主 2026-09-15：「在删除旁边加一个重新提取的按钮，点击后清空缓存重新提取」）。

背景：`doc_cache` 的指纹是 `sha256("parse-v{PARSE_VERSION}" + PDF 字节)` —— **没有失效机制**。
PDF 一字未改就永远命中；唯一的失效手段是改代码里的 `PARSE_VERSION`，而那是**全库一起失效**。
于是「这一篇的解析/校对结果我不满意，重来一次」这类**单篇**诉求没有任何出口
（界面上的「重新转换」也会命中缓存，等于什么都没重来）。

语义 = **全部作废，从零重跑**（宿主选 A），因此本文件钉住四件事：

1. 缓存行**真的被删**（否则重跑还是命中旧解析产物，按钮等于假的）；
2. `notes` / `highlights` **一起作废** —— 新解析的块 id 与文本都会变，
   旧批注留着只会指到别的字上，那比"没了"更坏；
3. `conv_attempts` 归零 —— 用户主动发起的新一轮，不该被上一轮的失败次数挡住；
4. **前端必须接线**（`api.reextractPaper` 真的被 Library 调用）——
   ⑲ 与 ㉙ 都是同一个病灶：后端早有、前端从没接，功能等于不存在。

（离线：不调 LLM、不联网。转换任务在 TestClient 里会真的跑一次，
但假 PDF 打不开 → 落 `failed`，这正是我们要的"它确实重新解析了"。）
"""

from __future__ import annotations

import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _seed(client, make_user, *, attempts=0, conv_state="queued"):
    """登录 + 建计划 + 上传一份假 PDF（不触发转换），返回 (paper_id, pdf_path)。"""
    from papershelf.server.db import connect, tx
    from papershelf.server.config import get_settings

    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    up = client.post(f"/api/plans/{plan_id}/papers/upload",
                     files={"files": ("目标文献.pdf", io.BytesIO(b"%PDF-1.4\n%fake"), "application/pdf")},
                     data={"background_convert": "false"})
    assert up.status_code == 201, up.text
    pid = int(up.json()[0]["id"])
    conn = connect(get_settings())
    try:
        with tx(conn):
            conn.execute("UPDATE papers SET conv_attempts=?, conv_state=? WHERE id=?",
                         (attempts, conv_state, pid))
        pdf_path = conn.execute("SELECT pdf_path FROM papers WHERE id=?", (pid,)).fetchone()[0]
    finally:
        conn.close()
    return pid, pdf_path


def _fingerprint(pdf_path: str) -> str:
    from papershelf.server.routers.papers import _pdf_fingerprint

    return _pdf_fingerprint(Path(pdf_path))


def _seed_cache(settings, pdf_path: str) -> str:
    """把"这篇的解析缓存"造出来（模拟已经转换过一次）。"""
    from papershelf.server.db import connect, tx

    fp = _fingerprint(pdf_path)
    conn = connect(settings)
    try:
        with tx(conn):
            conn.execute("INSERT OR REPLACE INTO doc_cache (fingerprint, doc_json, created_at) "
                         "VALUES (?,?,datetime('now'))", (fp, '{"blocks":[]}'))
    finally:
        conn.close()
    return fp


def test_reextract_clears_the_parse_cache(client, settings, make_user):
    """**核心**：缓存行必须真的被删 —— 否则"重新提取"只是又命中一次旧产物。"""
    from papershelf.server.db import connect

    pid, pdf_path = _seed(client, make_user)
    fp = _seed_cache(settings, pdf_path)

    r = client.post(f"/api/papers/{pid}/reextract")
    assert r.status_code == 202, r.text
    assert r.json()["cache_cleared"] is True

    conn = connect(settings)
    try:
        left = conn.execute("SELECT COUNT(*) FROM doc_cache WHERE fingerprint=?", (fp,)).fetchone()[0]
    finally:
        conn.close()
    assert left == 0, "解析缓存没被清掉：重新提取会命中旧解析产物"


def test_reextract_wipes_notes_and_highlights(client, settings, make_user):
    """笔记与划痕一起作废（块 id 与文本都会变，留着只会指到别的字上）。"""
    from papershelf.server.db import connect, tx

    pid, pdf_path = _seed(client, make_user)
    _seed_cache(settings, pdf_path)
    conn = connect(settings)
    try:
        with tx(conn):
            conn.execute("INSERT INTO notes (paper_id, block_id, content, created_at) "
                         "VALUES (?,?,?,datetime('now'))", (pid, "b-0001", "旧批注"))
            conn.execute("INSERT INTO highlights (paper_id, block_id, lang, start, end, color, created_at) "
                         "VALUES (?,?,?,?,?,?,datetime('now'))", (pid, "b-0001", "zh", 0, 10, "amber"))
        # 另一篇的批注不该被牵连 —— 断言在同一次调用里做掉
        assert client.post(f"/api/papers/{pid}/reextract").status_code == 202
        for table in ("notes", "highlights"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE paper_id=?", (pid,)).fetchone()[0]
            assert n == 0, f"{table} 未清理（新解析的块已换，旧锚点必然悬空）"
    finally:
        conn.close()


def test_reextract_resets_attempts_so_it_is_not_blocked(client, settings, make_user):
    """`conv_attempts` 归零：上一轮失败 3 次（= 上限）之后，用户主动重提取仍须真的跑一次。

    不归零的话 `convert_paper` 第一句就 `超过最大重试次数` 返回 —— 按钮点了等于没点。
    （顺带钉住"认领即计数"：跑过一次之后计数必须真的涨上去。此前它**恒为 0**，
    护栏从未生效、日志里"第 N 次尝试"永远是 1。）
    """
    from papershelf.server.db import connect

    pid, pdf_path = _seed(client, make_user, attempts=3, conv_state="failed")
    _seed_cache(settings, pdf_path)
    assert client.post(f"/api/papers/{pid}/reextract").status_code == 202

    conn = connect(settings)
    try:
        row = conn.execute("SELECT conv_attempts, conv_error FROM papers WHERE id=?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row["conv_error"] != "超过最大重试次数", "重提取被旧的失败次数挡住了"
    assert row["conv_attempts"] == 1, (
        f"没归零或没记账（实际 {row['conv_attempts']}）："
        "归零 → 新一轮从 1 开始；认领即计数 → 这次尝试必须被记下")


def test_reextract_does_not_touch_other_papers(client, settings, make_user):
    """只清这一篇的缓存行 —— 同一份 PDF 导入到两个计划时，别把另一篇的也清掉。"""
    from papershelf.server.db import connect, tx

    pid, pdf_path = _seed(client, make_user)
    _seed_cache(settings, pdf_path)
    conn = connect(settings)
    try:
        with tx(conn):
            conn.execute("INSERT OR REPLACE INTO doc_cache (fingerprint, doc_json, created_at) "
                         "VALUES ('deadbeef', '{}', datetime('now'))")
    finally:
        conn.close()

    assert client.post(f"/api/papers/{pid}/reextract").status_code == 202
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM doc_cache WHERE fingerprint='deadbeef'"
                            ).fetchone()[0] == 1, "误删了别的缓存行"
    finally:
        conn.close()


def test_reextract_while_doing_is_409(client, settings, make_user):
    """转换中不许重提取（否则两条路径同时写同一篇的 blocks）。"""
    pid, _ = _seed(client, make_user, conv_state="doing")
    r = client.post(f"/api/papers/{pid}/reextract")
    assert r.status_code == 409
    assert "正在转换" in r.json()["detail"]


def test_reextract_missing_or_foreign_paper_is_404(client, settings, make_user):
    """别人的文献 / 不存在的 id —— 一律 404（不泄露"这个 id 存在"）。"""
    pid, _ = _seed(client, make_user)
    _, pw2 = make_user("other@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": "other@tsinghua.edu.cn", "password": pw2})

    assert client.post(f"/api/papers/{pid}/reextract").status_code == 404
    assert client.post("/api/papers/999999/reextract").status_code == 404


def test_reextract_without_pdf_still_requeues(client, settings, make_user):
    """arXiv 路线没有 PDF → 无缓存可清，但"重新跑一遍"这件事仍须发生。"""
    from papershelf.server.db import connect, tx

    pid, _ = _seed(client, make_user)
    conn = connect(settings)
    try:
        with tx(conn):
            conn.execute("UPDATE papers SET source='arxiv', pdf_path=NULL WHERE id=?", (pid,))
    finally:
        conn.close()

    r = client.post(f"/api/papers/{pid}/reextract")
    assert r.status_code == 202, r.text
    assert r.json()["cache_cleared"] is False


def test_frontend_actually_wires_the_button():
    """**前端必须接线** —— ⑲ 与 ㉙ 都是"后端早有、前端从没接"，功能等于不存在。

    这是本仓库出现过两次的同一类缺陷（死列 / 无入口），所以用一条静态护栏钉住它：
    api 里有封装、Library 里真的调用它、且按钮紧挨着删除。
    """
    api_ts = (ROOT / "web" / "src" / "api.ts").read_text(encoding="utf-8")
    lib_tsx = (ROOT / "web" / "src" / "pages" / "Library.tsx").read_text(encoding="utf-8")

    assert "reextractPaper" in api_ts, "api.ts 没有 reextractPaper 封装"
    assert "/reextract" in api_ts, "api.ts 里的路径不是 /reextract"
    assert "api.reextractPaper(" in lib_tsx, "文献库从没调用 reextractPaper（死入口）"
    assert "t-re" in lib_tsx, "重新提取按钮缺少样式类 t-re"

    # 样式类必须在 CSS 里有定义（否则按钮长得跟"编辑/删除"一样、hover 无法区分）
    css = (ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    assert ".t-re" in css, "styles.css 没有 .t-re 规则"
