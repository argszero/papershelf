"""块级文档模型 —— 决策④（标记）与决策⑳（块级 JSON 为单一事实来源）的落地。

一个 Doc 由有序 Block 组成；每个 Block 有**稳定 ID**，该 ID 贯穿：
翻译标记（④）→ 渲染 → 阅读器对齐滚动（⑤）→ 块级重译/编辑（⑯）→ 笔记锚点。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

BlockType = Literal[
    "meta", "abstract", "h1", "h2", "h3", "h4", "p", "figure", "table", "eq", "refs"
]

# 中文译文的来源，用于决策⑯ 的「重跑不覆盖人工修订」
ZhSource = Literal["none", "mt", "human"]


def make_block_id(ordinal: int) -> str:
    """稳定块 ID：b-0001（4 位序号，随解析顺序分配）。"""
    return f"b-{ordinal:04d}"


# ── 表格块的**文本契约**（决策㊴，2026-09-16）─────────────────────────────
# 表格块同时持两样东西，缺一不可：
#   ① `payload["rows"]` / `payload["rows_zh"]`：**结构化网格**（`list[list[str]]`），
#      阅读器/导出按它渲染成真表格（列对齐是它的全部意义）；
#   ② `en` / `zh`：网格的**裸文本串**（一行一行、格与格之间用 `CELL_SEP`）。
# 为什么非要 ②：全项目的机器都只认 `en`/`zh` 这一个文本面 —— 校验器的漏译判定
# （`validate.expects_chinese` 吃的是 `en` 字符串）、翻译切片的大小估算、公式
# LaTeX 化的 `looks_math`、以及 agent 的 `read_blocks` 预览。若表格只有网格没有文本，
# 这些地方会把它当**空块**看待（漏译判不出、切片算成 0 字符），于是"表在库里、
# 但管线不认它"。
CELL_SEP = " | "
ROW_SEP = "\n"


def grid_shape(rows: Any) -> tuple[int, int] | None:
    """网格的 `(行数, 列数)`；不是「等宽的字符串二维表」就返回 `None`。

    单一来源：`set_table` 的护栏与 `table_cells` 的回落判定都读它
    （两处各写一份"什么算合法网格"迟早会漂）。
    """
    if not isinstance(rows, list) or not rows:
        return None
    width = -1
    for row in rows:
        if not isinstance(row, list) or not row:
            return None
        if not all(isinstance(c, str) for c in row):
            return None
        if width < 0:
            width = len(row)
        elif len(row) != width:
            return None
    return (len(rows), width)


def table_text(rows: list[list[str]] | None, caption: str = "") -> str:
    """网格 → 裸文本（`en`/`zh` 字段的那一份）。

    表注单独占第一行（校验器只看这一串文本，表注也是要判漏译的正文）。
    """
    lines: list[str] = []
    if (caption or "").strip():
        lines.append(str(caption).strip())
    for row in rows or []:
        lines.append(CELL_SEP.join("" if c is None else str(c) for c in row))
    return ROW_SEP.join(lines)


def table_cells(b: "Block", lang: str = "en") -> list[list[str]]:
    """取某语言的网格：中文缺失或**形状对不上**时回落英文网格（绝不吐半张表）。

    形状校验是必需的 —— `rows_zh` 由 LLM 产出，行/列数与英文不一致时若照渲，
    整张表的列会错位（比不译更难发现）。回落英文是**诚实**的降级：读者看到
    原文表格，而不是一张错行的表。
    """
    en = b.payload.get("rows") or []
    if lang == "en":
        return en if isinstance(en, list) else []
    zh = b.payload.get("rows_zh") or []
    if grid_shape(zh) and grid_shape(zh) == grid_shape(en):
        return zh
    return en if isinstance(en, list) else []


@dataclass
class Block:
    id: str
    type: BlockType
    en: str = ""                      # 英文原文（标记穿透的源）
    zh: str = ""                      # 中文译文
    zh_source: ZhSource = "none"
    section: str = ""                 # 所属章节（大纲 / 锚点）
    level: int = 0                    # 标题层级
    payload: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """转为可入库的扁平结构（blocks 表）。"""
        return {
            "id": self.id,
            "type": self.type,
            "en": self.en,
            "zh": self.zh,
            "zh_source": self.zh_source,
            "section": self.section,
            "level": self.level,
            "payload": json.dumps(self.payload, ensure_ascii=False),
        }


@dataclass
class Doc:
    meta: dict[str, Any] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)

    def add(self, type_: BlockType, en: str = "", **kw: Any) -> Block:
        b = Block(id=make_block_id(len(self.blocks) + 1), type=type_, en=en, **kw)
        self.blocks.append(b)
        return b

    def to_json(self) -> str:
        return json.dumps(
            {"meta": self.meta, "blocks": [asdict(b) for b in self.blocks], "assets": self.assets},
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> "Doc":
        data = json.loads(raw)
        return cls(
            meta=data.get("meta", {}),
            assets=data.get("assets", []),
            blocks=[Block(**b) for b in data.get("blocks", [])],
        )

    def by_id(self, block_id: str) -> Block | None:
        for b in self.blocks:
            if b.id == block_id:
                return b
        return None
