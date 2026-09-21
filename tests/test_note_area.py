"""小字注区（表注 / 脚注 / 作者单位）—— 解析 v16，离线，不联网、不调 LLM。

宿主 2026-09-21 两张截图（生产 `reader/2` 的阅读器 vs PDF 原件）：
「这一段提取的不对」「主要是样式不对」。三处失真**全部在解析层**：

| 现象 | 真因 |
| --- | --- |
| 6 行注被拼成一段 | 组装文本走 `" ".join(text.split())` —— `split()` 连换行一起吃掉 |
| 字号偏大（不像小字） | 整块 8.5pt（正文 10pt）这个**事实没记**，渲染端按正文出 |
| 上方少一条横线 | 注上横线在页面**中部**（实测 81%），而 `_rule_marks` 只在页顶/页底 14% 找页眉线 |
| 行首 `a`–`e` 不是上标 | 行首那个 span 的字号差（6.4 vs 8.5pt）也没记 |

本文件钉住四件事：
1. **判据宁漏不误**：真注区认出；**展示公式的续行**（`2 e2`）不认；
2. **块文本与坐标**：按源行拼（换行留在文本里）、行首小标留在文本里、粘连的补一个空格；
3. **渲染**：`<sup>` 只加标签不加删字符（划痕坐标一个都不动）、`pg-note` 只出现在
   `typeset=True`、两份 CSS 都有 `pg-note` 且带 `white-space:pre-line`；
4. **翻译侧**：prompt 规则 8 在位；译文行数不一致触发重译，但**不**进「待校对」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from papershelf.pipeline import markup, parse
from papershelf.pipeline.markup import CSS, _furn_cls, _furn_style, render_block
from papershelf.pipeline.model import Block
from papershelf.pipeline.parse import _Line, _note_area, _note_rule
from papershelf.pipeline.translator import SYSTEM_PROMPT, Translator, _note_lines

ROOT = Path(__file__).resolve().parents[1]
READER_CSS = ROOT / "web" / "src" / "styles.css"

# 生产 paper 2 第 21 页那条 a–e 表注的**真实形状**（字号 8.5 / 正文 10、行首小标 6.4；
# `aThese` 之间没有空格 —— PDF 里那个间距是画出来的，不是字符）。
BODY = 10.0


def _span(text: str, size: float) -> dict:
    return {"text": text, "size": size, "bbox": (51.0, 648.3, 544.3, 660.0)}


def _line(*spans: dict) -> dict:
    return {"spans": list(spans), "bbox": (51.0, 648.3, 544.3, 660.0)}


def _note_block(text_lines: list[tuple[str, str]], *, main: float = 8.5,
                first: float = 6.4, body: float = BODY) -> dict:
    """`text_lines` = `[(行首 span 文本, 该行其余文本)]`（其余文本可为空串）。"""
    lines = []
    for head, rest in text_lines:
        spans = [_span(head, first)]
        if rest:
            spans.append(_span(rest, main))
        lines.append(_line(*spans))
    return {"type": 0, "bbox": (51.0, 648.3, 544.3, 706.6), "lines": lines}


def _label(text: str, markers: list[str] | None = None) -> Block:
    """注区块；`markers` 缺省时按行首字符推断（与解析端 `_NoteArea.markers` 同形）。"""
    if markers is None:
        markers = [ln[:1] if len(ln) > 1 and ln[:1].isalnum() else "" for ln in text.split("\n")]
    return Block(id="b-0001", type="p", en=text, zh="", zh_source="none", section="",
                 level=0, payload={"markers": markers})


# ── 1. 判据（`_note_area`）───────────────────────────────────────────────
def test_note_area_is_recognised_with_markers_lines_and_size_ratio():
    """真注区：a–e 五行 + 一行无标记的引导句 ⇒ 认出 + 每行标记 + 字号比 0.85。"""
    block = _note_block([
        ("All studies utilising DED-LB/M were powder-fed, and", ""),
        ("a", "These studies utilise an off-line optical micrograph image dataset"),
        ("b", "Simulated sensor data was used in this study"),
        ("c", "Different aspects of the same study"),
    ])
    note = _note_area(block, BODY)
    assert note is not None
    assert note.em == 0.85                       # 8.5 / 10.0
    assert note.markers == ["", "a", "b", "c"]
    assert note.text.split("\n")[0].startswith("All studies")
    assert note.text.count("\n") == 3             # 按源行拼：3 个换行 = 4 行


def test_marker_glued_to_the_next_word_gets_one_space():
    """`aThese studies…` → `a These studies…`。

    PDF 里上标小标与正文之间**没有空格字符**（间距是排版画出来的），不补这一格，
    屏幕上就是 `aThese` —— 补在**解析期**（任何批注产生之前），所以不动任何坐标系。
    """
    note = _note_area(_note_block([("a", "These studies utilise data"), ("b", "Second")]), BODY)
    assert note is not None
    assert note.lines[0].text == "a These studies utilise data"
    assert note.lines[0].marker == "a"
    # 行首标记**留在文本里**（渲染时只把它包进 `<sup>`，字符数不变）
    assert all(ln.text.startswith(ln.marker) for ln in note.lines if ln.marker)


def test_display_math_continuation_is_not_a_note_area():
    """**反向判据**（实测的 3 处误报）：展示公式的续行也"以数字起头"。

    `2 e2` / `1 + p3(β5 −β7k3)e2`：行首是 1–2 位数字、字号也确实不同（上下标），
    但它们所在的块**字号与正文一样**（10pt）—— 靠 `_NOTE_SIZE_GAP` 这一条挡掉。
    """
    block = _note_block([("2", " e2"), ("1", " + p3(β5 −β7k3)e2")], main=BODY, first=7.0)
    assert _note_area(block, BODY) is None


def test_one_marker_line_is_not_enough():
    """一条注通常是 a…b…c：只有一行以标记起头，多半是巧合（不是注区）。"""
    block = _note_block([("a", "alone"), ("Plain line here", "")])
    assert _note_area(block, BODY) is None


def test_note_rule_is_taken_from_the_line_just_above_the_block():
    """注上横线（页面中部那条）：横跨整块宽度、在块上方 26pt 内 ⇒ 记下来（含颜色粗细）。

    页边横线（`_rule_marks`）只在页顶/页底 14% 内找 —— 这条在 81% 处，从来落不进产物。
    ⚠️ 线宽折算后**至少 1px**（原件 0.57pt × 1.45 = 0.83px，四舍五入归零就等于又没有线）。
    """
    block = {"type": 0, "bbox": (51.0, 648.3, 544.3, 706.6), "lines": []}   # 注区 y=[648.3, 706.6]
    above = _Line(640.5, 640.5, 51.0, 544.3, "#000000", 0.57)     # 线在注区**上方**（原件的真实形状）
    far = _Line(520.0, 520.0, 51.0, 544.3, "#000000", 0.57)       # 太远（> 26pt）
    half = _Line(640.5, 640.5, 300.0, 544.3, "#000000", 0.57)     # 半截线
    # ⚠️ `side` 是 **"above"**：线画在这块的**上边**（`::before`）。写成 `"below"` 时线会跑到
    #    注区下面 —— 肉眼是"注下面多了一条线"，与宿主报的"上方少了那条横线"不是同一件事。
    assert _note_rule(block, [above]) == {"side": "above", "color": "#000000", "width": 1.0}
    assert _note_rule(block, [far]) is None
    assert _note_rule(block, [half]) is None


# ── 2. 渲染（`markup`）─────────────────────────────────────────────────
def test_note_rendering_marks_up_sup_markers_and_keeps_every_character():
    """`<sup>` **只加标签不加删字符** —— 这是敢动这一块的唯一理由。

    `web/src/marks.ts` 的坐标尺子按 DOM 文本节点逐字符累加（㉛）：只要 CDATA 与块文本
    逐字相同，这一块上已有的划痕就**一个都不动**（块 id / ord 也不变 —— 不拆块）。
    """
    text = "a First line\nb Second line"
    b = _label(text)
    b.payload["note"] = {"em": 0.85}
    html = render_block(b, lang="en", typeset=True, anchors=True)
    assert html.startswith('<p class="pg-note"')
    assert "<sup>a</sup>" in html and "<sup>b</sup>" in html
    stripped = re.sub(r"<[^>]+>", "", html)
    assert stripped == text, "渲染后文本与块裸文本必须逐字相同（坐标尺子的前提）"


def test_note_marks_land_on_the_same_characters_as_before():
    """划痕（㉛ 任意字符区间）在注区里必须落在**同一批字**上。

    取一段**跨过换行、且从第二行行首那个上标小标起**的区间（`"b Sec"`）：
    ⚠️ 上标是**先于**划痕决定的（`sup` 来自解析层的 `payload["markers"]`），所以
    `<sup>` 必须**嵌在** `<mark>` 里 —— 否则"划痕从行首那个 `a` 起"时那个字母不上色
    （坐标不变、颜色缺一格，是肉眼才看得见的失真）。
    """
    text = "a First line\nb Second line"
    b = _label(text)
    b.payload["note"] = {"em": 0.85}
    marks = [{"id": 7, "lang": "en", "start": 13, "end": 18}]      # "b Sec"
    html = render_block(b, lang="en", typeset=True, marks=marks)
    m = re.search(r'<mark[^>]*>(.*?)</mark>', html)
    assert m and re.sub(r"<[^>]+>", "", m.group(1)) == text[13:18] == "b Sec"
    assert "<sup>b</sup>" in m.group(1), "行首那个字母要跟着上色（上标嵌在划痕里）"
    # 换行符仍留在标签之间的**文本**里（`pre-line` 靠它分行），整块文本逐字未变
    assert re.sub(r"<[^>]+>", "", html) == text


def test_note_class_and_variable_only_in_the_human_facing_render():
    """`pg-note` / `--note-fs` 只挂在 `typeset=True`（导出、校验、prompt 三条路径产物不变）。"""
    b = _label("a First line\nb Second line")
    b.payload["note"] = {"em": 0.85}
    assert _furn_cls(b) == "pg-note"
    assert _furn_style(b) == "--note-fs:0.85em"
    assert "pg-note" not in render_block(b, lang="en", typeset=False)
    assert "--note-fs" not in render_block(b, lang="en", typeset=False)
    # 不是注区（没有 note 戳）的块一个字节都不沾
    plain = _label("a First line\nb Second line")
    assert _furn_cls(plain) == "" and _furn_style(plain) == ""


def test_bogus_note_payload_degrades_to_a_plain_paragraph():
    """`payload` 里的值来自 PDF 实测，但**进 CSS 之前必须再校验一次**（同线宽/颜色那条纪律）。

    字号越界（0.05em 这种字看不见）、`markers` 形状不对，一律当"不是注区"按正文渲染 ——
    宁可少一层样式，也不能把一个越界数值写进页面。
    """
    b = _label("a First line\nb Second")
    b.payload["note"] = {"em": 0.05}
    assert _furn_style(b) == "" and _furn_cls(b) == ""
    b.payload["note"] = {"em": "0.85em; background:url(x)"}
    assert _furn_style(b) == "" and _furn_cls(b) == ""
    b.payload["note"] = {"em": 0.85}
    b.payload["markers"] = ["a", "<script>"]
    html = render_block(b, lang="en", typeset=True)
    assert "<script>" not in html and "<sup>a</sup>" in html


def test_note_class_is_defined_in_both_stylesheets_with_pre_line():
    """两份 CSS 都要有 `.pg-note` **且带 `white-space: pre-line`**（少一边=一半界面失效）。"""
    reader = READER_CSS.read_text(encoding="utf-8")
    for name, css in (("markup.CSS", CSS), ("web/src/styles.css", reader)):
        m = re.search(r"\.pg-note\s*\{([^}]*)\}", css)
        assert m, f"{name} 里没有 .pg-note 规则"
        assert "pre-line" in m.group(1), f"{name} 的 .pg-note 没有 white-space: pre-line（六行注会折成一段）"
        assert "var(--note-fs" in m.group(1), f"{name} 的 .pg-note 没有读 --note-fs（字号不跟原件）"


# ── 3. 端到端：合成一页 → 产物里注区分了行、带了标记与那条线 ────────────────
_W, _H = 595.0, 792.0


def _page_with_note(path):
    fitz = pytest.importorskip("fitz")
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    pg.insert_textbox(fitz.Rect(51, 100, 544, 400), "Body text. " * 60, fontsize=10)
    # 注上横线（页面中部，通栏细线）
    pg.draw_line(fitz.Point(51, 640.5), fitz.Point(544.3, 640.5), color=(0, 0, 0), width=0.57)
    # 引导句（8.5pt）+ 三行小字注（行首 6.4pt 的上标小标）：**一个文本块**
    # ⚠️ 必须一次落进同一个块（`insert_htmlbox`）：逐行 `insert_text` 会得到**三个独立的块**
    #    （每块只有 1 行以标记起头）—— 判据要求 ≥2 行，于是"注区"根本认不出来，
    #    而那与生产 PDF 的形状不符（生产那六行是**一块**）。
    html = ('<div style="font-size:8.5pt;line-height:1.3">'
            'All studies utilising DED-LB/M were powder-fed.<br>'
            '<sup style="font-size:6.4pt">a</sup> These studies utilise an off-line dataset<br>'
            '<sup style="font-size:6.4pt">b</sup> Simulated sensor data was used in this study<br>'
            '<sup style="font-size:6.4pt">c</sup> Different aspects of the same study</div>')
    pg.insert_htmlbox(fitz.Rect(51, 648, 544, 700), html)
    src.save(str(path))
    src.close()


def test_end_to_end_note_area_lands_in_the_document(tmp_path):
    from papershelf.pipeline.parse import parse_pdf

    pdf = tmp_path / "note.pdf"
    _page_with_note(pdf)
    doc = parse_pdf(pdf, assets_dir=tmp_path / "assets")

    notes = [b for b in doc.blocks if isinstance(b.payload.get("note"), dict)]
    assert len(notes) == 1, [(b.type, b.en[:40]) for b in doc.blocks]
    note = notes[0]
    assert note.en.count("\n") >= 3, note.en
    assert note.payload["markers"][0] == "" and "a" in note.payload["markers"]
    assert note.payload["note"]["em"] < 0.95
    assert note.payload["rule"]["side"] == "above"      # 注**上**那条线（渲染成 `::before`）
    # 正文块**不许**沾上注区的戳（判据宁窄勿宽）
    body = [b for b in doc.blocks if (b.en or "").startswith("Body text")]
    assert body and all(not b.payload.get("note") for b in body)
    # 渲染出来：小字 + 分行 + 上标（类名与「注上那条线」可以并存 ⇒ 只断言前缀）
    html = render_block(note, lang="en", typeset=True)
    assert 'class="pg-note' in html and "<sup>a</sup>" in html and "\n" in html


# ── 4. 翻译侧：prompt 规则 8 + 行数护栏 ──────────────────────────────────
def test_prompt_requires_the_same_line_breaks():
    assert "换行" in SYSTEM_PROMPT
    assert "行数与行序都相同" in SYSTEM_PROMPT


def test_note_lines_helper_counts_source_lines():
    b = _label("a one\nb two")
    assert _note_lines(b) == 1                      # 没有 note 戳 ⇒ 当普通段落
    b.payload["note"] = {"em": 0.85}
    assert _note_lines(b) == 2


def test_translation_with_the_wrong_line_count_is_retried_once_but_not_flagged():
    """行数不一致 ⇒ 触发一次定点重译；重试后仍不一致就接受（**不**挂「待校对」）。

    理由：这是**结构**要求（对照模式两栏形状），不是"译错了" —— 按「待校对」报出来
    只会让读者看到一堆无意义的黄标（而它真的重试不过时，退化成一段普通译文是可读的）。
    """
    b = _label("a one\nb two")
    b.payload["note"] = {"em": 0.85}
    texts = {b.id: "一段没有分行的中文译文"}
    assert Translator._check([b], texts) == [b.id]                      # 触发重译
    assert Translator._check([b], texts, lines_must_match=False) == []  # 收尾不报
    texts[b.id] = "a 第一行\nb 第二行"
    assert Translator._check([b], texts) == []
    # 普通段落（没有 note 戳）不受这条影响：一段中文照样合格
    plain = _label("a one\nb two")
    assert Translator._check([plain], {plain.id: "不分行的中文"}) == []


def test_markup_exports_the_note_readers():
    """`_note_em` / `_note_markers` 是渲染端的判据入口（与解析端同名的两个戳一一对应）。"""
    assert callable(markup._note_em) and callable(markup._note_markers)
    b = _label("a one\nb two")
    b.payload["note"] = {"em": 0.85}
    b.payload["markers"] = ["a", "b"]
    assert markup._note_em(b) == 0.85
    assert markup._note_markers(b) == ["a", "b"]
