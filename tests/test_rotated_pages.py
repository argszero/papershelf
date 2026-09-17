"""整页旋转 90° 的页面：**转正** + 表格行序 + 表注不再被静默丢弃（`PARSE_VERSION` = 12）

宿主 2026-09-17 报：「原 pdf 里，有一个竖向的表格，提取后格式错了」。
病灶不在表格识别，而在**坐标系**：那张表整页是**旋转着画**在竖版页面上的
（页面 `/Rotate` 仍是 0，所以 `page.rotation`、`page.rect` 都看不出异常 ——
只有 span 级的 `line["dir"]` 说真话：`(0,-1)`）。整条链路都假定文字水平，
于是表格的**列**（在页面坐标里是竖列）被当成行：

* 同一行里相邻的格子被**粘成一句**（`Single sensor Spectrum sensors SVM, DT, …`);
* Ref 列的值（`[ 92 ]`）因为 x 最小，排到了**表头之前**。

修法是**纯坐标变换**（`normalize_rotated_pages`：把这一页原样重画一遍、转正），
转正后它和一张普通横排表格**完全一样** —— 不需要给表格识别加任何特例。
本文件钉住四件事：

1. 判据（`_page_rotation`）认得出两个方向、且**不误判**正常页面；
2. 转正是**有条件的**：没有旋转页时**一个字节都不写**；
3. 转正后：行序正确、表注在、**该页不分栏**；
4. 顺带修掉的**表注被静默丢弃**（与旋转无关的独立缺陷，见 `pending_figure` 那段注释）——
   它在**没有**旋转页的 PDF 上也会发生，所以单独钉一条。
"""

from __future__ import annotations

import json

import pytest

_W, _H = 595.276, 790.866


def _fitz():
    return pytest.importorskip("fitz")


def _rect(*v):
    return _fitz().Rect(*v)


# ── 合成「整页旋转」的页面 ────────────────────────────────────────────────────
# 真件的几何：一张正常横排的表格画在**横版**页面上，再整页旋转 90° 贴进竖版页面。
# 这里用同一手法合成：先在横版页面上画好，再 `show_pdf_page(rotate=90)`。
_TABLE = [
    ["Sensor", "Sensor type", "ML", "Ref"],
    ["Single sensor", "Spectrum sensors", "SVM, DT, KNN, LDA", "[ 92 ]"],
    ["Single sensor", "CT", "DT, KNN, SVM, LDA, QDA", "[ 100 ]"],
    ["Multi-sensor", "Thermocouples, accelerometers", "RF, SVR, SVM, CART", "[ 98 ]"],
]


def _draw_table(pg, x0: float, y0: float, rows):
    """在页面上画一张**有横线**的表（横线是 `_table_regions` 判据的一半）。"""
    fitz = _fitz()
    col_w = [110.0, 200.0, 190.0, 50.0]
    row_h = 26.0
    for r in range(len(rows) + 1):
        y = y0 + r * row_h
        pg.draw_line(fitz.Point(x0, y), fitz.Point(x0 + sum(col_w), y), width=0.8)
    for r, row in enumerate(rows):
        x = x0
        for c, cell in enumerate(row):
            pg.insert_text((x + 4, y0 + r * row_h + row_h - 9), cell, fontsize=8)
            x += col_w[c]


def _rotated_table_pdf(path, *, caption: str = "Table 2  Research on single and multi-sensor"):
    """一页竖版页面，其内容是**整页旋转 90° 的横排表格**（复刻宿主报的那一页）。"""
    fitz = _fitz()
    # ① 先在横版页上画正着的表格
    lay = fitz.open()
    land = lay.new_page(width=_H, height=_W)
    land.insert_text((60, 40), "1068  The International Journal of Advanced Manufacturing Technology",
                     fontsize=9)
    if caption:
        land.insert_text((60, 70), caption, fontsize=9)
    _draw_table(land, 60.0, 90.0, _TABLE)
    # ② 整页旋转 90° 贴上竖版页面 —— 文字于是变成 `dir = (0,-1)`（自下而上）
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    pg.show_pdf_page(pg.rect, lay, 0, rotate=90)
    src.save(str(path))
    src.close()
    lay.close()
    return path


def _parse(path, **kw):
    from papershelf.pipeline.parse import parse_pdf
    return parse_pdf(path, assets_dir=path.parent / "assets", **kw)


# ── 1. 判据 ───────────────────────────────────────────────────────────────
def test_page_rotation_recognises_both_directions():
    """`insert_text(rotate=90)` 得到 `dir=(0,-1)`（真件就是这一种），`270` 得到 `(0,1)`。"""
    fitz = _fitz()
    from papershelf.pipeline.parse import _page_rotation

    for insert_rot, wanted in ((90, 270), (270, 90)):
        doc = fitz.open()
        pg = doc.new_page(width=_W, height=_H)
        # 字符数要过 `_ROT_MIN_CHARS`（200）—— 真件是整页表格，字符远多于此；
        # 再配一行水平页眉（真件也有），证明水平字不会把它盖成"正常页"
        for i in range(12):
            pg.insert_text((560, 60 + i * 60), "rotated table cell text here", fontsize=9,
                           rotate=insert_rot)
        pg.insert_text((60, 40), "1068 Journal header", fontsize=9)
        dirs = {ln["dir"] for b in pg.get_text("dict")["blocks"] if b.get("type") == 0
                for ln in b["lines"]}
        assert (0.0, -1.0 if insert_rot == 90 else 1.0) in dirs, dirs
        assert _page_rotation(pg) == wanted
        doc.close()


def test_page_rotation_says_zero_for_normal_pages():
    """一页正常的双栏正文（含一两行竖排标注）**不能**被判成旋转页 —— 转错的代价是整页躺下。"""
    fitz = _fitz()
    from papershelf.pipeline.parse import _page_rotation

    doc = fitz.open()
    pg = doc.new_page(width=_W, height=_H)
    pg.insert_textbox(_rect(51, 80, 290, 700), "Body text of the left column. " * 60, fontsize=10)
    pg.insert_textbox(_rect(310, 80, 549, 700), "Body text of the right column. " * 60, fontsize=10)
    pg.insert_text((560, 60), "figure", fontsize=8, rotate=90)      # 一行竖排标注
    assert _page_rotation(pg) == 0
    doc.close()


def test_normalisation_writes_nothing_when_no_page_is_rotated(tmp_path):
    """没有旋转页 ⇒ 不写副本、不改 doc 取源（`normalized.pdf` 一个字节都不该出现）。"""
    fitz = _fitz()
    pdf = tmp_path / "plain.pdf"
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    pg.insert_textbox(_rect(51, 80, 549, 700), "Ordinary body text. " * 60, fontsize=10)
    src.save(str(pdf))
    src.close()

    doc = _parse(pdf)
    assert not (tmp_path / "normalized.pdf").exists()
    assert "rotated_pages" not in doc.meta and "normalized_pdf" not in doc.meta


def test_normalised_copy_lives_in_the_paper_directory(tmp_path):
    """副本必须落在这篇文献**自己的目录**（= 图片资产目录的父目录），而不是全库共享的位置。

    `papers_dir/normalized.pdf` 是**共享名字**：并发（`MAX_CONCURRENCY=2`）时两篇互相覆盖
    ⇒ ①c 静默读到**别人的 PDF**；而且删文献（㉙）只收 `p<id>/` 与 PDF 本身，孤儿副本会一直留着。
    """
    (tmp_path / "p1").mkdir()
    pdf = _rotated_table_pdf(tmp_path / "p1" / "rot.pdf")
    doc = _parse(pdf)

    assert doc.meta["normalized_pdf"] == str(tmp_path / "p1" / "normalized.pdf")
    assert (tmp_path / "p1" / "normalized.pdf").exists()
    assert not (tmp_path / "normalized.pdf").exists(), "不该落在共享的 papers_dir 下"


# ── 2. 转正：行序、表注、不分栏 ───────────────────────────────────────────────
def test_rotated_page_is_transposed_and_keeps_row_order(tmp_path):
    """转正后：表头在前、每行的格子相邻、Ref 列**跟在它那一行之后**（旧行为是排到表头之前）。"""
    pdf = _rotated_table_pdf(tmp_path / "rot.pdf")
    doc = _parse(pdf)
    assert doc.meta["rotated_pages"] == [1]
    rows = [(b.payload.get("page"), b.en) for b in doc.blocks if b.type == "p"]
    assert all(p == 1 for p, _ in rows)
    texts = [t for _, t in rows]

    head = next(i for i, t in enumerate(texts) if t.startswith("Sensor Sensor type"))
    ref92 = next(i for i, t in enumerate(texts) if "[ 92 ]" in t)
    ref98 = next(i for i, t in enumerate(texts) if "[ 98 ]" in t)
    assert head < ref92 < ref98, texts        # 行序 = y 序，不再"列优先"
    # 表头与第 1 行之间只隔着第 1 行的格子（第 1 行那两块在第 2 行之前）
    assert all("DT, KNN, SVM" not in texts[i] for i in range(head, ref92)), texts


def test_rotated_page_keeps_its_caption(tmp_path):
    """表注必须留在产物里（转正后在页首）—— 它就是被 `pending_figure` 吞掉的那一条。"""
    pdf = _rotated_table_pdf(tmp_path / "rot.pdf")
    doc = _parse(pdf)

    caps = [b.en for b in doc.blocks if b.en.startswith("Table 2")]
    assert len(caps) == 1, [b.en for b in doc.blocks]
    # 表注在表体**之前**（转正后它是页首那块；旧行为里它根本不存在）
    texts = [b.en for b in doc.blocks]
    assert texts.index(caps[0]) < next(i for i, t in enumerate(texts)
                                       if t.startswith("Sensor Sensor type"))


def test_reading_order_columns_flag_means_pure_row_order():
    """`columns=False`（旋转页专用）= 退化成**纯 y 序**：整页是一张表，只有行序是对的。

    对比同一批块：`columns=True` 会把左栏整栏读完再读右栏 —— 那正是旋转页上发生的
    "格间空白被当成栏间空白"（实测第 5 页前 8 块全是右半张表的格子，左半张表排到后面）。
    """
    from papershelf.pipeline.parse import _reading_order

    W, H = 595.276, 790.866
    blocks = []
    for r in range(8):
        y = 100 + r * 30
        blocks.append({"type": 0, "text": f"L{r}", "bbox": [51.0, y, 280.0, y + 12]})
        blocks.append({"type": 0, "text": f"R{r}", "bbox": [310.0, y, 549.0, y + 12]})

    two_col = [b["text"] for b in _reading_order(blocks, W, H)]
    assert two_col == [f"L{r}" for r in range(8)] + [f"R{r}" for r in range(8)]
    by_y = [b["text"] for b in _reading_order(blocks, W, H, columns=False)]
    assert by_y == [f"{s}{r}" for r in range(8) for s in "LR"]      # 逐行交错 = 纯 y 序


def test_rotated_pages_are_the_only_ones_that_skip_column_detection(tmp_path, monkeypatch):
    """接线判据：旋转页那一次 `_reading_order` 必须带 `columns=False`，普通页照旧带默认值。

    直接量旋转换算出来的页面还不够（`_gutter` 在这类页面上未必真找到 gutter，
    那样"忘了传 columns=False"就会静默通过）—— 所以这里盯**参数本身**。
    """
    fitz = _fitz()
    from papershelf.pipeline import parse as P

    calls: list[bool] = []
    original = P._reading_order

    def spy(blocks, width, height, *, columns=True):
        calls.append(columns)
        return original(blocks, width, height, columns=columns)

    monkeypatch.setattr(P, "_reading_order", spy)

    # 第 1 页普通双栏正文、第 2 页整页旋转的表格
    pdf = tmp_path / "mixed.pdf"
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    pg.insert_textbox(_rect(51, 80, 290, 700), "Left column body text. " * 20, fontsize=10)
    pg.insert_textbox(_rect(310, 80, 549, 700), "Right column body text. " * 20, fontsize=10)
    lay = fitz.open()
    land = lay.new_page(width=_H, height=_W)
    land.insert_text((60, 40), "1068  The International Journal of Advanced Manufacturing Technology",
                     fontsize=9)
    land.insert_text((60, 70), "Table 2  Research on single and multi-sensor", fontsize=9)
    _draw_table(land, 60.0, 90.0, _TABLE)
    src.new_page(width=_W, height=_H).show_pdf_page(_rect(0, 0, _W, _H), lay, 0, rotate=90)
    src.save(str(pdf))
    src.close()
    lay.close()

    doc = _parse(pdf)
    assert doc.meta["rotated_pages"] == [2]
    assert calls == [True, False]


def test_normalised_copy_is_written_for_the_proofreader(tmp_path):
    """转正副本要落盘并写进 meta —— ①c 渲染页图 / 量疑似表区必须与解析产物**同一套坐标**。"""
    fitz = _fitz()
    pdf = _rotated_table_pdf(tmp_path / "rot.pdf")
    doc = _parse(pdf)
    norm = doc.meta["normalized_pdf"]
    assert norm.endswith("normalized.pdf") and (tmp_path / "normalized.pdf").exists()

    page = fitz.open(norm)[0]
    assert page.rect.width > page.rect.height                    # 竖版 → 横版
    dirs = {ln["dir"] for b in page.get_text("dict")["blocks"] if b.get("type") == 0
            for ln in b["lines"]}
    assert all(abs(d[1]) < 0.5 for d in dirs), dirs              # 文字全部水平了
    # 同一份副本，`_table_regions` 能认出表区（横线判据要能过 —— 见 `_h_rules` 的缝合）
    from papershelf.pipeline.proofread import _table_regions
    assert _table_regions(page, []), "转正后的表区提示为空 → agent 一点线索都拿不到"


def test_h_rules_stitch_per_cell_segments_into_one_line():
    """格边框常常**逐格画**：不缝合则每段都短于页宽的 25% ⇒ 表区判据恒为假（实测过）。"""
    fitz = _fitz()
    from papershelf.pipeline.proofread import _h_rules, _MIN_RULE_LEN

    doc = fitz.open()
    pg = doc.new_page(width=_H, height=_W)
    width = _W
    seg = width * _MIN_RULE_LEN / 2                              # 半条门槛：单段必被丢掉
    x = 60.0
    for _ in range(8):
        pg.draw_line(fitz.Point(x, 100.0), fitz.Point(x + seg, 100.0), width=0.8)
        x += seg
    rules = _h_rules(pg)
    assert len(rules) == 1 and rules[0][2] - rules[0][1] > seg * 6, rules


# ── 3. 表注被静默丢弃（与旋转无关的独立缺陷）──────────────────────────────────
def test_caption_is_not_swallowed_by_a_stale_pending_figure(tmp_path):
    """上一页的图（图注已由**几何配对**认领）不该把下一页的 `Table N …` 吃掉。

    病灶：`pending_figure` 是**跨页存活**的，而几何配对认领的图注**不走**那个分支
    ⇒ 变量从没被复位 ⇒ 下一页以 "Table N" 起头的表注撞进"给上一个图当图注"的判定，
    而那个图已有图注 → 什么都不写、直接 `continue`（**内容静默消失**）。
    实测语料：三份 PDF 的第 3 页 `Table 1 Commonly employed models …` 全都不在产物里。
    """
    fitz = _fitz()
    pdf = tmp_path / "fig-then-table.pdf"
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 300))
    pix.set_rect(pix.irect, (30, 90, 160))
    pg.insert_image(_rect(120, 200, 480, 470), pixmap=pix)
    pg.insert_textbox(_rect(120, 480, 480, 520), "Fig. 1 A workflow diagram of the proposed method.",
                      fontsize=9)
    pg2 = src.new_page(width=_W, height=_H)
    pg2.insert_textbox(_rect(51, 80, 549, 110),
                       "Table 1 Commonly employed models and algorithms in the fields of ML and DL",
                       fontsize=9)
    src.save(str(pdf))
    src.close()

    doc = _parse(pdf)
    texts = [b.en for b in doc.blocks]
    assert any(t.startswith("Table 1 Commonly employed models") for t in texts), texts
    # 图注仍然配到了那张图上（修的是"顺手吃掉后一行"，不是把配对改坏）
    fig = [b for b in doc.blocks if b.type == "figure"]
    assert len(fig) == 1 and fig[0].payload.get("caption", "").startswith("Fig. 1")


def test_caption_still_follows_its_own_figure(tmp_path):
    """紧跟在（**尚无**图注的）图后面的图注照旧归它 —— 修复不能把正常配对一并关掉。"""
    fitz = _fitz()
    pdf = tmp_path / "fig-figcaption.pdf"
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 300))
    pix.set_rect(pix.irect, (30, 90, 160))
    pg.insert_image(_rect(120, 200, 480, 470), pixmap=pix)
    # 没有几何配对可用（离图 200pt）→ 走 `pending_figure` 那条老路
    pg.insert_textbox(_rect(120, 690, 480, 720), "Fig. 3 A caption far below the image.",
                      fontsize=9)
    src.save(str(pdf))
    src.close()

    doc = _parse(pdf)
    fig = [b for b in doc.blocks if b.type == "figure"]
    assert fig and fig[0].payload.get("caption", "").startswith("Fig. 3")


# ── 4. ①c 侧的接线（必须读同一份转正副本）────────────────────────────────────
def test_converter_points_the_proofreader_at_the_normalised_copy(tmp_path, monkeypatch):
    """①c 必须把渲染源换成转正副本 —— 只读 meta 不换源等于没接线（坐标照样错位）。"""
    from pathlib import Path as _P

    from papershelf.server import converter

    text = _P(converter.__file__).read_text(encoding="utf-8")
    at = text.index('doc.meta.get("normalized_pdf")')
    wiring = text[at:at + 400]
    assert "norm.exists()" in wiring, wiring
    assert "proofread_pdf = norm" in wiring, wiring          # 换源，而不是只记一条日志
