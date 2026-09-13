"""⑲ 元数据与标签的 **LLM 自动抽取**。

## 为什么有这个文件（2026-09-12 补做）

决策⑲ 原文：「元数据（标题 / 作者 / 会议 / 年份）：由 LLM **自动抽取**（解析首块/摘要），
导入即填好」+「标签：LLM 自动生成 3–5 个」——**自始未实现**。

症状：生产文献库 12 篇全叫 `original`（上传时写的是文件名）、副标题恒「未提取到作者」、
发表/标签全 `—`。`converter.py` 里那几行 `meta.get("authors")` 是**有消费端、无生产端**：
`pipeline/` 里所有 `doc.meta[...]` 赋值只有 `title_en` / `dropped_images` / `refs_*`。

## 设计要点

1. **只吃前若干块**：标题/作者/期刊/年份只可能出现在首屏。全篇送进去白烧 token，
   而且首屏之后全是术语和公式，只会干扰判断。
2. **一次调用出全部字段**（含中文标题）：`title_zh` 在原决策里同样**没有任何赋值路径**。
   同一份上下文再单独问一次纯属浪费。
3. **输出必须强校验，失败就退回不写**：模型会加 markdown 围栏、会附解释、
   会把 `tags` 写成字符串。`papers` 的写回用 `COALESCE(NULLIF(title,''))` 保护用户手填值 ——
   一旦写进垃圾值，那一层就会把垃圾锁死。**宁可不写，不能写错。**
4. **`year` 必须是 int 或 None**：`papers.year` 是 INTEGER 列，喂 `"2024"` 会静默变 0。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .model import Block
from .translator import LLMConfig, Translator

METADATA_SYSTEM = """你是学术文献的元数据抽取器。你会看到一篇论文**开头的若干块**（英文原文）。
你的任务是从中抽取元数据，并以**严格的 JSON** 输出。

输出格式（只输出 JSON，不要 markdown 围栏、不要任何解释文字）：
{
  "title_en": "论文的完整英文标题",
  "title_zh": "标题的准确中文翻译",
  "authors": "作者列表，中文间隔号 · 分隔，最多 6 位，更多则末尾加「等」",
  "venue": "期刊或会议名称（含卷期可省略），无法确定则空字符串",
  "year": 2024,
  "tags": ["中文标签", "中文标签", "中文标签"]
}

规则：
1. **标题**取论文自身的标题。期刊名、页眉、栏目标题（如「ARTICLE」「Contents lists available at」）、
   出版社页脚、DOI 行**都不是**标题；arXiv 编号行也不是。若开头混有这些内容，请忽略它们。
   **必须逐字来自【候选标题】或【论文开头的若干块】，不得改写、缩写、翻译或凭常识补全** ——
   如果只能隐约猜出主题而看不到完整标题，`title_en` 写空字符串。
2. **作者**只取人名，去掉上标数字/符号与单位地址；单位、通讯作者标记不要写进来。
   分不清人名与单位时宁可少写。
3. **venue** 是期刊/会议名。若闻到的是预印本（arXiv 等），写 "arXiv"。
4. **year** 是四位数字年份（整数）。无法确定写 null。
5. **tags** 给 3–5 个**中文**关键词，反映论文主题（不是数据集名或机构名）。
6. 任何字段确实无法从给出的片段判断时，字符串字段写空字符串，tags 写 []，
   **绝不要编造**（编造比留空危害大得多）。"""

#: 抽取用的块类型。图/公式/参考文献不含元数据，且 figure 的 payload 在 `caption` 里。
_USABLE_TYPES = ("p", "h1", "h2", "h3", "h4", "meta", "abstract", "table")

#: 送进模型的块数上限与字符上限（首屏足够；再多只是烧 token）
MAX_BLOCKS = 14
MAX_CHARS = 4000


def _as_str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _as_year(v: Any) -> int | None:
    if isinstance(v, bool):                        # bool 是 int 的子类，先挡掉
        return None
    if isinstance(v, int):
        return v if 1900 <= v <= 2100 else None
    if isinstance(v, str):
        m = re.search(r"(19|20)\d{2}", v)
        if m:
            y = int(m.group(0))
            return y if 1900 <= y <= 2100 else None
    return None


def _as_tags(v: Any) -> list[str]:
    """标签归一：模型可能给 list、给逗号串、给带前缀的串。"""
    items: list[str] = []
    if isinstance(v, list):
        items = [x for x in v if isinstance(x, str)]
    elif isinstance(v, str):
        items = re.split(r"[,，、;；]", v)
    out: list[str] = []
    for t in (s.strip().strip("#") for s in items):
        if t and t not in out:
            out.append(t)
    return out[:5]


class MetadataExtractor(Translator):
    """复用 Translator 的 LLM 通道（OpenAI 兼容 + token 计量），换一套 system prompt。"""

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        self.system = METADATA_SYSTEM

    # ── 输入 ──────────────────────────────────────────────────────────────
    @staticmethod
    def head_blocks(blocks: list[Block]) -> list[Block]:
        """取开头可用的若干块（跳过图/公式/参考文献）。"""
        picked: list[Block] = []
        size = 0
        for b in blocks:
            if b.type not in _USABLE_TYPES:
                continue
            text = (b.en or "").strip()
            if not text:
                continue
            if size + len(text) > MAX_CHARS:
                break
            picked.append(b)
            size += len(text)
            if len(picked) >= MAX_BLOCKS:
                break
        return picked

    def _prompt(self, blocks: list[Block], candidates: list[str] | None = None) -> str:
        body = "\n".join(f"[{i + 1}] {b.type}: {(b.en or '').strip()}"
                         for i, b in enumerate(blocks))
        hint = ""
        if candidates:
            lines = "\n".join(f"  - {c}" for c in candidates)
            hint = ("\n\n【解析器从封面挑出的候选标题】（按出现顺序，**第一个往往是期刊名而非论文标题**，"
                    "请在其中选出真正的论文标题；都不对则以块内容为准）\n" + lines)
        return ("【论文开头的若干块】\n" + body + hint +
                "\n\n请按 system 规定的 JSON 格式输出这篇论文的元数据。")

    # ── 调用 ──────────────────────────────────────────────────────────────
    def extract(self, blocks: list[Block], *, candidates: list[str] | None = None,
                log=print) -> dict[str, Any]:
        """返回**已校验**的元数据字段（只含确实抽到的键；全抽不到则返回 `{}`）。"""
        head = self.head_blocks(blocks)
        if not head and not candidates:
            return {}
        try:
            raw = self._chat(self._prompt(head, candidates))
        except Exception as exc:                    # noqa: BLE001
            # 抽取失败**不得让整篇转换失败**：元数据是锦上添花，译文才是主产物。
            log(f"    ! 元数据抽取调用失败：{exc}")
            return {}

        data = _parse_json(raw)
        if data is None:
            log(f"    ! 元数据抽取返回不是合法 JSON，已忽略：{raw[:120]!r}")
            return {}

        out: dict[str, Any] = {}
        for key in ("title_en", "title_zh", "authors", "venue"):
            val = _as_str(data.get(key))
            if val:
                out[key] = val
        year = _as_year(data.get("year"))
        if year is not None:
            out["year"] = year
        tags = _as_tags(data.get("tags"))
        if tags:
            out["tags"] = tags
        return out


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse_json(text: str) -> dict[str, Any] | None:
    """容错解析：去围栏 → 直接解析 → 退而求其次取第一个 `{...}` 块。"""
    cleaned = _FENCE_RE.sub("", (text or "").strip()).strip()
    for candidate in (cleaned, _first_object(cleaned)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _first_object(text: str) -> str:
    """从「模型夹带了说明文字」的回复里抠出第一个平衡的 `{...}`。"""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""
