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

from .model import Block, Doc, make_block_id
from .parse import clean_text

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
# 2026-09-14 实测（同一条校对请求，池子里的视觉模型 `deepseek-v4-flash-vision-exp`）：
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


def _suspect_pages(doc: Doc) -> dict[str, int]:
    """`{页号: 可疑块数}` —— **只给页级计数**。

    ⚠️ 踩过：第一版把**全篇**可疑清单（`{页: {块: 标签}}`）塞进 seed，实测 10.5k 字符 ≈
    **2.7k tokens**，而 seed 是**每一轮都要重发**的 —— 37 页 78 轮 ≈ 白烧 21 万 tokens，
    换来的只是省掉一两轮探索。详情因此下移到**当页的工具结果**里（`read_blocks` 每块带
    `flags`、`check_artifacts` 给整页明细），它们只存在于"当前几轮"的上下文里。
    """
    counts: dict[str, int] = {}
    for b in doc.blocks:
        text = b.en if b.type != "figure" else (b.payload.get("caption") or "")
        if _artifact_tags(text):
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
- **块结构**：一句话被拆成两块（应合并）、一段被并成一块（应切分）、重复块（应删除）。
- **阅读顺序**：这一页正确的读序（先上后下、先左栏后右栏、通栏块在其所在位置）。
- **笔记**：抽取**丢失**的内容（图里有的图标/编号/符号而未抽到）在 `finish` 的总结里说明，
  但**不要凭想象补写**到文本里。

## 纪律

1. **只改你真的看出来的错**。正确的块别动 —— 无谓改动会破坏译文对齐。
2. 文本必须**逐字来自论文**。不许改写、润色、翻译、补写看不清的内容。
3. 保留引用编号、图表编号、数字、单位、公式、大小写、标点的一切原样（只修抽取噪声）。
4. **看不清就放大**（`read_page(page, region=…)`），不要猜。
5. 每页核对完必须 `mark_page_done(page)`；全部完成后 `finish(summary)`。
   还有页没 `mark_page_done` 时 `finish` 会被拒绝，并告诉你还差哪几页。
6. 工具报错（护栏拒绝）说明你的改法不成立 —— 要么换个改法，要么承认这块没问题，别硬来。

## 省成本：每页两轮（这是硬要求）

上下文**每轮重发一次**，所以「轮数 = 成本」；一次调用里的多个工具只算一轮。所以：

- **第 1 轮（看）**：同一轮里把 `read_page(page)` + `read_blocks(page)` 一起调
  （想同时看放大区就一起调；需要的话再带上 `read_block`）。
- **第 2 轮（改）**：把这一页**所有** `edit_block` / `split_block` / `merge_block` /
  `delete_block` / `reorder_page` 和 `mark_page_done(page)` **放在同一轮里一次性提交**。

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
            f("split_block", "把一块**切成多块**（它其实是多段被并在一起）。parts 是切好后的各段文本。",
              {"id": {"type": "string"},
               "parts": {"type": "array", "items": {"type": "string"},
                         "description": "切分后的各段文本（≥2 段，逐字来自论文）"},
               "reason": {"type": "string"}}, ["id", "parts"]),
            f("merge_block", "把某块**并入页内上一块**（它是上一块被切断的续写）。",
              {"id": {"type": "string"}, "reason": {"type": "string"}}, ["id"]),
            f("delete_block", "删除**重复**块（同一页里另有块已包含它的内容）。",
              {"id": {"type": "string"}, "reason": {"type": "string"}}, ["id"]),
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
                          "done": p in self.done_pages})
        pending = [x["page"] for x in pages if x["blocks"] and not x["done"]]
        return ToolOut(json.dumps({
            "pdf_pages": self.page_count, "blocks": len(self.doc.blocks),
            "pages": pages, "pending_pages": pending,
            # 提醒 agent 先做什么，省得它盲猜（提示词里也说了，这里是就近提醒）
            "hint": "先 read_page 看图 + read_blocks 看抽取结果 + check_artifacts 看程序量出的可疑处，"
                    "再动手改，最后 mark_page_done。",
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
            if flags := _artifact_tags(text):
                item["flags"] = flags                            # 就地把程序量到的可疑点带上
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
        for b in bs:
            text = b.en if b.type != "figure" else (b.payload.get("caption") or "")
            if got := _artifacts(text):
                hints[b.id] = got
            if len(hints) >= MAX_HINT_BLOCKS:
                break
        if not hints:
            return ToolOut(f"第 {page} 页没有程序能量出的可疑片段（但你仍需对照图像自行核对）。")
        return ToolOut(json.dumps({
            "page": page, "suspicious": hints,
            "note": "这些是程序量出的**事实**，不一定是错：行末断词要看图判断是断字（合并）"
                    "还是词内连字符（保留，如 three-dimensional、long- and short-term）。"
                    "每一条都要有结论：改就 edit_block，不改就算了；",
        }, ensure_ascii=False))

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
        prev.en = clean_text((prev.en.rstrip() + " " + b.en.lstrip()).strip())
        self.doc.blocks.remove(b)
        self.stats.merged += 1
        log.info("  ⇥ %s 并入 %s（%s）", b.id, prev.id, a.get("reason") or "")
        return ToolOut(f"{b.id} 已并入 {prev.id}。")

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
            "suspect_pages": _suspect_pages(doc) or None,
        }
        task = (f"这是《{seed['title'] or '未命名论文'}》的抽取结果概览：\n"
                + json.dumps(seed, ensure_ascii=False)
                + "\n\n请开始逐页校对。")
        if skip:
            task += (f" 注意：第 {sorted(skip)} 页**上次已经校对过**，本次不用再看"
                     f"（但如果它们的顺序/结构影响了本页的判断，你仍然可以 read_page 复查）。")
        task += (" `suspect_pages` 是程序按字符串规则量出的**页级可疑计数**（键是页号，值是可疑块数）："
                 "具体是哪些块、可疑在哪，`read_blocks` 的每块 `flags` 字段里就有（`check_artifacts` "
                 "给整页明细）。按「每页两轮」做：一轮看图看块，一轮把全部改动 + mark_page_done 提交。")
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
                   f"累计：文字修订 {stat.text_fixed} 块 / 合并 {stat.merged} / 拆分 {stat.split}"
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
