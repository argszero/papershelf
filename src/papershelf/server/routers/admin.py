"""最小管理页的后端（决策㉑）：列表 / 开号 / 禁用 / 重置密码 + 以他人身份查看。

配置（SMTP、LLM Key、白名单）**不在此处**——决策㉑ 明确走环境变量。
⑮ 未配 SMTP 时，这里是**唯一的开号入口**。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..db import rows_to_list, tx, utcnow
from ..security import (
    SESSION_COOKIE,
    create_user,
    current_admin,
    current_user,
    get_conn,
    hash_password,
    impersonation,
    session_row,
    user_by_email,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])
log = logging.getLogger("papershelf.admin")


class CreateUserIn(BaseModel):
    email: str
    password: str = Field(min_length=8)
    display_name: str = ""
    is_admin: bool = False


class PatchUserIn(BaseModel):
    status_: str | None = None      # active | disabled
    password: str | None = None
    is_admin: bool | None = None


class ImpersonateIn(BaseModel):
    user_id: int


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


# ── 以他人身份查看（impersonation，2026-10-26）─────────────────────────────
# 验收口径见 rant 2026-10-26T14:34:05。三条设计取舍：
#
# ① 状态存在**会话**上，不在进程里。`sessions.user_id` 永远是真正登录的人，
#    目标只是会话上多出来的一列 ⇒ 管理员登出或会话超时，冒充随之消失，
#    不需要第二条清理路径，也不可能出现"进程重启后忘了谁在冒充"。
# ② 生效点是 `security.session_user` —— `require_paper` / `require_plan` 一个字没改。
#    它们照旧拿解析出来的 id 去比 `pl.user_id`，于是冒充期间管理员看到的就是目标的
#    真实数据，而普通用户之间仍然完全隔离（决策①）。放宽归属校验等于对所有人开门。
# ③ 不嵌套是结构性的：冒充期间 `current_user` 是目标用户（普通用户），
#    `current_admin` 因此直接 403 ⇒ 整个 `/api/admin/*` 此时不可达，
#    也就没人能在冒充中再发起一次冒充，或改到目标的账号设置。


def _client_ip(request: Request) -> str:
    """来源 IP。生产有两层 nginx，先看 `X-Forwarded-For` 的第一跳。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _audit(event: str, request: Request, *, actor_id: int, target_id: int) -> None:
    """开始与结束各一行（验收 5）：时间、管理员 id、目标用户 id、来源 IP。

    `WARNING` 而不是 `INFO`：这是一次权限旁路，任何日志级别下都必须看得见；
    审计行被默认级别滤掉就等于没有审计。
    """
    log.warning("impersonate.%s actor=%s target=%s ip=%s at=%s",
                event, actor_id, target_id, _client_ip(request), utcnow())


@router.post("/impersonate")
def start_impersonation(body: ImpersonateIn, request: Request,
                        conn: sqlite3.Connection = Depends(get_conn),
                        admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
    """以某个用户身份查看：此后所有页面按该用户的视野渲染。"""
    if body.user_id == admin["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能以自己身份查看")
    row = conn.execute("SELECT * FROM users WHERE id=?", (body.user_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if row["status"] != "active":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目标用户已停用")
    if row["is_admin"]:
        # 验收 4「不得经此修改对方的账号设置」原本只在目标**也是管理员**时漏：
        # 那种情况下 `/api/admin/*` 在冒充期间仍然可达。与其给每个端点各加一道
        # "是不是在冒充"的判断，不如让这条旁路只通向普通用户 —— 运维要看的是
        # 普通用户的文献，而管理员的账号本来就不该被另一名管理员顶替操作。
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能以另一名管理员身份查看")
    token = request.cookies.get(SESSION_COOKIE)
    sess = session_row(conn, token)
    if sess is None:                    # `current_admin` 已保证有会话，这里只是防御
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    with tx(conn):
        conn.execute("UPDATE sessions SET act_as_user_id=?, act_as_at=? WHERE token=?",
                     (body.user_id, utcnow(), token))
    _audit("start", request, actor_id=admin["id"], target_id=body.user_id)
    return _public(dict(row))


@router.post("/impersonate/stop")
def stop_impersonation(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                       user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """退出冒充，完整回到管理员自身身份。

    这里**只能**挂 `current_user`：冒充期间管理员已经不是管理员了，`current_admin`
    会把退出按钮一起锁死 —— 那就再也退不出来，只能清 cookie。
    管理员 id 从会话上反推，`user["id"]` 是目标。
    """
    token = request.cookies.get(SESSION_COOKIE)
    imp = impersonation(conn, token)
    if imp is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前没有在冒充")
    with tx(conn):
        conn.execute("UPDATE sessions SET act_as_user_id=NULL, act_as_at=NULL WHERE token=?",
                     (token,))
    _audit("stop", request, actor_id=imp["actor_id"], target_id=user["id"])
    actor = conn.execute("SELECT * FROM users WHERE id=?", (imp["actor_id"],)).fetchone()
    if actor is None:                   # 理论上不可能（`session_row` 刚 JOIN 过）
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    return _public(dict(actor))
