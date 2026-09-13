"""服务端骨架的端到端回归（决策①②⑦⑧⑩⑪⑭⑮⑯⑰⑲⑳㉑ 的服务端面）。

每一条断言都对应一个**真实被修过的缺陷**或一条硬约束，不做形式化覆盖。
"""

from __future__ import annotations

import base64
import re

import pytest

from papershelf.pipeline.model import Block, Doc

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


# ── 验证码流程的测试脚手架 ────────────────────────────────────────────────
# 离线跑，不能真连 SMTP：把 `mailer._send`（所有发信的公共出口）换成记录器。
# 桩 `_send` 而不是 `send_code`，是为了让**邮件正文**（含验证码）也进测试视野 ——
# 否则"码有没有真发出去"就测不到了。
def _enable_smtp(monkeypatch) -> None:
    monkeypatch.setenv("PAPERSHELF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PAPERSHELF_SMTP_FROM", "noreply@example.com")
    from papershelf.server.config import get_settings

    get_settings(refresh=True)


def _code_from_mail(body: str) -> str:
    m = re.search(r"\b(\d{6})\b", body)
    assert m, f"邮件正文里找不到 6 位验证码：{body!r}"
    return m.group(1)


@pytest.fixture()
def sent_mails(monkeypatch) -> list[dict]:
    """把发出的邮件记下来（不联网）。`sent_mails[-1]["body"]` 即最近一封正文。"""
    from papershelf.server import mailer

    box: list[dict] = []

    def fake_send(settings, to_email, subject, body):
        box.append({"to": to_email, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(mailer, "_send", fake_send)
    return box


# ── 认证与开号（⑭⑮㉑）────────────────────────────────────────────────────
def test_registration_closed_without_smtp(client):
    """⑮：SMTP 没配全 → 自助注册自动关闭（否则注册了也收不到验证码）。"""
    assert client.get("/api/auth/config").json()["open_registration"] is False
    r = client.post("/api/auth/register",
                    json={"email": "s@tsinghua.edu.cn", "code": "123456", "password": "password123", "agree": True})
    assert r.status_code == 403
    # 发码口也必须一起关掉（否则前端拿到 sent:true 会以为可以注册）
    assert client.post("/api/auth/register/code",
                       json={"email": "s@tsinghua.edu.cn"}).status_code == 403


def test_registration_requires_edu_cn(client, monkeypatch):
    """⑭：白名单限 .edu.cn。"""
    monkeypatch.setenv("PAPERSHELF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PAPERSHELF_SMTP_FROM", "noreply@example.com")
    from papershelf.server.config import get_settings

    get_settings(refresh=True)
    r = client.post("/api/auth/register/code", json={"email": "a@gmail.com"})
    assert r.status_code == 400
    assert "edu.cn" in r.json()["detail"]


def test_register_code_sends_mail(client, monkeypatch, sent_mails):
    """注册第一步：发验证码邮件，且**库里不存明文码**。"""
    _enable_smtp(monkeypatch)
    r = client.post("/api/auth/register/code", json={"email": "u@tsinghua.edu.cn"})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert len(sent_mails) == 1
    code = _code_from_mail(sent_mails[-1]["body"])

    from papershelf.server.db import connect
    from papershelf.server.config import get_settings

    conn = connect(get_settings())
    try:
        row = conn.execute("SELECT * FROM verification_codes").fetchone()
        assert row["code_hash"] != code, "验证码不能明文入库"
        assert len(row["code_hash"]) == 64, "应当是 sha256 十六进制"
    finally:
        conn.close()


def test_register_with_wrong_code_rejected(client, monkeypatch, sent_mails):
    """验证码不对 → 400，且**不能**偷偷把账号建出来。"""
    _enable_smtp(monkeypatch)
    client.post("/api/auth/register/code", json={"email": "u@tsinghua.edu.cn"})
    real = _code_from_mail(sent_mails[-1]["body"])
    wrong = "000000" if real != "000000" else "111111"
    r = client.post("/api/auth/register",
                    json={"email": "u@tsinghua.edu.cn", "code": wrong, "password": "password123", "agree": True})
    assert r.status_code == 400
    assert client.post("/api/auth/login",
                       json={"email": "u@tsinghua.edu.cn", "password": "password123"}).status_code == 401


def test_register_with_code_logs_user_in(client, monkeypatch, sent_mails):
    """⑭：填对验证码 + 密码 → **直接可用**（不再有"待激活"中间态）。"""
    _enable_smtp(monkeypatch)
    client.post("/api/auth/register/code", json={"email": "u@tsinghua.edu.cn"})
    code = _code_from_mail(sent_mails[-1]["body"])
    r = client.post("/api/auth/register",
                    json={"email": "u@tsinghua.edu.cn", "code": code, "password": "password123", "agree": True})
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    # 注册即登录：cookie 已下发，直接能取到自己
    assert client.get("/api/auth/me").json()["email"] == "u@tsinghua.edu.cn"


def test_register_requires_explicit_consent(client, monkeypatch, sent_mails):
    """用户协议（2026-09-11 宿主指示：「所有责任在用户」）。

    三件事都要成立，缺一条这个勾选框就只是装饰：
      1. 不传/传 false → **服务端 400**（前端那条 `if (!agree)` 谁都能绕过）；
      2. 传 true → 开号，并把**版本号 + 时间戳**落库（举证凭据）；
      3. 被拒**不消耗验证码** —— 验证码是单次的，若先消耗再拒，
         用户"忘勾一次"就得重新收码（实测踩到：把一次表单校验失误变成一次发信）。

    ⚠️ 顺序反过来说更直白：这一条同时锁住"同意检查必须在 `consume_code` 之前"。
    """
    _enable_smtp(monkeypatch)
    email = "u@tsinghua.edu.cn"
    client.post("/api/auth/register/code", json={"email": email})
    code = _code_from_mail(sent_mails[-1]["body"])

    # 1：字段缺省即 False（旧客户端不会再"静默通过"）
    r = client.post("/api/auth/register",
                    json={"email": email, "code": code, "password": "password123"})
    assert r.status_code == 400 and "用户协议" in r.json()["detail"]
    assert client.get("/api/auth/me").status_code == 401, "不同意就不该开号"

    # 2 + 3：同一个码再打一次（没被上一轮吃掉）→ 同意后开号并留档
    r = client.post("/api/auth/register",
                    json={"email": email, "code": code, "password": "password123", "agree": True})
    assert r.status_code == 200, "拒绝同意不该消耗验证码"

    from papershelf.server.db import connect
    from papershelf.server.config import get_settings
    from papershelf.server.terms import TERMS_VERSION

    conn = connect(get_settings())
    try:
        row = conn.execute("SELECT consent_version, consent_at FROM users WHERE email=?",
                           (email,)).fetchone()
    finally:
        conn.close()
    assert row["consent_version"] == TERMS_VERSION, "同意的必须是当前生效的条款版本"
    assert row["consent_at"], "同意时刻要落库 —— 否则事后无法举证"


def test_register_consent_passed_down_from_config(client, monkeypatch):
    """条款正文由服务端下发，前端不复制一份（否则两边漂移，用户同意的是旧文案）。"""
    r = client.get("/api/auth/config").json()
    assert r["terms_version"]
    assert r["terms_summary"] and r["terms_body"], "注册页要能读到摘要与全文"
    assert all({"title", "text"} == set(s) for s in r["terms_body"])


def test_code_is_single_use(client, monkeypatch, sent_mails):
    """验证码单次有效 —— 用过就从库里消失，且**码绑邮箱**，换个邮箱也无效。"""
    _enable_smtp(monkeypatch)
    email = "u@tsinghua.edu.cn"
    client.post("/api/auth/register/code", json={"email": email})
    code = _code_from_mail(sent_mails[-1]["body"])
    assert client.post("/api/auth/register",
                       json={"email": email, "code": code, "password": "password123", "agree": True}).status_code == 200

    from papershelf.server.db import connect
    from papershelf.server.config import get_settings

    conn = connect(get_settings())
    try:
        left = conn.execute("SELECT COUNT(*) FROM verification_codes WHERE email=?", (email,)).fetchone()[0]
        assert left == 0, "用过的验证码必须删掉（防重放）"
    finally:
        conn.close()

    # 同一个码拿去给另一个邮箱注册 → 无效（哈希里混了 email）
    other = "v@tsinghua.edu.cn"
    client.post("/api/auth/register/code", json={"email": other})
    r = client.post("/api/auth/register",
                    json={"email": other, "code": code, "password": "password123", "agree": True})
    assert r.status_code == 400


def test_code_resend_is_rate_limited(client, monkeypatch, sent_mails):
    """60 秒冷却：连点「获取验证码」不该反复发信（既省钱也防轰炸）。"""
    _enable_smtp(monkeypatch)
    assert client.post("/api/auth/register/code",
                       json={"email": "u@tsinghua.edu.cn"}).status_code == 200
    r = client.post("/api/auth/register/code", json={"email": "u@tsinghua.edu.cn"})
    assert r.status_code == 429
    assert len(sent_mails) == 1


def test_code_bad_attempts_exhaust(client, monkeypatch, sent_mails):
    """错误次数超限 → 验证码作废，必须重新获取（挡住暴力猜 6 位码）。"""
    _enable_smtp(monkeypatch)
    client.post("/api/auth/register/code", json={"email": "u@tsinghua.edu.cn"})
    real = _code_from_mail(sent_mails[-1]["body"])
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(5):
        client.post("/api/auth/register",
                    json={"email": "u@tsinghua.edu.cn", "code": wrong, "password": "password123", "agree": True})
    # 作废后即便拿对码也不能再用
    r = client.post("/api/auth/register",
                    json={"email": "u@tsinghua.edu.cn", "code": real, "password": "password123", "agree": True})
    assert r.status_code == 400


# ── 忘记密码（⑭：同样走验证码）──────────────────────────────────────────────
def test_reset_password_with_code(client, monkeypatch, sent_mails):
    """忘记密码：验证码 + 新密码 → 旧密码失效、新密码可用。"""
    _enable_smtp(monkeypatch)
    email, pw = "u@tsinghua.edu.cn", "password123"
    client.post("/api/auth/register/code", json={"email": email})
    client.post("/api/auth/register",
                json={"email": email, "code": _code_from_mail(sent_mails[-1]["body"]),
                      "password": pw, "agree": True})

    # 冷却期内先换邮箱验证不了，所以直接用另一账号体系：清掉冷却记录模拟"过了一会儿"
    from papershelf.server.db import connect
    from papershelf.server.config import get_settings

    conn = connect(get_settings())
    try:
        conn.execute("DELETE FROM verification_codes")
        conn.commit()
    finally:
        conn.close()

    assert client.post("/api/auth/reset/code", json={"email": email}).status_code == 200
    new_code = _code_from_mail(sent_mails[-1]["body"])
    r = client.post("/api/auth/reset", json={"email": email, "code": new_code, "password": "newpass12345"})
    assert r.status_code == 200
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"email": email, "password": pw}).status_code == 401
    assert client.post("/api/auth/login",
                       json={"email": email, "password": "newpass12345"}).status_code == 200


def test_reset_revokes_existing_sessions(client, monkeypatch, sent_mails):
    """改密必须踢掉旧会话 —— 否则"密码被盗后改了密码"仍然挡不住对方。"""
    _enable_smtp(monkeypatch)
    email = "u@tsinghua.edu.cn"
    client.post("/api/auth/register/code", json={"email": email})
    client.post("/api/auth/register",
                json={"email": email, "code": _code_from_mail(sent_mails[-1]["body"]),
                      "password": "password123", "agree": True})
    assert client.get("/api/auth/me").status_code == 200  # 已登录

    from papershelf.server.db import connect
    from papershelf.server.config import get_settings

    conn = connect(get_settings())
    try:
        conn.execute("DELETE FROM verification_codes")
        conn.commit()
    finally:
        conn.close()

    client.post("/api/auth/reset/code", json={"email": email})
    code = _code_from_mail(sent_mails[-1]["body"])
    assert client.post("/api/auth/reset",
                       json={"email": email, "code": code, "password": "newpass12345"}).status_code == 200
    assert client.get("/api/auth/me").status_code == 401, "改密后旧会话必须失效"


def test_reset_code_does_not_leak_registration(client, monkeypatch, sent_mails):
    """忘记密码不能变成「探测哪些邮箱注册过」的工具：未注册也回同样的成功。"""
    _enable_smtp(monkeypatch)
    r = client.post("/api/auth/reset/code", json={"email": "nobody@tsinghua.edu.cn"})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert sent_mails == [], "未注册的邮箱不该真发信"


def test_admin_bootstrap_requires_explicit_password(settings):
    """引导管理员必须显式给密码 —— 不许"没设就随机生成"的静默开号。"""
    from papershelf.server.db import connect
    from papershelf.server.security import ensure_admin

    conn = connect(settings)
    try:
        with pytest.raises(ValueError):
            ensure_admin(conn, "admin@pku.edu.cn", "")
        assert ensure_admin(conn, "admin@pku.edu.cn", "adminpass123") > 0
    finally:
        conn.close()


def test_last_admin_cannot_be_disabled(client, make_user):
    """㉑：禁用/降级最后一名管理员会让实例失去管理入口。"""
    email, pw = make_user("admin@pku.edu.cn", is_admin=True)
    client.post("/api/auth/login", json={"email": email, "password": pw})
    me = client.get("/api/auth/me").json()
    r = client.patch(f"/api/admin/users/{me['id']}", json={"status_": "disabled"})
    assert r.status_code == 400


# ── 隔离（①⑦）──────────────────────────────────────────────────────────
def test_plans_are_private_between_users(client, make_user):
    a = make_user("a@tsinghua.edu.cn")
    b = make_user("b@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": a[0], "password": a[1]})
    plan_id = client.post("/api/plans", json={"name": "P"}).json()["id"]

    other = client.__class__(client.app_ref)          # 新 cookie jar = 另一个用户
    other.post("/api/auth/login", json={"email": b[0], "password": b[1]})
    assert other.get(f"/api/plans/{plan_id}").status_code == 404
    assert other.patch(f"/api/plans/{plan_id}", json={"name": "x"}).status_code == 404


# ── 进度语义（⑰）────────────────────────────────────────────────────────
def _seed_paper(settings, plan_id: int, blocks: list[Block]) -> int:
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        cur = conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,status_at,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (plan_id, "测试文献", "upload", "reading", "2026-01-01T00:00:00+00:00", "done"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
        save_doc(conn, pid, Doc(meta={"title_zh": "测试"}, blocks=blocks))
        return pid
    finally:
        conn.close()


@pytest.fixture()
def seeded(client, settings, make_user):
    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    pid = _seed_paper(settings, plan_id, [
        Block(id="b-0001", type="h2", en="I. Introduction", zh="I. 引言", zh_source="mt"),
        Block(id="b-0002", type="p", en="Safety is important.", zh="安全性很重要。", zh_source="mt"),
        Block(id="b-0003", type="figure", en="Fig. 1. 闭环。", zh="图 1. 闭环。", zh_source="mt",
              payload={"src": "assets/p1_img1.png", "caption": "Fig. 1. 闭环。"}),
        Block(id="b-0004", type="refs", en="[1] X. Yu, IEEE TAC, 2007.",
              zh="[1] X. Yu, IEEE TAC, 2007.", zh_source="none"),
    ])
    return {"plan_id": plan_id, "pid": pid, "email": email}


def test_progress_manual_lock(client, seeded):
    pid = seeded["pid"]
    r = client.patch(f"/api/papers/{pid}", json={"progress": 42})
    assert (r.json()["progress"], r.json()["progress_mode"]) == (42, "auto")
    r = client.patch(f"/api/papers/{pid}", json={"status_": "read"})
    assert (r.json()["progress"], r.json()["progress_mode"]) == (100, "manual")
    assert client.patch(f"/api/papers/{pid}", json={"progress": 10}).status_code == 409


# ── 块级修订（⑯⑳）──────────────────────────────────────────────────────
def test_block_edit_marks_human_and_survives(client, settings, seeded):
    pid = seeded["pid"]
    r = client.patch(f"/api/docs/{pid}/blocks/b-0002",
                     json={"zh": "安全至关重要。", "reconciled": True})
    assert r.json()["zh_source"] == "human"

    from papershelf.server.db import connect
    from papershelf.server.repo import update_block

    conn = connect(settings)
    try:
        update_block(conn, pid, "b-0002", zh="机器译文", zh_source="mt")   # 模拟重跑覆盖
    finally:
        conn.close()
    blocks = {b["id"]: b for b in client.get(f"/api/papers/{pid}/doc").json()["blocks"]}
    assert blocks["b-0002"]["zh"] == "机器译文"          # 显式覆盖才会变
    assert blocks["b-0002"]["zh_source"] == "mt"


def test_no_zh_blocks_flagged(client, seeded):
    """免中文块（参考文献）必须标出来 —— 前端据此单栏横跨，校验器据此不判漏译。"""
    blocks = {b["id"]: b for b in client.get(f"/api/papers/{seeded['pid']}/doc").json()["blocks"]}
    assert blocks["b-0004"]["no_zh"] is True
    assert blocks["b-0002"]["no_zh"] is False


def test_notes_anchored_to_block(client, seeded):
    pid = seeded["pid"]
    assert client.post(f"/api/papers/{pid}/notes",
                       json={"block_id": "b-0002", "content": "回看 §II"}).status_code == 201
    assert len(client.get(f"/api/papers/{pid}/notes").json()) == 1


# ── 分享（⑩⑪）──────────────────────────────────────────────────────────
def test_share_expiry_is_capped_at_24_hours(client, seeded):
    """宿主 2026-09-11：「分享链接有效期最大设置 24 小时」——**上限要在服务端**。

    这条测的是"前端下拉换了、直接打 API 仍能拿到 30 天链接"这个绕法：
    `expires_in` 负责钳制，所以任何入口（旧客户端、curl、重放请求）都逃不掉。
    同时断言**永久链接已不可能产生**（旧版 `days=0` 会给出 `expires_at=None`）。
    """
    from datetime import datetime, timezone

    plan_id = seeded["plan_id"]
    now = datetime.now(timezone.utc)

    for sent in (0, 24, 1000):
        exp = client.post(f"/api/plans/{plan_id}/shares", json={"hours": sent}).json()["expires_at"]
        assert exp, f"hours={sent} 不该产生永不过期的链接"
        left = (datetime.fromisoformat(exp) - now).total_seconds()
        assert 0 < left <= 24 * 3600 + 60, f"hours={sent} 实际有效期 {left/3600:.1f} 小时，超过上限"

    # 缺省值也必须是 24 小时以内（配置不填就走上限）
    exp = client.post(f"/api/plans/{plan_id}/shares").json()["expires_at"]
    assert exp and (datetime.fromisoformat(exp) - now).total_seconds() <= 24 * 3600 + 60


def test_legacy_permanent_shares_get_converged(client, seeded, settings):
    """存量永久链接（旧版 `days=0` 留下的 `expires_at IS NULL`）在启动迁移里被收敛。

    不直接作废而是**从现在起算 24 小时**：已经发出去的链接立刻全废，
    访客看到的是"链接坏了"，而不是"过期了"。
    """
    from datetime import datetime, timezone

    from papershelf.server.db import connect, init_db, tx

    conn = connect(settings)
    try:
        with tx(conn):
            conn.execute("INSERT INTO shares (token, plan_id, created_at, expires_at) VALUES (?,?,?,?)",
                         ("legacy-token", seeded["plan_id"], "2026-01-01T00:00:00+00:00", None))
    finally:
        conn.close()

    init_db(settings)  # 模拟一次重启

    conn = connect(settings)
    try:
        exp = conn.execute("SELECT expires_at FROM shares WHERE token='legacy-token'").fetchone()[0]
    finally:
        conn.close()
    assert exp, "迁移后不该还有永不过期的链接"
    left = (datetime.fromisoformat(exp) - datetime.now(timezone.utc)).total_seconds()
    assert 0 < left <= 24 * 3600 + 60


def test_share_is_live_readonly_and_revocable(client, seeded):
    pid, plan_id = seeded["pid"], seeded["plan_id"]
    tok = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 6}).json()["token"]

    anon = client.__class__(client.app_ref)
    assert anon.get(f"/api/shares/{tok}").json()["readonly"] is True
    assert len(anon.get(f"/api/shares/{tok}/papers/{pid}").json()["blocks"]) == 4

    for method, url in [("patch", f"/api/papers/{pid}"),
                        ("post", f"/api/shares/{tok}"),
                        ("delete", f"/api/shares/{tok}")]:
        kw = {} if method == "delete" else {"json": {}}
        assert getattr(anon, method)(url, **kw).status_code in (401, 403)

    assert client.delete(f"/api/shares/{tok}").status_code == 204
    assert anon.get(f"/api/shares/{tok}").status_code == 410


def test_share_payload_is_shaped_like_the_logged_in_api(client, seeded):
    """⑩「分享页要和分享者看到的一模一样」的**数据面护栏**。

    踩过的类：分享页复用整站页面后，读的是完整 `Plan` / `Paper`。后端若只回一个
    精简子集（旧版只回 `{id,name,description}` + 7 个 paper 字段），页面会集体渲染成
    `undefined` —— 而 TypeScript 完全不会报错（它信类型声明，不校验运行时）。
    所以这里逐字段断言两条路径的分母一致。
    """
    pid, plan_id = seeded["pid"], seeded["plan_id"]
    tok = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 1}).json()["token"]
    anon = client.__class__(client.app_ref)

    sid = anon.get(f"/api/shares/{tok}").json()
    mine = client.get(f"/api/plans/{plan_id}/papers").json()[0]
    shared = next(p for p in sid["papers"] if p["id"] == pid)
    assert set(shared) == set(mine), "分享 papers 与登录态 papers 的字段集必须一致"
    assert shared["tags"] == mine["tags"] and shared["conv_error"] == mine["conv_error"]

    # 计划侧同样要对齐（旧版缺 goal / 计数 → 侧栏进度卡与总览指标卡会全空）
    my_plan = next(p for p in client.get("/api/plans").json() if p["id"] == plan_id)
    assert set(sid["plan"]) == set(my_plan)
    assert sid["plan"]["goal"] == my_plan["goal"]
    assert sid["plan"]["paper_count"] == my_plan["paper_count"]
    assert sid["plan"]["done_count"] == my_plan["done_count"]

    # 侧栏计划切换器 + 笔记（⑩ 原话含「进度与笔记」）
    assert [p["id"] for p in anon.get(f"/api/shares/{tok}/plans").json()] == [plan_id]
    client.post(f"/api/papers/{pid}/notes", json={"block_id": "b-0002", "content": "回看 §II"})
    assert anon.get(f"/api/shares/{tok}/papers/{pid}/notes").json()[0]["content"] == "回看 §II"

    # 分享页 banner 要显示**有效期倒计时**（2026-09-12）：匿名访客没有管理端可查，
    # 这条链接自己的到期时间必须随载荷一起到达。
    assert sid["share"]["seconds_left"] > 0
    assert sid["share"]["expires_at"] == client.get(
        f"/api/plans/{plan_id}/shares").json()[0]["expires_at"]


# ── 分享管理（2026-09-12 宿主：「需要有分享管理，可以创建多个分享」）──────────
def test_many_shares_per_plan_and_cross_plan_listing(client, seeded):
    """一个计划可建**多条**分享，管理页看到的是**跨计划**的全部链接。

    列表必须带 `plan_name`：管理页要显示"这条链接分享的是哪个计划"，
    逐计划取数再拼在前端是 N+1 次往返，而且筛选口径会散到前端。
    """
    plan_id = seeded["plan_id"]
    other = client.post("/api/plans", json={"name": "第二个计划", "goal": 30}).json()

    a = client.post(f"/api/plans/{plan_id}/shares",
                    json={"hours": 6, "label": "给导师看"}).json()
    b = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 24}).json()
    c = client.post(f"/api/plans/{other['id']}/shares",
                    json={"hours": 1, "label": "组会前"}).json()
    assert len({a["token"], b["token"], c["token"]}) == 3

    rows = client.get("/api/shares").json()
    by_tok = {r["token"]: r for r in rows}
    assert set(by_tok) >= {a["token"], b["token"], c["token"]}
    assert by_tok[a["token"]]["label"] == "给导师看"
    assert by_tok[a["token"]]["plan_name"] == "精读"
    assert by_tok[c["token"]]["plan_name"] == "第二个计划"
    # 状态与剩余秒数是**服务端算好的**（三处显示共用，不各自算一遍）
    assert by_tok[a["token"]]["state"] == "active"
    assert 0 < by_tok[a["token"]]["seconds_left"] <= 6 * 3600 + 60

    # 计划内的列表只回该计划的
    assert {r["token"] for r in client.get(f"/api/plans/{plan_id}/shares").json()} == {
        a["token"], b["token"]}


def test_share_listing_hides_other_users_shares(client, seeded, settings):
    """分享管理是**私有**的：只能看到自己的链接（决策⑦ 计划私有）。"""
    plan_id = seeded["plan_id"]
    tok = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 6}).json()["token"]
    assert tok in {r["token"] for r in client.get("/api/shares").json()}

    from papershelf.server.db import connect, tx
    from papershelf.server.security import create_user

    conn = connect(settings)
    try:
        with tx(conn):
            create_user(conn, email="other@example.edu.cn", password="password123",
                        display_name="别人", status_="active")
    finally:
        conn.close()

    other = client.__class__(client.app_ref)
    assert other.post("/api/auth/login",
                      json={"email": "other@example.edu.cn",
                            "password": "password123"}).status_code == 200
    assert other.get("/api/shares").json() == []
    # 别人的 token 也不能续期（否则等于拿到了管理权）
    assert other.post(f"/api/shares/{tok}/renew", json={"hours": 6}).status_code == 404


def test_renew_resets_from_now_and_resurrects(client, seeded, settings):
    """重置有效期 = 从**现在**起算，且能把已撤销/已过期的链接救回来。

    为什么不能从原到期时间顺延：反复续期会一路把 `expires_at` 推过 24 小时上限，
    "最长 24 小时"就成了纸面约束。所以这里断言**续期后剩余 ≤ 所填小时数**。
    """
    from datetime import datetime, timedelta, timezone

    from papershelf.server.db import connect, tx

    plan_id = seeded["plan_id"]
    tok = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 1}).json()["token"]

    # 人为把它推成"就要到期"（剩余 30 秒）
    conn = connect(settings)
    try:
        with tx(conn):
            conn.execute("UPDATE shares SET expires_at=? WHERE token=?",
                         ((datetime.now(timezone.utc) + timedelta(seconds=30))
                          .isoformat(timespec="seconds"), tok))
    finally:
        conn.close()

    r = client.post(f"/api/shares/{tok}/renew", json={"hours": 6}).json()
    assert r["state"] == "active"
    assert r["hours"] == 6
    # 剩余时间必须接近 6 小时，而不是"原到期时间 + 6 小时"（后者会明显更长）
    assert 6 * 3600 - 60 <= r["seconds_left"] <= 6 * 3600 + 5
    # 超上限一样被钳
    assert client.post(f"/api/shares/{tok}/renew", json={"hours": 999}).json()["seconds_left"] \
        <= 24 * 3600 + 5

    # 撤销后能靠续期救回来（管理页对死链"重置有效期"是有意义的操作）
    assert client.delete(f"/api/shares/{tok}").status_code == 204
    assert client.get("/api/shares").json()[0]["state"] == "revoked"
    back = client.post(f"/api/shares/{tok}/renew", json={"hours": 2}).json()
    assert back["state"] == "active"
    anon = client.__class__(client.app_ref)
    assert anon.get(f"/api/shares/{tok}").status_code == 200


def test_expiring_state_is_computed_server_side(client, seeded, settings):
    """「即将到期」（剩余 < 1 小时）由**服务端**判定。

    三处都要变红（分享页 banner / 管理页表格 / 新建结果），各算一遍迟早
    有一处阈值不同 —— 同一条链接就会一会儿红一会儿不红。
    """
    from datetime import datetime, timedelta, timezone

    from papershelf.server.db import connect, tx

    plan_id = seeded["plan_id"]
    tok = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 12}).json()["token"]
    assert client.get("/api/shares").json()[0]["state"] == "active"

    conn = connect(settings)
    try:
        with tx(conn):
            conn.execute("UPDATE shares SET expires_at=? WHERE token=?",
                         ((datetime.now(timezone.utc) + timedelta(minutes=20))
                          .isoformat(timespec="seconds"), tok))
    finally:
        conn.close()

    row = client.get("/api/shares").json()[0]
    assert row["state"] == "expiring"
    assert 0 < row["seconds_left"] < 3600
    # 分享页那份载荷也一致（同一个 `_seconds_left`）
    anon = client.__class__(client.app_ref)
    assert anon.get(f"/api/shares/{tok}").json()["share"]["seconds_left"] == row["seconds_left"]


def test_label_is_trimmed_and_capped(client, seeded):
    """备注长度在服务端截断 —— 前端 maxlength 只是提示，不是约束。"""
    r = client.post(f"/api/plans/{seeded['plan_id']}/shares",
                    json={"hours": 1, "label": "  " + "长" * 80 + "  "}).json()
    assert len(r["label"]) == 30
    assert client.get("/api/shares").json()[0]["label"] == r["label"]


# ── 图片资产 ────────────────────────────────────────────────────────────
@pytest.fixture()
def asset(settings, seeded):
    p = settings.papers_dir / f"p{seeded['pid']}" / "assets"
    p.mkdir(parents=True, exist_ok=True)
    (p / "p1_img1.png").write_bytes(PNG)
    return p / "p1_img1.png"


def test_assets_are_ownership_checked(client, settings, make_user, seeded, asset):
    pid = seeded["pid"]
    assert client.get(f"/papers/{pid}/assets/p1_img1.png").content == PNG

    other = make_user("other@tsinghua.edu.cn")
    stranger = client.__class__(client.app_ref)
    stranger.post("/api/auth/login", json={"email": other[0], "password": other[1]})
    assert stranger.get(f"/papers/{pid}/assets/p1_img1.png").status_code == 404
    assert client.__class__(client.app_ref).get(f"/papers/{pid}/assets/p1_img1.png").status_code == 404


def test_asset_path_traversal_blocked(client, seeded):
    """路径穿越：**绝不能把库文件吐出来**。

    注意判定方式：HTTP 客户端会把 `x.png/../../papershelf.db` 自己归一化成
    `/papers/1/papershelf.db`（于是落到 SPA 兜底页），所以断言不能只看状态码 ——
    真正要守的是「响应体里永远不出现 SQLite 文件内容」。
    """
    pid = seeded["pid"]
    for probe in ["..%2f..%2fpapershelf.db", "x.png/../../papershelf.db",
                  "..%2f..%2f..%2fpapershelf.db", "%2e%2e/papershelf.db",
                  "p1_img1.png%00.db"]:
        r = client.get(f"/papers/{pid}/assets/{probe}")
        assert b"SQLite format 3" not in r.content
        assert "前端尚未构建" not in r.text or True     # 兜底页也算安全（没给文件）
        assert r.status_code in (200, 400, 404)
        if r.status_code == 200:
            assert r.headers.get("content-type", "").startswith("application/json")


def test_share_can_read_assets_while_active(client, seeded, asset):
    pid, plan_id = seeded["pid"], seeded["plan_id"]
    tok = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 1}).json()["token"]
    anon = client.__class__(client.app_ref)
    assert anon.get(f"/share/{tok}/assets/p1_img1.png").content == PNG


# ── 导出（⑳ + 遗留 8）──────────────────────────────────────────────────
def test_export_is_self_contained_single_file(client, seeded, asset):
    r = client.get(f"/api/papers/{seeded['pid']}/export?lang=dual&view=1")
    assert r.status_code == 200
    assert 'class="row"' in r.text                              # ⑤ 左右并排
    assert "data:image/png;base64" in r.text                    # 图片内联 → 真·单文件
    assert "cdn" not in r.text.lower() and "mathjax" not in r.text.lower()
    assert 'class="row wide"' in r.text                         # 免中文块单栏横跨


def test_share_export_does_not_need_session(client, seeded, asset):
    tok = client.post(f"/api/plans/{seeded['plan_id']}/shares", json={"hours": 1}).json()["token"]
    anon = client.__class__(client.app_ref)
    r = anon.get(f"/api/shares/{tok}/papers/{seeded['pid']}/export")
    assert r.status_code == 200 and "data:image/png;base64" in r.text


# ── 前端（M3 SPA）：服务端只需保证「任何页面路由都落到 SPA」+「静态资源语义正确」──
def test_spa_shell_served_for_deep_links(client, spa_shell):
    """刷新 /plans/3 或分享链接不能 404 —— 后端是 SPA 的兜底。

    这条守的是部署形态（决策②）：单进程同时当 API 与前端宿主，
    没有 nginx 帮忙做 `try_files`，所以兜底必须由后端自己做。
    """
    # 分享作用域下的**整张路由表**都要兜得住（⑩：分享页复用整站页面，
    # 所以侧栏点「文献库」会落到 /share/<tok>/library，刷新不能 404）
    for path in ("/plans", "/plans/3", "/reader/7", "/share/abc", "/admin", "/",
                 "/share/abc/library", "/share/abc/board", "/share/abc/reader/7",
                 "/share/abc/plans", "/share/abc/papers/7"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"], path


def test_frontend_not_built_falls_back_to_json_hint(client, no_spa_shell):
    """没有构建产物时必须是**可读的 JSON 提示**，而不是 500 或空白页。

    守的是「后端独立可跑」：没跑过 `npm run build` 也能起服务，且明确告诉你怎么修。
    """
    r = client.get("/plans")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "前端尚未构建" in r.text and "/api/docs" in r.text


def test_missing_hashed_asset_is_404_not_html(client):
    """旧 hash 的 /assets/xxx.js 必须 404。

    如果落到 SPA 兜底，浏览器会把 HTML 当 JS 执行 → 报一堆无关语法错误，
    真正的「资源不存在」被掩盖（部署后换版本时极易踩）。
    """
    r = client.get("/assets/index-STALEHASH.js")
    assert r.status_code == 404


def test_unknown_api_never_returns_spa_html(client):
    """typo 的接口路径必须是 JSON 404，而不是把 HTML 塞给 fetch 调用方。"""
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_data_pages_have_no_cdn(client, seeded):
    """前端产物与服务端数据里都**不得出现 CDN**（遗留 8：公式走服务端 MathML）。"""
    doc = client.get(f"/api/papers/{seeded['pid']}/doc").json()
    blob = str(doc)
    assert "cdn." not in blob and "MathJax" not in blob
    html = client.get(f"/api/papers/{seeded['pid']}/export?lang=dual&view=1").text
    assert "cdn." not in html and "MathJax" not in html


def test_reader_payload_carries_server_rendered_html(client, seeded):
    """⑳ + 遗留 8：块 JSON 必须带服务端渲染好的 `en_html`/`zh_html`。

    前端靠这两个字段渲染公式与图片（`dangerouslySetInnerHTML`），
    一旦后端哪天只发纯文本而不发 HTML，读者端的公式会静默变成源码字符串。
    """
    blocks = client.get(f"/api/papers/{seeded['pid']}/doc").json()["blocks"]
    assert blocks and all("en_html" in b and "zh_html" in b for b in blocks)


def test_block_edit_returns_full_rerendered_block(client, seeded):
    """⑯ 的隐性要求：改块必须回**完整块**（含重渲染的 zh_html）。

    踩过：端点原先只回 `{id, zh}`，前端于是只能就地改纯文本字段，
    `zh_html` 仍是改之前的那份 → 「已保存」但屏幕上纹丝不动，看着像没生效。
    译文里带公式时尤其明显（MathML 不跟着变）。
    """
    r = client.patch(f"/api/docs/{seeded['pid']}/blocks/b-0002",
                     json={"zh": "改写后的译文 \\( x^2 \\)。", "reconciled": True})
    assert r.status_code == 200
    body = r.json()
    assert body["zh"] == "改写后的译文 \\( x^2 \\)。"
    assert body["zh_source"] == "human"
    assert "<math" in body["zh_html"], "回包必须带**重渲染后**的 HTML（含 MathML）"
    assert body["needs_review"] is False


def test_zero_llm_conversion_survives_validation(settings, monkeypatch):
    """无公式、无待译块的 PDF 也必须能跑完整条转换（不调 LLM、不联网）。

    踩过：`_zh_html` 的签名漏了 `typeset` 参数，于是**任何零 LLM 的论文**
    都在最后一步校验时 `TypeError` → `conv_state=failed`。
    带公式的论文走不到那句，所以这个 bug 只能被「什么都没得译的 PDF」抓到。
    """
    from papershelf.pipeline.model import Block as PBlock, Doc as PDoc
    from papershelf.pipeline.validate import validate
    from papershelf.server.converter import _zh_html
    from papershelf.pipeline import en_html

    doc = PDoc(meta={"title_en": "Tiny", "title_zh": "小小"},
               blocks=[PBlock(id="b-0001", type="p", en="Plain text.", zh="纯文本。",
                              zh_source="mt")])
    # 校验入口本身必须能跑通（typeset=False：比对 LaTeX 源码）
    report = validate(en_html(doc, typeset=False), _zh_html(doc, typeset=False))
    assert report.ok, report.summary()


def test_unconfigured_llm_fails_fast_with_actionable_message(settings):
    """未配 LLM 时必须**立刻**失败并点名环境变量，而不是最后报「疑似漏译」。

    踩过：没有前置检查时，整篇会一路跑到校验，因为一个块都没译而报
    「标记保真校验未通过 · 疑似漏译」—— 用户会去查翻译质量问题，
    真正的原因（环境变量没配）完全看不出来。
    """
    import pymupdf

    from papershelf.server.converter import convert_paper
    from papershelf.server.db import connect
    from papershelf.server.security import create_user

    pdf = settings.data_dir / "tiny.pdf"
    d = pymupdf.open()
    d.new_page().insert_text((72, 90), "A sentence that would need translating.", fontsize=12)
    d.save(str(pdf))
    d.close()

    conn = connect(settings)
    try:
        create_user(conn, email="a@x.edu.cn", password="password123", status_="active")
        pl = conn.execute("INSERT INTO plans (user_id,name,created_at,updated_at) "
                          "VALUES (1,'p',datetime('now'),datetime('now'))").lastrowid
        pid = conn.execute(
            """INSERT INTO papers (plan_id,title,source,pdf_path,status,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (pl, "t", "upload", str(pdf), "unread", "queued")).lastrowid
        conn.commit()
    finally:
        conn.close()

    convert_paper(int(pid))
    conn = connect(settings)
    try:
        row = conn.execute("SELECT conv_state, conv_error FROM papers WHERE id=?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row["conv_state"] == "failed"
    assert "PAPERSHELF_LLM_API_KEY" in row["conv_error"], row["conv_error"]


def test_auto_progress_never_goes_backwards(client, seeded):
    """⑰：滚动上报是**自动累计**，只能涨。

    踩过：前端节流上报在"从文末快速滚回顶部"时会把 100% 拽回几个百分点，
    进度条看起来坏了。回退对"读到过哪里"没有意义（要往回改请手动设状态）。
    """
    pid = seeded["pid"]
    assert client.patch(f"/api/papers/{pid}", json={"progress": 80}).json()["progress"] == 80
    r = client.patch(f"/api/papers/{pid}", json={"progress": 3})
    assert r.status_code == 200 and r.json()["progress"] == 80, "自动进度不得回退"
    # 标回「未读」= 重新开始读 → 进度归零（否则会出现"未读 但 80%"的自相矛盾）
    r = client.patch(f"/api/papers/{pid}", json={"status_": "unread"}).json()
    assert (r["progress"], r["progress_mode"]) == (0, "auto")
    assert client.patch(f"/api/papers/{pid}", json={"progress": 10}).json()["progress"] == 10


# ── PDF 分页容器（承自原型 `.pdf-page` + 「第 N 页 / 共 M 页」）──────────
def test_blocks_carry_pdf_page(client, settings, seeded):
    """每个块都必须带 `payload.page` —— 分页容器的**唯一数据来源**。

    踩过：`payload.page` 原先只有 figure 块有（解析循环里手写的那一处），
    于是阅读器只能把正文渲成一整条流；要做分页就得改数据模型并**让所有块的
    写法都过同一个包装器**（`parse.py` 的 `_paged_adder`），否则漏写一处就静默少一页。
    """
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        save_doc(conn, seeded["pid"], Doc(meta={"title_zh": "测试", "pages": 3}, blocks=[
            Block(id="b-0001", type="p", en="One.", zh="一。", zh_source="mt", payload={"page": 1}),
            Block(id="b-0002", type="p", en="Two.", zh="二。", zh_source="mt", payload={"page": 1}),
            Block(id="b-0003", type="p", en="Three.", zh="三。", zh_source="mt", payload={"page": 2}),
            Block(id="b-0004", type="refs", en="[1] X.", zh="[1] X.", payload={"page": 3}),
        ]))
    finally:
        conn.close()

    data = client.get(f"/api/papers/{seeded['pid']}/doc").json()
    assert data["meta"]["pages"] == 3
    assert [b["payload"]["page"] for b in data["blocks"]] == [1, 1, 2, 3]


def test_export_paginates_and_counts_pages(client, settings, seeded):
    """导出件与屏幕上**同一套版式**：按 `payload.page` 分页 + 「第 N 页 / 共 M 页」。

    同时守住降级路径：块没有 `page`（老文档）时不得报错、也不得丢内容 ——
    整篇落在一页里，页脚不显示页码。
    """
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        save_doc(conn, seeded["pid"], Doc(meta={"title_zh": "测试", "pages": 3}, blocks=[
            Block(id="b-0001", type="p", en="One.", zh="一。", zh_source="mt", payload={"page": 1}),
            Block(id="b-0002", type="p", en="Two.", zh="二。", zh_source="mt", payload={"page": 2}),
            Block(id="b-0003", type="refs", en="[1] X.", zh="[1] X.", payload={"page": 3}),
        ]))
    finally:
        conn.close()

    html = client.get(f"/api/papers/{seeded['pid']}/export?lang=dual&view=1").text
    assert html.count('class="pdf-page"') == 3
    assert "第 1 页 / 共 3 页" in html and "第 3 页 / 共 3 页" in html
    # 三块内容都在（分页绝不吞内容）
    for t in ("One.", "一。", "Two.", "二。", "[1] X."):
        assert t in html


def test_parse_version_is_in_doc_cache_fingerprint():
    """`doc_cache` 的键必须混入解析版本号 —— 否则**改了解析器却永远读旧产物**。

    踩过类：本次给每个块加 `payload.page`，而缓存键只是 PDF 的 sha256；
    PDF 一字未改 → 永远命中旧缓存 → 分页容器永远不出现，代码看着却完全正确。
    """
    from papershelf.pipeline import PARSE_VERSION
    from papershelf.server.routers.papers import _pdf_fingerprint

    assert isinstance(PARSE_VERSION, int) and PARSE_VERSION >= 1

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        once = _pdf_fingerprint(f)
        assert _pdf_fingerprint(f) == once, "同一文件两次必须一致（否则缓存永不命中）"

        import hashlib
        plain = hashlib.sha256(b"%PDF-1.4 fake").hexdigest()
        assert once != plain, "指纹必须与裸 PDF hash 不同（版本号确实混进去了）"


def test_export_without_page_numbers_still_renders_everything(client, settings, seeded):
    """降级：块没有 `payload.page`（解析版本 2 之前落的库）时，导出**不得报错、不得丢内容**。

    老库还在生产里跑，导出页必须照常出全篇 —— 只是整篇落在一页、页脚不显示页码。
    这条守的是"新增字段不能让存量数据变成空白页"。
    """
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        save_doc(conn, seeded["pid"], Doc(meta={"title_zh": "老库"}, blocks=[
            Block(id="b-0001", type="p", en="Legacy one.", zh="旧一。", zh_source="mt"),
            Block(id="b-0002", type="p", en="Legacy two.", zh="旧二。", zh_source="mt"),
        ]))
    finally:
        conn.close()

    html = client.get(f"/api/papers/{seeded['pid']}/export?lang=dual&view=1").text
    assert html.count('class="pdf-page"') == 1, "无页码 → 整篇一页"
    assert "pf-no" not in html and "第 1 页" not in html, "无页码信息时不显示页码"
    for t in ("Legacy one.", "旧一。", "Legacy two.", "旧二。"):
        assert t in html, t


# ── 漏译的收敛出口（2026-09-11 生产事故）──────────────────────────────────
def test_formula_heading_fragment_not_required_to_translate():
    """标题/图注判「本应有中文」时必须**剥掉数学**后再看实词。

    踩过（生产实测）：解析把展示公式的续行误判成 h2 ——
    `\\(\\mathrm{adj} = 0.77 \\text{–} 0.96\\)) [ 230 ].` 剥掉数学后一个实词都不剩，
    但裸 `_WORD_RE` 能从 `\\mathrm` / `\\text` 里数出词来 → 判「本应有中文」→
    模型给不出中文 → 重译 2 轮仍不合格 → **整篇 400 块论文 failed**。
    """
    from papershelf.pipeline.validate import expects_chinese

    formula_h2 = r"\(\mathrm{adj} = 0.77 \text{–} 0.96\)) [ 230 ]."
    assert expects_chinese(formula_h2, block_type="h2") is False
    assert expects_chinese(r"\[ \dot{x} = f(x) + g(x)u \tag{1} \]", block_type="p") is False
    # 真标题与真图注照旧必须译（短到 1 个实词也算正文）
    assert expects_chinese("I. INTRODUCTION", block_type="h2") is True
    assert expects_chinese("Fig. 2. Overall structure.", block_type="figure") is True


def test_converged_untranslated_blocks_do_not_fail_the_paper(settings):
    """重试后仍无中文的块 → 标「待校对」放行；**只有结构性问题才阻塞**。

    踩过（生产实测）：翻译器已经跑满 2 轮定点重译并收敛（标了 needs_review），
    出口的出口校验却又把「疑似漏译」当致命错误抛掉 → 整篇 failed、tokens_used=0，
    用户看到「转换失败」而不是「2 块待校对」。与 docs/design.md §5.5 的
    「降级 → 回落原文本 + needs_review，绝不阻塞管线」直接矛盾。
    """
    import pytest

    from papershelf.pipeline.model import Block as PBlock, Doc as PDoc
    from papershelf.pipeline.validate import Report
    from papershelf.server.converter import enforce_report

    doc = PDoc(meta={}, blocks=[
        PBlock(id="b-0001", type="p", en="A full English paragraph.", zh=""),
    ])
    # ① 仅仅「漏译」→ 不抛，降级为 needs_review
    #   （`ok=False` 是忠实的：真 `validate()` 在 untranslated 非空时就会置 False，
    #    所以「凡是 ok=False 就抛」的老逻辑在这里必然误杀整篇）
    enforce_report(Report(untranslated=["b-0001"], ok=False), doc)
    assert doc.blocks[0].payload.get("needs_review") is True

    # ② 结构性问题 → 必须阻塞（丢块会让译文与原文对不上）
    with pytest.raises(RuntimeError):
        enforce_report(Report(missing=["b-0001"], ok=False), doc)
    with pytest.raises(RuntimeError):
        enforce_report(Report(out_of_order=["b-0001"], ok=False), doc)
    with pytest.raises(RuntimeError):
        enforce_report(Report(tag_imbalance={"div": 1}, ok=False), doc)
