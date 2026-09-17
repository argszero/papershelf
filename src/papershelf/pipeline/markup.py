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
from .validate import NO_ZH_TYPES

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
.pg-rule-below::after,.pg-rule-above::before{content:"";display:block;border-top:1px solid var(--rule)}
.pg-rule-below::after{margin:6px 0 16px}
.pg-rule-above::before{margin:16px 0 6px}
/* 标题的 `<h1..h4>` 外壳由阅读器出（`wrap=False`），服务端只能回一个 `<span>` 兜住装饰
   （`<h2>` 里塞 `div` 会被浏览器甩出去）→ 这里把它扶正成块级，线才横跨得起来。 */
span.pg-rule-below,span.pg-rule-above{display:block}
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
# ⚠️ 页边带的字号/颜色只作用于 `p`/`refs`（`_BAND_TYPES`）：标题有自己的字号层级，
#    万一有个真标题落在页顶带里，也不该被压成灰字。横线则与块类型无关。
_BAND_TYPES = ("p", "refs")


def _furn_cls(b: Block) -> str:
    """块的**版面装饰** CSS 类（`pg-band pg-top` / `pg-rule-below` …）；没有则空串。

    ⚠️ 只在**给人看的渲染**（`typeset=True`）里挂（`render_block` 里判）：
    校验/翻译/LaTeX 化 prompt 走的是 `typeset=False`，产物必须逐字不变
    （它们要的是"块的文本"，多一层 class 属性只会让 prompt 变脏、让 diff 失真）。
    """
    p = b.payload if isinstance(b.payload, dict) else {}
    out: list[str] = []
    band = p.get("band")
    if band in ("top", "bottom") and b.type in _BAND_TYPES:
        out.append(f"pg-band pg-{band}")
    rule = p.get("rule")
    if rule in ("below", "above"):
        out.append(f"pg-rule-{rule}")
    return " ".join(out)


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
               anchors: bool = False, base: int = 0) -> str:
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
    marks = sorted([m for m in (marks or []) if int(m["end"]) > int(m["start"])],
                   key=lambda m: int(m["start"]))
    if not marks and not anchors:
        # 既没有划痕、也不要尺子 → 一个 span 都不吐（prompt / 校验 / 导出的常态路径）。
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
                    out.append(_esc(text[pos:ca]))
                out.append(_wrap(_esc(text[ca:cb]), mk))
                pos = cb
            if pos < b:
                out.append(_esc(text[pos:b]))
        anchor(b)
    return "".join(out)


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

    免中文块（参考文献/公式，见 `validate.NO_ZH_TYPES`）在中文视图里**回落英文原文**，
    并标 `data-nt="1"` —— 这样校验器不会把它们误判为「漏译」。

    `marks` 是**本块本语言**的划痕（`[{id, lang, start, end, color}]`，见 `prose_html`）：
    只有阅读器/分享页会传（它们要可标注），**导出与校验不传**
    （导出是给人读的静态文件，多一层 span 只会让产物更难 diff、也更难复制）。

    ⚠️ `typeset` 默认 **False**（= 保留 LaTeX 源码）。这是刻意的失败安全方向：
    漏传这个参数时，宁可是"公式显示为源码"，也绝不能是"把 MathML 喂给 LLM"
    （后者会让翻译/LaTeX 化 prompt 拿到 HTML 标签，既烧钱又必然出错）。
    **给人看的渲染点必须显式传 `typeset=True`。**
    """
    nt = b.type in NO_ZH_TYPES
    # 中文视图：没有译文时**回落英文原文**，绝不渲染空白
    # （否则 dual 视图会出现整块右栏空着的洞、单语导出也会缺内容）
    text = (b.en if lang == "en" else (b.zh or b.en)) or ""
    if not typeset and lang != "en" and not (b.zh or "").strip():
        text = b.en
    attr = f' data-b="{b.id}"' + (' data-nt="1"' if nt else "") if marker else ""
    t = b.type
    mine = [m for m in (marks or []) if m.get("lang") == lang]
    # 版面装饰类（页边带 + 页边横线）；**只给人看的渲染**挂（见 `_furn_cls`）。
    furn = _furn_cls(b) if typeset else ""

    def elem(tag: str, base: str = "") -> str:
        """开标签：把版面装饰类与这个类型本来的类名合成**一个** `class` 属性。

        ⚠️ 必须合成而不能各写各的 —— 直接往 `attr` 前面再拼一个 `class="…"`
        会得到 `<h2 class="sec" class="pg-rule-below">`，浏览器只认第一个，
        装饰**静默失效**（这类"多写一个属性"的坑没有报错，只有肉眼看才看得出来）。
        """
        cls = " ".join(x for x in (base, furn) if x)
        return f'<{tag} class="{cls}"{attr}>' if cls else f"<{tag}{attr}>"

    def prose(chunk: str) -> str:
        """正文段的内部 HTML：划痕 + 偏移锚点（都在公式渲染**之前**按裸文本切好）。"""
        return prose_html(chunk, typeset=typeset, marks=mine, anchors=anchors)

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
            return f'<span class="{furn}">{body}</span>'
        return body
    if t == "abstract":
        return f"{elem('div', 'abstract')}{prose(text)}</div>"
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
    if t == "refs":
        # 同标题：参考文献条目也是可选中的文字（宿主常在上面标"这篇要读"），
        # 走 `prose()` 才有锚点与 `<mark>`；`typeset=False` 时输出与 `_esc()` 逐字相同。
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
    return f"{elem('p')}{prose(text)}</p>"


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
