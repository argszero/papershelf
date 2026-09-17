"""页面图形（`PARSE_VERSION` v11）：**矢量标识**、**页边底纹**、**并排的图注**。

宿主 2026-09-17 三条原话（截图 + 文字）：

  「图标+Springer，提取成了 `Vol.:(0123456789)`」            ← 页脚那串**纯白**的字（v9 已不入产物）
  「Springer 的图还是没有」（生产截图 + PDF 截图对照）        ← ① 与 ② 两件事
  「表格和原 pdf 差异较大」（另一条线，见 `tests/test_table.py`）

「Springer 的图」其实是**两样东西**，都从来没进过产物：

1. **页脚的 Springer 马标** —— 它是 PDF 里**画出来的矢量填充**（`get_drawings()`），
   既不在文字层（`get_text()`）也不在图片层（image block）里 ⇒ `_margin_graphics` 聚类 +
   三条判据后栅格化成**透明 PNG**，作为 `deco` 块进产物。
2. **右上角的 "Check for updates" 徽标** —— 它是**图片**，被旧的"无图注就剔除"规则删了
   ⇒ v11 删掉那道过滤（`_finalize` 不再丢），页边带里的小图改判 `deco`。
   ⚠️ 但它的**内嵌图片对象只是底板**（圆环/字样是矢量）⇒ 必须**整体栅格化**，
   直接贴内嵌字节出来的是个灰方块（见本文件末尾那条用例）。

外加两条同源缺陷：

3. **页眉下那条横线太浅**（v10 画了，但渲染端硬编码成 1px 浅灰）：v11 把 PDF 的
   stroke 颜色/粗细存进 `payload["rule"]`，渲染端照抄（`--rule-c` / `--rule-w`）。
4. **`CRITICAL REVIEW` 后面的浅灰底纹**：同样是矢量填充 ⇒ `_margin_shades`，挂在被它
   衬底的那个**文字块**上（`payload["shade"]`）。主保险是"色块里必须包着**可见**文字" ——
   否则页脚那串白字的**黑框**会被照抄成两个黑方块。

本文件钉住判据的**两个方向**（该收的收、不该收的一个不收）、`typeset=False` 产物逐字不变、
透明通道真的在、以及两份 CSS / 阅读器分支的存在（少一边就是"导出有、屏幕上没有"）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from papershelf.pipeline.markup import CSS, render_block
from papershelf.pipeline.model import Block
from papershelf.pipeline.parse import (
    _FIG_MAX_PER_PAGE,
    _FIG_MAX_WIDTH_RATIO,
    _margin_graphics,
    _margin_shades,
    _pair_captions_by_geometry,
)
from papershelf.pipeline.proofread import SYSTEM_PROMPT
from papershelf.pipeline.validate import expects_chinese

ROOT = Path(__file__).resolve().parents[1]
READER_SRC = ROOT / "web" / "src"
READER_CSS = READER_SRC / "styles.css"
READER_TSX = READER_SRC / "pages" / "Reader.tsx"

_H = 792.0                    # 页高（与 `test_page_furniture.py` 同一量级）
_W = 595.0
_BAND = _H * 0.14             # 页边带的绝对高度（判据按比例，这里只用来摆样本）


def _rect(x0: float, y0: float, x1: float, y1: float):
    fitz = pytest.importorskip("fitz")
    return fitz.Rect(x0, y0, x1, y1)


def _draw(x0: float, y0: float, x1: float, y1: float, fill=(0, 0, 0),
          kind: str = "f") -> dict:
    """`get_drawings()` 里的一条绘图记录（只保留判据用到的字段）。"""
    return {"type": kind, "rect": _rect(x0, y0, x1, y1), "fill": fill}


def _box(x0: float, y0: float, x1: float, y1: float) -> dict:
    """文字块/图片块的最小形状：判据只看 `bbox`。"""
    return {"type": 0, "bbox": [x0, y0, x1, y1], "lines": []}


# ── 1. 矢量标识（`_margin_graphics`）───────────────────────────────────────
_LOGO_TEXT = [_box(51, 120, 549, 700)]        # 一个正文块：列宽 498pt


def test_vector_logo_in_the_page_margin_is_found():
    """页脚那块 42×11pt 的黑色填充（Springer 马标）→ 一个候选，坐标原样。"""
    out = _margin_graphics([_draw(502.2, 736.2, 544.3, 747.3)], _LOGO_TEXT, [], _H)
    assert out == [[502.2, 736.2, 544.3, 747.3]]


def test_two_pieces_of_one_brand_mark_are_clustered():
    """马头与字标是两块填充、横间 4.2pt —— 不聚类就会变成两张"图"。"""
    out = _margin_graphics(
        [_draw(502.2, 736.2, 510.8, 746.9), _draw(515.0, 738.9, 544.3, 747.3)],
        _LOGO_TEXT, [], _H)
    assert len(out) == 1
    assert out[0][0] == 502.2 and out[0][2] == 544.3


def test_band_covering_visible_text_is_never_a_logo():
    """压着可见文字的色块不是"标识"（`CRITICAL REVIEW` 后面的灰底就在这一条落选）。

    这条是**主保险**：把正文截成图的代价远大于少一张标识（凭空多出一张"正文截图"）。
    """
    body = _box(51.0, 300.0, 549.0, 311.0)            # 正文块（把列宽撑到 498pt）
    line = _box(56.8, 64.2, 137.1, 75.8)              # 页眉里那行 "CRITICAL REVIEW"
    wide = _draw(51.0, 60.5, 289.1, 79.4, fill=(0.77, 0.78, 0.78))
    assert _margin_graphics([wide], [body, line], [], _H) == []
    # 换成**窄**色块（把它从"太宽"那条判据里摘出来，否则它只是被宽度判据顺便救了一把）——
    # 仍必须被"压着可见文字"拦住。变异检验：删掉那一条判据，本断言立刻转红。
    narrow = _draw(51.0, 60.5, 120.0, 79.4, fill=(0.77, 0.78, 0.78))
    assert _margin_graphics([narrow], [body, line], [], _H) == []
    # 同一块窄色块，那行字不在色板里 ⇒ 它就是一个候选（证明上一条不是"什么都不收"）
    assert _margin_graphics([narrow], [body], [], _H) == [[51.0, 60.5, 120.0, 79.4]]


def test_drawing_over_an_image_block_is_never_a_logo():
    """与已有图片块重叠的矢量图形不重复出图（实测首页徽标：图片层与矢量层各一份）。"""
    out = _margin_graphics([_draw(515.8, 66.4, 544.1, 94.7)],
                           _LOGO_TEXT, [_box(515.8, 66.5, 544.1, 94.6)], _H)
    assert out == []


def test_element_inside_the_body_is_ignored():
    """正文带里的图形不归这里管（表格线、图里的色块）。"""
    out = _margin_graphics([_draw(100.0, 400.0, 142.1, 411.1)], _LOGO_TEXT, [], _H)
    assert out == []


def test_tiny_element_is_noise():
    """小于 4pt 的当噪声丢掉（页面上的碎点/装饰粒）。"""
    out = _margin_graphics([_draw(502.2, 740.0, 505.0, 743.0)], _LOGO_TEXT, [], _H)
    assert out == []


def test_wide_decorative_band_is_not_a_logo():
    """通栏色带不是标识：宽度达到正文列宽 40% 的一律不收。"""
    wide = 498 * _FIG_MAX_WIDTH_RATIO + 5
    out = _margin_graphics([_draw(51.0, 736.0, 51.0 + wide, 746.0)], _LOGO_TEXT, [], _H)
    assert out == []


def test_tall_graphic_is_not_a_logo():
    """高过 40pt 的是插图（正文里的大图自有图片块管），不是页边标识。"""
    out = _margin_graphics([_draw(502.2, 700.0, 544.3, 747.3)], _LOGO_TEXT, [], _H)
    assert out == []


def test_per_page_cap_holds():
    """每页最多 `_FIG_MAX_PER_PAGE` 个 —— 判据万一走偏也不至于满页都是图。"""
    many = [_draw(60.0 + i * 60.0, 10.0, 100.0 + i * 60.0, 21.0)
            for i in range(_FIG_MAX_PER_PAGE + 3)]
    out = _margin_graphics(many, [_box(51, 120, 549, 700)], [], _H)
    assert len(out) == _FIG_MAX_PER_PAGE


# ── 2. 页边底纹（`_margin_shades`）─────────────────────────────────────────
_SHADE = _draw(45.0, 24.0, 300.0, 52.0, fill=(0.774, 0.776, 0.778))


def test_shading_behind_visible_text_is_kept():
    """"色块**包着可见文字**" ⇒ 这是底纹，记在那个文字块上（颜色原样）。"""
    text = _box(51.0, 28.0, 180.0, 40.0)
    out = _margin_shades([_SHADE], [text], [], _H)
    assert out == {id(text): {"color": "#c5c6c6"}}


def test_shading_without_visible_text_is_never_kept():
    """**黑框护栏**：页脚 `Vol.:(0123456789)` 是纯白字（v9 起不入产物），它那个黑框里
    就"没有可见文字"了 —— 白字一丢，黑框照抄出来就是两个黑方块。

    页面上**另有**在页边带里的文字（期刊页眉）时最容易出事故：判据必须比的是
    "这行字有 ≥60% 落在这块色板里"，而不是"页面上有没有文字"。
    """
    assert _margin_shades([_SHADE], [], [], _H) == {}
    header = _box(400.0, 28.0, 549.0, 40.0)          # 同一页的另一行页眉，**不在**色板里
    assert _margin_shades([_SHADE], [header], [], _H) == {}


def test_shading_only_partly_covering_a_line_is_not_its_backdrop():
    """只压住一小截的色块不是这行字的底纹（`_SHADE_COVER`：覆盖比例要够）。"""
    half = _box(250.0, 28.0, 400.0, 40.0)            # 与色板只重叠 50pt / 150pt
    assert _margin_shades([_SHADE], [half], [], _H) == {}


def test_shading_over_an_image_is_part_of_the_image():
    """图片里的灰色填充（徽标圆环）属于图，不是文字的底纹。"""
    text = _box(51.0, 28.0, 180.0, 40.0)
    out = _margin_shades([_SHADE], [text], [_box(45.0, 24.0, 300.0, 52.0)], _H)
    assert out == {}


def test_shading_in_the_body_is_ignored():
    """正文里的色块（表格底纹、图例）不归这里管。"""
    text = _box(51.0, 300.0, 180.0, 320.0)
    shade = _draw(45.0, 296.0, 300.0, 324.0)
    assert _margin_shades([shade], [text], [], _H) == {}


def test_stroke_is_not_a_shade():
    """只有填充（`type="f"`）才是底纹；描边（`type="s"`）是线，归 `_rule_marks`。"""
    text = _box(51.0, 28.0, 180.0, 40.0)
    stroke = _draw(45.0, 24.0, 300.0, 52.0, kind="s")
    assert _margin_shades([stroke], [text], [], _H) == {}


# ── 3. 并排（邻栏）的图注也要配上（`_pair_captions_by_geometry`）───────────
def test_caption_beside_the_figure_in_the_next_column_is_paired():
    """实测 Fig. 8/21/24：图占满右栏、图注排在被挤窄的**左栏**，两块纵向几乎完全重叠。

    旧判据在这种情形下量的是"纵向重叠 / 图高"，重叠 80.5pt 对 0.25×318pt 的阈值
    **差 0.9pt 被拒** ⇒ 图注配不上 ⇒ 图与图注一起消失。
    """
    img = {"type": 1, "bbox": [184.3, 397.3, 544.3, 715.6]}
    cap = {"type": 0, "bbox": [51.0, 395.0, 163.0, 477.8],
           "lines": [{"spans": [{"text": "Fig. 8  Lattice design and DIC testing."}]}]}
    caps, used = _pair_captions_by_geometry([img], [cap])
    assert caps[id(img)].startswith("Fig. 8")
    assert id(cap) in used


def test_caption_deep_inside_the_figure_is_still_rejected():
    """反过来：文字**压在图片里面**（横纵都重叠一大片）不是它的图注（可能是图里的字）。"""
    img = {"type": 1, "bbox": [184.3, 397.3, 544.3, 715.6]}
    cap = {"type": 0, "bbox": [250.0, 450.0, 500.0, 600.0],
           "lines": [{"spans": [{"text": "Fig. 3  some text inside the image"}]}]}
    caps, _used = _pair_captions_by_geometry([img], [cap])
    assert caps == {}


def test_caption_far_beside_the_figure_is_not_paired():
    """并排也要"够近"：横向间隙超过 `_CAP_MAX_GAP` 的话，那是别栏的话。"""
    img = {"type": 1, "bbox": [400.0, 397.3, 544.3, 715.6]}
    cap = {"type": 0, "bbox": [51.0, 400.0, 163.0, 470.0],
           "lines": [{"spans": [{"text": "Fig. 9  something else entirely"}]}]}
    caps, _used = _pair_captions_by_geometry([img], [cap])
    assert caps == {}


# ── 4. 渲染：装饰类、透明通道、以及"机器面"逐字不变 ─────────────────────────
def _deco(**payload) -> Block:
    payload.setdefault("src", "assets/p1_deco1.png")
    return Block(id="b-0009", type="deco", en="", zh="", zh_source="none", payload=payload)


def test_deco_renders_with_one_class_attribute_and_both_axis_classes():
    html = render_block(_deco(band="bottom", align="right", w=61, h=16), lang="en", typeset=True)
    assert html.startswith('<div class="deco deco-bottom deco-right"'), html
    # ⚠️ 合成**一个** class 属性（v11 首次实测就吐成了 `"deco deco deco-bottom deco-right"`）
    assert html.count("class=") == 1
    assert 'style="width:61px;height:16px"' in html, "显示尺寸必须按 PDF 物理尺寸给"


def test_deco_carries_no_decoration_into_the_machine_facing_renders():
    """`typeset=False`（校验/prompt/LaTeX 化）里：装饰类与显示尺寸**一个都不带**。

    `deco` 是**没有文字**的块，所以它照旧吐一个带 `data-b` 的空壳 —— 校验器按标记比对
    EN/ZH 两份产物，缺了标记会被判成"块丢失"（这是**数据完整性**，不是装饰）。
    """
    html = render_block(_deco(band="top", align="left", w=40, h=40), lang="en", typeset=False)
    assert 'data-b="b-0009"' in html
    assert "class=" not in html and "style=" not in html, html


def test_deco_never_asks_for_chinese():
    """`deco` 没有文字：要求它"有中文"会立刻变成一场永不收敛的重译。"""
    assert expects_chinese("", block_type="deco") is False


def test_shade_is_rendered_as_a_css_variable():
    b = Block(id="b-0002", type="p", en="CRITICAL REVIEW", zh="", zh_source="none",
              payload={"shade": {"color": "#c5c6c6"}})
    html = render_block(b, lang="en", typeset=True)
    assert 'class="pg-shade"' in html and "--shade-c:#c5c6c6" in html, html
    # 机器面向的那一份里没有底纹（它还只是"一行文字"）
    assert render_block(b, lang="en", typeset=False) == '<p data-b="b-0002">CRITICAL REVIEW</p>'


def test_shade_color_from_pdf_is_validated_before_use():
    """颜色来自 PDF —— 不合格就当作没有底纹（绝不把 PDF 里的字符串当 CSS 用）。"""
    for bad in ("red; background:url(x)", "#12", "javascript:alert(1)"):
        b = Block(id="b-0002", type="p", en="x", zh="", zh_source="none",
                  payload={"shade": {"color": bad}})
        assert "pg-shade" not in render_block(b, lang="en", typeset=True)


def test_rule_colour_and_width_come_from_the_pdf_in_both_stylesheets():
    """v10 的线是渲染端自己编的 1px 浅灰（宿主看到的结论是"线还是没有"）。

    v11 起颜色/粗细照抄 PDF：服务端吐 `--rule-c` / `--rule-w`，两份 CSS 都要**消费**它们。
    """
    b = Block(id="b-0001", type="p", en="header", zh="", zh_source="none",
              payload={"band": "top", "rule": {"side": "below", "color": "#000000",
                                               "width": 1.5}})
    html = render_block(b, lang="en", typeset=True)
    assert "--rule-c:#000000" in html and "--rule-w:1.5px" in html, html
    reader = READER_CSS.read_text(encoding="utf-8")
    for css in (CSS, reader):
        assert re.search(r"border-top:\s*var\(--rule-w,\s*1px\)\s*solid\s*var\(--rule-c", css), (
            "两份 CSS 都必须按 PDF 的颜色/粗细画线，否则又是「线太浅看不见」")


def test_deco_is_rendered_by_the_reader_and_defined_in_its_stylesheet():
    """阅读器必须**显式**出 `deco` 一支：落到正文兜底分支会丢掉贴边/左右交替。"""
    tsx = READER_TSX.read_text(encoding="utf-8")
    assert "b.type === 'deco'" in tsx, "阅读器没有 deco 分支 —— 标识会掉进正文流里"
    branch = tsx[tsx.index("b.type === 'deco'"):]
    branch = branch[:branch.index("b.type === 'table'")]
    assert "deco-${band}" in branch and "deco-${align}" in branch, "贴边/左右交替的类名丢了"
    reader = READER_CSS.read_text(encoding="utf-8")
    for cls in (".deco", ".deco-top", ".deco-bottom", ".deco-left", ".deco-right"):
        assert f"{cls} " in reader or f"{cls}," in reader or f"{cls}{{" in reader.replace(" ", ""), (
            f"web/src/styles.css 缺少 {cls}")


def test_deco_image_is_inline_level_in_both_stylesheets():
    """`deco-left`/`deco-right` 是**靠 `text-align` 摆位**的，块级元素不听 `text-align`。

    阅读器那份 CSS 有一条全局 `img { display: block }`（第 60 行附近）—— 于是屏幕上
    左右交替被整个压平到左边（PDF 里奇数页右下、偶数页左下），而**导出件看着是对的**
    （服务端那份 CSS 没有那条全局规则），是最容易"看着像好了"的一类缺陷。
    两份 CSS 都必须显式写死 `display: inline-block`。"""
    reader = READER_CSS.read_text(encoding="utf-8")
    assert "img { display: block" in reader, "阅读器的全局 img 规则变了，这条判据要重看"
    for name, css in (("markup.CSS", CSS), ("web/src/styles.css", reader)):
        m = re.search(r"\.deco img\s*\{([^}]*)\}", css)
        assert m, f"{name} 里没有 .deco img 规则"
        assert "inline-block" in m.group(1), f"{name} 的 .deco img 不是 inline 级 —— 左右交替会失效"
        assert re.search(r"\.deco\.deco-(left|right)\s*\{[^}]*text-align", css), (
            f"{name} 缺少 deco-left/right 的 text-align"
        )


def test_proofread_prompt_forbids_touching_deco_and_shading():
    """①c 校对 agent 会在 `read_blocks` 里看到 `deco` 块 —— 不交代清楚它就可能删掉。"""
    assert "deco" in SYSTEM_PROMPT
    assert "底纹" in SYSTEM_PROMPT


# ── 5. 端到端：合成一页（页眉灰底 + 页脚矢量标识）→ 产物里两样都在 ────────
def _page_with_graphics(path):
    fitz = pytest.importorskip("fitz")
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    # 页眉：一条浅灰底纹 + 白底黑字
    pg.draw_rect(_rect(45, 24, 300, 52), color=None, fill=(0.774, 0.776, 0.778))
    pg.insert_textbox(fitz.Rect(51, 28, 289, 47), "CRITICAL REVIEW", fontsize=8.5)
    # 正文
    pg.insert_textbox(fitz.Rect(51, 120, 549, 700), "Body text. " * 60, fontsize=10)
    # 页脚：Springer 马标那种**黑色填充轮廓**（既是矢量、又没有任何文字）
    pg.draw_rect(_rect(502.2, 736.2, 544.3, 747.3), color=None, fill=(0, 0, 0))
    src.save(str(path))
    src.close()


def test_end_to_end_graphics_and_shading_land_in_the_document(tmp_path):
    from papershelf.pipeline.parse import parse_pdf

    pdf = tmp_path / "graphics.pdf"
    _page_with_graphics(pdf)
    doc = parse_pdf(pdf, assets_dir=tmp_path / "assets")

    # ① 矢量标识 → `deco` 块 + 磁盘上的透明 PNG
    decos = [b for b in doc.blocks if b.type == "deco"]
    assert len(decos) == 1, [b.type for b in doc.blocks]
    deco = decos[0]
    assert deco.payload["band"] == "bottom" and deco.payload["align"] == "right"
    assert deco.payload["bbox"] == [502.2, 736.2, 544.3, 747.3]
    png = tmp_path / "assets" / Path(str(deco.payload["src"])).name
    assert png.exists(), "标识没有落盘"
    fitz = pytest.importorskip("fitz")
    pix = fitz.Pixmap(str(png))
    assert pix.alpha, "标识必须是**透明** PNG（否则深色底上会带一块白板）"
    assert pix.width > (544.3 - 502.2) * 6, "出图倍率太低，放大会糊"

    # ② 页眉那条灰底 → 挂在它衬底的那个文字块上（颜色照抄 PDF）
    shaded = [b for b in doc.blocks if b.payload.get("shade")]
    assert len(shaded) == 1
    assert shaded[0].en.strip() == "CRITICAL REVIEW"
    assert shaded[0].payload["shade"]["color"] == "#c5c6c6"

    # ③ 正文块**不许**沾上这些戳
    body = [b for b in doc.blocks if (b.en or "").startswith("Body text")]
    assert body and all(not b.payload.get("shade") for b in body)

    # ④ 无图注的图片不再被丢（v11 撤掉了"无图注就剔除"那道过滤）
    assert not (doc.meta.get("dropped_images") or []), doc.meta.get("dropped_images")


def test_captionless_margin_image_is_rasterised_whole_not_dumped_as_its_inner_plate(tmp_path):
    """**「Springer 的图还是没有」的真根**（v11 本地实测踩到）：

    那枚徽标在 PDF 里是「**内嵌图片 + 矢量叠加**」叠出来的一个整体，而那个内嵌图片对象
    只是它底下那块**平坦底色板**（原件实测 30×29、226 字节），圆环与 "Check for updates"
    字样全是矢量画的。若把内嵌字节直接当装饰图贴出来，屏幕上就是一个**光的灰方块**
    —— 看起来像"改了没生效"，其实是取错了那一层。

    判据（能不能一眼看出区别）：产物那块图里必须**同时**有底板的颜色与矢量叠加层的颜色。
    """
    fitz = pytest.importorskip("fitz")
    from papershelf.pipeline.parse import parse_pdf

    pdf = tmp_path / "margin-image.pdf"
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    pg.insert_textbox(fitz.Rect(51, 120, 549, 700), "Body text. " * 60, fontsize=10)
    plate = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 60))
    plate.set_rect(plate.irect, (250, 250, 250))
    pg.insert_image(_rect(515.8, 66.5, 544.1, 94.6), pixmap=plate)   # 底板（图片层）
    pg.draw_circle(fitz.Point(530.0, 80.5), 12.0, color=(1, 0, 0), width=3)  # 叠加层（矢量）
    src.save(str(pdf))
    src.close()

    doc = parse_pdf(pdf, assets_dir=tmp_path / "assets")
    decos = [b for b in doc.blocks if b.type == "deco"]
    assert len(decos) == 1, [b.type for b in doc.blocks]
    assert not [b for b in doc.blocks if b.type == "figure"], "页边的无图注小图不该当成插图"
    png = tmp_path / "assets" / Path(str(decos[0].payload["src"])).name
    pix = fitz.Pixmap(str(png))
    assert pix.alpha, "页边标识必须是透明 PNG"
    assert pix.width > 28 * 6, "出图倍率太低，放大会糊"
    reds = sum(1 for y in range(pix.height) for x in range(pix.width)
               if (lambda c: c[0] > 150 and c[1] < 100 and c[2] < 100)(pix.pixel(x, y)))
    assert reds > 100, "矢量叠加层没进图 —— 那是「直接贴内嵌底板」的老毛病（灰方块）"


def test_large_captionless_image_at_the_page_bottom_stays_a_figure(tmp_path):
    """**尺寸这一条判据不能省**：页底那条 110pt 带里常常压着一张**大插图**
    （实测 37 页论文的 Fig. 21 就落在页底带，360×213pt）—— 那是正文插图、
    只是恰好没配上图注，**不能**被当成"出版社标识"贴到右下角当装饰图。

    （同一份 PDF 的 Fig. 5/8/20/25/21/24 都曾被"无图注就剔除"整张删掉，
    见 `_finalize` 的 v11 改动与 `tests/test_table.py` 旁边的端到端记录。）
    """
    fitz = pytest.importorskip("fitz")
    from papershelf.pipeline.parse import parse_pdf

    pdf = tmp_path / "bigimage.pdf"
    src = fitz.open()
    pg = src.new_page(width=_W, height=_H)
    pg.insert_textbox(fitz.Rect(51, 40, 549, 300), "Body text. " * 40, fontsize=10)
    fitz.Pixmap  # 造一张真图（内容不重要，尺寸与位置才是判据）
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 900, 550))
    pix.set_rect(pix.irect, (30, 90, 160))
    pg.insert_image(_rect(184.3, 502.9, 544.3, 715.6), pixmap=pix)
    src.save(str(pdf))
    src.close()

    doc = parse_pdf(pdf, assets_dir=tmp_path / "assets")
    figs = [b for b in doc.blocks if b.type == "figure"]
    decos = [b for b in doc.blocks if b.type == "deco"]
    assert len(figs) == 1 and not decos, [b.type for b in doc.blocks]
    box = figs[0].payload["bbox"]
    assert box[3] == 715.6 and (box[3] - box[1]) > 40.0, box   # 又大又在页底带里
