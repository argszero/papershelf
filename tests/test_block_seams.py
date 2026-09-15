"""「一句话被拆到两个块里」的**标记**与**校对入口** —— 离线，不联网、不调 LLM。

宿主 2026-09-15 实测报（paper 1 第 22 页）：

    b-0238 (左栏末) … and RL; however, each of        ← 没有句末标点
    b-0239 (右栏首) the above sections does not talk… ← 小写起（真正的续写）

同一句话被栏间的切缝劈成两个块，中文译文于是也断成两半。宿主要求①c 校对 agent
把「**分块**分得对不对」也纳入校对，并选定**方案 B**：解析阶段只**标记**这条缝
（`payload["seam"] = "col-spill"`），**不自动合并** —— "该不该并"要看页图，
是 agent 的 `merge_block` 的活。

本文件钉住三件事：
1. **标记的判据**（`_column_spill_seams`）：几何（左栏→右栏的接缝）**加**文字
   （前块无句末标点 + 后块小写起）两条同时成立才标。只用文字规则实测 37 页报 73 处
   （大半是表格行与页眉），只用几何规则会把"正常换栏"一起标上。
2. **标记只标记**：`parse_pdf` 不得顺手把两个块并掉（那会让宿主少了"看图判断"这一步，
   也绕开了 `PARSE_VERSION` 之外的所有可追溯性）。
3. **agent 看得到**：`read_blocks`（`flags` + `seam`）、`check_artifacts`（`seams`）、
   页级 `suspect_pages` 计数、系统提示词 —— 四处都要有，缺一处 agent 就会漏掉这条缝。
"""

from __future__ import annotations

import json

import pytest

from papershelf.pipeline.model import Block, Doc, make_block_id
from papershelf.pipeline.parse import (_column_spill_seams, _page_gutter, _reading_order,
                                       _MARGIN_BAND)
from papershelf.pipeline.proofread import (SYSTEM_PROMPT, ProofreadTools, _block_tags,
                                           _suspect_pages)
from papershelf.pipeline.translator import LLMConfig

CFG = LLMConfig(base_url="http://x", api_key="k", model="m")

_W, _H = 595.276, 790.866
_GUTTER = 297.8


def blk(x0: float, y0: float, x1: float, y1: float, text: str, type_: int = 0) -> dict:
    """最小块：本文件只走 bbox（几何）与 spans 文本（判据）。"""
    return {"type": type_, "bbox": (x0, y0, x1, y1),
            "lines": [{"spans": [{"text": text, "font": "Arial", "size": 10}]}]}


def _left(text: str, y: float = 100.0) -> dict:
    return blk(51, y, 290, y + 200, text)


def _right(text: str, y: float = 100.0) -> dict:
    return blk(310, y, 549, y + 200, text)


# ── 1. 判据 ────────────────────────────────────────────────────────────────
def test_mid_sentence_column_spill_is_marked():
    """左栏末块没收句 + 右栏首块小写起 ⇒ 同一条缝，标在后一块上（合并时也并它）。"""
    a = _left("As discussed in Sect. 1.2, ML is classified into supervised learning; however, each of")
    b = _right("the above sections does not talk about the application of RL in AM.")
    marks = _column_spill_seams([a, b], _W, _H, _GUTTER)
    assert list(marks.values()) == ["col-spill"]
    assert id(b) in marks and id(a) not in marks


def test_left_column_ending_with_a_period_is_a_normal_column_switch():
    """左栏正常收句 ⇒ 右栏另起一段，是**正常换栏**，不许标（否则 agent 会把它合并掉）。"""
    a = _left("This paragraph ends properly with a period.")
    b = _right("a new paragraph that happens to start lowercase? no — but形如续写")
    assert _column_spill_seams([a, b], _W, _H, _GUTTER) == {}


@pytest.mark.parametrize("tail,head", [
    ("… ends mid sentence", "Upper case continuation"),        # 大写起 → 多半是新句/新段
    ("… ends mid sentence.", "lowercase after a full stop"),   # 前块收句了
    ("… ends mid sentence:", "lowercase"),                     # 冒号也算收句
])
def test_only_both_conditions_together_mark(tail, head):
    assert _column_spill_seams([_left(tail), _right(head)], _W, _H, _GUTTER) == {}


def test_no_gutter_means_no_seam():
    """单栏页（量不出 gutter）没有"栏间切缝"这回事。"""
    a = _left("… ends mid sentence")
    b = _right("lowercase continuation")
    assert _column_spill_seams([a, b], _W, _H, None) == {}


def test_same_side_neighbours_are_never_a_seam():
    """同一栏内相邻（L→L / R→R）不是栏间切缝 —— 那是段内换段。"""
    a = _left("left one ends mid sentence", y=100)
    b = _left("left two lowercase", y=320)
    assert _column_spill_seams([a, b], _W, _H, _GUTTER) == {}


def test_figures_break_the_seam_chain():
    """图（或图注）在两侧 ⇒ 不构成"半句话"。"""
    a = _left("… ends mid sentence")
    fig = blk(51, 320, 549, 420, "", type_=1)
    b = _right("lowercase continuation")
    assert _column_spill_seams([a, fig, b], _W, _H, _GUTTER) == {}


def test_marginal_blocks_do_not_form_seams():
    """页眉/页脚/页码不属于正文流（它们常常正落在栏间）。"""
    head = blk(51, _H * _MARGIN_BAND * 0.5, 549, _H * _MARGIN_BAND * 0.6,
               "1072 The International Journal of Advanced Manufacturing Technology")
    b = _right("lowercase continuation")
    assert _column_spill_seams([head, b], _W, _H, _GUTTER) == {}
    foot = blk(51, _H * (1 - _MARGIN_BAND * 0.4), 549, _H * (1 - _MARGIN_BAND * 0.3),
               "page footer text")
    assert _column_spill_seams([_left("… ends mid sentence"), foot], _W, _H, _GUTTER) == {}


def test_page_gutter_is_the_same_ruler_used_for_ordering():
    """排序与标缝必须用**同一个** gutter（两处各量一次迟早会错位）。"""
    blocks = [blk(51, 100 + i * 120, 290, 200 + i * 120, f"left {i}") for i in range(3)]
    blocks += [blk(310, 100 + i * 120, 549, 200 + i * 120, f"right {i}") for i in range(3)]
    gutter = _page_gutter(blocks, _W, _H)
    assert gutter is not None and 290 < gutter < 310
    # 按这个 gutter 排出来的顺序必须整栏读完再读下一栏（标缝依赖这个前提）
    tags = [_block_tags_for(b) for b in _reading_order(blocks, _W, _H)]
    assert tags == ["left 0", "left 1", "left 2", "right 0", "right 1", "right 2"]


def _block_tags_for(b: dict) -> str:
    return b["lines"][0]["spans"][0]["text"]


# ── 2. 端到端：真 PDF 上盖戳（合成两栏页，离线）────────────────────────────
def _two_col_pdf(path, left_last: str, right_first: str):
    fitz = pytest.importorskip("fitz")
    body = ("This paragraph contains enough words to wrap across several lines inside the column "
            "rectangle so that the extractor hands us one multi line block per textbox, which is "
            "what the real papers in this project look like.")
    src = fitz.open()
    pg = src.new_page(width=_W, height=792)
    pg.insert_textbox(fitz.Rect(51, 80, 290, 320), body, fontsize=10)
    pg.insert_textbox(fitz.Rect(51, 340, 290, 500), left_last, fontsize=10)
    pg.insert_textbox(fitz.Rect(310, 80, 549, 300), right_first, fontsize=10)
    pg.insert_textbox(fitz.Rect(310, 320, 549, 520), body, fontsize=10)
    src.save(str(path))
    src.close()


def _parse(path):
    from papershelf.pipeline.parse import parse_pdf
    return parse_pdf(path, assets_dir=path.parent / "assets")


def test_parse_stamps_the_seam_on_the_right_column_block(tmp_path):
    pdf = tmp_path / "two-col.pdf"
    _two_col_pdf(pdf, "The left column last paragraph ends mid sentence and",
                 "continues here in the right column with lowercase words.")
    doc = _parse(pdf)
    marked = [b for b in doc.blocks if b.payload.get("seam")]
    assert len(marked) == 1
    b = marked[0]
    assert b.payload["seam"] == "col-spill"
    assert b.payload["page"] == 1                     # 页码戳仍要在（阅读器分页依赖它）
    assert b.en.startswith("continues here in the right column")


def test_parse_only_marks_and_never_merges(tmp_path):
    """方案 B：解析阶段**只标记**，一句话仍然留在两个块里（合并是 agent 的活）。"""
    pdf = tmp_path / "two-col.pdf"
    _two_col_pdf(pdf, "The left column last paragraph ends mid sentence and",
                 "continues here in the right column with lowercase words.")
    doc = _parse(pdf)
    texts = [b.en for b in doc.blocks]
    assert any("ends mid sentence and" in t for t in texts)
    assert any("continues here in the right column" in t for t in texts)
    # 两个半句必须分处两块，且中间那块（左栏末块）不带戳
    i = next(i for i, t in enumerate(texts) if "ends mid sentence and" in t)
    assert doc.blocks[i].payload.get("seam") is None
    assert doc.blocks[i + 1].payload.get("seam") == "col-spill"


def test_parse_marks_nothing_when_the_left_column_ends_properly(tmp_path):
    pdf = tmp_path / "two-col.pdf"
    _two_col_pdf(pdf, "The left column last paragraph ends with a full stop.",
                 "A brand new right column paragraph starts here.")
    doc = _parse(pdf)
    assert [b for b in doc.blocks if b.payload.get("seam")] == []


# ── 3. agent 侧：四处入口都要能看到这条缝 ──────────────────────────────────
def _seam_doc() -> Doc:
    doc = Doc()
    doc.blocks.append(Block(id=make_block_id(1), type="p",
                            en="… and RL; however, each of", payload={"page": 1, "bbox": [51, 100, 290, 300]}))
    doc.blocks.append(Block(id=make_block_id(2), type="p",
                            en="the above sections does not talk about the application of RL.",
                            payload={"page": 1, "bbox": [310, 100, 549, 600], "seam": "col-spill"}))
    return doc


def test_read_blocks_surfaces_the_seam_and_what_to_do(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "t.pdf"
    src = fitz.open()
    src.new_page()
    src.save(str(pdf))
    src.close()
    tools = ProofreadTools(_seam_doc(), pdf)
    try:
        got = json.loads(tools.call("read_blocks", {"page": 1}).text)["blocks"]
        assert got[0].get("seam") is None
        assert "merge_block" in got[1]["seam"]                  # 说清该用什么工具
        assert "续段" in got[1]["flags"]                        # flags 里也要有（与字符串事实同列）
    finally:
        tools.close()


def test_check_artifacts_lists_seams_separately(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "t.pdf"
    src = fitz.open()
    src.new_page()
    src.save(str(pdf))
    src.close()
    tools = ProofreadTools(_seam_doc(), pdf)
    try:
        out = json.loads(tools.call("check_artifacts", {"page": 1}).text)
        assert list(out["seams"]) == ["b-0002"]
    finally:
        tools.close()


def test_suspect_pages_counts_seams_even_without_string_artifacts():
    """页级计数是 agent 唯一能看出"这页有栏间切缝"的入口 —— 漏了就是**假阴性**。"""
    doc = _seam_doc()
    assert _suspect_pages(doc) == {"1": 1}
    doc.blocks[1].payload.pop("seam")
    assert _suspect_pages(doc) == {}


def test_block_tags_is_the_single_source():
    """字符串事实与几何戳必须从**同一处**出，否则一个工具说有、另一个列不出来。"""
    doc = _seam_doc()
    assert _block_tags(doc.blocks[0]) == []
    assert _block_tags(doc.blocks[1]) == ["续段"]
    doc.blocks[0].en = "Text with vari- ous issues."
    assert "断词" in _block_tags(doc.blocks[0])


def test_prompt_requires_reviewing_the_block_split():
    """提示词必须明说"分块也要校对"，并给出**判据**（否则 agent 只当它是个普通可疑点）。"""
    assert "分块" in SYSTEM_PROMPT
    assert "seam" in SYSTEM_PROMPT
    assert "merge_block" in SYSTEM_PROMPT
    assert "句末标点" in SYSTEM_PROMPT


def test_merge_block_tool_points_at_the_seam(tmp_path):
    """工具的**自述**也要提，模型选工具时读的是它。"""
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "t.pdf"
    src = fitz.open()
    src.new_page()
    src.save(str(pdf))
    src.close()
    tools = ProofreadTools(_seam_doc(), pdf)
    try:
        spec = next(s for s in tools.specs() if s["function"]["name"] == "merge_block")
        assert "seam" in spec["function"]["description"]
    finally:
        tools.close()


# ── 4. 合并之后的**软提醒**（合并不可逆，判据却只是"疑似"）──────────────────
def test_merge_of_a_real_continuation_gets_no_warning(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "t.pdf"
    src = fitz.open()
    src.new_page()
    src.save(str(pdf))
    src.close()
    tools = ProofreadTools(_seam_doc(), pdf)
    try:
        out = tools.call("merge_block", {"id": "b-0002", "reason": "跨栏续段"}).text
        assert "已并入" in out and "⚠️" not in out
        assert tools.doc.blocks[0].en.endswith("does not talk about the application of RL.")
    finally:
        tools.close()


def test_merging_two_unrelated_blocks_gets_a_soft_warning(tmp_path):
    """**软提醒而非拒绝**：没有别的工具能连两块，硬拦会让 agent 在真需要合并时无路可走。

    但"前块正常收句 + 本块大写起"合并起来很可能变成一段连体文 —— 用**与标记同一套判据**
    复量一遍，把话说明（怎么拆回去），让 agent 自己决定信不信。
    """
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "t.pdf"
    src = fitz.open()
    src.new_page()
    src.save(str(pdf))
    src.close()
    doc = Doc()
    doc.blocks.append(Block(id="b-0001", type="p", en="A paragraph that ends properly here.",
                            payload={"page": 1, "bbox": [51, 100, 549, 200]}))
    doc.blocks.append(Block(id="b-0002", type="p", en="Another separate paragraph starts uppercase.",
                            payload={"page": 1, "bbox": [51, 220, 549, 300]}))
    tools = ProofreadTools(doc, pdf)
    try:
        out = tools.call("merge_block", {"id": "b-0002", "reason": "看着像一句"}).text
        assert "已并入" in out and "⚠️" in out and "split_block" in out   # 可逆的出路要给出
        assert len(tools.doc.blocks) == 1                                # 仍然照做（不是拒绝）
    finally:
        tools.close()


def test_criteria_have_a_single_source():
    """解析侧的标记与校对侧的复量必须读**同一个**判据函数（否则信号会自相矛盾）。"""
    import inspect
    from papershelf.pipeline import parse as P
    from papershelf.pipeline import proofread as PR

    assert "_looks_like_continuation" in inspect.getsource(P._column_spill_seams)
    assert "_looks_like_continuation" in inspect.getsource(PR.ProofreadTools._t_merge_block)
    # 判据本身：两种"不像"各一例 + 一个正例
    assert P._looks_like_continuation("… however, each of", "the above sections does not")
    assert not P._looks_like_continuation("… ends with a period.", "Another paragraph")
    assert not P._looks_like_continuation("… ends mid sentence", "Uppercase continuation")
