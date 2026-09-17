"""页面家具：**页眉/页脚文字**、**页边横线**与**页边底纹**（`PARSE_VERSION` v10/v11）
—— 离线，不联网、不调 LLM。

宿主 2026-09-17 两条原话（截图 + 文字）：

  「A, 页眉文字不需要有意丢掉。和 pdf 尽量保持一致」   ← ①
  「这里少了一条水平线」（圈的是每页页眉下那条通栏细线） ← ②

两条是同一件事的两半，因为**它们都来自"解析器只取文字、还顺手把页边重复的文字删掉"**：

1. `parse._running_headers` 会把"跨页重复出现在页边带的短文本"整行丢掉 —— 期刊页眉
   （`1052 Int J Adv Manuf Technol …`）正是这种东西，于是每页顶上一行**原件的版面内容
   被有意删掉了**。v10 撤掉这道过滤（水印/logo 那两道与页眉无关，照旧保留）。
2. `get_text()` 只回文字与图片块，**矢量线条一条都不进产物** —— 每页页眉下那条横线
   整份文档都不见。v10 用 `page.get_drawings()` 把它找回来，挂在**它所属的那一行文字**上
   （`payload["rule"]`：线在这一块的哪一侧），渲染端据此画页边装饰。

本文件钉住：
- 判据（`_rule_marks`）宁漏不误：**半截线**、**正文里的线**、**离文字太远的线**都不挂；
- 端到端（合成两页 PDF）：跨页重复的页眉**留着**，并且带上 `band=top` / `rule=below`；
- 渲染：装饰类**只出现在给人看的渲染里**（`typeset=False` 逐字不变 —— 导出/校验/prompt
  三条路径的产物一个字节都不许变）；
- 两份 CSS（服务端 `markup.CSS` + 阅读器 `web/src/styles.css`）**必须都有**这些类
  （少一边就是"界面上一半有、一半没有"，这种缺陷没有任何报错，只有肉眼可见）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from papershelf.pipeline.markup import CSS, _furn_cls, render_block
from papershelf.pipeline.model import Block
from papershelf.pipeline.parse import _Line, _PT_TO_PX, _RULE_MAX_GAP, _rule_marks

ROOT = Path(__file__).resolve().parents[1]
READER_CSS = ROOT / "web" / "src" / "styles.css"

_H = 792.0          # 页高（Letter/A4 都在这一量级；判据按比例，具体值不影响结论）


def _blk(x0: float, y0: float, x1: float, y1: float) -> dict:
    """最小块：`_rule_marks` 只看 `bbox`。"""
    return {"type": 0, "bbox": (x0, y0, x1, y1), "lines": []}


# ── 1. 判据：页边横线挂在**哪一块**上（`_rule_marks`）──────────────────────
def test_header_rule_is_attached_to_the_text_line_below_it():
    """页眉线：线在文字**之上**、贴着它 ⇒ 挂在那一行上，标 `below`（线在块下方）。"""
    head = _blk(51, 32, 540, 42)                 # 期刊页眉（贴页顶）
    body = _blk(51, 120, 540, 700)               # 正文
    lines = [_Line(52.0, 52.0, 40.0, 560.0, "#000000", 0.8)]   # 通栏细线，距页眉下沿 10pt
    marks = _rule_marks([head, body], lines, _H)
    # v11：值从字符串变成 `{side,color,width}` —— 颜色/粗细是**PDF 原件的**，
    # 渲染端不再自己编那条浅灰线（v10 的实际表现就是"线太浅，像没有"）。
    assert marks == {id(head): {"side": "below", "color": "#000000",
                               "width": round(0.8 * _PT_TO_PX * 2) / 2}}


def test_rule_inside_the_body_is_never_taken_for_a_page_rule():
    """正文带里的线（表格分隔线、图框线）不是页边横线 —— 一律不挂。

    这条是"宁可漏画"的核心：画错一条线比没有线更像排版事故（实测 Springer 第 3 页
    那张表的横线 `x=[208,544]` 就落在正文带，若只按"又细又长"判就会被当成页眉线）。
    """
    body = _blk(51, 120, 540, 700)
    lines = [_Line(400.0, 400.0, 40.0, 560.0)]
    assert _rule_marks([body], lines, _H) == {}


def test_half_width_line_is_not_a_rule():
    """半截线（分栏装饰、单元格竖线的横段）不算：必须横跨正文文字列。"""
    head = _blk(51, 32, 540, 42)
    lines = [_Line(52.0, 52.0, 51.0, 300.0)]
    assert _rule_marks([head], lines, _H) == {}


def test_line_far_from_any_text_is_not_attached():
    """离文字太远的线不挂：线必须**紧贴**某一行（否则"谁的那条线"无从判断）。"""
    body = _blk(51, 120, 540, 700)
    lines = [_Line(52.0, 52.0, 40.0, 560.0)]
    assert _rule_marks([body], lines, _H) == {}
    # 把线挪到刚好在容许距离内，就该挂上了（证明上一条不是因为别的条件被拒）
    near = _blk(51, 20, 540, 52 - _RULE_MAX_GAP)
    assert _rule_marks([near], lines, _H) == {id(near): {"side": "below", "color": None, "width": 1.0}}


def test_footer_rule_is_marked_above():
    """页脚线：文字在**线之下**（`Vol.:(0123456789)` 那一带的横线）⇒ 标 `above`。"""
    foot = _blk(51, 760, 540, 772)
    lines = [_Line(750.0, 750.0, 40.0, 560.0)]
    assert _rule_marks([foot], lines, _H) == {id(foot): {"side": "above", "color": None, "width": 1.0}}


# ── 2. 端到端：跨页重复的页眉**留着**（合成两页 PDF）──────────────────────
def _two_page_pdf(path, header: str):
    """两页、**页眉文字完全相同**、每页页眉下一条通栏细线。"""
    fitz = pytest.importorskip("fitz")
    src = fitz.open()
    for pno in range(2):
        pg = src.new_page(width=595, height=_H)
        pg.insert_textbox(fitz.Rect(51, 28, 549, 46), header, fontsize=8.5)
        pg.draw_line(fitz.Point(40, 54), fitz.Point(560, 54), color=(0, 0, 0), width=0.8)
        pg.insert_textbox(fitz.Rect(51, 120, 549, 400),
                          f"Body text of page {pno + 1}. " * 30, fontsize=10)
    src.save(str(path))
    src.close()


def test_repeated_page_header_survives_and_is_tagged(tmp_path):
    """v10 的核心行为：跨页重复的页眉**不再被删**，且带上版面戳。

    旧行为（v9 及以前）：`_running_headers` 把"跨页重复出现在页边的短文本"整行丢掉，
    两页的页眉一模一样 ⇒ 计数达阈值 ⇒ **两页的页眉都不见了**。这条测试因此在旧代码上必红。
    """
    header = "1052 Int J Adv Manuf Technol (2024) 135:1052-1087"
    pdf = tmp_path / "furniture.pdf"
    _two_page_pdf(pdf, header)

    from papershelf.pipeline.parse import parse_pdf
    doc = parse_pdf(pdf, assets_dir=tmp_path / "assets")

    heads = [b for b in doc.blocks if (b.en or "").startswith("1052 Int J")]
    assert len(heads) == 2, "跨页重复的页眉被丢掉了（v10 起不再这么做）"
    for b in heads:
        assert b.payload.get("band") == "top", "页眉没有被标成页边带（渲染端就没法把它渲成小字）"
        rule = b.payload.get("rule")
        assert isinstance(rule, dict) and rule.get("side") == "below", \
            f"页眉下那条横线没有被找回来：{rule!r}"
        # v11：颜色/粗细也要带过来（PDF 画的是纯黑 0.8pt；没有它们渲染端只能自己编）
        assert rule.get("color") == "#000000", rule
        assert float(rule.get("width") or 0) >= 1.0, rule
        assert b.payload.get("page") in (1, 2)
    # 正文块**不许**沾上版面戳（沾上就会把正文渲成小灰字、还多画一条线）
    body = [b for b in doc.blocks if (b.en or "").startswith("Body text")]
    assert len(body) == 2
    for b in body:
        assert b.payload.get("band") is None
        assert b.payload.get("rule") is None


# ── 3. 渲染：装饰类只在**给人看**的那一份里 ──────────────────────────────
def _p(**payload) -> Block:
    return Block(id="b-0001", type="p", en="Header line of the journal.",
                 zh="期刊页眉", zh_source="mt", payload=payload)


def test_furniture_classes_are_rendered_when_typeset():
    html = render_block(_p(band="top", rule="below"), lang="en", typeset=True)
    assert html.startswith('<p class="pg-band pg-top pg-rule-below"'), html
    # 合成**一个** class 属性：拆成两个 `class=` 时浏览器只认第一个，装饰静默失效
    assert html.count("class=") == 1


def test_furniture_changes_nothing_for_the_machine_facing_renders():
    """`typeset=False`（校验/翻译 prompt/LaTeX 化）的产物必须**逐字不变**。

    这是敢动渲染的底气所在：三条机器路径读的是"块的文本"，多一个 class 属性只会
    让 prompt 变脏、让校验 diff 失真。
    """
    plain = Block(id="b-0001", type="p", en="Header line of the journal.",
                  zh="期刊页眉", zh_source="mt", payload={})
    assert (render_block(_p(band="top", rule="below"), lang="en", typeset=False)
            == render_block(plain, lang="en", typeset=False))
    assert (render_block(_p(band="bottom", rule="above"), lang="zh", typeset=False)
            == render_block(plain, lang="zh", typeset=False))


def test_heading_keeps_the_rule_even_without_its_h_tag():
    """标题的 `<h1..h4>` 外壳由阅读器出（`wrap=False`）—— 横线**不能因此丢掉**。

    `<h2>` 里只允许短语内容，所以服务端只能回一个 `<span>` 兜住装饰；
    `span.pg-rule-*` 在两份 CSS 里都是 `display:block`，线照样横跨一行。
    """
    b = Block(id="b-0003", type="h2", en="2 Method", zh="2 方法", zh_source="mt",
              payload={"rule": "below"})
    html = render_block(b, lang="en", typeset=True, wrap=False)
    assert html.startswith('<span class="pg-rule-below">'), html
    assert "<h2" not in html, "外壳必须留给阅读器，否则会出现 <h2><h2>"


def test_band_small_text_never_applies_to_headings():
    """页边带的**小灰字**只作用于正文段：标题有自己的字号层级，不许被压成小灰字。

    万一有个真标题落在页顶带里（`band=top`），它也只是"带横线的标题"。
    """
    b = Block(id="b-0003", type="h2", en="2 Method", zh="", zh_source="none",
              payload={"band": "top", "rule": "below"})
    assert _furn_cls(b) == "pg-rule-below"
    assert _furn_cls(_p(band="top", rule="below")) == "pg-band pg-top pg-rule-below"


# ── 4. 两份 CSS 都要有这些类（少一边=一半界面没有）────────────────────────
def _class_names() -> set[str]:
    """`_furn_cls` 可能吐出的**全部**类名（从产出反推，不是手抄一份）。"""
    names: set[str] = set()
    combos = [{"band": "top", "rule": "below"}, {"band": "bottom", "rule": "above"},
              {"band": "top"}, {"band": "bottom"}, {"rule": "below"}, {"rule": "above"},
              {"shade": {"color": "#c5c6c6"}}]
    for pay in combos:
        names.update(_furn_cls(_p(**pay)).split())
    return names


def test_every_furniture_class_is_defined_in_both_stylesheets():
    """服务端 CSS 与阅读器 CSS 必须**都**定义这些类。

    起因同 `test_style_guard.py` 的家族：样式只写一边时没有任何报错 ——
    导出件上有线、屏幕上没有（或反过来），只能靠肉眼看出来。
    """
    names = _class_names()
    assert names == {"pg-band", "pg-top", "pg-bottom", "pg-rule-below", "pg-rule-above",
                     "pg-shade"}
    reader = READER_CSS.read_text(encoding="utf-8")
    missing_server = sorted(n for n in names if f".{n}" not in CSS)
    missing_reader = sorted(n for n in names if f".{n}" not in reader)
    assert not missing_server, f"markup.CSS 缺少类：{missing_server}"
    assert not missing_reader, f"web/src/styles.css 缺少类：{missing_reader}"


def test_rule_pseudo_elements_exist_in_both_stylesheets():
    """线是 `::after` / `::before` 的 border —— 伪元素规则缺了，类还在也没用。"""
    reader = READER_CSS.read_text(encoding="utf-8")
    for css in (CSS, reader):
        assert re.search(r"\.pg-rule-below::after", css), "缺 `.pg-rule-below::after`"
        assert re.search(r"\.pg-rule-above::before", css), "缺 `.pg-rule-above::before`"
        # 标题那一栏回的是 `<span class="pg-rule-*">`，必须扶正成块级，线才横跨得起来
        assert re.search(r"span\.pg-rule-below[^{]*\{[^}]*display:\s*block", css), (
            "`span.pg-rule-*` 没有被扶正成块级 —— 标题上的线会画不出来"
        )
