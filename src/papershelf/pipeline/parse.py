"""解析：PDF → 块级 Doc。

沿用宿主已验证管线的做法，并内建其踩过的坑的加固（见 docs/design.md §5.2）：
- 图片用 **image block 的 bbox**（不用 get_image_rects()，双栏时其 bbox 全从 y=58 起不可靠）
- 双栏页按「左栏 y 序 → 右栏 y 序」重排
- 页眉/页脚按「短文本 + 位于页边 + 跨页重复」剔除
- 图注按「图片块之后的文本块且以 Fig./Figure/表/Table 开头」配对
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple

import fitz  # PyMuPDF

from .model import Block, Doc, make_block_id

log = logging.getLogger("papershelf.pipeline.parse")

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
#   v6 → 「加粗小标题 + 正文」被并成一块的**行级拆分**（见 `_split_runin_heads`）：
#        此前 `Abstract` / `Keywords` 这类具名小标题**根本不存在于产物里**
#   v7 → 给**栏间被切开的续段**盖 `payload["seam"]` 戳（见 `_column_spill_seams`）：
#        只是**标记**、不自动合并 —— "该不该并"要看页图（①c 校对 agent 的活）
#   v8 → 行内拆分出的两块**归一 y 到源行**（见 `_sub_block`）：v6 拆 `Keywords`
#        时两块各自带 span 的 bbox，y0 差 ~1pt → 阅读顺序把关键词列表排到 `Keywords`
#        标题**之前**（宿主 2026-09-17 实测截图）
#   v9 → 页眉/页脚带里的**白色（不可见）文字**不入产物（`_drop_invisible_spans`）：
#        Springer 首页那行纯白 `Vol.:(0123456789)` 曾被当成正文段落渲染、还要白送翻译
#   v10 → ①**不再有意丢弃页眉/页脚文字**（`_running_headers` 整个撤掉：跨页重复就删，
#         删掉的是"和 pdf 一致"的版面内容 —— 宿主 2026-09-17：「页眉文字不需要有意丢掉。
#         和 pdf 尽量保持一致」）；②盖 `payload["band"]`（页边带）与 `payload["rule"]`
#         （**页边横线的位置**，见 `_rule_marks`）—— 矢量线条此前一条都没进产物，
#         每页页眉下那条通栏细线整份文档都不见（宿主：「这里少了一条水平线」）
#   v11 → ①页边横线**照抄 PDF 的粗细与颜色**（v10 只存了"哪一侧"，渲染端硬编码成
#         `1px solid var(--border)` 的浅灰 —— PDF 原件是纯黑 0.99pt，宿主看到的是
#         「线还是没有」；`payload["rule"]` 因此从字符串变成 `{side,color,width}`）；
#         ②页边带里的**矢量图形**（出版社/期刊标识）取自 `get_drawings()` 并渲染成
#         透明 PNG，作为 `deco` 块进产物（见 `_margin_graphics`）—— 这类标识既不是文字、
#         也不是图片，**从来没进过产物**（宿主：「Springer 的图还是没有」）；
#         ③**无图注的图片不再丢弃**（原先整批删掉"期刊 logo、作者头像"，实测把真图
#         也误删 —— 图注压在图片边缘上时配不上注，图与图注一起消失）；
#         ④图注配对允许**小幅纵向重叠**（`_CAP_*`），并改成"全局最近优先、一对一认领"。
#   v12 → ①**整页旋转 90° 的页面转正**（见 `_page_rotation` / `normalize_rotated_pages`）：
#         这类页面的文字是旋转着画上去的（页面 `/Rotate` 却是 0），而整条链路都假设
#         "文字是水平的" —— 表格的**列**在页面坐标里是竖排，按 y 排序 / 按 x 找栏间空白
#         于是把列当成行：同一行里相邻的格子被粘成一句、Ref 列的值跑到表头**之前**
#         （宿主 2026-09-17 实测截图：一张竖向表格提取后格式全错）。转正是**坐标
#         变换**，不是识别 —— 转正后它和一张普通横排表格完全一样；
#         ②这类页面**不参与分栏**（表格格间的大空隙会被 `_gutter` 当成栏间空白，
#         行序被劈成两半）：见 `_reading_order(columns=False)`；
#         ③修掉**表注被静默丢弃**（转正后暴露出来的独立缺陷）：图注已由几何配对
#         认领时，那个老式 `pending_figure` 没被复位，于是**下一页**以 "Table N" 起头
#         的表注撞进"给上一个图当图注"的分支，而那个图已有图注 → 整行被 `continue` 丢掉
#         （实测 8 页稿 `Table 2 …` 在任何块里都不存在，①c 只能如实报"表注在抽取中丢失"）。
#   v13 → **参考文献碎片合并成整条条目**（`merge_ref_entries`，宿主 2026-09-17：
#         「参考文献没有翻译」→ 决策「只译文献标题」）。原先文末材料只按"从 REFERENCES
#         起标 `refs`"处理，条目被 PyMuPDF 的块检测切成一堆 ~200 字符的碎片
#         （切口常落在**词中间**，一块里还常塞着下一条的前半截）：既没法译标题，
#         读者看到的也不是"一条文献"。现在切出**整条** → 新块类型 `ref`（只译标题）。
#   v14 → **修掉参考文献只合并出 2 条**（宿主 2026-09-19：「生产环境，References
#         只翻译了两条，剩下的既没有翻译，也无法重译」）：`_ref_entry_starts` 原先把
#         连号的起点钉死在 1，而页眉/页脚（`payload["band"]`）会把参考文献流截成好几段
#         ⇒ **只有含 `[1]`/`[2]` 的第一段合并成功**，其余 286 条（每片都带着连号 `[N]`）
#         一段都切不出来。改为取**最长的那一串连号**（起点不限），见 `_ref_entry_starts`。
PARSE_VERSION = 14

# 页面上下边缘（比例），落在其中的短文本块视为页眉/页脚候选
EDGE_TOP, EDGE_BOTTOM = 0.075, 0.925
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

# ── 页边横线（期刊页眉下那条通栏细线）───────────────────────────────────────
# 见 `_rule_marks`：PyMuPDF 的 `get_text()` 只回文字，**矢量线条一条都不在产物里**。
_RULE_BAND = 0.14           # 只在距页顶/页底这一比例内找页边横线
_RULE_MAX_THICKNESS = 2.0   # 线的粗细上限（pt）：比这"厚"的是方框/色块，不是规则线
_RULE_MAX_GAP = 26.0        # 线离那行文字多远之内才算"这行文字的线"（pt）
_RULE_EDGE_SLACK = 0.06     # 线两端各允许离正文文字列边缘的比例（超过就不是通栏线）

# ── 页边矢量图形（v11：出版社/期刊标识）─────────────────────────────────────
# 「Springer 的马标 + 字标」这类标识在 PDF 里**不是图片，是画出来的矢量填充**：
# 实测马头 84 个 items + "Springer" 字标 146 个 items，两块拼成 42.1×11.0pt 的一个整体，
# 每页页脚一个、左右交替。于是它既不在 `get_text()` 里（那是文字层），也不在 image
# block 里（那是图片层）—— **从来没进过产物**，不是"渲染丢了"（与 v10 那条页眉横线
# 同一个道理：先问"取过没有"，再问"画出来没有"）。
#
# 判据宁可漏画、不许把正文截成图（截错会凭空多出一张"正文截图"）：
_FIG_BAND = 0.14             # 只在距页顶/页底这一比例内找（与 `_RULE_BAND` 同一把尺）
_FIG_MIN_SIZE = 4.0          # 小于这个尺寸的当噪声丢掉（pt）
_FIG_MAX_HEIGHT = 40.0       # 高的不算"标识"（正文里的大插图自有图片块管）
_FIG_MAX_WIDTH_RATIO = 0.40  # 宽度达到正文列宽这个比例的不算标识（通栏色带/装饰横带）
_FIG_CLUSTER_GAP = 6.0       # 聚类：横向间隙上限（pt）—— 马头与字标之间就是这点距离
_FIG_CLUSTER_DY = 20.0       # 聚类：纵向相差上限（pt）
_FIG_MAX_PER_PAGE = 4        # 每页最多几个（判据万一走偏也不至于满页都是图）
_FIG_RENDER_SCALE = 8.0      # 渲染倍率（PDF pt → PNG px）：显示宽度约 60px ⇒ 约 6 倍像素密度
_FIG_TOL = 0.6               # 与文字/图片块的重叠容差（pt）

# ── 页边色块/底纹（v11：`CRITICAL REVIEW` 后面那条浅灰底纹）──────────────────
# 见 `_margin_shades`：判据是「色块**包着可见文字**」—— 反过来，包着**不可见白字**的
# 色块（Springer 页脚 `Vol.:(0123456789)` 的**黑框**：白字 v9 已不入产物）必须排除，
# 否则页脚会凭空多出两个黑方块。色块本身没有文字，所以它挂在**被它衬底的那个文字块**上。
_SHADE_MIN_WIDTH = 20.0      # 窄于这个的不是底纹（是图标的一部分）
_SHADE_MIN_HEIGHT = 4.0
_SHADE_MAX_HEIGHT = 60.0     # 高于这个的是整块色板，不是一行字的底纹
_SHADE_COVER = 0.60          # 文字有这么多比例落在色块里才算"衬底"
_SHADE_MAX_SPREAD = 3.0      # 色块宽度上限 = 文字宽 × 这个倍数 + `_SHADE_PAD_ALLOW`（pt）
_SHADE_PAD_ALLOW = 40.0

# PDF pt → 阅读器 px：PDF 正文基准 10pt ↔ 阅读器 `--doc-fs` 14.5px（`web/src/styles.css`）。
# 横线的粗细、图形的显示尺寸都按这个比例折算 —— 它们要和**读者看到的正文**成比例，
# 而不是和"96dpi 的物理尺寸"成比例（阅读器的正文列比 PDF 的物理宽度略宽）。
_PT_TO_PX = 1.45

# ── 图注配对（v11 修正）──────────────────────────────────────────────────────
# 旧判据「图注与图片 bbox 纵向重叠 ⇒ 不是图注」会把**真图连图注一起丢掉**：
# Springer 的图注常压在图片上边缘（实测 Fig. 5：图 y=297–716、图注 y=295.1–317.9，
# 重叠 20.9pt），于是配不上注 → 3b 判定"无图注的图" → 整张图被删。
_CAP_MAX_GAP = 90.0          # 图注离图片多远之内还算它的图注（pt）
_CAP_OVERLAP = 24.0          # 允许的纵向重叠上限（pt）—— 版面把图注压在图片边缘上
_CAP_OVERLAP_RATIO = 0.25    # 或"重叠不超过图高的这个比例"（大图允许压得更多）
_CAP_SIDE_PENALTY = 30.0     # 与图片**并排**（横向不重叠）的图注：可能是邻栏的话，代价更高

# ── 整页旋转（v12）────────────────────────────────────────────────────────────
# 有些期刊把**横排的大表格**印在竖版页面上：整页文字（含表格）是**旋转 90° 画上去的**，
# 而页面 `/Rotate` 仍是 0 —— 所以 PyMuPDF 的 `page.rotation`、`page.rect` 都看不出异常，
# 只有 span 级的 `line["dir"]` 说真话（`(0,-1)`：文字沿 y 轴自下而上）。
# 判据只看**字符数加权**的纵向比例，且要求纵向字符数够多（免得一两行竖排标注
# 把整页判成旋转页 —— 那个代价是把正常页面转 90°）。
_ROT_VERT_RATIO = 0.6        # 纵向字符占全页字符的比例超过它 ⇒ 判为旋转页
_ROT_MIN_CHARS = 200         # 纵向字符数下限（噪声门槛）

# 章节编号模式（IEEE/学术常见）：顶层「I. / II.」，次级「A. / B.」，深层「1.1 / 2.3.1」
_RE_H2 = re.compile(r"^([IVX]{1,6})\.\s+\S")
_RE_H3 = re.compile(r"^([A-Z])\.\s+\S")
_RE_H4 = re.compile(r"^\d+(?:\.\d+){1,3}\.?\s+\S")
# 无编号但具名的顶层章节（含 `Abstract` / `Keywords` —— 它们与编号章节同级）
_RE_H2_NAMED = re.compile(
    r"^(ABSTRACT|REFERENCES|BIBLIOGRAPHY|ACKNOWLEDGMENTS?|ACKNOWLEDGEMENTS?|"
    r"APPENDIX|CONCLUSIONS?|KEY\s?WORDS|摘要|关键词)\b", re.I)

# ── 「具名小标题 + 正文」被并成一块的拆分（见 `_split_runin_heads`）─────────────
# 期刊常把 `Abstract` / `Keywords` 排成**加粗行内标题**，PyMuPDF 的块聚合会把标题行与
# 紧随其后的正文并成**一个块** → 标题在产物里根本不存在（详见 `_split_runin_heads`）。
# 这里列出这些「整行文本就等于标题词」的名字（归一化后比对，含中文排版）。
_NAMED_HEADS = frozenset({
    "abstract", "keywords", "key words", "references", "bibliography",
    "acknowledgment", "acknowledgments", "acknowledgement", "acknowledgements",
    "appendix", "conclusion", "conclusions", "摘要", "关键词",
})
# 标题文本长度上限：超过就不可能是标题（宁可漏拆，也不能把正文劈碎）
_MAX_RUNIN_HEAD_CHARS = 60
# 拆出来之后剩下的正文至少这么长 —— 否则只是把一个碎片分成两个碎片
_MIN_RUNIN_BODY_CHARS = 25
_RE_BOLD_FONT = re.compile(r"bold|black|heavy|semibold", re.I)
# 句末标点（**不含冒号** —— `Abstract:` 是常见排法，要能拆）
_RE_TERMINAL_PUNCT = re.compile(r"[.,;!?，。；！？]$")
# 标题里不该出现的东西：人名/单位行才会有的逗号与分隔点
_RE_NOT_HEAD_LIKE = re.compile(r"[,;·、]|\bet\s+al\b")
# 编号前缀（**只剥编号、不要求后半像标题** —— 后半的判据见 `_looks_like_numbered_head`）
_RE_HEAD_NUM = re.compile(r"^(?:[IVX]{1,6}\.|[A-Z]\.|\d+(?:\.\d+){1,3}\.?)\s+")
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

# ── 参考文献条目起点（v13，见 `split_ref_entries`）──────────────────────────
# 两种主流编号：IEEE 的 `[12]`，以及 Springer/Vancouver 的 `12. `。
# ⚠️ `12. ` 这条**必须带"后面是大写字母"的判据**：光看 `\d{1,3}\.\s` 会把 DOI 的碎片
# 全抓进来（实测这篇的 `10. 3390/ met14 020195`、`11. 1117/1. Oe.` 全中）——而
# DOI 编号后面跟的是数字。`(?<![\w.,])` 则挡住长数字串的**尾巴**
# （`2053- 1591` 不许从中间切出一个 `1591.`）。
_RE_REF_BRACKET = re.compile(r"\[(\d{1,3})\]\s+")
_RE_REF_NUM = re.compile(r"(?<![\w.,])(\d{1,3})\.\s+(?=[A-Z\u00c0-\u024f\"“'‘])")
# 合并出的单条条目**上限**：超过它说明编号判据中途失效（后面的条目全被吞进最后一条），
# 宁可放弃合并也不产出一块两千字的"条目"。
_REF_MAX_ENTRY = 2000


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

    ⚠️ 只影响**排列顺序**，不影响块的取舍：页眉/页脚文字**照旧留在产物里**
    （v10 起 `_running_headers` 那道过滤已整个撤掉，见模块头的版本历史；宿主
    2026-09-17：「页眉文字不需要有意丢掉。和 pdf 尽量保持一致」）。这里唯一还删的
    是出版社水印（`_RE_WATERMARK`）与期刊 logo（`_RE_JUNK`），两条都与页眉无关。
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


def _page_gutter(blocks: list[dict[str, Any]], width: float, height: float) -> float | None:
    """本页的栏间空白（只在**正文带**里量）—— 单一来源，避免两处各量一把尺子。

    调用方：`_reading_order`（排序）与 `_column_spill_seams`（找栏间续段）。
    两者必须用**同一个** gutter，否则"按它排的序"和"按它标的缝"会错位。
    """
    core = [b for b in blocks
            if b["bbox"][3] > height * _MARGIN_BAND
            and b["bbox"][1] < height * (1 - _MARGIN_BAND)]
    return _gutter(core, width)


def _reading_order(
    blocks: list[dict[str, Any]], width: float, height: float, *,
    columns: bool = True,
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

    ## `columns=False`（v12：转正后的旋转页）

    这类页面整页是一张**横排表格**，格与格之间天然有大段空白 —— `_gutter` 会把
    「Sensor type 列右缘 / ML 列左缘」那条缝当成**栏间空白**，于是阅读顺序变成
    "每行的左半 → 每行的右半"，**行序被劈成两半**（实测：第 5 页前 8 块全是右半张表
    的单元格，左半张表排到了后面）。表格只有一种正确的顺序：**行序**。
    所以这里退化成纯 `_by_y`（边角块归位那条照旧保留）。
    """
    if not blocks:
        return []
    # 先找本页的 gutter：只在**正文带**里看（页眉/页脚/页码常正落在栏间，
    # 拿它们算剖面等于给 gutter 打钉子）。**通栏块由 `_gutter` 内部迭代剔除** ——
    # 所以这里不必预先筛窄块，只把页边带摘掉即可。这个 gutter 随后既用来判
    # "哪些边缘块其实是通栏页眉"，也用来分栏，两处口径一致。
    gutter = _page_gutter(blocks, width, height)
    body = [b for b in blocks if not _marginal(b, width, height, gutter)]
    runs: list[tuple[str, list[dict[str, Any]]]] = []
    for b in _by_y(body):
        full = (not columns) or (b["bbox"][2] - b["bbox"][0]) > width * _SPAN_RATIO
        cls = "full" if full else "col"
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


# ── 「栏间续段」：一句话被两栏的切缝劈成两个块 ────────────────────────────────
# 宿主 2026-09-15 实测报（paper 1 第 22 页）：左栏末块 `… and RL; however, each of`
# 与右栏首块 `the above sections does not talk about…` 是**同一句话**，却被抽成两个块 ——
# 译文于是也断成两半（两半各自翻译，中文读起来中间那句没头没尾）。
#
# ⚠️ 这里**只标记、不自动合并**（宿主选 B）：判据的两条都只是"疑似"，
#    "该不该并"要看页图（①c 校对 agent 的活）。标记放在块的 `payload["seam"]`，
#    agent 在 `read_blocks` 里能看到，合并用 `merge_block`（它本来就能并入页内上一块）。
#
# 判据必须**几何 + 文字两条同时成立**（缺一条就被噪声淹掉，实测）：
#   * 只用文字规则（前块无句末标点 + 后块小写起）：37 页报 **73** 处 ——
#     大半是**表格行**（第 18 页整张大表逐行中招）和页眉/水印夹在中间；
#   * 只用几何规则（左右栏接缝）：37 页报 **20** 处，但"左栏正常收句、右栏另起一段"
#     也落在同一条缝上，会被误标。
_RE_SENT_END = re.compile(r"""[.!?:;)\]"'\u201d\u2019\u3002\uff01\uff1f]\s*$""")
_RE_STARTS_CONT = re.compile(r"^[a-z(]")


def _looks_like_continuation(tail_prev: str, head_next: str) -> bool:
    """两块**像不像同一句话被切开**（判据的**单一来源**）。

    解析阶段（`_column_spill_seams` 盖 `payload["seam"]`）与校对阶段
    （`merge_block` 合并后的复量提醒）都读它 —— 两处各写一遍，迟早一个说"像"、
    另一个说"不像"，agent 收到的就是自相矛盾的信号。
    """
    tail_prev, head_next = (tail_prev or "").rstrip(), (head_next or "").lstrip()
    if not tail_prev or not head_next:
        return False
    return not _RE_SENT_END.search(tail_prev) and bool(_RE_STARTS_CONT.match(head_next))


def _side(block: dict[str, Any], gutter: float) -> str:
    """块在栏间空白的哪一侧（按**中心**判，与 `_columns` 同一口径）。"""
    return "L" if (block["bbox"][0] + block["bbox"][2]) / 2 < gutter else "R"


def _column_spill_seams(
    ordered: list[dict[str, Any]], width: float, height: float, gutter: float | None
) -> dict[int, str]:
    """返回 `{id(块): 理由}` —— 疑似「栏间被切开的续段」的**后一块**。

    ⚠️ 在**最终阅读顺序**上扫描相邻对（不是在原始块列表上）：分栏页里
    唯一可能发生这种切开的地方就是「左栏末块 → 右栏首块」这条接缝，
    而它在正确的阅读顺序里恰好是一对**相邻**块。用 `id()` 作键是因为此时块还没有
    稳定 ID（`b-0001` 要等 `_finalize` 重编）。
    """
    if gutter is None:
        return {}
    out: dict[int, str] = {}
    for a, b in zip(ordered, ordered[1:]):
        if a.get("type") != 0 or b.get("type") != 0:
            continue                       # 图（或图注）两侧不构成"半句话"
        if _marginal(a, width, height, gutter) or _marginal(b, width, height, gutter):
            continue                       # 页眉/页脚/页码不属于正文流
        if _side(a, gutter) != "L" or _side(b, gutter) != "R":
            continue                       # 只有「左栏 → 右栏」这条缝会切开一句话
        if not _looks_like_continuation(_block_text(a), _block_text(b)):
            continue                       # 左栏收句了 / 右栏大写起 → 这是正常的换栏
        out[id(b)] = "col-spill"
    return out


def _tag_furniture(blk: Any, band: str | None, rule: dict[str, Any] | None,
                   shade: dict[str, Any] | None = None) -> Any:
    """给块盖**版面事实**戳：`band`（页边带：top/bottom）、`rule`（页边横线）、
    `shade`（页边底纹）。

    这三个戳只影响**渲染**（页眉/页脚的小字与那条线、那行字背后的浅灰底纹），
    不参与翻译、不参与校验：`band` 只说"这一行贴在页面上下边缘"，`rule` 只说
    "它的一侧有一条什么样的页边横线"、`shade` 只说"它背后有一块什么颜色的底纹"
    —— 形如 `{"side": "below", "color": "#000000", "width": 1.4}`（`width` 已是 CSS px，
    见 `_PT_TO_PX`）与 `{"color": "#c6c6c7"}`。渲染端读它们（`markup.render_block` /
    阅读器 / 导出），**不再自己按坐标猜、也不自己编颜色**（同 ㊴「配对判据只在服务端」：
    判据写两份，迟早漂开；v10 就是渲染端把线硬编码成浅灰，看起来像"线没有"）。
    """
    if band:
        blk.payload["band"] = band
    if rule:
        blk.payload["rule"] = rule
    if shade:
        blk.payload["shade"] = shade
    return blk


class _Line(NamedTuple):
    """`get_drawings()` 里一条**又细又长**的横线（判据见 `_rule_marks`）。

    `color` 是 PDF 里的 stroke 颜色（`#rrggbb`，取不到为 None）、`width` 是线宽（pt）——
    v11 起它们一路进产物：v10 只记了"线在哪一侧"，渲染端便只能画一条自己的浅灰线。
    """
    y0: float
    y1: float
    x0: float
    x1: float
    color: str | None = None
    width: float = 0.0


def _hex_color(rgb: Any) -> str | None:
    """PDF 的 `(r,g,b)`（0–1 浮点）→ `#rrggbb`；取不到就 None（渲染端用默认色）。"""
    if not rgb or len(rgb) < 3:
        return None
    try:
        return "#" + "".join(f"{max(0, min(255, round(float(c) * 255))):02x}" for c in rgb[:3])
    except (TypeError, ValueError):
        return None


def _thin_lines(page: Any, drawings: list[Any] | None = None) -> list[_Line]:
    """这一页上**又细又长的横线**（判据见 `_rule_marks`）。

    只取 `get_drawings()` 的 `rect`（外加 stroke 的颜色/粗细），不碰 items ——
    线条在 PDF 里是路径（`l`/`re`/`qu`），逐条解释路径的成本与收益都不成比例。

    `drawings` 可传入**外部已取好**的绘图字典：`get_drawings()` 在整篇解析里是最贵的一次
    调用之一，而同一页要用它的地方有两处（页边横线、页边矢量图形）——
    取两遍等于整份文档白白多解析一遍矢量层（实测 37 页文档约 0.3s，翻倍就是白送）。
    """
    out: list[_Line] = []
    if drawings is None:
        drawings = _drawings(page)
    for dr in drawings:
        r = dr.get("rect")
        if r is None or r.width <= 0:
            continue
        if r.height <= _RULE_MAX_THICKNESS:
            out.append(_Line(r.y0, r.y1, r.x0, r.x1,
                             _hex_color(dr.get("color")), float(dr.get("width") or 0.0)))
    return out


def _drawings(page: Any) -> list[Any]:
    """`page.get_drawings()` 的安全壳：极少数 PDF 的绘图字典会抛，宁可没图不要断管线。"""
    try:
        return page.get_drawings() or []
    except Exception:                 # noqa: BLE001 —— 解析失败一律降级（这条链最贵的是中止转换）
        return []


def _rule_marks(blocks: list[dict[str, Any]], lines: list[_Line],
                height: float) -> dict[int, dict[str, Any]]:
    """页边横线 → `{id(块): {"side": "below"|"above", "color": …, "width": …}}`。

    起因（2026-09-17 宿主实测截图）：「这里少了一条水平线」 —— 每页页眉文字下方都有一条
    横跨正文宽的细线（矢量 stroke），而 `get_text()` 只回文字，**矢量线条一条都不在产物里**，
    于是整份文档的页眉线全没了。这里把它找回来，挂在**它所属的那一行文字**上。

    判据（宁可漏画，不许画错 —— 画错线比没有线更像"排版事故"）：

    1. **又细**（`_RULE_MAX_THICKNESS`）：表格的粗分隔线、图里的色块都不是"线"；
    2. **在页边带里**（`_RULE_BAND`，比 `_MARGIN_BAND` 宽 —— Springer 首页那条线在
       `y=60.0/791`，刚好落在 0.075 之外）：正文里的表格线/图框线一条都不算
       （实测 Springer 第 3 页表格线 `x=[208,544]` 在正文带，落选）；
    3. **横跨正文文字列**（两端各留 `_RULE_EDGE_SLACK`）：半截线（表格列的竖向分隔、
       分栏装饰）落选 —— 实测那条 `x=[208,544]` 也过不了这一条；
    4. **紧贴一行文字**（≤ `_RULE_MAX_GAP`，且文字整行在这条线的一侧）：这条最关键 ——
       它把"页边的一条线"变成"**这一行文字的那条线**"，于是线跟着文字走：
       页面重排、块被 ①c 改/挪都不会让线跑到别处，也不会把图框顶边当成页眉线。

    返回的是**块 → 线长什么样**（哪一侧 + 颜色 + 粗细），不是"页 → 有线条"：渲染时线画在
    那个块上，块在哪、线就在哪（出处是块的 `bbox`，与线条本体在 PDF 里的坐标无关）。
    v11 起**连颜色粗细一起带走** —— 否则渲染端只能画自己那条浅灰线（v10 的实际表现就是
    "线其实画了，但太浅看不见"）。
    """
    if not lines or not blocks:
        return {}
    xs = [(b["bbox"][0], b["bbox"][2]) for b in blocks]
    left, right = min(x0 for x0, _ in xs), max(x1 for _, x1 in xs)
    colw = right - left
    if colw <= 0:
        return {}
    slack = colw * _RULE_EDGE_SLACK
    marks: dict[int, dict[str, Any]] = {}

    def _spec(ln: _Line, side: str) -> dict[str, Any]:
        # 至少 1px（PDF 里存在 0.25pt 的细线，折算后四舍五入会归零 —— 归零等于又"没有线"）
        px = max(1.0, round(ln.width * _PT_TO_PX * 2) / 2)
        return {"side": side, "color": ln.color, "width": px}

    for ln in lines:
        if ln.x0 > left + slack or ln.x1 < right - slack:
            continue                                     # 半截线：不横跨正文列
        if ln.y0 <= height * _RULE_BAND:                 # 页眉线：文字在**线之上**
            best, side = None, None
            for b in blocks:
                gap = ln.y0 - b["bbox"][3]
                if 0 <= gap <= _RULE_MAX_GAP and (best is None or gap < best):
                    best, side = gap, b
            if side is not None:
                marks[id(side)] = _spec(ln, "below")
        elif ln.y1 >= height * (1 - _RULE_BAND):         # 页脚线：文字在**线之下**
            best, side = None, None
            for b in blocks:
                gap = b["bbox"][1] - ln.y1
                if 0 <= gap <= _RULE_MAX_GAP and (best is None or gap < best):
                    best, side = gap, b
            if side is not None:
                marks[id(side)] = _spec(ln, "above")
    return marks


def _rect_overlaps(a: Any, b: Any, tol: float = _FIG_TOL) -> bool:
    """两个 bbox `(x0,y0,x1,y1)` 是否真的压在一起（每个轴都要超过 `tol` pt）。

    `tol` 不是可有可无的：PDF 里"图的边框"与"图注第一行"常常差不到 0.5pt，
    零容差会把它们判成重叠；反过来容差过大又会把相邻的真重叠放过。
    """
    return (a[0] < b[2] - tol and b[0] < a[2] - tol
            and a[1] < b[3] - tol and b[1] < a[3] - tol)


def _cluster_rects(rects: list[Any]) -> list[list[float]]:
    """把相邻的矢量小块合成**一个整体**（返回 bbox `[x0,y0,x1,y1]`）。

    Springer 的标识就是两块：马头 84 个 items、`Springer` 字标 146 个 items，
    横间隙 4.2pt —— 不聚类就会变成两个"图"，一个在马头上、一个在字标上。
    """
    boxes = [[r.x0, r.y0, r.x1, r.y1] for r in rects]
    merged = True
    while merged:                      # 合并会放大 bbox，必须迭代到不再变化
        merged = False
        out: list[list[float]] = []
        for box in boxes:
            for prev in out:
                dx = max(prev[0], box[0]) - min(prev[2], box[2])
                dy = max(prev[1], box[1]) - min(prev[3], box[3])
                if dx <= _FIG_CLUSTER_GAP and dy <= _FIG_CLUSTER_DY:
                    prev[0], prev[1] = min(prev[0], box[0]), min(prev[1], box[1])
                    prev[2], prev[3] = max(prev[2], box[2]), max(prev[3], box[3])
                    merged = True
                    break
            else:
                out.append(box)
        boxes = out
    return boxes


def _margin_graphics(drawings: list[Any], texts: list[Any], images: list[Any],
                     height: float) -> list[list[float]]:
    """页边带里的**矢量图形**（出版社/期刊标识）→ 候选 bbox 列表。

    为什么需要它：这类标识既不在 `get_text()`（文字层）里，也不在 image block（图片层）里
    —— 它们是**画出来的矢量填充**（`get_drawings()`）。于是整份文档里它们**从来没出现过**
    （宿主 2026-09-17：「Springer 的图还是没有」；与 v10 那条页眉横线同一个道理：
    先问"取过没有"，再问"画出来没有"）。

    判据 —— 方向是**宁可漏画，绝不把正文截成图**（截错会凭空多出一张"正文截图"）：

    1. 每个绘图块足够**大**（`_FIG_MIN_SIZE`）且**不在正文带**（页边 `_FIG_BAND`）；
       这条同时把又细又长的规则线排除掉了（它们 h≈0）；
    2. 聚类后仍**不像正文**：宽 < 正文列宽的 `_FIG_MAX_WIDTH_RATIO`、高 ≤ `_FIG_MAX_HEIGHT`
       —— 通栏色带/装饰横带不是"标识"；
    3. **不压可见文字**（`texts` 是**已滤掉不可见白字**的块）：这条是主保险 ——
       Springer 首页那块 `CRITICAL REVIEW` 灰底就压着标题，落选（实测）；
    4. **不与图片块重叠**：那块区域的**图片块那一侧**会负责出图（主循环 → `_add_graphic`
       整体栅格化，理由见那里的注释），这里再来一张就是重影
       （实测 Springer 首页右上角"Check for updates"：既有 image block、也有同坐标的矢量块）。

    实测（8 页 Springer 论文）：8 个候选 = 每页页脚的马标 + 首页右上角的徽标，**零误报**；
    37 页那篇：37 个候选，同样零误报。
    """
    band = height * _FIG_BAND
    cands = []
    for dr in drawings:
        r = dr.get("rect")
        if r is None or r.width < _FIG_MIN_SIZE or r.height < _FIG_MIN_SIZE:
            continue
        if r.y1 > band and r.y0 < height - band:
            continue                                     # 不在页顶/页底那条带里
        cands.append(r)
    if not cands:
        return []
    xs = [(t["bbox"][0], t["bbox"][2]) for t in texts]
    colw = (max(x1 for _, x1 in xs) - min(x0 for x0, _ in xs)) if xs else 0.0
    if colw <= 0:
        return []
    out = []
    for c in _cluster_rects(cands):
        if c[2] - c[0] > colw * _FIG_MAX_WIDTH_RATIO or c[3] - c[1] > _FIG_MAX_HEIGHT:
            continue
        if any(_rect_overlaps(c, t["bbox"]) for t in texts):
            continue
        if any(_rect_overlaps(c, i["bbox"]) for i in images):
            continue
        out.append(c)
    # 大的优先（每页最多 `_FIG_MAX_PER_PAGE` 个 —— 判据万一走偏也不至于满页都是图）
    out.sort(key=lambda b: -((b[2] - b[0]) * (b[3] - b[1])))
    return out[:_FIG_MAX_PER_PAGE]


def _rect_area(a: Any, b: Any) -> float:
    """两个 bbox 的**交叠面积**（pt²），不相交为 0。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _margin_shades(drawings: list[Any], texts: list[Any], images: list[Any],
                   height: float) -> dict[int, dict[str, Any]]:
    """页边带里的**色块/底纹** → `{id(文字块): {"color": "#c6c6c7"}}`。

    起因：Springer 首页 `CRITICAL REVIEW` 后面有一条浅灰底纹（矢量填充
    `(51.0,60.5)-(289.1,79.4)`，`fill≈(0.774,0.776,0.778)`），我们也没有 ——
    与页眉横线同一个道理：`get_text()` 只回文字，填充块从来不在产物里。

    判据（主保险是第 3 条，它把"看不见的黑框"挡在门外）：

    1. **是填充块**（`type == "f"`、有 `fill` 颜色）且尺寸像一行字的底纹
       （`_SHADE_MIN/MAX_*`）—— 又细又长的走 `_rule_marks`（是线不是块）；
    2. **在页边带里**（`_FIG_BAND`）：正文里的表格底纹、图例色块不归这里管；
    3. **包着可见文字**（`_SHADE_COVER`，且色块不能比那行字宽太多）：色块**衬底**的
       是**看得见的字**才照抄。反向的例子就在同一页：页脚 `Vol.:(0123456789)` 是纯白字
       （v9 起不入产物），它那个**黑框**里就"没有可见文字"了 —— 不加这条判据，
       每页页脚会多出两个黑方块；
    4. **不压图片块**：那属于图的一部分（实测首页那个 "Check for updates" 圆环里
       有两块灰色填充）。
    """
    if not drawings or not texts:
        return {}
    band = height * _FIG_BAND
    out: dict[int, dict[str, Any]] = {}
    for dr in drawings:
        if dr.get("type") != "f" or not dr.get("fill"):
            continue
        r = dr.get("rect")
        if r is None or r.width < _SHADE_MIN_WIDTH:
            continue
        if not (_SHADE_MIN_HEIGHT <= r.height <= _SHADE_MAX_HEIGHT):
            continue
        if r.y1 > band and r.y0 < height - band:
            continue                                     # 不在页顶/页底那条带里
        box = [r.x0, r.y0, r.x1, r.y1]
        if any(_rect_overlaps(box, i["bbox"]) for i in images):
            continue
        best, best_cover = None, 0.0
        for t in texts:
            tb = t["bbox"]
            if tb[1] > band and tb[3] < height - band:
                continue                                 # 这行字不在页边带里
            area = max(1.0, (tb[2] - tb[0]) * (tb[3] - tb[1]))
            cover = _rect_area(box, tb) / area
            if cover > best_cover:
                best, best_cover = t, cover
        if best is None or best_cover < _SHADE_COVER:
            continue                                     # 没包着可见文字（白字黑框就在这一条被拒）
        tb = best["bbox"]
        if (r.x1 - r.x0) > (tb[2] - tb[0]) * _SHADE_MAX_SPREAD + _SHADE_PAD_ALLOW:
            continue                                     # 宽太多：是色板，不是这行字的底纹
        color = _hex_color(dr.get("fill"))
        if color and id(best) not in out:
            out[id(best)] = {"color": color}
    return out


def _render_graphic(page: Any, bbox: Any, path: Path) -> tuple[int, int] | None:
    """把页面上这一小块**原样**渲染成透明 PNG（返回像素宽高）。

    `alpha=True` 是关键：白底会变成透明 —— 标识贴在任何底色上都不会带一块白板
    （深色模式下尤其明显）。出图倍率见 `_FIG_RENDER_SCALE`（显示尺寸另算，只有 60px 上下，
    所以实际像素密度是 6 倍左右，缩放后边缘依然锐利）。

    ⚠️ 这里的"原样"是字面意思：**不重绘、不识别**，只是把 PDF 上那一小块栅格化。
    识别成"这是什么出版社的什么标识"再来重绘一遍，是另一件事（且必然失真）。
    """
    try:
        clip = fitz.Rect(*bbox)
        pix = page.get_pixmap(matrix=fitz.Matrix(_FIG_RENDER_SCALE, _FIG_RENDER_SCALE),
                              clip=clip, alpha=True)
        path.write_bytes(pix.tobytes("png"))
        return pix.width, pix.height
    except Exception:                 # noqa: BLE001 —— 出图失败只该少一张标识，不该毁整篇
        return None


def _band_of(bbox: Any, height: float) -> str | None:
    """这块东西贴在页顶带、页底带，还是压根不在页边（正文里）？"""
    if bbox[1] <= height * _FIG_BAND:
        return "top"
    if bbox[3] >= height * (1 - _FIG_BAND):
        return "bottom"
    return None


def _graphic_placement(band: str, bbox: Any, width: float) -> dict[str, Any]:
    """页边图形的**版面属性**：贴页顶还是页底、靠左还是靠右、显示多大（CSS px）。

    左右**交替**是原件的真实排版（Springer 奇数页在右下、偶数页在左下），
    所以这里从 `bbox` 的中心算，而不是一律靠右。
    尺寸按 `_PT_TO_PX` 折算 —— 要与读者看到的**正文**成比例，而不是与 PDF 的物理尺寸
    （阅读器的正文列比 493pt 宽）。
    """
    y0, y1, x0, x1 = bbox[1], bbox[3], bbox[0], bbox[2]
    return {
        "band": band,
        "align": "left" if (x0 + x1) / 2 < width / 2 else "right",
        "w": max(1, round((x1 - x0) * _PT_TO_PX)),
        "h": max(1, round((y1 - y0) * _PT_TO_PX)),
    }


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


# ── 参考文献条目（v13）──────────────────────────────────────────────────────
# 宿主 2026-09-17：「参考文献没有翻译」→ 定「**只译文献标题**，作者名/期刊名/DOI 保原文」。
# 要译标题，先得**有一条完整的条目**：PDF 抽出来的参考文献是一条连续文字流，
# 被 PyMuPDF 的块检测按**栏 / 行组**切成一堆 ~200 字符的碎片，而且切口经常落在
# **词中间**（实测：`… Metal additive man-` + `ufacturing (MAM) applications …`，
# 一块里还常常塞着下一条的**前半截**）。碎片块既没法喂给翻译通道
# （送"半个条目"过去，模型只能编一个标题出来），读者看到的也不是"一条文献"。
#
# 所以这里做**纯结构**的合并：把碎片按阅读顺序接成一条流 → 按条目编号切成整条 →
# 每块一个 `ref` 块。判定与内容都**只用已经抽出来的文字**，不碰 PDF、不调模型。
def _join_ref_fragments(parts: list[str]) -> str:
    """把碎片按阅读顺序接成一整条流 —— **只动空白，一个字符都不增删**。

    接缝规则只有三条，都来自实测形状：
    - 上一片以 `-` 结尾（行末断词/行末连字符）→ 直接接，**不插空格也不删连字符**：
      `man-` + `ufacturing` → `man-ufacturing`，`Laser-` + `directed` → `Laser-directed`。
      ⚠️ 这里**故意不"补回断词"**（不把 `man-ufacturing` 拼成 `manufacturing`）：
      PDF 里"断词连字符"与"词内连字符"长得**一模一样**（`Laser-directed` 就是反例），
      猜错就是凭空改字。看图能判的是 ①c 校对 agent（它本来就有 `行末断词` 提示），
      这里只做"不丢字、不加字"的拼接。
    - 任一侧已有空白（PDF 换行处通常留着尾随空格）→ 直接接。
    - 两侧都是非空白字符 → 补**一个**空格（否则两个词会粘成一个）。
    """
    out = ""
    for p in parts:
        if not out:
            out = p
            continue
        if out.endswith("-") and p[:1].isalpha():
            out += p
        elif out[-1:].isspace() or p[:1].isspace():
            out += p
        else:
            out += " " + p
    return out


def _ref_entry_starts(stream: str) -> list[int]:
    """条目起点在流里的下标（**只认连号**的那一串）。

    两层判据，缺一不可：
    ① 形状：`[N] ` 或 `N. ` + 大写字母起头（两种编号风格分别试，取命中多的那个）；
    ② **连号**：条目编号在原文本就是连续的 —— 把"连号"当判据才能真正挡住
       正文里那些长得像编号的东西（逗号后的年份、DOI 里的 `10.`）。
       允许跳一号（原件偶有漏号），跳两号以上即断开这一串。

    ⚠️ **连号判据不许钉死起点 = 1**（生产事故，2026-09-19）。原先 `want` 从 1 起步，
    于是一段流**只有恰好从 `[1]`/`1.` 开始**才认得出条目。而"一段"的边界是
    `merge_ref_entries` 划的：**跨页的页眉/页脚（`payload["band"]`）会把参考文献活生生截断**
    （生产 paper 1 的参考文献跨 4 页，被截成 6 段）。后果是**只有第一段（含 `[1]`、`[2]`）
    合并成功、译出 2 条**，后面 286 条明明每片都带着连号的 `[N]` 起点却一段都切不出来
    （实测：段内 bracket 候选 53/56/62/51/52/12 个，`_ref_entry_starts` 一律回 0）
    —— 界面表现正是"只翻译了两条，剩下的既没翻译也没法重译"（`refs` 免中文）。
    改法：候选照旧按形状取，但要找的是**最长的那一串连号**，起点在哪儿都行。
    """
    best: list[int] = []
    for pattern in (_RE_REF_BRACKET, _RE_REF_NUM):
        cands = [(int(m.group(1)), m.start()) for m in pattern.finditer(stream)]
        starts = _longest_consecutive_run(cands)
        if len(starts) > len(best):
            best = starts
    return best if len(best) >= 2 else []


def _longest_consecutive_run(cands: list[tuple[int, int]]) -> list[int]:
    """`(编号, 下标)` 候选里最长的一串连号，返回它们的下标。

    逐个候选当起点试着往下走：编号等于 `want` 或 `want+1`（后者 = 原件漏了一号）就接上，
    否则**跳过这个候选但不中断这一串**（形状判据难免在条目正文里认错一两个"编号"，
    为它把后面几十条正确的全丢掉才是真损失）。取最长的一串。
    """
    best: list[int] = []
    for i, (seed, _) in enumerate(cands):
        want = seed + 1
        run = [cands[i][1]]
        for num, start in cands[i + 1:]:
            if num in (want, want + 1):
                run.append(start)
                want = num + 1
        if len(run) > len(best):
            best = run
    return best


def split_ref_entries(stream: str) -> tuple[str, list[str]] | None:
    """参考文献文字流 → `(条目 1 之前的前导文字, [整条条目…])`；切不出来则 `None`。

    ⚠️ 只做**切分**：把流按起点下标切开、每段压平空白。各段拼回去必须等于原流
    （`tests/test_refs.py` 钉住了这条"一个字都不许丢"）。
    """
    starts = _ref_entry_starts(stream)
    if not starts:
        return None
    texts = []
    for i, pos in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(stream)
        text = " ".join(stream[pos:end].split())
        if text:
            texts.append(text)
    if not texts or max(len(t) for t in texts) > _REF_MAX_ENTRY:
        return None                      # 编号判据中途失效 → 放弃合并，保持原样
    return (" ".join(stream[:starts[0]].split()), texts)


def merge_ref_entries(blocks: list[Any], from_index: int) -> list[Any]:
    """把 `blocks[from_index:]`（文末材料段）里的参考文献碎片合并成 `ref` 块。

    - 只有 `refs` 类型、且**没有页眉/页脚戳**（`payload["band"]`）的块参与合并：
      页眉恰好落在参考文献那两页时也会被 3c 打成 `refs`，把它接进来会污染条目。
    - 连续的碎片算一段；段与段之间隔着别的块（图/表）→ 各段独立合并。
    - 段里切不出条目（没有编号，或编号判据中途失效）→ **原样返回那一段**，
      不产出一个两千字的"条目"，也不丢字。
    """
    head, tail = blocks[:from_index], blocks[from_index:]
    out: list[Any] = []
    run: list[Any] = []

    def flush() -> None:
        if run:
            out.extend(_merge_ref_run(run))
            run.clear()

    for b in tail:
        if b.type == "refs" and not (b.payload or {}).get("band"):
            run.append(b)
        else:
            flush()
            out.append(b)
    flush()
    return head + out


def _merge_ref_run(run: list[Any]) -> list[Any]:
    """合并一段连续的参考文献碎片；切不出条目时**原样**返回。"""
    first = run[0]
    stream = _join_ref_fragments([b.en for b in run])
    split = split_ref_entries(stream)
    if split is None:
        log.info("  · 参考文献区 %d 个碎片块切不出条目（无编号/编号不连续）→ 保持原样", len(run))
        return run
    leading, texts = split
    # 条目继承首片的 `page`（分页容器按它归页）与其余版面戳；`seam`（栏间续段戳）
    # 合并后已无意义 —— 它描述的是"与前一块的关系"，而前一块已经并进来了。
    payload = {k: v for k, v in (first.payload or {}).items() if k != "seam"}
    payload["ref_fragments"] = len(run)
    out: list[Any] = []
    if leading.strip():                  # 条目 1 之前若还有文字，单独留一块（绝不吞）
        out.append(Block(id=first.id, type="refs", en=leading, section=first.section,
                         payload=dict(payload)))
    for text in texts:
        out.append(Block(id="", type="ref", en=text, section=first.section,
                         payload=dict(payload)))
    log.info("  · 参考文献 %d 个碎片块 → %d 条（%s）", len(run), len(texts), first.id)
    return out


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


# ── 「具名小标题 + 正文」被并成一块 → 行级拆开 ─────────────────────────────

def _span_text(span: dict[str, Any]) -> str:
    return span.get("text") or ""


def _norm_head(text: str) -> str:
    """标题比对用的归一形式：折叠空白、去掉尾部的冒号/句点、转小写。"""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"[\s:：.·]+$", "", t)
    return t.lower()


def _is_bold_span(span: dict[str, Any]) -> bool:
    return bool(_RE_BOLD_FONT.search(span.get("font") or ""))


def _spans_bbox(spans: list[dict[str, Any]]) -> list[float] | None:
    boxes = [s["bbox"] for s in spans if s.get("bbox")]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _group_lines(pairs: list[tuple[int, dict[str, Any]]]) -> list[tuple[int, list[dict[str, Any]]]]:
    out: list[tuple[int, list[dict[str, Any]]]] = []
    for li, s in pairs:
        if out and out[-1][0] == li:
            out[-1][1].append(s)
        else:
            out.append((li, [s]))
    return out


def _union_bbox(boxes: list[list[float]]) -> list[float] | None:
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


# 白色（RGB 1,1,1）。`span["color"]` 是打包成整数的 RGB —— 0xFFFFFF 即白。
_WHITE = 0xFFFFFF


def _drop_invisible_spans(block: dict[str, Any], height: float) -> dict[str, Any] | None:
    """剔除页边带里的**白色（不可见）文字**；整块都是白字则整块丢掉。

    ## 为什么（2026-09-17 宿主实测截图：「图标+Springer，提取成了 `Vol.:(0123456789)`」）

    Springer 的版式在页面右下角留住一行 `Vol.:(0123456789)` —— 它是**纯白**画的
    （实测 `color=(1.0, 1.0, 1.0)`，字号 8pt，位于 y≈738–747，页高 791），
    纸面上**看不见**；那处真正可见的是矢量画的 Springer 图标（我们本来就不渲矢量图）。
    但 `get_text()` 照抽不误 → 产物多出一个毫无意义的段落、还要白送一次翻译。

    ## 判据为什么这么窄（宁可漏删，不许误删）

    白字**不一定**是不可见内容：深色底上的白字（章节横幅、图内标注）是真内容。
    所以只动**页眉/页脚带**（`EDGE_TOP` / `EDGE_BOTTOM`，即"页边短文本"的候选区）
    里的白字 —— 那里不可能是正文。实测 37 页原版全文：**全白块只有这 1 个**，
    且落在底带（y0/y1 = 738/747，页高 791 ⇒ 93.3%–94.4%），正文区零命中。

    ⚠️ 这里**不能用 `_edge_band`**（`_MARGIN_BAND = 0.05`，比 `EDGE_BOTTOM` 更窄）：
    实测该块 y1 = 747 而 `0.95 × 791 = 751` → `_edge_band` 判 `None`，
    「剔除不可见文字」会**静默不生效**（第一版就是这么写的，靠直接调用才量出来）。
    """
    y0, y1 = block["bbox"][1], block["bbox"][3]
    if y0 > height * EDGE_TOP and y1 < height * EDGE_BOTTOM:
        return block                       # 正文区：不动
    pairs = [(li, s) for li, ln in enumerate(block.get("lines") or [])
             for s in (ln.get("spans") or []) if _span_text(s).strip()]
    if not pairs:
        return block
    kept = [(li, s) for li, s in pairs if s.get("color") != _WHITE]
    if len(kept) == len(pairs):
        return block                       # 没有白字（绝大多数块走这条）
    if not kept:
        return None                        # 整块都是白字 → 整块丢掉
    return _sub_block(block, _group_lines(kept))


def _sub_block(block: dict[str, Any],
               groups: list[tuple[int, list[dict[str, Any]]]]) -> dict[str, Any]:
    """按「(原行号, spans)」分组造一个最小可用的文本块（bbox 由 spans 重算）。

    只保留后续流程真正用到的键：`type` / `bbox` / `lines`（其余流程看 `lines` 里的
    `spans`——字体、字号都在里面，标题判定要用）。

    ⚠️ **每行的纵向范围取「源行」的，不取 span 自己的**（2026-09-17 宿主实测修正）。
    行内拆分（`_split_runin_heads`）会把**同一行**切成两块，而 span 的 bbox 只包住
    它自己的字形 —— `Keywords`（无下伸部）与紧跟的关键词列表实测 y0 差 **1.4pt**。
    阅读顺序 `_by_y` 按 `(y0, x0)` 排，于是**列表排到了标题之前**，页面上渲染成
    「关键词列表 → Keywords 标题」（生产 paper 1 首页 `b-0008`/`b-0009`）。
    同一行的两块本该**只由 x 区分先后**，所以这里把 y 归到源行上（x 仍是 span 的）。
    """
    lines: list[dict[str, Any]] = []
    boxes: list[list[float]] = []
    src_lines = block.get("lines") or []
    for li, spans in groups:
        box = _spans_bbox(spans)
        if box is None:
            continue
        row = src_lines[li].get("bbox") if 0 <= li < len(src_lines) else None
        if row:
            box = [box[0], min(box[1], row[1]), box[2], max(box[3], row[3])]
        lines.append({"bbox": box, "spans": spans, "wmode": 0, "dir": (1, 0)})
        boxes.append(box)
    new = {k: v for k, v in block.items() if k in ("number", "size")}
    new.update({"type": 0, "bbox": _union_bbox(boxes) or block.get("bbox"), "lines": lines})
    return new


def _looks_like_numbered_head(text: str) -> bool:
    """`II. Section` / `A. Section` / `1.2 Section` 这类编号标题（用于拆分判据）。

    ⚠️ **不能只看 `_RE_H2/H3/H4`**：`_RE_H3` 是「单大写字母 + 句点」，而**作者名行**
    天然长这样 —— 实测 B2-02 的 `T. Herzog 1,2 · M. Brandt 1 · …` 就被误判成 `T.`
    开头的次级标题，作者行被劈成两块。所以剥掉编号后剩下的部分还要**像标题**：
    不含逗号/分隔点/`et al`（人名与单位行的指纹）、不超 8 个词；`II.`/`A.` 后不带数字。
    """
    m = _RE_HEAD_NUM.match(text)
    if not m:
        return False
    rest = text[m.end():].strip()
    if not rest or _RE_NOT_HEAD_LIKE.search(rest) or len(rest.split()) > 8:
        return False
    if (_RE_H2.match(text) or _RE_H3.match(text)) and any(c.isdigit() for c in rest):
        return False                                          # `A. 1.2 …` 是行号不是标题
    return True


def _split_runin_heads(block: dict[str, Any], body_size: float) -> list[dict[str, Any]]:
    """把「具名小标题 + 正文」被 PyMuPDF 并成一块的块拆开（返回 1 或 2 个块）。

    ## 为什么必须拆（2026-09-15 宿主实测报「abstract 没识别成标题」）

    页面上 `Abstract` 是**独占一行的加粗小标题**，PyMuPDF 却把它和紧随其后的 16 行摘要
    正文并成了**一个 17 行的块**。`_heading_level` 的两道闸门（`len(text) > 160 → None`、
    `_RE_H2_NAMED` 要求 `len(text) < 60`）于是把它判成普通段落 ——
    **`Abstract` 这个标题在产物里根本不存在**（生产 paper 2 实测 b-0006 = 整段摘要）。
    `Keywords` 同理，只是更隐蔽：Semibold 的 `Keywords` 与关键词列表在**同一行**。

    ## 判据（宁可漏拆，不许拆错）

    拆错会把一句话劈成两块，破坏左右对照与译文对齐，所以**只拆拆出来一定会被判成标题的**：

    - **A 整行**就是具名标题词（`Abstract` / `Keywords` / `摘要`…）—— 不看加粗
      （有些排版不加粗；"整行只有这一个词"本身就是足够的判据）；
    - **B 前导加粗段**是具名标题词、**编号标题**（`II.` / `A.` / `1.2`）或**字号大于正文**。

    两种情形都还要：标题短（≤ `_MAX_RUNIN_HEAD_CHARS`）、不以句末标点收尾、
    后面确实跟着正文（≥ `_MIN_RUNIN_BODY_CHARS` 字符）。**任何一条不满足就原样返回。**

    这样 B 不会把「**Note** 这是一段话」这种句中加粗（拆出来是 `p`、白碎一块）拆开：
    那种首行不是具名标题、不匹配编号、字号也没变大 → 不拆。
    """
    lines = block.get("lines") or []
    pairs: list[tuple[int, dict[str, Any]]] = [
        (li, s) for li, ln in enumerate(lines) for s in (ln.get("spans") or [])
        if _span_text(s).strip()
    ]
    if len(pairs) < 2:
        return [block]

    first_line = [s for li, s in pairs if li == 0]
    head: list[dict[str, Any]] = []
    tail: list[tuple[int, dict[str, Any]]] = []
    if first_line and _norm_head("".join(_span_text(s) for s in first_line)) in _NAMED_HEADS:
        head = first_line                                     # A：整行就是具名标题
        tail = [(li, s) for li, s in pairs if li > 0]
    else:                                                     # B：前导加粗段
        i = 0
        while i < len(pairs) and _is_bold_span(pairs[i][1]):
            i += 1
        head = [s for _, s in pairs[:i]]
        tail = pairs[i:]

    if not head or not tail:
        return [block]
    head_text = clean_text(" ".join(_span_text(s) for s in head)).strip()
    body_text = clean_text(" ".join(_span_text(s) for _, s in tail)).strip()

    if not head_text or len(head_text) > _MAX_RUNIN_HEAD_CHARS:
        return [block]
    if len(body_text) < _MIN_RUNIN_BODY_CHARS:
        return [block]
    if _RE_TERMINAL_PUNCT.search(head_text):
        return [block]
    if _norm_head(head_text) in _NAMED_HEADS:                 # A / B：具名
        pass
    elif _looks_like_numbered_head(head_text):                # B：编号标题
        pass
    elif max((s.get("size") or 0.0) for s in head) >= body_size + 1.0:
        pass                                                  # B：字号大于正文
    else:
        return [block]

    return [_sub_block(block, [(0, head)]), _sub_block(block, _group_lines(tail))]



def _pair_captions_by_geometry(
    images: list[dict[str, Any]], texts: list[dict[str, Any]]
) -> tuple[dict[int, str], set[int]]:
    """按几何就近把图注文本配给图片（优先图片下方，其次上方，允许小幅重叠）。

    跨栏大图（bbox 横跨两栏）在阅读序里会把图注甩到很远，顺序法不可靠；
    宿主既有管线的经验也是「用坐标交叉验证」，此处同理。

    ⚠️ v11 两处修正（2026-09-17）—— 旧版把**真图连图注一起丢掉**：

    1. **允许小幅纵向重叠**。旧判据是"纵向一重叠就不算图注"，而 Springer 的图注常压在
       图片上边缘（实测 Fig. 5：图 `y=297–716`、图注 `y=295.1–317.9`，重叠 20.9pt）
       ⇒ 配不上注 ⇒ `_finalize` 判"无图注的图" ⇒ **整张图被删**（37 页那篇丢了 4 张真图：
       Fig. 5/8/20/25）。现在按 `_CAP_OVERLAP`（或图高的 `_CAP_OVERLAP_RATIO`）放行小幅重叠，
       压得太多（图注压根在图里面）仍不算。
    2. **全局最近优先 + 一对一认领**。旧版逐图各挑各的最近者，会**互相抢**：实测 23 页
       上面那张图（Fig. 20，图注压在它上沿）没配上，反而把下面 Fig. 21 的图注抢了过来
       —— 两张图的图注全错位。现在把所有 `(图, 图注, 距离)` 排序后依次认领，最近的先落定。
       `_CAP_SIDE_PENALTY` 给"与图片并排（横向不重叠）"的候选加代价：并排的短文本
       更可能是邻栏的话，而不是这张图的图注。
    3. **并排（邻栏）的图注也算**。实测 Fig. 8/21/24：图占满右栏、图注排在被挤窄的
       **左栏**，两块纵向几乎完全重叠 —— 旧代码在这种情形下量的是"纵向重叠深度 / 图高"，
       重叠 80.5pt 对 0.25×318pt 的阈值，**差 0.9pt 被拒**。现在分成两条路：
       横向有重叠 ⇒ 上下关系（量纵向距离）；横向不重叠 ⇒ 并排关系（量**横向间隙**，
       纵向重叠是必然的，不做限制）。
    """
    cands: list[tuple[float, int, int]] = []
    for ii, img in enumerate(images):
        iy0, iy1 = img["bbox"][1], img["bbox"][3]
        ih = max(1.0, iy1 - iy0)
        for ti, tb in enumerate(texts):
            text = " ".join(_block_text(tb).split())
            if not _caption_like(text) or len(text) > 500:
                continue
            ty0, ty1 = tb["bbox"][1], tb["bbox"][3]
            x_overlap = min(img["bbox"][2], tb["bbox"][2]) - max(img["bbox"][0], tb["bbox"][0])
            if x_overlap > 0:
                # ── 上下关系（图注在图上/下方）：按纵向距离与重叠深度判
                gap = max(iy0 - ty1, ty0 - iy1)       # >0 分离，<0 纵向重叠
                if gap > _CAP_MAX_GAP:
                    continue
                if gap < -min(_CAP_OVERLAP, _CAP_OVERLAP_RATIO * ih):
                    continue                          # 压得太深：图注在图里面，不是它的
                score = abs(gap) + (8.0 if gap < 0 else 0.0)
            else:
                # ── 左右关系（图注在**邻栏**、与图并排）：v11 新增
                # 实测（37 页那篇的 Fig. 8/21/24）：图占满右栏、图注排在被挤窄的**左栏**里，
                # 两块**纵向几乎完全重叠**（Fig. 8：图 `y 397–716`、注 `y 395–478`）。
                # 旧代码把这种"左右并排"的候选一律加 `_CAP_SIDE_PENALTY` 后按纵向距离打分，
                # 而纵向重叠深度又用「图高比例」判 → 重叠 80.5pt > 0.25×318pt，**刚好被拒**
                # （差 0.9pt），于是图注配不上 → 图与图注一起消失。
                # 并排时该量的是**横向间隙**，纵向重叠反而是必然的。
                if min(iy1, ty1) - max(iy0, ty0) <= 0:
                    continue                          # 既不上下、也不并排 → 不相干
                hgap = max(img["bbox"][0] - tb["bbox"][2], tb["bbox"][0] - img["bbox"][2])
                if hgap > _CAP_MAX_GAP:
                    continue
                score = hgap + _CAP_SIDE_PENALTY
            cands.append((score, ii, ti))
    cands.sort()
    result: dict[int, str] = {}
    used: set[int] = set()
    taken: set[int] = set()
    for _score, ii, ti in cands:
        if ii in taken or id(texts[ti]) in used:
            continue
        taken.add(ii)
        used.add(id(texts[ti]))
        result[id(images[ii])] = " ".join(_block_text(texts[ti]).split())
    return result, used


def _page_rotation(page: Any) -> int:
    """这一页的文字是不是**整页旋转 90° 画的**；是则返回把它**转正**的度数（0/90/270）。

    只看 `line["dir"]`（PyMuPDF 除非页面 `/Rotate` 非 0，否则报的就是**内容坐标系**）：
      * `(0,-1)` ⇒ 字是自下而上排的 ⇒ `show_pdf_page(..., rotate=270)` 转正；
      * `(0, 1)` ⇒ 自上而下（顺时针转出来的那种）⇒ `rotate=90`。
    判据用**字符数加权**，且要求纵向字符数够多 —— 竖排的表头标注（一个词）不该
    把整页判成旋转页：那代价是把一页正常内容转 90°。
    """
    up = down = horiz = 0
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b.get("lines") or ():
            n = sum(len(s.get("text") or "") for s in (ln.get("spans") or ()))
            if not n:
                continue
            d = ln.get("dir") or (1, 0)
            if d[1] < -0.5:
                up += n
            elif d[1] > 0.5:
                down += n
            else:
                horiz += n
    vert = up + down
    if vert < _ROT_MIN_CHARS or vert / max(1, vert + horiz) < _ROT_VERT_RATIO:
        return 0
    return 270 if up >= down else 90


def normalize_rotated_pages(
    src: Any, dst: str | Path | None = None,
) -> tuple[Any | None, dict[int, int], Path | None]:
    """把「整页旋转 90°」的页面**转正**：返回 `(转正后的 doc | None, {页码: 度数}, 落盘路径 | None)`。

    ## 为什么必须转正，而不是"在别处补偿"

    表格的**行**在页面坐标里是**竖列**：整条链路（`_by_y` 排序、`_gutter` 找栏间空白、
    ①c 的 `read_page(region=…)`）都假定文字水平。实测（宿主 2026-09-17 报的"竖向表格"，
    8 页稿第 5 页 = 生产 paper 1 的第 5 页）：同一行里相邻的格子被**粘成一句**
    （`Single sensor Spectrum sensors SVM, DT, KNN, LDA, K-means, NN Monitoring the porosity…`），
    而 Ref 列的值（`[ 92 ]`）因为 x 最小，跑到了**表头之前**。

    转正是**纯坐标变换**：`show_pdf_page(rotate=…)` 把这一页原样重画一遍，
    文字层、矢量层（表格线/标识）与图片层一起跟着转（实测图片字节与尺寸不变），
    于是它和一张**普通的横排表格**完全一样 —— 不需要给表格识别加任何特例。

    返回的 doc 由调用方负责关闭。`dst` 只在这时写一次（没有旋转页则一个字节都不写）：
    它是给 **①c** 用的 —— 它渲染页图、量疑似表区，必须与解析产物**同一套坐标**，
    否则 `read_page(region=…)` 会放大到错误的地方。落盘失败不致命（返回的 doc 照样能用），
    只记一条 warning：那时 ①c 会退回去看原方向页面（校对质量下降，但不会崩）。
    """
    rot = {i: r for i, page in enumerate(src, 1) if (r := _page_rotation(page))}
    if not rot:
        return None, {}, None
    out = fitz.open()
    for i, page in enumerate(src, 1):
        r = rot.get(i)
        if r:
            # 转正后的页面：宽高互换（竖版 → 横版）
            fresh = out.new_page(width=page.rect.height, height=page.rect.width)
            fresh.show_pdf_page(fresh.rect, src, i - 1, rotate=r)
        else:
            out.insert_pdf(src, from_page=i - 1, to_page=i - 1)       # 原样复制，零改动
    saved: Path | None = None
    if dst is not None:
        saved = Path(dst)
        try:
            saved.parent.mkdir(parents=True, exist_ok=True)
            out.save(saved)
        except Exception as exc:                                  # noqa: BLE001 — 不致命
            log.warning("转正后的页面副本写盘失败（%s）→ ①c 将看原方向页图：%s", saved, exc)
            saved = None
    return out, rot, saved


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

    # 3b) ⚠️ v11 起**不再剔除"无图注的图片"**（2026-09-17 宿主：「和 pdf 尽量保持一致」）。
    #     原先这一步整批删掉"无图注的图片（期刊 logo、作者头像）"，但判据本身不可靠：
    #     图注压在图片边缘上、或图注在图上方的排版配不上注 ⇒ **真图被当成 logo 删掉**
    #     （实测 37 页论文丢 4 张真图：Fig. 5/8/20/25，每张都连着图注一起消失）。
    #     宁可多留一张 logo，也不能少一张图：多出来的东西看得见，少掉的看不见。
    #     `meta` 里保留这两个键（形状不变，历史上读过它的地方不会 KeyError）。
    doc.meta["dropped_images"] = []
    doc.meta["dropped_image_count"] = 0
    #     3a 认领作图的那些文本块仍要从正文流里去掉（它们的文字已经成了图注，
    #     留在正文就是同一句话出现两遍）—— 这正是 `kept` 在这里的唯一职责。
    kept: list[Any] = [b for i, b in enumerate(merged) if i not in claimed]

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
        # 3c-2) 把碎片合并成**整条**条目（v13）：`refs` = 尚未切出条目的碎片（免中文），
        #       `ref` = 一条完整文献（只译标题，见 `merge_ref_entries`）。
        kept = merge_ref_entries(kept, ref_start + 1)
        doc.meta["refs_start"] = head.id
        doc.meta["refs_count"] = sum(1 for b in kept[ref_start + 1:]
                                     if b.type in ("refs", "ref"))

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


def _add_graphic(page: Any, add: Any, out_dir: Path, name: str, bbox: Any,
                 place: dict[str, Any]) -> bool:
    """把页边矢量图形渲染成透明 PNG，并作为 `deco` 块加进文档。

    `deco` 是**装饰块**（v11 新增的块类型）：没有文字、不参与翻译、不参与校验
    （`validate.NO_ZH_TYPES`），渲染端只把它当一张按物理尺寸摆放的图。
    ⚠️ 不复用 `figure`：`figure` 的语义是"有图注的插图"（图注要译、样式带边框底色），
    一个出版社标识套进去就会出现灰底加边框、还会被 ①c agent 当成"缺图注的图"。

    返回**是否真的加进去了** —— 出图失败时调用方要能退回原路（见主循环里
    "无图注的小图片"那一段：栅格化不成，还得老老实实当 `figure` 贴内嵌图）。
    """
    if not _render_graphic(page, bbox, out_dir / name):
        return False                                      # 出图失败：少一张标识，不毁整篇
    add("deco", "", payload={
        "src": f"assets/{name}",
        "bbox": [round(float(v), 1) for v in bbox],
        **place,
    })
    return True


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
    # ── 整页旋转的页面**先转正**（v12）───────────────────────────────────────────
    # 转正后本函数后续每一行都不必再想"这页是不是躺着的"：坐标系就是正的。
    # 副本落在**这篇文献自己的目录**（`papers_dir/p<id>/normalized.pdf` = 图片资产目录的父目录）
    # —— 它必须给 ①c 用（渲染页图、量疑似表区），与这里**同一套坐标**。
    # ⚠️ **不能**图省事写成 `pdf_path.parent / "normalized.pdf"`：所有 PDF 都平铺在同一个
    # `papers_dir` 下，那个名字是**全库共享**的 ⇒ 并发（`MAX_CONCURRENCY=2`）时两篇互相覆盖，
    # ①c 会**读到别人的 PDF 而不报错**；而且删文献（㉙）只收 `p<id>/` 与 PDF 本身，孤儿副本
    # 会一直留在库里。放在 `p<id>/` 下顺带解决这两件事（`_remove_paper_files` 已整目录 rmtree）。
    # 没给 `assets_dir` 的临时脚本：副本名字带上源文件名，保证不共享。
    if assets_dir:
        norm_dst = Path(assets_dir).parent / "normalized.pdf"     # papers_dir/p<id>/normalized.pdf
    else:
        norm_dst = pdf_path.with_name(f"{pdf_path.stem}.normalized.pdf")
    norm_src, rotated, norm_path = normalize_rotated_pages(src, norm_dst)
    if norm_src is not None:
        src.close()                                   # 内容已在建副本时拷走（show_pdf_page）
        src = norm_src
        log.info("解析：%d 页整页旋转 → 已转正（%s）",
                 len(rotated), "、".join(f"第 {p} 页 {r}°" for p, r in sorted(rotated.items())))

    raw_pages: list[list[dict[str, Any]]] = []
    lines_per_page: list[list[_Line]] = []
    draws_per_page: list[list[Any]] = []
    heights: list[float] = []
    widths: list[float] = []

    for page in src:
        rect = page.rect
        raw_pages.append(page.get_text("dict")["blocks"])
        # 矢量层（`get_drawings()`）很贵，整篇只取一次，同一页的两个消费者共用：
        #   ① 页边横线（页眉下那条通栏细线，v10）；
        #   ② 页边矢量图形（出版社/期刊标识，v11 —— 它们在文字层与图片层里**都不存在**）。
        drawings = _drawings(page)
        draws_per_page.append(drawings)
        lines_per_page.append(_thin_lines(page, drawings))
        heights.append(rect.height)
        widths.append(rect.width)

    doc = Doc(meta={"source": pdf_path.name, "pages": len(raw_pages)})
    if rotated:
        # 落进 meta 三用：①c 据此取同一份 PDF；调试时一眼看得出哪几页被转正过；
        # 也解释了"这一页的分栏判断为什么被跳过"。
        doc.meta["rotated_pages"] = sorted(rotated)
        if norm_path is not None:
            doc.meta["normalized_pdf"] = str(norm_path)
    if title_hint:
        doc.meta["title_en"] = title_hint

    # 正文字号（用于标题判定）—— 按字符数加权的中位数，见 `_body_size` 的实测说明
    body_size = _body_size(raw_pages)

    section = ""
    pending_figure = None
    fig_index = 0
    deco_index = 0

    for page_no, blocks in enumerate(raw_pages, start=1):
        width = widths[page_no - 1]
        text_blocks = [b for b in blocks if b.get("type") == 0 and _block_text(b)]
        # ⚠️ 这里曾经有一道 `_running_headers`（跨页重复的页边短文本 → 判页眉/页脚 → 整行丢）。
        # 宿主 2026-09-17 定：「页眉文字不需要有意丢掉。和 pdf 尽量保持一致」→ 整条链撤掉
        # （剩下的水印滤除 `_RE_WATERMARK` 只针对出版社版权声明，与页眉无关）。
        # 页边带里的**白色（不可见）文字**不入产物（Springer 的 `Vol.:(0123456789)`
        # 就是纯白画的；见 `_drop_invisible_spans`）。
        cleaned = [_drop_invisible_spans(b, heights[page_no - 1]) for b in text_blocks]
        text_blocks = [b for b in cleaned if b is not None]
        # 「加粗小标题 + 正文」被 PyMuPDF 并成一块的，先按行拆开 —— 不拆则 `Abstract`
        # 这类标题在产物里**根本不存在**（见 `_split_runin_heads`）。
        text_blocks = [x for b in text_blocks for x in _split_runin_heads(b, body_size)]
        image_blocks = [b for b in blocks if b.get("type") == 1]

        # 每个块都**记下自己来自 PDF 第几页**（`payload.page`）——阅读器/导出据此重建
        # 「PDF 分页容器」（原型 `.pdf-page` + 「第 N 页 / 共 M 页」页脚）。
        # 解析循环本来就按页遍历（`enumerate(raw_pages, 1)`），此前只把页码写进了
        # figure 块，于是分页做不到；现在统一由这个包装器盖戳，避免漏写。
        # ⚠️ 页码是**版面事实**，不是渲染选项：合并块（merge_math_runs）沿用首块页码，
        #    跨页合并时页码会略有偏差（同一段公式被 PDF 拆到两页的极少数情形）。
        add = _paged_adder(doc, page_no)

        # 转正后的旋转页（v12）**不分栏**：整页是一张横排表格，格间空白会被
        # `_gutter` 当成栏间空白，行序被劈成"每行的左半 → 每行的右半"。
        if page_no in rotated:
            ordered = _reading_order(text_blocks + image_blocks, width,
                                     heights[page_no - 1], columns=False)
            seams: dict[int, str] = {}
        else:
            ordered = _reading_order(text_blocks + image_blocks, width, heights[page_no - 1])
            # 「栏间续段」只在**这一页的阅读顺序**上才看得出来（要左右栏的接缝），
            # 所以在这里算好、随块一起盖进 payload（见 `_column_spill_seams`）。
            seams = _column_spill_seams(ordered, width, heights[page_no - 1],
                                        _page_gutter(ordered, width, heights[page_no - 1]))
        captions, caption_blocks = _pair_captions_by_geometry(image_blocks, text_blocks)
        # 页边横线 → 挂在**它所属的那一行文字**上（`{id(块): "below"|"above"}`）。
        # 必须放在 `_split_runin_heads` **之后**算：拆出来的两块是新字典，按 `id()`
        # 盖章要在最终这批块上，否则页眉那行拆开时线会丢。
        rules = _rule_marks(text_blocks, lines_per_page[page_no - 1], heights[page_no - 1])
        # 页边**色块/底纹** → 挂在被它衬底的那个文字块上（`_margin_shades`）。
        shades = _margin_shades(draws_per_page[page_no - 1], text_blocks, image_blocks,
                                heights[page_no - 1])

        # 页边带里的**矢量图形**（出版社/期刊标识）—— 渲染成透明 PNG 作为 `deco` 块。
        # 放在本页正文**之前**：它们贴在页顶带（标识、徽标），而贴在页底带的那些
        # 放在本页正文之后（见循环末尾）—— 于是渲染出来与 PDF 的上下位置一致。
        # 判据只看**几何**（见 `_margin_graphics`），且必须用**已滤掉不可见白字**的
        # `text_blocks`：Springer 页脚的 `Vol.:(0123456789)` 是纯白的、恰好压在标识上，
        # 若把它当成"可见文字"，每页的马标都会因为"压着文字"被排除。
        page_h = heights[page_no - 1]
        graphics = _margin_graphics(draws_per_page[page_no - 1], text_blocks, image_blocks, page_h)
        bottoms: list[list[float]] = []
        for gbox in graphics:
            band_top = _band_of(gbox, page_h) == "top"
            if not band_top:
                bottoms.append(gbox)
                continue
            deco_index += 1
            _add_graphic(src[page_no - 1], add, out_dir, f"p{page_no}_deco{deco_index}.png",
                         gbox, _graphic_placement("top", gbox, width))

        for b in ordered:
            if id(b) in caption_blocks:                  # 已配作图注的文本块，不再单独成段
                continue
            if b.get("type") == 1:                       # ── 图片
                fig_index += 1
                cap = captions.get(id(b), "")
                band = _band_of(b["bbox"], page_h)
                # 无图注 **且** 贴在页边 **且** 尺寸像个标识（不是插图）= 出版社徽标/图标之类
                # （v11 起不再丢弃，但也不该当成正文插图：`figure` 会带边框灰底、
                # 还会被 ①c 当成"缺图注的图"）。
                # 典型：Springer 首页右上角的 "Check for updates"。
                # ⚠️ **不能直接把内嵌的图片对象贴出来**（v11 本地实测踩过）：那个 30×29
                # 的内嵌图只是徽标底下那块**平坦灰底板**（226 字节），而圆环、"Check for
                # updates" 字样都是**矢量绘制** —— 直接贴内嵌图，页面上就是一个空的灰方块
                # （宿主原话：「springer 的图还是没有」）。徽标是"图 + 矢量"叠出来的**一个
                # 整体**，只有把那一块区域整体栅格化（`_add_graphic` → `_render_graphic`）
                # 才拿得全 —— 与旁边那条"矢量标识"走同一条路。
                # ⚠️ 尺寸这一条不能省：页底那 110pt 带里常常压着一张**大插图**
                # （实测 37 页论文的 Fig. 21 就落在页底带，360×213pt）—— 那是正文插图，
                # 只是没配上图注而已，不能当成"出版社标识"贴到右下角去。
                tiny = (b["bbox"][3] - b["bbox"][1] <= _FIG_MAX_HEIGHT)
                if not cap and band and tiny:
                    box = b["bbox"]
                    if _add_graphic(src[page_no - 1], add, out_dir,
                                    f"p{page_no}_deco{deco_index + 1}.png", box,
                                    _graphic_placement(band, box, width)):
                        deco_index += 1
                        pending_figure = None
                        continue
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
                        "caption": cap,
                        "bbox": [round(v, 1) for v in b["bbox"]],
                    },
                )
                continue

            text = _block_text(b)

            # 图注：紧随图片块之后、以 Fig./Table 开头 → 挂到该图
            #
            # ⚠️ 三条判据缺一不可（v12 修的一处**静默丢内容**）：这里原来只写
            # `if pending_figure is not None and _caption_like(text)`，而这个
            # `pending_figure` 是**跨页存活**的，且**几何配对**（`_pair_captions_by_geometry`，
            # v11）认领过的图注**不会走这个分支** → 变量从没被复位。于是下一页以
            # "Table N …" 起头的**表注**撞进来，而那个图已经有图注了 → 什么都不写、
            # 直接 `continue` —— 实测 8 页稿第 5 页的 `Table 2 Research on single and
            # multi-sensor as ML input data for defect detection` **在任何块里都不存在**
            # （①c 只能如实报"表注在抽取中丢失，无法凭块补写"，而它是对的：不许凭图默写）。
            # 判据：**同一页** 且 那个图**还没有**图注，才认。
            if (pending_figure is not None
                    and pending_figure.payload.get("page") == page_no
                    and not pending_figure.payload.get("caption")
                    and _caption_like(text)):
                pending_figure.payload["caption"] = " ".join(text.split())
                pending_figure = None
                continue

            if _RE_WATERMARK.match(text):
                continue                                  # 出版社水印页脚（非论文内容）
            if _RE_JUNK.match(text) and _size_without_logo(b, body_size) > body_size * 2:
                continue                                  # 期刊 logo / 装饰单字

            level = _heading_level(b, body_size)
            clean_src = _text_without_logo(b, body_size)
            # 版面边带（页眉/页脚）、页边横线与底纹：**版面事实**，渲染端据此画页边装饰
            # （宿主 2026-09-17：「页眉文字不需要有意丢掉。和 pdf 尽量保持一致」）。
            band = _edge_band(b, heights[page_no - 1])
            rule = rules.get(id(b))
            shade = shades.get(id(b))
            if level == 1:
                _tag_furniture(add("h1", _desmallcaps(clean_src), section=section, level=1),
                               band, rule, shade)
                continue
            if level is not None:
                clean = _desmallcaps(clean_src)
                section = clean
                _tag_furniture(add(f"h{level}", clean, section=section, level=level),
                               band, rule, shade)
                continue

            blk = _tag_furniture(add("p", " ".join(text.split()), section=section), band, rule, shade)
            # 疑似「栏间被切开的续段」→ 盖戳（**只标记**，"该不该并"交给①c 校对 agent，
            # 见 `_column_spill_seams`）。
            if id(b) in seams:
                blk.payload["seam"] = seams[id(b)]

        # 贴在页底带的矢量图形（出版社页脚标识）—— 放在本页正文**之后**，
        # 于是它在分页容器里出现在页底，与 PDF 的位置一致（✕ 若一律放在页首，
        # 页脚的 Springer 马标会跑到每页正文顶上）。左右按原件的排版交替（见 `_graphic_placement`）。
        for gbox in bottoms:
            deco_index += 1
            _add_graphic(src[page_no - 1], add, out_dir, f"p{page_no}_deco{deco_index}.png",
                         gbox, _graphic_placement("bottom", gbox, width))

    src.close()
    return _finalize(doc)
