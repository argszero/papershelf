"""计划与计划设置 —— 决策①⑦ 的归属载体，⑫ 术语表的挂载点。

⚠️ 一切访问都先过 `require_plan`（用户间完全隔离）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..db import dump_json, load_json, rows_to_list, tx, utcnow
from ..repo import plan_public
from ..security import current_user, get_conn, require_plan

router = APIRouter(prefix="/api/plans", tags=["plans"])


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: int | None = None
    description: str = ""


class PlanPatch(BaseModel):
    name: str | None = None
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
    sets, args = [], []
    for field in ("name", "goal", "description"):
        val = getattr(body, field)
        if val is not None:
            sets.append(f"{field}=?")
            args.append(val)
    if sets:
        sets.append("updated_at=?")
        args += [utcnow(), plan_id]
        with tx(conn):
            conn.execute(f"UPDATE plans SET {', '.join(sets)} WHERE id=?", args)
    return plan_public(dict(conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()))


@router.delete("/{plan_id}", status_code=204)
def delete_plan(plan_id: int, conn: sqlite3.Connection = Depends(get_conn),
                user: dict[str, Any] = Depends(current_user)) -> None:
    require_plan(conn, plan_id, user)
    with tx(conn):                      # 级联删 papers/docs/blocks/notes/shares
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))


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
