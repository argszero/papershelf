"""笔记（决策⑯ 块锚点 + 决策㉛ 区间锚点）：可锚到任意选区 / 整块 / 整篇。

锚点三档，前端选了什么就是什么（原型没有"是否锚定"的开关，见 ㉚ 的思路）：
`(block_id, lang, start, end)` 齐全 = 锚到一段选区；只有 `block_id` = 整块；都没有 = 文献级。

**给选区写笔记会顺手自动高亮**（宿主 2026-09-13：「选中添加笔记时，应该同时自动高亮」）：
命中选区的笔记本来就有坐标，缺的只是一道划痕；没有就在**同一个事务**里补上，
于是"划了重点却没上色"这个中间态根本不存在（也就没有"笔记存了、划痕丢了"的孤儿）。
颜色取**当前那支笔**（前端把工具栏的 `pen` 带上来），不给就退默认色。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ...pipeline.markup import DEFAULT_HL_COLOR, HL_COLORS
from ..db import tx, utcnow
from ..repo import ensure_highlight, list_notes
from ..security import current_user, get_conn, require_paper

router = APIRouter(prefix="/api", tags=["notes"])


class NoteIn(BaseModel):
    content: str = Field(min_length=1)
    block_id: str | None = None
    # 决策㉛：选区锚点 `(lang, start, end)` + 选区原文摘录（列表里回显「…」）。
    # 后端**不做一致性校验**（比如"这对偏移真的落在块里吗"）：算错坐标是前端的问题，
    # 而旧笔记在译文被改过之后偏移漂掉是**正常现象**，不该让保存失败。
    lang: str | None = None
    start: int | None = None
    end: int | None = None
    quote: str | None = None
    # 给某条已有划痕写笔记时带上它 —— 卡片才能显示划痕的颜色圆点、点笔记跳到那一道划痕。
    hl_id: int | None = None
    # 没有 `hl_id` 但要自动补一道划痕时用哪支笔。只在"真的新建"时才校验/生效。
    color: str | None = None


class NotePatch(BaseModel):
    content: str = Field(min_length=1)


@router.get("/papers/{paper_id}/notes")
def get_notes(paper_id: int, conn: sqlite3.Connection = Depends(get_conn),
              user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    require_paper(conn, paper_id, user)
    return list_notes(conn, paper_id)


def _is_range(body: NoteIn) -> bool:
    """锚点是否落在**一段选区**上（三者齐全且区间非空才算）。"""
    return (body.block_id is not None and body.lang in ("en", "zh")
            and body.start is not None and body.end is not None and body.end > body.start)


def _auto_color(body: NoteIn) -> str:
    if body.color is None:
        return DEFAULT_HL_COLOR
    if body.color not in HL_COLORS:
        raise HTTPException(400, f"颜色只能是 {'/'.join(HL_COLORS)}")
    return body.color


@router.post("/papers/{paper_id}/notes", status_code=201)
def add_note(paper_id: int, body: NoteIn, conn: sqlite3.Connection = Depends(get_conn),
             user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    require_paper(conn, paper_id, user)
    color = _auto_color(body) if (body.hl_id is None and _is_range(body)) else DEFAULT_HL_COLOR
    with tx(conn):
        hl_id = body.hl_id
        if hl_id is None and _is_range(body):
            # 选区还没有划痕 → **同一个事务里**补一道（笔记与它的划痕要么都在、要么都不在）
            hl_id = int(ensure_highlight(
                conn, paper_id, block_id=body.block_id, lang=body.lang,
                start=body.start, end=body.end, color=color,
            )["id"])
        cur = conn.execute(
            "INSERT INTO notes (paper_id, block_id, content, created_at, quote,"
            " lang, start, end, hl_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (paper_id, body.block_id, body.content, utcnow(), body.quote,
             body.lang, body.start, body.end, hl_id),
        )
        note_id = int(cur.lastrowid)
    row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return dict(row)


def _owned_note(conn: sqlite3.Connection, note_id: int, user: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        """SELECT n.* FROM notes n JOIN papers p ON p.id = n.paper_id
           JOIN plans pl ON pl.id = p.plan_id WHERE n.id=? AND pl.user_id=?""",
        (note_id, user["id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "笔记不存在")
    return dict(row)


@router.patch("/notes/{note_id}")
def edit_note(note_id: int, body: NotePatch, conn: sqlite3.Connection = Depends(get_conn),
              user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _owned_note(conn, note_id, user)
    with tx(conn):
        conn.execute("UPDATE notes SET content=? WHERE id=?", (body.content, note_id))
    return dict(conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone())


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int, conn: sqlite3.Connection = Depends(get_conn),
                user: dict[str, Any] = Depends(current_user)) -> None:
    _owned_note(conn, note_id, user)
    with tx(conn):
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
