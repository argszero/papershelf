"""表格块的**翻译与校验通路**（决策㊴）—— 离线回归，不调 LLM、不联网。

宿主 2026-09-16（选 A：双语对照）后，「表格单元格要不要中文」这件事把三处连起来了：

1. `validate.expects_chinese` 必须认得 `table`（否则要么整表免译、要么判漏译进死循环）；
2. `translator` 走**另一条通道**（送网格、要网格）—— 表格不能走正文那条 HTML 往返路，
   `<td>` 的边界在 `extract_blocks` 解回来时会当场塌掉；
3. 形状护栏：译文网格与英文网格必须**逐行同宽**，否则整份不用（错行的表比不译更难发现）。

本文件钉住这三条，以及"表格块在整篇校验里能被认出来、且不会因为标了 data-nt 而免检"。
"""

from __future__ import annotations

from papershelf.pipeline.markup import render_block
from papershelf.pipeline.model import Block, grid_shape
from papershelf.pipeline.translator import LLMConfig, Translator
from papershelf.pipeline.validate import expects_chinese, validate

CFG = LLMConfig(base_url="http://x", api_key="k", model="m")


def _table(rows=None, **payload) -> Block:
    rows = rows or [["ML category", "ML model"],
                    ["Supervised", "Naive Bayes (BN)"],
                    ["Unsupervised", "K-means"]]
    from papershelf.pipeline.model import table_text
    return Block(id="b-0003", type="table", en=table_text(rows),
                 payload={"rows": rows, "caption": payload.pop("caption", ""), **payload})


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
