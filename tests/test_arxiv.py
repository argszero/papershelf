"""arXiv 导入（⑱）的离线回归。

`tests/data/arxiv_sample.html` 是从 arXiv 真实页面（1706.03762 Attention）
**原样切下来的片段**——不联网也能测，且能钉住 laTeXML 的结构约定。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from papershelf.pipeline import Doc
from papershelf.pipeline.markup import render_block
from papershelf.server import arxiv

SAMPLE = (Path(__file__).parent / "data" / "arxiv_sample.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("ref,want", [
    ("1706.03762", ("1706.03762", "")),
    ("https://arxiv.org/abs/2405.04434v2", ("2405.04434", "v2")),
    ("arXiv:1706.03762v5", ("1706.03762", "v5")),
])
def test_normalize_ref(ref, want):
    assert arxiv.normalize_ref(ref) == want


def test_normalize_ref_rejects_garbage():
    with pytest.raises(ValueError):
        arxiv.normalize_ref("这不是 arXiv 编号")


@pytest.fixture()
def doc(settings, monkeypatch) -> Doc:
    monkeypatch.setattr(arxiv, "_fetch_html", lambda *a, **k: SAMPLE)
    monkeypatch.setattr(arxiv, "_download_pdf", lambda *a, **k: pytest.fail("不该回落到 PDF"))
    # ⚠️ 图片下载也必须打桩：否则单测会真去 arxiv.org 拉图，慢且离线必挂
    monkeypatch.setattr(arxiv, "_download_image", lambda url, target: None)
    return arxiv.import_arxiv_doc({"id": 7, "title": "", "source": "arxiv",
                                   "source_ref": "1706.03762"}, settings)


def test_title_and_abstract_extracted(doc):
    assert doc.meta["title_en"] == "Attention Is All You Need"
    assert doc.blocks[0].type == "abstract"
    assert doc.blocks[0].en.startswith("The dominant sequence transduction")


def test_display_equation_becomes_eq_block_with_number(doc):
    """展示公式必须成为 `eq` 块，且**编号来自原文**（不是序号）。

    曾有的真实缺陷：非贪婪正则在内层 `</table>` 处提前截断，
    12 条展示公式静默丢成 5 条，编号退化成块序号。
    """
    eqs = [b for b in doc.blocks if b.type == "eq"]
    assert eqs, "展示公式一条都没抽出来"
    numbered = [b for b in eqs if b.payload.get("number")]
    assert numbered, "编号公式的编号丢了"
    assert numbered[0].payload["number"] == "1"
    assert "mathrm{Attention}" in numbered[0].payload["latex"]


def test_equation_latex_is_raw_and_clean(doc):
    """payload 里是**裸 LaTeX**（无定界符），且清掉了 `\\displaystyle`/`\\label` 之类残留。"""
    for b in doc.blocks:
        if b.type == "eq":
            tex = b.payload["latex"]
            assert not tex.startswith(("\\(", "\\["))
            assert "\\displaystyle" not in tex and "\\label{" not in tex


def test_eq_block_renders_to_mathml_via_shared_path(doc):
    """eq 块必须经 `render_math_block` 渲染成 MathML。

    曾有的真实缺陷：payload 是裸 LaTeX，走通用 `render_math`（按定界符切分）
    会被当成散文整段转义，公式以源码显示。
    """
    eq = next(b for b in doc.blocks if b.type == "eq")
    html = render_block(eq, lang="en", typeset=True)
    assert "<math" in html and "math-raw" not in html


def test_inline_math_uses_native_alttext(doc):
    """行内公式直接用 `<math alttext>` 的原生 LaTeX —— 零 LLM 成本（决策㉓ 的捷径）。"""
    inline = [b for b in doc.blocks if b.type == "p" and "\\(" in b.en]
    assert inline, "行内公式没转成 LaTeX 定界符"
    assert any("Attention" in b.en for b in doc.blocks)     # 正文可用


def test_references_become_ref_blocks(doc):
    """arXiv 的 `<li id="bib…">` **天然一条一块** → 直接是 `ref`（完整条目）。

    决策㊹（2026-09-17）：`ref` **参与翻译，但只译标题**；`refs` 只留给
    「切不出条目的碎片」（PDF 路线才有那种碎片，arXiv 没有）。
    """
    refs = [b for b in doc.blocks if b.type == "ref"]
    assert refs and refs[0].en.strip()
    assert refs[0].en.startswith("[1]") or "Vaswani" in refs[0].en, refs[0].en[:80]
    assert not [b for b in doc.blocks if b.type == "refs"], "arXiv 条目不该落回 `refs` 碎片类型"


def test_references_are_expected_to_have_chinese(doc):
    """`ref` **不是**免中文块：标题必须译出来（否则就是 ㊹ 要修的那个缺陷本身）。"""
    from papershelf.pipeline.validate import NO_ZH_TYPES, expects_chinese

    refs = [b for b in doc.blocks if b.type == "ref"]
    assert refs
    assert "ref" not in NO_ZH_TYPES
    assert expects_chinese(refs[0].en, block_type="ref"), "参考文献条目的标题没被要求译出来"


def test_blocks_are_sequentially_numbered(doc):
    """块 ID 必须连续（插入摘要后统一重编号）——笔记锚点、修订、对齐全靠它。"""
    assert [b.id for b in doc.blocks] == [f"b-{i:04d}" for i in range(1, len(doc.blocks) + 1)]


def test_no_content_when_page_is_empty(settings, monkeypatch):
    """结构变化时必须**响亮失败**，而不是静默产出一篇空文档。"""
    monkeypatch.setattr(arxiv, "_fetch_html", lambda *a, **k: "<html><body>nope</body></html>")
    monkeypatch.setattr(arxiv, "_download_pdf", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        arxiv.import_arxiv_doc({"id": 1, "title": "", "source": "arxiv",
                                "source_ref": "1706.03762"}, settings)
