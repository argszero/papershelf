"""划痕（决策㉛）—— 高亮的单位是**任意字符区间**，不再是句子。

原型那一版是"点一句 → 整句变黄"（㉚）。宿主 2026-09-13 指出那不是阅读论文时标注的方式：
**标准做法是先挑一支颜色的笔，再随手划住任意一段**，划多长就是多长；选中任意一段也能加备注。
于是锚点从「句」换成「字符区间」。

坐标是块**裸文本**（`blocks.en` / `blocks.zh`）的字符偏移 `[start, end)`，
与 `pipeline.markup.prose_html` 渲染时用的坐标是同一套 —— 服务端渲染、
前端只把 DOM 选区换算成这对数字（换算靠渲染器吐出的 `<span class="o" data-o>` 锚点）。

为什么颜色**不带含义**（宿主原话：「好看的几种颜色、没有含义」）：
带含义就得有图例、要教用户"黄=重点"，还会诱导"颜色用错了"这类无谓的焦虑。
`HL_COLORS` 就是四支等价的笔，选哪支纯看心情。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...pipeline.markup import DEFAULT_HL_COLOR, HL_COLORS
from ..repo import create_highlight, delete_highlight, list_highlights, set_highlight_color
from ..security import current_user, get_conn, require_paper

router = APIRouter(prefix="/api", tags=["highlights"])


class HighlightIn(BaseModel):
    block_id: str = Field(min_length=1)
    lang: str = Field(pattern="^(en|zh)$")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    color: str = DEFAULT_HL_COLOR


class ColorIn(BaseModel):
    color: str


def _check_color(color: str) -> str:
    if color not in HL_COLORS:
        raise HTTPException(400, f"颜色只能是 {'/'.join(HL_COLORS)}")
    return color


@router.get("/papers/{paper_id}/highlights")
def get_highlights(paper_id: int, conn: sqlite3.Connection = Depends(get_conn),
                   user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    """该文献的全部划痕（前端拿它给笔记卡片配颜色圆点、以及本地增删）。"""
    require_paper(conn, paper_id, user)
    return list_highlights(conn, paper_id)


@router.post("/papers/{paper_id}/highlights", status_code=201)
def add_highlight(paper_id: int, body: HighlightIn,
                  conn: sqlite3.Connection = Depends(get_conn),
                  user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """划一道。

    `end <= start` 视为空选区 → 400（前端不该发出来，发了就是前端的换算错了，
    静默存一条零长划痕只会让后面更难查）。
    """
    require_paper(conn, paper_id, user)
    if body.end <= body.start:
        raise HTTPException(400, "选区为空")
    row = create_highlight(conn, paper_id, block_id=body.block_id, lang=body.lang,
                           start=body.start, end=body.end, color=_check_color(body.color))
    return row


@router.patch("/highlights/{hl_id}")
def recolor(hl_id: int, body: ColorIn, conn: sqlite3.Connection = Depends(get_conn),
            user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """换一支笔（只改颜色，端点不动 —— 所以笔记的锚点不受影响）。"""
    color = _check_color(body.color)
    row = conn.execute("SELECT paper_id FROM highlights WHERE id=?", (hl_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "划痕不存在")
    require_paper(conn, row["paper_id"], user)
    fresh = set_highlight_color(conn, hl_id, color)
    if fresh is None:
        raise HTTPException(404, "划痕不存在")
    return fresh


@router.delete("/highlights/{hl_id}")
def remove_highlight(hl_id: int, conn: sqlite3.Connection = Depends(get_conn),
                     user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """擦掉这道划痕。**幂等**：已经没了也返回 200 —— 前端那记删除是本地状态驱动的，
    对不存在的行回 404 只会弹一句用户看不懂的报错，而屏幕上早已是他要的样子。"""
    row = conn.execute("SELECT paper_id FROM highlights WHERE id=?", (hl_id,)).fetchone()
    if row is None:
        return {"id": hl_id, "deleted": False}
    require_paper(conn, row["paper_id"], user)
    return {"id": hl_id, "deleted": delete_highlight(conn, hl_id)}
