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
def test_only_prose_blocks_get_anchors():
    """可划区域只有正文段（含摘要）。标题/参考文献不吐锚点 —— 它们不是可划区域，
    前端也就不会在里面生成划痕坐标（`refs` 尤其：一条文献切句/划半句都没有意义）。"""
    head = Block(id="b-0001", type="h2", en="I. Introduction", zh="I. 引言", zh_source="mt")
    assert "data-o" not in render_block(head, lang="en", typeset=True, anchors=True)
    refs = Block(id="b-0009", type="refs", en="[1] X. Yu, IEEE TAC, 2007.", zh="",
                 zh_source="none")
    assert "data-o" not in render_block(refs, lang="en", typeset=True, anchors=True)

    abstract = Block(id="b-0002", type="abstract", en="Abstract body here.", zh="摘要正文。",
                     zh_source="mt")
    assert 'class="abstract"' in render_block(abstract, lang="zh", typeset=True, anchors=True)
    assert "data-o" in render_block(abstract, lang="zh", typeset=True, anchors=True)


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
