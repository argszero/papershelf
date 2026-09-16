"""正文渲染 + 那把"尺子"（决策㉛）。

㉚ 的高亮单位是**句子**，㉛ 换成**任意字符区间**。渲染器因此多了一份职责：
既要把划痕包成 `<mark>`，又要吐出让前端能把 DOM 选区换算回**裸文本字符偏移**的锚点
（`<span class="o" data-o="N">`）—— 因为公式已经被渲染成 MathML，DOM 文本与裸文本
不再一一对应，没有尺子就算不出坐标。

本文件钉住三件事：
1. 尺子**只在可标注的视图**里出现（prompt / 校验 / 导出路径一个多余 span 都没有）；
2. 划痕落在**对的那几个字符**上，公式是**原子**（不许把 MathML 劈成两半）；
3. 切句规则（`split_en` / `split_zh` / `sentence_offsets`）**仍然是对的** ——
   它们从"高亮的锚点"降级成了"存量数据的换算器"（㉚ 的句锚点 → ㉛ 的字符区间），
   规则必须与当年写 sid 的那一版逐字相同，否则换算出的区间整体偏移。
"""

from __future__ import annotations

import re

from papershelf.pipeline.markup import (
    math_units,
    prose_html,
    render_block,
    sentence_offsets,
    split_en,
    split_zh,
)
from papershelf.pipeline.model import Block


# ── 切句：存量换算用（规则不许动）───────────────────────────────────────
def test_split_en_basic():
    assert split_en("One. Two! Three?") == ["One.", " Two!", " Three?"]


def test_split_en_keeps_decimals_together_but_splits_abbreviations():
    """小数里的点**不切**（后面不是空白），但 `Fig. 2` 这种缩写点**会切**。

    这是原型的既有口径（「终止标点 + 其后是空白」就是全部规则），切句如今只剩
    「把旧的句锚点换算成区间」一个用途 —— 换算必须与当年一致，所以规则照旧不许优化。
    有人想加缩写词典时，请先看到这里写着"这是原型的口径，动了会让存量划痕整体平移"。
    """
    assert split_en("We report 28.4 BLEU. See Fig. 2 for details.") == [
        "We report 28.4 BLEU.", " See Fig.", " 2 for details."]
    assert split_en("Smith et al. 2020") == ["Smith et al.", " 2020"]


def test_split_en_trailing_quotes_stay_with_the_sentence():
    assert split_en('He said "go." Then he left.') == ['He said "go."', " Then he left."]


def test_split_zh_basic():
    assert split_zh("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]


def test_split_zh_semicolon_and_ellipsis():
    assert split_zh("甲；乙……丙") == ["甲；", "乙……", "丙"]


def test_sentence_offsets_tile_the_text():
    """句子区间要**首尾相接、无空洞**（换算出的划痕才与当年那句完全重合）。"""
    en = "Safety is important. It is also hard."
    spans = sentence_offsets(en, "en")
    assert [en[a:b] for a, b in spans] == ["Safety is important.", " It is also hard."]
    assert spans[1][0] == spans[0][1]

    zh = "安全性很重要。它也难。"
    zspans = sentence_offsets(zh, "zh")
    assert [zh[a:b] for a, b in zspans] == ["安全性很重要。", "它也难。"]


# ── 公式单元：偏移计算里的原子 ──────────────────────────────────────────
def test_math_units_split_text_and_formulas():
    units = math_units(r"See \(x^2\) here.")
    assert units == [(0, 4, "t"), (4, 11, "m"), (11, 17, "t")]
    assert math_units("no math") == [(0, 7, "t")]
    assert math_units("") == []


# ── 尺子：只在可标注的视图里出现 ────────────────────────────────────────
def test_anchors_only_in_the_annotatable_view():
    """**prompt / 校验 / 导出路径不许出现锚点。**

    翻译 prompt 走的是 `typeset=False` 的渲染结果，导出与校验同理 ——
    往里面掺零宽 span，轻则让产物难 diff、重则让模型在译文里"抄"回这些标签。
    """
    b = Block(id="b-0007", type="p", en="One. Two.", zh="一。二。", zh_source="mt")
    for typeset in (False, True):
        plain = render_block(b, lang="en", marker=False, typeset=typeset)
        assert "<span" not in plain and "data-o" not in plain and "<mark" not in plain
        assert "One. Two." in plain
    # 阅读器那份（anchors=True）必须有
    view = render_block(b, lang="en", marker=False, typeset=True, anchors=True)
    assert 'class="o" data-o="0"' in view and 'data-o="9"' in view


def test_anchor_is_emitted_once_per_position():
    """接缝处**同一个位置只吐一个锚点**（相邻单元的 end == 下一个 unit 的 start）。

    重复锚点本身无害，但一篇 400 个公式的论文会白多出几十 KB，
    且"这里为什么有两个锚点"对读代码的人是纯噪音。
    """
    html = prose_html(r"See \(x^2\) here.", typeset=True, anchors=True)
    assert html.count('class="o"') == 4          # 0 / 4 / 11 / 17
    assert 'data-o="4"' in html and 'data-o="11"' in html
    assert html.count('data-o="4"') == 1


def test_marks_do_not_need_anchors_but_come_along_in_the_view():
    html = prose_html("abcdef", typeset=True,
                      marks=[{"id": 3, "lang": "en", "start": 1, "end": 4, "color": "green"}])
    assert ">bcd</mark>" in html and "a<mark" in html and "</mark>ef" in html
    assert 'data-o=' not in html                  # 没要尺子 → 不吐锚点


# ── 划痕落在对的字符上 ──────────────────────────────────────────────────
def test_mark_wraps_exactly_the_selected_characters():
    text = "Safety is important."
    html = prose_html(text, typeset=True, anchors=True,
                      marks=[{"id": 1, "lang": "en", "start": 0, "end": 6, "color": "amber"}])
    assert '<mark class="hl hl-amber" data-h="1" tabindex="0" role="mark">Safety</mark>' in html
    assert html.endswith(f'<span class="o" data-o="{len(text)}"></span>')


def test_unknown_color_falls_back_to_the_default_pen():
    """库里出现非四支笔的颜色（老数据/手改库）→ 退默认色，**绝不吐坏类名**。"""
    html = prose_html("abc", typeset=True,
                      marks=[{"id": 1, "lang": "en", "start": 0, "end": 2, "color": "<script>"}])
    assert "hl-amber" in html and "<script>" not in html


def test_math_unit_is_atomic():
    """公式是**原子**：只与它相交的划痕也整个包进去（宁可多包，不能把 MathML 劈开）。

    前端解析选区时会把端点**吸附到公式边界**，所以正常路径下不会产生"半个公式"的划痕；
    真出现了（比如手改库）也必须渲染成一个完整的划痕。
    """
    html = prose_html(r"See \(x^2\) here.", typeset=True, anchors=True,
                      marks=[{"id": 7, "lang": "en", "start": 5, "end": 8, "color": "blue"}])
    assert html.count("<mark") == 1 and html.count("</mark>") == 1
    assert html.index("<mark") < html.index("<math") < html.index("</mark>")
    assert 'data-h="7"' in html


def test_text_mark_stops_at_the_formula_boundary():
    """划痕按**单元**切开：文本部分裁到边界，公式部分由它自己整个包。

    `See \\(x^2\\) here.` 里公式占 `[4, 11)`。划 `[1, 6)` = 前半段文本 + 整个公式；
    划 `[1, 4)` = 只有文本，公式**一点不沾**（这正是前端"吸附到公式边界"的正常形态）。
    """
    text = r"See \(x^2\) here."
    both = prose_html(text, typeset=True, anchors=True,
                      marks=[{"id": 9, "lang": "en", "start": 1, "end": 6, "color": "pink"}])
    assert ">ee </mark>" in both                      # 文本被裁到 4 为止
    assert both.count("<mark") == 2                   # 公式自己也被包了一个
    assert both.index("<mark") < both.index("<math") < both.index("</mark>", both.index("<math"))

    text_only = prose_html(text, typeset=True, anchors=True,
                           marks=[{"id": 9, "lang": "en", "start": 1, "end": 4, "color": "pink"}])
    assert ">ee </mark>" in text_only                 # 到公式前一格为止（含那个空格）
    assert text_only.count("<mark") == 1              # 公式没被沾上
    assert "<mark" not in text_only[text_only.index("<math"):]


# ── 块级渲染：哪些块可划、哪些不可 ──────────────────────────────────────
def test_every_selectable_block_gets_anchors():
    """**凡是能选中的文字都要有尺子**（2026-09-16 宿主：「标题行，选中后没有笔记工具的弹出 mark-bar」）。

    原先这里钉的是"只有正文段可划"：标题与参考文献条目走 `_esc()`，既没有零宽锚点、
    也没有 `<mark>`，于是选中标题时 `marks.ts::selectionSegments` 拿不到坐标系，
    **浮条根本不出现**（用户看到的正是这个）。现在它们与正文段一样走 `prose()`。

    真正"不可划"的只剩**没有可选文字**的块：公式（MathML 排版产物）、图片。
    """
    head = Block(id="b-0001", type="h2", en="I. Introduction", zh="I. 引言", zh_source="mt")
    en = render_block(head, lang="en", marker=False, typeset=True, anchors=True)
    assert 'class="o"' in en and 'data-o="0"' in en and en.startswith('<h2 class="sec">')
    assert 'class="o"' in render_block(head, lang="zh", marker=False, typeset=True, anchors=True)

    refs = Block(id="b-0009", type="refs", en="[1] X. Yu, IEEE TAC, 2007.", zh="",
                 zh_source="none")
    assert "data-o" in render_block(refs, lang="en", marker=False, typeset=True, anchors=True)

    # 标题上的划痕也要真的渲染出来（否则"能划但看不见"，比不能划更糟）
    marked = render_block(head, lang="en", marker=False, typeset=True, anchors=True,
                          marks=[{"id": 4, "lang": "en", "start": 3, "end": 15, "color": "amber"}])
    assert 'data-h="4"' in marked and "<mark" in marked

    # ⚠️ 导出 / 校验 / prompt 路径（`anchors` 默认 False）**逐字不变**：
    # 标题仍是 `<h2 class="sec">…</h2>`，一个多余 span 都没有。
    assert render_block(head, lang="en", marker=False,
                        typeset=False) == '<h2 class="sec">I. Introduction</h2>'
    assert render_block(refs, lang="en", marker=False,
                        typeset=False) == '<p class="ref-item">[1] X. Yu, IEEE TAC, 2007.</p>'

    # 公式块没有可选文字 → 不进这套坐标系（它本来就没有"裸文本"可划）
    eq = Block(id="b-0011", type="eq", en="", zh="", zh_source="none",
               payload={"latex": "x^2", "number": "3"})
    assert "data-o" not in render_block(eq, lang="en", typeset=True, anchors=True)


def test_heading_mark_offsets_follow_the_bare_text():
    """标题的锚点必须**就是**裸文本的偏移 —— 前端靠它把 DOM 选区换算回坐标。

    这条容易在"标题有前导编号/空白"时错位：`prose_html` 是按裸文本切的，
    所以断言"每个锚点值都落在裸文本上、且最后一个等于长度"。
    """
    text = "3.1 Column spilling in two-column layouts"
    head = Block(id="b-0002", type="h3", en=text, zh="", zh_source="none")
    html = render_block(head, lang="en", marker=False, typeset=True, anchors=True)
    positions = [int(m) for m in re.findall(r'data-o="(\d+)"', html)]
    assert positions[0] == 0 and positions[-1] == len(text)
    assert all(0 <= p <= len(text) for p in positions)


def test_marks_are_filtered_by_language():
    """一块里同时有中英划痕时，**各渲染各的** —— 原文与译文没有字级对应。"""
    b = Block(id="b-0007", type="p", en="One. Two.", zh="一。二。", zh_source="mt")
    marks = [{"id": 1, "lang": "en", "start": 0, "end": 3, "color": "amber"},
             {"id": 2, "lang": "zh", "start": 0, "end": 2, "color": "green"}]
    en = render_block(b, lang="en", marker=False, typeset=True, marks=marks)
    zh = render_block(b, lang="zh", marker=False, typeset=True, marks=marks)
    assert 'data-h="1"' in en and 'data-h="2"' not in en
    assert 'data-h="2"' in zh and 'data-h="1"' not in zh


def test_empty_text_does_not_crash():
    b = Block(id="b-0012", type="p", en="", zh="", zh_source="none")
    assert render_block(b, lang="en", marker=False, typeset=True, anchors=True) == "<p></p>"
    assert prose_html("", typeset=True, anchors=True) == ""


def test_typeset_false_keeps_latex_source():
    """校验/prompt 走 `typeset=False`：公式必须留**源码**，否则比对与翻译都拿不到原式。"""
    b = Block(id="b-0013", type="p", en=r"See \(x^2\).", zh="", zh_source="none")
    assert r"\(x^2\)" in render_block(b, lang="en", marker=False, typeset=False)


# ── 表格（决策㊴）────────────────────────────────────────────────────────
def _table_block(**payload) -> Block:
    rows = payload.pop("rows", [["Layer Type", "Complexity"], ["Self-Attention", "O(n²·d)"]])
    return Block(id="b-0007", type="table", en="x",
                 payload={"rows": rows, **payload})


def test_table_renders_the_grid_per_language():
    """表格块的两栏来自**两份网格**：`payload.rows`（英）/ `rows_zh`（中）。

    `zh` 缺失时回落英文（与正文块「没有译文就回落原文」同一条规则）——
    宁可让读者看到原文表格，也绝不吐一张空表。
    """
    b = _table_block(rows_zh=[["层类型", "每层复杂度"], ["自注意力", "O(n²·d)"]],
                     caption="Table 1. Path lengths.", caption_zh="表1. 路径长度。")
    en = render_block(b, lang="en", typeset=True)
    zh = render_block(b, lang="zh", typeset=True)
    assert "<th>Layer Type</th>" in en and "<td>O(n²·d)</td>" in en
    assert "Table 1. Path lengths." in en
    assert "<th>层类型</th>" in zh and "<td>自注意力</td>" in zh
    assert "表1. 路径长度。" in zh and "Layer Type" not in zh


def test_table_falls_back_to_english_when_the_zh_grid_shape_differs():
    """形状不符就**整份回落**（绝不渲半张错行的表）—— 与 `model.table_cells` 同一判据。"""
    b = _table_block(rows_zh=[["层类型"]])              # 行数/列数都对不上
    zh = render_block(b, lang="zh", typeset=True)
    assert "Layer Type" in zh and "层类型" not in zh


def test_table_without_a_grid_renders_as_a_paragraph():
    """历史/异常数据（只有 `en`、没有网格）走段落分支 —— 不吐空表壳。"""
    b = Block(id="b-0008", type="table", en="orphan table text")
    assert render_block(b, lang="en", typeset=True) == '<p data-b="b-0008">orphan table text</p>'


def test_table_text_is_the_text_face_used_by_the_machines():
    """`en`/`zh` 是网格的**裸文本串**：校验器、切片、`read_blocks` 只认它。"""
    from papershelf.pipeline.model import table_text
    rows = [["a", "b"], ["c", ""]]
    assert table_text(rows) == "a | b\nc | "
    assert table_text(rows, "Table 1. x") == "Table 1. x\na | b\nc | "
