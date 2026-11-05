"""`pending_job` 的类不变量：**排队时声明，终态清掉，状态迁移原样留着**。

## 这一列是什么

`papers.pending_job` 回答「轮到它时做什么」（`None` = 完整转换 / `"refs"` = 只重建文末
参考文献 / `"assets"` = 只把缺的插图补回来）。它**跨请求存活** —— 用户点一下按钮写进去，
真正的执行发生在常驻队列的下一次轮询里（`converter.convert_paper` 认领后按它分派）。

正因为它跨请求存活，「写」与「清」必须成对：漏清一次，残留的声明就会把**下一次**排队
变成**上一次**的活 —— 「修文献 → 用户又点重新转换 → 实际又跑了一遍修文献」，静默且难查。

## 两条不变量（本文件钉住的就是这两条）

1. **每个终态写入都必须清掉它**（`conv_state=done`/`failed`）。实现有两个载体：
   `_set_state`（`failed`、`done`、以及经它的 `_set_decline_state`）与成功收尾的
   `_writeback_meta`。**后者原先漏了** —— `done` 的行可以带着 `pending_job='refs'`
   躺在库里、在回答一个早已结束的问题。
2. **每个排队写入都必须声明它**，入口只有一个：`enqueue()`（`job=None` 也是一个声明，
   它的意思正是"清成完整转换"）。四个排队入口目前都记得显式写 `job` —— 但那是约定。

## 两个**故意**不清它的地方

- `claim_paper`（`queued → doing`）与 `recover_stuck`（`doing → queued`）都**原样保留**：
  前者认领之后要照它分派，后者是崩溃恢复 —— 重启后必须接着做完**同一件事**。
  把它们「顺手清掉」会让重启把一篇修文献变成整篇重跑（百万级 token）。
  这两条也在这里钉住，否则下一个人会把"清掉"推广过头。

## 为什么在修之前是红的

`_writeback_meta` 是**唯一**不清该列的终态写入。它今天还没造成事故，只因为四个排队入口
都记得声明 `job`（`enqueue(..., job=None)` 会把它清成 NULL）—— 也就是说，
这条不变量一直靠**约定**，而约定漏一处就是上面那句坏结局。本文件把它变成**结构**。
"""

from __future__ import annotations

import pytest


def _seed(settings, **cols) -> int:
    """插一行 papers，返回 id。默认形态 = 一篇**正在跑**、且声明了 `job='refs'` 的文献。

    这就是缺陷的形状：`refs` 请求落在中途后，成功收尾会把它一起带进 `done`。
    """
    from papershelf.server.db import connect

    row = {"title": "original", "title_is_placeholder": 1,
           "pdf_path": "/data/papers/2026-11-05T120000+0000_original.pdf",
           "conv_state": "doing", "pending_job": "refs"}
    row.update(cols)
    conn = connect(settings)
    try:
        conn.execute("INSERT OR IGNORE INTO users (email,password_hash,status,created_at) "
                     "VALUES ('u@x.edu.cn','h','active',datetime('now'))")
        uid = conn.execute("SELECT id FROM users WHERE email='u@x.edu.cn'").fetchone()["id"]
        conn.execute("INSERT INTO plans (user_id,name,created_at) VALUES (?,?,datetime('now'))",
                     (uid, "计划"))
        plan_id = conn.execute(
            "SELECT id FROM plans ORDER BY id DESC LIMIT 1").fetchone()["id"]
        cur = conn.execute(
            f"INSERT INTO papers (plan_id, source, status, created_at, updated_at, "
            f"{', '.join(row)}) "
            f"VALUES (?, 'upload', 'unread', datetime('now'), datetime('now'), "
            f"{', '.join('?' * len(row))})",
            (plan_id, *row.values()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _state(settings, pid: int) -> dict:
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        return dict(conn.execute(
            "SELECT conv_state, conv_error, tokens_used, pending_job, title "
            "FROM papers WHERE id=?", (pid,)).fetchone())
    finally:
        conn.close()


def _current(conn, pid: int) -> dict:
    """`_writeback_meta` 要的那份行快照（`cli.py` 的调用形状就是这样）。"""
    return dict(conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone())


# ── ① 成功收尾必须清掉：`done` 的行不许再带着"轮到它时做什么" ─────────────
def test_the_success_settle_clears_a_stale_job(settings):
    """**这一条在修之前是红的**：`conv_state` 写成 `done`，`pending_job` 原样留着。

    收尾该做的事一件都没少：状态、token 记账、元数据写回都在，只是多清了这一列。
    """
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    pid = _seed(settings)
    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, _current(conn, pid), {"title_en": "A settled paper"}, 1234)
    finally:
        conn.close()

    st = _state(settings, pid)
    assert st["conv_state"] == "done"
    assert st["tokens_used"] == 1234, "token 记账被这次修改吃掉了"
    assert st["title"] == "A settled paper", "元数据写回被这次修改吃掉了"
    assert st["pending_job"] is None, (
        "成功收尾把 conv_state 写成 done，却留下 pending_job='refs' —— "
        "这一列在回答「轮到它时做什么」，而这篇已经没有任何「轮到它」的时刻了")


# ── ② 类不变量：**每个**终态写入都清，不只是刚才那一个 ──────────────────
@pytest.mark.parametrize("carrier", [
    "writeback_meta",        # 成功收尾（原先漏的就是这一个）
    "_set_state done",       # 修不了但产物在（`_set_decline_state` 走这里）
    "_set_state failed",     # 转换失败 / 产物真的不在
    "_set_decline_state",    # 「这一次修不了」—— 没有产物 ⇒ failed
])
def test_every_terminal_write_clears_the_column(settings, carrier):
    """新增一个终态载体而不清这一列，这里就该多一条红。

    参数表就是"终态写入到底有几个"的清单：谁新加了载体，就往这里加一行 ——
    加的那一行会立刻告诉你它有没有清。
    """
    from papershelf.server.converter import _set_decline_state, _set_state, _writeback_meta
    from papershelf.server.db import connect

    pid = _seed(settings)
    conn = connect(settings)
    try:
        if carrier == "writeback_meta":
            _writeback_meta(conn, pid, _current(conn, pid), {}, 0)
            expected = "done"
        elif carrier == "_set_state done":
            _set_state(conn, pid, "done", None)
            expected = "done"
        elif carrier == "_set_state failed":
            _set_state(conn, pid, "failed", "boom")
            expected = "failed"
        else:
            _set_decline_state(conn, pid, "没有可修的产物")   # 没有产物 ⇒ failed
            expected = "failed"
    finally:
        conn.close()

    st = _state(settings, pid)
    assert st["conv_state"] == expected
    assert st["pending_job"] is None, f"{carrier} 落了终态却没清掉 pending_job"


# ── ③ 排队时**声明**而不是继承：`enqueue()` 是唯一的重排入口 ─────────────
def test_a_requeue_declares_the_job_instead_of_inheriting_it(settings):
    """`job=None` 是一个**声明**（"完整转换"），不是"沿用上一轮"。

    这条是 ㊹修订二 那条硬约束的正面：入口记得声明，残留值就劫持不了下一次排队。
    （反面 —— 漏声明会劫持 —— 由 `test_assets_rebuild.py` ⑥ 从用户操作那一侧钉住。）
    """
    from papershelf.server.converter import enqueue
    from papershelf.server.db import connect

    pid = _seed(settings, conv_state="done")          # 上一轮留下的 job='refs'
    conn = connect(settings)
    try:
        enqueue(conn, pid, reset_attempts=True)       # 完整转换
        st = _state(settings, pid)
        assert (st["conv_state"], st["pending_job"]) == ("queued", None), \
            "job=None 没有清掉残留声明 —— 下一次排队会被它劫持"

        enqueue(conn, pid, job="refs", reset_attempts=True)
    finally:
        conn.close()
    st = _state(settings, pid)
    assert (st["conv_state"], st["pending_job"]) == ("queued", "refs")


# ── ④ 两个状态迁移**故意**保留声明（别把"清掉"推广过头）────────────────
def test_the_two_state_transitions_keep_the_declaration(settings):
    """`claim_paper`（认领）与 `recover_stuck`（启动恢复）都必须原样留着它。

    认领后要照它分派；崩溃恢复要接着做**同一件事**。这里若被"顺手清掉"，
    一次重启就会把「重建参考文献」变成整篇重跑 —— 百万级 token 的错误。
    """
    from papershelf.server.converter import claim_paper
    from papershelf.server.db import connect
    from papershelf.server.queue import recover_stuck

    pid = _seed(settings, conv_state="queued", pending_job="refs")
    conn = connect(settings)
    try:
        assert claim_paper(conn, pid) is True
        st = _state(settings, pid)
        assert st["conv_state"] == "doing"
        assert st["pending_job"] == "refs", "认领抹掉了声明 ⇒ 认领后按 job 分派的依据没了"

        assert recover_stuck(conn) == 1
    finally:
        conn.close()

    st = _state(settings, pid)
    assert st["conv_state"] == "queued", "启动恢复必须把它放回队列"
    assert st["pending_job"] == "refs", "启动恢复抹掉了声明 ⇒ 重启后跑的是另一件事"
