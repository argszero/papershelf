"""最小管理页的后端（决策㉑）：列表 / 开号 / 禁用 / 重置密码。

配置（SMTP、LLM Key、白名单）**不在此处**——决策㉑ 明确走环境变量。
⑮ 未配 SMTP 时，这里是**唯一的开号入口**。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..db import rows_to_list, tx, utcnow
from ..security import create_user, current_admin, get_conn, hash_password, user_by_email

router = APIRouter(prefix="/api/admin", tags=["admin"])


class CreateUserIn(BaseModel):
    email: str
    password: str = Field(min_length=8)
    display_name: str = ""
    is_admin: bool = False


class PatchUserIn(BaseModel):
    status_: str | None = None      # active | disabled
    password: str | None = None
    is_admin: bool | None = None


def _public(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "email": r["email"], "display_name": r["display_name"],
            "status": r["status"], "is_admin": bool(r["is_admin"]),
            "created_at": r["created_at"], "activated_at": r["activated_at"]}


@router.get("/users")
def list_users(conn: sqlite3.Connection = Depends(get_conn),
               admin: dict[str, Any] = Depends(current_admin)) -> list[dict[str, Any]]:
    return [_public(r) for r in rows_to_list(conn.execute(
        "SELECT * FROM users ORDER BY created_at DESC").fetchall())]


@router.post("/users", status_code=201)
def create(body: CreateUserIn, conn: sqlite3.Connection = Depends(get_conn),
           admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
    if user_by_email(conn, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已存在")
    # 管理员开号 → 直接 active（⑮ 未配 SMTP 时的路径，不需要邮件激活）
    user_id = create_user(conn, email=body.email, password=body.password,
                          display_name=body.display_name, status_="active",
                          is_admin=body.is_admin)
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _public(dict(row))


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: PatchUserIn,
               conn: sqlite3.Connection = Depends(get_conn),
               admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if body.status_ is not None and body.status_ not in ("active", "disabled"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "状态取值不合法")
    if row["is_admin"] and (body.status_ == "disabled" or body.is_admin is False):
        others = conn.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin=1 AND id<>?",
                              (user_id,)).fetchone()["n"]
        if others == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能禁用/降级最后一名管理员")

    sets, args = [], []
    if body.status_ is not None:
        sets.append("status=?")
        args.append(body.status_)
        if body.status_ == "active" and not row["activated_at"]:
            sets.append("activated_at=?")
            args.append(utcnow())
    if body.password:
        sets.append("password_hash=?")
        args.append(hash_password(body.password))
    if body.is_admin is not None:
        sets.append("is_admin=?")
        args.append(1 if body.is_admin else 0)
    if sets:
        args.append(user_id)
        with tx(conn):
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)
    return _public(dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()))


@router.get("/stats")
def stats(conn: sqlite3.Connection = Depends(get_conn),
          admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
    """用量可见性的最小形态（待定项 6）：谁在烧统一 Key 的钱（决策⑧）。"""
    rows = rows_to_list(conn.execute(
        """SELECT u.id, u.email,
                  COUNT(DISTINCT p.id) AS papers,
                  COALESCE(SUM(p.tokens_used), 0) AS tokens
           FROM users u
           LEFT JOIN plans pl ON pl.user_id = u.id
           LEFT JOIN papers p ON p.plan_id = pl.id
           GROUP BY u.id ORDER BY tokens DESC"""
    ).fetchall())
    return {"per_user": rows}
