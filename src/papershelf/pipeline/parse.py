"""解析：PDF → 块级 Doc。

沿用宿主已验证管线的做法，并内建其踩过的坑的加固（见 docs/design.md §5.2）：
- 图片用 **image block 的 bbox**（不用 get_image_rects()，双栏时其 bbox 全从 y=58 起不可靠）
- 双栏页按「左栏 y 序 → 右栏 y 序」重排
- 页眉/页脚按「短文本 + 位于页边 + 跨页重复」剔除
- 图注按「图片块之后的文本块且以 Fig./Figure/表/Table 开头」配对
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from .model import Doc, make_block_id

# 解析版本号 —— **改动解析产物形状时必须手动 +1**。
#
# 为什么需要它（踩过类）：`doc_cache` 的主键是 **PDF 内容 sha256**，PDF 没变则永远命中
# 缓存。于是「解析器改了（本次：每个块新增 `payload.page`），旧库却仍读旧解析产物」——
# 分页容器永远不出现，而代码看上去完全正确。把版本号混进 fingerprint，
# 解析语义一变即自动失效重建（代价是那次重跑要重做 LaTeX 化与翻译）。
#
# v3（2026-09-14）：阅读顺序从「整页二选一（单栏/双栏）」改为**分区**处理
# （通栏段 + 窄块段 + 栏间空白剖面），修掉**混合版式**下两栏逐行交错
# （生产 paper 12 首页正文首句错位即此例）。
#
# v4（2026-09-14）：v3 在纸上成立但**实际仍有大量页面交错**（实测 12 篇语料 265 页，
# 逐页比对发现 62 页仍在两栏间来回跳，宿主看到的"格式乱了"就是这些页）。三处修正：
#   ① **页眉/页脚不再参与分段**（`_marginal`）：它们的宽度不按"栏"来
#      （`x 252.6–544.3` 只有 49% 页宽，因为页码占了左侧），会被当成"窄块"留在分栏段里
#      把 gutter 打钉子 → 分栏失败。判据改用**贴页边 + 横跨本页实测 gutter**。
#   ② `_gutter` **迭代剔除跨栏块**（XY-cut 经典做法）：一张通栏图 + 一行通栏页眉就够
#      让最小覆盖度超过上限（`_MAX_BRIDGE_RATIO` 是**比例**，块少时塌成常数 1，
#      而通栏块数不随之变少）→ 探测放弃。**页面越稀疏越容易中招**。
#   ③ gutter 取**同分点里最靠页中**的那个：栏间空白必在中间，取最左同分点会落进
#      左栏内部（B7-01 第 24 页：`[208, 252]` 整段覆盖度都是 1，208 是左栏内缘）。
# 另：一侧只有 1 个块时，若它**纵向占满这段**（`_coverage ≥ 0.5`）也算一整栏
# （B7-01 第 8 页右栏整段被 PDF 抽成 1 个块）。
# 实测：栏跳变好 **62 页**、变差 **0 页**、持平 203 页（265 页语料逐页比对）。
# 版本历史（混入缓存指纹：语义一变，旧解析产物自动失效、下次转换重建）
#   v4 → 分栏阅读顺序（gutter 探测 + 迭代剔通栏块 + 覆盖度兜底；实测好 62 页 / 差 0 页）
#   v5 → 抽取文本**确定性清洗**（连字 ﬁ/ﬂ → fi/fl、`\xa0` → 空格，见 `clean_text`）
PARSE_VERSION = 5

# 页面上下边缘（比例），落在其中的短文本块视为页眉/页脚候选
EDGE_TOP, EDGE_BOTTOM = 0.075, 0.925
MIN_REPEAT_RATIO = 0.3   # 跨页重复比例超过此值 → 判为页眉/页脚
CAPTION_PREFIXES = ("fig", "figure", "table", "tab.", "表", "图")

# ── 阅读顺序（分区 + 栏间空白）──────────────────────────────────────────────
# 跨栏块判定：宽度超过「页面宽度 × 该比例」的块视为通栏（标题、摘要、大表、跨栏图）。
_SPAN_RATIO = 0.62
# 找 gutter（栏间空白）只在页面中央这段找 —— 栏间空白必在中间，不必全页扫。
_GUTTER_CENTER = (0.35, 0.65)
# 找 gutter 只看正文带：页眉/页脚/页码常落在栏间，会把 gutter 打钉子。
_GUTTER_EDGE_TOP, _GUTTER_EDGE_BOTTOM = 0.09, 0.91
# 允许横跨 gutter 的块占比上限（公式溢出、跨栏小图）；超过就认为"这不是双栏"。
_MAX_BRIDGE_RATIO = 0.06
# 分栏后每栏至少这么多块，否则不分（防止把单栏页的缩进项切坏）。
_MIN_COL_BLOCKS = 2
# 版面边角带（比例）：落在距页顶/页底这条带内的块**可能是**页眉/页脚/页码/水印。
# 它只用来判断"该不该参与分段/分栏"（见 `_marginal`），不决定取舍。
_MARGIN_BAND = 0.05

# 章节编号模式（IEEE/学术常见）：顶层「I. / II.」，次级「A. / B.」，深层「1.1 / 2.3.1」
_RE_H2 = re.compile(r"^([IVX]{1,6})\.\s+\S")
_RE_H3 = re.compile(r"^([A-Z])\.\s+\S")
_RE_H4 = re.compile(r"^\d+(?:\.\d+){1,3}\.?\s+\S")
# 无编号但具名的顶层章节
_RE_H2_NAMED = re.compile(r"^(REFERENCES|REFERENCES\b|ACKNOWLEDGMENT|ACKNOWLEDGEMENT|APPENDIX|CONCLUSION)\b", re.I)
# 小型大写字母（small caps）在 PDF 提取时会插入伪空格：I NTRODUCTION → INTRODUCTION
_RE_SMALLCAPS = re.compile(r"\b([A-Z]) ([A-Z]{2,})")
# 纯装饰/标记块（如期刊 logo 里的单字母）
_RE_JUNK = re.compile(r"^[\W_]{0,2}$")
# IEEE Xplore 的水印页脚（版权声明），非论文内容
_RE_WATERMARK = re.compile(
    r"^(Authorized licensed use limited to|Downloaded on .{0,60}from IEEE Xplore|"
    r"Restrictions apply|\d{4}\s+IEEE\.\s*Personal use is permitted)", re.I
)
# 参考文献条目特征：卷期页码 / 会议 / 年份
_RE_REF_ENTRY = re.compile(r"(\bpp\.|\bvol\.|\bProc\.|IEEE Trans|\bdoi:|arXiv:|,\s*(?:19|20)\d{2}\b)")
_RE_REF_START = re.compile(r"^\s*\[\d{1,3}\]")
REFS_TAIL_FRACTION = 0.35   # 参考文献只可能出现在文末这段区间之后
MIN_REFS_RUN = 8            # 文末连续命中该数目的块才算参考文献段



def _spans(block: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in block.get("lines", []):
        out.extend(line.get("spans", []))
    return out


# PDF 里 `ﬁ` 是**一个字形**（U+FB01），不是 `f`+`i` 两个字符 —— 展开是纯字符串替换、
# 没有任何歧义，所以**不该交给模型**（实测：不处理时模型只挑它注意到的几处，残留一半）。
# 同类的还有不换行空格 `\xa0`（会让"标题里带一个看不见的空格"，影响检索与排版）。
_LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
    "\u2010": "-", "\u2011": "-", "\u00ad": "",   # 连字符变体 / 软连字符（直接删）
}
_SPACES = {"\xa0": " ", "\u2009": " ", "\u202f": " ", "\u200b": ""}


def clean_text(text: str) -> str:
    """抽取文本的**确定性**清洗：连字展开 + 特殊空格归一（不做需要判断的事）。

    「行末断词该不该合并」（`vari- ous` → `various`，但 `three- dimensional` → `three-dimensional`）
    要靠看图才能定，那是 ①c 视觉校对的活；这里只做**换掉字符**这种没有歧义的部分。
    """
    if not text:
        return text
    for src, dst in _LIGATURES.items():
        if src in text:
            text = text.replace(src, dst)
    for src, dst in _SPACES.items():
        if src in text:
            text = text.replace(src, dst)
    return text


def _block_text(block: dict[str, Any]) -> str:
    return clean_text(" ".join(s.get("text", "") for s in _spans(block)).strip())


def _max_size(block: dict[str, Any]) -> float:
    sizes = [s.get("size", 0.0) for s in _spans(block) if s.get("text", "").strip()]
    return max(sizes) if sizes else 0.0


def _is_bold(block: dict[str, Any]) -> bool:
    fonts = [s.get("font", "") for s in _spans(block) if s.get("text", "").strip()]
    return bool(fonts) and all(("Bold" in f) or ("black" in f.lower()) for f in fonts)


def _body_size(raw_pages: list[list[dict[str, Any]]]) -> float:
    """正文基准字号 = 按**字符数加权**的中位数（不是按块数的中位数）。

    ## 为什么必须加权（2026-09-14 生产实测）

    原实现取「每块最大字号」的中位数。问题不在中位数，在**权重**：
    页眉/页脚/页码/脚注（8.5pt）块数多但每块很短，正文（10pt）块数少但每块很长。
    实测 B3-01：8.5pt 有 **359 块 / 58.5k 字符**、10pt 有 124 块 / **85k 字符** ——
    按块数取中位数得 8.5，按字符数得 10.0。

    基准取错 1.5pt 的后果不是"稍微不准"，而是**`body_size + 1.0` 那条兜底规则
    把正文段落整段判成 h2 标题**（10.0 ≥ 8.5 + 1.0），而 `size ≥ body_size * 1.8`
    又再也认不出真正的文章标题。实测同一份 PDF：8.5 → 37 个"标题"（含 6 个整段正文），
    10.0 → 30 个（全部正确）。**标题误判会污染大纲、`section` 归属与阅读器导航。**
    """
    counts: Counter[float] = Counter()
    for blocks in raw_pages:
        for b in blocks:
            if b.get("type") != 0:
                continue
            size = _max_size(b)
            text = _block_text(b)
            if size and text:
                counts[round(size, 1)] += len(text)      # 权重 = 字符数
    if not counts:
        return 10.0
    total = sum(counts.values())
    acc = 0
    for size in sorted(counts):
        acc += counts[size]
        if acc >= total / 2:
            return size
    return 10.0


def _by_y(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))


def _edge_band(block: dict[str, Any], height: float) -> str | None:
    """块贴在**页顶带**还是**页底带**：返回 `"top"` / `"bottom"` / `None`。

    ⚠️ 判据是块**起始**落在顶带内、或块**结束**落在底带内 —— **不是"整块都在带内"**。
    实测教训（2026-09-14，这条判据第一版写错过）：期刊页眉高约 10pt 而从 `y≈32` 起，
    上边距带高 39pt，于是 `y1 = 41 > 39` → 旧判据**全部返回 False**，
    「摘掉页眉页脚再分栏」这个修复**静默不生效**（页面顺序看起来毫无变化）。
    贴边是根据"贴"这个动作定义的：块的**一条边**贴住页面边缘即可。
    """
    if block["bbox"][1] <= height * _MARGIN_BAND:
        return "top"
    if block["bbox"][3] >= height * (1 - _MARGIN_BAND):
        return "bottom"
    return None


def _marginal(block: dict[str, Any], width: float, height: float,
              gutter: float | None = None) -> bool:
    """是不是**版面边角**块（页码/期刊页眉/出版社页脚/水印），不属于正文流。

    判据故意取得很窄（必须 "贴页顶/页底" **且** "横向不是一栏正文的宽度"）：
    - 块起于页顶带或终于页底带（`_edge_band`），**且**
    - 横向满足三者之一：**极短**（页码/水印）／**横跨这一页的栏间空白**
      （`gutter`，期刊页眉那种通栏一行）／**几乎占满页宽**（单栏页的页眉页脚）。

    ⚠️ **"横跨 gutter"必须用页面实测的 gutter，不能用"宽 > 70% 页宽"代替**：
    实测 B7-01 第 2 页页眉是 `x 252.6–544.3`（仅 49% 宽）—— 因为页码 `3116`
    占了页眉左侧那一小段，页眉块被切成"从中间开始"的一行，既不算极短也不算通栏，
    于是**漏网**、留在 col 段里把 gutter 打钉子 → 分栏失败、两栏逐行交错。

    横向这条同时是**分栏页的护栏**：两栏页里每栏正文宽约 40% 页面宽，
    既不极短也不跨 gutter，于是顶部/底部的段落不会被误摘出栏流。

    ## 为什么需要它（2026-09-14 实测，v3 的缺陷）

    v3 按「宽度 > 62% 页面宽」区分通栏块与窄块，再对窄块段分栏。但页面**顶部**的
    通栏页眉（`1052 The International Journal of …`）与**底部**的宽页脚
    （`Vol.:(0123456789)`）会**夹在分栏段之间**，把整页切成交错的段：
    实测第 1 页输出成「左栏首段 → 页脚 → 期刊页眉 → 左栏续段 → … → 右栏」，
    也就是左栏读到一半跳到右栏又跳回来（宿主看到的"格式乱了"）。

    页眉页脚不是正文，本就不该参与分栏或段落流；把它们从**版面分段**里摘掉，
    顺序里不再出现"中间被边缘块打断"，正文两栏就能连续地一栏读到底。

    ⚠️ 只影响**排列顺序**，不影响块的取舍：真正的过滤仍由 `_running_headers`
    （跨页重复的短文本）与 `_RE_WATERMARK` 负责，这里不删任何内容。
    """
    if _edge_band(block, height) is None:
        return False
    x0, x1 = block["bbox"][0], block["bbox"][2]
    if (x1 - x0) < width * 0.35 or (x1 - x0) > width * 0.7:
        return True
    return gutter is not None and x0 < gutter < x1


def _columns(blocks: list[dict[str, Any]], gutter: float | None
             ) -> list[list[dict[str, Any]]] | None:
    """按栏间空白 `gutter` 把 blocks 切成左右两栏；`gutter is None` 则切不出来。

    `gutter` 由 `_reading_order` 用 `_gutter` 算一次并复用（本页口径统一，
    也避免每个窄块段各算一次、算出不同的 gutter）。
    """
    if gutter is None:
        return None
    cols: list[list[dict[str, Any]]] = [[], []]
    for b in blocks:
        cx = (b["bbox"][0] + b["bbox"][2]) / 2
        cols[0 if cx < gutter else 1].append(b)
    return [_by_y(c) for c in cols]


def _gutter(blocks: list[dict[str, Any]], width: float) -> float | None:
    """找**栏间空白**(gutter) 的 x 坐标；这组块不像分栏就返回 None。

    用**覆盖度剖面**，不是「相邻块间隙 ≥ N」那种邻接扫描 —— 后者会被**溢出栏间的
    公式碎片**骗过：B5-01 第 7 页右栏从 x=300.9 起、而左栏一道公式的 `≤ 0`
    一直伸到 299.2，真正的空白只剩 1.7pt，任何像样的间隙阈值都扫不出来
    （实测：那页 55 个块被当成"一栏"）。剖面则处处成立：真栏内任意 x 都被大量块
    横跨，只有 gutter 接近 0。

    ⚠️ 调用方传进来的块**应当只在正文带里**（页眉/页脚/页码常常正落在两栏之间的
    空白里，拿它们算剖面等于给 gutter 打钉子 —— B2-03 第 24 页的页码 `186` 就卡在
    栏间）。而**通栏块由本函数自己迭代剔除**（见下），不必调用方预先筛窄块。
    """
    if len(blocks) < 2:
        return None

    def profile(bs: list[dict[str, Any]]) -> tuple[float, int]:
        """覆盖度最小的 x。**同分时取最靠页中的那个** —— 栏间空白必在中间，
        取最左边的同分点会落在空白的左边缘（甚至左栏内缘）上。
        实测 B7-01 第 24 页：左栏右边缘 291.7、页眉右边缘 544.3，`[208, 252]`
        整段覆盖度都是 1（只有左栏块横跨），取最左同分点得 208.3 —— 那是**左栏内部**，
        按它切栏会把左栏劈开。
        """
        lo, hi = width * _GUTTER_CENTER[0], width * _GUTTER_CENTER[1]
        best_x, best_cov = lo, None
        x = lo
        while x <= hi:
            cov = sum(1 for b in bs if b["bbox"][0] < x < b["bbox"][2])
            if best_cov is None or cov < best_cov:
                best_x, best_cov = x, cov
            elif cov == best_cov and abs(x - width / 2) < abs(best_x - width / 2):
                best_x = x
            x += 0.5
        return best_x, (best_cov if best_cov is not None else len(bs))

    best_x, best_cov = profile(blocks)
    # 「容许横跨的块数」在块少时至少给 1（公式溢出/跨栏小图本来就要容忍）。
    allowed = max(1, int(len(blocks) * _MAX_BRIDGE_RATIO))
    #
    # ⚠️ **迭代剔除跨栏块**（XY-cut 的经典做法，实测必需）：
    # 通栏大图、跨栏一行（页眉/摘要/长图注）会横跨 gutter，把"覆盖度"顶到上限 →
    # 探测直接放弃 → 退回按 y 排 → 两栏逐行交错。实测 B7-01 第 30 页：
    # 一张通栏图 + 一行通栏页眉就够让最小覆盖度 = 2（上限恰好是 1），
    # 而页面上真正需要分栏的只有 8 个正文块。**页面越稀疏越容易中招** ——
    # 因为 `_MAX_BRIDGE_RATIO` 是比例，块少时它塌成常数 1，而通栏块数并不随之变少。
    # 所以：把"横跨当前 gutter 的块"摘掉再重算，直到不再超标。
    while best_cov > allowed:
        crossers = [b for b in blocks if b["bbox"][0] < best_x < b["bbox"][2]]
        blocks = [b for b in blocks if b not in crossers]
        if len(blocks) < 2:
            return None
        best_x, best_cov = profile(blocks)
        allowed = max(1, int(len(blocks) * _MAX_BRIDGE_RATIO))

    left = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 < best_x]
    right = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 >= best_x]
    if not left or not right:
        return None
    if min(len(left), len(right)) < _MIN_COL_BLOCKS:
        # 一侧块数少：只有它**纵向差不多与另一栏齐平**才算「一整栏文字被 PDF 并成一块」。
        # 实测 B7-01 第 8 页：图下的右栏整段被抽成 **1 个块**（高 188pt / 左栏高 239pt），
        # 只按块数判定就会得出"这不是两栏"，于是退回按 y 排 → 左栏读一段跳右栏再跳回。
        # ⚠️ 同一侧**块数 > 1** 时不设门槛：两栏各自成段时"哪侧块少"本就随机
        #    （实测 4:1 的页面存在），按比例卡会把正常页判成不是分栏。
        sparse, dense = (left, right) if len(left) < len(right) else (right, left)
        if _extent(sparse) < _extent(dense) * _COL_SPAN_RATIO:
            return None
    return best_x


# 一侧块数少时，它**纵向与另一栏齐平**到多少才算「一整栏文字被 PDF 并成一块」。
# ⚠️ 量的是**两侧各自覆盖的纵向长度之比**，不是"占本组块跨度的比例"：
#    后者会被页眉/页码这类贴着页边的块把跨度撑到整页高、把比例稀释掉
#    （实测 B7-01 第 8 页：188pt / 703pt = 0.27 → 判据永远不成立）。
# ⚠️ 也不能要求"完全齐平"：段落最后一个字块常常明显短于另一栏的末段
#    （实测该页 188pt vs 239pt = 0.79，而这不是"又一个块"，就是整栏文字）。
#    0.5 与"两栏各自成段"的常见失衡（4:1 之类，比值仍接近 1）拉得开。
_COL_SPAN_RATIO = 0.5


def _extent(blocks: list[dict[str, Any]]) -> float:
    """这组块**纵向覆盖的总长度**（区间并集；重叠部分只算一次）。"""
    spans = sorted((b["bbox"][1], b["bbox"][3]) for b in blocks)
    total, cur0, cur1 = 0.0, None, None
    for a, b in spans:
        if cur1 is None or a > cur1:
            if cur1 is not None:
                total += cur1 - cur0
            cur0, cur1 = a, b
        else:
            cur1 = max(cur1, b)
    if cur1 is not None:
        total += cur1 - cur0
    return total


def _reading_order(
    blocks: list[dict[str, Any]], width: float, height: float
) -> list[dict[str, Any]]:
    """按阅读顺序排序：**分区**处理 —— 通栏块自成一段，窄块段内再分栏。

    旧实现只有「整页单栏」和「整页双栏」两种模式，且双栏与否靠 `_two_column`
    （左右各 ≥4 个窄块）整页猜一次。**混合版式猜错就静默按 y 排 → 两栏逐行交错**
    （生产 paper 12 首页即此例，见 `PARSE_VERSION` 注释）。

    这里按 y 把块切成「通栏段 / 窄块段」，各自处理：
    通栏段按 y 排；窄块段尝试分栏（成功则左栏整栏 → 右栏整栏，失败退回按 y）。
    混合版式下封面信息（通栏）因此落在其下的双栏正文之前，不会再错位。

    ⚠️ **版面边角块（页眉/页脚/页码/水印）不参与分段**，只按 y 归位到页首或页尾
    （`_marginal`）。它们夹在正文段之间会把整页切成交错的段（实测第 1 页输出
    「左栏首段 → 页脚 → 页眉 → 左栏续段 → 右栏」）。正文两栏因此能连续读到底。
    """
    if not blocks:
        return []
    # 先找本页的 gutter：只在**正文带**里看（页眉/页脚/页码常正落在栏间，
    # 拿它们算剖面等于给 gutter 打钉子）。**通栏块由 `_gutter` 内部迭代剔除** ——
    # 所以这里不必预先筛窄块，只把页边带摘掉即可。这个 gutter 随后既用来判
    # "哪些边缘块其实是通栏页眉"，也用来分栏，两处口径一致。
    core = [b for b in blocks
            if b["bbox"][3] > height * _MARGIN_BAND
            and b["bbox"][1] < height * (1 - _MARGIN_BAND)]
    gutter = _gutter(core, width)
    body = [b for b in blocks if not _marginal(b, width, height, gutter)]
    runs: list[tuple[str, list[dict[str, Any]]]] = []
    for b in _by_y(body):
        cls = "full" if (b["bbox"][2] - b["bbox"][0]) > width * _SPAN_RATIO else "col"
        if runs and runs[-1][0] == cls:
            runs[-1][1].append(b)
        else:
            runs.append((cls, [b]))

    out: list[dict[str, Any]] = []
    for cls, group in runs:
        if cls == "full":
            out.extend(_by_y(group))
            continue
        cols = _columns(group, gutter)
        if cols:
            for col in cols:
                out.extend(col)
        else:
            out.extend(_by_y(group))

    # 边角块归位：贴页顶的（页眉/页码在上）排到最前，贴页底的收到最后
    # （它们不属于正文流，但也不能被丢掉 —— 期刊页眉与脚注是阅读器里可见的内容）。
    # ⚠️ 贴页顶的按 y **倒序**逐个 insert(0) —— 连续 insert(0) 会把顺序反过来，
    #    倒序插才能得到正序（期刊页眉在页码上方时，页眉先出现）。
    edge = [x for x in blocks if _marginal(x, width, height, gutter)]
    for b in [x for x in _by_y(edge) if _edge_band(x, height) == "top"][::-1]:
        out.insert(0, b)
    out.extend([x for x in _by_y(edge) if _edge_band(x, height) != "top"])
    return out


def _running_headers(pages: list[list[dict[str, Any]]], heights: list[float]) -> set[str]:
    """跨页重复出现在页边短文本 → 页眉/页脚。"""
    counter: Counter[str] = Counter()
    for blocks, h in zip(pages, heights):
        for b in blocks:
            text = _block_text(b)
            if not text or len(text) > 120:
                continue
            y0, y1 = b["bbox"][1], b["bbox"][3]
            if y1 < h * EDGE_TOP or y0 > h * EDGE_BOTTOM:
                counter[text] += 1
    threshold = max(2, int(len(pages) * MIN_REPEAT_RATIO))
    return {t for t, n in counter.items() if n >= threshold}


def _caption_like(text: str) -> bool:
    low = text.lower().lstrip().rstrip(".:")
    return any(low.startswith(p) for p in CAPTION_PREFIXES)


def _fonts(block: dict[str, Any]) -> set[str]:
    return {s.get("font", "") for s in _spans(block) if s.get("text", "").strip()}


_RE_HYPHEN_PSEUDO_SPACE = re.compile(r"\s+-\s*")

# ── 展示公式的「碎片行」合并 ────────────────────────────────────────────────
# PDF 会把一个矩阵/多行公式抽成**逐行独立文本块**（`⎡`、`⎢⎢⎢⎢⎣`、单行公式、`,` 各成一块）。
# 碎片各自送 LLM 只会拼出空矩阵（实测 b-0066 = `\begin{bmatrix}\end{bmatrix}`），
# 所以按「连续数学行」合并成一个块，再交给 LaTeX 化。
_MATH_GLYPH_RE = re.compile(r"[⎡⎢⎣⎤⎥⎦┌┐└┘│⌈⌉⌊⌋|]")
_LATEX_CMD_STRIP_RE = re.compile(r"\\[A-Za-z]+\*?")
_MAX_MATH_LINE_LEN = 220


def _is_math_line(text: str) -> bool:
    """该块是不是「没有散文的数学行」——剥掉 LaTeX 命令与符号后，不剩 ≥3 字母的词。"""
    t = (text or "").strip()
    if not t or len(t) > _MAX_MATH_LINE_LEN:
        return False
    stripped = _LATEX_CMD_STRIP_RE.sub(" ", t)
    words = [w for w in re.findall(r"[A-Za-z]+", stripped) if len(w) >= 3]
    return not words


def merge_math_runs(blocks: list[Any]) -> list[Any]:
    """把连续的「数学行」碎块合并为一块（保留首块的 ID，其余块移除）。

    仅当一段连续数学行**含数学信号**（括号字形 / LaTeX 命令 / 等号）且总长足够时才合并，
    避免把孤立的 `,` 或数字碎片误并。
    """
    out: list[Any] = []
    i, n = 0, len(blocks)
    merged = 0
    while i < n:
        b = blocks[i]
        if b.type == "p" and _is_math_line(b.en):
            j = i
            while j < n and blocks[j].type == "p" and _is_math_line(blocks[j].en):
                j += 1
            run = blocks[i:j]
            body = "\n".join(x.en.strip() for x in run)
            has_signal = bool(_MATH_GLYPH_RE.search(body) or "\\" in body or "=" in body)
            if len(run) >= 2 and has_signal and len(body) >= 4:
                head = run[0]
                head.en = body
                head.payload["merged_from"] = [x.id for x in run[1:]]
                out.append(head)
                merged += 1
                i = j
                continue
        out.append(b)
        i += 1
    if merged:
        seen: dict[str, int] = {}
        for k, b in enumerate(out, start=1):
            seen[b.id] = k              # ID 保持不变（稳定优先于连续）
        return out
    return blocks


def _desmallcaps(text: str) -> str:
    """修复小型大写字母在文本提取时产生的伪空格（仅用于标题）。

    两种情况：`S AFETY` → `SAFETY`，以及 smallcaps 连字符被拆成 `SAFETY -CRITICAL`。
    """
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _RE_SMALLCAPS.sub(r"\1\2", out)
    out = _RE_HYPHEN_PSEUDO_SPACE.sub("-", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def _is_decoration(span: dict[str, Any], body_size: float) -> bool:
    """装饰字（期刊 logo / 页标）：字号极大但内容极短（如 29.9pt 的「I」）。"""
    txt = span.get("text", "").strip()
    return bool(txt) and len(txt) <= 3 and span.get("size", 0.0) >= body_size * 2


def _text_without_logo(block: dict[str, Any], body_size: float) -> str:
    keep = [
        s.get("text", "")
        for s in _spans(block)
        if s.get("text", "").strip() and not _is_decoration(s, body_size)
    ]
    return clean_text(" ".join(keep).strip())


def _size_without_logo(block: dict[str, Any], body_size: float) -> float:
    sizes = [
        s.get("size", 0.0)
        for s in _spans(block)
        if s.get("text", "").strip() and not _is_decoration(s, body_size)
    ]
    return max(sizes) if sizes else 0.0


def _heading_level(block: dict[str, Any], body_size: float) -> int | None:
    """判定标题层级；返回 1–4，或 None（不是标题）。

    IEEE 论文的章节标题常与正文**同字号**，只能靠字体与编号模式识别：
    顶层 `I.` + Helvetica；次级 `A.` + Helvetica-Oblique。
    """
    text = _text_without_logo(block, body_size)
    if not text or len(text) > 160:
        return None
    size = _size_without_logo(block, body_size)
    fonts = _fonts(block)
    oblique = any("Oblique" in f or "Italic" in f for f in fonts)

    if size >= body_size * 1.8:
        return 1                                   # 文章标题
    if _RE_H2.match(text) and not oblique:
        return 2
    if _RE_H3.match(text) and oblique:
        return 3
    if _RE_H4.match(text):
        return 4
    if _RE_H2_NAMED.match(text) and len(text) < 60:
        return 2
    if size >= body_size + 1.0 and len(text) < 90:
        return 2
    return None



def _pair_captions_by_geometry(
    images: list[dict[str, Any]], texts: list[dict[str, Any]]
) -> tuple[dict[int, str], set[int]]:
    """按几何就近把图注文本配给图片（优先图片下方，其次上方）。

    跨栏大图（bbox 横跨两栏）在阅读序里会把图注甩到很远，顺序法不可靠；
    宿主既有管线的经验也是「用坐标交叉验证」，此处同理。
    """
    result: dict[int, str] = {}
    used: set[int] = set()
    for img in images:
        iy0, iy1 = img["bbox"][1], img["bbox"][3]
        best: tuple[float, dict[str, Any]] | None = None
        for tb in texts:
            if id(tb) in used:
                continue
            text = " ".join(_block_text(tb).split())
            if not _caption_like(text) or len(text) > 500:
                continue
            ty0, ty1 = tb["bbox"][1], tb["bbox"][3]
            if ty0 >= iy1:                       # 图片下方
                dist = ty0 - iy1
            elif ty1 <= iy0:                     # 图片上方（部分排版图注在前）
                dist = (iy0 - ty1) + 8
            else:
                continue                         # 纵向重叠，不视为图注
            if dist > 90:
                continue
            if best is None or dist < best[0]:
                best = (dist, tb)
        if best is not None:
            used.add(id(best[1]))
            result[id(img)] = " ".join(_block_text(best[1]).split())
    return result, used


def _finalize(doc: Doc) -> Doc:
    """收尾清理：合并标题行、抽出标题到 meta、剔除无图注的 logo/头像、重编块 ID。"""
    blocks = doc.blocks

    # 1) 合并被 PDF 拆成多行的 h1（标题）
    merged: list[Any] = []
    for b in blocks:
        if merged and b.type == "h1" and merged[-1].type == "h1":
            merged[-1].en = (merged[-1].en + " " + b.en).strip()
            continue
        merged.append(b)

    # 2) 标题移入 meta（由页眉渲染，避免正文重复）
    #
    # ⚠️ 这里曾经把**真标题弄丢**（2026-09-12 修）：原先取 `titles[0]` 当标题、
    #    然后 `merged = [b for b in merged if b.type != "h1"]` 把**所有** h1 全删掉。
    #    但封面页常有两个 h1：期刊名（页眉大字号）在前、**论文真标题在后**
    #    （实测 12 篇里 2 篇如此，第 1、3 篇）。于是"取第一个"取到了期刊名，
    #    "删全部"把真标题销毁 —— `Precision Engineering` 成了标题，
    #    而真标题 `Sensor-integrated data acquisition and ...` 从此在库里不存在。
    #    后来接上 LLM 抽取元数据（⑲）也没救：真标题已经不在块里，模型只能编。
    #
    #    修法：**先把所有候选记进 meta**（`title_candidates`），再删块。
    #    这样"取第一个"仍是既有的保守默认（保证零回归），而 LLM 能在候选里挑对的。
    #    块本身照旧删除 —— 它们已被捕获进 meta，不是丢失，且留在正文会让标题在
    #    阅读器里重复出现一次。
    titles = [b for b in merged if b.type == "h1"]
    if titles:
        cands = [t.en.strip() for t in titles if (t.en or "").strip()]
        if cands:
            doc.meta["title_candidates"] = cands
            doc.meta["title_en"] = cands[0]
    merged = [b for b in merged if b.type != "h1"]

    # 3a) 回溯配对：部分排版把图注放在图片**之前**（B5-01 的 Fig. 2 即如此）
    claimed: set[int] = set()
    for i, b in enumerate(merged):
        if b.type != "figure" or (b.payload.get("caption") or "").strip():
            continue
        for j in range(i - 1, max(-1, i - 5), -1):
            cand = merged[j]
            if j in claimed or cand.type != "p":
                continue
            if _caption_like(cand.en) and len(cand.en) < 500:
                b.payload["caption"] = " ".join(cand.en.split())
                claimed.add(j)
            break

    # 3b) 剔除仍无图注的图片（期刊 logo、作者头像）——记录在 meta 中便于追溯
    dropped: list[dict[str, Any]] = []
    kept: list[Any] = []
    for i, b in enumerate(merged):
        if i in claimed:
            continue
        if b.type == "figure" and not (b.payload.get("caption") or "").strip():
            dropped.append({"src": b.payload.get("src", ""), "page": b.payload.get("page")})
            continue
        kept.append(b)
    doc.meta["dropped_images"] = dropped
    doc.meta["dropped_image_count"] = len(dropped)

    # 3c) 识别**文末材料**（参考文献 + 作者简介）：从「REFERENCES 小标题」起直到文末。
    #     意义有三层：
    #     (a) 参考文献按 prompt 规则保持英文，本就不该送模型翻；
    #     (b) 省 token —— 这篇论文的参考文献占正文 26%；
    #     (c) 关键：若标出来，它们会被「必须含中文」的校验判成漏译 → 陷入永不收敛的重译。
    #     同时把 REFERENCES 小标题本身提为 h2（PDF 里它是无编号标题，原判定漏掉了）。
    ref_start = None
    for i in range(len(kept) - 1, int(len(kept) * REFS_TAIL_FRACTION) - 1, -1):
        b = kept[i]
        if b.type not in ("p", "h2"):
            continue
        flat = re.sub(r"\s+", "", b.en).upper()
        if flat.startswith(("REFERENCES", "BIBLIOGRAPHY")) and len(flat) < 40:
            ref_start = i
            break
    if ref_start is None:                       # 标题丢了就退回「[1] 条目起点」判据
        for i in range(len(kept) - 1, int(len(kept) * REFS_TAIL_FRACTION) - 1, -1):
            if kept[i].type == "p" and _RE_REF_START.match(kept[i].en):
                ref_start = i
                break
    if ref_start is not None:
        head = kept[ref_start]
        head.en = _desmallcaps(head.en)          # "R EFERENCES" → "REFERENCES"
        head.type, head.section, head.level = "h2", "REFERENCES", 2
        for b in kept[ref_start + 1:]:
            if b.type in ("p", "refs"):
                b.type = "refs"
        doc.meta["refs_start"] = head.id
        doc.meta["refs_count"] = sum(1 for b in kept[ref_start + 1:] if b.type == "refs")

    # 3d) 图注并入 en 字段：图注也是正文内容，必须走「标记穿透」翻译通道，
    #     否则中文版会留下英文图注（且会被校验器判成漏译）。图片本体另行由 payload.src 渲染。
    for b in kept:
        if b.type == "figure" and (b.payload.get("caption") or "").strip():
            b.en = " ".join(b.payload["caption"].split())

    # 3e) 合并展示公式的碎片行（PDF 逐行抽块的产物）—— 必须在重编 ID 之前
    kept = merge_math_runs(kept)

    # 4) 重编块 ID，保证从 b-0001 连续
    for i, b in enumerate(kept, start=1):
        b.id = make_block_id(i)
    doc.blocks = kept
    doc.meta.setdefault("block_count", len(kept))
    return doc


def _paged_adder(doc: Doc, page_no: int):
    """返回一个「加块即盖页码戳」的 `doc.add` 替身（见 `parse_pdf` 里的用法）。

    只把 `page_no` 定格在创建时（循环变量若被闭包捕获，全书页码都会变成最后一页）；
    `section` 由解析循环**在调用时**显式传入 —— 它在同一页内会随小标题变化，
    提前定格会把标题之后的段落全挂到页首章节上。
    """
    def add(type_: str, text: str = "", *, section: str = "", **kw: Any) -> Any:
        blk = doc.add(type_, text, section=section, **kw)
        blk.payload["page"] = page_no
        return blk
    return add


def parse_pdf(
    pdf_path: str | Path,
    assets_dir: str | Path | None = None,
    title_hint: str | None = None,
) -> Doc:
    """解析 PDF → Doc（英文块，带稳定 ID）。"""
    pdf_path = Path(pdf_path)
    out_dir = Path(assets_dir) if assets_dir else pdf_path.parent / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)

    src = fitz.open(pdf_path)
    raw_pages: list[list[dict[str, Any]]] = []
    heights: list[float] = []
    widths: list[float] = []

    for page in src:
        rect = page.rect
        raw_pages.append(page.get_text("dict")["blocks"])
        heights.append(rect.height)
        widths.append(rect.width)

    headers = _running_headers(raw_pages, heights)

    doc = Doc(meta={"source": pdf_path.name, "pages": len(raw_pages)})
    if title_hint:
        doc.meta["title_en"] = title_hint

    # 正文字号（用于标题判定）—— 按字符数加权的中位数，见 `_body_size` 的实测说明
    body_size = _body_size(raw_pages)

    section = ""
    pending_figure = None
    fig_index = 0

    for page_no, blocks in enumerate(raw_pages, start=1):
        width = widths[page_no - 1]
        text_blocks = [b for b in blocks if b.get("type") == 0 and _block_text(b)]
        text_blocks = [b for b in text_blocks if _block_text(b) not in headers]
        image_blocks = [b for b in blocks if b.get("type") == 1]

        # 每个块都**记下自己来自 PDF 第几页**（`payload.page`）——阅读器/导出据此重建
        # 「PDF 分页容器」（原型 `.pdf-page` + 「第 N 页 / 共 M 页」页脚）。
        # 解析循环本来就按页遍历（`enumerate(raw_pages, 1)`），此前只把页码写进了
        # figure 块，于是分页做不到；现在统一由这个包装器盖戳，避免漏写。
        # ⚠️ 页码是**版面事实**，不是渲染选项：合并块（merge_math_runs）沿用首块页码，
        #    跨页合并时页码会略有偏差（同一段公式被 PDF 拆到两页的极少数情形）。
        add = _paged_adder(doc, page_no)

        ordered = _reading_order(text_blocks + image_blocks, width, heights[page_no - 1])
        captions, caption_blocks = _pair_captions_by_geometry(image_blocks, text_blocks)

        for b in ordered:
            if id(b) in caption_blocks:                  # 已配作图注的文本块，不再单独成段
                continue
            if b.get("type") == 1:                       # ── 图片
                fig_index += 1
                name = ""
                raw_bytes = b.get("image")
                if raw_bytes:
                    ext = (b.get("ext") or "png").lower()
                    name = f"p{page_no}_img{fig_index}.{ext}"
                    try:
                        (out_dir / name).write_bytes(raw_bytes)
                        w_, h_ = b.get("width"), b.get("height")
                        doc.assets.append({
                            "name": name, "width": w_, "height": h_,
                            # 宽高比：前端在图片**加载前**用它占位。否则 `loading="lazy"`
                            # 的图要滚到才尺寸突变，整篇高度上浮数千像素 —— 实测让
                            # 「大纲跳转」落点偏掉约 7500px（跳完还在第 13 页半空中）。
                            "ratio": (round(w_ / h_, 4) if w_ and h_ else None),
                            "page": page_no,
                        })
                    except Exception:
                        name = ""
                pending_figure = add(
                    "figure", "", section=section,
                    payload={
                        "src": f"assets/{name}" if name else "",
                        "caption": captions.get(id(b), ""),
                        "bbox": [round(v, 1) for v in b["bbox"]],
                    },
                )
                continue

            text = _block_text(b)

            # 图注：紧随图片块之后、以 Fig./Table 开头 → 挂到该图
            if pending_figure is not None and _caption_like(text):
                if not pending_figure.payload.get("caption"):
                    pending_figure.payload["caption"] = " ".join(text.split())
                pending_figure = None
                continue

            if _RE_WATERMARK.match(text):
                continue                                  # 出版社水印页脚（非论文内容）
            if _RE_JUNK.match(text) and _size_without_logo(b, body_size) > body_size * 2:
                continue                                  # 期刊 logo / 装饰单字

            level = _heading_level(b, body_size)
            clean_src = _text_without_logo(b, body_size)
            if level == 1:
                add("h1", _desmallcaps(clean_src), section=section, level=1)
                continue
            if level is not None:
                clean = _desmallcaps(clean_src)
                section = clean
                add(f"h{level}", clean, section=section, level=level)
                continue

            add("p", " ".join(text.split()), section=section)

    src.close()
    return _finalize(doc)
