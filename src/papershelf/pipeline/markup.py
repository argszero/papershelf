"""英文 HTML 生成 —— 决策④ 的第 ②步：**每个块带稳定标记**。

产物是单文件仿期刊 HTML（沿用宿主已验证产物的样式，决策③）：
单栏流式 `.page`（决策⑥）、**服务端渲染的 MathML 公式**（见 `mathml.py`，遗留项 8）、
相对路径引用图片。每个块元素带 `data-b="b-XXXX"` —— 该标记将被要求**穿透翻译**保留到中文版。

⚠️ `typeset` 开关的意义（**改动前务必理解**）：校验器（`validate.py`）比对的是**带标记的
LaTeX 原文**（`\tag{12}` 这类编号就写在里面）。所以：
- `typeset=False` → 保留 LaTeX 源码，**只给校验用**；
- `typeset=True`（默认）→ 渲染成 MathML，**给人看**。
渲染公式是**派生态**：LaTeX 始终留在 `Block.payload["latex"]` / 文本里，
将来换渲染器不必重跑 LLM。
"""

from __future__ import annotations

import html as _html
import re

from .mathml import mathml_css, render_math, render_math_block
from .model import Block, Doc, table_caption, table_cells, table_layout
from .validate import NO_ZH_TYPES, PARTIAL_ZH_TYPES

CSS = """
:root{--ink:#1a1a1a;--accent:#2f5d7a;--rule:#d8d2c4;--bg-abstract:#f3f7fa}
*{box-sizing:border-box}
body{margin:0;padding:0 0 60px;background:#eceae4;color:var(--ink);line-height:1.78;
  font-family:-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",Georgia,serif;font-size:16.5px}
.page{max-width:940px;margin:0 auto;background:#fff;box-shadow:0 0 18px rgba(0,0,0,.12);padding:44px 52px 70px}
@media(max-width:640px){.page{padding:22px 18px 50px}}
.masthead{border-bottom:3px solid var(--accent);padding-bottom:18px;margin-bottom:28px;text-align:center}
.masthead h1{font-size:29px;line-height:1.3;margin:14px 0 6px}
.masthead .meta{font-size:13.5px;color:#666;border-top:1px solid var(--rule);padding-top:12px}
h2.sec{font-size:21px;margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--rule)}
h3.sub{font-size:17.5px;margin:26px 0 10px}
h4{font-size:16px;margin:20px 0 8px}
p{margin:0 0 13px}
figure{margin:22px 0;text-align:center}
figure img{max-width:100%;height:auto;border:1px solid var(--rule);background:#f7f5f0}
figcaption{font-size:13.5px;color:#555;margin-top:8px;text-align:left}
div.eq{margin:14px 0;overflow-x:auto}
table.datatable{border-collapse:collapse;width:100%;margin:18px 0;font-size:14.5px}
table.datatable caption{caption-side:top;text-align:left;font-size:13.5px;color:#555;padding-bottom:6px}
table.datatable th,table.datatable td{border:1px solid var(--rule);padding:6px 9px;vertical-align:top}
.abstract{background:var(--bg-abstract);border-left:3px solid var(--accent);padding:14px 18px;margin:0 0 22px}
p.ref-item{font-size:13.5px;color:#444;margin:0 0 6px;padding-left:20px;text-indent:-20px}
/* ── 页面家具（见 `_furn_cls`）─────────────────────────────────────────────
   页眉/页脚小字：比正文小一号、灰。PDF 里它们是 8.5pt（正文 10pt），
   「和 pdf 尽量保持一致」就是把这点字号差也带上。
   页边横线：`::after`/`::before` 的 border —— 线跟着**承载它的那个块**走，
   块被 ①c 改写/挪动时线不会跑到别处（这也正是把线挂在块上、而不是挂在页上的原因）。 */
.pg-band{font-size:.84em;color:#6b6b6b;line-height:1.5}
.pg-band.pg-top{margin-top:0}
.pg-band.pg-bottom{margin-bottom:0}
/* 小字注区（v16：表注/脚注/作者单位）：字号按 PDF 原件与正文的比值（`--note-fs`，
   解析实测，如 8.5/10 = .85em）；`white-space: pre-line` 让块文本里的换行**照抄源行**
   （解析阶段按行拼接，见 `parse._note_area`）—— 不加这条，6 行注会被浏览器折成一段。 */
.pg-note{font-size:var(--note-fs,.85em);color:#6b6b6b;line-height:1.45;white-space:pre-line}
/* 线的**颜色与粗细照抄 PDF**（v11）：解析阶段把原件的 stroke 存进 `payload.rule`
   （`{side,color,width}`），渲染端用两个 CSS 变量接住 —— 内联样式只能作用在元素上，
   而线是伪元素画的，变量可以继承进伪元素。变量缺省时才退回主题色。 */
.pg-rule-below::after,.pg-rule-above::before{content:"";display:block;
  border-top:var(--rule-w,1px) solid var(--rule-c,var(--rule))}
.pg-rule-below::after{margin:6px 0 16px}
.pg-rule-above::before{margin:16px 0 6px}
/* 标题的 `<h1..h4>` 外壳由阅读器出（`wrap=False`），服务端只能回一个 `<span>` 兜住装饰
   （`<h2>` 里塞 `div` 会被浏览器甩出去）→ 这里把它扶正成块级，线才横跨得起来。 */
span.pg-rule-below,span.pg-rule-above{display:block}
/* 页边底纹（v11）：`CRITICAL REVIEW` 后面那条浅灰带 —— 颜色照抄 PDF（`--shade-c`），
   左右各负向抵消一点内边距，让色带与正文列对齐（原件色块从列边缘起）*/
.pg-shade { background: var(--shade-c, transparent); padding: .12em .38em; margin-inline: -.38em; border-radius: 2px; }
/* ── 页边装饰图（`deco`：出版社/期刊标识，v11）─────────────────────────────
   解析阶段把矢量标识**原样**渲染成透明 PNG（`parse._render_graphic`），尺寸也按原件的
   物理尺寸折算好放进 `payload`（`w`/`h`，CSS px）—— 渲染端只负责摆位，不重新缩放。
   ⚠️ 不要用 `figure` 的那套样式：标识没有图注，套上灰底加边框就不像原件了。 */
.deco{margin:8px 0;line-height:0}
.deco.deco-top{margin:0 0 12px}
.deco.deco-bottom{margin:12px 0 0}
.deco.deco-left{text-align:left}
.deco.deco-right{text-align:right}
/* 标识图**必须是 inline 级**：`deco-left`/`deco-right` 靠 `text-align` 摆位，
   块级元素不听 `text-align`（阅读器那份 CSS 里有一条全局 `img{display:block}`，
   实测把左右交替整个压平到左边；这里显式写死，两份 CSS 口径一致）。 */
.deco img{display:inline-block;max-width:100%;height:auto;vertical-align:middle}
[data-b]{scroll-margin-top:14px}
"""

MATHJAX = ""   # 已移除 CDN 依赖：公式改为服务端 LaTeX → MathML（遗留项 8）。保留空串以免下游 import 失败。


def _esc(s: str) -> str:
    return _html.escape(s or "", quote=False)


# ── 页面家具（页眉/页脚小字 + 页边横线，v10 决策）───────────────────────────
# 解析阶段把**版面事实**盖在块的 `payload` 上（`parse._tag_furniture`）：
#   `band` = "top"/"bottom" —— 这行字贴在页面上下边缘（期刊页眉、页码、出版社页脚）；
#   `rule` = "below"/"above" —— 这行字的一侧有一条横跨正文列的细线（Springer 页眉线）。
# 渲染端只**读**这两个戳，不自己按坐标猜（同 ㊴「配对判据只在服务端」：判据写两份迟早漂开）。
#
# 为什么只有这几条样式：宿主 2026-09-17「页眉文字不需要有意丢掉。和 pdf 尽量保持一致」——
# 文字留下来之后，如果按正文的 16.5px 渲染，屏幕上每页都顶着一行"和正文一样大的页眉"，
# 看起来像重复段落。PDF 里它是 8.5pt（正文 10pt）的灰字，所以这里就把它渲成
# **稍小 + 灰**，并在它那一侧补回那条线（矢量线此前一条都不在产物里）。
#
# ⚠️ 页边带的字号/颜色只作用于 `p`/`refs`/`ref`（`_BAND_TYPES`）：标题有自己的字号层级，
#    万一有个真标题落在页顶带里，也不该被压成灰字。横线则与块类型无关。
_BAND_TYPES = ("p", "refs", "ref")

#: PDF stroke 颜色（`parse._hex_color` 产出）—— 只有这个形状允许进 HTML 属性
_RE_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _rule_side(b: Block) -> str | None:
    """块的页边横线在**哪一侧**（`below`/`above`）；没有则为 None。

    ⚠️ v10 的 `payload["rule"]` 是字符串（"below"），v11 起是字典
    （`{"side","color","width"}`，见 `parse._rule_marks`）—— 这里两种都认：
    存量文档（解析版本 10 及以前落的库）与 ①c agent 改过的块都还在库里，
    读不出来就等于**那条线在屏幕上消失**（不会报错，只会少一条线）。
    """
    rule = b.payload.get("rule") if isinstance(b.payload, dict) else None
    side = rule.get("side") if isinstance(rule, dict) else rule
    return side if side in ("below", "above") else None


def _shade_color(b: Block) -> str:
    """块背后那块**底纹**的颜色（`#rrggbb`）；没有或不合格则空串。

    值来自 PDF 的填充色（`parse._margin_shades`），必须校验后再拼进 HTML 属性 ——
    宁可没有底纹，也不能把 PDF 里的字符串当 CSS 用（同 `_furn_style` 对线宽的颜色校验）。
    """
    p = b.payload if isinstance(b.payload, dict) else {}
    shade = p.get("shade")
    color = shade.get("color") if isinstance(shade, dict) else None
    return color if isinstance(color, str) and _RE_HEX_COLOR.fullmatch(color) else ""


# ── 小字注区（v16：表注 / 脚注 / 作者单位）─────────────────────────────────
# 解析阶段（`parse._note_area`）认出来的**小字多行注**：
#   · 文本里**保留着源行的换行**（`"\n"`）—— 渲染端用 `white-space: pre-line` 照抄分行；
#   · `payload["note"]["em"]` = 这一块的字号 ÷ 正文字号（原件的事实，渲染端照抄）；
#   · `payload["markers"]` = 每行行首那个**上标小标**（"a"/"b"/…，没有的行是空串）。
#
# ⚠️ 换行与行首那个字母**必须原样留在 DOM 文本里**（渲染时只**加标签**、不加删字符）：
#    `web/src/marks.ts` 的坐标尺子是"按 DOM 文本节点逐字符累加"（㉛），少一个字符，
#    这一块上的所有划痕就整体平移 —— 所以 `<sup>` 只是把那个**本来就有的**字母包起来。
_RE_NOTE_MARK = re.compile(r"^[a-zA-Z]$|^\d{1,2}$")


def _note_em(b: Block) -> float:
    """小字注区的字号比（`em`）；不是注区或数值不合格则 0.0。

    值来自 PDF 实测（`parse._note_area`），与线宽/颜色同一条纪律：**校验后再进 CSS**。
    区间取 (0.5, 0.98]：小字注比正文小，但不至于小到看不见（越界一律当"不是注区"，
    宁可按正文渲染，也不能把一个 0.05em 的字号写进页面）。
    """
    p = b.payload if isinstance(b.payload, dict) else {}
    note = p.get("note")
    raw = note.get("em") if isinstance(note, dict) else None
    try:
        em = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return em if 0.5 < em <= 0.98 else 0.0


def _note_markers(b: Block) -> list[str]:
    """与注区各**行**平行的行首标记（不是注区则空表）。

    只认单字母 / 1–2 位数字（`_RE_NOTE_MARK`）：这个值会进 HTML，形状不认识的
    一律当"这行没有标记"（宁可少一个 `<sup>`，也不能把 `payload` 里的任意串写进页面）。
    """
    if not _note_em(b):
        return []
    p = b.payload if isinstance(b.payload, dict) else {}
    ms = p.get("markers")
    if not isinstance(ms, list):
        return []
    return [m if isinstance(m, str) and _RE_NOTE_MARK.match(m) else "" for m in ms]


def _furn_cls(b: Block) -> str:
    """块的**版面装饰** CSS 类（`pg-band pg-top` / `pg-rule-below` / `deco`…）；没有则空串。

    ⚠️ 只在**给人看的渲染**（`typeset=True`）里挂（`render_block` 里判）：
    校验/翻译/LaTeX 化 prompt 走的是 `typeset=False`，产物必须逐字不变
    （它们要的是"块的文本"，多一层 class 属性只会让 prompt 变脏、让 diff 失真）。
    """
    p = b.payload if isinstance(b.payload, dict) else {}
    out: list[str] = []
    if b.type == "deco":
        # 页边装饰图：`deco` + 贴页顶/页底 + 靠左/靠右（左右交替是原件的排版）
        out.append("deco")
        if p.get("band") in ("top", "bottom"):
            out.append(f"deco-{p['band']}")
        if p.get("align") in ("left", "right"):
            out.append(f"deco-{p['align']}")
        return " ".join(out)
    band = p.get("band")
    if band in ("top", "bottom") and b.type in _BAND_TYPES:
        out.append(f"pg-band pg-{band}")
    if _note_em(b):
        out.append("pg-note")
    side = _rule_side(b)
    if side:
        out.append(f"pg-rule-{side}")
    if _shade_color(b):
        out.append("pg-shade")
    return " ".join(out)


def _furn_style(b: Block) -> str:
    """页边横线的**颜色/粗细**（PDF 原件的值）→ 内联 CSS 变量；没有则空串。

    只在 `typeset=True` 的渲染里用（同 `_furn_cls`）。线是伪元素画的，内联样式够不到
    伪元素，所以这里用的是**可继承的自定义属性**（`--rule-c` / `--rule-w`），
    在 `CSS` 里由 `::after`/`::before` 读。

    ⚠️ 值来自 PDF，必须校验后再拼进 HTML 属性（`#rrggbb` / 正数）——
    宁可退回主题色，也不能把 PDF 里的字符串当 CSS 用。
    """
    p = b.payload if isinstance(b.payload, dict) else {}
    parts: list[str] = []
    em = _note_em(b)
    if em:
        # 小字注区的字号是**PDF 原件的事实**（解析实测的比值）—— 用 CSS 变量传，
        # 阅读器那份 styles.css 读同一个变量（两处样式各写一份数值迟早漂开）。
        parts.append(f"--note-fs:{em:g}em")
    shade = _shade_color(b)
    if shade:
        # 底纹颜色同样是**PDF 原件的值**（`parse._margin_shades`），照抄
        parts.append(f"--shade-c:{shade}")
    rule = p.get("rule")
    if not isinstance(rule, dict) or not _rule_side(b):
        return ";".join(parts)
    color = rule.get("color")
    if isinstance(color, str) and _RE_HEX_COLOR.fullmatch(color):
        parts.append(f"--rule-c:{color}")
    try:
        width = float(rule.get("width") or 0)
    except (TypeError, ValueError):
        width = 0.0
    if 0 < width <= 8:
        parts.append(f"--rule-w:{width:g}px")
    return ";".join(parts)


def _mathy(text: str, *, typeset: bool) -> str:
    """块文本 → HTML 片段：渲染公式时走 `render_math`（自带转义），否则纯转义。"""
    if typeset:
        return render_math(text)
    return _esc(text)


# ── 划痕（决策㉛：高亮/笔记的最小单位是**任意字符区间**）───────────────────
# 颜色**不带含义**（宿主 2026-09-13：「好看的几种颜色、没有含义」），所以这里是
# 一组等价的笔色，不做"黄=重点"这类语义编码，也就不需要图例。
HL_COLORS = ("amber", "green", "blue", "pink")
DEFAULT_HL_COLOR = "amber"

# 与 `mathml.render_math` 的分段正则**必须逐字一致**：它决定"渲染后哪些字符不再是文本"。
# 不一致的后果很隐蔽 —— 前端算出的偏移会与渲染结果错位几个字符，划痕整体平移。
_MATH_RE = re.compile(r"\\\[(.+?)\\\]|\\\((.+?)\\\)", re.S)


def math_units(text: str) -> list[tuple[int, int, str]]:
    """裸文本 → `[(start, end, kind)]`，`kind` 为 `'t'`（文本）或 `'m'`（公式）。

    `'m'` 单元在渲染后是一个不透明的 MathML 子树：里面也有文本节点（`x`、`2`…），
    但那是**排版产物**，与裸文本的字符不对应。所以偏移计算里它必须是**原子**的
    （前端遇到它就跳过，靠两侧锚点定位）。
    """
    text = text or ""
    out: list[tuple[int, int, str]] = []
    pos = 0
    for m in _MATH_RE.finditer(text):
        if m.start() > pos:
            out.append((pos, m.start(), "t"))
        out.append((m.start(), m.end(), "m"))
        pos = m.end()
    if pos < len(text):
        out.append((pos, len(text), "t"))
    return out


def _clip(marks: list[dict], start: int, end: int) -> list[tuple[int, int, dict]]:
    """把划痕裁到 `[start, end)` 这个单元里，返回 `[(a, b, mark)]`（可能为空）。"""
    out = []
    for mk in marks:
        a, b = max(start, int(mk["start"])), min(end, int(mk["end"]))
        if a < b:
            out.append((a, b, mk))
    return out


def prose_html(text: str, *, typeset: bool, marks: list[dict] | None = None,
               anchors: bool = False, base: int = 0,
               sup_at: int | None = None, sup_len: int = 1) -> str:
    """正文段的内部 HTML：可带**划痕**（任意字符区间）与**偏移锚点**。

    ⚠️ `anchors` 默认 **False**，只有要交给用户去划线的视图（阅读器/分享页）才开。
    锚点是给 DOM 选区换算坐标用的**尺子**，对别处全是噪音：翻译 prompt、
    校验产物（`synth.py`）、导出文件都吃这份 HTML，多一层空 span 只会让
    产物更难 diff、让 prompt 更贵。这里与 `typeset` 同一取向 ——
    **不发锚点最多是"划不了新的一道"，发了错地方则是往 LLM 输入里掺垃圾**。

    `base` 是这段文字在**整块裸文本**里的起点偏移（默认 0 = 自成一体）。
    只有表格用它（决策㊵）：一张表的裸文本是"表注一行 + 每行 ` | ` 相连"（`model.table_text`），
    单元格是这张表里的若干片段 —— 锚点必须吐**全局**偏移，尺子才连得上；
    划痕也按全局坐标进来、在这里裁到本格。`base=0` 时行为与从前逐字节相同。

    `sup_at` / `sup_len`（默认 `None` / 1）与 `base` 同一套坐标：把**那一个字符**包成
    `<sup>`（小字注区行首的上标小标，v16，见 `parse._note_area`）。放在这里而不是由调用方
    自己拼，是因为"包在上标里"与"包在划痕里"是两件会**互相嵌套**的事 —— 调用方拼的话，
    划痕正好从行首那个字母起时就会漏掉它（`_sup_seg` 的注释）。

    ## 锚点是干什么的（改动前务必理解）

    公式在服务端被渲染成 MathML —— 于是 **DOM 里的文本与块裸文本不再一一对应**
    （`\\(x\\)` 三个字符变成了 `<math>` 里的一堆节点）。而用户划的选区只存在于 DOM 里，
    要把它变成可持久化的坐标，前端必须知道"屏幕上这个位置对应裸文本的第几个字符"。
    渲染器自己吐出的零宽锚点（`<span class="o" data-o="N">`）就是那把尺子：
    前端按文档序走一遍文本节点累加字符数、遇到锚点就把计数拨到 N、遇到 `<math>` 整个跳过。

    ⇒ **锚点不能省**，也不能只在公式两侧放：每个单元的**两端**都要有，
    否则两次公式之间的那段文本没有起点可数。

    ## 划痕如何落进 HTML

    - 文本单元按划痕边界切开，每段要么裸渲染、要么包一层 `<mark class="hl hl-<颜色>">`；
    - 公式单元是**原子**：只要有划痕与之相交，整个公式一起被包进去（宁可多包一点，
      也不能把 MathML 从中间劈开）。所以前端在解析选区时会把端点**吸附到公式边界**，
      正常路径下不会产生"半个公式"的划痕。
    """
    text = text or ""
    # `base`：划痕按**整块**坐标进来，这里换算成本片段的局部坐标（裁切照旧只认局部）
    if base:
        marks = [{**m, "start": int(m["start"]) - base, "end": int(m["end"]) - base}
                 for m in (marks or [])]
    # 上标同样按整块坐标进来（`sup_at` 缺省/越界 = 这一片段里没有要包成 `<sup>` 的字符）
    sup: int | None = None
    if sup_at is not None and 0 <= sup_at - base < len(text):
        sup = sup_at - base
    marks = sorted([m for m in (marks or []) if int(m["end"]) > int(m["start"])],
                   key=lambda m: int(m["start"]))
    if not marks and not anchors and sup is None:
        # 既没有划痕、也不要尺子、也没有上标 → 一个 span 都不吐
        # （prompt / 校验 / 导出的常态路径）。
        return _mathy(text, typeset=typeset)
    units = math_units(text)
    if not units:
        return _mathy(text, typeset=typeset)
    out: list[str] = []
    last = -1                                         # 上一个锚点的位置（去重，见下）

    def anchor(pos: int) -> None:
        """吐一个零宽锚点 —— **同一个位置只吐一次**。

        相邻单元的接缝处天然同位置（前一个单元的 `end` == 后一个单元的 `start`），
        不去重就会在每个公式两侧各多吐一个空 span。锚点本身无害，但一篇 400 个公式的
        论文会白多出几十 KB 的产物，而且"这个位置有两个锚点"对读代码的人是噪音。
        """
        nonlocal last
        if not anchors:
            return
        if pos != last:
            out.append(f'<span class="o" data-o="{base + pos}"></span>')
            last = pos

    for a, b, kind in units:
        anchor(a)
        if kind == "m":
            inner = _mathy(text[a:b], typeset=typeset)
            hit = _clip(marks, a, b)
            out.append(_wrap(inner, hit[0][2]) if hit else inner)
        else:
            pos = a
            for ca, cb, mk in _clip(marks, a, b):
                if ca > pos:
                    out.append(_sup_seg(text[pos:ca], pos, sup, sup_len))
                out.append(_wrap(_sup_seg(text[ca:cb], ca, sup, sup_len), mk))
                pos = cb
            if pos < b:
                out.append(_sup_seg(text[pos:b], pos, sup, sup_len))
        anchor(b)
    return "".join(out)


def _sup_seg(seg: str, start: int, sup: int | None, sup_len: int) -> str:
    """`seg` 的 HTML 转义；`sup`（本片段坐标，`None` = 没有）那个字符另外包一层 `<sup>`。

    ⚠️ **只加标签、不加删字符**：`_esc(seg)` 与 `_sup_seg(seg, …)` 的可见文本**逐字相同**
    —— 这是敢在小字注区（v16）里动渲染的唯一理由（`web/src/marks.ts` 的尺子按 DOM 文本
    节点逐字符累加，少一个字符这块上已有的划痕就整体平移）。

    与划痕的关系：上标是**先于**划痕决定的（`payload["markers"]` 来自解析层），所以这里
    在片段的**内部**插标签，而不是像以前那样把标记吐在片段外 —— 否则"划痕正好从行首那个
    `a` 起"时，那个字母会落在 `<mark>` 之外（颜色缺一格），坐标却还是同一套。
    """
    if sup is None:
        return _esc(seg)
    lo, hi = sup, sup + max(1, sup_len)
    i, j = max(0, lo - start), min(len(seg), hi - start)
    if i >= j:
        return _esc(seg)
    return f"{_esc(seg[:i])}<sup>{_esc(seg[i:j])}</sup>{_esc(seg[j:])}"


def _wrap(inner: str, mark: dict) -> str:
    color = str(mark.get("color") or "")
    if color not in HL_COLORS:
        color = DEFAULT_HL_COLOR          # 库里出现未知颜色时退到默认色，绝不吐坏类名
    return (f'<mark class="hl hl-{color}" data-h="{int(mark["id"])}" '
            f'tabindex="0" role="mark">{inner}</mark>')


# ── 句子切分（留给**存量数据的迁移**：㉚ 的句锚点要换算成字符区间）─────────
def split_en(text: str) -> list[str]:
    """英文切句：终止标点连同其后的引号/括号一起收尾，**后面必须跟空白**才算句末。

    「后面必须跟空白」这条很关键：`28.4`、`et al.`、`Fig. 2` 里的点后面不是空白，
    所以不会被切成两句（原型的做法，照抄）。
    """
    out: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c not in ".!?":
            i += 1
            continue
        j = i + 1
        while j < n and text[j] in ".!?\"'”’)]":
            j += 1
        if j >= n or text[j].isspace():
            out.append(text[start:j])
            start = j
            i = j
        else:
            i += 1
    if start < n:
        out.append(text[start:])
    return [s for s in out if s.strip()]


def split_zh(text: str) -> list[str]:
    """中文切句：到 `。！？；…` 为止（标点留在句尾）。"""
    parts = re.findall(r"[^。！？；…]+[。！？；…]*", text or "")
    return [s for s in parts if s.strip()]


def sentence_offsets(text: str, lang: str) -> list[tuple[int, int]]:
    """句子的字符区间 `[(start, end)]`（**只给存量数据迁移用**，见 db._migrate）。

    ㉚ 的句锚点（`b-0010:c0`）要换算成区间：句序号在这份切分里的位置就是答案。
    切分规则必须与当时写入 sid 的那一版**逐字相同**，否则换算出的区间会整体偏移 ——
    所以 `split_en` / `split_zh` 保留下来、并被测试钉住，不做"顺手优化"。
    """
    parts = split_zh(text) if lang == "zh" else split_en(text)
    out: list[tuple[int, int]] = []
    pos = 0
    for s in parts:
        i = (text or "").find(s, pos)
        if i < 0:
            break
        out.append((i, i + len(s)))
        pos = i + len(s)
    return out


def render_block(b: Block, *, lang: str, marker: bool = True, typeset: bool = False,
                 marks: list[dict] | None = None, anchors: bool = False,
                 wrap: bool = True) -> str:
    """渲染单个块。lang='en' 用 en 字段，'zh' 用 zh 字段。

    `wrap=False` 只对标题有意义：只回**内部** HTML，不带 `<h1..h4>` 外壳。
    阅读器要自己渲 `<h2 class="doc-h lvlN" data-b=…>`（挂块 id 与字号层级），
    若再塞一份服务端的 `<h2>`，CSS 之外还会得到 `<h2><h2>` —— 浏览器会把内层甩出去。

    免中文块（参考文献碎片/公式，见 `validate.NO_ZH_TYPES`）在中文视图里**回落英文原文**，
    并标 `data-nt="1"` —— 这样校验器不会把它们误判为「漏译」。
    `ref`（完整文献条目，只译标题）是**另一回事**：译出来了就要求中文那栏真有中文
    （`data-nt` 只在**没译出来**时挂 —— 否则 "这一条没译" 会被静默豁免）。

    `marks` 是**本块本语言**的划痕（`[{id, lang, start, end, color}]`，见 `prose_html`）：
    只有阅读器/分享页会传（它们要可标注），**导出与校验不传**
    （导出是给人读的静态文件，多一层 span 只会让产物更难 diff、也更难复制）。

    ⚠️ `typeset` 默认 **False**（= 保留 LaTeX 源码）。这是刻意的失败安全方向：
    漏传这个参数时，宁可是"公式显示为源码"，也绝不能是"把 MathML 喂给 LLM"
    （后者会让翻译/LaTeX 化 prompt 拿到 HTML 标签，既烧钱又必然出错）。
    **给人看的渲染点必须显式传 `typeset=True`。**
    """
    nt = b.type in NO_ZH_TYPES or (b.type == "ref" and not (b.zh or "").strip())
    # 中文视图：没有译文时**回落英文原文**，绝不渲染空白
    # （否则 dual 视图会出现整块右栏空着的洞、单语导出也会缺内容）
    text = (b.en if lang == "en" else (b.zh or b.en)) or ""
    if not typeset and lang != "en" and not (b.zh or "").strip():
        text = b.en
    # `data-pt`（**部分中文**块，见 `validate.PARTIAL_ZH_TYPES`）：它跟 `data-nt` 一样
    # 必须随 HTML 走 —— 校验器拿到的只有 HTML，没有块类型，不挂这个戳
    # 「中文栏里的数字是原件照抄的」这条豁免就永远不生效（`ref` 渲染出来是 `<p>`）。
    pt = b.type in PARTIAL_ZH_TYPES
    attr = (f' data-b="{b.id}"' + (' data-nt="1"' if nt else "")
            + (' data-pt="1"' if pt else "")) if marker else ""
    t = b.type
    mine = [m for m in (marks or []) if m.get("lang") == lang]
    # 版面装饰类（页边带 + 页边横线 + 装饰图）与线的颜色/粗细；**只给人看的渲染**挂
    # （见 `_furn_cls` / `_furn_style`）。
    furn = _furn_cls(b) if typeset else ""
    fstyle = _furn_style(b) if typeset else ""
    st = f' style="{fstyle}"' if fstyle else ""

    def elem(tag: str, base: str = "") -> str:
        """开标签：把版面装饰类与这个类型本来的类名合成**一个** `class` 属性。

        ⚠️ 必须合成而不能各写各的 —— 直接往 `attr` 前面再拼一个 `class="…"`
        会得到 `<h2 class="sec" class="pg-rule-below">`，浏览器只认第一个，
        装饰**静默失效**（这类"多写一个属性"的坑没有报错，只有肉眼看才看得出来）。
        """
        cls = " ".join(x for x in (base, furn) if x)
        return f'<{tag} class="{cls}"{attr}{st}>' if cls else f"<{tag}{attr}{st}>"

    def prose(chunk: str) -> str:
        """正文段的内部 HTML：划痕 + 偏移锚点（都在公式渲染**之前**按裸文本切好）。"""
        return prose_html(chunk, typeset=typeset, marks=mine, anchors=anchors)

    def note_body(chunk: str) -> str:
        """**小字注区**的内部 HTML：按源行渲染，行首那个上标小标包成 `<sup>`。

        与 `prose()` 的唯一区别是"分段渲染 + 上标"，所以两者在**没有注戳时完全等价**；
        `typeset=False`（翻译 prompt / 校验 / LaTeX 化）走的是同一条路径 ——
        它只是把行首那个字母包一层 `<sup>`，**字符一个不多一个不少**。
        （翻译回来的中文是 `extract_blocks` 取的**纯文本**，标签本就会被丢掉；
        渲染时按 `payload["markers"]` 重新包一遍，所以模型不需要"学会写 `<sup>`"。）

        ⚠️ 每条行之间的 `"\\n"` 必须**留在输出里**：块文本本身就是这么存的
        （`parse._NoteArea.text`），而 `web/src/marks.ts` 的坐标尺子按 DOM 文本节点
        逐字符累加 —— 少一个换行符，这一块上已有的划痕就整体平移一格。
        """
        lines = chunk.split("\n")
        tpl = _note_markers(b)
        out: list[str] = []
        base = 0                                       # 本行在**整块裸文本**里的起点
        for i, ln in enumerate(lines):
            mk = tpl[i] if i < len(tpl) else ""
            # 行首那个小标**留在原文里**，只让渲染器把**这一个字符**包成 `<sup>`
            # （`sup_at` 那一套 —— 与划痕的嵌套由 `prose_html` 内部处理，见其 docstring）
            sup_at = base if (mk and ln.startswith(mk)) else None
            out.append(prose_html(ln, typeset=typeset, marks=mine, anchors=anchors,
                                  base=base, sup_at=sup_at, sup_len=len(mk)))
            base += len(ln)
            if i < len(lines) - 1:
                out.append("\n")
                base += 1
        return "".join(out)

    if t in ("h1", "h2", "h3", "h4"):
        tag = {"h1": "h1", "h2": "h2", "h3": "h3", "h4": "h4"}[t]
        cls = "sec" if t == "h2" else ("sub" if t == "h3" else "")
        # ⚠️ 标题也是**可选中的文字**，所以它同样必须走 `prose()`（2026-09-16 宿主：
        # 「标题行，选中后没有笔记工具的弹出 mark-bar」）。原先这里是 `_esc(text)`，
        # 于是标题既没有零宽锚点（前端量不出字符坐标）、也没有 `<mark>`（划痕渲染不出来）
        # —— 表现是"选中标题什么都不会发生"，看起来像前端坏了，其实是**渲染器少给了一把尺子**。
        # `typeset=False` 时 `prose()` 就是 `_esc()`，导出/校验产物逐字不变。
        body = prose(text)
        if wrap:
            return f"{elem(tag, cls)}{body}</{tag}>"
        # `wrap=False`：`<h1..h4>` 外壳由阅读器出，这里只回内部 HTML。
        # 但**页边横线不能就这么丢掉**（㊳ 的教训：渲染器少给一点，前端就整条路不可用）
        # —— 用一个 `<span>` 兜住（`<h2>` 里只允许短语内容，`div` 会被浏览器甩出去），
        # `span.pg-rule-*` 在 CSS 里是 `display:block`，线照样横跨一行。
        if furn:
            return f'<span class="{furn}"{st}>{body}</span>'
        return body
    if t == "abstract":
        return f"{elem('div', 'abstract')}{prose(text)}</div>"
    if t == "deco":
        # 页边装饰图（v11：出版社/期刊标识）。**没有文字**：不参与翻译也不参与校验
        # （`validate.NO_ZH_TYPES`），所以这里只吐一张按原件尺寸摆好的图。
        # ⚠️ `data-b` 标记照旧要吐（校验器按标记比对 EN/ZH 两份产物，缺一个标记
        # 就会被判成"块丢失"）—— 所以没有 src 时也照样吐这个空壳。
        src = b.payload.get("src", "")
        size = ""
        try:
            w, h = float(b.payload.get("w") or 0), float(b.payload.get("h") or 0)
            # 显示尺寸是**给人看的**（导出/阅读器用），与其余装饰一样只挂在 `typeset=True`
            # 那一份里；机器面（校验/翻译 prompt）只需要 `data-b` 这个身份标记。
            if typeset and w > 0 and h > 0:
                size = f' style="width:{w:g}px;height:{h:g}px"'
        except (TypeError, ValueError):
            size = ""
        img = f'<img src="{_esc(src)}"{size} alt="">' if src else ""
        return f"{elem('div')}{img}</div>"
    if t == "figure":
        src = b.payload.get("src", "")
        # 图注也走翻译通道：中文视图用译文，未译时回落英文原文（图片本体由 payload.src 渲染）
        cap = (text or "").strip() or b.payload.get("caption", "")
        img = f'<img src="{_esc(src)}" alt="">' if src else ""
        cap_html = f"<figcaption>{_mathy(cap, typeset=typeset)}</figcaption>" if cap else ""
        return f"{elem('figure')}{img}{cap_html}</figure>"
    if t == "table":
        rows = table_cells(b, lang)
        if not rows:
            # 网格缺失（历史数据 / 别的生产者只填了 `en`）→ 按段落渲染，
            # 绝不吐一张空表壳（空表在页面上是"什么都没有"，比看到原文更糟）。
            return f"{elem('p')}{prose(text)}</p>"
        cap = table_caption(b, lang)
        # ⚠️ **表格的每一格也要在划痕的坐标系里**（2026-09-16 决策㊵ —— 宿主：
        # 「表格也需要支持选中后出mark-bar」）。表格的裸文本是"表注一行 + 每行以
        # ` | ` 相连"（`model.table_text`），格子只是这块裸文本里的若干片段，
        # 所以每格的锚点必须吐**整块**偏移（`prose_html(base=…)`），划痕也按整块坐标进来。
        # 少了这一步，选中格子里的字时前端量不出坐标 → **浮条根本不出现**
        # （与 ㊳「选中标题没有 mark-bar」是同一个坑：渲染器少给了一把尺子）。
        # 起点由 `model.table_layout` 与裸文本**同一份拼接代码**算出，不另数一遍分隔符。
        _, offsets = table_layout(rows, cap)
        cap_html = ""
        if str(cap).strip():
            cap_html = ("<caption>"
                        + prose_html(str(cap), typeset=typeset, marks=mine, anchors=anchors)
                        + "</caption>")
        body = []
        for i, row in enumerate(rows):
            cell = "th" if i == 0 else "td"
            inner = "".join(
                f"<{cell}>{prose_html(str(c), typeset=typeset, marks=mine, anchors=anchors, base=offsets[i][j][0])}</{cell}>"
                for j, c in enumerate(row)
            )
            body.append(f"<tr>{inner}</tr>")
        return f"{elem('table', 'datatable')}{cap_html}{''.join(body)}</table>"
    if t in ("refs", "ref"):
        # 同标题：参考文献条目也是可选中的文字（宿主常在上面标"这篇要读"），
        # 走 `prose()` 才有锚点与 `<mark>`；`typeset=False` 时输出与 `_esc()` 逐字相同。
        # `refs` = 尚未切出条目的碎片块，`ref` = 一条完整文献（只译标题，见 `model.BlockType`）。
        return f"{elem('p', 'ref-item')}{prose(text)}</p>"
    if t == "eq":
        latex = b.payload.get("latex", "") or text
        # eq 块的 payload 是**裸 LaTeX**（无定界符）→ 必须走 render_math_block
        if typeset:
            inner = render_math_block(latex, str(b.payload.get("number") or ""))
            return f"{elem('div', 'eq')}{inner}</div>"
        # 校验用：保留源码，编号以 \tag{} 形式挂在公式末尾
        num = b.payload.get("number")
        tag = f" \\tag{{{num}}}" if num else ""
        return f"{elem('div', 'eq')}\\[ {_esc(latex)}{tag} \\]</div>"
    # 正文段（`p` 与其它落不到分支的类型）。小字注区（v16）走**按源行 + 上标**那条路。
    return f"{elem('p')}{(note_body(text) if _note_em(b) else prose(text))}</p>"


def render_fragment(blocks: list[Block], *, lang: str, marker: bool = True,
                    typeset: bool = False,
                    marks: dict[str, list[dict]] | None = None,
                    anchors: bool = False) -> str:
    """`marks`：块 ID → 该块的划痕（按语言在 `render_block` 里再筛一次）。"""
    marks = marks or {}
    return "\n".join(render_block(b, lang=lang, marker=marker, typeset=typeset,
                                  marks=marks.get(b.id), anchors=anchors) for b in blocks)


def document_html(
    blocks: list[Block],
    *,
    lang: str,
    title: str = "",
    subtitle: str = "",
    meta_line: str = "",
    marker: bool = True,
    lang_attr: str = "en",
    typeset: bool = True,          # 给人看的入口 → 默认渲染公式
) -> str:
    """合成单文件仿期刊 HTML（单栏流式，决策⑥）。

    `typeset=False` → 保留 LaTeX 源码，供 `validate.py` 比对（见模块文档）。
    """
    mast = ""
    if title or subtitle or meta_line:
        mast = (
            '<div class="masthead">'
            + (f"<h1>{_esc(title)}</h1>" if title else "")
            + (f'<div class="title-en">{_esc(subtitle)}</div>' if subtitle else "")
            + (f'<div class="meta">{_esc(meta_line)}</div>' if meta_line else "")
            + "</div>"
        )
    return f"""<!DOCTYPE html>
<html lang="{lang_attr}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title or 'papershelf')}</title>
<style>{CSS}{mathml_css()}</style>
</head>
<body>
<div class="page">
{mast}
{render_fragment(blocks, lang=lang, marker=marker, typeset=typeset)}
</div>
</body>
</html>
"""


def en_html(doc: Doc, *, marker: bool = True, typeset: bool = True) -> str:
    meta = doc.meta
    return document_html(
        doc.blocks,
        lang="en",
        title=meta.get("title_en", ""),
        subtitle=meta.get("title_zh", ""),
        meta_line=meta.get("authors", ""),
        marker=marker,
        lang_attr="en",
        typeset=typeset,
    )
