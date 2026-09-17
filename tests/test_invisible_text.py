"""页边带里的**白色（不可见）文字**不入产物 —— 离线回归。

起因（2026-09-17 宿主实测截图）：「图标+Springer，提取成了 `Vol.:(0123456789)`」。

真因：Springer 版式在首页右下角留住一行 `Vol.:(0123456789)`，是**纯白**画的
（实测 `color=(1,1,1)`，8pt，y≈738–747 / 页高 791），纸面上看不见；
那处真正可见的是矢量画的 Springer 图标（我们本来就不渲矢量图）。`get_text()` 照抽。

判据故意很窄（宁可漏删，不许误删）：**只动页眉/页脚带里的白字**。
深色底上的白字（章节横幅、图内标注）是真内容，正文区一律不动。
"""

from __future__ import annotations

import pytest

fitz = pytest.importorskip("fitz")

from papershelf.pipeline.parse import parse_pdf  # noqa: E402

BODY = ["Body sentence number %d with enough words to look like prose." % i for i in range(3)]


def _pdf(path, items) -> None:
    """`items` = `[(x, y, text, color), …]`，color 默认黑。"""
    doc = fitz.open()
    page = doc.new_page()
    for x, y, text, *rest in items:
        color = rest[0] if rest else (0, 0, 0)
        page.insert_text((x, y), text, fontname="helv", fontsize=10, color=color)
    doc.save(str(path))
    doc.close()


def _texts(doc) -> str:
    return "\n".join(b.en or "" for b in doc.blocks)


def test_white_footer_text_is_dropped(tmp_path):
    """页脚带里的白字（Springer 的 `Vol.:(0123456789)` 就是这一种）→ 丢掉。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [(72, 100, t) for t, _ in zip(BODY, range(3))]
         + [(72, 800, "Vol.:(0123456789)", (1, 1, 1))])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    assert "Vol.:(0123456789)" not in _texts(doc), [b.en for b in doc.blocks]
    assert "Body sentence number 0" in _texts(doc)          # 正文没被顺手删掉


def test_black_footer_text_is_kept(tmp_path):
    """页脚里的**黑**字（页码等）照旧保留 —— 这条规则只管白字。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [(72, 100, BODY[0]), (72, 800, "1052")])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    assert "1052" in _texts(doc)


def test_white_body_text_is_kept(tmp_path):
    """正文区（深色底上的白字）是真内容 —— 不许删。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [(72, 300, "RESULTS AND DISCUSSION", (1, 1, 1)),
               (72, 320, BODY[1])])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    assert "RESULTS AND DISCUSSION" in _texts(doc)


def test_mixed_footer_block_keeps_the_black_part(tmp_path):
    """同一块里黑白混排 → 只扔白字，黑字留下（块不整块消失）。"""
    pdf = tmp_path / "t.pdf"
    _pdf(pdf, [(72, 100, BODY[0]),
               (72, 800, "Vol.:(0123456789)", (1, 1, 1)),
               (72, 800, "Submitted 2024", (0, 0, 0))])
    doc = parse_pdf(pdf, assets_dir=tmp_path / "a")
    text = _texts(doc)
    assert "Vol.:(0123456789)" not in text
    assert "Submitted 2024" in text
