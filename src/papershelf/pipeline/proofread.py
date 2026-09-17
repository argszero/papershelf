"""原文抽取校对 —— **agent 形态**（管线 ①c）。

## 为什么必须是 agent（宿主 2026-09-14）

> 「LLM 需要用于提取后的校对更新，要校对文字、格式，所有能校对的都要校对」
> 「参考 emrg 代码，在提取完成后，llm 校对更新的方式应该是 agent 方式的，也就是给 agent
>   提供输入（原始 pdf 或者 pdf 转成的图片、提取的结果），以及工具（读图片工具、
>   读提取结果的工具、修改提取结果的工具等等）。然后让 llm 以 agent 的形式来校对更新。」

第一版是「逐页送图 + 抽取结果，要模型回一份 `{order, problems}`」的**单轮调用**。实测它的
三个毛病都源自"只有一轮"这个形态本身：

1. **看不清的地方不能要求放大**。上标编号、公式碎片、表格数字，130dpi 整页图看不清 ——
   单轮里模型只能说"看不清"；agent 可以 `read_page(region=…)` 把那一角裁出来再看一遍。
2. **覆盖靠运气**。同一页跑两次，行末断词一次修 10 处、一次只修 3 处（它只挑自己注意到的）。
   单轮没有追问的余地；agent 有 `check_artifacts`（程序量出的可疑片段）与 `mark_page_done`
   （每页必须显式收尾），没结论的页会被**追问**到给出结论为止 —— 这才是"所有能校对的都要校对"。
3. **不能按需索取**。单轮必须一次灌进整页所有块文本；agent 里 `read_blocks` 分页取、
   `read_block` 取单块，注意力花在真正要判的地方，也顺带压住了上下文。

## 工具契约（照 emrg 的 `ToolExecutor` / `ToolRegistry`）

`ProofreadTools.specs()` 出 OpenAI function-calling 的定义，`call(name, args)` 执行。
命名与职责对齐 emrg 的做法：**读**（图/块/尺子）与**写**（改文本/切分/合并/删除/排序）分开，
写操作全部过护栏，**被拒时把原因当成工具结果返回**（agent 能据此改正或放弃）。

**图像怎么投递**：OpenAI 的 `role:"tool"` 消息只能装文本，所以渲染图走**紧跟工具结果之后的
user 消息**（`[工具 read_page(1) 的返回：第 1 页渲染图]` + `image_url`）。实测该 provider 接受
这种交错，且模型确实在看图 —— 它能读出作者行的上标编号、以及抽取里丢掉的 ✉/ORCID 图标。

## 安全护栏

模型会**编**，所以每个修改都有一条**可判定**的接受条件（见各工具方法），拒绝计数进
`rejected` 并写日志；同时 `_clean` 兜底（连字/特殊空格是确定性事实，不该赌模型）。
"""

from __future__ import annotations

import base64
import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .model import Block, Doc, grid_shape, make_block_id, table_text
from .parse import _looks_like_continuation, clean_text

log = logging.getLogger("papershelf.pipeline.proofread")

# ── 送入文本的上限 ────────────────────────────────────────────────────────
# `read_blocks` 默认分批给的块文本截断长度与条数：agent 可以再 `read_block` 取全文，
# 所以这里宁可截断（省上下文），不要一次灌满。
BLOCK_PREVIEW = 320
# 一次给多少块：**能一次给完就不要分页**。理由不是省那点字符，是省**轮数** ——
# 参考页（bibliography）一页 34 个小块，按 8 块一次要 5 次调用，而这 5 条结果会在后面
# 每一轮的上下文里重发（实测那 4 页均价 103k tokens/页、21.7k/轮，是普通页的 4 倍）。
BLOCKS_PER_PAGE_CALL = 20
# 但也不能一次灌爆：单次结果的字符上限（超了就按这个切，agent 再取下一批）。
BLOCKS_CALL_CHARS = 6000
# `read_page` 的放大上限（像素）：太大拖慢调用，太小看不清上下标。
MAX_CROP_PX = 1800
# `check_artifacts` 每页最多列几块（超了先修要紧的）。
MAX_HINT_BLOCKS = 12

# ── 图片编码与请求体大小（实测出来的数，不是拍的）────────────────────────
# 服务端请求体上限实测 **1MB**：0.5MB / 1MB 都过（400=图片本身不合法），1.5MB 直接 413。
# 而 130dpi 整页 **PNG 有 385KB** —— 三张就超。所以：
#   ① 编成 JPEG（同分辨率省 ~40%，正文仍然清楚，实测模型照样读出上标与图注小图标）；
#   ② 剪图按**体积**算，不按张数（不同页的图大小差很多，按张数会在某页撞上）。
JPEG_QUALITY = 75
# 请求体上限：服务端 1MB 硬限（413）+ 上游网关对超大请求会 **504**（实测 37 页跑到第 11 分钟
# 被咬了一次）→ 留出余量，宁可按体积剪图也不要吃到那两条边。
MAX_BODY_BYTES = 800_000
# 单轮最多投递几张图。2 张 ≈ 640KB（130dpi JPEG ~234KB/张）—— 3 张就顶到 1MB 边缘，
# 所以提示词里也明说"一轮最多看两页"（剪图会让模型少看到它要的那一张）。
MAX_IMAGES_PER_ROUND = 2


def _encode(pix) -> bytes:
    """渲染结果 → JPEG 字节（PyMuPDF 不支持 jpg 时退回 PNG）。"""
    try:
        return pix.tobytes("jpg", jpg_quality=JPEG_QUALITY)
    except Exception:                                     # noqa: BLE001 — 老版本没有 jpg 输出
        return pix.tobytes("png")


def _body_size(messages: list[dict]) -> int:
    return len(json.dumps({"messages": messages}, ensure_ascii=False))


def _fit_images(messages: list[dict], budget: int = MAX_BODY_BYTES) -> int:
    """从**最旧**的图开始丢，直到消息体小于 `budget`（返回丢了几张）。

    丢图不丢信息：渲染结果有内存缓存，模型想再看只要重新 `read_page`（不重新渲染）。
    """
    dropped = 0
    while _body_size(messages) > budget:
        oldest = next((m for m in messages
                       if isinstance(m.get("content"), list)
                       and any(isinstance(p, dict) and p.get("type") == "image_url"
                               for p in m["content"])), None)
        if oldest is None:
            return dropped                                 # 没有图可丢了，剩下的靠调用方兜底
        kept = [p for p in oldest["content"]
                if not (isinstance(p, dict) and p.get("type") == "image_url")]
        n = len(oldest["content"]) - len(kept)
        dropped += n
        head = next((p.get("text", "") for p in kept if isinstance(p, dict)), "")
        if kept and len(kept) > 1:
            oldest["content"] = kept
        else:
            oldest["content"] = (f"{head} ⚠️ {n} 张渲染图已从上下文移除（控制请求体大小）；"
                                 f"需要再看就重新调用 read_page（命中渲染缓存）。")
    return dropped


# ── 思考预算（**成本的真正大头**）────────────────────────────────────────
# 2026-09-14 实测（同一条校对请求，池子里的视觉模型 `deepseek-v4-flash-vision-exp`；
# ⚠️ 该名 2026-09-15 已改叫 `deepseek-flash`，旧名仍作别名可用、指向同一后端）：
#   基线 max_tokens=32000      completion=2001 tokens / 思考 6819 字符 / 10.0s
#   max_tokens=1500            completion=1179 tokens / 8.6s（压 max_tokens 只能压一点）
#   reasoning_effort="low"     completion=1179 tokens
#   reasoning_effort="minimal" completion= 693 tokens
#   thinking={"type":"disabled"} completion=  80 tokens / 思考 0 字符 / 1.1s   ← 12 倍
# 而"每轮固定开销 + 上下文每轮重发"意味着：**轮数 × 每轮输出 ≈ 整篇成本**，
# 所以这一个参数就决定了"agent 能不能跑完 37 页"。默认 `off`：
# 校对是"照着图核对字符串"，不是解数学题，省下的思考换来的成本下降是量级上的。
_THINKING_OFF = {"off", "disabled", "none", "false", "0"}


def _apply_thinking(payload: dict, thinking: str) -> None:
    """把 `thinking` 配置翻译成 provider 参数（空=不发，即用上游默认）。"""
    t = (thinking or "").strip().lower()
    if not t or t in {"auto", "on", "default"}:
        return
    if t in _THINKING_OFF:
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["reasoning_effort"] = t


# 文字修订的接受条件：不许暴涨（防编造），也不许太不像（防重写）。
_MAX_GROWTH_RATIO = 3.0
_MAX_GROWTH_SLACK = 300
_MIN_SIMILARITY = 0.55

# 类型修订（`set_block_type`）：允许的层级映射 + 标题长度上限。
# `h1` **故意不在**：h1 是文章标题（解析阶段就抽进 meta、由页眉渲染），
# 正文里再出现一个 h1 会让标题重复、并污染大纲（宿主强调「样式与排版的一致性」）。
_BLOCK_TYPES = {"p": 0, "h2": 2, "h3": 3, "h4": 4}
_MAX_HEAD_CHARS = 160

_RE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)

# ── 自动检出（"尺子"）：程序能**量**出可疑，但"该不该改"只有看图才知道 ──────
_RE_HYPHEN_BREAK = re.compile(r"\b([A-Za-z]{2,})-\s+([a-z]{2,})")
_RE_CITE_INNER_SPACE = re.compile(r"\[[^\]\n]{0,14}?\]")
_RE_SPACE_BEFORE_DOT = re.compile(r"\w\s+\.(?:\s|$)")


def _artifacts(text: str) -> list[str]:
    """按字符串规则列出可疑片段（只报事实，不含判断）。"""
    text = text or ""
    out: list[str] = []
    if m := _RE_HYPHEN_BREAK.search(text):
        out.append(f"行末断词 {m.group(1)!r} + {m.group(2)!r}（需看图判断是断字还是词内连字符）")
    for hit in _RE_CITE_INNER_SPACE.findall(text):
        if re.search(r"\s", hit.strip("[]")) or re.match(r"\[\s", hit):
            out.append(f"引用编号内含空格 {hit!r}")
            break
    if _RE_SPACE_BEFORE_DOT.search(text):
        out.append("句点前有空格")
    return out


def _artifact_tags(text: str) -> list[str]:
    """`_artifacts` 的紧凑版（只给标签，给 seed 用）。

    为什么要这个：`overview` + 每页一次 `check_artifacts` 观测下来是**每页 1 轮**的
    固定开销，而一轮 ≈ 2.2k tokens（推理模型的思考底噪）。全篇的可疑清单是**纯字符串
    计算**、零成本就能算出来，所以在 seed 里一次性交给 agent —— 它一开始就知道哪几页
    有问题、问题是什么，直接进"看图 → 改"。
    """
    text = text or ""
    tags: list[str] = []
    if _RE_HYPHEN_BREAK.search(text):
        tags.append("断词")
    for hit in _RE_CITE_INNER_SPACE.findall(text):
        if re.search(r"\s", hit.strip("[]")) or re.match(r"\[\s", hit):
            tags.append("引用空格")
            break
    if _RE_SPACE_BEFORE_DOT.search(text):
        tags.append("句点前空格")
    return tags


def _seam_tag(block: Any) -> str | None:
    """块的「栏间续段」标记（解析阶段盖的 `payload["seam"]`）。"""
    if block.payload.get("seam"):
        return "续段"
    return None


def _block_tags(block: Any, extra: set[str] | None = None) -> list[str]:
    """这一块上程序能量到的**全部**标签（字符串事实 + 解析阶段盖的几何戳）。

    单一来源：`read_blocks` 的 `flags`、`_suspect_pages` 的页级计数、
    `check_artifacts` 的明细都读它 —— 三处各写一遍迟早会漂（一个说这页有可疑、
    另一个列不出来）。

    `extra` 是**在 ①c 内部现算**的几何戳（目前只有"疑似表区"，见 `_table_regions`）：
    它们不是在 `parse.py` 里盖的（那样要涨 `PARSE_VERSION`、存量全失效），
    所以由调用方传进来。
    """
    text = block.en if block.type != "figure" else (block.payload.get("caption") or "")
    tags = _artifact_tags(text)
    if tag := _seam_tag(block):
        tags.append(tag)
    if extra and block.id in extra:
        tags.append("表格?")
    return tags


def _suspect_pages(doc: Doc, table_ids: set[str] | None = None) -> dict[str, int]:
    """`{页号: 可疑块数}` —— **只给页级计数**。

    ⚠️ 踩过：第一版把**全篇**可疑清单（`{页: {块: 标签}}`）塞进 seed，实测 10.5k 字符 ≈
    **2.7k tokens**，而 seed 是**每一轮都要重发**的 —— 37 页 78 轮 ≈ 白烧 21 万 tokens，
    换来的只是省掉一两轮探索。详情因此下移到**当页的工具结果**里（`read_blocks` 每块带
    `flags`、`check_artifacts` 给整页明细），它们只存在于"当前几轮"的上下文里。

    ⚠️ 计数里**必须带上「续段」**（2026-09-15 宿主：「还要校对分块分的对不对」）——
    它来自解析阶段量出的几何事实，页级计数是 agent 唯一能看出"这一页有栏间切缝"的入口；
    漏了它，`suspect_pages` 会给出"这页没问题"的**假阴性**。同理**表格**（㊴）：
    表区是 ①c 现算的几何事实，不并入计数就等于告诉 agent"这页没有表"。
    """
    counts: dict[str, int] = {}
    for b in doc.blocks:
        if _block_tags(b, table_ids):
            page = str(int(b.payload.get("page") or 0))
            counts[page] = counts.get(page, 0) + 1
    return counts


SYSTEM_PROMPT = """你是学术论文的**原文抽取校对 agent**。

程序已用坐标从 PDF 里抽出文本块（`id` / 类型 / 文本 / 位置）。PDF 原件在你手里，你可以把
任意一页（或一页里的一小块区域）渲染成图像来看。你的任务：**对照页面图像，逐页校对抽取结果，
并就地修正**。

## 必须核对的东西（宿主原话：所有能校对的都要校对）

- **文字**：行末断词（`vari- ous` → `various`）、连字、上下标、错字、多余空格（引用编号
  `[ 12 ]` → `[12]`、句点前空格）、被版面切碎的公式片段。
- **块结构**（宿主 2026-09-15：「还要校对**分块**分得对不对，尽量不要把一句话拆分到两个段里」）：
  一句话被拆成两块（应合并）、一段被并成一块（应切分）、重复块（应删除）。
  抽取常按**版面几何**切块，因此两栏页面上「左栏末块 + 右栏首块」很容易把**同一句话**
  劈成两个段落 —— 中文译文于是也断成两截。程序**已经把这种接缝量出来了**：
  `read_blocks` / `check_artifacts` 里带 `seam` 的块，就是「它前面那块是**左栏末块**」的
  右栏首块。对照页图判断：前块末尾**没有句末标点**（`. ! ? :` 等）而本块**从小写字母或
  左括号起** ⇒ 是同一句被切开 → `merge_block` 合并；前块正常收句 ⇒ 那只是正常的换栏，
  **不要动**。`merge_block` 只能并入**本页上一块**，正好就是这种情形。
  ⚠️ **页眉/页脚不在此列**（贴在页面上下边缘的刊物名、卷期、页码、DOI 那一行）：
  宿主 2026-09-17 明确「页眉文字不需要有意丢掉。和 pdf 尽量保持一致」——
  它们本就是原件的版面内容，**跨页重复也不许删**，更不许并入正文或升成标题。
- **块类型**：这一块到底是**标题**还是**段落**（`p` / `h2` / `h3` / `h4`）。
  判据是页图上的**字体、字号、加粗、是否独占一行**；标题/段落判错时用 `set_block_type` 改。
- **表格**（宿主 2026-09-16：「表格和原 pdf 差异较大」）：抽取器**根本不识别表格** ——
  它按阅读顺序吐行，于是**同一行里相邻栏的格子被粘成一句话**。表现是：`Naive Bayes (BN)`
  与 `Support vector machine (SVM)` 本属同一行的两格，却被写进同一块；
  表头行、行列归属也全丢了。这类块现在会带 `表格?` 标记（程序量出的疑似表区，判据是
  **横线 + 同一行上横着好几段文字**这两条同时成立，见 `check_artifacts` 的 `tables`
  与 `overview` 的每页 `tables` 计数）。
  **确认是表格后**（务必先 `read_page(page, region=…)` 放大看那一条带）用 `set_table` 重建：
  - `ids` = 组成这张表的那些块（同一页、按阅读顺序，**含表头行**）；提示里的 `blocks`
    只是程序按文字位置**推出来的线索**，别照抄 —— 用 `read_blocks(page)` 对一遍，
    缺表头行、混进正文段都不行；
  - `rows` = 网格，**第一行是表头**，一格一段文字；**格子里的字必须逐字来自这些块**
    （程序逐格核对，找不到就整份拒收 —— 不许你凭图默写、不许翻译、不许补全）；
  - 单元格里被换行切开的两截要接回同一格（`Convolutional Neural` + `Networks` → 一格）；
  - ⚠️ **一个格子可以横跨好几块**：抽取是按阅读顺序切块的，表格的一行常被切成几段
    （本块结尾 "…by integrating meas-" + 下一块开头 "ured and predicted data…" 是**同一格**）。
    程序是按你列进 `ids` 的块**拼起来**的一段文本逐格核对的 —— 跨块拼接没问题，
    行末断词的连字符也会自动接回，**别因为"这一格在两块里"就放弃这张表**；
    反过来，若某格的另一半在**没列进 ids** 的块里，工具会直接把块号告诉你，补上即可。
  - 表注（`Table 1. …`）单独放进 `caption` 参数，不要塞进格子当第一行。
  ⚠️ **反面例子（不要动）**：双栏排版的正文**不是**表格 —— 它读起来是通顺的句子、
  没有横线、没有列对齐。把整页正文当成一张 62×7 的大表是最典型的误判
  （`page.find_tables(strategy="text")` 就会这么干，所以程序没用那个 API）。
  另外：`表格?` 标记只是**嫌疑**（横线也可能是分栏线/页眉线），拿不准就**别改**。
- **阅读顺序**：这一页正确的读序（先上后下、先左栏后右栏、通栏块在其所在位置）。
- **笔记**：抽取**丢失**的内容（图里有的图标/编号/符号而未抽到）在 `finish` 的总结里说明，
  但**不要凭想象补写**到文本里。

## 样式与排版的一致性（宿主强调：这一条最重要）

校对的产物是**一篇读起来版式统一的文档**，不是一堆各自看着合理的块。所以判类型时**不要在单块上
孤立地看**，要拿它和全篇**同类**比 —— `read_blocks` 会把每块的 `type` 一起给你，比一比就知道：

- 正文的章节标题（`1 Introduction`、`2 Methods`…）是 h2，那么**无编号的具名小节**
  （`Abstract`、`Keywords`、`Acknowledgements`、`REFERENCES`）就是**同级 h2**；
- 上一级用 h2、下一级用 h3/h4 —— **同级的标题必须同级**，不许一个 h2 一个 h3；
- 图注、表注、作者行、单位行、页眉页脚、出版社水印**都不是标题**，别升成 h 级
  （把作者行或页脚变成 h2，大纲就会长出一堆假章节）。

**拿不准就别改**：层级一旦乱，大纲、章节归属、左右对照的排版全都跟着乱 ——
这比"某一块看着更像标题"严重得多。改类型用 `set_block_type`，**不要**用它当改文字的替代
（那用 `edit_block`），反之亦然。

## 纪律

1. **只改你真的看出来的错**。正确的块别动 —— 无谓改动会破坏译文对齐。
2. 文本必须**逐字来自论文**。不许改写、润色、翻译、补写看不清的内容。
3. 保留引用编号、图表编号、数字、单位、公式、大小写、标点的一切原样（只修抽取噪声）。
4. **看不清就放大**（`read_page(page, region=…)`），不要猜。
5. 每页核对完必须 `mark_page_done(page)`；全部完成后 `finish(summary)`。
   还有页没 `mark_page_done` 时 `finish` 会被拒绝，并告诉你还差哪几页。
6. 工具报错（护栏拒绝）说明你的改法不成立 —— 要么换个改法，要么承认这块没问题，别硬来。
7. **改文字与改类型是两件事**：`edit_block` 只改文字，`set_block_type` 只改类型；
   改类型时**一个字都不许动**（正文原样留在块里）。

## 省成本：每页两轮（这是硬要求）

上下文**每轮重发一次**，所以「轮数 = 成本」；一次调用里的多个工具只算一轮。所以：

- **第 1 轮（看）**：同一轮里把 `read_page(page)` + `read_blocks(page)` 一起调
  （想同时看放大区就一起调；需要的话再带上 `read_block`）。
- **第 2 轮（改）**：把这一页**所有** `edit_block` / `set_block_type` / `split_block` /
  `merge_block` / `set_table` / `delete_block` / `reorder_page` 和 `mark_page_done(page)`
  **放在同一轮里一次性提交**。

别把改动一个一个分轮提交（同一个块反复 `edit_block` 也算）。一页最多两轮；也可以一轮处理两页
（`read_page(1)` + `read_page(2)`，**一轮最多看两页**，再多会被服务器截掉）。确实需要复看时
才多花一轮，但要克制。

## 连字说明

`ﬁ`/`ﬂ` 这类连字与不换行空格**程序已经处理**，不用报。
"""


# ── 工具的类型（对齐 emrg 的 tool_types）──────────────────────────────────

@dataclass
class ToolSpec:
    """一个工具对模型的自述（名称 + 说明 + JSON Schema）。"""

    name: str
    description: str
    parameters: dict


@dataclass
class ToolOut:
    """工具执行结果。

    `images`：本次要投递给模型的渲染图（base64 PNG）。它们会在**全部**工具结果之后
    以一条 user 消息发出 —— 插在 tool 消息中间会破坏 `tool_calls` 与 `tool` 的配对。
    """

    text: str
    images: list[str] = field(default_factory=list)
    error: bool = False


@dataclass
class ProofreadStats:
    """本次校对的账本（进日志 + `doc.meta["proofread"]`）。"""

    pages: int = 0                     # 本次实际标记完成的页数
    skipped: int = 0                   # 上次已校对过 / 无文本 / 过长的页
    failed: int = 0                    # 调用或解析失败、重试用尽
    reordered: int = 0                 # 页数
    text_fixed: int = 0
    retyped: int = 0                   # 类型修订（`set_block_type`：标题 ↔ 段落）
    tabled: int = 0                    # 表格重建（`set_table`：几块 → 一张表）
    merged: int = 0
    split: int = 0
    dropped: int = 0
    rejected: int = 0                  # 被护栏拒绝的修改数
    rounds: int = 0                    # agent 轮数
    tool_calls: int = 0
    tokens: int = 0
    ok_pages: list[int] = field(default_factory=list)
    unreviewed: list[int] = field(default_factory=list)   # 结束时仍未校对的页
    notes: list[str] = field(default_factory=list)        # agent 记录的"抽取丢失"等观察
    stopped: str = ""                  # 非正常结束的原因（轮数/预算用尽）

    def summary(self) -> str:
        s = (f"{self.pages} 页 / 顺序修订 {self.reordered} 页 / 文字修订 {self.text_fixed} 块"
             f" / 类型修订 {self.retyped} 块 / 表格重建 {self.tabled} 张"
             f" / 合并 {self.merged} / 拆分 {self.split} / 去重 {self.dropped}"
             f" / 护栏拒绝 {self.rejected} / {self.rounds} 轮 {self.tool_calls} 次工具"
             f" / 失败 {self.failed} 页 / 跳过 {self.skipped} 页 / {self.tokens} tokens")
        if self.unreviewed:
            s += f" / ⚠️ 未核对 {len(self.unreviewed)} 页"
        if self.stopped:
            s += f" / ⚠️ {self.stopped}"
        return s


# ── 工具集（agent 的手脚）─────────────────────────────────────────────────

class ProofreadTools:
    """在**一份 `Doc`** 上工作的工具集：读页图 / 读抽取结果 / 改抽取结果。

    一个实例 = 一次会话。`specs()` 交给模型，`call()` 执行；所有写操作都过护栏。
    """

    def __init__(self, doc: Doc, pdf_path: str | Path, *, dpi: int = 130,
                 skip_pages: set[int] | None = None):
        self.doc = doc
        self.pdf_path = Path(pdf_path)
        self.dpi = dpi
        self.done_pages: set[int] = set(skip_pages or ())
        self.stats = ProofreadStats()
        self._src = None                       # 惰性打开（`fitz` 只在真要用图时载入）
        self._cache: dict[tuple, str] = {}     # (page, region, dpi) → base64
        self._tbl: dict[int, list[dict]] | None = None      # 页 → 疑似表区（几何现算）
        self._tbl_ids: set[str] | None = None               # 表区盖住的块号（缓存）
        self.finished = False

    # ── PDF ──────────────────────────────────────────────────────────────
    @property
    def src(self):
        if self._src is None:
            import fitz
            if not self.pdf_path.exists():
                raise FileNotFoundError(f"校对需要 PDF 原件（渲染页图）：{self.pdf_path}")
            self._src = fitz.open(self.pdf_path)
        return self._src

    def close(self) -> None:
        if self._src is not None:
            self._src.close()
            self._src = None

    @property
    def page_count(self) -> int:
        return self.src.page_count

    def _page_blocks(self, page: int) -> list[Block]:
        """按文档顺序取该页**当前**的块（会随切分/合并/删除实时变化）。"""
        return [b for b in self.doc.blocks if int(b.payload.get("page") or 0) == page]

    def _find(self, block_id: str) -> Block | None:
        return next((b for b in self.doc.blocks if b.id == block_id), None)

    # ── 疑似表区（几何，㊴）───────────────────────────────────────────────
    def table_regions(self, page: int) -> list[dict]:
        """该页的疑似表区（惰性算一次、按页缓存 —— 纯本地计算，不花 token）。"""
        if self._tbl is None:
            self._tbl = {}
        if page not in self._tbl:
            try:
                self._tbl[page] = _table_regions(self.src[page - 1], self._page_blocks(page))
            except Exception as exc:                           # noqa: BLE001 — 提示层失败不该毁掉校对
                log.debug("疑似表区几何计算失败（第 %d 页）：%s", page, exc)
                self._tbl[page] = []
        return self._tbl[page]

    @property
    def table_hint_ids(self) -> set[str]:
        """被表区盖住的块号（喂给 `_suspect_pages` 的页级计数与 `flags`）。"""
        if self._tbl_ids is None:
            out: set[str] = set()
            for page in range(1, self.page_count + 1):
                for reg in self.table_regions(page):
                    out.update(reg["ids"])
            self._tbl_ids = out
        return self._tbl_ids

    def _tags(self, b: Block) -> list[str]:
        """`_block_tags` + ①c 现算的几何戳（疑似表区；**已经是表格的块不再提示**）。"""
        return _block_tags(b, self.table_hint_ids if b.type != "table" else None)

    def _render(self, page: int, region=None, dpi: int | None = None) -> str:
        """渲染某页（或其中一块区域）为 base64 PNG。`region` 为页内**比例** [x0,y0,x1,y1]。"""
        dpi = dpi or self.dpi
        key = (page, tuple(region) if region else None, dpi)
        if key in self._cache:
            return self._cache[key]
        import fitz
        p = self.src[page - 1]
        rect = p.rect
        if region:
            x0, y0, x1, y1 = [float(v) for v in region]
            x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
            y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
            clip = fitz.Rect(rect.x0 + x0 * rect.width, rect.y0 + y0 * rect.height,
                             rect.x0 + x1 * rect.width, rect.y0 + y1 * rect.height)
            if clip.is_empty or clip.width < 4 or clip.height < 4:
                raise ValueError("region 太小或为空")
            # 裁出来的区域按"最长边不超过 MAX_CROP_PX"重新定 dpi（放大才看得清上下标）
            zoom = min(MAX_CROP_PX / max(clip.width, clip.height), 8.0)
            pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        else:
            pix = p.get_pixmap(dpi=dpi)
        b64 = base64.b64encode(_encode(pix)).decode()
        self._cache[key] = b64
        return b64

    # ── 工具定义 ──────────────────────────────────────────────────────────
    def specs(self) -> list[dict]:
        def f(name: str, description: str, properties: dict, required: list[str]) -> ToolSpec:
            return ToolSpec(name, description,
                            {"type": "object", "properties": properties, "required": required})

        page = {"type": "integer", "description": "页号，从 1 开始"}
        t = [
            f("overview", "看全篇规模：页数、块数，以及**每页的块数与字符数、是否已核对**。"
                          "开始校对前先调这个，决定从哪页看起。", {}, []),
            f("read_page", "把某页渲染成图像**给你自己看**（图会在下一条消息里）。"
                           "看不清细节（上下标、公式碎片、表格数字）时用 region 放大："
                           "region 是页内**比例** [x0,y0,x1,y1]，取值 0–1，"
                           "如 [0.05,0.05,0.5,0.12] 表示左上角那一块。",
              {"page": page, "region": {"type": "array", "items": {"type": "number"},
                                        "description": "页内比例 [x0,y0,x1,y1]，省略=整页"}},
              ["page"]),
            f("read_blocks", "读某页的抽取结果（块列表，文本有截断）。用 offset 分批翻页看后面的。",
              {"page": page, "offset": {"type": "integer", "description": "从第几块开始，默认 0"},
               "limit": {"type": "integer", "description": f"一次给几块，默认 {BLOCKS_PER_PAGE_CALL}"}},
              ["page"]),
            f("read_block", "读**单个块**的完整文本与位置（前面截断的用这个取全文）。",
              {"id": {"type": "string", "description": "块号，如 b-0012"}}, ["id"]),
            f("check_artifacts", "程序按字符串规则量出的**可疑片段**清单（行末断词、引用编号内空格、"
                                 "句点前空格…）。只列事实，**该不该改由你对照图像判定**。",
              {"page": page}, ["page"]),
            f("edit_block", "修正某块的**文字**（断词合并、多余空格、上下标、错字）。"
                            "text 必须是该块修正后的**完整文本**，且逐字来自论文。",
              {"id": {"type": "string"}, "text": {"type": "string"},
               "reason": {"type": "string", "description": "简述依据（你从图上看到了什么）"}},
              ["id", "text"]),
            f("set_block_type", "改某块的**类型**：它其实是标题还是段落、几级标题。"
                                "只在你对照页图**确认**了它被抽错时才改（加粗/独占一行/"
                                "与全篇同类标题样式一致）。文本一个字都不动。"
                                "h2 = 顶层章节（与 Abstract、Keywords、REFERENCES 同级），"
                                "h3 / h4 依次更深。h1 是文章标题（不在正文里，别用）。",
              {"id": {"type": "string"},
               "type": {"type": "string", "enum": ["p", "h2", "h3", "h4"],
                        "description": "p=普通段落；h2/h3/h4=标题层级"},
               "reason": {"type": "string", "description": "依据：图上什么字体/字号，和谁同级"}},
              ["id", "type"]),
            f("split_block", "把一块**切成多块**（它其实是多段被并在一起）。parts 是切好后的各段文本。",
              {"id": {"type": "string"},
               "parts": {"type": "array", "items": {"type": "string"},
                         "description": "切分后的各段文本（≥2 段，逐字来自论文）"},
               "reason": {"type": "string"}}, ["id", "parts"]),
            f("merge_block", "把某块**并入页内上一块**（它是上一块被切断的续写）。"
                             "两栏页面上最常见：`read_blocks` 里带 `seam` 的右栏首块 = 左栏末块"
                             "那句话的续写（先看页图确认前块没正常收句，再合并）。",
              {"id": {"type": "string"}, "reason": {"type": "string"}}, ["id"]),
            f("delete_block", "删除**重复**块（同一页里另有块已包含它的内容）。",
              {"id": {"type": "string"}, "reason": {"type": "string"}}, ["id"]),
            f("set_table", "把**表格**的若干块重建成一张真表格（解析器不会识别表格，它把这些行"
                           "按阅读顺序吐成段落，同一行里相邻栏的格子被粘成一句话）。"
                           "ids = 要消费掉的块号（同一页、连续的那几行，表头行也在里面）；"
                           "rows = 网格，**第一行是表头**，每个格子一行文字。"
                           "⚠️ 格子里必须是**你已经在这些块里读到的原文**（一字不改、不翻译、"
                           "不补全）：程序会逐格核对，找不到就整份拒收。"
                           "caption 填表注原文（如 `Table 1. Performance ...`）若不打算单独保留它。",
              {"ids": {"type": "array", "items": {"type": "string"},
                       "description": "被消费掉的块号（同一页；按阅读顺序）"},
               "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                        "description": "网格：第一行是表头；每行列数必须相同"},
               "caption": {"type": "string", "description": "表注原文，可空"},
               "reason": {"type": "string", "description": "依据：图上这是什么表、列怎么分的"}},
              ["ids", "rows"]),
            f("reorder_page", "重排某页的阅读顺序。ids 必须是该页**全部块号的一个排列**"
                              "（一个不多、一个不少）。",
              {"page": page, "ids": {"type": "array", "items": {"type": "string"}},
               "reason": {"type": "string"}}, ["page", "ids"]),
            f("mark_page_done", "声明**这一页已经核对完**（含『看过了，无需修改』）。"
                                "每页都要调一次；没调的页最后会被追问。",
              {"page": page, "note": {"type": "string", "description": "本页做了什么（可不填）"}},
              ["page"]),
            f("finish", "全部页核对完之后收尾。还有页没 mark_page_done 时会被拒绝并告诉你还差哪几页。",
              {"summary": {"type": "string", "description": "整体说明：改了什么、有哪些抽取丢失无法修改"}},
              []),
        ]
        return [{"type": "function",
                 "function": {"name": x.name, "description": x.description,
                              "parameters": x.parameters}} for x in t]

    # ── 执行 ──────────────────────────────────────────────────────────────
    def call(self, name: str, args: dict) -> ToolOut:
        self.stats.tool_calls += 1
        fn = getattr(self, f"_t_{name}", None)
        if fn is None:
            return ToolOut(f"未知工具 {name}。可用：{', '.join(self._tool_names())}", error=True)
        try:
            return fn(args or {})
        except Exception as exc:                          # noqa: BLE001 — 工具异常回给 agent 自己处理
            log.warning("工具 %s 执行失败：%s", name, exc)
            return ToolOut(f"{type(exc).__name__}: {exc}", error=True)

    def _tool_names(self) -> list[str]:
        return [s["function"]["name"] for s in self.specs()]

    # 读 ──────────────────────────────────────────────────────────────────
    def _t_overview(self, a: dict) -> ToolOut:
        pages = []
        for p in range(1, self.page_count + 1):
            bs = self._page_blocks(p)
            pages.append({"page": p, "blocks": len(bs),
                          "chars": sum(len(b.en) + len(b.payload.get("caption") or "") for b in bs),
                          "done": p in self.done_pages,
                          # 疑似表区数量（几何现算）：agent 靠它决定"先看哪几页"，
                          # 否则 37 页里那 4 张表全靠它自己扫出来（实测它扫不出来 —— 宿主
                          # 报缺陷时，agent 早就"核对完成"过那几页了）。
                          "tables": len(self.table_regions(p))})
        pending = [x["page"] for x in pages if x["blocks"] and not x["done"]]
        return ToolOut(json.dumps({
            "pdf_pages": self.page_count, "blocks": len(self.doc.blocks),
            "pages": pages, "pending_pages": pending,
            # 提醒 agent 先做什么，省得它盲猜（提示词里也说了，这里是就近提醒）
            "hint": "先 read_page 看图 + read_blocks 看抽取结果 + check_artifacts 看程序量出的可疑处，"
                    "再动手改，最后 mark_page_done。`tables>0` 的页请务必放大看那一条带。",
        }, ensure_ascii=False))

    def _t_read_page(self, a: dict) -> ToolOut:
        page = int(a.get("page") or 0)
        if not 1 <= page <= self.page_count:
            return ToolOut(f"页号超范围：1–{self.page_count}", error=True)
        region = a.get("region")
        b64 = self._render(page, region)
        where = f"第 {page} 页" + (f"（区域 {region}）" if region else "")
        return ToolOut(f"{where}已渲染，图像见下一条消息。", images=[b64])

    def _t_read_blocks(self, a: dict) -> ToolOut:
        page = int(a.get("page") or 0)
        bs = self._page_blocks(page)
        if not bs:
            return ToolOut(f"第 {page} 页没有文本块。")
        off = max(0, int(a.get("offset") or 0))
        lim = max(1, int(a.get("limit") or BLOCKS_PER_PAGE_CALL))
        out = []
        used = 0
        for b in bs[off:off + lim]:
            text = b.en if b.type != "figure" else (b.payload.get("caption") or "")
            item = {"id": b.id, "type": b.type, "chars": len(text),
                    "bbox": b.payload.get("bbox"),
                    "text": text[:BLOCK_PREVIEW] + ("…" if len(text) > BLOCK_PREVIEW else "")}
            if flags := self._tags(b):
                item["flags"] = flags                            # 就地带上程序量到的可疑点
            if b.payload.get("seam"):
                # 「续段」是**几何事实**：解析阶段量出这条栏间切缝可疑（前一块在左栏、
                # 末尾没有句末标点；本块在右栏、从小写起）。判"是不是同一句话"要看页图。
                item["seam"] = ("本块位于右栏开头，而它前一块是左栏末块 —— 疑似同一句话被"
                                "栏间切开的续段。看页图核对：若确认是同一句，用 merge_block 合并；"
                                "若前块本就正常收句，则不要动。")
            out.append(item)
            used += min(len(text), BLOCK_PREVIEW)
            if used >= BLOCKS_CALL_CHARS and len(out) < len(bs) - off:
                break                                          # 单次结果不超字符上限
        nxt = off + len(out)
        tail = len(bs) - nxt
        return ToolOut(json.dumps({"page": page, "total": len(bs), "from": off,
                                   "blocks": out,
                                   "more": f"还有 {tail} 块，用 offset={nxt} 继续看" if tail > 0 else ""},
                                  ensure_ascii=False))

    def _t_read_block(self, a: dict) -> ToolOut:
        b = self._find(str(a.get("id") or ""))
        if b is None:
            return ToolOut(f"没有块 {a.get('id')}", error=True)
        return ToolOut(json.dumps({"id": b.id, "page": b.payload.get("page"), "type": b.type,
                                   "bbox": b.payload.get("bbox"), "section": b.section,
                                   "text": b.en if b.type != "figure"
                                           else (b.payload.get("caption") or "")},
                                  ensure_ascii=False))

    def _t_check_artifacts(self, a: dict) -> ToolOut:
        page = int(a.get("page") or 0)
        bs = self._page_blocks(page)
        if not bs:
            return ToolOut(f"第 {page} 页没有文本块。")
        hints: dict[str, list[str]] = {}
        seams: dict[str, str] = {}
        hint_ids = self.table_hint_ids
        regions = self.table_regions(page)
        for b in bs:
            text = b.en if b.type != "figure" else (b.payload.get("caption") or "")
            if got := _artifacts(text):
                hints[b.id] = got
            if b.payload.get("seam"):
                seams[b.id] = "疑似被栏间切开的续段（前一块是左栏末块）：确认是同一句话就 merge_block"
            if b.id in hint_ids and b.type != "table":
                hints.setdefault(b.id, []).append("落在**疑似表区**里（横线 + 同一行并排文字量出来的）")
            if len(hints) + len(seams) >= MAX_HINT_BLOCKS:
                break
        tables = self._table_hint(regions)
        if not hints and not seams and not tables:
            return ToolOut(f"第 {page} 页没有程序能量出的可疑片段（但你仍需对照图像自行核对）。")
        return ToolOut(json.dumps({
            "page": page, "suspicious": hints, "seams": seams, "tables": tables,
            "note": "这些是程序量出的**事实**，不一定是错：行末断词要看图判断是断字（合并）"
                    "还是词内连字符（保留，如 three-dimensional、long- and short-term）；"
                    "`seams` 里的块要对照页图看**分块**对不对（跨栏一句话被切成两段 → 合并）；"
                    "`tables` 是程序量出的疑似表区（横线 + 同一行并排文字，两条同时成立），"
                    "用 read_page(page, region=…) 放大看那一条带 —— 确认是表格就用 set_table 重建。"
                    "每一条都要有结论：改就 edit_block / merge_block / set_table，不改就算了；",
        }, ensure_ascii=False))

    @staticmethod
    def _table_hint(regions: list[dict]) -> list[dict]:
        """表区提示：给 agent 一句人话 + 可直接用的放大 region。"""
        return [{
            "y": [r["y0"], r["y1"]], "blocks": r["ids"],
            "zoom": f"read_page(page, region={r['region']})",
            "hint": "这一段有表格横线、且同一行上横着好几段文字（表格的几何特征）："
                    "`blocks` 是程序按文字位置推出来的线索，仅供参考；先放大看这一条带，"
                    "再用 read_blocks(page) 逐块对一遍（含表头行），确认是同一张表的几行就用 "
                    "set_table 重建；若本来就是通顺的正文/公式，不要动。",
        } for r in regions]

    # 写 ──────────────────────────────────────────────────────────────────
    def _t_edit_block(self, a: dict) -> ToolOut:
        b = self._find(str(a.get("id") or ""))
        if b is None:
            return ToolOut(f"没有块 {a.get('id')}", error=True)
        new = clean_text(a.get("text")) if isinstance(a.get("text"), str) else ""
        ok, why = _acceptable_text_change(b.en, new)
        if not ok:
            self.stats.rejected += 1
            return ToolOut(f"修改被护栏拒绝：{why}。原文长度 {len(b.en)}，"
                           f"开头：{b.en[:80]!r}", error=True)
        old, b.en = b.en, new
        self.stats.text_fixed += 1
        log.info("  ✎ %s 文字修订：%r → %r（%s）", b.id, old[:60], new[:60], a.get("reason") or "")
        return ToolOut(f"已更新 {b.id}（{len(old)} → {len(new)} 字符）。")

    def _t_set_block_type(self, a: dict) -> ToolOut:
        """改块的**类型**（标题 ↔ 段落）—— 文字不动。

        宿主 2026-09-15：「agent 要告诉它保证样式和排版的一致性是很重要的」。
        所以护栏不只是"格式合法"，还要挡掉**会把层级搞乱**的改法：
        长段落不能变标题（那是 `split_block` 的活）、图片/参考文献条目不能变标题、
        `h1` 不许用（h1 是文章标题，由 meta 渲染，正文里出现会重复且污染大纲）。
        """
        b = self._find(str(a.get("id") or ""))
        if b is None:
            return ToolOut(f"没有块 {a.get('id')}", error=True)
        want = str(a.get("type") or "").strip().lower()
        if want not in _BLOCK_TYPES:
            self.stats.rejected += 1
            return ToolOut(f"类型 {want!r} 不接受。可用：p（段落）/ h2（顶层章节，与 Abstract、"
                           f"Keywords、REFERENCES 同级）/ h3 / h4。h1 是文章标题（由元数据渲染，"
                           f"正文里不要用）。", error=True)
        if b.type in ("figure", "refs"):
            self.stats.rejected += 1
            return ToolOut(f"{b.id} 是{b.type}块，不能改类型"
                           + ("（改图注文字用 `edit_block`）。" if b.type == "figure"
                              else "（REFERENCES 小标题已由程序标好）。"), error=True)
        text = (b.en or "").strip()
        if want != "p" and len(text) > _MAX_HEAD_CHARS:
            self.stats.rejected += 1
            return ToolOut(f"拒绝：这一块有 {len(text)} 字符，标题不该这么长 —— "
                           f"如果它其实是『标题 + 正文』被并在一起，先用 `split_block` 切开，"
                           f"再给标题那一段调类型。", error=True)
        if b.type == want:
            return ToolOut(f"{b.id} 已经是 {want}，无需改动。")
        old = b.type
        b.type, b.level = want, _BLOCK_TYPES[want]
        self.stats.retyped += 1
        log.info("  ⤴ %s 类型修订：%s → %s（%s）", b.id, old, want, a.get("reason") or "")
        return ToolOut(f"{b.id}：{old} → {want}（文字未动）。")

    def _t_split_block(self, a: dict) -> ToolOut:
        b = self._find(str(a.get("id") or ""))
        if b is None:
            return ToolOut(f"没有块 {a.get('id')}", error=True)
        parts = [clean_text(p).strip() for p in (a.get("parts") or []) if isinstance(p, str) and p.strip()]
        if len(parts) < 2:
            self.stats.rejected += 1
            return ToolOut("切分被拒绝：parts 至少两段非空文本。", error=True)
        ok, why = _acceptable_text_change(b.en, "\n\n".join(parts))
        if not ok:
            self.stats.rejected += 1
            return ToolOut(f"切分被护栏拒绝：{why}", error=True)
        pos = self.doc.blocks.index(b)
        b.en = parts[0]
        fresh = []
        for text in parts[1:]:
            # 逐个把"已分配的"传进去，否则多个新块会共用一个 ID（见 `_next_id` 说明）
            nb = Block(id=_next_id(self.doc, {n.id for n in fresh}), type=b.type, en=text,
                       section=b.section, level=b.level, payload=dict(b.payload))
            fresh.append(nb)
        for i, nb in enumerate(fresh):
            self.doc.blocks.insert(pos + 1 + i, nb)
        self.stats.split += 1
        log.info("  ⇤ %s 拆成 %d 块（%s）", b.id, len(parts), a.get("reason") or "")
        return ToolOut(f"{b.id} 已拆成 {len(parts)} 块，新块号：{[n.id for n in fresh]}。")

    def _t_merge_block(self, a: dict) -> ToolOut:
        b = self._find(str(a.get("id") or ""))
        if b is None:
            return ToolOut(f"没有块 {a.get('id')}", error=True)
        page_bs = self._page_blocks(int(b.payload.get("page") or 0))
        pos = page_bs.index(b) if b in page_bs else -1
        prev = page_bs[pos - 1] if pos > 0 else None
        if prev is None or prev.type == "figure":
            self.stats.rejected += 1
            return ToolOut("合并被拒绝：这一页里没有可并入的前一块（它是本页第一块）。", error=True)
        tail_prev, head_b = prev.en.rstrip(), b.en.lstrip()   # 合并前留证据（合并后就分不出来了）
        prev.en = clean_text((tail_prev + " " + head_b).strip())
        self.doc.blocks.remove(b)
        self.stats.merged += 1
        log.info("  ⇥ %s 并入 %s（%s）", b.id, prev.id, a.get("reason") or "")
        # ⚠️ **软提醒，不是拒绝**：合并是**不可逆的语义决定**，而"这到底是不是同一句话"
        # 只有看图的人（agent）知道。这里只用**和标记同一套判据**复量一遍：
        # 前块已收句 + 本块大写起 ⇒ 不太像同一句 —— 那时把话说清（怎么拆回来），
        # 而不是硬拦（硬拦会让 agent 在真正需要合并时无路可走：没有别的工具能连两块）。
        out = f"{b.id} 已并入 {prev.id}。"
        if not _looks_like_continuation(tail_prev, head_b):
            out += (" ⚠️ 程序复量后**不太像**同一句（前一块读作已收句、或本块以大写起）。"
                    "你若是有意合并就忽略这条；若是并错了，用 `split_block` 拆回两段。")
        return ToolOut(out)

    def _t_set_table(self, a: dict) -> ToolOut:
        """把若干小块重建成**一张表格块**（决策㊴，宿主 2026-09-16：「表格和原 pdf 差异较大」）。

        ## 为什么是 agent 干这件事
        解析器（`parse.py`）零表格识别 —— 它按阅读顺序吐行，于是同一行里相邻栏的格子
        被粘成一句话（`Naive Bayes (BN) Support vector machine (SVM)`），列关系全丢。
        程序侧的候选方案都试过：`page.find_tables()` 三种策略全抓不到这种**有横线没竖线**
        的表；`strategy="text"` 会把双栏正文当成 62×7 的大表（这正是提示词里的反例）。
        而 agent 本来就在逐页看图，判"哪几行是一张表、列怎么分、哪些格要合并"是它的强项。

        ## 但**认字**不交给它
        图只用来判结构；格子里必须填它已经在块里读到的字（文本层是零 OCR 误差的）。
        三条护栏（逐字来源 / 形状 / 不吃正文）各自对应一种可判定的坏结果，见模块里
        `_table_placeable` 与 `_table_residue` 的说明。
        """
        ids = [str(x) for x in (a.get("ids") or [])]
        ids = list(dict.fromkeys(ids))                    # 去重且保序（顺序=阅读顺序）
        if len(ids) < 2:
            self.stats.rejected += 1
            return ToolOut("建表被拒绝：ids 至少要两块（一行一列的「表」其实是段落，用 edit_block）。",
                           error=True)
        blocks: list[Block] = []
        for i in ids:
            b = self._find(i)
            if b is None:
                self.stats.rejected += 1
                return ToolOut(f"建表被拒绝：没有块 {i}", error=True)
            blocks.append(b)
        pages = {int(b.payload.get("page") or 0) for b in blocks}
        if len(pages) != 1:
            self.stats.rejected += 1
            return ToolOut(f"建表被拒绝：ids 跨了 {sorted(pages)} 页。跨页表请**按页分开建**"
                           f"（每页一张），程序不替你把两页的表拼起来。", error=True)
        for b in blocks:
            if b.type in ("figure", "table"):
                self.stats.rejected += 1
                return ToolOut(f"建表被拒绝：{b.id} 是 {b.type} 块，不能作为表格的一行"
                               + ("（它已经是表格了）。" if b.type == "table" else "。"), error=True)

        rows_in = a.get("rows")
        shape = grid_shape(rows_in)
        if not shape:
            self.stats.rejected += 1
            return ToolOut("建表被拒绝：rows 必须是**等宽的字符串二维数组**"
                           "（每行列数相同、第一行是表头）。", error=True)
        n_rows, n_cols = shape
        if n_rows < 2 or n_cols < 2:
            self.stats.rejected += 1
            return ToolOut(f"建表被拒绝：{n_rows} 行 × {n_cols} 列 —— 至少 2 行 2 列才叫表格；"
                           f"单行/单列的内容请用 edit_block 或 split_block 处理。", error=True)
        if n_rows > _TABLE_MAX_ROWS:
            self.stats.rejected += 1
            return ToolOut(f"建表被拒绝：{n_rows} 行太多了（多半是把整页正文当成了表）。", error=True)
        rows = [[clean_text(c).strip() for c in row] for row in rows_in]
        caption = clean_text(a.get("caption")).strip() if isinstance(a.get("caption"), str) else ""
        if not any(c for row in rows for c in row):
            self.stats.rejected += 1
            return ToolOut("建表被拒绝：整张表都是空格子。", error=True)

        src = "\n".join(b.en for b in blocks)
        placed = [c for row in rows for c in row] + ([caption] if caption else [])
        # ① 逐字来源：每个格子的文字必须能在**被消费的块拼起来**的文本里找到
        missing = [c for c in [x for row in rows for x in row if x.strip()]
                   if not _table_placeable(src, c)]
        if missing:
            self.stats.rejected += 1
            # ⚠️ 拒绝信息必须**指得出地方**（2026-09-16 实测）：最常见的失手不是"看图默写"，
            # 而是**漏列了 ids** —— 那一格的另一半在别的块里（`ids` 少了一个 b-0211 之类的）。
            # 只说"找不到"，agent 就只能瞎猜或放弃；指出块号它一次就能补上。
            # 判据 = 「把这个块也拼进来，这一格就凑齐了」（**不是**"这个块里有这串字"：
            # 一格横跨两块时，任何**单块**里都找不到完整的它）。
            hints = []
            for m in missing[:3]:
                fixers = [b.id for b in self._page_blocks(next(iter(pages)))
                          if b not in blocks and _table_placeable(src + "\n" + b.en, m)]
                if fixers:
                    hints.append(f"「{m[:36]}」再补 {'/'.join(fixers[:3])} 就凑齐了")
            tip = (" —— 这些字**没列进 ids**：" + "；".join(hints) + "。"
                   "把它们也列进 ids 就行（别照抄，先 read_blocks 对一遍）") if hints else (
                   "。请逐字复制块里的原文；若缺字，先用 edit_block 把该块改对")
            return ToolOut("建表被拒绝：这些格子的文字在源块里**找不到**"
                           f"（不能凭记忆/看图默写）：{[m[:40] for m in missing[:5]]}{tip}",
                           error=True)
        # ③ 不吃正文：拼起来的源文本里不许有成句的文字没被装进格子
        left = _table_residue(src, placed)
        if left:
            self.stats.rejected += 1
            where = _blocks_holding(blocks, left)
            return ToolOut(
                f"建表被拒绝：这些块拼起来还有没进格子的文字：{left[:8]}"
                + (f"（出现在 {'、'.join(where)}）" if where else "")
                + "。要么把它们也填进对应格子 —— ⚠️ **一个格子的字可以横跨好几块**"
                  "（本块末尾 + 下一块开头常常是同一格，行末断词的连字符会自动接回，"
                  "程序按这些块**拼起来**的文本核对）；"
                  "要么**别把它们列进 ids**（表注用 caption 传，表外脚注留在原块里别动）。",
                error=True)

        first = blocks[0]
        pos = self.doc.blocks.index(first)
        nb = Block(id=_next_id(self.doc), type="table",
                   en=table_text(rows, caption), section=first.section,
                   payload={"rows": rows, "caption": caption, "src_ids": ids,
                            "page": int(first.payload.get("page") or 0),
                            "bbox": first.payload.get("bbox")})
        # ⚠️ **先插后删**（顺序不能反）：若先删掉那几个块，`pos` 就已经不是它原来的位置了
        # （被消费的块里若有排在被消费序列之前的…实测会把表格块甩到页尾 —— 阅读顺序错乱，
        # 而且错得很隐蔽：表格还在，只是跑到别的段落后面去了）。
        self.doc.blocks.insert(pos, nb)
        for b in blocks:
            self.doc.blocks.remove(b)
        self.stats.tabled += 1
        log.info("  ▦ %s → 表格块 %s（%d×%d，吃掉 %s，%s）", first.id, nb.id, n_rows, n_cols,
                 ",".join(ids), a.get("reason") or "")
        return ToolOut(f"已重建表格：{len(ids)} 块 → 一个表格块 **{nb.id}**（{n_rows} 行 × {n_cols} 列）。"
                       f"⚠️ 这一页的块号已变化，后面引用请用新块号。中文译文由后续翻译步骤补上"
                       f"（不必你译）；原块 {ids} 已删除。")

    def _t_delete_block(self, a: dict) -> ToolOut:
        b = self._find(str(a.get("id") or ""))
        if b is None:
            return ToolOut(f"没有块 {a.get('id')}", error=True)
        norm = _norm(b.en)
        # 去重必须**可判定**：本块文本（归一化后）要真的被另一个块包含，否则就是误报。
        if len(norm) < 15 or not any(o is not b and norm in _norm(o.en) for o in self.doc.blocks):
            self.stats.rejected += 1
            return ToolOut("删除被拒绝：全篇找不到包含该块内容的块，疑似误报。", error=True)
        self.doc.blocks.remove(b)
        self.stats.dropped += 1
        log.info("  ⌫ %s 判为重复块，已移除（%s）", b.id, a.get("reason") or "")
        return ToolOut(f"{b.id} 已删除。")

    def _t_reorder_page(self, a: dict) -> ToolOut:
        page = int(a.get("page") or 0)
        live = self._page_blocks(page)
        ids = [str(x) for x in (a.get("ids") or [])]
        if sorted(ids) != sorted(b.id for b in live):
            self.stats.rejected += 1
            return ToolOut("重排被拒绝：ids 必须是本页全部块号的一个排列。本页块号："
                           f"{[b.id for b in live]}", error=True)
        index = {b.id: b for b in live}
        new_seq = [index[i] for i in ids]
        if new_seq == live:
            return ToolOut("顺序与当前一致，无需改动。")
        pos = self.doc.blocks.index(live[0])
        rest = [b for b in self.doc.blocks if b not in live]
        self.doc.blocks[:] = rest[:pos] + new_seq + rest[pos:]
        self.stats.reordered += 1
        log.info("  ⇅ 第 %d 页顺序修订（%s）", page, a.get("reason") or "")
        return ToolOut(f"第 {page} 页顺序已更新。")

    # 收尾 ────────────────────────────────────────────────────────────────
    def _t_mark_page_done(self, a: dict) -> ToolOut:
        page = int(a.get("page") or 0)
        if not 1 <= page <= self.page_count:
            return ToolOut(f"页号超范围：1–{self.page_count}", error=True)
        self.done_pages.add(page)
        if a.get("note"):
            self.stats.notes.append(f"第 {page} 页：{a['note']}")
        left = self.pending_pages
        return ToolOut(f"第 {page} 页已标记完成。" + (f"还剩 {left} 页未核对。" if left else
                                                    "全部页已核对，可以 finish 了。"))

    def _t_finish(self, a: dict) -> ToolOut:
        left = self.pending_pages
        if left:
            # 拒绝是**故意的**：宿主的要求是"所有能校对的都要校对"，漏页不算完成。
            return ToolOut(f"还有 {len(left)} 页没核对：{left}。请先处理它们再 finish。", error=True)
        self.finished = True
        if a.get("summary"):
            self.stats.notes.append(str(a["summary"]))
        return ToolOut("收尾完成。")

    @property
    def pending_pages(self) -> list[int]:
        """还有内容但尚未标记完成的页。"""
        return [p for p in range(1, self.page_count + 1)
                if p not in self.done_pages and self._page_blocks(p)]


# ── 校对 agent 本体 ───────────────────────────────────────────────────────

class Proofreader:
    """驱动一个**校对 agent**：给它工具，让它自己看图、读抽取结果、改抽取结果。"""

    def __init__(self, cfg, *, dpi: int = 130, max_tokens: int = 32000,
                 max_rounds: int = 120, token_budget: int = 600_000, retries: int = 2,
                 thinking: str = ""):
        self.cfg = cfg
        self.thinking = thinking
        self.dpi = dpi
        self.max_tokens = max_tokens
        self.max_rounds = max_rounds
        self.token_budget = token_budget
        self.retries = retries
        self.tools: ProofreadTools | None = None
        self.stats = ProofreadStats()
        self._log_fn = None

    # ── 一次 LLM 调用（带工具）────────────────────────────────────────────
    def _chat(self, messages: list[dict], tools: list[dict]) -> dict:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        _apply_thinking(payload, self.thinking)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.cfg.api_key}",
                   "Content-Type": "application/json"}
        # 请求体闸门：服务端上限实测 1MB，而一张整页图 ~300KB —— 几轮下来必撞 413。
        # 剪图**不丢信息**（渲染有缓存，模型想再看就再 read_page），所以宁可剪也不要崩。
        dropped = _fit_images(messages)
        if dropped:
            log.info("①c 请求体偏大，从最旧的图开始剪掉 %d 张（模型可重新 read_page）", dropped)
        data = self._post(payload, headers)
        payload.pop("_retried", None)
        usage = data.get("usage") or {}
        self.stats.tokens += int(usage.get("total_tokens") or 0)
        return data["choices"][0]

    def _post(self, payload: dict, headers: dict) -> dict:
        """发一次请求，**失败的调用要重试**（这是整篇成败所在）。

        ## 为什么必须重试（2026-09-14 实测，真跑 37 页时被咬）

        一篇 37 页、已改 173 块的校对跑到第 11 分钟，上游网关回了一次
        **504 Gateway Time-out** —— 而 `retries` 参数当时是个**从未被使用的摆设**，
        异常一路冒到 `converter` 的兜底 `except`，整篇校对**归零**（11 分钟、几十万 token 白烧）。
        长任务里"一次瞬时故障 = 全部重来"是最贵的失败模式，必须在这里挡住。

        `429` / `5xx` / 超时 / 连接错误 → 退避重试；`4xx`（除 413）是请求本身的问题，直接抛。
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                with httpx.Client(timeout=self.cfg.timeout) as client:
                    resp = client.post(f"{self.cfg.base_url}/chat/completions", json=payload,
                                       headers=headers)
                    if resp.status_code in (413, 502) and not payload.get("_retried"):
                        # 413 回的是 **HTML 错误页**（不是 JSON）—— 不先兜住的话 `resp.json()`
                        # 会把整篇校对炸掉。这里再剪一轮然后重试一次。
                        payload["_retried"] = True
                        log.warning("①c 服务端 %d（请求体过大）→ 剪掉全部历史图后重试",
                                    resp.status_code)
                        _fit_images(payload["messages"], budget=200_000)
                        continue
                    if RETRY_STATUS.match(str(resp.status_code)):
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}（上游瞬时故障）", request=resp.request,
                            response=resp)
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as exc:
                if not RETRY_STATUS.match(str(exc.response.status_code)):
                    raise                                  # 4xx（除 429）是请求本身的问题
                if attempt > self.retries:
                    log.error("①c 调用失败 %d 次，放弃：%s", attempt - 1, exc)
                    raise
                wait = RETRY_BACKOFF ** attempt
                log.warning("①c 调用失败（%s）→ %.1fs 后重试（第 %d/%d 次）",
                            exc, wait, attempt, self.retries)
                time.sleep(wait)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt > self.retries:
                    log.error("①c 调用失败 %d 次，放弃：%s", attempt - 1, exc)
                    raise
                wait = RETRY_BACKOFF ** attempt
                log.warning("①c 调用异常（%s）→ %.1fs 后重试（第 %d/%d 次）",
                            exc, wait, attempt, self.retries)
                time.sleep(wait)

    # ── 整篇 ──────────────────────────────────────────────────────────────
    def proofread_doc(self, doc: Doc, pdf_path: str | Path, *, skip_pages=(),
                      log_fn=None) -> ProofreadStats:
        """让 agent 校对整篇 `doc`（**就地修改** blocks）。返回统计。"""
        skip = {int(p) for p in skip_pages or ()}
        tools_impl = ProofreadTools(doc, pdf_path, dpi=self.dpi, skip_pages=skip)
        self.tools = tools_impl
        # ⚠️ 账本**只有一个**：工具层在改，循环层在读 —— 各持一份会立刻对不上
        # （踩过：`text_fixed` 记在工具那份里，日志里却永远是 0）。
        self.stats = tools_impl.stats
        self.stats.skipped = len(skip)
        self._log_fn = log_fn
        try:
            messages = self._seed(doc, skip)
            self._run(messages, tools_impl)
        except Exception as exc:                             # noqa: BLE001
            # ⚠️ 中断也要**交账**：已改的块已经落在 `doc` 上、已核对的页在 `done_pages` 里 ——
            # 直接抛出去会让调用方（converter）连"改到哪了"都不知道，下次重跑又从第 1 页开始
            # （见 `_post` 里 504 那次：11 分钟、几十万 token 全白烧）。
            self.stats.failed += 1
            self.stats.stopped = f"中断（{type(exc).__name__}: {exc}）"
            log.error("①c 校对中断，保留已完成的 %d 页：%s", len(tools_impl.done_pages), exc)
        finally:
            tools_impl.close()

        st = self.stats
        st.pages = len(tools_impl.done_pages - skip)
        st.ok_pages = sorted(tools_impl.done_pages)
        st.unreviewed = tools_impl.pending_pages
        if not tools_impl.finished and not st.stopped:
            st.stopped = "agent 未调用 finish"
        return st

    # ── 会话初始化 ────────────────────────────────────────────────────────
    def _seed(self, doc: Doc, skip: set[int]) -> list[dict]:
        pages = sorted({int(b.payload.get("page") or 0) for b in doc.blocks})
        seed = {
            "pdf_pages": self.tools.page_count,
            "blocks": len(doc.blocks),
            "pages_with_blocks": pages,
            "block_types": _type_counts(doc),
            "already_reviewed_pages": sorted(skip) or None,
            "title": (doc.meta.get("title") or doc.meta.get("title_en") or "")[:200] or None,
            # 只给**页级计数**（全篇明细放 seed 会每轮重发，见 `_suspect_pages` 的说明）
            "suspect_pages": _suspect_pages(doc, self.tools.table_hint_ids) or None,
        }
        task = (f"这是《{seed['title'] or '未命名论文'}》的抽取结果概览：\n"
                + json.dumps(seed, ensure_ascii=False)
                + "\n\n请开始逐页校对。")
        if skip:
            task += (f" 注意：第 {sorted(skip)} 页**上次已经校对过**，本次不用再看"
                     f"（但如果它们的顺序/结构影响了本页的判断，你仍然可以 read_page 复查）。")
        task += (" `suspect_pages` 是程序量出的**页级可疑计数**（键是页号，值是可疑块数）："
                 "具体是哪些块、可疑在哪，`read_blocks` 的每块 `flags` 字段里就有（`check_artifacts` "
                 "给整页明细，`overview` 里每页还有 `tables` = 疑似表区数）。"
                 "⚠️ 计数里**混着三类**：字符串规则的（断词/引用空格）、"
                 "**几何的**「续段」（被栏间切开的续段，一句话分成两块，"
                 "`flags` 里显示为 `续段`、说明在 `read_blocks` 的 `seam` 字段）、"
                 "以及**几何的**「表格?」（落在横线表区里的块 —— 抽取器不会识别表格，"
                 "这些行多半是「同一行相邻栏被粘成一句话」，要用 `set_table` 重建）。"
                 "这两类几何戳都必须**对照页图**才能下结论。按「每页两轮」做：一轮看图看块，"
                 "一轮把全部改动 + mark_page_done 提交。")
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]

    # ── agent 主循环 ──────────────────────────────────────────────────────
    def _run(self, messages: list[dict], impl: ProofreadTools) -> None:
        nudges = 0
        page_nudges = 0
        since_done = 0
        while True:
            if self.stats.rounds >= self.max_rounds:
                self.stats.stopped = f"轮数用尽（{self.max_rounds} 轮）"
                log.warning("①c 校对轮数用尽（%d 轮），停止", self.max_rounds)
                return
            if self.stats.tokens >= self.token_budget:
                self.stats.stopped = f"token 预算用尽（{self.stats.tokens}/{self.token_budget}）"
                log.warning("①c 校对 token 预算用尽（%d），停止", self.stats.tokens)
                return

            self.stats.rounds += 1
            before = self.stats.tokens
            body = _body_size(messages)
            choice = self._chat(messages, impl.specs())
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
            # 每轮一行账：**成本可见**才能调（哪一轮贵、贵在上下文还是贵在工具调用）。
            log.info("①c 第 %d 轮：上下文 %.1fKB / 本轮 %d tokens / %d 个工具 %s",
                     self.stats.rounds, body / 1024, self.stats.tokens - before, len(calls),
                     ",".join((tc.get("function") or {}).get("name", "?") for tc in calls)[:80])
            text = (msg.get("content") or "").strip()
            if text:
                log.debug("①c agent 第 %d 轮说：%s", self.stats.rounds, text[:160])

            if not calls:
                # 模型直接停下（没调工具）—— 若还有页没核对，就追问；否则视作结束
                left = impl.pending_pages
                if not left or nudges >= MAX_NUDGES:
                    if left:
                        self.stats.stopped = f"agent 停手，仍有 {len(left)} 页未核对"
                        log.warning("①c agent 停手，仍有未核对页：%s", left)
                    return
                nudges += 1
                messages.append({"role": "assistant", "content": msg.get("content")})
                messages.append({"role": "user", "content": _nudge(left)})
                log.info("①c 追问（第 %d 次）：还有 %d 页未核对", nudges, len(left))
                continue

            # 工具轮：assistant(tool_calls) → 每个工具一条 tool 消息 → 图集中成一条 user 消息
            messages.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": calls})
            images: list[str] = []
            page_done = False
            for tc in calls:
                name = (tc.get("function") or {}).get("name") or ""
                raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                args = _load_args(raw_args)
                out = impl.call(name, args)
                if out.images:
                    images.extend(out.images)
                if name == "mark_page_done" and not out.error:
                    page_done = True
                # ⚠️ **护栏拒绝要进日志**（`error=True` 走 INFO，附工具参数）：
                # 只数个数（"护栏拒绝 5"）在事后是**不可诊断**的 —— 生产实测就卡在这里
                # （2026-09-16：真跑一遍只重建出 1 张表，想知道另外几张为什么没成，
                # 日志里只有计数、没有原因，只能重跑一遍）。可判定的坏结果必须留下判据。
                if out.error:
                    log.info("①c 工具 %s(%s) 被拒绝：%s", name,
                             json.dumps(args, ensure_ascii=False)[:400], out.text[:300])
                log.debug("①c 工具 %s(%s) → %s", name, json.dumps(args, ensure_ascii=False)[:120],
                          out.text[:120])
                messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                                 "content": out.text})
            if images:
                # ⚠️ 图必须跟在**所有** tool 消息之后：插在中间会破坏 tool_calls ↔ tool 配对
                messages.append({"role": "user", "content": _image_message(images)})
                _prune_images(messages)
            if page_done:
                self._compact(messages, impl)
                since_done = 0                                   # 新一轮"每页两轮"计时
            elif impl.finished:
                return                                           # 收尾了就别再问一轮
            else:
                # 硬护栏：提示词说了"每页两轮"，但模型不一定听。**轮数就是钱**，
                # 所以超了就当场追问一次，把"继续摸索"掐掉（只提醒，不打断它的工具调用）。
                since_done += 1
                if (since_done >= MAX_ROUNDS_PER_PAGE and impl.pending_pages
                        and page_nudges < MAX_PAGE_NUDGES):
                    page_nudges += 1
                    since_done = 0
                    messages.append({"role": "user", "content": _hurry(impl.pending_pages)})
                    log.info("①c 本页已花 %d 轮未收尾，催办（第 %d 次）",
                             MAX_ROUNDS_PER_PAGE, page_nudges)

    # ── 上下文压缩（页面边界）──────────────────────────────────────────────
    def _compact(self, messages: list[dict], impl: ProofreadTools) -> None:
        """一页核对完就把它的往返记录压成一行 —— 不动**当前页**的历史。

        ## 为什么必须压（实测：3 页烧掉 41 万 tokens）

        agent 的消息是**每轮重发**的，所以上下文长度≈成本。35 轮下来，第 1 页那些
        "读图 → 读块 → 改 → 确认"的往返被重发了 30 多次，而校对第 3 页时它们**毫无用处**：
        改动早已落在 `doc` 上（工具层持有），模型需要知道的只是"那一页做了什么、还差什么"。
        压缩后同一篇从 41 万降到十万量级。

        安全性：`system` 与任务说明保留；摘要里只写**已完成页的结论**；被剪掉的历史**不是状态**
        （状态在 doc 里），模型若想复查还能 `read_page` / `read_blocks` 重新查。
        """
        if len(messages) <= KEEP_TAIL_MESSAGES + 3:
            return
        cut = len(messages) - KEEP_TAIL_MESSAGES
        while cut < len(messages) and messages[cut].get("role") == "tool":
            cut += 1                                           # 不留下孤儿 tool 消息
        done = sorted(impl.done_pages)
        stat = impl.stats
        summary = (f"[进度摘要·自动生成] 已核对完成的页：{done}（共 {impl.page_count} 页）。"
                   f"累计：文字修订 {stat.text_fixed} 块 / 类型修订 {stat.retyped} 块"
                   f" / 合并 {stat.merged} / 拆分 {stat.split}"
                   f" / 去重 {stat.dropped} / 顺序修订 {stat.reordered} 页 / 护栏拒绝 {stat.rejected}。"
                   f"这些改动**已经落库**，不必重做；上面被省略的历史只是那几页的工具往返记录，"
                   f"需要复查就重新 read_page / read_blocks。还没核对的页：{impl.pending_pages}。")
        messages[:] = messages[:2] + [{"role": "user", "content": summary}] + messages[cut:]
        log.debug("①c 上下文压缩：保留最近 %d 条 + 进度摘要", len(messages) - 3)


MAX_NUDGES = 3
# "每页两轮"的硬护栏：连着这么多轮没有 mark_page_done 就催一次。
# 提示词里写了纪律，但**提示词不是护栏** —— 实测模型会一轮一个改动地磨（3 页磨到 16 轮）。
MAX_ROUNDS_PER_PAGE = 3
MAX_PAGE_NUDGES = 8
# 页边界压缩后保留的尾部消息条数（覆盖"当前页正在做的这件事"）。
# ⚠️ 这个数字直接乘进成本：`read_blocks` 的结果（一页几 KB）会**在后面每一轮重发**，
# 留 14 条 ≈ 7 轮的工具往返。实测参考页（大块拆成一堆小块）就是被这里放大的：
# 一页 34 块 → 5 次调用 → 5 条几 KB 的结果被反复重发 → 21.7k tokens/轮。
KEEP_TAIL_MESSAGES = 8
# 瞬时故障重试：上游网关 429/5xx/超时/断连都重试，退避 2s / 4s / 8s…（共 `retries` 次）
RETRY_STATUS = re.compile(r"^(429|5\d\d)$")
RETRY_BACKOFF = 2.0


def _nudge(left: list[int]) -> str:
    return (f"你还没有 mark_page_done 这些页：{left}。宿主的要求是**所有能校对的都要校对**，"
            f"不能漏页。请继续：read_page 看图 → read_blocks 看抽取结果 → check_artifacts 看"
            f"程序量出的可疑处 → 必要时改 → mark_page_done，逐页做完再 finish。")


def _hurry(left: list[int]) -> str:
    return (f"⚠️ 你已经连着 {MAX_ROUNDS_PER_PAGE} 轮没有 mark_page_done 了 —— 上下文每轮重发一次，"
            f"**轮数就是成本**。现在：把这一页已知的全部修改（edit_block / split_block / merge_block / "
            f"delete_block / reorder_page）和 mark_page_done **放在同一轮里一次提交**，然后立刻做下一页"
            f"（一轮里可以同时 read_page 好几页）。还有 {len(left)} 页没核对：{left}。")


def _image_message(images: list[str]) -> list[dict]:
    content: list[dict] = [{"type": "text",
                            "text": f"[工具 read_page 的返回：{len(images)} 张页面渲染图，见下]"}]
    for b64 in images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return content


def _prune_images(messages: list[dict]) -> None:
    """只留最近一轮的图，更早的换成一行说明（真正的体积闸门是 `_fit_images`）。

    ## 为什么必须剪（实测 413）

    图片一旦进了 `messages` 就会**每一轮都重发**：一张 130dpi 整页图 ~300KB，几轮下来
    请求体越过服务端 1MB 上限，直接 `413 Request Entity Too Large` —— 而服务端回的是
    **HTML 错误页**，`resp.json()` 会炸掉整篇校对。
    """
    holders = [m for m in messages if isinstance(m.get("content"), list)
               and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])]
    # 只留最近一轮的前 MAX_IMAGES_PER_ROUND 张，其余交给 _fit_images 按体积收尾
    flat = [(m, p) for m in holders for p in m["content"]
            if isinstance(p, dict) and p.get("type") == "image_url"]
    keep = {id(p) for _, p in flat[-MAX_IMAGES_PER_ROUND:]}
    for m in holders:
        kept = [p for p in m["content"]
                if not (isinstance(p, dict) and p.get("type") == "image_url") or id(p) in keep]
        if any(isinstance(p, dict) and p.get("type") == "image_url" for p in kept):
            m["content"] = kept
        else:
            head = next((p.get("text", "") for p in kept if isinstance(p, dict)), "")
            m["content"] = (f"{head} ⚠️ 渲染图已从上下文移除（控制请求体大小）；"
                            f"需要再看就重新调用 read_page（命中渲染缓存）。")


# ── 表格重建的护栏（决策㊴）───────────────────────────────────────────────
# 字符**只能来自文本层**：agent 的图只用来判结构（哪几行是一张表、列边界、合并格），
# 格子里的字必须是它已经在块里读到的那些字。这三条护栏把"看图认字"这条错路堵死：
#   ① 逐字来源：每个格子的文字必须能在被消费的源块文本里找到（找不到 = 它在凭记忆默写）；
#   ② 形状：等宽二维表、至少 2 行 2 列（一行一列的东西不是表，是段落）；
#   ③ 不吃正文：源块里不许有"没被装进格子"的成句文字（表注除外，它是 `caption`）。
_TABLE_MAX_ROWS = 80              # 一张表最多这么多行（超了多半是把正文当表了）
_TABLE_MIN_RESIDUE_WORDS = 4      # 源块里剩这么多实词 = 有字没进格子 → 拒收


def _flat(text: str) -> str:
    """匹配用的归一化：空白压平 + **去掉行末断词的连字符**。

    两处都要，否则会把**正确**的重建判成编造：PDF 里 "Convolutional Neural" 与
    "Networks" 分属两行（组段时是 `\\n`），而格子里是连起来的一串；行末断词
    "Neuro-\\nlinguistic" 在格子里是 "Neurolinguistic"。
    """
    t = re.sub(r"\s+", " ", (text or "").replace("\u00ad", ""))
    return re.sub(r"(?<=[A-Za-z])-\s+(?=[a-z])", "", t).strip()


# 各种连字符（ASCII 之外的破折号常出现在页码区间、型号 "5–8-1" 里）
_HYPHENS = "\u2010\u2011\u2012\u2013\u2014\u2212"


def _drop_char(ch: str) -> bool:
    """机器核对时**不算内容**的字符：空白与连字符。"""
    return ch.isspace() or ch == "-" or ch in _HYPHENS


def _tight(text: str) -> str:
    """最松的一层归一化：压掉**全部空白**与**全部连字符**（在 `_flat` 之后再压一遍）。

    为什么不放宽"字"、只放宽这两样（2026-09-16 生产实测补的）：
    空白与连字符在 PDF 抽取里最不可靠 —— `[ 115 ]` / `[115]`、`K -means` / `K-means`、
    `multi- branch` / `multi-branch`、`meas- ured` / `measured` 都是**同一串字**。
    护栏的目的是"不许编造内容"；**丢一个字符**才是编造，那是另一条判据（不放宽）。
    实测代价：不肯放宽时，agent 写对了 `[115]`（论文原样）却被判"找不到"，
    它只能去 `edit_block` 改源块 —— 而**源块是对的、论文也这么印**，改它反而错。
    """
    return "".join(ch for ch in _flat(text) if not _drop_char(ch))


def _tight_index(text: str) -> tuple[str, list[int]]:
    """`_tight(text)` + 每个字符在 `text` 里的下标（把命中位置映射回原文用）。"""
    chars, idx = [], []
    for i, ch in enumerate(text):
        if not _drop_char(ch):
            chars.append(ch)
            idx.append(i)
    return "".join(chars), idx


def _table_placeable(src: str, needle: str) -> bool:
    """`needle`（一格文字）是否来自 `src` —— 按**空白/连字符不敏感**的方式比。"""
    n = _tight(needle)
    if not n:
        return True                                   # 空格子无需校验（允许空尾格）
    return n in _tight(src)


def _table_residue(src: str, placed: list[str]) -> list[str]:
    """从**被消费块拼起来的文本**里挖掉"已装进格子的字"后，剩下的**成句**片段。

    判据故意宽松（≥4 个实词才算"一段没被消费的话"）：表头的单位、脚注的星号、
    表号 "Table 4." 这类残渣本来就该剩下来，为它们拒收会让 agent 无路可走。

    ⚠️ **必须在"拼起来的文本"上算，不能逐块算**（2026-09-16 实测修正）。
    一个格子的字**经常横跨好几块** —— 抽取按阅读顺序切块，表格一行会被切成几段：
    `b-0193` 结尾是 "…by integrating meas-"，`b-0194` 开头是 "ured and predicted data…"，
    合起来才是那一格的 "…integrating measured and predicted data…"。
    逐块挖时，这串字在**任何单独一块里都不完整** → 一个也挖不掉 → 整块文本被判成
    "没进格子的正文" → **正确**的重建被拒收（生产实测：agent 连试 3 次全被拒、
    最后放弃，见「表格重建 1 张」那次）。拼起来算则与 `_table_placeable` 看的是
    **同一个文本面**，两条护栏不再各说各话。

    挖除按 `_tight` 的松比对做（空白/连字符不敏感），**但残留检测仍在带空白的 `_flat`
    文本上做** —— 数实词要靠空白划词界，全压掉就一个词都数不出来了。
    位置映射由 `_tight_index` 提供：在压缩串上命中的一段，映射回原文把那段抹掉。
    """
    t = _flat(src)
    tight, idx = _tight_index(t)
    cut = [False] * len(t)
    for p in sorted(placed, key=len, reverse=True):
        tp = _tight(p)
        if len(tp) < 2:
            continue
        start = 0
        while (k := tight.find(tp, start)) >= 0:
            for j in range(k, k + len(tp)):
                cut[idx[j]] = True
            start = k + len(tp)
    left = "".join(" " if cut[i] else ch for i, ch in enumerate(t))
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z's-]{3,}", left) if w.lower() not in _STOPWORDS]
    return words if len(words) >= _TABLE_MIN_RESIDUE_WORDS else []


def _blocks_holding(blocks: list[Block], words: list[str]) -> list[str]:
    """哪些块里还能看到这些残留词 —— 只为了让拒绝信息**指得出地方**（不是判据）。"""
    out = []
    for b in blocks:
        t = _flat(b.en).lower()
        if any(w.lower() in t for w in words[:8]):
            out.append(b.id)
    return out


_STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "not",
              "table", "figure", "note", "total", "other", "than", "more", "less", "see"}


# ── 疑似表区（几何，纯本地计算、零 token）─────────────────────────────────
# 与 ㊲ 的「续段」同一个套路：**程序只负责量出可疑处、把 agent 引到那一块去看**，
# 判"这是不是表格、该怎么分格"仍在 agent。已实测（㊲ 的对照实验）标记本身买到的
# 是**召回率与假阴性消除**，不是替代判据 —— 但代价为零，该给。
#
# 判据（两条**缺一不可**，都在真数据上调过）：
#   ① **横线**：这些表有横线、**没有竖线**，所以 `page.find_tables()` 抓不到；
#   ② **同一行上横着好几段文字**：表格行被 `get_text()` 按阅读顺序拆成了并排的几段
#      （正是「Naive Bayes (BN) Support vector machine (SVM)」这种粘连的来源）。
#      只靠①会把**图框边、期刊页眉装饰线**全报成表格（实测 37 页报 20+ 处）；
#      只靠②会把**双栏正文**报成表格（第 29 页两个正文栏每行都是 2 段）。两条合起来才有
#      第 3/20/21 页那三张表，且 37 页里零误报。
#
# ⚠️ **不要用块的 `payload["bbox"]` 当判据**：`parse.py` 只在 **figure** 块上存 bbox，
#    正文块（`p`/`h*`）根本没有 —— 旧版就是这么写的，于是"块落在表区里"永远命中 0 个块，
#    整个提示层**静默全空**（2026-09-16 实测：37 页 regions 恒为 0；而单测里
#    `_table_doc()` 自己给块塞了 bbox，所以一路是绿的）。块号只能**按文本位置反推**，
#    且只当**线索**给 agent（见 `_band_block_ids`）。
_MIN_RULE_LEN = 0.25       # 一条横线至少要有页宽的这个比例才算表格线
_RULE_TOL = 1.5            # |dy| 小于它就算水平线
_ROW_GAP = 220.0           # 相邻两条横线的最大间距：表头线 → 底线之间往往隔着一两百像素
_BAND_PAD = 6.0            # 文本行/块与表区的 y 容差
_MARGIN_BAND = 0.09        # 页眉/页脚带（期刊装饰线就在这里，不是表格线）
_MIN_BAND_LINES = 4        # 表带里至少要有这么多行文本
_MIN_BAND_RATIO = 1.6      # 每「行」平均要摊到这么多段文字（表格一行里横着好几格）
_MIN_BAND_COLS = 3         # 同一行上的文字至少要起于 3 个不同的 x（双栏正文只有 2 个）
_MAX_TABLE_REGIONS = 3     # 一页最多报几处（多了就是噪声，agent 反而看不见）


def _page_text_lines(page) -> list[tuple[float, float, float, float, str]]:
    """页面**文本行**（不含图片）：`[(x0, y0, x1, y1, 文本)]`，按 PDF 自己的顺序。"""
    out: list[tuple[float, float, float, float, str]] = []
    try:
        data = page.get_text("dict")
    except Exception:                                          # noqa: BLE001
        return out
    for blk in data.get("blocks") or []:
        if blk.get("type") != 0:                               # 1 = 图片
            continue
        for ln in blk.get("lines") or []:
            txt = "".join(s.get("text") or "" for s in (ln.get("spans") or []))
            if not txt.strip():
                continue
            x0, y0, x1, y1 = (ln.get("bbox") or (0, 0, 0, 0))[:4]
            out.append((float(x0), float(y0), float(x1), float(y1), txt))
    return out


def _band_block_ids(lines, blocks: list[Block], y0: float, y1: float) -> list[str]:
    """表带里的块号 —— **按文本位置反推的线索**，不是事实（`payload["bbox"]` 不可用）。

    做法：表带里的每一行文本，去块里找"最紧的、包含这行字"的块（包含它的最短块 ——
    短块比长块更可能是它的家）。只有当**这个块被命中的行全部落在表带内**才算数，
    免得把骑在带子边缘的正文段一起拖进来。只在 `p` 块里找：行的料永远是段落，
    而**图注、标题的文字会和表带里的行撞车**（实测第 21 页那张表的表注被拆成
    「inspection, testing, and / validation」两行，正好是 h4「4.1 ML in inspection,
    testing, and verification」的子串 → 把标题拉进线索里纯属噪声）。
    """
    hits: dict[str, list[float]] = {}
    for _x0, ly0, _x1, ly1, txt in lines:
        if ly0 < y0 - _BAND_PAD or ly1 > y1 + _BAND_PAD:
            continue
        t = _flat(txt)
        if len(t) < 3:
            continue
        cands = [b for b in blocks
                 if b.type == "p" and t in _flat(b.en)]
        if not cands:
            continue
        owner = min(cands, key=lambda b: len(b.en))
        hits.setdefault(owner.id, []).extend([ly0, ly1])
    return [b.id for b in blocks
            if (v := hits.get(b.id))
            and min(v) >= y0 - _BAND_PAD and max(v) <= y1 + _BAND_PAD]


def _h_rules(page) -> list[tuple[float, float, float]]:
    """页内所有**水平线**：`[(y, x0, x1)]`（线 + 矩形上下边都算）。"""
    out: list[tuple[float, float, float]] = []
    try:
        drawings = page.get_drawings()
    except Exception:                                          # noqa: BLE001 — 老版本没有该 API
        return out
    width = float(page.rect.width)
    for d in drawings:
        for item in d.get("items") or []:
            if not item:
                continue
            kind = item[0]
            segs = []
            if kind == "l":                                    # ('l', Point, Point)
                p1, p2 = item[1], item[2]
                segs.append((p1.x, p1.y, p2.x, p2.y))
            elif kind == "re":                                 # ('re', Rect)
                r = item[1]
                segs += [(r.x0, r.y0, r.x1, r.y0), (r.x0, r.y1, r.x1, r.y1)]
            for x1, y1, x2, y2 in segs:
                if abs(y2 - y1) > _RULE_TOL:
                    continue
                x0, x1b = min(x1, x2), max(x1, x2)
                if x1b - x0 >= _MIN_RULE_LEN * width:
                    out.append((round((y1 + y2) / 2, 2), x0, x1b))
    return sorted(out)


def _table_regions(page, blocks: list[Block]) -> list[dict]:
    """量出这一页的**疑似表区**：`[{y0, y1, x0, x1, ids, region}]`。

    `region` 是**页内比例**（0–1），可直接喂给 `read_page(page, region=…)` 放大看 ——
    这是这条提示真正的作用：让 agent 把注意力放在那一条带上，而不是"整页扫一眼"。
    `ids` 只是线索（见 `_band_block_ids` 的免责说明）。

    判据见文件上方「疑似表区」那一段：**横线 + 同一行上横着好几段文字**，缺一不可。
    """
    rect = page.rect
    height = float(rect.height)
    # 0) 去掉页眉/页脚带里的线：期刊在每个内容页顶部都印一对装饰横线，
    #    不滤掉的话每页都"疑似有表"（实测 37 页里 30 页中招）。
    rules = [r for r in _h_rules(page)
             if height * _MARGIN_BAND < r[0] < height * (1 - _MARGIN_BAND)]
    if len(rules) < 2:
        return []
    # 1) 按 y 聚簇：相邻横线间距不超过 `_ROW_GAP` 且 x 区间有重叠
    groups: list[list[tuple[float, float, float]]] = []
    for r in rules:
        if groups:
            last = groups[-1][-1]
            overlap = min(last[2], r[2]) - max(last[1], r[1])
            if r[0] - last[0] <= _ROW_GAP and overlap > 0:
                groups[-1].append(r)
                continue
        groups.append([r])
    # 2) 每组配「带子里的文本」验一遍：横线只说明"这里画了线"，有没有**表格状的文字**
    #    才是判据（图框边、通栏分隔线过不了这一关）。
    lines = _page_text_lines(page)
    out: list[dict] = []
    for g in groups:
        if len(g) < 2:
            continue
        y0, y1 = g[0][0], g[-1][0]                          # 首尾两条横线 = 表区的上下界
        if y1 - y0 < 8:
            continue
        band = [l for l in lines if l[1] >= y0 - _BAND_PAD and l[3] <= y1 + _BAND_PAD]
        if len(band) < _MIN_BAND_LINES:
            continue
        rows = {round(l[1] / 4) for l in band}
        cols = {round(l[0] / 4) for l in band}
        if not rows or len(band) / len(rows) < _MIN_BAND_RATIO or len(cols) < _MIN_BAND_COLS:
            continue
        x0 = min(r[1] for r in g)
        x1 = max(r[2] for r in g)
        out.append({
            "y0": round(y0, 1), "y1": round(y1, 1),
            "x0": round(x0, 1), "x1": round(x1, 1),
            "ids": _band_block_ids(band, blocks, y0, y1),
            "region": [max(0.0, round(x0 / rect.width - 0.02, 3)),
                       max(0.0, round(y0 / rect.height - 0.02, 3)),
                       min(1.0, round(x1 / rect.width + 0.02, 3)),
                       min(1.0, round(y1 / rect.height + 0.02, 3))],
        })
        if len(out) >= _MAX_TABLE_REGIONS:
            break
    return out


def _load_args(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        # 有的模型会给一层 markdown 围栏，或前后带解释字 —— 抠出第一个 JSON 对象
        text = _RE_FENCE.sub("", raw or "")
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return {}
        else:
            return {}
    return data if isinstance(data, dict) else {}


def _type_counts(doc: Doc) -> dict:
    out: dict[str, int] = {}
    for b in doc.blocks:
        out[b.type] = out.get(b.type, 0) + 1
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _next_id(doc: Doc, taken: set[str] | None = None) -> str:
    """取一个没用过的块 ID。

    ⚠️ `taken` 不是可选的装饰：切分时**多个新块是一次性全部构造、之后才插入**的，
    只看 `doc.blocks` 会让它们**拿到同一个 ID**（实测切 4 段 → 3 个块全叫 `b-0044`，
    重复 ID 会让划痕/笔记锚错块）。调用方必须把"本次已分配但还没插入"的 ID 也传进来。
    """
    used = {b.id for b in doc.blocks} | set(taken or ())
    n = len(doc.blocks)
    while True:
        n += 1
        cand = make_block_id(n)
        if cand not in used:
            return cand


def _acceptable_text_change(old: str, new: str) -> tuple[bool, str]:
    """文字修订护栏 —— 允许"补回截断"，不许"整段编造"（返回 (是否接受, 原因)）。"""
    old, new = (old or "").strip(), (new or "").strip()
    if not new:
        return False, "修正文本为空"
    if new == old:
        return False, "与原文相同（没改动就不必调用）"
    if len(new) > len(old) * _MAX_GROWTH_RATIO + _MAX_GROWTH_SLACK:
        return False, f"长度暴涨（{len(old)} → {len(new)} 字符），疑似编造"
    ratio = difflib.SequenceMatcher(None, old, new).ratio()
    if ratio < _MIN_SIMILARITY:
        return False, f"相似度仅 {ratio:.2f}，疑似重写而非校对"
    return True, ""
