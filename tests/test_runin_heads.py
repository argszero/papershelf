"""「加粗小标题 + 正文」被并成一块 → 行级拆分（`_split_runin_heads`）回归 —— 离线。

起因（2026-09-15，宿主实测）：「"abstract" 还是没有识别为标题」。

真因是 **PyMuPDF 的块聚合**：页面上 `Abstract` 是**独占一行的加粗小标题**，但它被和紧随其后的
16 行摘要正文并成了**一个块**；`_heading_level` 的两道闸门（`len(text) > 160`、
`_RE_H2_NAMED` 的 `len(text) < 60`）于是把它判成普通段落 —— **`Abstract` 在产物里根本不存在**。
`Keywords` 更隐蔽：Semibold 的 `Keywords` 与关键词列表在**同一行**。

本文件用**合成 PDF**（PyMuPDF 现场生成，形状与真实 PDF 一致：加粗行 + 正文行落在同一个块里）
钉住三件事：
  1. 具名小标题（`Abstract` / `Keywords`）被拆出来且判成 h2；
  2. **不误拆**：加粗的句中短语、作者行（`T. Herzog 1,2` —— 实测语料里被 `_RE_H3` 误伤）都不拆；
  3. **不丢字**：拆分前后全文文本逐字一致（拆分只搬位置，不改内容）。
"""

from __future__ import annotations

import re

import pytest

fitz = pytest.importorskip("fitz")

from papershelf.pipeline.parse import parse_pdf  # noqa: E402


def _flat(text: str) -> str:
    return re.sub(r"[\s\u00a0]+", "", text or "")


def _pdf(path, pages) -> None:
    """`pages` = 每页的 `[(x, y, text, fontname), …]`（fontname: hebo=粗体 / helv=常规）。"""
    doc = fitz.open()
    for items in pages:
        page = doc.new_page()
        for x, y, text, font in items:
            page.insert_text((x, y), text, fontname=font, fontsize=10)
    doc.save(str(path))
    doc.close()


def _blocks(doc):
    return [(b.type, b.level, b.en) for b in doc.blocks]


def _find(doc, prefix: str):
    return next((b for b in doc.blocks if (b.en or "").startswith(prefix)), None)


BODY = ["This is the abstract body line number %d with enough words to look like prose." % i
        for i in range(4)]


# ── ① 真缺陷：Abstract / Keywords ───────────────────────────────────────────

def test_bold_abstract_line_is_split_out_as_heading(tmp_path):
    """加粗 `Abstract` 与正文被 PyMuPDF 并成一块 → 必须拆出标题（就是宿上报的那一幕）。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [[(72, 100, "Abstract", "hebo")]
               + [(72, 114 + i * 12, t, "helv") for i, t in enumerate(BODY)]])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")

    head = _find(doc, "Abstract")
    assert head is not None, f"Abstract 标题没进产物：{_blocks(doc)}"
    assert (head.type, head.level) == ("h2", 2)
    assert head.en.strip() == "Abstract"                       # 标题块里只有标题
    body = _find(doc, "This is the abstract body")
    assert body is not None and body.type == "p"               # 正文另成一段


def test_keywords_on_the_same_line_is_split_out(tmp_path):
    """`Keywords`（Semibold）与关键词列表**同一行** → 也要拆开。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [[(72, 100, "Keywords", "hebo"),
                (72 + 52, 100, " additive manufacturing, machine learning, deep learning", "helv")]
               + [(72, 130 + i * 12, t, "helv") for i, t in enumerate(BODY)]])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")

    head = _find(doc, "Keywords")
    assert head is not None and head.type == "h2"
    assert head.en.strip() == "Keywords"
    kw = _find(doc, "additive manufacturing")
    assert kw is not None and kw.type == "p" and "machine learning" in kw.en


def test_abstract_without_bold_is_still_split(tmp_path):
    """有些排版不给小标题加粗 —— 靠「整行只有这一个词」判定，不靠字体。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [[(72, 100, "Abstract", "helv")]
               + [(72, 114 + i * 12, t, "helv") for i, t in enumerate(BODY)]])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    head = _find(doc, "Abstract")
    assert head is not None and head.type == "h2" and head.en.strip() == "Abstract"


# ── ② 不许误拆（拆错会把一句话劈成两块、破坏译文对齐）───────────────────────

def test_plain_bold_sentence_is_not_split(tmp_path):
    """首行整行加粗、但不是具名标题、字号也没变大 → 不拆（拆出来只会是碎的 `p`）。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [[(72, 100, "Important note about the results", "hebo")]
               + [(72, 114 + i * 12, t, "helv") for i, t in enumerate(BODY)]])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    texts = [b[2] for b in _blocks(doc)]
    assert not any(t.strip() == "Important note about the results" for t in texts)
    assert any(t.startswith("Important note about the results") for t in texts)


def test_author_line_is_not_split(tmp_path):
    """实测语料 B2-02 的反例：加粗的 `T. Herzog 1,2` 撞上 `_RE_H3`（单大写字母+句点）。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [[(72, 100, "T. Herzog 1,2", "hebo"),
                (72 + 70, 100, " · M. Brandt 1 · A. Trinchi 2 · A. Sola 2", "helv")]])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    assert len(doc.blocks) == 1
    assert doc.blocks[0].type == "p"
    assert doc.blocks[0].en.startswith("T. Herzog 1,2 · M. Brandt")


def test_lone_heading_without_body_is_untouched(tmp_path):
    """块里只有 `Abstract` 一行（没有正文）→ 本来就会被判成标题，不该再拆。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [[(72, 100, "Abstract", "hebo")]])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    assert [(b.type, b.en) for b in doc.blocks] == [("h2", "Abstract")]


# ── ③ 不丢字（拆分只搬位置，不改内容）───────────────────────────────────────

def test_split_loses_no_text(tmp_path):
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [[(72, 100, "Abstract", "hebo")]
               + [(72, 114 + i * 12, t, "helv") for i, t in enumerate(BODY)]
               + [(72, 200, "1 Introduction", "hebo")]
               + [(72, 214 + i * 12, "Introduction body sentence %d." % i, "helv") for i in range(3)]])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    src = fitz.open(str(pdf))
    want = _flat(src[0].get_text())
    src.close()
    got = _flat("".join(b.en for b in doc.blocks))
    assert got == want, f"拆分后文本对不上\nwant={want}\ngot ={got}"
