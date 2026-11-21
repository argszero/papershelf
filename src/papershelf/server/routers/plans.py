"""计划与计划设置 —— 决策①⑦ 的归属载体，⑫ 术语表的挂载点。

⚠️ 一切访问都先过 `require_plan`（用户间完全隔离）。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import dump_json, load_json, rows_to_list, tx, utcnow
from ..repo import plan_public
from ..security import current_user, get_conn, require_plan

router = APIRouter(prefix="/api/plans", tags=["plans"])


# 计划名的长度规则只写一处，创建与修改共用 —— 否则 `PATCH /api/plans/{id}`
# 会收下 `POST /api/plans` 用 422 拒收的名称（空串、500 字），同一份数据两条路径两个标准。
PlanName = Annotated[str, Field(min_length=1, max_length=200)]


class PlanIn(BaseModel):
    name: PlanName
    goal: int | None = None
    description: str = ""


class PlanPatch(BaseModel):
    name: PlanName | None = None
    goal: int | None = None
    description: str | None = None


class GlossaryIn(BaseModel):
    glossary: list[dict[str, Any]] = []      # [{en, zh, note}]（⑫ v1 只做术语表）


@router.get("")
def list_plans(conn: sqlite3.Connection = Depends(get_conn),
               user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    rows = rows_to_list(conn.execute(
        "SELECT * FROM plans WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
    ).fetchall())
    stats = {
        r["plan_id"]: (r["total"], r["done"])
        for r in conn.execute(
            """SELECT plan_id, COUNT(*) AS total,
                      SUM(CASE WHEN status IN ('read','reviewed') THEN 1 ELSE 0 END) AS done
               FROM papers GROUP BY plan_id"""
        ).fetchall()
    }
    return [plan_public(r, *stats.get(r["id"], (0, 0))) for r in rows]


@router.post("", status_code=201)
def create_plan(body: PlanIn, conn: sqlite3.Connection = Depends(get_conn),
                user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO plans (user_id, name, goal, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (user["id"], body.name, body.goal, body.description, utcnow(), utcnow()),
        )
        conn.execute("INSERT INTO plan_settings (plan_id, glossary) VALUES (?,?)",
                     (cur.lastrowid, dump_json([])))
    return plan_public(dict(conn.execute("SELECT * FROM plans WHERE id=?", (cur.lastrowid,)).fetchone()))


@router.get("/{plan_id}")
def get_plan(plan_id: int, conn: sqlite3.Connection = Depends(get_conn),
             user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return plan_public(require_plan(conn, plan_id, user))


@router.patch("/{plan_id}")
def patch_plan(plan_id: int, body: PlanPatch, conn: sqlite3.Connection = Depends(get_conn),
               user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    require_plan(conn, plan_id, user)
    # ⚠️ `body.x is not None` 分不清「没传这个字段」和「显式传 null 来清空」：
    #    编辑抽屉里把「目标篇数」删空会发 `goal: null`，旧逻辑静默忽略 ⇒
    #    界面上报"保存成功"、那个数字却还在（#31）。与 `papers.py::patch_paper` 同一个坑：
    #    `model_fields_set` 才是「这次请求实际带了哪些字段」。
    provided = body.model_fields_set
    sets, args = [], []
    if "name" in provided:
        # `plans.name` 是 NOT NULL（`db.py`）⇒ null 不可能是"清空"，只能是非法值。
        if body.name is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "计划名称不能为空")
        sets.append("name=?")
        args.append(body.name)
    if "goal" in provided:                    # null = 清空目标篇数（列可空）
        sets.append("goal=?")
        args.append(body.goal)
    if "description" in provided:
        # 说明清空落成空串而不是 NULL：创建路径的缺省值就是 `""`（`PlanIn.description`），
        # 前端 `Plan.description` 也声明成 string 而非可空 —— 两条路径给同一个值。
        sets.append("description=?")
        args.append("" if body.description is None else body.description)
    if sets:
        sets.append("updated_at=?")
        args += [utcnow(), plan_id]
        with tx(conn):
            conn.execute(f"UPDATE plans SET {', '.join(sets)} WHERE id=?", args)
    return plan_public(dict(conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()))


@router.delete("/{plan_id}", status_code=204)
def delete_plan(plan_id: int, conn: sqlite3.Connection = Depends(get_conn),
                user: dict[str, Any] = Depends(current_user),
                settings: Settings = Depends(get_settings)) -> None:
    """删除一个计划，**连同它名下每篇文献的磁盘产物**。

    `DELETE FROM plans` 只级联数据库里的行（`papers`/`docs`/`blocks`/`notes`/`shares`），
    而 PDF、`papers/p<id>/`、`logs/p<id>.log` 是裸文件 —— 级联碰不到它们。原先这条路径
    **一个文件都不收**：删计划 = 库里干净、盘上原名原样留着（实测见
    `tests/test_delete_paper.py::test_deleting_a_plan_reclaims_every_papers_files`）。

    收尾复用 `_remove_paper_files`（与 `DELETE /api/papers/{id}` 同一个函数）——
    一处实现，两条删除路径共用，不新增第二个删除助手。

    ⚠️ **行必须在删之前读出来**：级联删完就查不到 `pdf_path` 了。
    """
    require_plan(conn, plan_id, user)
    papers = rows_to_list(conn.execute(
        "SELECT id, pdf_path FROM papers WHERE plan_id=?", (plan_id,)).fetchall())
    with tx(conn):                      # 级联删 papers/docs/blocks/notes/shares
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    from .papers import _remove_paper_files

    for paper in papers:
        _remove_paper_files(settings, paper.get("pdf_path"), int(paper["id"]), conn)


@router.get("/{plan_id}/settings")
def get_settings_(plan_id: int, conn: sqlite3.Connection = Depends(get_conn),
                  user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    require_plan(conn, plan_id, user)
    row = conn.execute("SELECT * FROM plan_settings WHERE plan_id=?", (plan_id,)).fetchone()
    return {"glossary": load_json(row["glossary"], []) if row else []}


@router.patch("/{plan_id}/settings")
def patch_settings(plan_id: int, body: GlossaryIn, conn: sqlite3.Connection = Depends(get_conn),
                   user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    require_plan(conn, plan_id, user)
    with tx(conn):
        conn.execute(
            """INSERT INTO plan_settings (plan_id, glossary) VALUES (?,?)
               ON CONFLICT(plan_id) DO UPDATE SET glossary=excluded.glossary""",
            (plan_id, dump_json(body.glossary)),
        )
    return {"glossary": body.glossary}
