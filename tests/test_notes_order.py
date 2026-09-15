"""笔记列表的顺序 = **锚点在原文里的位置**，不是写入时间（宿主 2026-09-15）。

原话：「右侧笔记列表，顺序不对。现在看起来好像是最新的笔记在前面。实际上，应该按照笔记
关联的原文的位置为顺序，前面的上面句子的笔记就在上面，下面句子的笔记就在下面。」

原来的 `list_notes` 是 `ORDER BY created_at DESC` —— 也就是**倒序的写入时间**。
这在"边读边记"的真实用法里必然错位：读第一段时记的笔记，读到第五段时再看列表，
它被挤到了最下面。

顺序由**服务端**定（`repo._NOTE_ORDER`）：只有服务端手上有 `blocks.ord`（阅读顺序，
㉜ 分栏感知排好的），前端据此只负责渲染；新增笔记后前端**重拉列表**而不是自己插位置 ——
两份排序实现必然分叉。

这里钉住四件事：
① 跨块按文档顺序，与写入顺序**无关**；
② 同一段内：整段笔记在前，然后按字符位置；
③ 两栏（原文/中文）各有笔记时，位置相同者原文列在前；
④ 没有落点的（文献级笔记）排在最后。
"""

from __future__ import annotations

import pytest

from papershelf.pipeline.model import Block, Doc

EN1 = "First paragraph is here. It has two sentences."
ZH1 = "第一段在这里。它有两个句子。"
EN3 = "Third paragraph is here."
ZH3 = "第三段在这里。"


def _seed(settings, plan_id: int) -> int:
    """三块正文（`b-0002` / `b-0003` / `b-0004`）+ 一个标题，供跨块排序用。"""
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        cur = conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,status_at,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (plan_id, "排序测试", "upload", "reading", "2026-01-01T00:00:00+00:00", "done"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
        save_doc(conn, pid, Doc(meta={}, blocks=[
            Block(id="b-0001", type="h2", en="I. Intro", zh="I. 引言", zh_source="mt"),
            Block(id="b-0002", type="p", en=EN1, zh=ZH1, zh_source="mt"),
            Block(id="b-0003", type="p", en=EN3, zh=ZH3, zh_source="mt"),
            Block(id="b-0004", type="p", en="Fourth.", zh="第四段。", zh_source="mt"),
        ]))
        return pid
    finally:
        conn.close()


@pytest.fixture()
def owned(client, settings, make_user):
    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    return {"pid": _seed(settings, plan_id), "plan_id": plan_id, "settings": settings}


def _note(client, pid: int, content: str, *, block_id: str | None = "b-0002",
          lang: str | None = None, start: int | None = None, end: int | None = None) -> dict:
    body: dict = {"content": content}
    if block_id is not None:
        body["block_id"] = block_id
    if lang is not None:
        body.update({"lang": lang, "start": start, "end": end})
    r = client.post(f"/api/papers/{pid}/notes", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _order(client, pid: int) -> list[str]:
    return [n["content"] for n in client.get(f"/api/papers/{pid}/notes").json()]


def test_notes_follow_document_order_not_creation_time(client, owned):
    """**核心用例**：先记后面那段、再记前面那段，列表仍按原文顺序（前面在上）。"""
    pid = owned["pid"]
    _note(client, pid, "第三段的笔记", block_id="b-0003", lang="zh", start=0, end=4)
    _note(client, pid, "第二段的笔记", block_id="b-0002", lang="zh", start=0, end=4)
    assert _order(client, pid) == ["第二段的笔记", "第三段的笔记"]


def test_notes_in_one_block_go_by_character_offset(client, owned):
    """同一段内按字符位置 —— 后面的字在后，与写入顺序无关。"""
    pid = owned["pid"]
    _note(client, pid, "后半句", block_id="b-0002", lang="en", start=27, end=45)
    _note(client, pid, "开头", block_id="b-0002", lang="en", start=0, end=5)
    _note(client, pid, "中间", block_id="b-0002", lang="en", start=10, end=20)
    assert _order(client, pid) == ["开头", "中间", "后半句"]


def test_block_level_note_comes_first_within_its_block(client, owned):
    """整段笔记（没有字符区间）排在**它那一段**的区间笔记之前，但仍在上一段之后。"""
    pid = owned["pid"]
    _note(client, pid, "第三段区间", block_id="b-0003", lang="zh", start=0, end=2)
    _note(client, pid, "第二段区间", block_id="b-0002", lang="zh", start=0, end=2)
    _note(client, pid, "第二段整段", block_id="b-0002")
    assert _order(client, pid) == ["第二段整段", "第二段区间", "第三段区间"]


def test_same_offset_prefers_the_original_column(client, owned):
    """位置相同时**原文列在前**（与并排阅读的左→右一致）；跨栏时位置优先。"""
    pid = owned["pid"]
    _note(client, pid, "中文 0", block_id="b-0002", lang="zh", start=0, end=3)
    _note(client, pid, "原文 0", block_id="b-0002", lang="en", start=0, end=5)
    assert _order(client, pid) == ["原文 0", "中文 0"]
    # 原文列里更靠后的位置，仍应排在中文列开头之后（位置优先于栏）
    _note(client, pid, "原文 30", block_id="b-0002", lang="en", start=30, end=40)
    assert _order(client, pid) == ["原文 0", "中文 0", "原文 30"]


def test_unanchored_note_goes_last(client, owned):
    """文献级笔记（没有落点）排在最后 —— 列表是顺着原文读的，它不该打断中间。"""
    pid = owned["pid"]
    _note(client, pid, "文献级", block_id=None)
    _note(client, pid, "第二段", block_id="b-0002", lang="zh", start=0, end=2)
    assert _order(client, pid) == ["第二段", "文献级"]


def test_order_uses_real_block_order_not_id_order(client, owned):
    """排序靠的是 `blocks.ord`（阅读顺序），不是块 id 的字典序。

    ⚠️ 这条是给未来留的护栏：㉜ 的分栏感知会**重排** `ord`，㉝ 的 agent 还有 `reorder_page`
    工具 —— 一旦 id 顺序与文档顺序脱钩，按 id 排就会错。这里把 `b-0002`（第二段）
    和 `b-0004`（第四段）的 `ord` 对调，id 顺序与阅读顺序就故意相反了。
    """
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect

    pid = owned["pid"]
    conn = connect(get_settings())
    try:
        with conn:
            conn.execute("UPDATE blocks SET ord=9 WHERE paper_id=? AND id='b-0002'", (pid,))
            conn.execute("UPDATE blocks SET ord=1 WHERE paper_id=? AND id='b-0004'", (pid,))
    finally:
        conn.close()

    _note(client, pid, "第四块（现在最前）", block_id="b-0004", lang="zh", start=0, end=2)
    _note(client, pid, "第二块（现在最后）", block_id="b-0002", lang="zh", start=0, end=2)
    assert _order(client, pid) == ["第四块（现在最前）", "第二块（现在最后）"]
