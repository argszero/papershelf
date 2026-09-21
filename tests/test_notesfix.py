"""「修复小字注区」= 回到 PDF 重跑解析、**只改写注区那几块**，批注跟着搬家（2026-09-21）。

宿主 2026-09-21 两张截图（生产 `reader/2` 的阅读器 vs PDF 原件）：「这一段提取的不对」
「主要是样式不对」→ 解析 v16 把四件事补上（按源行分行、记字号比、行首上标小标、注上那条
**页中**横线）。但**存量产物不会自己变好**，而原来的唯一出口「重新提取」会清掉**整篇**的
`notes` / `highlights` —— 对已经读过并做了批注的篇目不可接受。

于是有了 `server/notesfix.rebuild_paper_notes`。本文件钉住它的四条不变量（按风险从大到小）：

1. **批注一条都不许丢、不许落错地方** —— 只改写注区块 ⇒ 别的块上的锚点天然不动；
   钉在注区块上的英文锚点按"只动空白"的映射搬家（偏移用**独立字符串搜索**核对）；
2. **只动空白**：注区块的新旧文本去掉空白后必须逐字相同 —— 不满足就**不写**
   （认不出对应块 / 形状对不上 ⇒ 跳过，绝不猜）；
3. **幂等**：再跑一次是 no-op（不落库、不调模型）；
4. **中文侧是重新翻译** ⇒ 中文锚点按"锚点落空"处置（划痕删、笔记转文献级且**留住内容**）；
   译不出来时保留旧译文（它只差空白，读起来照样对）。
"""

from __future__ import annotations

import json

import pytest

from papershelf.pipeline.model import Block
from papershelf.server import notesfix

# 生产 paper 2 第 21 页那条 a–e 表注的真实形状：解析 v15 出来是**一段**（`" ".join(split)`，
# 行首小标与后面的字之间没有空格），v16 出来是**四行**（换行 + 补一个空格）。
OLD_EN = ("All studies utilising DED-LB/M were powder-fed, and terminology used in studies has "
          "been preserved where possible aThese studies utilise an off-line optical micrograph "
          "image dataset, known as UB-Moog b Simulated sensor data was used in this study "
          "c Different aspects of the same study")
NEW_EN = ("All studies utilising DED-LB/M were powder-fed, and terminology used in studies has "
          "been preserved where possible\n"
          "a These studies utilise an off-line optical micrograph image dataset, known as "
          "UB-Moog\n"
          "b Simulated sensor data was used in this study\n"
          "c Different aspects of the same study")


def _fresh_note(block_id: str = "b-0999", *, en: str = NEW_EN, page: int = 21) -> Block:
    return Block(id=block_id, type="p", en=en, zh="", zh_source="none", payload={
        "page": page,
        "note": {"em": 0.85},
        "markers": ["", "a", "b", "c"],
        "rule": {"side": "above", "color": "#000000", "width": 1.0},
    })


class FakeTr:
    """模拟翻译：把注区块译成"同样分行的中文"。

    ⚠️ 走**真的**调用形状（`ordered` + `translate_blocks(only=…)`）：`rebuild_paper_notes`
    要靠 `tr.ordered(tmp)` 与返回值 zip 起来对块号，自己编一个假的返回值顺序会让那段
    对不上号（那正是"译文写错块"这类事故的来源）。
    """

    # ⚠️ 新译文与**旧译文共享开头那几个词**（`所有研究都使用粉末送料`）—— 真实的重译
    #    就是这个形状，而它正好是"中文锚点能不能硬映射"这条判据的试金石：
    #    共享前缀会让 `remap_span` 找得到映射 ⇒ 锚点会被贴到**别的字**上（看起来对，
    #    其实没有任何字级对应）。本工具的取向是**一律不映射**（见 `rebuild_paper_notes`）。
    OLD_ZH = "所有研究都使用粉末送料 a这些研究使用离线数据集 b 使用模拟传感数据 c 同一项研究的不同方面"

    def __init__(self, zh: str = "所有研究都使用粉末送料\n a 这些研究使用离线数据集\n"
                                  " b 使用模拟传感数据\n c 同一项研究的不同方面",
                 fail: bool = False) -> None:
        self.zh = zh
        self.fail = fail
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
            if self.fail:
                self.needs_review.append(b.id)
                out.append("")
                continue
            self.tokens_used += 1200
            out.append(self.zh)
        return out


# ── 夹具 ────────────────────────────────────────────────────────────────
def _flat(s: str) -> str:
    return "".join(s.split())


@pytest.fixture()
def seeded(settings, tmp_path, monkeypatch):
    """一篇文章：正文 3 块 + 一个**旧形状**的小字注区块（＋钉在各处的批注）。"""
    from papershelf.pipeline.model import Doc
    from papershelf.server.db import connect, tx
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    pdf = tmp_path / "p1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% real parsing is monkeypatched away\n")
    with tx(conn):
        conn.execute("INSERT INTO users (id,email,password_hash,status,created_at)"
                     " VALUES (1,'u@tsinghua.edu.cn','x','active','')")
        conn.execute("INSERT INTO plans (id,user_id,name) VALUES (1,1,'p')")
        conn.execute("INSERT INTO papers (id,plan_id,title,conv_state,pdf_path) "
                     "VALUES (1,1,'测试文献','done',?)", (str(pdf),))

    body = [
        Block(id="b-0001", type="h2", en="3 Results", zh="3 结果", zh_source="mt"),
        Block(id="b-0002", type="p", en="Grain size decreases with energy density.",
              zh="晶粒尺寸随能量密度下降。", zh_source="mt"),
        Block(id="b-0003", type="p", en="Porosity stays below one percent.",
              zh="孔隙率低于百分之一。", zh_source="mt"),
    ]
    note = Block(id="b-0004", type="p", en=OLD_EN, zh=FakeTr.OLD_ZH, zh_source="mt",
                 payload={"page": 21})
    save_doc(conn, 1, Doc(meta={}, assets=[], blocks=body + [note]))

    def anchor(table, block_id, lang, s, e, **extra):
        cols = list(extra)
        conn.execute(
            f"INSERT INTO {table} (paper_id, block_id, lang, start, end"
            + (f", {', '.join(cols)}" if cols else "") + ") VALUES (1,?,?,?,?"
            + (f", {', '.join('?' for _ in cols)}" if cols else "") + ")",
            (block_id, lang, s, e, *[extra[c] for c in cols]))

    # 下标**现场算**，不手写（手写过一次：注释写 A、数字落在 B 上，映射明明是对的、测试却红）
    opt_en = OLD_EN.index("optical")
    grain = body[1].en.index("Grain")
    with tx(conn):
        anchor("highlights", "b-0004", "en", opt_en, opt_en + 7, color="amber")   # 要跟着搬
        anchor("highlights", "b-0004", "zh", 0, 5, color="green")                # 中文重译 ⇒ 删
        anchor("notes", "b-0004", "en", opt_en, opt_en + 7, content="英文区间笔记",
               created_at="2026-09-21")
        anchor("notes", "b-0004", "zh", 0, 5, content="中文区间笔记", created_at="2026-09-21")
        anchor("notes", "b-0004", None, None, None, content="整块笔记", created_at="2026-09-21")
        anchor("highlights", "b-0002", "en", grain, grain + 5, color="blue")     # 不许动
        anchor("notes", "b-0002", "en", grain, grain + 5, content="正文笔记",
               created_at="2026-09-21")

    # 重跑解析的唯一入口（真实现要读 PDF、要 fitz；这里替换成"已经解析好的新块"）
    monkeypatch.setattr(notesfix, "_fresh_blocks", lambda pdf: [_fresh_note()])
    return conn


def _note(conn):
    r = conn.execute("SELECT * FROM blocks WHERE paper_id=1 AND id='b-0004'").fetchone()
    return dict(r), json.loads(r["payload"])


def _marks(conn):
    hl = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM highlights").fetchall()}
    nt = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM notes").fetchall()}
    return hl, nt


# ── 1. 主路径 ────────────────────────────────────────────────────────────
def test_note_block_is_rewritten_in_place_with_lines_markers_and_rule(seeded):
    tr = FakeTr()
    got = notesfix.rebuild_paper_notes(seeded, 1, translator=tr, log=lambda *a: None)
    assert got["ok"] and got["changed"] and got["notes"] == 1

    row, payload = _note(seeded)
    assert row["en"] == NEW_EN
    assert payload["note"] == {"em": 0.85}
    assert payload["markers"] == ["", "a", "b", "c"]
    assert payload["rule"] == {"side": "above", "color": "#000000", "width": 1.0}
    assert row["zh"] == tr.zh and row["zh_source"] == "mt"
    # **只翻这一块**（别把正文块也送进模型 = 白花钱 + 译文可能被改写）
    assert tr.calls == [{"b-0004"}]

    # 别的块**逐字节未变**（id、文本、payload 一起比）
    for bid, en in (("b-0001", "3 Results"),
                    ("b-0002", "Grain size decreases with energy density."),
                    ("b-0003", "Porosity stays below one percent.")):
        r = dict(seeded.execute("SELECT * FROM blocks WHERE id=?", (bid,)).fetchone())
        assert r["en"] == en and json.loads(r["payload"]) == {}


def test_every_non_whitespace_character_is_conserved(seeded):
    """改写**只动空白**（判据本身就是这么定的）—— 逐字核对一遍。"""
    notesfix.rebuild_paper_notes(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    row, _ = _note(seeded)
    assert _flat(row["en"]) == _flat(OLD_EN)
    assert row["en"].count("\n") == 3


# ── 2. 批注搬家 ──────────────────────────────────────────────────────────
def test_english_anchors_land_on_the_same_words(seeded):
    """英文锚点要落在**同一批字**上 —— 用独立字符串搜索核对（不照抄实现的换算）。"""
    notesfix.rebuild_paper_notes(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    hl, nt = _marks(seeded)
    hl4 = [r for r in hl.values() if r["block_id"] == "b-0004"]
    nt4 = [r for r in nt.values() if r["block_id"] == "b-0004"]
    assert len(hl4) == 1 and len(nt4) == 2                 # 中文那两条已按政策处置
    for r in (hl4[0], [n for n in nt4 if n["start"] is not None][0]):
        assert NEW_EN[int(r["start"]):int(r["end"])] == "optical"
    # 正文上的锚点一个都没动
    grain = "Grain size decreases with energy density.".index("Grain")
    assert [r for r in hl.values() if r["block_id"] == "b-0002"][0]["start"] == grain
    assert [r for r in nt.values() if r["block_id"] == "b-0002"][0]["end"] == grain + 5


def test_chinese_anchors_follow_the_translation_policy(seeded):
    """中文侧是**重新翻译**（新旧译文没有字级对应）⇒ 划痕删、笔记转「文献级」但**留住内容**。"""
    notesfix.rebuild_paper_notes(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    hl, nt = _marks(seeded)
    # 中文划痕：删（它只有坐标，文字已经不存在了）；指向它的笔记 `hl_id` 置 NULL
    assert not [r for r in hl.values() if r["lang"] == "zh"]
    zh_note = [r for r in nt.values() if r["content"] == "中文区间笔记"][0]
    assert zh_note["block_id"] is None and zh_note["lang"] is None
    assert zh_note["start"] is None and zh_note["end"] is None
    assert zh_note["quote"] == FakeTr.OLD_ZH[:5]       # 原本划住的那 5 个字还在笔记里
    # 整块锚（无字符区间）指的是"这一块"，跟着块走、不用改
    whole = [r for r in nt.values() if r["content"] == "整块笔记"][0]
    assert whole["block_id"] == "b-0004" and whole["start"] is None


def test_translation_failure_keeps_the_old_chinese_without_a_review_flag(seeded):
    """译不出来 ⇒ **保留旧译文**（与原文只差空白，读起来照样对），且**不**挂「待校对」。

    与翻译侧那条"结构要求不进待校对"同一取向（`translator._check(lines_must_match=False)`）：
    给一个内容基本正确的块刷黄标，只会让读者学会忽略黄标。
    """
    tr = FakeTr(fail=True)
    notesfix.rebuild_paper_notes(seeded, 1, translator=tr, log=lambda *a: None)
    row, payload = _note(seeded)
    assert row["en"] == NEW_EN and row["zh"] == FakeTr.OLD_ZH
    assert row["zh_source"] == "mt" and "needs_review" not in payload
    # 中文没变 ⇒ 中文锚点也不该被处置掉（它们仍然指着同一段中文）
    hl, nt = _marks(seeded)
    assert [r for r in hl.values() if r["lang"] == "zh"]
    assert [r for r in nt.values() if r["content"] == "中文区间笔记"][0]["lang"] == "zh"


def test_a_hand_revised_note_block_keeps_its_chinese(seeded):
    """**人工修订过的注区块不重译** —— 变的是空白与渲染戳，内容没变，人写的那段中文依然对。

    决策⑯（重跑不得冲掉人工修订）与 `refsfix` 的「`zh_source='human'` 不碰」在这里同样成立：
    重译一遍不但白花钱，还会把手写的东西换成机翻、并把「已人工修订」标记**降级**成 `mt`
    （㊽ 修过同型缺陷：跑了半天没变化，标记却没了）。
    """
    from papershelf.server.db import tx
    with tx(seeded):
        seeded.execute("UPDATE blocks SET zh_source='human' WHERE paper_id=1 AND id='b-0004'")
    tr = FakeTr()
    got = notesfix.rebuild_paper_notes(seeded, 1, translator=tr, log=lambda *a: None)
    assert got["changed"] and got["notes"] == 1
    assert tr.calls == []                                  # 一个人工修订块都不送模型
    row, payload = _note(seeded)
    assert row["en"] == NEW_EN and payload["note"] and payload["markers"]
    assert row["zh"] == FakeTr.OLD_ZH and row["zh_source"] == "human"   # 人写的一个字没动
    # 中文锚点也不动（中文没变 ⇒ 它们仍指着同一段字）
    hl, nt = _marks(seeded)
    assert [r for r in hl.values() if r["lang"] == "zh"]
    assert [r for r in nt.values() if r["content"] == "中文区间笔记"][0]["lang"] == "zh"


def test_only_the_machine_translated_notes_are_sent_to_the_model(seeded, monkeypatch):
    """混合场景（一篇里两种注区都有）⇒ 送出去的就**只有机器译的那几块**。

    这一条同时钉住两件事：① 没把整篇都送去翻（白花钱）；② 人工修订那块不在集合里。
    单块的人工修订用例证不了后者 —— 那时"全是人工修订"那条捷径会先把模型挡住。
    """
    from papershelf.pipeline.model import Block
    from papershelf.server.repo import load_doc, save_doc

    old2 = "Table 2 Chemical composition a Balance b By the supplier c Not measured"
    new2 = "Table 2 Chemical composition\na Balance\nb By the supplier\nc Not measured"
    doc = load_doc(seeded, 1)
    doc.blocks.append(Block(id="b-0005", type="p", en=old2, zh="表 2 化学成分",
                            zh_source="human", payload={"page": 22}))
    save_doc(seeded, 1, doc)
    monkeypatch.setattr(notesfix, "_fresh_blocks", lambda pdf: [
        _fresh_note(), _fresh_note("b-0998", en=new2, page=22)])

    tr = FakeTr()
    got = notesfix.rebuild_paper_notes(seeded, 1, translator=tr, log=lambda *a: None)
    assert got["notes"] == 2
    assert tr.calls == [{"b-0004"}]                    # 正文块与人工修订块都不在里面
    r5 = dict(seeded.execute("SELECT * FROM blocks WHERE id='b-0005'").fetchone())
    assert r5["en"] == new2                            # 结构照样修
    assert r5["zh"] == "表 2 化学成分" and r5["zh_source"] == "human"   # 中文一个字没动


def test_no_translate_only_fixes_the_structure(seeded):
    got = notesfix.rebuild_paper_notes(seeded, 1, translate=False, log=lambda *a: None)
    assert got["changed"] and got["tokens"] == 0
    row, payload = _note(seeded)
    assert row["en"] == NEW_EN and row["zh_source"] == "mt"
    assert payload["note"] and payload["markers"]


# ── 3. 幂等 / 认不出就不动 ───────────────────────────────────────────────
def test_second_run_is_a_noop(seeded):
    notesfix.rebuild_paper_notes(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    tr = FakeTr()
    got = notesfix.rebuild_paper_notes(seeded, 1, translator=tr, log=lambda *a: None)
    assert got["ok"] and not got["changed"] and got["notes"] == 0
    assert tr.calls == []                                   # 没落库、没调模型


def test_a_note_block_that_cannot_be_matched_is_left_alone(seeded, monkeypatch):
    """配不上（同页同文字找不到**唯一**对应块）⇒ 跳过。

    判据是"去掉空白后逐字相同"，所以"文字被改过的块"天然配不上 —— 这正是敢自动改写的
    前提：配不上就不动，而不是"差不多就改"。
    """
    other = _fresh_note("b-0999", en=NEW_EN.replace("optical", "electron"), page=21)
    monkeypatch.setattr(notesfix, "_fresh_blocks", lambda pdf: [other])
    tr = FakeTr()
    got = notesfix.rebuild_paper_notes(seeded, 1, translator=tr, log=lambda *a: None)
    assert got["ok"] and not got["changed"] and got["notes"] == 0
    row, _ = _note(seeded)
    assert row["en"] == OLD_EN and tr.calls == []


def test_a_note_block_on_a_page_we_do_not_have_is_left_alone(seeded, monkeypatch):
    """同页判据也要真：页码对不上就不匹配（两块长得一样、但在不同页 ⇒ 不许猜）。"""
    monkeypatch.setattr(notesfix, "_fresh_blocks",
                        lambda pdf: [_fresh_note("b-0999", page=99)])
    got = notesfix.rebuild_paper_notes(seeded, 1, translator=FakeTr(), log=lambda *a: None)
    assert not got["changed"]


def test_dry_run_writes_nothing_and_calls_no_model(seeded):
    tr = FakeTr()
    got = notesfix.rebuild_paper_notes(seeded, 1, translator=tr, dry_run=True,
                                       log=lambda *a: None)
    assert got["dry_run"] and got["notes"] == 1 and not got["changed"] and tr.calls == []
    row, _ = _note(seeded)
    assert row["en"] == OLD_EN


def test_missing_pdf_is_reported_not_guessed(settings, tmp_path):
    """没有 PDF 原件（落盘文件被删）⇒ 报出来，而不是"当成没事"或"清掉重来"。"""
    from papershelf.pipeline.model import Doc
    from papershelf.server.db import connect, tx
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    with tx(conn):
        conn.execute("INSERT INTO users (id,email,password_hash,status,created_at)"
                     " VALUES (1,'u@tsinghua.edu.cn','x','active','')")
        conn.execute("INSERT INTO plans (id,user_id,name) VALUES (1,1,'p')")
        conn.execute("INSERT INTO papers (id,plan_id,title,conv_state,pdf_path) "
                     "VALUES (1,1,'t','done',?)", (str(tmp_path / "gone.pdf"),))
    save_doc(conn, 1, Doc(meta={}, assets=[],
                          blocks=[Block(id="b-0001", type="p", en="x", zh="", zh_source="none")]))
    got = notesfix.rebuild_paper_notes(conn, 1, log=lambda *a: None)
    assert not got["ok"] and "PDF" in got["reason"]


def test_a_paper_without_a_document_is_reported(settings):
    from papershelf.server.db import connect, tx

    conn = connect(settings)
    with tx(conn):
        conn.execute("INSERT INTO users (id,email,password_hash,status,created_at)"
                     " VALUES (1,'u@tsinghua.edu.cn','x','active','')")
        conn.execute("INSERT INTO plans (id,user_id,name) VALUES (1,1,'p')")
        conn.execute("INSERT INTO papers (id,plan_id,title,conv_state) "
                     "VALUES (1,1,'t','queued')")
    got = notesfix.rebuild_paper_notes(conn, 1, log=lambda *a: None)
    assert not got["ok"] and "产物" in got["reason"]
