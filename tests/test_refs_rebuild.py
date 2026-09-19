"""「修复参考文献」= 在**已落库的产物**上重跑条目合并，**批注跟着搬家**（2026-09-19）。

宿主原话：「push 上生产，生产上你第一篇 pdf 我已经添加了不少笔记了，想办法在不影响笔记的
情况下，可以手动改一下后面的 References？」

背景：解析 v14 修掉了「参考文献只有第一段能合并」（`_ref_entry_starts` 原先把连号起点钉死在
1，而跨页页眉/页脚会把文献流截成好几段），但**存量产物不会自己变好** —— 原来的唯一出口
「重新提取」会 `DELETE FROM notes/highlights`（块 id 与文本都会变），对已经读过并做了批注的
篇目不可接受。于是有了 `server.refsfix.rebuild_paper_refs`。

本文件钉住它的四条不变量（顺序就是风险从大到小）：

1. **批注一条都不许丢、不许落错地方** —— 正文块 id 不变（锚点天然不动）；钉在
   被合并掉的碎片上的，按 `RefSpan` 换算到新条目，偏移量用**独立字符串搜索**核对
   （中文侧还要过"标题被换成中文"的长度差）；
2. **文字逐字守恒**（合并只动空白，一个字符都不增删）；
3. **幂等** —— 再跑一次是 no-op（不落库、不调模型）；
4. **不越界** —— 带 `band` 戳的碎片（落在文献页上的页眉）与切不出条目的段（APA 体例）
   一律原样不动。
"""

from __future__ import annotations

import re

import pytest

from papershelf.pipeline.model import Block
from papershelf.pipeline.translator import ref_zh_text
from papershelf.server.refsfix import _to_zh, rebuild_paper_refs


# ── 夹具 ────────────────────────────────────────────────────────────────
def _flat(s: str) -> str:
    return "".join(s.split())


class FakeTr:
    """模拟第三条通道（`_translate_ref`）：把条目里的 `TITLE-k` 换成中文标题。

    ⚠️ 走**真的** `ref_zh_text`：`rebuild_paper_refs` 之后要按"标题换了"算偏移，
    自己编一个假的替换规则会让那段映射测了个寂寞。
    """

    def __init__(self) -> None:
        self.tokens_used = 0
        self.needs_review: list[str] = []
        self.calls: list[set[str]] = []

    def ordered(self, blocks):
        return [b for b in blocks
                if b.type != "figure" or b.en or (b.payload or {}).get("caption")]

    def translate_blocks(self, blocks, *, only=None, **kw):
        self.calls.append(set(only or ()))
        out: list[str] = []
        for b in self.ordered(blocks):
            if only is not None and b.id not in only:
                out.append("")
                continue
            m = re.match(r"\[(\d+)\]", b.en)
            if b.type != "ref" or not m:
                out.append("")
                continue
            k = m.group(1)
            zh, _why = ref_zh_text(b.en, f"TITLE-{k}", f"中文标题{k}")
            if zh:
                b.payload["title_en"], b.payload["title_zh"] = f"TITLE-{k}", f"中文标题{k}"
                b.zh = zh
                self.tokens_used += 100
                out.append(zh)
            else:
                self.needs_review.append(b.id)
                out.append("")
        return out


@pytest.fixture()
def seeded(settings):
    """一篇文章：正文 4 块 + 文末三段参考文献（中间夹一个页眉）+ 一条 APA 段。"""
    from papershelf.server.db import connect, tx
    from papershelf.server.repo import save_doc
    from papershelf.pipeline.model import Doc

    conn = connect(settings)
    with tx(conn):
        conn.execute("INSERT INTO users (id,email,password_hash,status,created_at)"
                     " VALUES (1,'u@tsinghua.edu.cn','x','active','')")
        conn.execute("INSERT INTO plans (id,user_id,name) VALUES (1,1,'p')")
        conn.execute("INSERT INTO papers (id,plan_id,title,conv_state) "
                     "VALUES (1,1,'测试文献','done')")

    # 正文（笔记/划痕钉在这里）—— id 必须**一个都不变**，否则批注会挪到别的句子上。
    body = [
        Block(id="b-0001", type="h2", en="1 Introduction", zh="1 引言", zh_source="mt"),
        Block(id="b-0002", type="p", en="Metal additive manufacturing is growing fast.",
              zh="金属增材制造发展迅速。", zh_source="mt"),
        Block(id="b-0003", type="p", en="Closed-loop quality assurance matters.",
              zh="闭环质量保证很重要。", zh_source="mt"),
        Block(id="b-0004", type="h2", en="References", zh="参考文献", zh_source="mt",
              section="REFERENCES"),
    ]
    # 第 1 段碎片：切口落在词中间（`man-` + `ufacturing`），一块里还塞着下一条的开头。
    f1 = Block(id="b-0005", type="refs", section="REFERENCES",
               en="[1] A. Author, B. Author. TITLE-1 sensing in metal additive man-",
               payload={"page": 8})
    f2 = Block(id="b-0006", type="refs", section="REFERENCES",
               en="ufacturing. Journal of Manufacturing Processes 12(3):45. "
                  "[2] C. Author. TITLE-2 for laser powder bed fusion. Materials 9(1):1.",
               payload={"page": 8})
    # 落在文献页上的**页眉**：3c 也会把它打成 `refs`，但它带 `band` 戳 ⇒ 不许并进来。
    band = Block(id="b-0007", type="refs", section="REFERENCES",
                 en="J. Manuf. Process. 160 (2026) 50-81", payload={"band": "top", "page": 9})
    # 第 2 段碎片：被上面那个页眉截断的第二段（这正是 v14 修的那个真病灶）。
    f3 = Block(id="b-0008", type="refs", section="REFERENCES",
               en="[3] D. Author. TITLE-3 of machine learning in AM. Additive Manufacturing 5:2.",
               payload={"page": 9})
    f4 = Block(id="b-0009", type="refs", section="REFERENCES",
               en="[4] E. Author. TITLE-4 and process monitoring. Metals 14(2):195.",
               payload={"page": 9})
    # ⚠️ 这里必须放一个**非 refs** 的块：相邻的 `refs` 块属于**同一段**，而一段里
    #    "最后一条之后的所有文字"归最后一条（`split_ref_entries` 的语义，v13 起如此）。
    #    真实篇目里这类段落之间通常夹着图/表，所以这个形状更接近生产。
    fig = Block(id="b-0010", type="figure", section="REFERENCES",
                en="Fig. 9. A figure between two reference blocks.", payload={"page": 9})
    # APA 体例：没有编号 ⇒ 切不出条目 ⇒ 必须**原样**留着（不是"清掉重来"）。
    apa = Block(id="b-0011", type="refs", section="REFERENCES",
                en="Author, A. (2020). A title without numbers. Journal of Things, 3, 1-9.",
                payload={"page": 9})

    doc = Doc(meta={"refs_start": "b-0004", "refs_count": 7},
              assets=[], blocks=body + [f1, f2, band, f3, f4, fig, apa])
    save_doc(conn, 1, doc)

    def anchor(table, block_id, lang, s, e, **extra):
        cols = ", ".join(extra)
        ph = ", ".join("?" for _ in extra)
        conn.execute(
            f"INSERT INTO {table} (paper_id, block_id, lang, start, end"
            + (f", {cols}" if cols else "") + ") VALUES (1,?,?,?,?"
            + (f", {ph}" if cols else "") + ")",
            (block_id, lang, s, e, *extra.values()))

    with tx(conn):
        # 正文锚点（两条语言各一）—— 它们**不该**被动到。
        anchor("highlights", "b-0002", "en", 0, 20, color="amber")
        anchor("notes", "b-0003", "zh", 0, 6, content="正文笔记", created_at="2026-09-19")
        # 碎片锚点：英文一条、中文一条，都钉在同一个字符串上（便于独立核对）。
        # ⚠️ 下标**现场算**，不手写 —— 手写过一次，注释写「Journal of」而数字落在别的词上，
        #    结果是映射明明是对的、测试却红（核对锚点这类断言最容易被自己骗）。
        js = f2.en.index("Journal of")
        assert f2.en[js:js + 10] == "Journal of"
        anchor("highlights", "b-0006", "en", js, js + 10, color="green")
        anchor("notes", "b-0006", "zh", js, js + 10, content="碎片上的笔记",
               created_at="2026-09-19")
        # 整块锚（无字符区间）钉在另一段碎片上。
        anchor("notes", "b-0008", None, None, None, content="整块笔记", created_at="2026-09-19")
    return conn


def _blocks(conn):
    from papershelf.server.repo import load_doc
    doc = load_doc(conn, 1)
    assert doc is not None
    return doc


# ── 1. 主路径：合并且批注无损 ────────────────────────────────────────────
def test_rebuild_merges_fragments_and_keeps_body_ids(seeded):
    tr = FakeTr()
    got = rebuild_paper_refs(seeded, 1, translator=tr, log=lambda *a: None)
    assert got["ok"] and got["changed"]
    assert got["refs"] == 6 and got["entries"] == 4        # 6 碎片 → 4 条

    doc = _blocks(seeded)
    # 正文块 id 与内容**逐字未变**（批注坐标的前提）
    assert [b.id for b in doc.blocks[:4]] == ["b-0001", "b-0002", "b-0003", "b-0004"]
    assert doc.blocks[1].en == "Metal additive manufacturing is growing fast."
    # 三条入口都成了整条 `ref`，且**都带中文**
    refs = [b for b in doc.blocks if b.type == "ref"]
    assert len(refs) == 4
    assert all(b.zh and "中文标题" in b.zh for b in refs)
    # 新 id 必须**不撞**现有块，且接着最大号往下发
    old = {f"b-{i:04d}" for i in range(1, 12)}
    assert not (set(b.id for b in refs) & old)
    assert sorted(b.id for b in refs) == ["b-0012", "b-0013", "b-0014", "b-0015"]
    # 只翻新块（`only` 里不该混进正文块）
    assert tr.calls == [{"b-0012", "b-0013", "b-0014", "b-0015"}]


def test_rebuild_moves_anchors_with_exact_offsets(seeded):
    """碎片上的批注要**落在同一段文字上** —— 用独立字符串搜索核对偏移。"""
    rebuild_paper_refs(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    doc = _blocks(seeded)
    by_id = {b.id: b for b in doc.blocks}
    rows = {r["id"]: dict(r) for r in seeded.execute("SELECT * FROM highlights").fetchall()}
    notes = {r["id"]: dict(r) for r in seeded.execute("SELECT * FROM notes").fetchall()}

    # 正文锚点：块 id、区间一个都没变
    assert rows[1]["block_id"] == "b-0002" and (rows[1]["start"], rows[1]["end"]) == (0, 20)
    n_body = next(n for n in notes.values() if n["content"] == "正文笔记")
    assert n_body["block_id"] == "b-0003" and (n_body["start"], n_body["end"]) == (0, 6)

    # 碎片锚点：搬到第 1 条新条目上，且**正好框住「Journal of」**
    hl = rows[2]
    b = by_id[hl["block_id"]]
    assert b.type == "ref" and "[1]" in b.en
    j = b.en.index("Journal of")                     # 期望值**独立算**，不用夹具里的下标
    assert (hl["start"], hl["end"]) == (j, j + len("Journal of"))

    # 中文侧：中文栏里同样要框住「Journal of」（标题换中文后长度变了，偏移要跟着走）
    n_zh = next(n for n in notes.values() if n["content"] == "碎片上的笔记")
    zb = by_id[n_zh["block_id"]]
    assert n_zh["block_id"] == hl["block_id"]
    zj = zb.zh.index("Journal of")
    assert (n_zh["start"], n_zh["end"]) == (zj, zj + len("Journal of"))
    assert n_zh["start"] != hl["start"]              # 确实被平移过

    # 整块锚：只需换块 id，落到覆盖碎片开头的那一条上
    n_whole = next(n for n in notes.values() if n["content"] == "整块笔记")
    assert n_whole["block_id"] == "b-0014" and n_whole["start"] is None
    assert by_id["b-0014"].en.startswith("[3]")
    # 一条都没丢
    assert len(rows) == 2 and len(notes) == 3


def test_anchor_on_the_next_entry_inside_the_same_fragment(seeded):
    """一个碎片里塞着**下一条的开头**（真数据常态）：锚点要跟着那一条走，不是跟着碎片走。"""
    f2 = seeded.execute("SELECT en FROM blocks WHERE paper_id=1 AND id='b-0006'").fetchone()["en"]
    k = f2.index("Materials 9")                       # 第 2 条里的期刊名
    seeded.execute("INSERT INTO highlights (paper_id, block_id, lang, start, end, color)"
                   " VALUES (1,'b-0006','en',?,?,'amber')", (k, k + 9))
    seeded.commit()
    new_id = seeded.execute("SELECT max(id) m FROM highlights").fetchone()["m"]

    rebuild_paper_refs(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    doc = _blocks(seeded)
    by_id = {b.id: b for b in doc.blocks}
    hl = seeded.execute("SELECT * FROM highlights WHERE id=?", (new_id,)).fetchone()
    b = by_id[hl["block_id"]]
    assert b.en.startswith("[2]")                     # 落到第 2 条上
    assert b.en[hl["start"]:hl["end"]] == "Materials"


def test_rebuild_conserves_every_character(seeded):
    """合并只动空白 —— 一个字符都不许增删（老规矩，见 tests/test_refs.py）。"""
    before = _blocks(seeded)
    frag_text = _flat("".join(b.en for b in before.blocks
                              if b.type == "refs" and not (b.payload or {}).get("band")))
    rebuild_paper_refs(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    doc = _blocks(seeded)
    tail = [b for b in doc.blocks if b.type in ("refs", "ref")
            and not (b.payload or {}).get("band")]
    assert _flat("".join(b.en for b in tail)) == frag_text


# ── 2. 不越界 ───────────────────────────────────────────────────────────
def test_banded_fragments_and_unsplittable_runs_stay_put(seeded):
    """页眉（`band`）与 APA 段都**原样**留着 —— 不动它们才是对的。"""
    rebuild_paper_refs(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    doc = _blocks(seeded)
    band = next(b for b in doc.blocks if b.id == "b-0007")
    assert band.type == "refs" and band.en == "J. Manuf. Process. 160 (2026) 50-81"
    assert band.payload["band"] == "top"

    apa = next(b for b in doc.blocks if b.id == "b-0011")
    assert apa.type == "refs" and apa.en.startswith("Author, A. (2020).")

    # 位置也按原顺序：2 条 ref → 页眉 → 2 条 ref → 图 → APA
    assert [b.id for b in doc.blocks[4:]] == ["b-0012", "b-0013", "b-0007",
                                              "b-0014", "b-0015", "b-0010", "b-0011"]


def test_no_translate_leaves_new_entries_english(seeded):
    got = rebuild_paper_refs(seeded, 1, translate=False, log=lambda *a: None)
    assert got["changed"] and got["tokens"] == 0
    doc = _blocks(seeded)
    refs = [b for b in doc.blocks if b.type == "ref"]
    assert refs and all(not (b.zh or "").strip() for b in refs)


# ── 3. 幂等 ─────────────────────────────────────────────────────────────
def test_second_run_is_a_noop(seeded):
    rebuild_paper_refs(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    snap = [(b.id, b.type, b.en, b.zh) for b in _blocks(seeded).blocks]
    tr = FakeTr()
    got = rebuild_paper_refs(seeded, 1, translator=tr, log=lambda *a: None)
    assert got["ok"] and not got["changed"] and got["tokens"] == 0
    assert tr.calls == []                       # 没调模型
    assert [(b.id, b.type, b.en, b.zh) for b in _blocks(seeded).blocks] == snap


def test_doc_metadata_and_block_count_follow(seeded):
    rebuild_paper_refs(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    row = seeded.execute("SELECT block_count, meta FROM docs WHERE paper_id=1").fetchone()
    import json
    assert row["block_count"] == len(_blocks(seeded).blocks)
    assert json.loads(row["meta"])["refs_count"] == 6       # 4 条 ref + 页眉 + APA


# ── 4. 中文侧偏移映射（纯函数） ──────────────────────────────────────────
def test_zh_offset_mapping_shifts_by_the_title_length():
    """`_to_zh`：标题前不动、标题后平移、落进标题里夹到标题开头。"""
    b = Block(id="b-0001", type="ref", en="[1] A. B. TITLE-X here. Journal 1:2.",
              payload={"title_en": "TITLE-X", "title_zh": "中文标题中文标题中文标题"})
    zh, _ = ref_zh_text(b.en, "TITLE-X", "中文标题中文标题中文标题")
    b.zh = zh
    a = b.en.index("TITLE-X")
    z = a + len("TITLE-X")
    assert b.zh[:a] == b.en[:a]                     # 标题之前逐字相同
    assert b.zh.index("Journal") == b.en.index("Journal") + (12 - len("TITLE-X"))
    assert _to_zh(b, 0, a) == (0, a)                # 标题前：不动
    assert _to_zh(b, a, a + 3) == (a, a)            # 落在标题里：夹到标题开头
    j = b.en.index("Journal")
    assert _to_zh(b, j, j + 7) == (b.zh.index("Journal"), b.zh.index("Journal") + 7)
    assert _to_zh(b, 0, len(b.en))[1] == len(b.zh)  # 到末尾：跟到中文末尾


def test_zh_mapping_is_identity_when_the_title_was_not_translated():
    """没译出来的条目：中文栏回落原文 ⇒ 坐标不该被平移。"""
    b = Block(id="b-0001", type="ref", en="[1] A. B. TITLE-X here.", payload={})
    assert (b.zh or "") == ""
    assert _to_zh(b, 3, 9) == (3, 9)


# ══ 5. 排队接线：`pending_job='refs'` 走同一台状态机（2026-09-19 宿主点单）══════
#
# 上面钉的是 `rebuild_paper_refs` 这个**函数**。可它在生产上根本调不到 —— 除非有人
# 进容器跑 CLI。宿主要的是一条**自己点得到**的路（「可以手动改一下后面的 References？」），
# 于是它被接进常驻队列：`POST /api/papers/{id}/rebuild-refs` → `pending_job='refs'`
# → `converter._run_refs_fix`。下面每一条都对应一个"接错线"的具体坏法。
def _job(settings, pid: int) -> dict:
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        return dict(conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone())
    finally:
        conn.close()


def _queue_refs(settings, pid: int) -> None:
    from papershelf.server.converter import enqueue
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        enqueue(conn, pid, job="refs", reset_attempts=True)
    finally:
        conn.close()


def test_converter_runs_the_refs_job_without_touching_the_pipeline(seeded, settings,
                                                                  monkeypatch):
    """`pending_job='refs'` ⇒ 只跑修文献：**不解析、不校对、不重译正文**，批注无损。

    ⚠️ 这里把 `converter._run` 换成会炸的替身：它一旦被碰到，说明这次排队又跑了
    整条管线（用户点的是"只修参考文献"，代价差着两个数量级）。
    """
    import papershelf.server.refsfix as refsfix
    import papershelf.server.converter as converter

    monkeypatch.setattr(converter, "_run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("只修参考文献的队列任务**不许**跑整条管线")))
    monkeypatch.setattr(refsfix, "_make_translator", lambda conn, pid: FakeTr())

    before_notes = seeded.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    before_marks = seeded.execute("SELECT COUNT(*) FROM highlights").fetchone()[0]
    _queue_refs(settings, 1)
    converter.convert_paper(1)

    row = _job(settings, 1)
    assert row["conv_state"] == "done", \
        f"跑完必须是 done（现在是 {row['conv_state']}/{row['conv_error']}）"
    assert row["pending_job"] is None, "终态必须清掉 pending_job，否则下次排队会被它劫持"
    assert row["tokens_used"] == 400, "4 条新条目 × 100 token 要记进用量"
    assert seeded.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == before_notes
    assert seeded.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == before_marks
    assert len([b for b in _blocks(seeded).blocks if b.type == "ref"]) == 4


def test_refs_job_failure_lands_failed_and_clears_the_job(seeded, settings, monkeypatch):
    """炸了要落 `failed` + 带上原因，**并且**清掉 `pending_job`（不许留着污染下一轮）。"""
    import papershelf.server.refsfix as refsfix
    import papershelf.server.converter as converter

    def boom(*a, **k):
        raise RuntimeError("模型欠费")

    monkeypatch.setattr(refsfix, "rebuild_paper_refs", boom)
    _queue_refs(settings, 1)
    converter.convert_paper(1)
    row = _job(settings, 1)
    assert row["conv_state"] == "failed" and row["pending_job"] is None
    assert "模型欠费" in (row["conv_error"] or "")


def test_a_normal_retry_is_not_hijacked_by_a_leftover_refs_job(seeded, settings, client,
                                                              make_user, monkeypatch):
    """**最可能的坏结局**：`pending_job` 是跨请求存活的 —— 用户点过「重建参考文献」，
    再点「重新转换」，那次转换会被上一轮的 `refs` 劫持（跑的不是他要的事）。

    所以每个排队入口都必须显式声明 `job`；重试入口声明的是 `None`（= 完整转换）。
    这条测试走的是**真路由**（而不是 `enqueue` 本身）—— 要钉住的正是"路由有没有记得声明"。
    """
    from papershelf.server.db import connect
    from papershelf.server.routers import papers as papers_router

    monkeypatch.setattr(papers_router, "convert_paper", lambda *a, **k: None)
    email, pw = make_user()
    conn = connect(settings)
    try:
        # ⚠️ 计划的归属必须**查出来**（`seeded` 已经占了 user 1，`make_user` 拿到的不是它）——
        #    写死 user_id 会让请求 404，而 404 只会红成"夹具不对"、不会红成"代码错了"
        #    （本轮就在这上面白跑了一轮）。
        uid = int(conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0])
        conn.execute("INSERT INTO plans (id,user_id,name) VALUES (9,?,'p')", (uid,))
        conn.execute("INSERT INTO papers (id,plan_id,title,conv_state,pending_job,"
                     "conv_attempts) VALUES (9,9,'t','failed','refs',2)")
        conn.commit()
    finally:
        conn.close()
    client.post("/api/auth/login", json={"email": email, "password": pw})

    assert client.post("/api/papers/9/convert").status_code == 202
    row = _job(settings, 9)
    assert row["pending_job"] is None, "重试入口必须把 job 清成「完整转换」"
    assert row["conv_state"] == "queued" and row["conv_attempts"] == 0


def test_rebuild_refs_endpoint_queues_the_job(settings, client, make_user, monkeypatch):
    """端点：已生成的文献 → 202 + `pending_job='refs'`；**没有产物**的 → 409。

    ⚠️ 把路由里的 `convert_paper` 换成哑替身：TestClient 会**就地跑完**后台任务，
    否则这条测试实际是在考"跑完之后长什么样"（那由 `_run_refs_fix` 那几条负责），
    真正要钉的是**排队那一刻的库状态**。
    """
    from papershelf.server.db import connect
    from papershelf.server.routers import papers as papers_router

    monkeypatch.setattr(papers_router, "convert_paper", lambda *a, **k: None)

    email, pw = make_user()
    conn = connect(settings)
    try:
        conn.execute("INSERT INTO plans (id,user_id,name) VALUES (9,1,'p')")
        conn.execute("INSERT INTO papers (id,plan_id,title,conv_state,conv_attempts) "
                     "VALUES (9,9,'t','done',2)")
        conn.execute("INSERT INTO papers (id,plan_id,title,conv_state) "
                     "VALUES (8,9,'u','none')")
        conn.commit()
    finally:
        conn.close()
    client.post("/api/auth/login", json={"email": email, "password": pw})

    r = client.post("/api/papers/9/rebuild-refs")
    assert r.status_code == 202, r.text
    assert r.json()["job"] == "refs"
    row = _job(settings, 9)
    assert row["conv_state"] == "queued" and row["pending_job"] == "refs"
    assert row["conv_attempts"] == 0, "人工入口把重试计数归零（与重试/重新提取一致）"

    # 没有产物 → 409（不能让用户点完只得到一句"转换完成"而其实什么都没生成）
    assert client.post("/api/papers/8/rebuild-refs").status_code == 409
    # 转换中 → 409
    conn = connect(settings)
    try:
        conn.execute("UPDATE papers SET conv_state='doing' WHERE id=9")
        conn.commit()
    finally:
        conn.close()
    assert client.post("/api/papers/9/rebuild-refs").status_code == 409


def test_migration_adds_pending_job_to_a_legacy_library(settings):
    """迁移：真删列的旧库要能补回来，且**存量行留 NULL**（= 完整转换，行为不变）。"""
    from papershelf.server.db import _migrate, connect

    conn = connect(settings)
    try:
        conn.execute("ALTER TABLE papers DROP COLUMN pending_job")   # 模拟旧库
        conn.commit()
        assert "pending_job" not in {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
        _migrate(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
    finally:
        conn.close()
    assert "pending_job" in cols
