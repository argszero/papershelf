"""「最近阅读」+「待读 → 在读」自动翻转（⑰ 补充，㊱，2026-09-15）。

宿主原话：「我有篇文章读了 13%，为什么『在读』还是 0 篇？」「那么，什么时候变成 unread 呢？」

⑰ 定的是**两层信号互不绑定**：百分比在阅读器里滚动自动累计，四态状态只能手动标。
副作用是 —— 只要没拖过看板，读 90% 也一直躺在「待读」列，「在读」永远 0 篇。
宿主判定「是」（读过、还没读完 = 在读），于是补上这条：

- **「待读 → 在读」自动翻转**：阅读器滚动上报进度 = 此刻正在读，这就是那个信号。
  「读懂没有」仍然只有人知道 → 「已读/已整理」**不自动**，⑰ 的手动优先原样保留。
- **新增 `last_read_at`**：「最近阅读」的唯一数据来源。**只有滚动上报写它** ——
  改标题/改标签/拖看板都不写，所以它只可能表示"最后一次真正阅读"。
  （不能复用 `updated_at`：那个被任何 PATCH 刷新，记的是"最近一次改动"。）

本文件钉住这些边界；每一条都对应一个真的会出错的写法。
"""

from __future__ import annotations

import io

OLD = "2020-01-01T00:00:00+00:00"


# ── 脚手架 ──────────────────────────────────────────────────────────────
def _row(settings, pid: int) -> dict:
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        return dict(conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone())
    finally:
        conn.close()


def _set(settings, pid: int, **cols) -> None:
    """直接把某几列改成**可辨识的旧值**。

    ⚠️ 为什么必须这么写：时间戳是**秒级**的，两次紧挨着的 PATCH 会落在同一秒 ——
    那样「`status_at` 没被重新盖」这条断言**即使代码写错了也会通过**。
    钉成 2020 年才分得清"原地不动"与"又盖了一次"。
    """
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        conn.execute(f"UPDATE papers SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                     (*cols.values(), pid))
        conn.commit()
    finally:
        conn.close()


def _start(settings, make_user, client, email="owner@tsinghua.edu.cn") -> int:
    """登录 + 建计划 + 上传一份假 PDF（`background_convert=false`，不触发转换）。

    走**真实上传路由**而不是手写 INSERT：这样"导入即待读、且没有阅读时间"这条断言
    才是在验证生产路径，而不是在验证我在测试里抄的那份 SQL。
    """
    email, pw = make_user(email)
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    up = client.post(f"/api/plans/{plan_id}/papers/upload",
                     files={"files": ("目标文献.pdf", io.BytesIO(b"%PDF-1.4\n%fake"),
                                      "application/pdf")},
                     data={"background_convert": "false"})
    assert up.status_code == 201, up.text
    return int(up.json()[0]["id"])


# ── 导入与自动翻转 ──────────────────────────────────────────────────────
def test_import_starts_unread_and_never_read(client, settings, make_user):
    """导入的那一刻 = 待读 + 无阅读时间（这就是「什么时候变成 unread」的答案之一）。"""
    pid = _start(settings, make_user, client)
    r = client.get(f"/api/plans/1/papers").json()[0]
    assert r["id"] == pid
    assert r["status"] == "unread" and r["progress"] == 0
    assert r["last_read_at"] is None, "从没读过就不该有阅读时间（宁缺勿编）"
    assert r["status_at"], "导入时盖 status_at（= 进入「待读」的时刻）"


def test_scrolling_flips_unread_to_reading(client, settings, make_user):
    """读一次 → 自动「待读 → 在读」，同时记下「最近阅读」。"""
    pid = _start(settings, make_user, client)
    _set(settings, pid, status_at=OLD)
    r = client.patch(f"/api/papers/{pid}", json={"progress": 13}).json()
    assert r["progress"] == 13
    assert r["status"] == "reading", "滚动上报 = 正在读，必须自动翻到「在读」"
    assert r["status_at"] != OLD, "翻转这一刻要盖 status_at"
    assert r["last_read_at"], "滚动上报必须记下最近阅读时间"


def test_status_at_is_stamped_only_on_the_flip(client, settings, make_user):
    """`status_at` 只在**翻转那一刻**盖一次，不能每次滚动都盖。

    否则它退化成"最近滚动时间"：看板列内排序（按 `status_at`）会让正在读的文献
    在列里反复乱跳，总览那条"停摆"提醒也再也读不出"停了多久"。
    """
    pid = _start(settings, make_user, client)
    client.patch(f"/api/papers/{pid}", json={"progress": 5})
    _set(settings, pid, status_at=OLD, last_read_at=OLD)
    r = client.patch(f"/api/papers/{pid}", json={"progress": 40}).json()
    assert r["status"] == "reading"
    assert r["status_at"] == OLD, "已经在「在读」，再滚动不得重盖 status_at"
    assert r["last_read_at"] != OLD, "但每次滚动都要刷新最近阅读"


def test_backwards_scroll_refreshes_last_read_but_never_progress(client, settings, make_user):
    """⑰ 的「只增不减」照旧，但**翻回来也算在读**。

    踩过：从文末快速滚回顶部，最后那次上报会把 100% 拽回 3%，进度条看着像坏了。
    所以 `progress` 不接受回退值 —— 但 `last_read_at` 要更新：
    用户此刻确实在看着这篇，"最近阅读"若不刷新，看板上会误报成停摆。
    """
    pid = _start(settings, make_user, client)
    client.patch(f"/api/papers/{pid}", json={"progress": 80})
    _set(settings, pid, last_read_at=OLD)
    r = client.patch(f"/api/papers/{pid}", json={"progress": 3}).json()
    assert r["progress"] == 80, "自动进度不得回退"
    assert r["last_read_at"] != OLD, "翻回来也是在读，最近阅读要刷新"


# ── 手动优先（⑰ 原样保留）────────────────────────────────────────────────
def test_manual_status_is_not_overwritten_by_scrolling(client, settings, make_user):
    """手动标过的状态不被滚动推翻；标回「未读」则进度清零、下次滚动重新开始读。"""
    pid = _start(settings, make_user, client)
    r = client.patch(f"/api/papers/{pid}", json={"status_": "reading"}).json()
    manual_at = r["status_at"]
    assert client.patch(f"/api/papers/{pid}", json={"progress": 7}).json()["status_at"] == manual_at, \
        "手动标「在读」的时刻不能被随后的滚动改写"

    r = client.patch(f"/api/papers/{pid}", json={"status_": "unread"}).json()
    assert (r["status"], r["progress"], r["progress_mode"]) == ("unread", 0, "auto"), \
        "标回「未读」要把进度清零，否则出现『未读 但 80%』的自相矛盾"
    assert client.patch(f"/api/papers/{pid}", json={"progress": 1}).json()["status"] == "reading", \
        "清零后重新开始读 → 再次自动翻到「在读」"


def test_read_lock_rejects_scrolling_and_keeps_last_read(client, settings, make_user):
    """标「已读」后进度锁定：滚动被 409 拒，且**不得**留下阅读痕迹。

    若这条写漏，`last_read_at` 会在"已读"的文献上继续跳动 —— 那它就不再是阅读信号了。
    """
    pid = _start(settings, make_user, client)
    r = client.patch(f"/api/papers/{pid}", json={"status_": "read"}).json()
    assert (r["progress"], r["progress_mode"]) == (100, "manual")
    _set(settings, pid, last_read_at=OLD)
    assert client.patch(f"/api/papers/{pid}", json={"progress": 50}).status_code == 409
    assert _row(settings, pid)["last_read_at"] == OLD, "被拒的上报不许写任何东西"
    assert _row(settings, pid)["status"] == "read"


def test_only_scrolling_writes_last_read_at(client, settings, make_user):
    """「最近阅读」只有滚动能写 —— 改标题/标签/状态都不许碰它。

    这正是不能复用 `updated_at` 的原因：后者被任何 PATCH 刷新，记的是"最近一次改动"。
    """
    pid = _start(settings, make_user, client)
    client.patch(f"/api/papers/{pid}", json={"progress": 9})
    stamp = _row(settings, pid)["last_read_at"]
    assert stamp
    for body in ({"title": "改个名"}, {"tags": ["综述"]}, {"authors": "张三"},
                 {"status_": "reading"}, {"venue": "某期刊"}, {"year": 2024}):
        client.patch(f"/api/papers/{pid}", json=body)
    assert _row(settings, pid)["last_read_at"] == stamp, "只有滚动上报能刷新「最近阅读」"


# ── 手动修改进度（㊺，2026-09-19，宿主选 B）──────────────────────────────
#
# 宿主：「还需要支持手动修改进度」。后端一直支持写 `progress`，缺的是**语义**：
# 同一个字段有**两种含义** —— "滚到过哪里"（自动累计）与"我说它是多少"（手改），
# 而它们发的 JSON 长得一模一样（`{"progress": 30}`），**靠值分不清**。
# 于是新增 `progress_by`：`"user"` = 手改，缺省 = 滚动上报。猜错的后果不是报错，
# 而是**把 ⑰ 那条"只增不减"的护栏拆掉**（它挡的是"进度条自己往回退"）。宿主选 **B**：
# 手改**不锁**（`progress_mode` 仍是 `auto`），它只是把数字修正一下。
def _user_edit(client, pid: int, value: int):
    return client.patch(f"/api/papers/{pid}", json={"progress": value, "progress_by": "user"})


def test_user_edit_can_go_backwards(client, settings, make_user):
    """手改能往下改 —— 这就是这个功能的全部意义（滚动上报仍然不许回退）。"""
    pid = _start(settings, make_user, client)
    client.patch(f"/api/papers/{pid}", json={"progress": 80})
    assert _user_edit(client, pid, 30).json()["progress"] == 30
    assert client.patch(f"/api/papers/{pid}", json={"progress": 5}).json()["progress"] == 30, \
        "同一条护栏要按「谁在报数」分流：滚动照旧只增不减"


def test_user_edit_is_not_evidence_of_reading(client, settings, make_user):
    """填一个数字 **不等于** 正在读：不翻状态、不盖 `status_at`、不写 `last_read_at`。

    否则「最近阅读」会退化成"最近改过进度的时间戳"，它存在的意义就没了。
    """
    pid = _start(settings, make_user, client)
    _set(settings, pid, status_at=OLD)
    r = _user_edit(client, pid, 50).json()
    assert r["progress"] == 50
    assert r["status"] == "unread", "手改不是「在读」的证据"
    assert r["status_at"] == OLD, "更不该盖「进入该状态的时刻」"
    assert r["last_read_at"] is None, "「最近阅读」只有滚动上报能写"


def test_user_edit_keeps_auto_so_scroll_still_accumulates(client, settings, make_user):
    """方案 **B** 的代价，明确写进测试：改小之后继续往下滚会被抬回去（宿主已知情）。"""
    pid = _start(settings, make_user, client)
    assert _user_edit(client, pid, 20).json()["progress_mode"] == "auto", "B = 手改不锁"
    assert client.patch(f"/api/papers/{pid}", json={"progress": 60}).json()["progress"] == 60
    assert _user_edit(client, pid, 10).json()["progress"] == 10
    assert client.patch(f"/api/papers/{pid}", json={"progress": 60}).json()["progress"] == 60, \
        "代价：一滚动就被拉回原值（选 B 时就接受了）"


def test_user_edit_is_allowed_after_manual_lock(client, settings, make_user):
    """已标「已读」时，**手改**不被 409 挡（手改是人的明确指令），但滚动上报照旧被挡。"""
    pid = _start(settings, make_user, client)
    client.patch(f"/api/papers/{pid}", json={"status_": "read"})
    r = _user_edit(client, pid, 30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["progress"] == 30
    assert body["progress_mode"] == "manual", "手改不解除「手动优先」，只是它自己也算手动"
    assert client.patch(f"/api/papers/{pid}", json={"progress": 50}).status_code == 409


def test_progress_by_rejects_unknown_values(client, settings, make_user):
    """取值必须显式可验：拼错的 `progress_by` 若被静默当成滚动，功能会"看着像没生效"。"""
    pid = _start(settings, make_user, client)
    assert client.patch(f"/api/papers/{pid}",
                        json={"progress": 10, "progress_by": "scroll"}).status_code == 200
    assert client.patch(f"/api/papers/{pid}",
                        json={"progress": 10, "progress_by": "banana"}).status_code == 400


# ── 迁移 ────────────────────────────────────────────────────────────────
def test_migration_backfills_only_rows_with_reading_evidence(settings, make_user, client):
    """存量行迁移：**有证据的才回填**，没证据的一律留 NULL。

    `progress > 0` 只可能来自滚动上报（阅读器是唯一来源），而滚动上报恰好会刷新
    `updated_at`，所以对这些行它是最接近的近似值 —— 留空反而会出现
    "进度 13% 但最近阅读：未读"的自相矛盾。`progress = 0` 的行不许编时间戳。
    """
    from papershelf.server.db import _migrate, connect

    read = _start(settings, make_user, client, "a@tsinghua.edu.cn")
    never = _start(settings, make_user, client, "b@tsinghua.edu.cn")
    conn = connect(settings)
    try:
        conn.execute("UPDATE papers SET status='reading', progress=13, updated_at=? WHERE id=?",
                     (OLD, read))
        conn.execute("UPDATE papers SET status='unread', progress=0, updated_at=? WHERE id=?",
                     (OLD, never))
        # 模拟**真正的旧库**：必须真删列，否则 `_migrate` 的幂等守卫会让整段跳过
        # （守卫是有意的，不是缺陷）——那样测试通过的全是别的分支。
        conn.execute("ALTER TABLE papers DROP COLUMN last_read_at")
        conn.commit()
        assert "last_read_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
        _migrate(conn)
        got = {r["id"]: r["last_read_at"] for r in conn.execute("SELECT id,last_read_at FROM papers")}
    finally:
        conn.close()
    assert got[read] == OLD
    assert got[never] is None


def test_share_and_login_paper_shape_stay_identical(client, settings, make_user):
    """⑩：多了 `last_read_at`，两条路径（登录态 / 匿名分享）必须**同时**带上它。

    只加一边的坏法不是报错而是 `undefined` —— TypeScript 信类型声明，运行时不会喊。
    """
    pid = _start(settings, make_user, client)
    client.patch(f"/api/papers/{pid}", json={"progress": 13})
    plan_id = 1
    tok = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 1}).json()["token"]
    anon = client.__class__(client.app_ref)

    mine = client.get(f"/api/plans/{plan_id}/papers").json()[0]
    shared = next(p for p in anon.get(f"/api/shares/{tok}").json()["papers"] if p["id"] == pid)
    assert set(shared) == set(mine), "分享 papers 与登录态 papers 的字段集必须一致"
    assert shared["last_read_at"] == mine["last_read_at"], "分享侧也要能看到「最近阅读」"
    assert shared["last_read_at"], "滚过一次就该有值"
