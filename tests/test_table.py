"""表格块的**翻译与校验通路**（决策㊴）—— 离线回归，不调 LLM、不联网。

宿主 2026-09-16（选 A：双语对照）后，「表格单元格要不要中文」这件事把三处连起来了：

1. `validate.expects_chinese` 必须认得 `table`（否则要么整表免译、要么判漏译进死循环）；
2. `translator` 走**另一条通道**（送网格、要网格）—— 表格不能走正文那条 HTML 往返路，
   `<td>` 的边界在 `extract_blocks` 解回来时会当场塌掉；
3. 形状护栏：译文网格与英文网格必须**逐行同宽**，否则整份不用（错行的表比不译更难发现）。

本文件钉住这三条，以及"表格块在整篇校验里能被认出来、且不会因为标了 data-nt 而免检"。
"""

from __future__ import annotations

import re

from papershelf.pipeline.markup import render_block
from papershelf.pipeline.model import (Block, grid_shape, table_layout, table_text,
                                        table_zh_usable)
from papershelf.pipeline.translator import LLMConfig, Translator
from papershelf.pipeline.validate import expects_chinese, validate

CFG = LLMConfig(base_url="http://x", api_key="k", model="m")


def _table(rows=None, **payload) -> Block:
    rows = rows or [["ML category", "ML model"],
                    ["Supervised", "Naive Bayes (BN)"],
                    ["Unsupervised", "K-means"]]
    caption = payload.pop("caption", "")
    # ⚠️ 裸文本必须**带上表注**（表注是裸文本的第一行）：`proofread.set_table` 就是
    # `en=table_text(rows, caption)`。旧夹具漏传 caption，于是"带表注的表"这个形状
    # 从来没被测过 —— 而那正是生产里每一张表的样子（`table_layout` 的偏移全靠它）。
    return Block(id="b-0003", type="table", en=table_text(rows, caption),
                 payload={"rows": rows, "caption": caption, **payload})


class _Fake(Translator):
    """把 LLM 调用换成"按剧本回一段字符串"的替身（工具与护栏都是真的）。"""

    def __init__(self, replies, **kw):
        super().__init__(CFG, **kw)
        self.replies = list(replies)
        self.prompts: list[str] = []

    def _chat(self, user: str, system: str | None = None) -> str:
        self.prompts.append(user)
        return self.replies.pop(0) if self.replies else "{}"


# ── 1. 「该不该有中文」─────────────────────────────────────────────────────
def test_expects_chinese_for_tables_follows_prose_not_shape():
    """有实词的表要中文；纯数字/符号的表**免译**（往格子里塞中文只会更糟）。"""
    assert expects_chinese(_table().en, block_type="table")
    numeric = Block(id="b-0004", type="table",
                    en="12.4 | 3.1\n0.98 | 0.87", payload={})
    assert not expects_chinese(numeric.en, block_type="table")


# ── 2. 表格走自己的通道 ────────────────────────────────────────────────────
def test_tables_are_translated_as_grids_not_html():
    b = _table()
    tr = _Fake(['{"caption_zh": "表1. 分类器对比。", "rows_zh": '
                '[["机器学习类别", "机器学习模型"], ["监督学习", "朴素贝叶斯（BN）"],'
                ' ["无监督学习", "K 均值"]]}'])
    out = tr.translate_blocks([b])
    assert b.payload["rows_zh"][0] == ["机器学习类别", "机器学习模型"]
    assert b.payload["caption_zh"] == "表1. 分类器对比。"
    assert b.zh.startswith("表1. 分类器对比。")
    assert out[0] == b.zh                                   # 返回值与 ordered 对齐
    assert "<td>" not in tr.prompts[0]                       # 送的是**网格**，不是 HTML


def test_bad_shape_is_rejected_then_retried_then_marked_for_review():
    """形状不符 → 重试一次；仍不符 → 不写中文、标「待校对」，**绝不渲一张错行的表**。"""
    b = _table()
    tr = _Fake(['```json\n{"rows_zh": [["机器学习类别"]]}\n```',     # 行数列数都不对
                '{"rows_zh": [["机器学习类别", "机器学习模型"]]}'])   # 仍不对
    tr.translate_blocks([b])
    assert "rows_zh" not in b.payload
    assert b.id in tr.needs_review and b.payload.get("needs_review") is True


def test_empty_trailing_cells_are_padded_not_rejected():
    """模型把**空尾格**整行省掉是排版习惯，不是理解错误：补空串，不整份作废。"""
    b = _table(rows=[["Method", "F1", "Notes"], ["SVM", "0.91", ""]])
    tr = _Fake(['{"rows_zh": [["方法", "F1", "备注"], ["支持向量机", "0.91"]]}'])
    tr.translate_blocks([b])
    assert b.payload["rows_zh"][1] == ["支持向量机", "0.91", ""]
    assert grid_shape(b.payload["rows_zh"]) == grid_shape(b.payload["rows"])


def test_tables_share_the_convergence_target_with_plain_blocks():
    """正文与表格的「不合格」判定两处各留一份 → 界面上的「待校对」数目对不上。"""
    b = _table()
    tr = _Fake(['{"rows_zh": []}'])
    tr.translate_blocks([b])
    assert tr.needs_review == ["b-0003"]


# ── 3. 整篇校验：表格不能被当成"多余的块"或"漏译"───── ────────────────────
def test_validate_accepts_a_translated_table_and_flags_an_untranslated_one():
    b = _table()
    en = render_block(b, lang="en", typeset=False, marker=True)
    zh_missing = render_block(b, lang="zh", typeset=False, marker=True)
    report = validate(en, zh_missing)
    # 「漏译」是**警告级**（`enforce_report` 只把结构性问题判死），所以这里看的是
    # 「有没有判成漏译」而不是 `ok`（`ok` 含 untranslated，必然为 False）。
    assert b.id in report.untranslated
    assert not (report.missing or report.extra or report.duplicated
                or report.out_of_order or report.tag_imbalance)

    b.payload["rows_zh"] = [["机器学习类别", "机器学习模型"],
                            ["监督学习", "朴素贝叶斯（BN）"],
                            ["无监督学习", "K 均值"]]
    zh = render_block(b, lang="zh", typeset=False, marker=True)
    report2 = validate(en, zh)
    assert report2.ok and not report2.untranslated
    assert not report2.missing and not report2.extra


# ── 3. 表格的**划痕坐标系**（决策㊵）──────────────────────────────────────
# 宿主 2026-09-16：「表格也需要支持选中后出mark-bar」。选中的是 DOM 里的字，
# 要落成 `(block_id, lang, start, end)`，就得有一把尺子 —— 而表格的裸文本是
# "表注一行 + 每行以 ` | ` 相连"（`model.table_text`），**格子只是其中的片段**。
# 于是：锚点要吐整块偏移，且每格起点必须与裸文本逐字对得上（对不上就是划痕整体位移）。

def _gridded() -> Block:
    rows = [["Layer Type", "Complexity per Layer"],
            ["Self-Attention", "O(n²·d)"],
            ["Recurrent", "O(n·d²)"]]
    cap = "Table 1. Maximum path lengths."
    b = _table(rows, caption=cap)
    b.payload["caption_zh"] = "表 1. 最大路径长度。"
    b.payload["rows_zh"] = [["层类型", "每层复杂度"],
                            ["自注意力", "O(n²·d)"],
                            ["循环", "O(n·d²)"]]
    b.zh = table_text(b.payload["rows_zh"], b.payload["caption_zh"])
    return b


def test_cell_offsets_point_at_the_cells_in_the_bare_text():
    """`table_layout` 的每格区间，在它自己吐的裸文本里必须**逐字**就是那一格。

    这条是整套坐标系的地基：锚点值是它、`<mark>` 的裁切也是它。
    自己再拼一遍文本/再数一遍分隔符（曾经想省这一步）就会漂开几个字符 ——
    表现是"划痕能划但位置不对"，很难发现。
    """
    b = _gridded()
    text, offs = table_layout(b.payload["rows"], b.payload["caption"])
    assert text == b.en                    # 与块裸文本同一份拼接
    for i, row in enumerate(b.payload["rows"]):
        for j, cell in enumerate(row):
            a, e = offs[i][j]
            assert text[a:e] == cell, (i, j, text[a:e], cell)
    # 表注占第一行，第一格紧接其后
    assert text[:len(b.payload["caption"])] == b.payload["caption"]
    assert offs[0][0][0] == len(b.payload["caption"]) + 1


def test_every_table_cell_carries_the_ruler_and_its_marks():
    """每一格都要有零宽锚点（否则选中它前端量不出坐标 = **浮条不出现**，㊳ 同型）；
    划痕按**整块坐标**进来，落到它相交的那一格上（跨格 = 两格各有一段）。"""
    b = _gridded()
    text, offs = table_layout(b.payload["rows"], b.payload["caption"])
    a0 = offs[0][0][0]                     # 第一格 "Layer Type" 的起点
    html = render_block(b, lang="en", typeset=True, anchors=True,
                        marks=[{"id": 7, "lang": "en", "start": a0 + 2, "end": a0 + 6, "color": "blue"}])
    assert 'data-o="0"' in html and f'data-o="{len(text)}"' in html
    # 每个格子的起点都是一个锚点值 → 尺子连得上（数一数够不够）
    anchors = {int(x) for x in re.findall(r'data-o="(\d+)"', html)}
    assert all(s in anchors for row in offs for s, _ in row)
    # 划痕只包住被选中的那 4 个字：裁切按**整块**坐标做（局部坐标会整体偏移）
    assert f">{text[a0 + 2:a0 + 6]}</mark>" in html

    # 跨格的一道划痕：**每个相交的格子各出现一段** `<mark>`（与正文"跨段选区拆开"同一取向：
    # 块间没有共同坐标系，格子间也没有 —— 界面上看不出区别，同色连成一片）
    tail = len(text)
    span = render_block(b, lang="en", typeset=True, anchors=True,
                        marks=[{"id": 8, "lang": "en", "start": a0, "end": tail, "color": "amber"}])
    crossed = sum(1 for row in offs for s, e in row if s < tail and e > a0)
    assert span.count('data-h="8"') == crossed > 2


def test_anchors_are_off_for_the_machine_facing_renders():
    """`anchors=False` + 无划痕时，表格产物与"从前逐字节相同"（导出/校验/prompt 路径）。

    这是敢动渲染的底气：`prose_html` 的常态分支就是 `_mathy()`。
    """
    b = _gridded()
    got = render_block(b, lang="en", typeset=False)
    assert "data-o" not in got and "<mark" not in got
    assert got.startswith('<table class="datatable" data-b="b-0003">')
    assert "<caption>Table 1. Maximum path lengths.</caption>" in got


def test_zh_table_is_usable_only_when_the_grid_is_really_chinese():
    """对照模式渲不渲**右边那张表**的判据（`table_zh_usable`）：

    - 形状不符（`rows_zh` 与 `rows` 不同宽）→ 不可用（宁可只给英文表，也不给一张错行的表）；
    - 逐格照抄英文（模型把输入原样回一遍）→ 不可用（否则右边是一张与左边**一模一样**的表）；
    - 形状一致且真的有中文字 → 可用。
    """
    b = _gridded()
    assert table_zh_usable(b)
    b.payload["rows_zh"] = [["层类型"], ["自注意力"], ["循环"]]        # 形状不符
    assert not table_zh_usable(b)
    b.payload["rows_zh"] = [list(r) for r in b.payload["rows"]]          # 照抄英文
    assert not table_zh_usable(b)
    b.payload["rows_zh"] = [["层类型", "每层复杂度"], ["自注意力", "O(n²·d)"], ["循环", "O(n·d²)"]]
    b.zh = ""                                                           # 没译文
    assert not table_zh_usable(b)
