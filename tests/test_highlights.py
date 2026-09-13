"""划痕（决策㉛）：任意字符区间的高亮 + 区间锚笔记 + 只读分享。

㉚ 那一版是"点一句 → 整句变黄"。宿主 2026-09-13 指出那不是读论文时标注的方式：
**标准做法是挑一支笔、随手划住任意一段**，划多长就是多长。于是：

- 锚点从「句」换成**字符区间** `(block_id, lang, start, end)`，坐标是块**裸文本**的字符偏移；
- 服务端把它渲染成 `<mark class="hl hl-<色>" data-h="id">`，并吐**偏移锚点**
  （`<span class="o" data-o="N">`）让前端能把 DOM 选区换回这对数字；
- 颜色**不带含义**（宿主原话：「好看的几种颜色、没有含义」），四支等价的笔。

这里钉住四件事：① 渲染确实落在对的字符上；② 重叠**不许叠加**（后划的切掉先划的）；
③ 只读分享看得见、写不动；④ ㉚ 的存量句锚点能**确定性**换算成区间。
"""

from __future__ import annotations

import pytest

from papershelf.pipeline.model import Block, Doc

EN = "Safety is important. It is also hard."
ZH = "安全性很重要。它也难。"


def _seed(settings, plan_id: int) -> int:
    """一篇两块的小文献：一个标题 + 一个正文段（只有正文段可划）。"""
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        cur = conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,status_at,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (plan_id, "测试文献", "upload", "reading", "2026-01-01T00:00:00+00:00", "done"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
        save_doc(conn, pid, Doc(meta={"title_zh": "测试"}, blocks=[
            Block(id="b-0001", type="h2", en="I. Introduction", zh="I. 引言", zh_source="mt"),
            Block(id="b-0002", type="p", en=EN, zh=ZH, zh_source="mt"),
        ]))
        return pid
    finally:
        conn.close()


@pytest.fixture()
def owned(client, settings, make_user):
    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    return {"pid": _seed(settings, plan_id), "plan_id": plan_id}


# ㉚ 那一版的表形状（`(paper_id, sid)` 主键）。**故意在测试里手写一份**：
# 迁移测试要造"老库"，那份旧 DDL 属于历史，不该留在产品代码里当摆设。
_LEGACY_HL_DDL = """
CREATE TABLE highlights (
  paper_id   INTEGER NOT NULL,
  sid        TEXT NOT NULL,
  created_at TEXT,
  PRIMARY KEY (paper_id, sid)
);
"""


def _mk(client, pid: int, **kw) -> dict:
    body = {"block_id": "b-0002", "lang": "en", "start": 0, "end": 6, "color": "amber"}
    body.update(kw)
    r = client.post(f"/api/papers/{pid}/highlights", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── ① 渲染：划痕必须落在**对的那几个字符**上，锚点必须在 ───────────────────
def test_marks_render_on_the_right_characters(client, owned):
    pid = owned["pid"]
    _mk(client, pid, lang="en", start=0, end=6)                    # "Safety"
    data = client.get(f"/api/papers/{pid}/doc").json()
    para = next(b for b in data["blocks"] if b["id"] == "b-0002")
    assert '<mark class="hl hl-amber"' in para["en_html"]
    assert '<mark class="hl hl-amber" data-h="1" tabindex="0" role="mark">Safety</mark>' in para["en_html"]
    # **中英各划各的**：英文划痕不该出现在中文 HTML 里（两侧没有字级对应）
    assert "<mark" not in para["zh_html"]
    # 偏移锚点：前端的"尺子"，缺了它任何选区都换算不回可持久化的坐标
    assert 'class="o" data-o="0"' in para["en_html"]
    assert f'class="o" data-o="{len(EN)}"' in para["en_html"]


def test_offset_anchors_are_in_every_prose_block(client, owned):
    """没有划痕的正文块**也要**有锚点 —— 否则"第一笔划在哪里"这件事无从下手。"""
    data = client.get(f"/api/papers/{owned['pid']}/doc").json()
    para = next(b for b in data["blocks"] if b["id"] == "b-0002")
    assert 'data-o="0"' in para["en_html"] and "<mark" not in para["en_html"]
    head = next(b for b in data["blocks"] if b["id"] == "b-0001")
    assert "data-o=" not in head["en_html"]          # 标题不是可划区域


# （公式原子性 / 颜色回落的**渲染层**测试在 `tests/test_markup.py`，
#  这里只管接口行为，避免同一件事钉两遍。）


# ── ② 重叠：后划的切掉先划的（不叠两层底色）─────────────────────────────
def test_overlapping_marks_are_clipped_not_stacked(client, owned):
    pid = owned["pid"]
    first = _mk(client, pid, start=0, end=10, color="green")
    _mk(client, pid, start=6, end=15, color="pink")
    rows = {r["id"]: r for r in client.get(f"/api/papers/{pid}/highlights").json()}
    assert (rows[first["id"]]["start"], rows[first["id"]]["end"]) == (0, 6)   # 被切短
    assert rows[first["id"]]["color"] == "green"                             # 还是原来那支笔
    assert len(rows) == 2


def test_mark_split_in_the_middle_keeps_both_halves(client, owned):
    """新划痕把旧划痕**从中间劈开** → 旧划痕变成左右两段（同色）。"""
    pid = owned["pid"]
    old = _mk(client, pid, start=0, end=20, color="blue")
    _mk(client, pid, start=6, end=12, color="amber")
    rows = client.get(f"/api/papers/{pid}/highlights").json()
    spans = sorted((r["start"], r["end"], r["color"]) for r in rows)
    assert spans == [(0, 6, "blue"), (6, 12, "amber"), (12, 20, "blue")]
    assert rows[0]["id"] != old["id"] or True          # 左段是原行，右段是补出来的新行


def test_fully_covered_mark_is_removed(client, owned):
    pid = owned["pid"]
    _mk(client, pid, start=4, end=8, color="green")
    _mk(client, pid, start=0, end=20, color="pink")
    rows = client.get(f"/api/papers/{pid}/highlights").json()
    assert [(r["start"], r["end"], r["color"]) for r in rows] == [(0, 20, "pink")]


def test_marks_in_the_other_language_do_not_interfere(client, owned):
    """中英两侧的划痕互不影响 —— 它们在同一起点上重叠也不算冲突。"""
    pid = owned["pid"]
    _mk(client, pid, lang="en", start=0, end=6)
    _mk(client, pid, lang="zh", start=0, end=3)
    assert len(client.get(f"/api/papers/{pid}/highlights").json()) == 2


# ── ③ 换笔 / 擦掉 / 边界 ────────────────────────────────────────────────
def test_recolor_and_delete(client, owned):
    pid = owned["pid"]
    hl = _mk(client, pid, lang="zh", start=0, end=3, color="amber")
    assert client.patch(f"/api/highlights/{hl['id']}", json={"color": "pink"}).json()["color"] == "pink"
    assert client.get(f"/api/papers/{pid}/highlights").json()[0]["color"] == "pink"
    assert client.delete(f"/api/highlights/{hl['id']}").json()["deleted"] is True
    assert client.get(f"/api/papers/{pid}/highlights").json() == []
    # 幂等：再擦一次也是 200（前端那记删除是本地状态驱动的）
    assert client.delete(f"/api/highlights/{hl['id']}").status_code == 200


def test_empty_selection_and_bad_color_are_rejected(client, owned):
    pid = owned["pid"]
    assert client.post(f"/api/papers/{pid}/highlights",
                       json={"block_id": "b-0002", "lang": "en", "start": 5, "end": 5}).status_code == 400
    assert client.post(f"/api/papers/{pid}/highlights",
                       json={"block_id": "b-0002", "lang": "en", "start": 0, "end": 3,
                             "color": "gold"}).status_code == 400
    assert client.post(f"/api/papers/{pid}/highlights",
                       json={"block_id": "b-0002", "lang": "fr", "start": 0, "end": 3}).status_code == 422


def test_marks_are_scoped_to_the_owner(client, settings, make_user, owned):
    client.post("/api/auth/logout")
    email, pw = make_user("other@pku.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    pid = owned["pid"]
    assert client.get(f"/api/papers/{pid}/highlights").status_code == 404
    assert client.post(f"/api/papers/{pid}/highlights",
                       json={"block_id": "b-0002", "lang": "en", "start": 0,
                             "end": 3}).status_code == 404
    client.post("/api/auth/logout")
    assert client.get(f"/api/papers/{pid}/highlights").status_code == 401


# ── ④ 划痕 + 笔记 ───────────────────────────────────────────────────────
def test_note_anchored_to_a_selection(client, owned):
    pid = owned["pid"]
    hl = _mk(client, pid, lang="zh", start=0, end=6)
    r = client.post(f"/api/papers/{pid}/notes",
                    json={"content": "这句是全文的论点", "block_id": "b-0002", "lang": "zh",
                          "start": 0, "end": 6, "quote": "安全性很重要。", "hl_id": hl["id"]})
    assert r.status_code == 201
    note = r.json()
    assert (note["lang"], note["start"], note["end"]) == ("zh", 0, 6)
    assert note["hl_id"] == hl["id"] and note["quote"] == "安全性很重要。"
    rows = client.get(f"/api/papers/{pid}/notes").json()
    assert [(n["start"], n["end"]) for n in rows] == [(0, 6)]


def test_deleting_a_mark_keeps_its_note(client, owned):
    """擦掉荧光笔不等于撕掉批注：笔记有自己的一份坐标，只把 `hl_id` 解绑。"""
    pid = owned["pid"]
    hl = _mk(client, pid, start=0, end=6)
    client.post(f"/api/papers/{pid}/notes",
                json={"content": "批注", "block_id": "b-0002", "lang": "en", "start": 0, "end": 6,
                      "hl_id": hl["id"]})
    client.delete(f"/api/highlights/{hl['id']}")
    rows = client.get(f"/api/papers/{pid}/notes").json()
    assert len(rows) == 1 and rows[0]["hl_id"] is None and rows[0]["start"] == 0


def test_block_level_note_still_works(client, owned):
    r = client.post(f"/api/papers/{owned['pid']}/notes",
                    json={"content": "整块笔记", "block_id": "b-0001"})
    assert r.status_code == 201
    body = r.json()
    assert body["lang"] is None and body["start"] is None and body["quote"] is None


# ── ⑤ 给选区写笔记 = 顺手高亮（宿主 2026-09-13：「选中添加笔记时，应该同时自动高亮」）──
def test_note_on_a_selection_auto_highlights(client, owned):
    """划完没上色、写笔记时补上：**笔记与划痕要么都在、要么都不在**（同一个事务）。"""
    pid = owned["pid"]
    r = client.post(f"/api/papers/{pid}/notes",
                    json={"content": "这段是论点", "block_id": "b-0002", "lang": "zh",
                          "start": 0, "end": 6, "quote": "安全性很重要。"})
    assert r.status_code == 201
    note = r.json()
    assert note["hl_id"] is not None
    marks = client.get(f"/api/papers/{pid}/highlights").json()
    assert len(marks) == 1
    assert marks[0]["id"] == note["hl_id"]
    assert (marks[0]["lang"], marks[0]["start"], marks[0]["end"]) == ("zh", 0, 6)
    assert marks[0]["color"] == "amber"                      # 没指定就退默认笔色
    # 屏幕上要真的上色：`<mark>` 是服务端渲染进 zh_html 的
    doc = client.get(f"/api/papers/{pid}/doc").json()
    para = next(b for b in doc["blocks"] if b["id"] == "b-0002")
    assert f'<mark class="hl hl-amber" data-h="{note["hl_id"]}"' in para["zh_html"]


def test_auto_highlight_uses_the_current_pen(client, owned):
    """颜色取**工具栏当前那支笔**（前端把 pen 带上来），不是死写默认色。"""
    pid = owned["pid"]
    note = client.post(f"/api/papers/{pid}/notes",
                       json={"content": "存疑", "block_id": "b-0002", "lang": "en",
                             "start": 0, "end": 6, "color": "pink"}).json()
    assert client.get(f"/api/papers/{pid}/highlights").json()[0]["color"] == "pink"
    assert note["hl_id"] is not None


def test_auto_highlight_reuses_an_identical_existing_mark(client, owned):
    """这一段**已经**划过 → 认旧的那一道，绝不"删旧插新"。

    否则这道划痕会换个 id，而**别的笔记还锚在旧 id 上**（`hl_id` 当场悬空、卡片圆点消失）。
    """
    pid = owned["pid"]
    old = _mk(client, pid, lang="en", start=0, end=6, color="green")
    note = client.post(f"/api/papers/{pid}/notes",
                       json={"content": "已有划痕", "block_id": "b-0002", "lang": "en",
                             "start": 0, "end": 6, "quote": "Safety", "color": "amber"}).json()
    assert note["hl_id"] == old["id"]
    rows = client.get(f"/api/papers/{pid}/highlights").json()
    assert len(rows) == 1 and rows[0]["color"] == "green"    # 已有的笔色不被笔记改掉


def test_note_with_existing_hl_id_creates_no_extra_mark(client, owned):
    pid = owned["pid"]
    hl = _mk(client, pid, lang="en", start=7, end=10)
    client.post(f"/api/papers/{pid}/notes",
                json={"content": "带 id", "block_id": "b-0002", "lang": "en",
                      "start": 7, "end": 10, "hl_id": hl["id"]})
    assert len(client.get(f"/api/papers/{pid}/highlights").json()) == 1


def test_block_level_note_highlights_nothing(client, owned):
    """整块笔记 / 文献级笔记**没有区间可划** → 不该凭空造一道划痕。"""
    pid = owned["pid"]
    client.post(f"/api/papers/{pid}/notes", json={"content": "整块", "block_id": "b-0002"})
    client.post(f"/api/papers/{pid}/notes", json={"content": "整篇"})
    client.post(f"/api/papers/{pid}/notes",       # 区间为空（start == end）也不算选区
                json={"content": "空区间", "block_id": "b-0002", "lang": "en",
                      "start": 3, "end": 3})
    assert client.get(f"/api/papers/{pid}/highlights").json() == []


def test_bad_color_on_note_writes_nothing(client, owned):
    """颜色不合法 → 400，且**笔记与划痕都不许落库**（校验发生在事务之前）。"""
    pid = owned["pid"]
    r = client.post(f"/api/papers/{pid}/notes",
                    json={"content": "金色笔", "block_id": "b-0002", "lang": "en",
                          "start": 0, "end": 6, "color": "gold"})
    assert r.status_code == 400
    assert client.get(f"/api/papers/{pid}/notes").json() == []
    assert client.get(f"/api/papers/{pid}/highlights").json() == []


def test_editing_a_translation_keeps_marks(client, owned):
    """改译文要按**新文本**重切划痕 —— 编辑一次顺手抹掉整页高亮是不能接受的。"""
    pid = owned["pid"]
    _mk(client, pid, lang="zh", start=0, end=3)
    r = client.patch(f"/api/docs/{pid}/blocks/b-0002", json={"zh": "安全性极其重要。它也难。"})
    assert r.status_code == 200
    assert '<mark class="hl hl-amber"' in r.json()["zh_html"]


def test_single_block_refresh_returns_marks(client, owned):
    """单块 GET 是"划一道立刻看见 <mark>"的取数路径（不重拉整篇）。"""
    pid = owned["pid"]
    hl = _mk(client, pid, lang="en", start=7, end=10)
    block = client.get(f"/api/docs/{pid}/blocks/b-0002").json()
    assert f'data-h="{hl["id"]}"' in block["en_html"]
    assert client.get(f"/api/docs/{pid}/blocks/b-nope").status_code == 404


# ── ⑤ 只读分享 ──────────────────────────────────────────────────────────
def test_share_sees_marks_but_cannot_write(client, owned):
    pid = owned["pid"]
    hl = _mk(client, pid, lang="en", start=0, end=6)
    client.post(f"/api/papers/{pid}/notes",
                json={"content": "给读者看的批注", "block_id": "b-0002", "lang": "en",
                      "start": 0, "end": 6, "quote": "Safety", "hl_id": hl["id"]})
    token = client.post(f"/api/plans/{owned['plan_id']}/shares",
                        json={"hours": 1, "label": "给导师"}).json()["token"]
    client.post("/api/auth/logout")

    marks = client.get(f"/api/shares/{token}/papers/{pid}/highlights").json()
    assert [(m["start"], m["end"], m["color"]) for m in marks] == [(0, 6, "amber")]
    notes = client.get(f"/api/shares/{token}/papers/{pid}/notes").json()
    assert notes and notes[0]["hl_id"] == hl["id"]
    # 分享页看到的 HTML 也必须带划痕（看得见但不能改）
    doc = client.get(f"/api/shares/{token}/papers/{pid}").json()
    para = next(b for b in doc["blocks"] if b["id"] == "b-0002")
    assert '<mark class="hl' in para["en_html"]
    # 写操作一律 403（⑪ 由中间件兜底，不靠前端不渲染按钮）
    assert client.post(f"/api/shares/{token}/papers/{pid}/highlights",
                       json={"block_id": "b-0002", "lang": "en", "start": 0,
                             "end": 3}).status_code == 403
    assert client.delete(f"/api/highlights/{hl['id']}").status_code == 401


# ── ⑥ ㉚ → ㉛ 的存量换算（句锚点 → 字符区间）─────────────────────────────
def test_legacy_sid_highlights_are_migrated_to_ranges(settings):
    """生产上有真数据（12 号文献 4 条）→ 换算必须**确定性**，不能丢。

    换算得出来是因为：块 ID 已在 sid 里、语言已在 sid 里、切句规则没变。
    """
    from papershelf.server.db import HIGHLIGHTS_DDL, _migrate, connect, init_db

    init_db(settings)
    conn = connect(settings)
    try:
        pid = _seed(settings, _mk_plan(conn))
        conn.execute("DROP TABLE highlights")
        conn.executescript(_LEGACY_HL_DDL)
        conn.execute("INSERT INTO highlights (paper_id, sid, created_at) VALUES (?,?,?)",
                     (pid, "b-0002:e1", "2026-01-01T00:00:00+00:00"))   # 第 2 句
        conn.commit()
        _migrate(conn)
        row = conn.execute("SELECT * FROM highlights").fetchone()
        assert (row["block_id"], row["lang"]) == ("b-0002", "en")
        assert EN[row["start"]:row["end"]] == "It is also hard."    # 换算落在第 2 句上
        assert row["color"] == "amber"                              # 存量划痕给默认笔色
        # 幂等：再跑一次迁移不改动已迁移的数据
        conn.execute("DROP TABLE highlights")
        conn.executescript(HIGHLIGHTS_DDL)
        conn.execute("INSERT INTO highlights (paper_id, block_id, lang, start, end, color,"
                     " created_at) VALUES (?,?,?,?,?,?,?)",
                     (pid, "b-0002", "en", 0, 6, "green", "x"))
        conn.commit()
        _migrate(conn)
        assert conn.execute("SELECT color FROM highlights").fetchone()["color"] == "green"
    finally:
        conn.close()


def test_unresolvable_legacy_sid_is_dropped(settings):
    """块已经不在（或 sid 不属于本仓库那套）→ 丢弃并记数，不留一行没有坐标的高亮。"""
    from papershelf.server.db import _migrate, connect, init_db

    init_db(settings)
    conn = connect(settings)
    try:
        pid = _seed(settings, _mk_plan(conn))
        conn.execute("DROP TABLE highlights")
        conn.executescript(_LEGACY_HL_DDL)
        for sid in ("b-9999:e0", "p3e1", "b-0002:e9"):
            conn.execute("INSERT INTO highlights (paper_id, sid, created_at) VALUES (?,?,?)",
                         (pid, sid, "x"))
        conn.commit()
        _migrate(conn)
        assert conn.execute("SELECT COUNT(*) c FROM highlights").fetchone()["c"] == 0
    finally:
        conn.close()


def _mk_plan(conn) -> int:
    conn.execute("INSERT INTO users (email,password_hash,status,created_at)"
                 " VALUES ('m@x.edu.cn','h','active',datetime('now'))")
    conn.execute("INSERT INTO plans (user_id,name,created_at) VALUES"
                 " ((SELECT id FROM users LIMIT 1),'p',datetime('now'))")
    conn.commit()
    return conn.execute("SELECT id FROM plans ORDER BY id DESC LIMIT 1").fetchone()["id"]
