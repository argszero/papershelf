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
