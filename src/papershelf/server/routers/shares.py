"""分享 —— 决策⑩⑪：**实时只读视图**（不是快照）+ 可撤销 + 可设有效期。

安全要点（§4 末尾的硬约束）：**只读分享的写保护必须由后端拒绝**。
匿名访问只能走 `GET /api/shares/{token}*` 这一组白名单，其它一律 403（见 app.py 的中间件）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..config import SHARE_MAX_HOURS, Settings, get_settings
from ..db import expires_in, new_token, tx, utcnow
from ..repo import (doc_public, list_highlights, list_notes, load_doc, paper_public,
                    plan_public, plan_stats)
from ..security import current_user, get_conn, require_plan

router = APIRouter(prefix="/api", tags=["shares"])


def resolve_share(conn: sqlite3.Connection, token: str) -> dict[str, Any]:
    """校验分享令牌：存在、未撤销、未过期。"""
    row = conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享链接无效")
    share = dict(row)
    if share["revoked_at"]:
        raise HTTPException(status.HTTP_410_GONE, "分享链接已撤销")
    if share["expires_at"] and share["expires_at"] < utcnow():
        raise HTTPException(status.HTTP_410_GONE, "分享链接已过期")
    return share


class ShareIn(BaseModel):
    # 有效期（小时），上限 SHARE_MAX_HOURS=24（宿主 2026-09-11 指示）。
    # 超出上限不报错，直接钳到上限 —— 旧的 `days=7` 调用会退化成 7 小时而不是 7 天，
    # 宁可更短不可更长：这是"最长 24 小时"这类约束该有的失败方向。
    hours: int = 0
    # 备注（2026-09-12「分享管理」）：给链接起个名（"给导师看 / 组会前"），
    # 链接一多，光看 token 尾号认不出哪条是哪条。
    label: str = ""


def _share_row(row: sqlite3.Row, settings: Settings) -> dict[str, Any]:
    """把一行 shares 补成前端要的形状 —— 列表与创建/续期的返回**必须同形**。

    加了 `state` 与 `seconds_left` 两个**服务端算好的**字段：倒计时是这份数据里
    唯一会随时间变的东西，各页面各算一遍迟早会有一处忘了算（分享页/管理页/新建结果
    三处都要显示）。服务端给一份统一的剩余秒数，前端只负责每秒递减。
    """
    share = dict(row)
    share["url"] = f"{settings.base_url}/share/{share['token']}"
    exp = share.get("expires_at")
    now = utcnow()
    if share.get("revoked_at"):
        share["state"] = "revoked"
    elif exp and exp < now:
        share["state"] = "expired"
    else:
        share["state"] = "active"
    share["seconds_left"] = _seconds_left(exp, now)
    # 「即将到期」= 剩余不足 1 小时（原型 `shareState` 同款阈值）。
    # 由服务端判而不是前端各自比一遍：三处显示（banner / 管理页 / 新建结果）
    # 的告警色必须同时出现，否则同一条链接会一会儿红一会儿不红。
    if share["state"] == "active" and 0 <= share["seconds_left"] < 3600:
        share["state"] = "expiring"
    return share


def _seconds_left(expires_at: str | None, now: str) -> int:
    """剩余秒数；无法解析/无到期时间 → 0（当作已过期，失败方向是**更保守**）。"""
    if not expires_at:
        return 0
    try:
        return max(0, int((datetime.fromisoformat(expires_at) - datetime.fromisoformat(now))
                          .total_seconds()))
    except ValueError:
        return 0


@router.post("/plans/{plan_id}/shares", status_code=201)
def create_share(plan_id: int, body: ShareIn | None = None,
                 conn: sqlite3.Connection = Depends(get_conn),
                 user: dict[str, Any] = Depends(current_user),
                 settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    require_plan(conn, plan_id, user)
    token = new_token(24)
    hours = (body.hours if body else 0) or settings.share_default_hours
    hours = max(1, min(int(hours), SHARE_MAX_HOURS))
    exp = expires_in(hours)
    label = (body.label if body else "").strip()[:30]
    with tx(conn):
        conn.execute(
            "INSERT INTO shares (token, plan_id, created_at, expires_at, label, hours)"
            " VALUES (?,?,?,?,?,?)",
            (token, plan_id, utcnow(), exp, label, hours))
    row = conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    return _share_row(row, settings)


@router.get("/plans/{plan_id}/shares")
def list_shares(plan_id: int, conn: sqlite3.Connection = Depends(get_conn),
                user: dict[str, Any] = Depends(current_user),
                settings: Settings = Depends(get_settings)) -> list[dict[str, Any]]:
    require_plan(conn, plan_id, user)
    rows = conn.execute(
        "SELECT * FROM shares WHERE plan_id=? ORDER BY created_at DESC", (plan_id,)).fetchall()
    return [_share_row(r, settings) for r in rows]


@router.get("/shares")
def list_all_shares(conn: sqlite3.Connection = Depends(get_conn),
                    user: dict[str, Any] = Depends(current_user),
                    settings: Settings = Depends(get_settings)) -> list[dict[str, Any]]:
    """**跨计划**的分享列表 —— 「分享管理」页（原型 `renderShares`）看的是全部链接。

    为什么要单独一个端点而不是让前端把每个计划的列表拼起来：分享管理页要按
    状态筛选、要显示计划名；逐个计划取数在链接多时是 N+1 次往返，且**筛选口径**
    会散到前端。`plan_name` 一并带出，前端不必再去计划列表里对号。
    """
    rows = conn.execute(
        """SELECT s.*, p.name AS plan_name FROM shares s
           JOIN plans p ON p.id = s.plan_id
           WHERE p.user_id=? ORDER BY s.created_at DESC""",
        (user["id"],)).fetchall()
    return [{**_share_row(r, settings), "plan_name": r["plan_name"]} for r in rows]


@router.post("/shares/{token}/renew")
def renew_share(token: str, body: ShareIn | None = None,
                conn: sqlite3.Connection = Depends(get_conn),
                user: dict[str, Any] = Depends(current_user),
                settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """重置有效期（原型「重置有效期」）：从**现在**起算，不是从原到期时间顺延。

    从原到期时间顺延会让"续期"变成"排队"，而且反复续期能把链接一路推到
    24 小时上限之外（`exp` 越滚越大）—— 上限就成了纸面上的。
    """
    row = conn.execute(
        """SELECT s.* FROM shares s JOIN plans p ON p.id = s.plan_id
           WHERE s.token=? AND p.user_id=?""",
        (token, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享链接不存在")
    hours = (body.hours if body else 0) or settings.share_default_hours
    hours = max(1, min(int(hours), SHARE_MAX_HOURS))
    exp = expires_in(hours)
    label = ((body.label if body else "").strip()[:30]) or row["label"] or ""
    with tx(conn):
        # 续期顺带**复活**已撤销/已过期的链接（原型 `rec.revoked = false` 同款）：
        # 管理页上"重置有效期"对一条死链是有意义的操作，不然只能重新建一条。
        conn.execute(
            "UPDATE shares SET expires_at=?, revoked_at=NULL, label=?, hours=? WHERE token=?",
            (exp, label, hours, token))
    return _share_row(conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone(),
                      settings)


@router.delete("/shares/{token}", status_code=204)
def revoke_share(token: str, conn: sqlite3.Connection = Depends(get_conn),
                 user: dict[str, Any] = Depends(current_user)) -> None:
    row = conn.execute(
        """SELECT s.* FROM shares s JOIN plans p ON p.id = s.plan_id
           WHERE s.token=? AND p.user_id=?""",
        (token, user["id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享链接不存在")
    with tx(conn):
        conn.execute("UPDATE shares SET revoked_at=? WHERE token=?", (utcnow(), token))


# ── 匿名只读（无需登录，决策⑪ 持链接即可看）────────────────────────────────
@router.get("/shares/{token}")
def share_plan(token: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """⑩ 的取数口：**形状必须与登录态一致**（`GET /api/plans` + `.../papers`）。

    为什么要同形（宿主 2026-09-11：「分享后的页面应该和分享者看到的一模一样」）：
    分享页不再是另写的一套精简视图，而是**复用整站页面**（总览/文献库/看板/阅读器/计划）
    只读化 —— 那些页面读的是完整 `Plan` 与完整 `Paper`。少一个字段（例如 `goal`、
    `tags`、`conv_error`），分享页就会集体渲染成 `undefined`，而 TS 不会报错。
    """
    share = resolve_share(conn, token)
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (share["plan_id"],)).fetchone()
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享内容已删除")
    papers = [paper_public(dict(r)) for r in conn.execute(
        "SELECT * FROM papers WHERE plan_id=? ORDER BY created_at DESC",
        (share["plan_id"],),
    ).fetchall()]
    total, done = plan_stats(conn, share["plan_id"])
    # `expires_at` / `seconds_left` 一并下发：分享页 banner 要显示**有效期倒计时**
    # （宿主 2026-09-12）。匿名访客没有管理端的列表可查，这条链接自己的到期时间
    # 必须随这份载荷一起到达，否则前端只能去猜。
    return {
        "plan": plan_public(dict(plan), total, done),
        "papers": papers,
        "readonly": True,
        "share": {
            "token": share["token"],
            "label": share.get("label") or "",
            "expires_at": share["expires_at"],
            "seconds_left": _seconds_left(share["expires_at"], utcnow()),
        },
    }


@router.get("/shares/{token}/plans")
def share_plans(token: str, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    """侧栏计划切换器的数据源（分享是**单计划视图**，数组里只有一个）。

    为什么要这个端点而不是让前端拿 `share_plan` 里的 `plan` 拼一个单元素数组：
    侧栏/计划页读的是 `Plan[]`（`paper_count` / `done_count` 计数来自服务端）。
    前端自己拼就会出现两套"计划卡"的取数逻辑，口径迟早漂移。
    """
    share = resolve_share(conn, token)
    row = conn.execute("SELECT * FROM plans WHERE id=?", (share["plan_id"],)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享内容已删除")
    return [plan_public(dict(row), *plan_stats(conn, share["plan_id"]))]


@router.get("/shares/{token}/papers/{paper_id}/notes")
def share_notes(token: str, paper_id: int,
                conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    """分享者的笔记（原型分享模态的原话：「查看该计划的全部文献、**进度与笔记**」）。

    只读 —— 写入口由 app.py 的中间件按 token 一律 403（⑪ 的硬约束在后端）。
    """
    share = resolve_share(conn, token)
    row = conn.execute("SELECT id FROM papers WHERE id=? AND plan_id=?",
                       (paper_id, share["plan_id"])).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不在分享范围内")
    return list_notes(conn, paper_id)


@router.get("/shares/{token}/papers/{paper_id}/highlights")
def share_highlights(token: str, paper_id: int,
                     conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    """分享者的划痕（决策㉛）。只读 —— 写入口由 app.py 的中间件按 token 一律 403。

    分享页**要看得见划痕**：分享者划的重点正是他给读者的导读
    （原型同理：`is-hl`/`hl` 的应用不判只读态，只有写操作才判）。
    """
    share = resolve_share(conn, token)
    row = conn.execute("SELECT id FROM papers WHERE id=? AND plan_id=?",
                       (paper_id, share["plan_id"])).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不在分享范围内")
    return list_highlights(conn, paper_id)


@router.get("/shares/{token}/papers/{paper_id}")
def share_paper(token: str, paper_id: int,
                conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """实时只读：拿到的是分享者**当前**的块级数据（决策⑩），不是快照。"""
    share = resolve_share(conn, token)
    row = conn.execute("SELECT * FROM papers WHERE id=? AND plan_id=?",
                       (paper_id, share["plan_id"])).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不在分享范围内")
    data = doc_public(conn, paper_id, asset_prefix=f"/share/{token}/assets")
    if data is None:
        # ⑪ 只读分享是**无鉴权**入口：状态码之外必须给一句人话，否则匿名读者
        # 只会看到一个 409 数字（前端兜底文案是给登录用户写的「稍后自动刷新」）。
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "该文献尚未转换完成，转换完成后刷新本页即可阅读")
    return data


@router.get("/shares/{token}/papers/{paper_id}/export")
def share_export(token: str, paper_id: int, request: Request, lang: str = "dual",
                 conn: sqlite3.Connection = Depends(get_conn)):
    from fastapi.responses import HTMLResponse

    from ...pipeline import synth

    share = resolve_share(conn, token)
    row = conn.execute("SELECT * FROM papers WHERE id=? AND plan_id=?",
                       (paper_id, share["plan_id"])).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不在分享范围内")
    doc = load_doc(conn, paper_id)
    if doc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "该文献尚未转换完成")
    mode = "dual" if lang == "dual" else ("en" if lang == "en" else "zh")
    html = synth(doc.blocks, mode, title=doc.meta.get("title_zh") or "",
                 meta_line=doc.meta.get("authors", ""),
                 asset_prefix=f"/share/{token}/assets")
    html = request.app.state.inline_images(html, paper_id)
    return HTMLResponse(html, headers={
        "Content-Disposition": f'inline; filename="{paper_id}-{mode}.html"'
    })
