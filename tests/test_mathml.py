"""公式渲染（遗留项 8）：服务端 LaTeX → MathML，零 CDN、零前端依赖。

这些断言都对应真实踩过的坑，不是形式化覆盖。
"""

from __future__ import annotations

import re

import pytest

from papershelf.pipeline import Doc
from papershelf.pipeline.mathml import latex_to_mathml, render_math
from papershelf.pipeline.markup import render_block, render_fragment
from papershelf.pipeline.model import Block


def test_no_cdn_anywhere_in_rendered_html():
    """产物里不许出现任何 CDN —— 自托管常在内网/离线（决策③）。"""
    from papershelf.pipeline import document_html

    html = document_html([Block(id="b-0001", type="p", en="速度 \\( v \\) 恒定。")],
                         lang="zh", title="t")
    assert "cdn" not in html.lower()
    assert "mathjax" not in html.lower()
    assert "<math" in html


def test_inline_and_display_math_render():
    blocks = [
        Block(id="b-0001", type="p", en="其中 \\( x = y + z \\) 成立。"),
        Block(id="b-0002", type="eq", en="\\[ \\frac{a}{b} = c \\]",
              payload={"latex": "\\frac{a}{b} = c", "number": "4"}),
    ]
    html = render_fragment(blocks, lang="en", typeset=True)
    assert html.count("<math") == 2
    assert "math-display" in html
    # 编号必须落到产物里（读者要靠它对齐原文）
    assert "4" in html


def test_math_text_is_escaped_but_math_is_not():
    html = render_math("a < b 且 \\( x_1 < x_2 \\)")
    assert "&lt;" in html                      # 散文被转义
    assert "<math" in html                     # 公式是真标签


def test_render_block_defaults_to_raw_latex():
    """默认**不渲染**：漏传 typeset 时宁可显示源码，也绝不把 MathML 喂给 LLM。"""
    b = Block(id="b-0001", type="p", en="其中 \\( x \\) 成立。")
    assert "<math" not in render_block(b, lang="en")
    assert "\\(" in render_block(b, lang="en")
    assert "<math" in render_block(b, lang="en", typeset=True)


def test_llm_prompts_never_contain_mathml():
    """翻译/LaTeX 化的 prompt 必须是 LaTeX 源码（含 MathML 会让模型去猜，必错）。"""
    from papershelf.pipeline.equations import Latexizer
    from papershelf.pipeline.translator import Translator

    b = Block(id="b-0007", type="p", en="设 \\( V(x) > 0 \\) 成立。")
    tr = Translator.__new__(Translator)
    tr.glossary = []
    prompt = tr._user_prompt([b], None, None)
    assert "<math" not in prompt
    assert "\\(" in prompt

    lz = Latexizer.__new__(Latexizer)
    assert "<math" not in lz._prompt([b])


def test_validation_sees_raw_latex_not_mathml(tmp_path):
    """校验器拿到的必须是源码：`\\tag{}` 里的编号在渲染后就找不到了。"""
    from papershelf.pipeline import document_html, en_html

    doc = Doc(meta={"title_zh": "题"}, blocks=[
        Block(id="b-0001", type="p", en="式 \\( a \\) 给出。", zh="式 \\( a \\) 给出。",
              zh_source="mt"),
        Block(id="b-0002", type="eq", en="\\[ b \\tag{7} \\]",
              payload={"latex": "b \\tag{7}", "number": "7"}),
    ])
    raw = en_html(doc, typeset=False)
    assert "<math" not in raw
    assert "\\tag{7}" in raw
    assert "<math" in document_html(doc.blocks, lang="zh", typeset=True)


@pytest.mark.parametrize("tex", [
    r"\frac{\partial V}{\partial x}", r"\begin{bmatrix} a & b \\ c & d \end{bmatrix}",
    r"\left\| x \right\|_2^2", r"\tilde{x} + \hat{y}", r"\alpha\beta\Gamma",
    r"\sum_{\substack{i=1\\ i\neq j}}^{n} x_i",
])
def test_hard_latex_constructs_convert(tex):
    """实测会出现在真实论文里的构造，逐一确保可转（失败会回落源码，但不能成片失败）。"""
    out = latex_to_mathml(tex)
    assert out and "<math" in out


def test_tag_becomes_visible_number():
    """`\\tag{12}` 在 MathML 里要变成可见编号，否则读者不知道这是第几式。"""
    out = latex_to_mathml(r"a = b \tag{12}")
    assert out and "12" in out


def test_broken_latex_falls_back_instead_of_raising():
    """渲染是派生态：坏公式只回落，绝不能让整篇转换失败。"""
    html = render_math(r"\(\frac{1}{\)")
    assert "math-raw" in html and "&#92;(" in html or "\\(" in html
