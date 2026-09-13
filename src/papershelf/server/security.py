"""认证与授权 —— 决策①⑦⑭⑮㉑ 的落地。

- 密码：argon2id
- 会话：**服务端 sessions 表 + 签名 cookie**（待定项 1 的提案）——
  存表是为了能撤销、能列出登录设备；签名是为了防篡改。
- 注册：⑭ 限 `.edu.cn` 白名单；⑮ SMTP 未配则**自动关闭**自助注册（改由管理员开号）。
- 隔离：一切资源访问都要过 `require_plan_owner` / `require_paper_owner`（决策① 用户间完全隔离）。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status

from .config import Settings, get_settings
from .db import dump_json, expires_in, new_token, rows_to_list, utcnow, tx

SESSION_COOKIE = "papershelf_session"
SESSION_DAYS = 30

_ph = PasswordHasher()
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


# ── 密码 ──────────────────────────────────────────────────────────────────
def hash_password(raw: str) -> str:
    return _ph.hash(raw)


def verify_password(stored: str, raw: str) -> bool:
    try:
        return _ph.verify(stored, raw)
    except (VerifyMismatchError, Exception):
        return False


# ── 邮箱白名单（⑭）────────────────────────────────────────────────────────
def email_allowed(email: str, settings: Settings) -> bool:
    if not _EMAIL_RE.match(email or ""):
        return False
    domains = settings.allowed_domains
    if not domains:
        return True
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in domains)


# ── 注册 / 激活 ───────────────────────────────────────────────────────────
def create_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    display_name: str = "",
    status_: str = "pending",
    is_admin: bool = False,
    consent_version: str | None = None,
) -> int:
    """建号。`consent_version` 非空时**同时记录同意时刻**（用户协议的举证凭据）。"""
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO users
                 (email, password_hash, display_name, status, is_admin, created_at,
                  consent_version, consent_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (email.lower().strip(), hash_password(password), display_name,
             status_, 1 if is_admin else 0, utcnow(),
             consent_version, utcnow() if consent_version else None),
        )
        return int(cur.lastrowid)


def user_by_email(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
    return dict(row) if row else None


def activate_user(conn: sqlite3.Connection, user_id: int) -> None:
    with tx(conn):
        conn.execute("UPDATE users SET status='active', activated_at=? WHERE id=?",
                     (utcnow(), user_id))


# ── 邮箱验证码（⑭：注册与忘记密码共用）────────────────────────────────────
# 只存哈希：验证码 6 位数字、空间只有 10^6，但库里落哈希能挡住"读到库直接登录"。
# 单次有效（用完即删）+ 失败次数上限（超限作废）→ 暴力尝试在 TTL 内不可能成功。
CODE_PURPOSES = ("register", "reset")


def _hash_code(email: str, purpose: str, code: str) -> str:
    """带 email/purpose 盐，避免同一验证码在不同邮箱/用途间通用。"""
    return hashlib.sha256(f"{email.lower().strip()}|{purpose}|{code}".encode()).hexdigest()


def issue_code(conn: sqlite3.Connection, *, email: str, purpose: str, settings: Settings) -> str:
    """生成并存下一个验证码，返回**明文**（只此一次，供发信）。

    ⚠️ 冷却时间在调用方之前判（要能返回"还要等 N 秒"），这里只负责写库。
    """
    email = email.lower().strip()
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.now(timezone.utc)
    exp = (now + timedelta(seconds=max(60, settings.code_ttl_seconds))).isoformat(timespec="seconds")
    with tx(conn):
        conn.execute(
            """INSERT INTO verification_codes (email, purpose, code_hash, attempts, created_at, expires_at)
               VALUES (?,?,?,0,?,?)
               ON CONFLICT(email, purpose) DO UPDATE SET
                 code_hash=excluded.code_hash, attempts=0,
                 created_at=excluded.created_at, expires_at=excluded.expires_at""",
            (email, purpose, _hash_code(email, purpose, code),
             now.isoformat(timespec="seconds"), exp),
        )
    return code


def code_cooldown_left(conn: sqlite3.Connection, *, email: str, purpose: str,
                       settings: Settings) -> int:
    """同邮箱同用途的剩余冷却秒数（0 = 可以发）。"""
    row = conn.execute(
        "SELECT created_at FROM verification_codes WHERE email=? AND purpose=?",
        (email.lower().strip(), purpose),
    ).fetchone()
    if row is None:
        return 0
    try:
        sent = datetime.fromisoformat(row["created_at"])
    except (TypeError, ValueError):
        return 0
    elapsed = (datetime.now(timezone.utc) - sent).total_seconds()
    return max(0, int(settings.code_cooldown_seconds - elapsed))


def consume_code(conn: sqlite3.Connection, *, email: str, purpose: str, code: str,
                 settings: Settings) -> bool:
    """校验并**作废**验证码（单次有效）。失败累加次数，超限直接删掉要求重取。

    返回 False 时调用方只需回一句"验证码不正确或已过期" ——
    不区分"没发过/过期/码错"，避免给出可用于枚举的差异信息。
    """
    email = email.lower().strip()
    row = conn.execute(
        "SELECT * FROM verification_codes WHERE email=? AND purpose=?", (email, purpose)
    ).fetchone()
    if row is None:
        return False
    expired = row["expires_at"] <= utcnow()
    ok = (not expired) and secrets.compare_digest(
        str(row["code_hash"]), _hash_code(email, purpose, (code or "").strip())
    )
    if not ok:
        attempts = int(row["attempts"]) + 1
        with tx(conn):
            if expired or attempts >= max(1, settings.code_max_attempts):
                conn.execute("DELETE FROM verification_codes WHERE email=? AND purpose=?",
                             (email, purpose))
            else:
                conn.execute(
                    "UPDATE verification_codes SET attempts=? WHERE email=? AND purpose=?",
                    (attempts, email, purpose),
                )
        return False
    with tx(conn):
        conn.execute("DELETE FROM verification_codes WHERE email=? AND purpose=?", (email, purpose))
    return True


def set_password(conn: sqlite3.Connection, user_id: int, new_password: str) -> None:
    """重置口令，并**踢掉该用户所有会话**（改密后旧 cookie 必须立即失效）。"""
    with tx(conn):
        conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                     (hash_password(new_password), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def ensure_admin(conn: sqlite3.Connection, email: str, password: str) -> int:
    """首次启动引导：`PAPERSHELF_ADMIN_EMAIL` 指定的账号成为管理员（§7）。

    ⚠️ 安全约束（**不要改回默认密码**）：绝不允许"没设密码就随机生成"这种静默开号——
    自托管实例一旦暴露端口，配错的 ADMIN_EMAIL 配上猜不到的密码等于把人锁在门外，
    而随机密码又只写进日志/无处可见。所以**必须显式给出密码**，否则直接报错，
    引导失败可见、可修，不给任何后门。
    """
    if not password or len(password) < 8:
        raise ValueError(
            "PAPERSHELF_ADMIN_PASSWORD 未设置或过短（≥8 位）；"
            "首次引导需要它来创建/校验管理员账号"
        )
    existing = user_by_email(conn, email)
    if existing:
        with tx(conn):
            conn.execute(
                "UPDATE users SET is_admin=1, status='active', password_hash=? WHERE id=?",
                (hash_password(password), existing["id"]),
            )
        return int(existing["id"])
    return create_user(conn, email=email, password=password,
                       display_name="管理员", status_="active", is_admin=True)


# ── 会话 ──────────────────────────────────────────────────────────────────
def start_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    token = new_token(32)
    exp = expires_in(SESSION_DAYS)
    with tx(conn):
        conn.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                     (token, user_id, utcnow(), exp))
    return token, exp or ""


def end_session(conn: sqlite3.Connection, token: str) -> None:
    with tx(conn):
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def session_user(conn: sqlite3.Connection, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token=? AND (s.expires_at IS NULL OR s.expires_at > ?)""",
        (token, utcnow()),
    ).fetchone()
    if row is None:
        return None
    if row["status"] != "active":
        return None
    return dict(row)


# ── FastAPI 依赖 ──────────────────────────────────────────────────────────
def get_conn(request: Request):
    """每请求一个连接（SQLite 单文件，够用；WAL 下并发读不互相阻塞）。"""
    conn = request.app.state.conn_factory()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn=Depends(get_conn)) -> dict[str, Any]:
    user = session_user(conn, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    return user


def current_admin(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if not user.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


# ── 归属校验（决策① 用户间完全隔离）──────────────────────────────────────
def require_plan(conn: sqlite3.Connection, plan_id: int, user: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None or row["user_id"] != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "计划不存在")
    return dict(row)


def require_paper(conn: sqlite3.Connection, paper_id: int, user: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        """SELECT p.* FROM papers p JOIN plans pl ON pl.id = p.plan_id
           WHERE p.id=? AND pl.user_id=?""",
        (paper_id, user["id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
    return dict(row)


__all__ = [
    "SESSION_COOKIE", "SESSION_DAYS", "CODE_PURPOSES", "activate_user", "code_cooldown_left",
    "consume_code", "create_user", "current_admin",
    "current_user", "email_allowed", "end_session", "ensure_admin", "get_conn",
    "hash_password", "issue_code", "require_paper", "require_plan", "session_user",
    "set_password", "start_session",
    "user_by_email", "verify_password",
]
