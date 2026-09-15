"""阅读顺序（分区 + 栏间空白）回归测试 —— 离线，不联网、不读真实 PDF。

起因（2026-09-14，宿主报「pdf 抽取原文不对」，生产 paper 12 = B7-01-AI-in-AM-review）：

    首页正文首句是「unattainable concepts in smart materials (SMs) [7] …」，
    而它其实是**右栏的第二段**的尾巴；左栏第一段「AM, also known as 3D printing…」
    被排到了它的后面。读起来就是"一句话没有开头"。

根因：旧实现把整页**二选一**地判成「单栏」或「双栏」（`_two_column`：左右各 ≥4 个窄块），
而首页是**混合版式** —— 通栏的标题/摘要 + 下方双栏正文。整页一刀切的启发式判成单栏
（实测 left=9 / right=3，不到阈值），于是退回"按 y 排序" → **两栏逐行交错**，
右栏段落在左栏段落之间来回穿插。

本文件把修好的行为钉住：
  1. 混合版式：通栏块各就各位，其下双栏**整栏读完再读下一栏**（不再逐行交错）；
  2. 纯双栏页：不得出现 L/R 交替（那是交错的直接指纹）；
  3. **公式溢出栏间**不得让分栏失败（B5-01 第 7 页：右栏自 x=300.9 起，
     左栏一道公式的 `≤ 0` 伸到 299.2，真空白只剩 1.7pt）；
  4. **页码卡在栏间**不得让分栏失败（B2-03 第 24 页的页码 "186" 正落在 gutter 里）；
  5. 单栏页保持按 y。
"""

from __future__ import annotations

import pytest

from papershelf.pipeline.parse import _reading_order


def blk(x0: float, y0: float, x1: float, y1: float, tag: str) -> dict:
    """最小块：`_reading_order` 只看 bbox，tag 仅用于断言可读性。"""
    return {"bbox": (x0, y0, x1, y1), "lines": [], "_tag": tag}


def order_tags(blocks: list[dict], width: float, height: float) -> list[str]:
    return [b["_tag"] for b in _reading_order(blocks, width, height)]


# ── 1. 混合版式（生产 paper 12 首页的真实 bbox）──────────────────────────────
# 版面（实测）：通栏页眉/标题/作者/摘要，下方左侧「1 Introduction」+ 首段 + 作者单位，
# 右侧两段正文（是左栏首段的**续写**）。正确顺序 = 左栏整栏 → 右栏整栏。
_W, _H = 595.276, 790.866


def _mixed_layout_page() -> list[dict]:
    return [
        blk(51, 33, 339, 53, "journal-header"),
        blk(57, 65, 137, 76, "critical-review"),
        blk(51, 105, 479, 142, "title"),
        blk(51, 157, 507, 182, "authors"),
        blk(51, 201, 326, 220, "received"),
        blk(51, 239, 547, 427, "abstract"),
        blk(51, 438, 457, 451, "keywords"),
        blk(51, 471, 131, 486, "h-introduction"),
        blk(307, 473, 547, 561, "right-1-continuation"),
        blk(51, 498, 292, 611, "left-body"),
        blk(307, 560, 547, 711, "right-2"),
        blk(51, 643, 116, 655, "author-name"),
        blk(65, 653, 157, 665, "author-email"),
        blk(51, 673, 271, 695, "affiliation-1"),
        blk(51, 698, 262, 720, "affiliation-2"),
        blk(504, 738, 545, 757, "page-marker"),
    ]


def test_mixed_layout_reads_each_column_through_before_the_next():
    tags = order_tags(_mixed_layout_page(), _W, _H)
    # 左栏首段必须先于右栏那段"续写"，否则就是本 bug（一句话没有开头）
    assert tags.index("left-body") < tags.index("right-1-continuation")
    # 通栏的标题区必须先于下方双栏正文
    assert tags.index("abstract") < tags.index("h-introduction")
    assert tags.index("keywords") < tags.index("h-introduction")
    # 右栏两段之间保持先后
    assert tags.index("right-1-continuation") < tags.index("right-2")


def test_mixed_layout_does_not_interleave_the_two_columns():
    """交错指纹：正文带里的 L/R 侧别不得来回跳。"""
    tags = order_tags(_mixed_layout_page(), _W, _H)
    body = [t for t in tags if t in {
        "h-introduction", "left-body", "author-name", "author-email",
        "affiliation-1", "affiliation-2", "right-1-continuation", "right-2",
    }]
    sides = ["L" if t != "right-1-continuation" and t != "right-2" else "R" for t in body]
    flips = sum(1 for a, b in zip(sides, sides[1:]) if a != b)
    assert flips == 1, f"两栏应当只切换一次（L…L → R…R），实际 {flips} 次：{tags}"


# ── 2. 纯双栏页 ────────────────────────────────────────────────────────────
def test_pure_two_column_page_is_column_major():
    blocks = [
        blk(50, 100, 290, 300, "L1"),
        blk(50, 320, 290, 520, "L2"),
        blk(310, 100, 550, 300, "R1"),
        blk(310, 320, 550, 520, "R2"),
    ]
    assert order_tags(blocks, 600, 800) == ["L1", "L2", "R1", "R2"]


# ── 3. 公式溢出栏间（B5-01 第 7 页的真实几何）──────────────────────────────
def test_overflowing_equation_does_not_defeat_column_detection():
    """左栏一道公式伸进栏间（右栏自 x=300.9 起，公式到 299.2，空白仅 1.7pt）。

    相邻块间隙扫描在这里必然失败（任何 ≥2pt 的阈值都扫不出），
    必须靠**覆盖度剖面**认出 gutter —— 这正是本用例守的东西。
    """
    blocks = [
        blk(37.9, 65, 289.0, 210, "L-prose"),
        blk(37.9, 250, 299.2, 262, "L-equation-overflow"),   # 伸进栏间
        blk(37.9, 300, 289.0, 470, "L-prose-2"),
        blk(300.9, 65, 552.0, 200, "R-prose"),
        blk(300.9, 240, 552.0, 460, "R-prose-2"),
        blk(300.9, 500, 552.0, 700, "R-prose-3"),
    ]
    assert order_tags(blocks, 594, 792) == [
        "L-prose", "L-equation-overflow", "L-prose-2", "R-prose", "R-prose-2", "R-prose-3"
    ]


# ── 4. 页码卡在栏间（B2-03 第 24 页的真实几何）─────────────────────────────
def test_page_number_in_the_gutter_does_not_defeat_column_detection():
    """页码正落在两栏之间的空白里（x=292.6..305.3）——它会把 gutter 打钉子。

    所以找 gutter 必须**只看正文带**（排除页眉/页脚/页码）。
    """
    blocks = [
        blk(41.6, 52, 288.6, 240, "L1"),
        blk(41.6, 260, 288.6, 450, "L2"),
        blk(292.6, 754.5, 305.3, 764.9, "page-number"),   # 卡在栏间 + 页边
        blk(310.6, 52, 557.5, 240, "R1"),
        blk(310.6, 260, 557.5, 450, "R2"),
    ]
    tags = order_tags(blocks, 595, 794)
    assert tags.index("L2") < tags.index("R1"), f"页码导致分栏失败、退回交错：{tags}"


# ── 5. 单栏页不动 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("blocks", [
    [blk(50, 100, 400, 160, "a"), blk(50, 200, 400, 260, "b"), blk(50, 300, 400, 360, "c")],
])
def test_single_column_page_stays_in_y_order(blocks: list[dict]):
    assert order_tags(blocks, 450, 800) == ["a", "b", "c"]


def test_wide_block_is_treated_as_spanning_not_as_a_column():
    """通栏块（宽度 > 页面 62%）不得被卷进分栏，否则它会被塞进左栏或右栏里。"""
    blocks = [
        blk(40, 60, 560, 120, "spanning-title"),
        blk(40, 200, 270, 400, "L"),
        blk(300, 200, 560, 400, "R"),
    ]
    assert order_tags(blocks, 600, 800) == ["spanning-title", "L", "R"]


# ── 6. 页眉/页脚不参与分栏（v4；B7-01 第 2 页真实几何）────────────────────────
# 该页页眉 `x 252.6–544.3` **只有 49% 页宽**（页码 "3116" 占了页眉左侧那一小段），
# 于是它既不算"窄"也不算"通栏"，被判为窄块留在分栏段里 → 正卡在 gutter 上 → 分栏失败。
# 它是**页眉**：贴页顶 + 横跨本页实测 gutter（≈297.8）。
def test_journal_header_spanning_the_gutter_does_not_defeat_column_detection():
    blocks = [
        blk(51.0, 32.9, 67.2, 42.5, "page-number"),
        blk(252.6, 32.9, 544.3, 42.5, "journal-header"),
        blk(51.0, 60.1, 291.6, 186.1, "L1"),
        blk(51.0, 185.1, 291.7, 711.1, "L2"),
        blk(306.1, 60.1, 546.8, 148.6, "R1"),
        blk(306.1, 147.6, 546.8, 711.1, "R2"),
        blk(51.0, 738.2, 91.9, 757.0, "footer"),
    ]
    tags = order_tags(blocks, 595.276, 790.866)
    assert tags.index("L2") < tags.index("R1"), f"页眉导致分栏失败、退回交错：{tags}"


# ── 7. 通栏图不得让 gutter 探测放弃（v4；B7-01 第 30 页真实几何）──────────────
# 页面上只有 11–12 个块：一张通栏图 + 一行通栏页眉就够让"横跨 gutter 的块数"= 2，
# 而 `_MAX_BRIDGE_RATIO`（6%）在块少时只允许 1 个 → 探测放弃 → 两栏交错。
# 修法 = 迭代剔除跨栏块（XY-cut 的做法）再重算剖面。
def test_full_width_figure_does_not_defeat_gutter_detection():
    blocks = [
        blk(51.0, 32.9, 67.2, 42.5, "page-number"),
        blk(252.6, 32.9, 544.3, 42.5, "journal-header"),
        blk(63.8, 58.0, 531.5, 412.0, "full-width-figure"),
        blk(51.0, 412.2, 399.6, 434.0, "caption-overflowing-gutter"),
        blk(51.0, 435.1, 291.6, 609.0, "L1"),
        blk(51.0, 610.1, 291.6, 737.0, "L2"),
        blk(306.1, 435.1, 546.8, 534.0, "R1"),
        blk(306.1, 535.1, 546.8, 736.0, "R2"),
        blk(51.0, 738.2, 91.9, 757.0, "footer"),
    ]
    tags = order_tags(blocks, 595.276, 790.866)
    assert tags.index("L2") < tags.index("R1"), f"通栏图导致分栏失败、退回交错：{tags}"


# ── 8. 一整栏文字被 PDF 抽成 1 个块时仍要分栏（v4；B7-01 第 8 页）───────────
# 图下的右栏整段被并成**一个**块（高 189pt），只按块数（`_MIN_COL_BLOCKS = 2`）
# 判定会得出"这不是两栏"→ 退回按 y 排 → 左栏读一段跳右栏再跳回。
# 修法：一侧块数少时，只要它**纵向占满这段**（`_coverage ≥ 0.5`）就算一整栏。
def test_single_block_column_still_splits():
    blocks = [
        blk(51.0, 32.9, 67.2, 42.5, "page-number"),
        blk(252.6, 32.9, 544.3, 42.5, "journal-header"),
        blk(63.8, 58.0, 531.5, 412.0, "full-width-figure"),
        blk(51.0, 496.9, 202.8, 518.0, "L-caption"),
        blk(51.0, 522.6, 291.6, 546.0, "L1"),
        blk(51.0, 547.6, 291.7, 658.0, "L2"),
        blk(51.0, 660.1, 291.6, 736.0, "L3"),
        blk(306.1, 522.6, 546.8, 711.0, "R-whole-column-as-one-block"),
    ]
    tags = order_tags(blocks, 595.276, 790.866)
    assert tags.index("L3") < tags.index("R-whole-column-as-one-block"), \
        f"右栏只 1 个块就放弃分栏、退回交错：{tags}"


# ── 9. gutter 不得落在左栏内部（v4；B7-01 第 24 页）─────────────────────────
# 该页左栏右边缘 291.7、页眉右边缘 544.3，`[208, 252]` 整段覆盖度都是最小值，
# 取**最左**同分点得 208.3 —— 那是左栏内部，按它切栏会把左栏劈开。
# 栏间空白必在页面中央，所以同分时取**最靠页中**的那个。
def test_gutter_prefers_the_centre_most_minimum():
    blocks = [
        blk(51.0, 32.9, 67.2, 42.5, "page-number"),
        blk(252.6, 32.9, 544.3, 42.5, "journal-header"),
        blk(51.0, 60.1, 291.7, 446.0, "L1"),
        blk(306.1, 60.1, 546.8, 146.0, "R1"),
        blk(306.1, 147.6, 546.8, 446.0, "R2"),
        blk(306.1, 447.6, 546.8, 736.0, "R3"),
        blk(51.0, 738.2, 91.9, 757.0, "footer"),
    ]
    tags = order_tags(blocks, 595.276, 790.866)
    assert tags == ["page-number", "journal-header", "L1", "R1", "R2", "R3", "footer"], \
        f"gutter 落进左栏内部（把左栏劈开）：{tags}"


# ── 10. 贴页边判定看"块的一条边贴住"，不是"整块都在带内"（v4 的静默失效）───
def test_edge_block_counts_even_if_it_extends_past_the_band():
    """期刊页眉高约 10pt 而从 y≈32 起 —— 上边距带高 39pt，于是 y1 = 41 > 39。

    第一版判据写成"整块都在带内"→ 全部返回 False →**摘页眉这个修复静默不生效**
    （页面顺序看起来毫无变化）。贴边是按"贴"这个动作定义的。
    """
    page_header = blk(51.0, 32.4, 340.3, 53.3, "header-body-exceeds-band")
    body = blk(51.0, 200.0, 291.0, 400.0, "body")
    tags = order_tags([body, page_header], 595.276, 790.866)
    assert tags == ["header-body-exceeds-band", "body"]
