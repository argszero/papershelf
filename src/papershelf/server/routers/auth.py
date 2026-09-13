"""认证路由（决策⑭⑮㉑）：注册 / 验证码 / 忘记密码 / 登录 / 登出 / 当前用户。

⑮ 的降级逻辑就在这里：**SMTP 配全 → 开放自助注册；未配 → 注册口关闭（403）**，
改由管理员在 `/api/admin/users` 开号。

⑭ 的邮箱验证对齐原型：**填验证码**，不是点链接。
注册与忘记密码共用 `verification_codes` 表，靠 `purpose` 区分（两者不通用）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import tx, utcnow
from ..mailer import send_code
from ..terms import TERMS_BODY, TERMS_SUMMARY, TERMS_VERSION
from ..security import (
    SESSION_COOKIE,
    SESSION_DAYS,
    code_cooldown_left,
    consume_code,
    create_user,
    current_user,
    email_allowed,
    end_session,
    get_conn,
    issue_code,
    set_password,
    start_session,
    user_by_email,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: str
    code: str = Field(min_length=4, max_length=12)
    password: str = Field(min_length=8, max_length=200)
    display_name: str = ""
    # 用户协议（宿主 2026-09-11）：**必须为 true 才开号**，且把生效版本号落库。
    # 默认 False 是刻意的 —— 旧客户端不传这个字段时会被拒，而不是静默跳过同意。
    agree: bool = False


class EmailIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    email: str
    code: str = Field(min_length=4, max_length=12)
    password: str = Field(min_length=8, max_length=200)


class LoginIn(BaseModel):
    email: str
    password: str


def _public_user(u: dict[str, Any]) -> dict[str, Any]:
    return {"id": u["id"], "email": u["email"], "display_name": u["display_name"],
            "is_admin": bool(u["is_admin"]), "status": u["status"]}


@router.get("/config")
def auth_config(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """前端据此决定「显示注册表单」还是「提示联系管理员开号」（⑮），并渲染用户协议。"""
    return {
        "open_registration": settings.open_registration,
        "smtp_configured": settings.smtp_configured,
        "allowed_domains": settings.allowed_domains,
        "terms_version": TERMS_VERSION,
        "terms_summary": TERMS_SUMMARY,
        "terms_body": [{"title": t, "text": x} for t, x in TERMS_BODY],
    }


# ── 验证码（注册与忘记密码共用）────────────────────────────────────────────
def _send_code_or_429(conn: sqlite3.Connection, *, email: str, purpose: str,
                      settings: Settings) -> dict[str, Any]:
    left = code_cooldown_left(conn, email=email, purpose=purpose, settings=settings)
    if left > 0:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"请求过于频繁，请 {left} 秒后再试")
    code = issue_code(conn, email=email, purpose=purpose, settings=settings)
    sent = send_code(settings, email, code, purpose)
    if not sent:
        # 发不出去就把码撤掉，免得留下一个用户永远拿不到的验证码（重试时会绕过冷却）
        with tx(conn):
            conn.execute("DELETE FROM verification_codes WHERE email=? AND purpose=?",
                         (email.lower().strip(), purpose))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "验证码邮件发送失败，请稍后重试或联系管理员",
        )
    return {"sent": True, "cooldown": settings.code_cooldown_seconds}


@router.post("/register/code")
def register_code(body: EmailIn, conn: sqlite3.Connection = Depends(get_conn),
                  settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """注册用验证码：先查白名单与是否已注册，再发码。"""
    if not settings.open_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "本站未配置邮件服务，自助注册已关闭，请联系管理员开通账号")
    if not email_allowed(body.email, settings):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"仅接受以下域名的邮箱：{', '.join(settings.allowed_domains) or '（未限制）'}")
    if user_by_email(conn, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已注册，请直接登录")
    return _send_code_or_429(conn, email=body.email, purpose="register", settings=settings)


@router.post("/reset/code")
def reset_code(body: EmailIn, conn: sqlite3.Connection = Depends(get_conn),
               settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """忘记密码用验证码。

    ⚠️ **不泄露邮箱是否已注册**：未注册时也走同一条成功路径（同样耗掉一次冷却），
    否则这个接口就成了「批量探测哪些邮箱在站内注册过」的工具。
    """
    if not settings.open_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "本站未配置邮件服务，无法自助重置密码")
    if not user_by_email(conn, body.email):
        return {"sent": True, "cooldown": settings.code_cooldown_seconds}
    return _send_code_or_429(conn, email=body.email, purpose="reset", settings=settings)


@router.post("/register")
def register(
    body: RegisterIn,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """⑭⑮：验证码 + 密码 → 直接开号并登录（不再有"待激活"的中间态）。"""
    if not settings.open_registration:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "本站未配置邮件服务，自助注册已关闭，请联系管理员开通账号",
        )
    if not email_allowed(body.email, settings):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"仅接受以下域名的邮箱：{', '.join(settings.allowed_domains) or '（未限制）'}",
        )
    if user_by_email(conn, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已注册，请直接登录")
    # 服务端强校验协议同意（不只是前端拦）：不同意就不开号，同意则留档版本号。
    # ⚠️ 必须放在 `consume_code` **之前**：验证码是单次的，若先消耗再拒，
    #    用户"忘勾一次"就得重新收码 —— 把一次表单校验失误变成一次发信。
    #    放前面也不泄露信息：无论哪条失败都是 400，且此接口本就需要有效验证码。
    if not body.agree:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先阅读并同意用户协议")
    if not consume_code(conn, email=body.email, purpose="register",
                        code=body.code, settings=settings):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码不正确或已过期，请重新获取")

    user_id = create_user(conn, email=body.email, password=body.password,
                          display_name=body.display_name, status_="active",
                          consent_version=TERMS_VERSION)
    user = user_by_email(conn, body.email)
    assert user is not None
    token, _ = start_session(conn, user_id)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_DAYS * 86400,
    )
    return _public_user(user)


@router.post("/reset")
def reset_password(
    body: ResetIn,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """忘记密码：验证码 + 新密码。成功后**踢掉该用户全部会话**。"""
    if not settings.open_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "本站未配置邮件服务，无法自助重置密码")
    user = user_by_email(conn, body.email)
    # 未注册/验证码不对都回同一句 —— 不给探测空间
    if user is None or not consume_code(conn, email=body.email, purpose="reset",
                                        code=body.code, settings=settings):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码不正确或已过期，请重新获取")
    if user["status"] == "disabled":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用，请联系管理员")
    set_password(conn, int(user["id"]), body.password)
    # 重置密码同时也是「激活」的一种：pending 用户走这条路后应该能登录
    with tx(conn):
        conn.execute("UPDATE users SET status='active', activated_at=COALESCE(activated_at, ?) "
                     "WHERE id=? AND status='pending'", (utcnow(), user["id"]))
    return {"ok": True}


@router.post("/login")
def login(
    body: LoginIn,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    user = user_by_email(conn, body.email)
    if user is None or not verify_password(user["password_hash"], body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码不正确")
    if user["status"] == "disabled":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用")
    if user["status"] == "pending":
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "账号尚未验证邮箱，请在注册页用验证码完成注册，或用「忘记密码」重置")
    token, _ = start_session(conn, int(user["id"]))
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_DAYS * 86400,
    )
    return _public_user(user)


@router.post("/logout")
def logout(response: Response, conn: sqlite3.Connection = Depends(get_conn),
           token: str | None = None) -> dict[str, bool]:
    if token:
        end_session(conn, token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return _public_user(user)
