"""块级修订 —— 决策⑯ 的兜底通道（重译此块 / 编辑译文）。

决策⑯ 的边界很明确：**不做自动语义自检**，语义问题一律由用户按需修订。
所以这里只有两个动作：
1. `PATCH .../{block_id}` 人工改译文 → `zh_source='human'`（重跑不得覆盖）；
2. `POST .../{block_id}/retranslate` 只重译这一块（带上下文，走同一条翻译通道）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...pipeline import LLMConfig, Translator
from ...pipeline.validate import expects_chinese
from ..config import Settings, get_settings
from ..db import load_json
from ..repo import get_block, list_highlights, public_block, update_block
from ..security import current_user, get_conn, require_paper

router = APIRouter(prefix="/api/docs/{paper_id}", tags=["blocks"])


class BlockPatch(BaseModel):
    zh: str
    # 前端「确认无误」后置 true → 撤掉「待校对」徽标（§5.7）
    reconciled: bool = False


def _glossary(conn: sqlite3.Connection, plan_id: int) -> list[dict[str, Any]]:
    row = conn.execute("SELECT glossary FROM plan_settings WHERE plan_id=?", (plan_id,)).fetchone()
    return load_json(row["glossary"], []) if row else []


@router.get("/blocks/{block_id}")
def get_one_block(paper_id: int, block_id: str,
                  conn: sqlite3.Connection = Depends(get_conn),
                  user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """取**单块**（含按当前划痕渲染好的 HTML，㉛）。

    为什么需要它：划痕是服务端渲染进 HTML 的，前端划一道之后要让屏幕上出现
    `<mark>`，只有三条路 ——（a）自己拿 Range 去包 DOM（前端就得懂切句/公式/转义，
    与"服务端渲染"这条不变量冲突）；（b）重拉整篇（900 块的文档要几百 KB，
    划一道就重下一次，纯浪费）；（c）**重拉这一块**。选 (c)。
    """
    require_paper(conn, paper_id, user)
    fresh = get_block(conn, paper_id, block_id)
    if fresh is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "块不存在")
    marks = [h for h in list_highlights(conn, paper_id) if h["block_id"] == block_id]
    return public_block(fresh, marks=marks)


@router.patch("/blocks/{block_id}")
def edit_block(paper_id: int, block_id: str, body: BlockPatch,
               conn: sqlite3.Connection = Depends(get_conn),
               user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    require_paper(conn, paper_id, user)
    patch = {"reconciled": True} if body.reconciled else None
    ok = update_block(conn, paper_id, block_id, zh=body.zh, zh_source="human",
                      payload_patch=patch)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "块不存在")
    # 回一份**完整块**（含重新渲染的 zh_html）—— 前端整块替换，
    # 否则它只能就地改纯文本，`zh_html` 还是旧的，界面看起来「没保存上」。
    fresh = get_block(conn, paper_id, block_id)
    # ⚠️ 必须带**本块的划痕**：改了译文就要按新文本重切一遍高亮，
    # 不传 = 编辑一次译文顺手抹掉这一页的所有划痕（服务端渲染的 HTML 是划痕的唯一来源）。
    marks = [h for h in list_highlights(conn, paper_id) if h["block_id"] == block_id]
    return public_block(fresh, marks=marks) if fresh else {"id": block_id, "zh": body.zh}


@router.post("/blocks/{block_id}/retranslate")
def retranslate_block(paper_id: int, block_id: str,
                      conn: sqlite3.Connection = Depends(get_conn),
                      user: dict[str, Any] = Depends(current_user),
                      settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """只重译这一块（决策⑯ 的「重译此块」）。上下文取前后各一块。"""
    paper = require_paper(conn, paper_id, user)
    rows = conn.execute("SELECT * FROM blocks WHERE paper_id=? ORDER BY ord",
                        (paper_id,)).fetchall()
    target = next((r for r in rows if r["id"] == block_id), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "块不存在")

    from ...pipeline.model import Block

    def to_block(r) -> Block:
        return Block(id=r["id"], type=r["type"], en=r["en"] or "", zh=r["zh"] or "",
                     zh_source=r["zh_source"] or "none", section=r["section"] or "",
                     level=r["level"] or 0, payload=load_json(r["payload"], {}))

    ordered = [to_block(r) for r in rows]
    idx = [i for i, b in enumerate(ordered) if b.id == block_id][0]
    target_block = ordered[idx]

    if not settings.llm_base_url or not settings.llm_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务端未配置 LLM")
    if not expects_chinese(target_block.en, block_type=target_block.type):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "该块按规则保持原文（公式/参考文献），无需翻译")

    cfg = LLMConfig(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                    model=settings.llm_model)
    tr = Translator(cfg, _glossary(conn, paper["plan_id"]))
    texts = tr.translate_blocks(
        [target_block], only={block_id}, max_retry=1,
        log=lambda *a: None,
    )
    new_zh = (texts[0] if texts else "").strip()
    if not new_zh:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "重译失败，请稍后重试")

    # ⚠️ 表格的译文**不只在 `zh` 里**：网格（`payload.rows_zh` / `caption_zh`）才是
    # 阅读器真正渲染的东西（决策㊴）。翻译器把它写在了**内存里的那个 Block 副本**上，
    # 而这里是"取库里的行 → 拼 Block → 翻译 → 单列回写"，副本一丢网格就没了 ——
    # 表现是「重译此块」后中文照旧是英文（重译了、但渲染用的网格没变）。
    # 参考文献条目（`ref`）同理：`title_zh` 是"这条译了什么"的记录，一起回写。
    patch: dict[str, Any] = {"reconciled": True}
    if target_block.type == "table":
        for k in ("rows_zh", "caption_zh"):
            if k in target_block.payload:
                patch[k] = target_block.payload[k]
    elif target_block.type == "ref":
        for k in ("title_en", "title_zh"):
            if k in target_block.payload:
                patch[k] = target_block.payload[k]
    update_block(conn, paper_id, block_id, zh=new_zh, zh_source="mt", payload_patch=patch)
    fresh = get_block(conn, paper_id, block_id)
    marks = [h for h in list_highlights(conn, paper_id) if h["block_id"] == block_id]
    out = public_block(fresh, marks=marks) if fresh else {"id": block_id, "zh": new_zh}
    out["tokens"] = tr.tokens_used
    return out
