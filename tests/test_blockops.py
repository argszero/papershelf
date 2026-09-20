"""手工修订的**三个原语**：插入块 / 编辑块 / 删除块（宿主 2026-09-20）。

宿主原话：

> 所以，实际上不是拆分，是可以在任意 block 的前面或者后面添加新的 block。同样，合并也不是
> 单独的操作。只要支持新 block 的插入、老 block 的编辑、老 block 的删除。就相当于实现了
> block 的手动拆分和合并。并且可以确保只影响当前 block 的笔记，其前后未动的 block 的笔记
> 可以不影响。

所以这里**没有** `split` / `merge` 两个动作可测：拆分是"插入 + 编辑"、合并是"编辑 + 删除"
的组合。本文件钉的是**组合之外的那条不变量**（顺序 = 风险从大到小）：

1. **相邻块一条批注都不动** —— 编辑走 `WHERE id=?`、插入取"最大号 + 1"、块 id 永不重编号，
   所以钉在别的块上的 `(block_id, lang, start, end)` 数学上不可能变。这是宿主唯一的那条
   判据（"能保则做，反之不能做"），所以用**全表快照**量，而不是抽查几条。
2. **只有被编辑那一块的批注会动，且动得有据**：坐标映射不了就**不硬贴**（划痕删、笔记转
   文献级并带着原文摘录），因为这些"丢"必须由前端当场告诉用户。
3. `en` 与 `zh` 是**两套独立坐标系** —— 改中文不动英文批注，反之亦然（中英没有字级对应）。
4. 插入块**不重编号**已有块；`ord` 位置正确（在前后两块之间）；不继承版面戳（`band` 等）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from papershelf.pipeline.model import Block, Doc
from papershelf.server.blockops import (
    TEXT_TYPES,
    delete_block,
    edit_block,
    insert_block,
    remap_span,
)

EN2 = "Safety is important. It is also hard."
ZH2 = "安全性很重要。它也难。"
EN3 = "Closed-loop control."


# ── 夹具 ────────────────────────────────────────────────────────────────
def _seed(settings) -> int:
    """一篇四块的小文献：标题 + 两段正文 + 一张图。

    图块（`payload` 类）是刻意放的：插入的**参照块**可能是它（"在图后面补一段"），
    而它的文字不在 `en`/`zh` 里，所以类型回落与"不继承版面戳"都拿它当反例。
    """
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        # `OR IGNORE`：HTTP 那几条用例先建了真账号/真计划（id 1），这里只补一篇文献。
        conn.execute("INSERT OR IGNORE INTO users (id,email,password_hash,status,created_at)"
                     " VALUES (1,'u@tsinghua.edu.cn','x','active','')")
        conn.execute("INSERT OR IGNORE INTO plans (id,user_id,name) VALUES (1,1,'p')")
        conn.execute("INSERT INTO papers (id,plan_id,title,conv_state) "
                     "VALUES (1,1,'测试文献','done')")
        conn.commit()
        save_doc(conn, 1, Doc(meta={"title_zh": "测试"}, blocks=[
            Block(id="b-0001", type="h2", level=2, en="I. Introduction", zh="I. 引言",
                  zh_source="mt", section="1", payload={"page": 1}),
            Block(id="b-0002", type="p", en=EN2, zh=ZH2, zh_source="mt",
                  section="1", payload={"page": 1}),
            Block(id="b-0003", type="p", en=EN3, zh="闭环控制。", zh_source="mt",
                  section="1", payload={"page": 2, "band": "top"}),
            Block(id="b-0004", type="figure", en="", zh="",
                  section="1", payload={"page": 2, "file": "a.png"}),
        ]))
        return 1
    finally:
        conn.close()


def _anchor(conn, table, block_id, lang, s, e, **extra):
    cols = ", ".join(extra)
    ph = ", ".join("?" for _ in extra)
    conn.execute(
        f"INSERT INTO {table} (paper_id, block_id, lang, start, end"
        + (f", {cols}" if cols else "") + ") VALUES (1,?,?,?,?"
        + (f", {ph}" if cols else "") + ")",
        (block_id, lang, s, e, *extra.values()))


@pytest.fixture()
def paper(settings):
    """正文三块 + 图一块，批注钉在 b-0002 / b-0003（**不含** b-0001 与 b-0004）。"""
    from papershelf.server.db import connect, tx

    pid = _seed(settings)
    conn = connect(settings)
    # ⚠️ 下标**现场算**，不手写：这两个词的位置手写错过一次，
    #    结果是映射明明对、测试却红（核对锚点的断言最容易被自己骗）。
    head = EN2.index("Safety")
    tail = EN2.index("hard.")
    also = EN2.index("also")
    assert EN2[head:head + 6] == "Safety" and EN2[tail:tail + 5] == "hard."
    assert EN2[also:also + 4] == "also"
    with tx(conn):
        _anchor(conn, "highlights", "b-0002", "en", head, head + 6, color="amber")
        _anchor(conn, "highlights", "b-0002", "en", tail, tail + 5, color="amber")
        _anchor(conn, "highlights", "b-0002", "zh", 0, 3, color="green")
        _anchor(conn, "notes", "b-0002", "en", also, also + 4, content="英文笔记",
                created_at="2026-09-20")
        _anchor(conn, "notes", "b-0002", "zh", 0, 3, content="中文笔记", hl_id=3,
                created_at="2026-09-20")
        # b-0003 上：划痕 + **整块笔记**（`start` 为空，指的是"这一块"，永远不用搬）
        _anchor(conn, "highlights", "b-0003", "en", 0, 5, color="violet")
        _anchor(conn, "notes", "b-0003", None, None, None, content="整块笔记",
                created_at="2026-09-20")
    return pid, conn


def _snapshot(conn) -> list[tuple]:
    """全表快照（含 id）—— 「相邻块的批注零变化」必须逐字段量，不能抽查几列。

    两张表各自取全列并加上表名，合成一个可直接 `==` 比较的列表。
    """
    hls = [("hl",) + tuple(r) for r in conn.execute(
        "SELECT id, block_id, lang, start, end, color, created_at FROM highlights"
        " ORDER BY id").fetchall()]
    notes = [("note",) + tuple(r) for r in conn.execute(
        "SELECT id, block_id, lang, start, end, hl_id, quote, content FROM notes"
        " ORDER BY id").fetchall()]
    return hls + notes


def _blocks(conn) -> list[tuple[str, str]]:
    return [(r["id"], r["type"]) for r in
            conn.execute("SELECT id, type FROM blocks WHERE paper_id=1 ORDER BY ord")]


def _marks_of(conn, block_id: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT id, lang, start, end, color FROM highlights WHERE block_id=? ORDER BY id",
        (block_id,)).fetchall()]


# ── ① 坐标映射（`remap_span`）────────────────────────────────────────────
@pytest.mark.parametrize("old,new,s,e,want", [
    # 逐字未动 ⇒ 原位
    ("Hello world", "Hello world", 6, 11, (6, 11)),
    # 中间插入 ⇒ 改动点**右侧**整体右移（改了点左边的文字不该跟着动）
    ("Hello world", "Hello brave world", 6, 11, (12, 17)),
    ("Hello world", "Hello brave world", 0, 5, (0, 5)),
    # 区间**终点**正落在插入点上 ⇒ 一个字都不跟着走（合并的形状：把下一块接上来）
    ("Hello", "Hello world", 0, 5, (0, 5)),
    ("Hello world", "Hello brave world", 0, 6, (0, 6)),
    # 尾部剪切（拆分）与尾部删除 ⇒ 剩下的文字原位
    ("Hello world", "Hello ", 0, 5, (0, 5)),
    ("Hello world", "Hello ", 0, 6, (0, 6)),
    # 前部删除 ⇒ 后面的文字整体左移
    ("aaaXXXXbbb", "aaabbb", 7, 9, (3, 5)),
    # 整段落在被删掉的那段里 ⇒ None（**不许硬贴到相邻字符上**）
    ("Hello brave world", "Hello world", 7, 11, None),
    ("aaaXXXXbbb", "aaabbb", 4, 6, None),
    # 空区间（划痕本不该有，但别让它变成"永远跟着走的 0 宽度划痕"）
    ("aaabbb", "aaabbb", 3, 3, None),
])
def test_remap_span_is_deterministic(old, new, s, e, want):
    got = remap_span(old, new, s, e)
    assert got == want
    if got is not None:
        assert 0 <= got[0] < got[1] <= len(new)


def test_remap_span_clamps_to_the_new_text():
    """区间有一端落进被替换掉的那段 ⇒ **夹到接缝**，不许越出 `len(new)`。"""
    got = remap_span("abcdef", "abXYef", 1, 6)
    assert got is not None
    assert 0 <= got[0] <= got[1] <= len("abXYef")


# ── ② 编辑块：本块有据地动、邻居一动不动 ─────────────────────────────────
def test_edit_zh_only_touches_the_chinese_side(paper):
    """只改中文：英文批注**一个都不动**（两栏是两套坐标系），中文按新文字重锚。"""
    pid, conn = paper
    before_en = _marks_of(conn, "b-0002")
    res = edit_block(conn, pid, "b-0002", zh="安全性极其重要。它也难。", reconciled=True)
    assert res["ok"] and res["anchors_moved"] == 2 and res["anchors_dropped"] == []
    row = conn.execute("SELECT zh, zh_source, payload FROM blocks WHERE id='b-0002'").fetchone()
    assert row["zh"] == "安全性极其重要。它也难。"
    assert row["zh_source"] == "human"           # ⑯：人工修订不得被重跑覆盖
    # 英文侧划痕逐字段不变（`_marks_of` 里含 id/lang/start/end/color）
    assert _marks_of(conn, "b-0002") == before_en
    # 中文划痕仍在 zh 上、仍在开头三个字上
    zh = [m for m in _marks_of(conn, "b-0002") if m[1] == "zh"]
    assert zh and zh[0][2:4] == (0, 3)


def test_edit_en_only_marks_the_translation_stale(paper):
    """只改原文：老译文与新原文已对不上 ⇒ 保留它，但**如实**挂「待校对」。"""
    pid, conn = paper
    res = edit_block(conn, pid, "b-0002", en=EN2 + " So we test it.")
    assert res["zh_stale"] is True
    row = conn.execute("SELECT zh, zh_source, payload FROM blocks WHERE id='b-0002'").fetchone()
    assert row["zh"] == ZH2                       # 老译文**保留**（用户可能还要自己改）
    assert row["zh_source"] == "mt"               # 没改中文就不许把它标成人工修订
    from papershelf.server.db import load_json
    assert load_json(row["payload"], {}).get("needs_review") is True


def test_edit_en_dropping_a_span_reports_what_was_lost(paper):
    """**静默丢批注是这个功能最坏的失败模式** ⇒ 丢了什么必须算得出来。

    把尾巴剪掉（拆分的那一半）：落在尾巴上的英文划痕随之移除、笔记转「文献级」并带回原文；
    **落在被保留的那一段上的划痕原位不动**。
    """
    pid, conn = paper
    head = "Safety is important."
    res = edit_block(conn, pid, "b-0002", en=head)
    assert res["ok"]
    assert sorted(d["kind"] for d in res["anchors_dropped"]) == ["highlight", "note"]
    # 划痕只有坐标 ⇒ 落在被删掉文字上的真的没了；留下的是中文那条 + 头部那条（原位）
    hls = [(h[1], h[2], h[3]) for h in _marks_of(conn, "b-0002")]
    assert ("en", 0, 6) in hls and ("zh", 0, 3) in hls
    assert len(hls) == 2
    # 笔记有内容 ⇒ 内容留着、锚点清空，**摘录是被删掉的那段文字**
    n = conn.execute("SELECT * FROM notes WHERE content='英文笔记'").fetchone()
    assert n["block_id"] is None and n["lang"] is None and n["start"] is None
    assert n["hl_id"] is None
    assert n["quote"] == "also"
    # 中文那条笔记一个字没动（两栏两套坐标系）
    zh_n = conn.execute("SELECT * FROM notes WHERE content='中文笔记'").fetchone()
    assert zh_n["block_id"] == "b-0002" and (zh_n["start"], zh_n["end"]) == (0, 3)


def test_edit_leaves_neighbours_byte_identical(paper):
    """宿主那句话就是这条断言：**前后未动的 block 的笔记不受影响**。

    全表快照比对（不是抽几列）—— 编辑第 2 块时，第 3 块的划痕/笔记一个字段都不许变。
    """
    pid, conn = paper
    before = _snapshot(conn)
    nb_before = [r for r in before if r[2] == "b-0003"]     # 邻居那几条
    assert nb_before
    edit_block(conn, pid, "b-0002", en="Totally different text.", zh="完全不同的文字。")
    after = _snapshot(conn)
    assert [r for r in after if r[2] == "b-0003"] == nb_before
    # 连 rowid 都没挪位 —— 快照按 id 排，顺序也不该变
    assert [r[1] for r in after if r[2] == "b-0003"] == [r[1] for r in nb_before]


def test_edit_rejects_unknown_type_and_empty_body(paper):
    pid, conn = paper
    assert edit_block(conn, pid, "b-0002", type="figure")["code"] == "bad_args"
    assert edit_block(conn, pid, "b-0002")["code"] == "bad_args"
    assert edit_block(conn, pid, "b-9999", zh="x")["code"] == "not_found"


# ── ③ 插入块 ────────────────────────────────────────────────────────────
def test_insert_after_never_renumbers_existing_blocks(paper):
    """新块 = **最大号 + 1**；已有块 id 一个不动（否则钉在它们上的批注当场指到别的段上）。"""
    pid, conn = paper
    before = _snapshot(conn)
    res = insert_block(conn, pid, after="b-0002", en="Inserted sentence.", zh="插入的句子。")
    assert res["ok"] and res["id"] == "b-0005"
    assert _blocks(conn) == [
        ("b-0001", "h2"), ("b-0002", "p"), ("b-0005", "p"), ("b-0003", "p"), ("b-0004", "figure")]
    assert _snapshot(conn) == before              # 批注一条没动（包括 ord 变化也不影响）
    row = conn.execute("SELECT * FROM blocks WHERE id='b-0005'").fetchone()
    assert row["zh_source"] == "human"            # 用户自己写的字，重跑不许覆盖
    from papershelf.server.db import load_json
    assert load_json(row["payload"], {}).get("page") == 1   # 跟着参照块留在同一页


def test_insert_before_the_first_block_lands_at_the_top(paper):
    pid, conn = paper
    res = insert_block(conn, pid, before="b-0001", en="Preface.", zh="前言。")
    assert res["ok"]
    assert _blocks(conn)[0] == (res["id"], "h2")   # 参照块是标题 ⇒ 新块默认也是标题
    assert _blocks(conn)[1] == ("b-0001", "h2")


def test_insert_without_translation_is_marked_for_review(paper):
    """插一段只有英文的新块 ⇒ 挂「待校对」（它确实还没有中文），前端可「重译此块」。"""
    pid, conn = paper
    res = insert_block(conn, pid, after="b-0004", en="A caption to write later.")
    from papershelf.server.db import load_json
    row = conn.execute("SELECT * FROM blocks WHERE id=?", (res["id"],)).fetchone()
    assert row["zh"] == "" and row["zh_source"] == "none"
    assert load_json(row["payload"], {}).get("needs_review") is True
    # 参照块是图（`payload` 类）⇒ 类型回落 `p`，并且**不继承**版面戳
    assert row["type"] == "p"
    assert "band" not in load_json(row["payload"], {})


def test_insert_does_not_inherit_layout_stamps(paper):
    """`band`/`shade` 描述的是"这一行在 PDF 上的版面位置"，手写的新块没有这回事。"""
    pid, conn = paper
    res = insert_block(conn, pid, after="b-0003", en="New paragraph.", zh="新段落。")
    from papershelf.server.db import load_json
    payload = load_json(conn.execute("SELECT payload FROM blocks WHERE id=?",
                                     (res["id"],)).fetchone()["payload"], {})
    assert "band" not in payload and payload.get("page") == 2


def test_insert_rejects_bad_arguments(paper):
    pid, conn = paper
    assert insert_block(conn, pid, en="x")["code"] == "bad_args"          # 没说插在哪
    assert insert_block(conn, pid, after="nope", en="x")["code"] == "not_found"
    assert insert_block(conn, pid, after="b-0001", type="deco")["code"] == "bad_args"


def test_insert_and_delete_without_a_doc_conflict(settings):
    """没有产物（转换未完成）⇒ 409 而不是 500（`no_doc` 这个码映射到 HTTP 状态）。"""
    from papershelf.server.db import connect

    conn = connect(settings)
    conn.execute("INSERT INTO users (id,email,password_hash,status,created_at)"
                 " VALUES (1,'u@x.edu.cn','x','active','')")
    conn.execute("INSERT INTO plans (id,user_id,name) VALUES (1,1,'p')")
    conn.execute("INSERT INTO papers (id,plan_id,title,conv_state) "
                 "VALUES (9,1,'没产物','queued')")
    conn.commit()
    assert insert_block(conn, 9, after="b-0001", en="x")["code"] == "no_doc"
    assert delete_block(conn, 9, "b-0001")["code"] == "no_doc"


# ── ④ 删除块（合并的另一半）──────────────────────────────────────────────
def test_delete_keeps_note_content_and_removes_highlights(paper):
    """**用户写的字比它的锚点值钱**：笔记一条不删，只把锚点清空成「文献级笔记」。"""
    pid, conn = paper
    nb_before = [r for r in _snapshot(conn) if r[2] == "b-0003"]
    res = delete_block(conn, pid, "b-0002")
    assert res["ok"] and res["notes_unanchored"] == 2
    assert res["highlights_deleted"] == 3          # 英文两条 + 中文一条，**全部**随块收走
    assert [r["id"] for r in conn.execute("SELECT id FROM highlights WHERE block_id='b-0002'")] == []
    kept = conn.execute("SELECT * FROM notes WHERE content IN ('英文笔记','中文笔记')"
                        " ORDER BY id").fetchall()
    assert len(kept) == 2
    assert all(r["block_id"] is None and r["hl_id"] is None for r in kept)
    assert all(r["quote"] for r in kept)          # 每条都带着自己原本划住的原文
    assert res["quotes"]
    # 被删块的邻居一动不动
    assert [r for r in _snapshot(conn) if r[2] == "b-0003"] == nb_before


def test_delete_does_not_touch_other_blocks(paper):
    pid, conn = paper
    assert delete_block(conn, pid, "b-0003")["ok"]
    assert _blocks(conn) == [("b-0001", "h2"), ("b-0002", "p"), ("b-0004", "figure")]
    assert _marks_of(conn, "b-0002") != []
    n = conn.execute("SELECT * FROM notes WHERE content='整块笔记'").fetchone()
    assert n["block_id"] is None and n["content"] == "整块笔记"


def test_delete_unknown_block_is_a_clean_refusal(paper):
    pid, conn = paper
    assert delete_block(conn, pid, "b-9999")["code"] == "not_found"


# ── ⑤ HTTP 面：端点必须回**完整块**（前端只能整块替换）────────────────────
@pytest.fixture()
def owned(client, settings, make_user):
    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    from papershelf.server.db import connect

    _seed(settings)                               # ⚠️ 用户/计划/文献都是 id 1（见 `_seed`）
    conn = connect(settings)
    conn.execute("UPDATE papers SET plan_id=?", (plan_id,))
    conn.commit()
    return {"pid": 1}


def test_patch_returns_the_whole_block_with_its_marks(client, owned):
    """⚠️ 回完整块**且带上本块划痕**：`zh_html` 是划痕的唯一来源，不带 = 编辑一次抹掉划痕。"""
    pid = owned["pid"]
    hl = client.post(f"/api/papers/{pid}/highlights",
                     json={"block_id": "b-0002", "lang": "en", "start": 0, "end": 6,
                           "color": "amber"}).json()
    r = client.patch(f"/api/docs/{pid}/blocks/b-0002",
                     json={"zh": "安全性极其重要。", "reconciled": True})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["id"] == "b-0002" and got["zh_source"] == "human"
    assert 'data-h="%d"' % hl["id"] in got["en_html"]
    assert "zh_html" in got and "en" in got and "zh" in got      # 完整块，不是 {id, zh}
    # 只改了中文 ⇒ 只有 `lang='zh'` 的批注会重锚，而这块的划痕在 `en` 上 ⇒ 移动 0 条
    assert got["anchors_moved"] == 0 and got["anchors_dropped"] == []


def test_post_inserts_and_get_shows_it_in_order(client, owned):
    pid = owned["pid"]
    r = client.post(f"/api/docs/{pid}/blocks",
                    json={"after": "b-0002", "en": "Added.", "zh": "新增。"})
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    ids = [b["id"] for b in client.get(f"/api/papers/{pid}/doc").json()["blocks"]]
    assert ids.index(new_id) == ids.index("b-0002") + 1


def test_delete_returns_the_ledger_for_the_toast(client, owned):
    pid = owned["pid"]
    client.post(f"/api/papers/{pid}/highlights",
                json={"block_id": "b-0003", "lang": "en", "start": 0, "end": 5,
                      "color": "amber"})
    r = client.delete(f"/api/docs/{pid}/blocks/b-0003")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["ok"] and got["highlights_deleted"] == 1 and got["notes_unanchored"] == 0
    ids = [b["id"] for b in client.get(f"/api/papers/{pid}/doc").json()["blocks"]]
    assert "b-0003" not in ids


def test_endpoints_refuse_other_peoples_papers(client, owned, make_user):
    """别人的文献一律 404（`require_paper`）—— 三个原语都不能绕过这道门。"""
    make_user("other@tsinghua.edu.cn", "password123")
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": "other@tsinghua.edu.cn",
                                         "password": "password123"})
    pid = owned["pid"]
    assert client.patch(f"/api/docs/{pid}/blocks/b-0002",
                        json={"zh": "改动"}).status_code == 404
    assert client.post(f"/api/docs/{pid}/blocks",
                       json={"after": "b-0002", "zh": "新增"}).status_code == 404
    assert client.delete(f"/api/docs/{pid}/blocks/b-0002").status_code == 404


def test_text_types_matches_the_frontend_contract():
    """服务端的名单本身：`figure`/`table`/`deco`/`eq` 的内容在 `payload` 里，
    凭空造一个只会渲染成空白；`h1` 不在其中（正文不产生 h1 块，标题改的是元数据）。"""
    assert set(TEXT_TYPES) == {"p", "h2", "h3", "h4", "abstract", "refs", "ref"}
    for t in ("figure", "table", "deco", "eq", "h1"):
        assert t not in TEXT_TYPES


def test_frontend_type_list_is_the_same_as_the_server():
    """**跨边界护栏**：前端 `Reader.tsx` 的 `TEXT_TYPES` 必须与服务端逐字一致。

    两边是各写一份的常量（前端在浏览器里，后端在 Python 里），漂开的表现是
    "工具条上多了一个按钮，点下去收到 400"—— 本地不点就发现不了。
    这条护栏直接读前端源码里的那个字面量（不跑浏览器），漂了就红。
    """
    src = (Path(__file__).resolve().parents[1]
           / "web" / "src" / "pages" / "Reader.tsx").read_text(encoding="utf-8")
    m = re.search(r"const TEXT_TYPES = new Set\(\[([^\]]*)\]\)", src)
    assert m, "前端找不到 TEXT_TYPES 常量（改名了？这条护栏要跟着改）"
    front = {t.strip().strip("'\"") for t in m.group(1).split(",") if t.strip()}
    assert front == set(TEXT_TYPES), f"前端 {sorted(front)} ≠ 服务端 {sorted(TEXT_TYPES)}"
