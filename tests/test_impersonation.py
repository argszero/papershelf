"""管理员「以他人身份查看」（impersonation）的护栏。

rant 2026-10-26T14:34:05 的验收项逐条落在这里：身份解析与横幅数据（1/2）、
目标数据真的可见（3）、冒充碰不到账号设置（4）、审计两行（5）、六条边界（6）。

**核心不变量**：`require_paper` / `require_plan` 一个字没改。冒充能生效，
只因为 `security.session_user` 把这条会话解析成了目标用户；普通用户之间
仍然完全隔离（决策①）—— 所以这里既验"目标的数据进来了"，
也验"第三方的数据仍然 404"。
"""

from __future__ import annotations

import logging

from papershelf.server.security import SESSION_COOKIE


def _login(client, email: str, password: str) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _seed_paper(settings, user_id: int, title: str = "p") -> int:
    """直接建一条 plan + 一条 paper —— 这里要验的是**归属**，不是转换管线。"""
    from papershelf.server.db import connect, utcnow

    conn = connect(settings)
    try:
        plan_id = conn.execute(
            "INSERT INTO plans (user_id, name, created_at) VALUES (?,?,?)",
            (user_id, f"plan-of-{user_id}", utcnow())).lastrowid
        paper_id = conn.execute(
            "INSERT INTO papers (plan_id, title, conv_state, created_at, updated_at) "
            "VALUES (?,?,'done',?,?)",
            (plan_id, title, utcnow(), utcnow())).lastrowid
        conn.commit()
        return paper_id
    finally:
        conn.close()


def _uid(settings, email: str) -> int:
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        return conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
    finally:
        conn.close()


def _set_status(settings, user_id: int, status_: str) -> None:
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        conn.execute("UPDATE users SET status=? WHERE id=?", (status_, user_id))
        conn.commit()
    finally:
        conn.close()


def _admin_and_target(client, settings, make_user):
    """建一个管理员 + 一个普通用户，以管理员身份登录，返回 (管理员 id, 目标 id)。"""
    a_email, a_pw = make_user("admin@tsinghua.edu.cn", is_admin=True)
    b_email, _ = make_user("reader@tsinghua.edu.cn")
    _login(client, a_email, a_pw)
    return _uid(settings, a_email), _uid(settings, b_email)


def _impersonate(client, user_id: int):
    return client.post("/api/admin/impersonate", json={"user_id": user_id})


# ── 1/2 · 身份解析与横幅数据 ──────────────────────────────────────────────

def test_me_reports_the_target_and_the_actor(client, settings, make_user):
    admin_id, target_id = _admin_and_target(client, settings, make_user)
    assert _impersonate(client, target_id).status_code == 200

    me = client.get("/api/auth/me").json()
    assert me["id"] == target_id
    assert me["email"] == "reader@tsinghua.edu.cn"
    assert me["is_admin"] is False          # 冒充期间不再是管理员
    assert me["impersonating"]["actor_id"] == admin_id
    assert me["impersonating"]["actor_email"] == "admin@tsinghua.edu.cn"
    assert me["impersonating"]["since"]


def test_me_reports_no_impersonation_when_absent(client, make_user):
    email, pw = make_user("solo@tsinghua.edu.cn")
    _login(client, email, pw)
    assert client.get("/api/auth/me").json()["impersonating"] is None


# ── 3 · 目标的数据真的进来了，自己的数据真的出去了 ────────────────────────

def test_paper_visibility_follows_the_resolved_identity(client, settings, make_user):
    admin_id, target_id = _admin_and_target(client, settings, make_user)
    admin_paper = _seed_paper(settings, admin_id, "admin-paper")
    target_paper = _seed_paper(settings, target_id, "target-paper")

    # 未冒充：自己那篇在，目标那篇 404（决策① 隔离）
    assert client.get(f"/api/papers/{admin_paper}/doc").status_code != 404
    assert client.get(f"/api/papers/{target_paper}/doc").status_code == 404

    assert _impersonate(client, target_id).status_code == 200

    # 冒充后完全对调 —— 同一个 cookie，没有改任何归属校验
    assert client.get(f"/api/papers/{target_paper}/doc").status_code != 404
    assert client.get(f"/api/papers/{admin_paper}/doc").status_code == 404


def test_a_third_party_stays_isolated_while_impersonating(client, settings, make_user):
    _, target_id = _admin_and_target(client, settings, make_user)
    c_email, _ = make_user("third@tsinghua.edu.cn")
    third_paper = _seed_paper(settings, _uid(settings, c_email), "third-paper")

    assert _impersonate(client, target_id).status_code == 200
    assert client.get(f"/api/papers/{third_paper}/doc").status_code == 404


# ── 4 · 冒充碰不到账号设置（也不可达管理接口）─────────────────────────────

def test_admin_surface_is_forbidden_while_impersonating(client, settings, make_user):
    _, target_id = _admin_and_target(client, settings, make_user)
    assert _impersonate(client, target_id).status_code == 200

    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/stats").status_code == 403
    # 连"改目标密码"这条路也进不去 —— 那是验收 4 要求的不变量
    patch = client.patch(f"/api/admin/users/{target_id}", json={"password": "newpassword1"})
    assert patch.status_code == 403


def test_impersonation_does_not_change_the_target_password(client, settings, make_user):
    b_email, b_pw = make_user("reader@tsinghua.edu.cn")
    a_email, a_pw = make_user("admin@tsinghua.edu.cn", is_admin=True)
    _login(client, a_email, a_pw)
    assert _impersonate(client, _uid(settings, b_email)).status_code == 200

    client.post("/api/auth/logout")
    # 目标自己的口令照旧可用（验收 4：不影响其正常登录）
    assert client.post("/api/auth/login",
                       json={"email": b_email, "password": b_pw}).status_code == 200


# ── 5 · 审计 ──────────────────────────────────────────────────────────────

def test_start_and_stop_each_log_one_audit_line(client, settings, make_user, caplog):
    admin_id, target_id = _admin_and_target(client, settings, make_user)

    with caplog.at_level(logging.WARNING, logger="papershelf.admin"):
        assert _impersonate(client, target_id).status_code == 200
        assert client.post("/api/admin/impersonate/stop").status_code == 200

    lines = [r.getMessage() for r in caplog.records if "impersonate." in r.getMessage()]
    assert len(lines) == 2, lines
    assert "impersonate.start" in lines[0]
    assert "impersonate.stop" in lines[1]
    for line in lines:
        assert f"actor={admin_id}" in line      # 谁
        assert f"target={target_id}" in line    # 顶了谁
        assert "ip=" in line and "at=" in line  # 来源与时间


# ── 6 · 边界 ──────────────────────────────────────────────────────────────

def test_non_admin_is_forbidden(client, settings, make_user):
    email, pw = make_user("plain@tsinghua.edu.cn")
    other, _ = make_user("other@tsinghua.edu.cn")
    _login(client, email, pw)
    assert _impersonate(client, _uid(settings, other)).status_code == 403


def test_stop_restores_the_admin_identity(client, settings, make_user):
    admin_id, target_id = _admin_and_target(client, settings, make_user)
    admin_paper = _seed_paper(settings, admin_id, "admin-paper")
    assert _impersonate(client, target_id).status_code == 200

    r = client.post("/api/admin/impersonate/stop")
    assert r.status_code == 200 and r.json()["id"] == admin_id

    me = client.get("/api/auth/me").json()
    assert me["id"] == admin_id and me["is_admin"] is True
    assert me["impersonating"] is None
    assert client.get(f"/api/papers/{admin_paper}/doc").status_code != 404
    assert client.get("/api/admin/users").status_code == 200


def test_stop_without_impersonating_is_a_400(client, make_user):
    email, pw = make_user("plain@tsinghua.edu.cn")
    _login(client, email, pw)
    assert client.post("/api/admin/impersonate/stop").status_code == 400


def test_no_nesting(client, settings, make_user):
    """冒充期间 `/api/admin/*` 整体 403 —— 嵌套因此结构上不可能。"""
    _, target_id = _admin_and_target(client, settings, make_user)
    assert _impersonate(client, target_id).status_code == 200
    assert _impersonate(client, target_id).status_code == 403


def test_unknown_target_is_a_404(client, settings, make_user):
    _admin_and_target(client, settings, make_user)
    assert _impersonate(client, 9999).status_code == 404


def test_disabled_target_is_refused(client, settings, make_user):
    _, target_id = _admin_and_target(client, settings, make_user)
    _set_status(settings, target_id, "disabled")
    assert _impersonate(client, target_id).status_code == 400


def test_cannot_impersonate_self(client, settings, make_user):
    admin_id, _ = _admin_and_target(client, settings, make_user)
    assert _impersonate(client, admin_id).status_code == 400


def test_cannot_impersonate_another_admin(client, settings, make_user):
    """验收 4 的补口：目标是管理员时 `/api/admin/*` 在冒充期间仍然可达。"""
    _admin_and_target(client, settings, make_user)
    make_user("admin2@tsinghua.edu.cn", is_admin=True)
    assert _impersonate(client, _uid(settings, "admin2@tsinghua.edu.cn")).status_code == 400


def test_an_expired_session_loses_the_impersonation(client, settings, make_user):
    _, target_id = _admin_and_target(client, settings, make_user)
    assert _impersonate(client, target_id).status_code == 200

    from papershelf.server.db import connect

    token = client.cookies.get(SESSION_COOKIE)
    conn = connect(settings)
    try:
        conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE token=?",
                     (token,))
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/auth/me").status_code == 401


def test_logout_ends_the_impersonation(client, settings, make_user):
    a_email, a_pw = make_user("admin@tsinghua.edu.cn", is_admin=True)
    b_email, _ = make_user("reader@tsinghua.edu.cn")
    _login(client, a_email, a_pw)
    assert _impersonate(client, _uid(settings, b_email)).status_code == 200

    client.post("/api/auth/logout")
    me = _login(client, a_email, a_pw)
    assert me["id"] == _uid(settings, a_email)
    assert me["is_admin"] is True


def test_disabling_the_target_mid_flight_falls_back_to_the_admin(client, settings, make_user):
    """目标被停用 ⇒ 旁路自动失效，退回管理员本人（横幅随之消失）。"""
    admin_id, target_id = _admin_and_target(client, settings, make_user)
    assert _impersonate(client, target_id).status_code == 200
    _set_status(settings, target_id, "disabled")

    me = client.get("/api/auth/me").json()
    assert me["id"] == admin_id
    assert me["impersonating"] is None


# ── 迁移 ──────────────────────────────────────────────────────────────────

def test_migration_adds_the_two_columns_to_an_old_sessions_table(settings):
    from papershelf.server.db import connect, init_db

    conn = connect(settings)
    try:
        conn.execute("DROP TABLE sessions")
        conn.execute("""CREATE TABLE sessions (
                          token TEXT PRIMARY KEY,
                          user_id INTEGER NOT NULL,
                          created_at TEXT NOT NULL,
                          expires_at TEXT NOT NULL)""")
        conn.commit()
    finally:
        conn.close()

    init_db(settings)

    conn = connect(settings)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    finally:
        conn.close()
    assert {"act_as_user_id", "act_as_at"} <= cols
